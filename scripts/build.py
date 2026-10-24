"""Build an editable whiteboard project from authored JSON; optionally render it."""
from __future__ import annotations
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from validate import validate_document

SKILL = Path(__file__).resolve().parents[1]

def run(script, *args):
    subprocess.run([sys.executable, str(SKILL/'scripts'/script), *map(str, args)], check=True)

def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def copy_asset(source_root, target_root, relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError(f'Use a project-relative asset path: {relative}')
    source, target = source_root/path, target_root/path
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.resolve() != target.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path, help='Authored project JSON')
    parser.add_argument('--output', required=True, type=Path, help='Compiled project directory')
    parser.add_argument('--audio-plan', type=Path)
    parser.add_argument('--narration', type=Path, help='Import an existing narration audio file')
    parser.add_argument('--silent', action='store_true', help='Explicitly build without spoken narration')
    parser.add_argument('--render', action='store_true', help='Export contact sheet and MP4')
    parser.add_argument('--sheet', action='store_true')
    parser.add_argument('--browser')
    args = parser.parse_args()
    if sum(bool(x) for x in (args.audio_plan, args.narration, args.silent)) > 1:
        parser.error('Choose one of --audio-plan, --narration, --silent')
    source = args.project.resolve()
    root = args.output.resolve()
    if root == SKILL or SKILL in root.parents:
        parser.error('Build outside the installed skill folder')
    root.mkdir(parents=True, exist_ok=True)
    doc = json.loads(source.read_text(encoding='utf-8-sig'))
    if args.render and doc.get('narrationTimingStatus') == 'needs-realignment':
        raise ValueError('Narration changed: measure the new voice, update affected timings, and mark narrationTimingStatus aligned before video export')
    plan = args.audio_plan
    if plan is None and not args.narration and not args.silent and doc.get('audio', {}).get('plan'):
        plan = source.parent/doc['audio']['plan']
    imported = args.narration
    if imported is None and not plan and not args.silent and doc.get('audio', {}).get('file'):
        imported = source.parent/doc['audio']['file']
    doc.setdefault('schemaVersion', 'handdrawn-explainer/1')
    doc.setdefault('canvas', {'width': 1280, 'height': 720})
    doc.setdefault('fps', 30)
    doc.setdefault('drawPolicy', {'profile': 'all-drawn'})
    if doc.get('drawPolicy', {}).get('profile') == 'all-drawn':
        # Explicit styles are preserved and checked. Omitted styles select the
        # blank white board contract instead of the legacy paper decoration.
        doc.setdefault('theme', {})
        doc['theme'].setdefault('paper', '#ffffff')
        doc['theme'].setdefault('dotSpacing', 0)
        doc['theme'].setdefault('ink', '#282b28')
    else:
        doc.setdefault('theme', {'paper': '#fdfcf3', 'ink': '#282b28', 'dot': '#d5d8c8'})
    doc.setdefault('cues', [])
    doc.setdefault('elements', [])
    doc.setdefault('narrationPolicy', {'profile': 'continuous', 'maxPauseMs': 500,
                                     'maxLeadMs': 150, 'maxTailMs': 300, 'thresholdDb': -45})
    if args.silent:
        doc['narrationPolicy'] = {'profile': 'silent'}
    elif plan and json.loads(plan.read_text(encoding='utf-8-sig')).get('mode') == 'silent':
        doc['narrationPolicy'] = {'profile': 'silent'}
    doc['toolMode'] = 'serial'
    all_elements = doc['elements']+[e for s in doc.get('scenes', []) for e in s.get('elements', [])]
    for e in all_elements:
        e.setdefault('startMs', 0)
        e.setdefault('durationMs', 0)
        e.setdefault('tool', 'eraser' if e.get('type') == 'erase' else doc.get('defaultTool', 'pencil'))
        if e.get('type') == 'image':
            for field in ('src', 'inkSrc', 'traceSrc'):
                copy_asset(source.parent, root, e[field])
    validate_document(doc, root)
    write_json(root/'source.project.json', doc)
    renderer = SKILL/'assets'/'renderer'
    for path in renderer.iterdir():
        if path.is_file():
            shutil.copy2(path, root/path.name)
    fonts = root/'assets'/'fonts'
    fonts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL/'assets'/'fonts'/'LXGWWenKai-Regular.ttf', fonts/'LXGWWenKai-Regular.ttf')
    for filename in ('srt-whiteboard-animation-MIT.txt', 'LXGWWenKai-OFL.txt', 'provenance.json'):
        source_license = SKILL/'LICENSES'/filename
        if source_license.is_file():
            copy_asset(SKILL, root, 'LICENSES/'+filename)
    run('schedule.py', '--input', root/'source.project.json', '--output', root/'timeline.json')
    timeline = json.loads((root/'timeline.json').read_text(encoding='utf-8'))
    validate_document(timeline, root, scheduled=True)
    if timeline.get('drawPolicy', {}).get('profile') == 'all-drawn':
        run('check_all_drawn.py', '--project', root, '--structure-only')
    run('compile_assets.py', 'glyphs', '--timeline', root/'timeline.json', '--output-dir', root/'assets')
    if plan:
        plan_doc = json.loads(plan.read_text(encoding='utf-8-sig'))
        if plan_doc.get('durationMs') != doc['durationMs']:
            raise ValueError('Audio plan durationMs must equal project durationMs')
        run('audio.py', '--plan', plan.resolve(), '--output-dir', root/'audio', '--timeline', root/'timeline.json')
        write_json(root/'narration.plan.json', plan_doc)
    elif imported:
        imported_plan = {'durationMs': doc['durationMs'], 'mode': 'files', 'maxSpeed': 1,
                         'segments': [{'id': 'import', 'startMs': 0, 'endMs': doc['durationMs'],
                                       'text': '', 'audioFile': str(imported.resolve()),
                                       'trimSilence': False, 'normalize': False}]}
        write_json(root/'import.plan.json', imported_plan)
        run('audio.py', '--plan', root/'import.plan.json', '--output-dir', root/'audio', '--timeline', root/'timeline.json')
    elif args.silent:
        run('audio.py', '--silent', '--duration-ms', doc['durationMs'], '--output-dir', root/'audio', '--timeline', root/'timeline.json')
    elif args.render:
        raise ValueError('Choose narration, an audio plan, or explicit --silent before exporting')
    if (root/'audio'/'plan.json').is_file():
        # Rebuilding from this compiled project's source must work after moving
        # it, without reaching back to the original authored plan or recording.
        doc['audio'] = {'plan': 'audio/plan.json'}
        write_json(root/'source.project.json', doc)
        # Inspect the actual spoken track before encoding; tool noise and music
        # must never mask a gap in narration.
        run('check_audio.py', '--project', root)
    report = validate_document(timeline, root, scheduled=True)
    write_json(root/'project-validation.json', report)
    if args.render or args.sheet:
        options = ['--project', root, '--sheet']
        if args.browser:
            options += ['--browser', args.browser]
        run('render.py', *options)
        review_options = ['--project', source, '--output-dir', root/'review', '--compiled', root]
        review_plan = plan or (root/'import.plan.json' if imported else root/'audio'/'plan.json')
        if review_plan and review_plan.is_file():
            # Patches bind the editable plan, not a generated audio-plan copy.
            review_options += ['--audio-plan', review_plan.resolve()]
        run('review_project.py', *review_options)
        if args.render:
            export_options = ['--project', root, '--video']
            if args.browser:
                export_options += ['--browser', args.browser]
            run('render.py', *export_options)
    print(json.dumps({'project': str(root), 'preview': str(root/'index.html'),
                     'timeline': str(root/'timeline.json'), 'rendered': args.render}, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
