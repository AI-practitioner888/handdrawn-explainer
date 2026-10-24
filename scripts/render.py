"""Serve a compiled project locally and export deterministic H.264 frames."""
from __future__ import annotations
import argparse
import base64
import functools
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright
from validate import validate_document
from check_all_drawn import enabled as all_drawn_enabled, inspect_structure, inspect_pixels, write_report

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

@contextmanager
def serve(project):
    http = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(project)))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{http.server_port}'
    finally:
        http.shutdown()
        http.server_close()

def launch(p, executable=None):
    options = {'headless': True, 'args': ['--disable-background-timer-throttling']}
    selected = executable or os.environ.get('HANDDRAWN_BROWSER')
    if not selected:
        candidates = [Path(os.environ.get('PROGRAMFILES', ''))/'Google/Chrome/Application/chrome.exe',
                      Path(os.environ.get('PROGRAMFILES(X86)', ''))/'Microsoft/Edge/Application/msedge.exe']
        selected = next((str(x) for x in candidates if x.is_file()), None)
    if selected:
        options['executable_path'] = selected
    return p.chromium.launch(**options)

def frame(page, seconds):
    raw = page.evaluate('t => window.renderAt(t)', float(seconds))
    return base64.b64decode(raw.partition(',')[2])

def contact_sheet(page, out, duration, width, height):
    times = [duration*i/11 for i in range(12)]
    times[-1] = max(0, duration-.02)
    w, h = 420, round(420*height/width)
    result = Image.new('RGB', (3*w, 4*(h+24)), '#eceee4')
    draw = ImageDraw.Draw(result)
    for i, t in enumerate(times):
        raw = frame(page, t)
        picture = Image.open(io.BytesIO(raw)).convert('RGB')
        x, y = i%3*w, i//3*(h+24)
        result.paste(picture.resize((w, h), Image.Resampling.LANCZOS), (x, y))
        draw.text((x+8, y+h+5), f'{t:.2f}s', fill='#386d4d')
    result.save(out/'contact-sheet.jpg', quality=94)
    (out/'cover.jpg').write_bytes(frame(page, max(0, duration-.1)))

