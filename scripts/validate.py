"""Validate the supported scene contract; render.py verifies actual frames."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path

TYPES = {'text', 'stroke', 'fill', 'hatch', 'crosshatch', 'paint', 'image', 'erase'}
TOOLS = {'pencil', 'eraser', 'pen', 'marker'}

def finite(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f'{label} must be finite and >= {minimum}')
    return value

def validate_document(doc, root: Path | None = None, scheduled=False):
    if not isinstance(doc, dict):
        raise ValueError('Project must be an object')
    duration = finite(doc.get('durationMs'), 'durationMs', 1)
    canvas = doc.get('canvas', {'width': 1280, 'height': 720})
    for key in ('width', 'height'):
        value = finite(canvas.get(key), f'canvas.{key}', 100)
        if int(value) != value or value % 2:
            raise ValueError(f'canvas.{key} must be an even integer for H.264')
    fps = finite(doc.get('fps', 30), 'fps', 1)
    if fps > 60 or fps != int(fps):
        raise ValueError('fps must be an integer from 1 to 60')
    scenes = doc.get('scenes')
    if not isinstance(scenes, list) or not scenes:
        raise ValueError('At least one scene is required')
    scopes = [('global', 0, duration, doc.get('elements', []))]
    scene_ids, end = set(), 0
    for scene in scenes:
        sid = scene.get('id')
        if not isinstance(sid, str) or not sid or sid == 'global' or sid in scene_ids:
            raise ValueError(f'Invalid/duplicate scene id: {sid}')
        scene_ids.add(sid)
        start = finite(scene.get('startMs'), f'{sid}.startMs')
        stop = finite(scene.get('endMs'), f'{sid}.endMs')
        if abs(start-end) > .01 or stop <= start or stop > duration:
            raise ValueError(f'{sid}: scenes must cover duration contiguously in display order')
        scopes.append((sid, start, stop, scene.get('elements', [])))
        end = stop
    if abs(end-duration) > .01:
        raise ValueError('The last scene must end at durationMs')
    lookup = {}
    count = 0
    for sid, offset, stop, elements in scopes:
        if not isinstance(elements, list):
            raise ValueError(f'{sid}.elements must be an array')
        for e in elements:
            eid = e.get('id')
            if not isinstance(eid, str) or not eid or (sid, eid) in lookup:
                raise ValueError(f'{sid}: element ids must be nonempty and unique')
            lookup[sid, eid] = e
            kind = e.get('type')
            if kind not in TYPES:
                raise ValueError(f'{eid}: unsupported type {kind}')
            start = finite(e.get('startMs', 0), f'{eid}.startMs')
            length = 0 if e.get('instant') else finite(e.get('durationMs', 0), f'{eid}.durationMs')
            if offset + start + length > stop + .01:
                raise ValueError(f'{eid}: animation exceeds scene duration')
            if e.get('tool', doc.get('defaultTool', 'pencil')) not in TOOLS:
                raise ValueError(f'{eid}: unsupported tool')
            for field in ('width', 'brushSize', 'spacing'):
                if field in e:
                    finite(e[field], f'{eid}.{field}', .001)
            if 'opacity' in e and (finite(e['opacity'], f'{eid}.opacity') > 1):
                raise ValueError(f'{eid}: opacity must be between 0 and 1')
            if 'transform' in e:
                finite(e['transform'].get('scale', 1), f'{eid}.transform.scale', .001)
            if kind == 'text':
                if not isinstance(e.get('text'), str) or not e['text']:
                    raise ValueError(f'{eid}: text is empty')
                finite(e.get('size'), f'{eid}.size', 1)
                for coord in ('x', 'y'):
                    finite(e.get(coord), f'{eid}.{coord}')
            if kind in {'stroke', 'fill', 'hatch', 'crosshatch', 'paint'} and not e.get('d'):
                raise ValueError(f'{eid}: SVG d is required')
            if kind == 'erase':
                if sid == 'global':
                    raise ValueError(f'{eid}: global erase is unsupported; place target and eraser in one scene')
                r = e.get('region', {})
                for key in ('x', 'y', 'width', 'height'):
                    finite(r.get(key), f'{eid}.region.{key}', 1 if key in {'width', 'height'} else 0)
                if r['x']+r['width'] > canvas['width'] or r['y']+r['height'] > canvas['height']:
                    raise ValueError(f'{eid}: erase region exceeds canvas')
                if not e.get('targets') or not isinstance(e['targets'], list):
                    raise ValueError(f'{eid}: explicit targets are required')
            if kind == 'image':
                animation = e.get('animation')
                if isinstance(animation, dict):
                    animation = animation.get('type', animation.get('mode'))
                for mode in (e.get('mode'), animation):
                    if mode is not None and (not isinstance(mode, str) or mode not in {'sketch', 'fade', 'draw'}):
                        raise ValueError(f'{eid}: unsupported image mode {mode}')
                for coord in ('x', 'y'):
                    finite(e.get(coord), f'{eid}.{coord}')
                for key in ('src', 'inkSrc', 'traceSrc'):
                    path = e.get(key)
                    if not isinstance(path, str) or not path:
                        raise ValueError(f'{eid}: {key} is required')
                    if root is not None and not (root/path).is_file():
                        raise ValueError(f'{eid}: missing {key}: {path}')
                finite(e.get('width'), f'{eid}.width', 1)
            count += 1
    for sid, _, _, elements in scopes:
        for e in elements:
            deps = e.get('afterId', [])
            deps = [deps] if isinstance(deps, str) else deps
            if not isinstance(deps, list):
                raise ValueError(f'{e["id"]}: afterId must be an id or array')
            for dep in deps:
                if (sid, dep) not in lookup and ('global', dep) not in lookup:
                    raise ValueError(f'{e["id"]}: missing dependency {dep}')
            for target in e.get('targets', []):
                if (sid, target) not in lookup or lookup[sid, target]['type'] == 'erase' or target == e['id']:
                    raise ValueError(f'{e["id"]}: invalid same-scene erase target {target}')
    for cue in doc.get('cues', []):
        a = finite(cue.get('startMs'), 'cue.startMs')
        b = finite(cue.get('endMs'), 'cue.endMs')
        if b <= a or b > duration or not isinstance(cue.get('text'), str):
            raise ValueError('Invalid caption interval/text')
    if scheduled:
        previous = 0
        tracked = set()
        for action in doc.get('toolTrack', []):
            a = finite(action.get('startMs'), 'tool.startMs')
            b = finite(action.get('endMs'), 'tool.endMs')
            if a < previous-.001 or b <= a or b > duration+.001:
                raise ValueError('Overlapping/out-of-bounds tool actions')
            key = action.get('sceneId'), action.get('elementId')
            if key not in lookup:
                raise ValueError(f'Unknown tool action {key}')
            if key in tracked:
                raise ValueError(f'Duplicate tool action {key}')
            tracked.add(key)
            previous = b
        expected = {key for key, e in lookup.items() if not e.get('instant') and e.get('durationMs', 0) > 0 and e['type'] != 'fill'
                    and not (e['type'] == 'image' and (e.get('mode') == 'fade' or e.get('animation') == 'fade'))}
        if expected != tracked:
            raise ValueError(f'Missing/extra animated tool actions: {expected.symmetric_difference(tracked)}')
    from check_all_drawn import enabled as all_drawn_enabled, assert_all_drawn
    if all_drawn_enabled(doc):
        assert_all_drawn(doc, scheduled=scheduled)
    return {'valid': True, 'durationMs': duration, 'canvas': canvas, 'sceneCount': len(scenes), 'elementCount': count}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', required=True, type=Path)
    p.add_argument('--scheduled', action='store_true')
    a = p.parse_args()
    try:
        doc = json.loads(a.project.read_text(encoding='utf-8-sig'))
        print(json.dumps(validate_document(doc, a.project.parent, a.scheduled), ensure_ascii=False))
    except (ValueError, OSError) as error:
        p.exit(2, f'Validation failed: {error}\n')

if __name__ == '__main__':
    main()
