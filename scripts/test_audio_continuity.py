"""Synthetic, offline regression tests for real narration continuity checks."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from check_audio import inspect_audio

RATE = 16000


def tone(ms: float, level_db: float = -18) -> np.ndarray:
    seconds = np.arange(round(ms * RATE / 1000)) / RATE
    return np.sin(2 * np.pi * 440 * seconds) * (10 ** (level_db / 20)) * np.sqrt(2)


def quiet(ms: float) -> np.ndarray:
    return np.zeros(round(ms * RATE / 1000))


def write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(RATE)
        target.writeframes(np.round(samples * 32767).astype("<i2").tobytes())


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wav = self.root / "narration.wav"

    def tearDown(self):
        self.tmp.cleanup()

    def analyze(self, samples, policy=None, **kwargs):
        write_wav(self.wav, samples)
        return inspect_audio(self.wav, policy, **kwargs)

    def test_long_pause_is_rejected(self):
        report = self.analyze(np.concatenate([tone(800), quiet(800), tone(800)]))
        self.assertFalse(report["passed"])
        self.assertEqual(report["longSilentIntervals"][0]["kind"], "pause")
        self.assertEqual(report["longSilentIntervals"][0]["durationMs"], 800)

    def test_continuous_legal_narration(self):
        report = self.analyze(np.concatenate([quiet(100), tone(800), quiet(200), tone(800), quiet(150)]))
        self.assertTrue(report["passed"])
        self.assertEqual(report["longSilentIntervals"], [])
        self.assertEqual(len(report["source"]["sha256"]), 64)
        self.assertEqual(report["analyzedTrack"], "narration")

    def test_lead_and_tail_limits(self):
        report = self.analyze(np.concatenate([quiet(200), tone(800), quiet(400)]))
        self.assertFalse(report["passed"])
        self.assertEqual([row["kind"] for row in report["longSilentIntervals"]], ["lead", "tail"])

    def test_threshold_is_configurable(self):
        self.assertFalse(self.analyze(tone(1000, -50))["passed"])
        self.assertTrue(self.analyze(tone(1000, -50), {"thresholdDb": -55})["passed"])

    def test_limits_are_configurable(self):
        report = self.analyze(np.concatenate([quiet(200), tone(800), quiet(800), tone(800), quiet(400)]),
                              {"maxLeadMs": 250, "maxPauseMs": 850, "maxTailMs": 450})
        self.assertTrue(report["passed"])

    def test_silent_and_intentional_profiles_allow_explicit_silence(self):
        for profile in ("silent", "intentional"):
            with self.subTest(profile=profile):
                report = self.analyze(quiet(2000), {"profile": profile})
                self.assertTrue(report["passed"])
                self.assertFalse(report["enforced"])
                self.assertFalse(report["audibleNarrationDetected"])
                self.assertEqual(report["silentIntervals"][0]["kind"], "entire_track")
        self.assertFalse(self.analyze(quiet(2000))["passed"])

    def test_actual_gaps_are_distinct_from_window_gaps(self):
        manifest = {"segments": [{"id": "a", "startMs": 0, "endMs": 800, "windowEndMs": 1300},
                                  {"id": "b", "startMs": 1400, "endMs": 2200, "windowEndMs": 2200}]}
        report = self.analyze(np.concatenate([tone(800), quiet(600), tone(800)]), manifest=manifest)
        self.assertEqual(report["actualSegmentGaps"][0]["durationMs"], 600)
        self.assertEqual(report["segmentWindowGaps"][0]["durationMs"], 100)
        self.assertFalse(report["passed"])

    def test_cli_failure_exit_and_project_policy(self):
        project = self.root / "project"
        (project / "audio").mkdir(parents=True)
        write_wav(project / "audio" / "narration.wav", np.concatenate([tone(600), quiet(700), tone(600)]))
        (project / "timeline.json").write_text(json.dumps({"narrationPolicy": {"profile": "continuous"}}), encoding="utf-8")
        command = [sys.executable, str(Path(__file__).with_name("check_audio.py")), "--project", str(project)]
        failure = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(failure.returncode, 2, failure.stderr)
        report = json.loads((project / "audio-continuity.json").read_text(encoding="utf-8"))
        self.assertFalse(report["passed"])
        self.assertTrue(report["source"]["path"].endswith("narration.wav"))
        success = subprocess.run(command + ["--max-pause-ms", "750"], capture_output=True, text=True)
        self.assertEqual(success.returncode, 0, success.stderr)

    def test_project_silent_mode_is_not_rejected(self):
        project = self.root / "silent-project"
        (project / "audio").mkdir(parents=True)
        (project / "timeline.json").write_text("{}", encoding="utf-8")
        (project / "audio" / "input-plan.json").write_text('{"mode":"silent","segments":[]}', encoding="utf-8")
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("check_audio.py")), "--project", str(project)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((project / "audio-continuity.json").read_text(encoding="utf-8"))
        self.assertTrue(report["passed"])
        self.assertEqual(report["narrationPolicy"]["profile"], "silent")

    def test_audio_only_cli_reports_failure_and_explicit_intentional(self):
        write_wav(self.wav, np.concatenate([tone(600), quiet(900), tone(600)]))
        output = self.root / "audio-report.json"
        command = [sys.executable, str(Path(__file__).with_name("check_audio.py")),
                   "--audio", str(self.wav), "--output", str(output)]
        failure = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(failure.returncode, 2, failure.stderr)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["passed"])
        intentional = subprocess.run(command + ["--profile", "intentional"], capture_output=True, text=True)
        self.assertEqual(intentional.returncode, 0, intentional.stderr)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["longSilentIntervals"]), 1)

    def test_stereo_does_not_cancel_active_narration(self):
        left = tone(1000)
        stereo = np.stack((left, -left), axis=1)
        with wave.open(str(self.wav), "wb") as target:
            target.setnchannels(2)
            target.setsampwidth(2)
            target.setframerate(RATE)
            target.writeframes(np.round(stereo * 32767).astype("<i2").tobytes())
        report = inspect_audio(self.wav)
        self.assertTrue(report["passed"])
        self.assertEqual(report["source"]["channels"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