def export(page, errors, out, doc, fps, name, audio_dir):
    for executable in ('ffmpeg', 'ffprobe'):
        if not shutil.which(executable):
            raise RuntimeError(f'Missing {executable} on PATH')
    duration = doc['durationMs']/1000
    count = round(duration*fps)
    if abs(count/fps-duration) > .001:
        raise ValueError('durationMs must end on a frame boundary for the chosen fps')
    silent, final = out/'silent.mp4', out/name
    encoder = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'image2pipe',
        '-framerate', str(fps), '-vcodec', 'mjpeg', '-i', 'pipe:0', '-an', '-c:v', 'libx264',
        '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(silent)], stdin=subprocess.PIPE)
    tools, actions = 0, 0
    succeeded = False
    try:
        for i in range(count):
            if errors:
                raise RuntimeError('; '.join(errors))
            encoder.stdin.write(frame(page, i/fps))
            stats = page.evaluate('() => window.lastRenderStats')
            tools = max(tools, stats['visibleTools'])
            actions = max(actions, stats['activeActions'])
            if tools > 1 or actions > 1:
                raise RuntimeError(f'Concurrent drawing detected: {stats}')
            if i % max(1, fps*3) == 0:
                print(f'frame {i}/{count}', flush=True)
        succeeded = True
    finally:
        if encoder.stdin:
            encoder.stdin.close()
        code = encoder.wait()
    if not succeeded or code:
        raise RuntimeError('Frame encoding failed')
    tracks = []
    if audio_dir:
        tracks = [audio_dir/x for x in ('narration.wav', 'sfx.wav') if (audio_dir/x).is_file()]
    if tracks:
        command = ['ffmpeg', '-y', '-loglevel', 'error', '-i', str(silent)]
        for track in tracks:
            command.extend(['-i', str(track)])
        inputs = ''.join(f'[{i+1}:a]' for i in range(len(tracks)))
        filters = inputs + f'amix=inputs={len(tracks)}:duration=longest:normalize=0,apad,alimiter=limit=0.95[a]'
        command += ['-filter_complex', filters, '-map', '0:v:0', '-map', '[a]', '-c:v', 'copy', '-c:a', 'aac',
                    '-b:a', '192k', '-t', str(duration), '-movflags', '+faststart', str(final)]
        subprocess.run(command, check=True)
    else:
        shutil.copy2(silent, final)
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(final)], text=True))
    video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    if abs(float(probe['format']['duration'])-duration) > .05 or int(video['nb_frames']) != count:
        raise RuntimeError('Export duration/frame count mismatch')
    report = {'output': str(final.resolve()), 'durationSeconds': float(probe['format']['duration']),
              'width': video['width'], 'height': video['height'], 'fps': video['avg_frame_rate'], 'frames': count,
              'maxVisibleToolsAcrossAllFrames': tools, 'maxConcurrentInkActionsAcrossAllFrames': actions,
              'browserErrors': errors, 'toolActions': len(doc.get('toolTrack', [])), 'audioInputTracks': len(tracks),
              'encodedAudioStreams': sum(s['codec_type']=='audio' for s in probe['streams']),
              'finalHoldMs': doc['durationMs']-max((x['endMs'] for x in doc.get('toolTrack', [])), default=0)}
    (out/'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path, help='Compiled project directory')
    parser.add_argument('--timeline', default='timeline.json')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--name', default='explainer.mp4')
    parser.add_argument('--audio', type=Path)
    parser.add_argument('--fps', type=int)
    parser.add_argument('--browser')
    parser.add_argument('--sheet', action='store_true')
    parser.add_argument('--video', action='store_true')
    args = parser.parse_args()
    project = args.project.resolve()
    doc = json.loads((project/args.timeline).read_text(encoding='utf-8-sig'))
    if args.video and doc.get('narrationTimingStatus') == 'needs-realignment':
        raise ValueError('Narration timing needs re-alignment before exporting video')
    drawing_report = inspect_structure(doc)
    if all_drawn_enabled(doc):
        drawing_report['timelineSha256'] = hashlib.sha256((project/args.timeline).read_bytes()).hexdigest()
        write_report(project/'all-drawn-check.json', drawing_report)
    validate_document(doc, project, scheduled=True)
    out = (args.output or project/'output').resolve()
    out.mkdir(parents=True, exist_ok=True)
    width, height = doc['canvas']['width'], doc['canvas']['height']
    audio = (args.audio or project/'audio').resolve()
    if args.video and (audio/'narration.wav').is_file():
        checker = Path(__file__).with_name('check_audio.py')
        if audio == (project/'audio').resolve():
            subprocess.run([sys.executable, str(checker), '--project', str(project)], check=True)
        else:
            # An explicit alternate track is still measured, using this
            # project's policy rather than the default audio's cached report.
            # The standalone checker supports policy via its CLI; mirror the
            # known fields so alternate audio cannot bypass the export gate.
            policy = doc.get('narrationPolicy', {})
            command = [sys.executable, str(checker), '--audio', str(audio/'narration.wav'),
                       '--output', str(project/'audio-continuity.json')]
            for key, flag in [('profile', '--profile'), ('maxPauseMs', '--max-pause-ms'),
                              ('maxLeadMs', '--max-lead-ms'), ('maxTailMs', '--max-tail-ms'),
                              ('thresholdDb', '--threshold-db')]:
                if key in policy:
                    command.extend([flag, str(policy[key])])
            subprocess.run(command, check=True)
    query = {'export': '1', 'timeline': args.timeline}
    if audio.is_dir() and audio.is_relative_to(project):
        query['audio'] = audio.relative_to(project).as_posix()
    with serve(project) as base, sync_playwright() as p:
        browser = launch(p, args.browser)
        try:
            page = browser.new_page(viewport={'width': width, 'height': height}, device_scale_factor=1)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base+'/index.html?'+urlencode(query), wait_until='load')
            page.evaluate('() => window.ready')
            if errors:
                raise RuntimeError('; '.join(errors))
            layout = page.evaluate('() => window.layoutReport || []')
            outside = [item for item in layout if item.get('type') == 'text' and (item.get('outOfCanvas') or item.get('outsideCanvas'))]
            if outside:
                raise RuntimeError('Text exceeds canvas: '+json.dumps(outside, ensure_ascii=False))
            if args.sheet or not args.video:
                contact_sheet(page, out, doc['durationMs']/1000, width, height)
            if args.video:
                if all_drawn_enabled(doc):
                    # Use a separate tools-hidden page, so sample comparisons
                    # measure only board marks and never the moving pencil.
                    check_page = browser.new_page(viewport={'width': width, 'height': height}, device_scale_factor=1)
                    try:
                        check_page.goto(base+'/index.html?'+urlencode({**query, 'hideTools': '1'}), wait_until='load')
                        check_page.evaluate('() => window.ready')
                        drawing_report = inspect_pixels(check_page, doc, drawing_report, args.fps or doc.get('fps', 30))
                        write_report(project/'all-drawn-check.json', drawing_report)
                        if not drawing_report['passed']:
                            raise RuntimeError('All-drawn pixel checks failed before encoding: '+ '; '.join(drawing_report['violations']))
                    finally:
                        check_page.close()
                export(page, errors, out, doc, args.fps or doc.get('fps', 30), args.name, audio)
        finally:
            browser.close()

if __name__ == '__main__':
    main()
