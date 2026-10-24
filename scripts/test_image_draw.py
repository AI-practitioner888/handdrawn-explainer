"""Browser pixel checks for image drawing; writes only to a temporary fixture."""
from __future__ import annotations

import base64
import io
import json
import re
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
from playwright.sync_api import sync_playwright

from render import launch, serve


def pixels(page, seconds):
    raw = page.evaluate("""t => {
      window.renderAt(t, false);
      return document.querySelector('#board').toDataURL('image/png');
    }""", seconds)
    return Image.open(io.BytesIO(base64.b64decode(raw.partition(',')[2]))).convert('RGB')


def check():
    renderer = Path(__file__).resolve().parents[1] / 'assets' / 'renderer'
    with tempfile.TemporaryDirectory(prefix='handdrawn-image-draw-') as temp:
        root = Path(temp)
        shutil.copy2(renderer / 'renderer.js', root / 'renderer.js')
        html=(renderer / 'index.html').read_text(encoding='utf-8')
        (root / 'index.html').write_text(re.sub(r'@font-face\{[^}]*\}', '', html), encoding='utf-8')
        color = Image.new('RGBA', (80, 60), '#e03824')
        draw = ImageDraw.Draw(color)
        draw.rectangle((40, 0, 79, 59), fill='#145bc9')
        draw.rectangle((0, 40, 79, 59), fill='#24824a')
        color.save(root / 'color.png')
        paths = [[[0, y], [80, y]] if row % 2 == 0 else [[80, y], [0, y]]
                 for row, y in enumerate(range(5, 60, 10))]
        (root / 'trace.json').write_text(json.dumps({
            'width': 80, 'height': 60, 'paths': paths, 'brushSize': 10,
        }), encoding='utf-8')
        element = {
            'id': 'object', 'type': 'image', 'mode': 'draw',
            'src': './color.png', 'traceSrc': './trace.json',
            'x': 40, 'y': 30, 'width': 80,
            'startMs': 100, 'durationMs': 1000,
            # This legacy setting must never truncate draw mode's pencil.
            'inkPhase': .2,
        }
        timeline = {
            'title': 'Draw image behavior', 'durationMs': 1200,
            'canvas': {'width': 160, 'height': 120},
            'theme': {'paper': '#ffffff', 'dotSpacing': 0},
            'drawPolicy': {'profile': 'all-drawn'},
            'scenes': [{'id': 'scene', 'startMs': 0, 'endMs': 1200, 'elements': [element]}],
            'toolTrack': [{'sceneId': 'scene', 'elementId': 'object',
                           'startMs': 100, 'endMs': 1100, 'travelStartMs': 100}],
        }
        (root / 'timeline.json').write_text(json.dumps(timeline), encoding='utf-8')
        with serve(root) as url, sync_playwright() as p:
            browser = launch(p)
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(url + '/index.html?hideTools&export')
            page.evaluate('window.ready')
            blank = pixels(page, 0)
            assert blank.getextrema() == ((255, 255), (255, 255), (255, 255)), 'All-drawn paper must be pure white'
            at_start = pixels(page, .1)
            assert ImageChops.difference(blank, at_start).getbbox() is None, 'Start must be blank'
            quarter = pixels(page, .35)
            assert quarter.getpixel((45, 35)) == (224, 56, 36), 'Swept pixels reveal original color'
            assert quarter.getpixel((45, 75)) == blank.getpixel((45, 75)), 'Unswept pixels stay blank'
            changed = ImageChops.difference(quarter, blank).convert('L')
            changed_count = sum(value > 0 for value in changed.tobytes())
            assert 800 < changed_count < 2000, changed_count
            final = pixels(page, 1.1)
            assert ImageChops.difference(final.crop((40, 30, 120, 90)), color.convert('RGB')).getbbox() is None
            assert ImageChops.difference(pixels(page, .35), quarter).getbbox() is None, 'Reverse seeking must be deterministic'
            assert ImageChops.difference(pixels(page, 0), blank).getbbox() is None, 'Reverse to zero must clear all pixels'
            # Give a second trace only one row, sharing the source image.
            # Completion must retain unswept pixels and must not snap to the full image.
            (root / 'partial.json').write_text(json.dumps({
                'width': 80, 'height': 60, 'strokes': [paths[0]], 'brushSize': 10,
            }), encoding='utf-8')
            partial_timeline = json.loads(json.dumps(timeline))
            partial_timeline['scenes'][0]['elements'].append({
                **element, 'id': 'partial-object', 'traceSrc': './partial.json',
                'startMs': 1100, 'durationMs': 100,
            })
            partial_timeline['toolTrack'].append({
                'sceneId': 'scene', 'elementId': 'partial-object',
                'startMs': 1100, 'endMs': 1200, 'travelStartMs': 1100,
            })
            # Move the covered source off-board; same source, different trace.
            partial_timeline['scenes'][0]['elements'][0]['x'] = 200
            (root / 'partial-timeline.json').write_text(json.dumps(partial_timeline), encoding='utf-8')
            page.goto(url + '/index.html?hideTools&export&timeline=./partial-timeline.json')
            page.evaluate('window.ready')
            partial_final = pixels(page, 1.2)
            assert partial_final.getpixel((45, 35)) == (224, 56, 36)
            assert partial_final.getpixel((45, 75)) == blank.getpixel((45, 75)), 'Completion must never reveal unswept pixels'
            page.goto(url + '/index.html?export')
            page.evaluate('window.ready')
            page.evaluate('window.renderAt(.35, false)')
            stats = page.evaluate('window.lastRenderStats')
            assert stats['visibleTools'] == stats['concurrentInk'] == 1
            assert abs(stats['tip']['x'] - 80) < .001 and abs(stats['tip']['y'] - 45) < .001, stats
            page.evaluate('window.renderAt(.9, false)')
            stats = page.evaluate('window.lastRenderStats')
            assert stats['visibleTools'] == stats['concurrentInk'] == 1, 'Draw pencil must remain active past legacy inkPhase'
            page.evaluate('window.renderAt(1.1, false)')
            assert page.evaluate('window.lastRenderStats.visibleTools') == 0
            assert not errors, errors
            browser.close()
    print('PASS image draw: blank start, progressive original color, full coverage, no final snap, reverse seek, distinct traces, one path-driven tool')


if __name__ == '__main__':
    check()
