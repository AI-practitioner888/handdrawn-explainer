"""Check the default blank-board drawing contract before video encoding.

Structure checks exclude passive reveals and require one matching tool action
for every element. Browser checks inspect lossless board pixels with tools
hidden. They sample scene starts and action progress; they are deliberately
reported as samples, not as proof about every pixel of every exported frame.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
from pathlib import Path
from urllib.parse import urlencode

from PIL import Image, ImageChops, ImageColor

ALLOWED_TYPES = {'text', 'stroke', 'paint', 'hatch', 'crosshatch', 'image', 'erase'}


def enabled(doc):
    policy = doc.get('drawPolicy', {})
    if not isinstance(policy, dict):
        raise ValueError('drawPolicy must be an object')
    profile = policy.get('profile', 'all-drawn')
    if not isinstance(profile, str) or profile not in {'all-drawn', 'legacy'}:
        raise ValueError('drawPolicy.profile must be all-drawn or explicit legacy')
    return profile == 'all-drawn'


def inspect_structure(doc, scheduled=True):
    """Return a report, retaining all violations so a failed export is useful."""
    policy_enabled = enabled(doc)
    report = {'profile': doc.get('drawPolicy', {}).get('profile', 'all-drawn'),
              'enabled': policy_enabled, 'structureChecked': policy_enabled,
              'pixelChecked': False, 'passed': True, 'violations': [],
              'elementCount': 0, 'toolActionCount': len(doc.get('toolTrack', [])),
              'scope': 'structure only; no rendered pixel claim'}
    if not policy_enabled:
        return report
    errors = report['violations']
    if doc.get('elements'):
        errors.append('Global elements are forbidden; every scene starts with an empty board')
    if doc.get('cues'):
        errors.append('Runtime caption cues are forbidden; draw caption text as text elements')
    if doc.get('transition', {}).get('durationMs', 0):
        errors.append('Scene wipes can recall old drawings; transition.durationMs must be 0')
    theme = doc.get('theme', {})
    try:
        if ImageColor.getrgb(theme.get('paper', '#ffffff')) != (255, 255, 255):
            errors.append('all-drawn requires a pure white paper color')
    except (ValueError, TypeError):
        errors.append('Invalid paper color')
    if theme.get('dotSpacing', 0) != 0:
        errors.append('all-drawn requires theme.dotSpacing 0')
    if doc.get('toolMode', 'serial') != 'serial':
        errors.append('all-drawn requires toolMode serial')
    expected = {}
    elements_by_key = {}
    fps = doc.get('fps', 30)
    for scene in doc.get('scenes', []):
        sid, offset = scene.get('id'), scene.get('startMs', 0)
        # The first frame actually encoded after this scene begins must remain
        # blank, even when a scene boundary lies between two frame timestamps.
        first_frame_ms = math.ceil(offset * fps / 1000 - 1e-9) * 1000 / fps
        first_local_ms = first_frame_ms - offset
        for e in scene.get('elements', []):
            report['elementCount'] += 1
            label = f'{sid}/{e.get("id")}'
            kind = e.get('type')
            if kind not in ALLOWED_TYPES:
                errors.append(f'{label}: {kind} is not a supported drawn element')
            if e.get('instant'):
                errors.append(f'{label}: instant content is forbidden')
            length = e.get('durationMs', 0)
            if not isinstance(length, (int, float)) or isinstance(length, bool) or length <= 0:
                errors.append(f'{label}: every visible element needs a positive drawing duration')
            if e.get('startMs', 0) < first_local_ms - .01:
                errors.append(f'{label}: drawing starts before the scene\'s first encoded blank frame')
            if e.get('passive'):
                errors.append(f'{label}: passive visual reveals are forbidden')
            if kind == 'image':
                animation = e.get('animation')
                if isinstance(animation, dict):
                    animation = animation.get('type', animation.get('mode'))
                if e.get('mode') != 'draw' and animation != 'draw':
                    errors.append(f'{label}: images must use mode draw, a progressive swept brush mask')
                if e.get('mode') == 'fade' or animation == 'fade':
                    errors.append(f'{label}: image fades are forbidden')
            expected[(sid, e.get('id'))] = (offset + e.get('startMs', 0),
                                           offset + e.get('startMs', 0) + length)
            elements_by_key[(sid, e.get('id'))] = e
    for key, element in elements_by_key.items():
        if element.get('type') != 'erase':
            continue
        label = '/'.join(key)
        targets = element.get('targets')
        if not isinstance(targets, list) or not targets:
            errors.append(f'{label}: erasing requires explicit drawn targets')
            continue
        for target_id in targets:
            target_key = key[0], target_id
            target = elements_by_key.get(target_key)
            if target is None or target.get('type') == 'erase' or target_key == key:
                errors.append(f'{label}: eraser target {target_id} must be a drawn object in the same scene')
            elif expected[target_key][1] > expected[key][0] + .01:
                errors.append(f'{label}: target {target_id} must finish drawing before erasing starts')
    if scheduled:
        actual = {}
        previous_end = -math.inf
        for action in doc.get('toolTrack', []):
            key = action.get('sceneId'), action.get('elementId')
            a, b = action.get('startMs', -1), action.get('endMs', -1)
            if key in actual:
                errors.append(f'{key}: duplicate tool action')
            actual[key] = (a, b)
            if a < previous_end - .01 or b <= a:
                errors.append(f'{key}: overlapping or invalid tool interval')
            previous_end = b
        for key, interval in expected.items():
            if key not in actual:
                errors.append(f'{key}: missing tool action')
            elif any(abs(a-b) > .01 for a, b in zip(interval, actual[key])):
                errors.append(f'{key}: tool action must cover the full element drawing interval')
        for key in actual.keys() - expected.keys():
            errors.append(f'{key}: extra tool action')
    report['passed'] = not errors
    return report


def assert_all_drawn(doc, scheduled=True):
    report = inspect_structure(doc, scheduled=scheduled)
    if not report['passed']:
        raise ValueError('All-drawn policy failed: ' + '; '.join(report['violations']))
    return report


def _picture(page, ms):
    raw = page.evaluate('''t => {
        window.renderAt(t / 1000, false);
        return document.querySelector('#board').toDataURL('image/png');
    }''', float(ms))
    return Image.open(io.BytesIO(base64.b64decode(raw.partition(',')[2]))).convert('RGB')


def _changed_pixels(a, b):
    diff = ImageChops.difference(a, b)
    # Count pixels with any RGB channel change. Painting over a colored region
    # is valid and need not increase the number of non-white pixels.
    red, green, blue = diff.split()
    combined = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return a.width*a.height - combined.histogram()[0]


def inspect_pixels(page, doc, report=None, fps=None):
    """Inspect a ready, tools-hidden renderer page using lossless board pixels."""
    report = report or inspect_structure(doc)
    if not report['enabled'] or not report['passed']:
        return report
    if not page.evaluate("() => new URLSearchParams(location.search).has('hideTools')"):
        raise ValueError('Pixel checks require a renderer page with hideTools')
    fps = fps or doc.get('fps', 30)
    width, height = doc['canvas']['width'], doc['canvas']['height']
    white = Image.new('RGB', (width, height), 'white')
    blank_checks, progress_checks = [], []
    for scene in doc['scenes']:
        index = math.ceil(scene['startMs'] * fps / 1000 - 1e-9)
        ms = index * 1000 / fps
        changed = _changed_pixels(_picture(page, ms), white)
        item = {'sceneId': scene['id'], 'frameIndex': index, 'sampleMs': ms,
                'nonWhitePixels': changed, 'passed': changed == 0}
        blank_checks.append(item)
        if changed:
            report['violations'].append(f'{scene["id"]}: first encoded scene frame contains {changed} non-white pixels')
    for action in doc.get('toolTrack', []):
        a, b = action['startMs'], action['endMs']
        before = _picture(page, max(0, a - .001))
        start = _picture(page, a)
        middle = _picture(page, a + (b-a) * .5)
        final = _picture(page, b - .001)
        pop = _changed_pixels(before, start)
        first_half = _changed_pixels(start, middle)
        second_half = _changed_pixels(middle, final)
        item = {'sceneId': action['sceneId'], 'elementId': action['elementId'],
                'sampleMs': [max(0, a-.001), a, a+(b-a)*.5, b-.001],
                'startPopPixels': pop, 'firstHalfChangedPixels': first_half,
                'secondHalfChangedPixels': second_half,
                'passed': pop == 0 and first_half > 0 and second_half > 0}
        progress_checks.append(item)
        if not item['passed']:
            report['violations'].append(f'{action["sceneId"]}/{action["elementId"]}: pixel progress samples failed {item}')
    report.update(pixelChecked=True, sceneStartChecks=blank_checks,
                  actionProgressSamples=progress_checks,
                  pixelSampleCount=len(blank_checks)+len(progress_checks)*4,
                  scope='structure plus lossless scene-start and per-action progress samples; not exhaustive per-pixel frame validation')
    report['passed'] = not report['violations']
    return report


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path, help='Compiled project directory')
    parser.add_argument('--timeline', default='timeline.json')
    parser.add_argument('--structure-only', action='store_true')
    parser.add_argument('--browser')
    parser.add_argument('--fps', type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    timeline = project/args.timeline
    doc = json.loads(timeline.read_text(encoding='utf-8-sig'))
    report = inspect_structure(doc)
    report['timelineSha256'] = hashlib.sha256(timeline.read_bytes()).hexdigest()
    if report['enabled'] and report['passed'] and not args.structure_only:
        from playwright.sync_api import sync_playwright
        from render import launch, serve
        with serve(project) as base, sync_playwright() as p:
            browser = launch(p, args.browser)
            try:
                page = browser.new_page(viewport=doc['canvas'], device_scale_factor=1)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base+'/index.html?'+urlencode({'export': '1', 'hideTools': '1', 'timeline': args.timeline}), wait_until='load')
                page.evaluate('() => window.ready')
                if errors:
                    raise RuntimeError('; '.join(errors))
                report = inspect_pixels(page, doc, report, args.fps)
            finally:
                browser.close()
    write_report(args.output or project/'all-drawn-check.json', report)
    print(json.dumps({'enabled': report['enabled'], 'passed': report['passed'],
                      'structureChecked': report['structureChecked'], 'pixelChecked': report['pixelChecked'],
                      'violations': report['violations']}, ensure_ascii=False))
    if not report['passed']:
        parser.exit(2, 'All-drawn checks failed before video encoding\n')


if __name__ == '__main__':
    main()
