"""Behavior tests for serial shading, paint, tools, erasure and deterministic seek.

Run: python scripts/test_effects.py --output /path/to/output
The retained project and PNG frames double as executable technique examples.
"""
from __future__ import annotations

import argparse
import base64
import functools
import io
import json
import os
import shutil
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

from schedule import ScheduleError, schedule_document

SKILL = Path(__file__).resolve().parents[1]
OUTPUT = Path.cwd() / "output" / "skill-runtime-test"


def rect(x, y, width, height):
    return f"M {x} {y} h {width} v {height} h {-width} Z"


def fixture():
    return {
        "title": "绘画技法与工具测试", "durationMs": 8000,
        "canvas": {"width": 640, "height": 360},
        "theme": {"paper": "#ffffff", "ink": "#222222", "dotSpacing": 0},
        "scenes": [{"id": "effects", "startMs": 0, "endMs": 8000, "elements": [
            {"id": "hatching", "type": "hatch", "d": rect(20, 25, 150, 120),
             "startMs": 0, "durationMs": 1000, "locked": True,
             "angle": 45, "spacing": 10, "width": 1.5, "color": "#40614e"},
            {"id": "shadow", "type": "crosshatch", "d": rect(205, 25, 150, 120),
             "startMs": 1200, "durationMs": 1200, "locked": True,
             "angle": 45, "crossAngle": 135, "spacing": 10, "width": 1.5,
             "color": "#354a41"},
            {"id": "color", "type": "paint", "d": rect(390, 25, 180, 120),
             "startMs": 2600, "durationMs": 1400, "locked": True,
             "angle": 0, "brushSize": 18, "spacing": 12, "color": "#dbaa53"},
            {"id": "erase", "type": "erase", "targets": ["color"],
             "region": {"x": 390, "y": 25, "width": 180, "height": 120},
             "startMs": 4300, "durationMs": 650, "locked": True, "brushSize": 24},
            {"id": "pen", "type": "stroke", "d": "M 35 215 L 170 215",
             "startMs": 5200, "durationMs": 400, "locked": True,
             "color": "#000000", "tool": "pen"},
            {"id": "marker", "type": "stroke", "d": "M 215 215 L 350 215",
             "startMs": 5800, "durationMs": 400, "locked": True,
             "color": "#000000", "tool": "marker"},
            {"id": "pencil", "type": "stroke", "d": "M 400 215 L 555 215",
             "startMs": 6400, "durationMs": 400, "locked": True,
             "color": "#000000", "tool": "pencil"},
        ]}], "cues": [],
    }


class ScheduleBehaviorTests(unittest.TestCase):
    def test_new_effects_and_hidden_tools_all_reserve_track(self):
        doc = fixture()
        doc["scenes"][0]["elements"][1]["pencil"] = False
        out = schedule_document(doc)
        self.assertEqual(len(out["toolTrack"]), 7)
        self.assertEqual(out["toolMode"], "serial")
        self.assertEqual(out["serialSchedule"]["maxSimultaneousInk"], 1)
        for a, b in zip(out["toolTrack"], out["toolTrack"][1:]):
            self.assertGreaterEqual(b["startMs"] - a["endMs"], 70)

    def test_paint_does_not_overlap_and_impossible_budget_rejects(self):
        doc = fixture()
        for e in doc["scenes"][0]["elements"]:
            e["locked"] = False
            e["startMs"] = 0
            e["durationMs"] = 100
        doc["durationMs"] = doc["scenes"][0]["endMs"] = 500
        with self.assertRaisesRegex(ScheduleError, "do not fit"):
            schedule_document(doc)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


class RuntimeBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SKILL / "assets/renderer/index.html", OUTPUT / "index.html")
        shutil.copy2(SKILL / "assets/renderer/renderer.js", OUTPUT / "renderer.js")
        (OUTPUT / "assets/fonts").mkdir(parents=True, exist_ok=True)
        shutil.copy2(SKILL / "assets/fonts/LXGWWenKai-Regular.ttf", OUTPUT / "assets/fonts/LXGWWenKai-Regular.ttf")
        cls.doc = schedule_document(fixture())
        (OUTPUT / "effects.project.json").write_text(json.dumps(fixture(), ensure_ascii=False, indent=2), encoding="utf-8")
        (OUTPUT / "timeline.json").write_text(json.dumps(cls.doc, ensure_ascii=False, indent=2), encoding="utf-8")
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(OUTPUT)))
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        chrome = Path(os.environ.get("HANDDRAWN_BROWSER", r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
        kwargs = {"headless": True}
        if chrome.exists():
            kwargs["executable_path"] = str(chrome)
        cls.browser = cls.playwright.chromium.launch(**kwargs)
        cls.page = cls.browser.new_page(viewport={"width": 640, "height": 360})
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.goto(f"http://127.0.0.1:{cls.http.server_port}/index.html?export=1")
        cls.page.evaluate("() => window.ready")

    @classmethod
    def tearDownClass(cls):
        # Retain unannotated pixels plus a labeled contact sheet for inspection.
        times = [0.5, 1.8, 3.0, 4.0, 4.7, 6.9]
        font = ImageFont.truetype(str(SKILL / "assets/fonts/LXGWWenKai-Regular.ttf"), 19)
        labels = ["斜排线逐笔出现", "第二组排线叠加", "色带逐笔涂满", "涂色完成", "橡皮擦除指定目标", "钢笔 / marker / 铅笔"]
        sheet = Image.new("RGB", (1280, 1188), "#eceee4")
        for i, (time, label) in enumerate(zip(times, labels)):
            frame = cls.frame(time)
            frame.save(OUTPUT / f"effect-{time:.1f}s.png")
            x, y = (i % 2) * 640, (i // 2) * 396
            sheet.paste(frame, (x, y))
            ImageDraw.Draw(sheet).text((x + 15, y + 367), f"{time:.1f}s  {label}", font=font, fill="#386d4d")
        sheet.save(OUTPUT / "effects-contact-sheet.png")
        (OUTPUT / "runtime-report.json").write_text(json.dumps({"browserErrors": cls.errors, "layoutReport": cls.page.evaluate("window.layoutReport"), "stats": cls.page.evaluate("window.debugTotals()")}, ensure_ascii=False, indent=2), encoding="utf-8")
        cls.browser.close()
        cls.playwright.stop()
        cls.http.shutdown()
        cls.http.server_close()

    @classmethod
    def frame(cls, seconds):
        raw = cls.page.evaluate("t => {renderAt(t, false);return document.querySelector('#board').toDataURL('image/png');}", seconds)
        return Image.open(io.BytesIO(base64.b64decode(raw.partition(",")[2]))).convert("RGB")

    def test_masked_effects_progress_and_tool_tip_inside_mask(self):
        for element_id, bounds, start, duration in [("hatching", (20, 25, 170, 145), 0, 1), ("shadow", (205, 25, 355, 145), 1.2, 1.2), ("color", (390, 25, 570, 145), 2.6, 1.4)]:
            def pixels(time):
                arr = np.array(self.frame(time))
                x0, y0, x1, y1 = bounds
                return np.any(arr[y0:y1, x0:x1] < 235, axis=2).sum()
            self.assertLess(pixels(start + duration * .12), pixels(start + duration * .65))
            for p in [.1, .3, .5, .7, .9]:
                self.page.evaluate("t => renderAt(t,false)", start + duration * p)
                tip = self.page.evaluate("window.lastRenderStats.tip")
                self.assertIsNotNone(tip)
                self.assertLessEqual(bounds[0] - 1, tip["x"])
                self.assertLessEqual(tip["x"], bounds[2] + 1)
                self.assertLessEqual(bounds[1] - 1, tip["y"])
                self.assertLessEqual(tip["y"], bounds[3] + 1)
        trace = self.page.evaluate("window.debugTrace('shadow')")
        slopes = {(1 if points[-1][0] > points[0][0] else -1) * (1 if points[-1][1] > points[0][1] else -1) for points in trace["parts"]}
        self.assertEqual(slopes, {-1, 1})

    def test_eraser_removes_target_preserving_neighbor(self):
        # Compare quiet frames; a lifted pen crossing a neighbor is not erasure.
        before, after = np.array(self.frame(4.1)), np.array(self.frame(5.0))
        self.assertGreater(np.any(before[25:145, 390:570] < 235, axis=2).sum(), 15000)
        self.assertLess(np.any(after[25:145, 390:570] < 235, axis=2).sum(), 10)
        self.assertTrue(np.array_equal(before[25:145, 205:355], after[25:145, 205:355]))

    def test_tools_change_actual_ink_width_and_transparency(self):
        arr = np.array(self.frame(7.0))
        pen = np.min(arr[190:240, 90], axis=1)
        marker = np.min(arr[190:240, 270], axis=1)
        self.assertGreater((marker < 235).sum(), (pen < 235).sum() * 3)
        self.assertGreater(marker.min(), pen.min() + 70)

    def test_single_tool_reverse_seek_determinism_and_layout(self):
        self.page.evaluate("window.resetRenderStats()")
        forward = {}
        for time in [i / 20 for i in range(161)]:
            if time in {1.0, 2.0, 4.0, 7.0}:
                forward[time] = self.frame(time).tobytes()
            else:
                self.page.evaluate("t => renderAt(t,false)", time)
            stats = self.page.evaluate("window.lastRenderStats")
            self.assertLessEqual(stats["visibleTools"], 1)
            self.assertLessEqual(stats["concurrentInk"], 1)
        for time in sorted(forward, reverse=True):
            self.assertEqual(self.frame(time).tobytes(), forward[time])
        self.assertEqual(self.page.evaluate("[document.querySelector('#board').width,document.querySelector('#board').height]"), [640, 360])
        self.assertFalse(any(e["outOfCanvas"] for e in self.page.evaluate("window.layoutReport")))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    OUTPUT = args.output.resolve()
    unittest.main(argv=[__file__], verbosity=2)
