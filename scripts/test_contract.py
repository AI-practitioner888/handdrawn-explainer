"""Regression checks for SVG origins, visible glyph bounds and erase scope."""
from __future__ import annotations
import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright
from render import serve, launch
from schedule import schedule_document
from validate import validate_document
from compile_assets import build_glyphs

SKILL = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix='handdrawn-contract-') if args.output is None else None
    root = args.output.resolve() if args.output else Path(temporary.name)
    root.mkdir(parents=True, exist_ok=True)
    cases = [
        ('relative', 'M 100 100 l 10 0 m 20 0 l 10 0', [(100, 100), (130, 100)], [(110, 100), (140, 100)]),
        ('implicit', 'M 100 140 l 10 0 m 20 0 10 0 0 10', [(100, 140), (130, 140)], [(110, 140), (140, 150)]),
        ('closed', 'M 100 180 l 10 0 l 0 10 Z m 20 0 l 10 0', [(100, 180), (120, 180)], [(100, 180), (130, 180)]),
        ('first', 'm 2e2 220 l 10 0 m -20 0 l 10 0', [(200, 220), (190, 220)], [(210, 220), (200, 220)]),
    ]
    elements = [{'id': key, 'type': 'stroke', 'd': d, 'startMs': i*500, 'durationMs': 300,
                 'locked': True, 'wobble': False} for i, (key, d, _, _) in enumerate(cases)]
    elements += [{'id': 'edge-i', 'type': 'text', 'text': 'i', 'x': 466, 'y': 250, 'size': 40,
                  'startMs': 0, 'durationMs': 0, 'instant': True}]
    source = {'title': 'Contract regression', 'durationMs': 3000, 'fps': 30,
              'canvas': {'width': 480, 'height': 640}, 'theme': {'paper': '#ffffff', 'ink': '#222222', 'dotSpacing': 0},
              'scenes': [{'id': 'board', 'startMs': 0, 'endMs': 3000, 'elements': elements}], 'cues': []}
    validate_document(source)
    invalid = copy.deepcopy(source)
    invalid['elements'] = [{'id': 'title', 'type': 'text', 'text': 'i', 'x': 50, 'y': 20, 'size': 40,
                            'startMs': 0, 'durationMs': 0},
                           {'id': 'bad-erase', 'type': 'erase', 'targets': ['title'],
                            'region': {'x': 40, 'y': 10, 'width': 80, 'height': 80}, 'startMs': 500, 'durationMs': 300}]
    try:
        validate_document(invalid)
        raise AssertionError('Global eraser was accepted')
    except ValueError as error:
        assert 'global erase' in str(error)
    timeline = schedule_document(source)
    (root/'timeline.json').write_text(json.dumps(timeline), encoding='utf-8')
    for file in (SKILL/'assets'/'renderer').iterdir():
        if file.is_file():
            shutil.copy2(file, root/file.name)
    build_glyphs(root/'timeline.json', root/'assets')
    report = {'globalEraseRejected': True, 'svgCases': []}
    with serve(root) as base, sync_playwright() as p:
        browser = launch(p)
        try:
            page = browser.new_page()
            page.goto(base+'/index.html?export=1', wait_until='load')
            page.evaluate('() => window.ready')
            for key, _, starts, ends in cases:
                trace = page.evaluate('id => window.debugTrace(id)', key)
                paths = trace['parts'] if isinstance(trace, dict) else trace
                # debugTrace returns raw polylines rather than the metric object.
                if paths and isinstance(paths[0], dict):
                    paths = [part['points'] for part in paths]
                for path, start, end in zip(paths, starts, ends):
                    assert abs(path[0][0]-start[0]) < .02 and abs(path[0][1]-start[1]) < .02, (key, path[0], start)
                    assert abs(path[-1][0]-end[0]) < .02 and abs(path[-1][1]-end[1]) < .02, (key, path[-1], end)
                assert len(paths) == len(starts)
                report['svgCases'].append({'id': key, 'passed': True})
            bounds = page.evaluate('() => window.layoutReport.find(e=>e.id==="edge-i")')
            assert not bounds['outOfCanvas'], bounds
            report['visibleGlyphBounds'] = bounds
        finally:
            browser.close()
    # A real export proves the edge text no longer triggers a false rejection.
    subprocess.run([sys.executable, str(SKILL/'scripts'/'render.py'), '--project', str(root), '--sheet', '--video'], check=True)
    report['passed'] = True
    (root/'contract-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    if temporary:
        temporary.cleanup()

if __name__ == '__main__':
    main()
