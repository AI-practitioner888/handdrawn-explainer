"""Check local dependencies without reading credentials or modifying the machine."""
from __future__ import annotations
import importlib.util
import argparse
import json
import shutil
import sys

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', help='Optional Chromium executable')
    args = parser.parse_args()
    modules = {x: importlib.util.find_spec(x) is not None for x in ('numpy', 'PIL', 'cv2', 'playwright')}
    executables = {x: shutil.which(x) for x in ('ffmpeg', 'ffprobe')}
    report = {'python': sys.version.split()[0], 'pythonSupported': sys.version_info >= (3, 10),
              'modules': modules, 'executables': executables,
              'optionalEdgeTTS': importlib.util.find_spec('edge_tts') is not None,
              'browser': {'available': False}}
    if all(modules.values()):
        try:
            from playwright.sync_api import sync_playwright
            from render import launch
            with sync_playwright() as p:
                browser = launch(p, args.browser)
                report['browser'] = {'available': True, 'version': browser.version}
                browser.close()
        except Exception as error:
            report['browser'] = {'available': False, 'error': str(error),
                                 'setup': 'Pass --browser/HANDDRAWN_BROWSER or install Playwright Chromium'}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['pythonSupported'] or not all(modules.values()) or not all(executables.values()) or not report['browser']['available']:
        sys.exit(2)

if __name__ == '__main__':
    main()
