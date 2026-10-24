"""Behavioral pixel checks for erase, preserved neighbors, region growth and seek."""
from __future__ import annotations
import argparse
import io
import json
from pathlib import Path
import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright
from render import serve, launch, frame

def crop(page, time_ms, region):
    picture = Image.open(io.BytesIO(frame(page, time_ms/1000))).convert('RGB')
    x, y, w, h = (int(region[k]) for k in ('x', 'y', 'width', 'height'))
    return np.asarray(picture.crop((x, y, x+w, y+h))).astype(float)

def ink_count(pixels, paper):
    return int((np.linalg.norm(pixels-np.array(paper), axis=2) > 85).sum())

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--checks', type=Path, help='Array of pixel check objects; defaults to timeline.qaChecks')
    parser.add_argument('--browser')
    args = parser.parse_args()
    root = args.project.resolve()
    doc = json.loads((root/'timeline.json').read_text(encoding='utf-8-sig'))
    checks = json.loads(args.checks.read_text(encoding='utf-8-sig')) if args.checks else doc.get('qaChecks', [])
    color = doc.get('theme', {}).get('paper', '#fdfcf3').lstrip('#')
    paper = tuple(int(color[i:i+2], 16) for i in (0, 2, 4))
    results = []
    with serve(root) as base, sync_playwright() as p:
        browser = launch(p, args.browser)
        try:
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base+'/index.html?export=1&hideTools=1', wait_until='load')
            page.evaluate('() => window.ready')
            time = doc['durationMs']/1000*.71
            first = frame(page, time)
            frame(page, doc['durationMs']/1000*.15)
            frame(page, doc['durationMs']/1000-.01)
            repeated = frame(page, time)
            results.append({'kind': 'seek-determinism', 'passed': first == repeated})
            layout = page.evaluate('() => window.layoutReport || []')
            outside = [item for item in layout if item.get('type') == 'text' and (item.get('outOfCanvas') or item.get('outsideCanvas'))]
            results.append({'kind': 'text-fit', 'passed': not outside, 'outside': outside})
            for check in checks:
                kind = check['kind']
                if kind in {'erase', 'preserve'}:
                    before = crop(page, check['beforeMs'], check['region'])
                    after = crop(page, check['afterMs'], check['region'])
                    old, new = ink_count(before, paper), ink_count(after, paper)
                    if kind == 'erase':
                        maximum = check.get('maxAfterRatio', .08)
                        passed = old >= check.get('minBeforePixels', 20) and new <= max(4, old*maximum)
                        results.append({'kind': kind, 'passed': passed, 'beforePixels': old, 'afterPixels': new})
                    else:
                        delta = float(np.abs(before-after).mean())
                        passed = delta <= check.get('maxMeanDifference', 1.0)
                        results.append({'kind': kind, 'passed': passed, 'meanDifference': delta})
                elif kind == 'growth':
                    counts = [ink_count(crop(page, time_ms, check['region']), paper) for time_ms in check['timesMs']]
                    increments = [b-a for a, b in zip(counts, counts[1:])]
                    passed = all(x > check.get('minAddedPixels', 10) for x in increments)
                    results.append({'kind': kind, 'passed': passed, 'counts': counts, 'increments': increments})
                else:
                    raise ValueError(f'Unknown check kind: {kind}')
            if errors:
                results.append({'kind': 'browser-errors', 'passed': False, 'errors': errors})
        finally:
            browser.close()
    report = {'passed': all(x['passed'] for x in results), 'checks': results,
              'pixelCheckCount': len(checks), 'note': 'No pixel assertions were authored' if not checks else None}
    (root/'render-checks.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['passed']:
        raise SystemExit(2)

if __name__ == '__main__':
    main()
