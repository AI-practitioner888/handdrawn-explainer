"""Apply reviewed set operations to the authored project, never to a build copy."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from validate import validate_document

PATCH_SCHEMA = 'handdrawn-explainer/patch/1'
COMMON_FIELDS = {'color', 'opacity', 'transform', 'startMs', 'durationMs',
                 'minDurationMs', 'tool', 'instant', 'locked'}
TYPE_FIELDS = {
    'text': {'text', 'x', 'y', 'size', 'align'},
    'stroke': {'d', 'width', 'wobble'},
    'fill': {'d'},
    'hatch': {'d', 'angle', 'spacing', 'width', 'bounds'},
    'crosshatch': {'d', 'angle', 'crossAngle', 'spacing', 'width', 'bounds'},
    'paint': {'d', 'angle', 'brushSize', 'spacing', 'bounds'},
    'image': {'x', 'y', 'width', 'inkPhase', 'brushSize'},
    'erase': {'region', 'brushSize'},
}
SPEECH_FIELDS = {'text', 'spokenText', 'startMs', 'endMs', 'voice', 'rate', 'pitch',
                 'audioFile', 'trimSilence', 'normalize', 'gainDb'}
SYNTHESIS_FIELDS = {'text', 'spokenText', 'voice', 'rate', 'pitch'}


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    raw = Path(path).read_bytes()
    return json.loads(raw.decode('utf-8-sig')), raw


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')


def authored_project(doc, path):
    if 'toolTrack' in doc or 'serialSchedule' in doc or Path(path).name == 'timeline.json':
        raise ValueError('Use the authored project.json, not a scheduled timeline')
    # A source.project.json may be the only authored source in a moved portable
    # bundle. Reject scheduled documents by content, not the portable filename.
    validate_document(doc, Path(path).parent)


def scopes(doc):
    result = {'global': {'id': 'global', 'startMs': 0, 'endMs': doc['durationMs'],
                         'elements': doc.get('elements', [])}}
    result.update({scene['id']: scene for scene in doc['scenes']})
    return result


def number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    if minimum is not None and value < minimum:
        raise ValueError(f'{name} must be >= {minimum}')
    if maximum is not None and value > maximum:
        raise ValueError(f'{name} must be <= {maximum}')


def validate_value(field, value, speech=False):
    if field in {'transform', 'region', 'bounds'}:
        allowed = {'x', 'y', 'scale'} if field == 'transform' else {'x', 'y', 'width', 'height'}
        if not isinstance(value, dict) or not value or set(value)-allowed:
            raise ValueError(f'{field}: only {sorted(allowed)} may be set')
        for key, item in value.items():
            number(item, f'{field}.{key}', .001 if key in {'width', 'height', 'scale'}
                   else (None if field == 'transform' else 0))
    elif field in {'instant', 'locked', 'trimSilence', 'normalize'}:
        if not isinstance(value, bool):
            raise ValueError(f'{field} must be a boolean')
    elif field in {'text', 'spokenText', 'd', 'color', 'voice', 'rate', 'pitch', 'audioFile'}:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{field} must be a nonempty string')
    elif field == 'align':
        if value not in {'left', 'center', 'right'}:
            raise ValueError('align must be left, center, or right')
    elif field == 'tool':
        if value not in {'pencil', 'pen', 'marker', 'eraser'}:
            raise ValueError('Unsupported tool')
    elif field == 'opacity' or field == 'inkPhase':
        number(value, field, 0, 1)
    elif field in {'angle', 'crossAngle', 'gainDb'}:
        number(value, field)
    else:
        number(value, field, .001 if field in {'size', 'width', 'spacing', 'brushSize'} else 0)


def validate_plan(plan, doc, plan_path):
    if not isinstance(plan, dict) or plan.get('durationMs') != doc['durationMs']:
        raise ValueError('Audio plan durationMs must equal project durationMs')
    if plan.get('mode', 'auto') not in {'auto', 'edge', 'files', 'silent'}:
        raise ValueError('Unsupported audio plan mode')
    number(plan.get('maxSpeed', 1.15), 'maxSpeed', 1, 1.5)
    segments = plan.get('segments', [])
    if not isinstance(segments, list):
        raise ValueError('Audio plan segments must be an array')
    ids = set()
    for segment in segments:
        sid = segment.get('id')
        if not isinstance(sid, str) or not sid or sid in ids:
            raise ValueError('Narration segment IDs must be stable, unique nonempty strings')
        ids.add(sid)
        number(segment.get('startMs'), f'{sid}.startMs', 0)
        number(segment.get('endMs'), f'{sid}.endMs', 0, doc['durationMs'])
        if segment['endMs'] <= segment['startMs']:
            raise ValueError(f'{sid}: invalid narration interval')
        for key in SPEECH_FIELDS.intersection(segment):
            validate_value(key, segment[key], speech=True)
        if segment.get('sceneId') and segment['sceneId'] not in scopes(doc):
            raise ValueError(f'{sid}: unknown narration sceneId')
        if plan.get('mode') == 'files' and not segment.get('audioFile'):
            raise ValueError(f'{sid}: files mode requires audioFile')
        if plan.get('mode') != 'silent' and not segment.get('audioFile') and not segment.get('spokenText', segment.get('text')):
            raise ValueError(f'{sid}: provide narration text or audioFile')
    ordered = sorted(segments, key=lambda s: s['startMs'])
    for left, right in zip(ordered, ordered[1:]):
        if left['endMs'] > right['startMs']:
            raise ValueError(f'Overlapping narration intervals: {left["id"]}, {right["id"]}')
    if plan.get('mode') != 'silent' and not segments:
        raise ValueError('Narration plan has no segments; use mode silent explicitly')


def generated_provenance(segment):
    """An edge/auto plan can still import a recording; its mode is not provenance."""
    records = [segment]
    records += [segment[key] for key in ('provenance', 'audioProvenance')
                if isinstance(segment.get(key), dict)]
    for record in records:
        provider = str(record.get('provider', '')).lower()
        if provider in {'edge-tts', 'openai-tts', 'qwen-tts', 'azure-tts'}:
            return True
        if record.get('generated') is True or record.get('kind') in {'tts', 'generated'}:
            return True
    return False


def set_fields(obj, values, prefix):
    changes = []
    for field, value in values.items():
        if isinstance(value, dict):
            existing = obj.get(field, {})
            if not isinstance(existing, dict):
                raise ValueError(f'{prefix}.{field} is not an object')
            merged = copy.deepcopy(existing)
            for key, item in value.items():
                if key not in merged or merged[key] != item:
                    changes.append({'field': f'{field}.{key}', 'before': merged.get(key), 'after': item})
                    merged[key] = copy.deepcopy(item)
            obj[field] = merged
        elif field not in obj or obj[field] != value:
            changes.append({'field': field, 'before': obj.get(field), 'after': value})
            obj[field] = copy.deepcopy(value)
    return changes


def apply_patch(doc, patch, project_path, plan=None, plan_path=None, project_hash=None, plan_hash=None):
    if patch.get('schemaVersion') != PATCH_SCHEMA:
        raise ValueError(f'Patch schemaVersion must be {PATCH_SCHEMA}')
    if patch.get('baseProjectSha256') != project_hash:
        raise ValueError('Stale patch: baseProjectSha256 does not match the authored project')
    operations = patch.get('operations')
    if not isinstance(operations, list) or not operations:
        raise ValueError('Patch operations must be a nonempty array')
    result, audio = copy.deepcopy(doc), copy.deepcopy(plan)
    scene_map, changes, seen_fields = scopes(result), [], set()
    for i, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise ValueError(f'Operation {i+1} must be an object')
        if set(operation)-{'op', 'target', 'sceneId', 'elementId', 'segmentId', 'set', 'regenerate'}:
            raise ValueError(f'Operation {i+1}: unsupported operation fields')
        if operation.get('op', 'set') != 'set':
            raise ValueError('Only set operations are supported')
        target, scene_id, values = operation.get('target'), operation.get('sceneId'), operation.get('set')
        if scene_id not in scene_map:
            raise ValueError(f'Unknown sceneId: {scene_id}')
        if not isinstance(values, dict) or not values:
            raise ValueError('Every operation needs a nonempty set object')
        if target == 'visual':
            if 'segmentId' in operation or 'regenerate' in operation:
                raise ValueError('Speech fields cannot be used in a visual operation')
            identifier = operation.get('elementId')
            element = next((e for e in scene_map[scene_id]['elements'] if e['id'] == identifier), None)
            if element is None:
                raise ValueError(f'Unknown elementId: {scene_id}/{identifier}')
            allowed = COMMON_FIELDS | TYPE_FIELDS[element['type']]
            obj = element
        elif target == 'speech':
            if 'elementId' in operation:
                raise ValueError('Use segmentId for speech operations')
            if audio is None or plan_path is None:
                raise ValueError('Speech edits require --audio-plan')
            if patch.get('baseAudioPlanSha256') != plan_hash:
                raise ValueError('Stale audio patch: baseAudioPlanSha256 does not match the narration plan')
            identifier = operation.get('segmentId')
            obj = next((s for s in audio.get('segments', []) if s['id'] == identifier), None)
            if obj is None:
                raise ValueError(f'Unknown segmentId: {identifier}')
            scope = scene_map[scene_id]
            if scene_id == 'global' or not (obj['startMs'] < scope['endMs'] and obj['endMs'] > scope['startMs']):
                raise ValueError(f'{identifier}: segment does not belong to scene {scene_id}')
            if obj.get('sceneId') and obj['sceneId'] != scene_id:
                raise ValueError(f'{identifier}: sceneId does not match narration binding')
            allowed = SPEECH_FIELDS
            if 'regenerate' in operation and not isinstance(operation['regenerate'], bool):
                raise ValueError('regenerate must be a boolean')
        else:
            raise ValueError(f'Unknown target: {target}')
        restricted = set(values)-allowed
        if restricted:
            raise ValueError(f'{scene_id}/{identifier}: restricted fields: {sorted(restricted)}')
        for field, value in values.items():
            validate_value(field, value, speech=target == 'speech')
            key = (target, scene_id if target == 'visual' else '', identifier, field)
            if key in seen_fields:
                raise ValueError(f'Duplicate set field: {identifier}.{field}')
            seen_fields.add(key)
        before = copy.deepcopy(obj)
        field_changes = set_fields(obj, values, f'{scene_id}/{identifier}')
        refresh = target == 'speech' and any(change['field'] in SYNTHESIS_FIELDS for change in field_changes)
        actions = []
        if target == 'speech':
            if 'text' in values and values['text'] != before.get('text') and 'spokenText' not in values and 'spokenText' in before:
                obj.pop('spokenText', None)
                field_changes.append({'field': 'spokenText', 'before': before['spokenText'], 'after': None})
                actions.append('Cleared previous pronunciation override; new text is now spoken')
            old_file = before.get('audioFile')
            replacement = values.get('audioFile')
            if replacement:
                source = (Path(plan_path).parent/replacement).resolve()
                if not source.is_file():
                    raise ValueError(f'New audioFile does not exist: {source}')
                if refresh and old_file and source == (Path(plan_path).parent/old_file).resolve():
                    raise ValueError('Changed speech needs a different audioFile or regenerate:true')
                if operation.get('regenerate'):
                    raise ValueError('Choose a new audioFile or regenerate:true, not both')
                if replacement != old_file or refresh:
                    obj['provenance'] = {'kind': 'imported', 'provider': 'supplied-file',
                                         'sourceSha256': sha256_bytes(source.read_bytes())}
                    actions.append('Use the explicitly supplied replacement audio')
            elif refresh or operation.get('regenerate'):
                if old_file and not generated_provenance(before) and operation.get('regenerate') is not True:
                    raise ValueError(f'{identifier}: existing audio has no generated provenance; provide a new audioFile or regenerate:true')
                if old_file:
                    obj.pop('audioFile', None)
                    field_changes.append({'field': 'audioFile', 'before': old_file, 'after': None})
                for stale in ('audioProvenance', 'provider', 'config', 'configSha256', 'sourceSha256', 'fileSha256'):
                    obj.pop(stale, None)
                obj['provenance'] = {'kind': 'tts', 'status': 'pending-regeneration'}
                obj['trimSilence'] = True
                if audio.get('mode') == 'files':
                    audio['mode'] = 'auto'
                    actions.append('Changed files mode to auto so edited speech can be synthesized')
                actions.append('Re-synthesize this segment on the next audio build; old audio will not be reused')
        if field_changes or actions:
            changes.append({'target': target, 'sceneId': scene_id,
                            'elementId' if target == 'visual' else 'segmentId': identifier,
                            'changes': field_changes, 'actions': actions})
    affected_speech = [change for change in changes if change['target'] == 'speech']
    if affected_speech:
        result.pop('narrationBeats', None)
        result['narrationTimingStatus'] = 'needs-realignment'
        for scene in result.get('scenes', []):
            if any(segment['id'] == change['segmentId']
                   and segment['startMs'] < scene['endMs'] and segment['endMs'] > scene['startMs']
                   for change in affected_speech for segment in (plan or {}).get('segments', [])):
                scene.pop('narrationText', None)
                for element in scene.get('elements', []):
                    element.pop('speechBeat', None)
    validate_document(result, Path(project_path).parent)
    if audio is not None:
        validate_plan(audio, result, plan_path)
    return result, audio, changes


def atomic_write_all(files):
    """Validate before staging; roll back all written documents if a replace fails."""
    previous, staged, committed = {}, {}, []
    try:
        for path, content in files.items():
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            previous[path] = path.read_bytes() if path.exists() else None
            fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp', dir=path.parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            staged[path] = Path(temporary)
        for path, temporary in staged.items():
            os.replace(temporary, path)
            committed.append(path)
    except Exception:
        for path in reversed(committed):
            if previous[path] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous[path])
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def report_for(changes, project_path, output_path, project_hash, new_hash, plan_path, plan_hash, new_plan_hash):
    visuals = [c for c in changes if c['target'] == 'visual']
    speech = [c for c in changes if c['target'] == 'speech']
    text_ids = [f'{c["sceneId"]}/{c["elementId"]}' for c in visuals
                if any(x['field'] in {'text', 'size', 'align'} for x in c['changes'])]
    return {
        'schemaVersion': 'handdrawn-explainer/revision-report/1',
        'sourceProject': str(project_path), 'outputProject': str(output_path),
        'baseProjectSha256': project_hash, 'projectSha256': new_hash,
        'audioPlan': str(plan_path) if plan_path else None,
        'baseAudioPlanSha256': plan_hash, 'audioPlanSha256': new_plan_hash,
        'changes': changes,
        'refresh': {'textGlyphs': text_ids, 'timeline': bool(visuals or speech),
                    'audioMixAndCaptions': bool(speech or visuals),
                    'reviewPreviews': bool(changes), 'video': bool(changes),
                    'generatedIllustrations': []},
        'nextSteps': ([
            'Rebuild the derived timeline and changed glyphs from the authored source; inspect the new schedule against speech.',
            'Rebuild audio mixing, captions and tool sound; source TTS is cached by spoken text and voice configuration.',
            'Render fresh review frames and the video. Existing illustration files are reused; a full render does not regenerate artwork.'
        ] if changes else ['No values changed; existing source documents and caches remain valid.']),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--patch', required=True, type=Path)
    parser.add_argument('--audio-plan', type=Path)
    parser.add_argument('--output', type=Path, help='Optional sibling authored JSON; default edits --project atomically')
    args = parser.parse_args()
    try:
        source = args.project.resolve()
        output = (args.output or source).resolve()
        if output.parent != source.parent:
            raise ValueError('--output must be beside the authored project so relative assets remain valid')
        doc, raw = read_json(source)
        authored_project(doc, source)
        patch, _ = read_json(args.patch)
        plan_path = args.audio_plan.resolve() if args.audio_plan else None
        plan, plan_raw = read_json(plan_path) if plan_path else (None, None)
        if plan is not None:
            validate_plan(plan, doc, plan_path)
        project_hash, plan_hash = sha256_bytes(raw), sha256_bytes(plan_raw) if plan_raw else None
        revised, revised_plan, changes = apply_patch(doc, patch, source, plan, plan_path, project_hash, plan_hash)
        revised_raw = json_bytes(revised) if revised != doc else raw
        revised_plan_raw = json_bytes(revised_plan) if revised_plan != plan else plan_raw
        report = report_for(changes, source, output, project_hash, sha256_bytes(revised_raw), plan_path,
                            plan_hash, sha256_bytes(revised_plan_raw) if revised_plan_raw else None)
        report_path = output.with_name(output.stem+'.revision-report.json')
        files = {report_path: json_bytes(report)}
        if output != source or revised_raw != raw:
            files[output] = revised_raw
        if plan_path and revised_plan_raw != plan_raw:
            if plan_path == output or plan_path == report_path:
                raise ValueError('Project, narration and report paths must be distinct')
            files[plan_path] = revised_plan_raw
        # Catch edits that arrived while the patch was being validated.
        if source.read_bytes() != raw or (plan_path and plan_path.read_bytes() != plan_raw):
            raise ValueError('Source changed during validation; regenerate the review and patch')
        atomic_write_all(files)
        print(json.dumps({'changedObjects': len(changes), 'project': str(output),
                          'report': str(report_path), 'refresh': report['refresh']}, ensure_ascii=False))
    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as error:
        parser.exit(2, f'Revision refused: {error}\n')


if __name__ == '__main__':
    main()
