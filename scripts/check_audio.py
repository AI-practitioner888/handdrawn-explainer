"""Check real narration WAV silence; never inspect mixed audio or tool sounds.

Continuous narration is the default. Explicit intentional/silent profiles retain
the waveform measurements without rejecting deliberate silence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import wave
from pathlib import Path

import numpy as np

DEFAULT_POLICY = {
    "profile": "continuous", "maxPauseMs": 500.0, "maxLeadMs": 150.0,
    "maxTailMs": 300.0, "thresholdDb": -45.0, "frameMs": 10.0,
}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def normalize_policy(policy: dict | None = None) -> dict:
    result = {**DEFAULT_POLICY, **(policy or {})}
    if result["profile"] not in {"continuous", "intentional", "silent"}:
        raise ValueError("narrationPolicy.profile must be continuous, intentional, or silent")
    for key in ("maxPauseMs", "maxLeadMs", "maxTailMs", "thresholdDb", "frameMs"):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"narrationPolicy.{key} must be a finite number")
        result[key] = float(value)
    if any(result[key] < 0 for key in ("maxPauseMs", "maxLeadMs", "maxTailMs")):
        raise ValueError("Silence limits cannot be negative")
    if not -180 <= result["thresholdDb"] <= 0:
        raise ValueError("thresholdDb must be between -180 and 0 dBFS")
    if not 0 < result["frameMs"] <= 100:
        raise ValueError("frameMs must be greater than 0 and at most 100")
    return result


def load_pcm(path: Path) -> tuple[np.ndarray, dict]:
    """Read integer PCM WAV, preserving channels for RMS without cancellation."""
    raw_file = path.read_bytes()
    with wave.open(str(path), "rb") as source:
        channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        frames = source.getnframes()
        if source.getcomptype() != "NONE" or channels < 1 or rate < 1:
            raise ValueError("Expected uncompressed integer PCM WAV")
        raw = source.readframes(frames)
    if width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128
    elif width in (2, 4):
        samples = np.frombuffer(raw, dtype=f"<i{width}").astype(np.float64) / (2 ** (width * 8 - 1))
    elif width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        integers = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        integers = (integers ^ 0x800000) - 0x800000
        samples = integers.astype(np.float64) / 8388608
    else:
        raise ValueError("Supported PCM sample widths are 8, 16, 24, and 32 bits")
    if samples.size != frames * channels:
        raise ValueError("WAV data is truncated")
    if not frames:
        raise ValueError("Narration WAV contains no samples")
    return samples.reshape(frames, channels), {
        "path": str(path.resolve()), "sha256": hashlib.sha256(raw_file).hexdigest(),
        "format": "integer-pcm-wav", "sampleRate": rate, "channels": channels,
        "sampleWidthBits": width * 8, "frames": frames,
        "durationMs": round(frames * 1000 / rate, 6),
    }


def waveform_intervals(samples: np.ndarray, sample_rate: int, policy: dict) -> tuple[list[dict], bool]:
    frame_samples = max(1, round(sample_rate * policy["frameMs"] / 1000))
    frame_starts = np.arange(0, len(samples), frame_samples)
    energy = np.mean(samples ** 2, axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(energy)))
    frame_ends = np.minimum(frame_starts + frame_samples, len(samples))
    rms = np.sqrt(np.maximum(0, (cumulative[frame_ends] - cumulative[frame_starts]) / (frame_ends - frame_starts)))
    silent = rms <= 10 ** (policy["thresholdDb"] / 20)
    intervals = []
    start = None
    for index in range(len(silent) + 1):
        if index < len(silent) and silent[index]:
            if start is None:
                start = index
        elif start is not None:
            first, last = int(frame_starts[start]), int(frame_ends[index - 1])
            kind = "entire_track" if first == 0 and last == len(samples) else "lead" if first == 0 else "tail" if last == len(samples) else "pause"
            limit = min(policy["maxLeadMs"], policy["maxTailMs"]) if kind == "entire_track" else policy[{"lead": "maxLeadMs", "tail": "maxTailMs", "pause": "maxPauseMs"}[kind]]
            duration = (last - first) * 1000 / sample_rate
            intervals.append({
                "kind": kind, "startMs": round(first * 1000 / sample_rate, 6),
                "endMs": round(last * 1000 / sample_rate, 6), "durationMs": round(duration, 6),
                "limitMs": limit, "exceedsLimit": duration > limit + 1e-6,
                "maxRmsDb": round(20 * math.log10(max(float(np.max(rms[start:index])), 1e-12)), 2),
            })
            start = None
    return intervals, bool(np.any(~silent))


def segment_gaps(segments: list[dict], duration_ms: float, policy: dict, *, windows: bool = False) -> list[dict]:
    """Report positive placement gaps; windowEndMs is not actual speech end."""
    rows = sorted((row for row in segments if isinstance(row.get("startMs"), (int, float))
                   and isinstance(row.get("endMs"), (int, float))), key=lambda row: row["startMs"])
    gaps = []
    previous_end, previous_id = 0.0, None
    for index, row in enumerate(rows):
        start = float(row["startMs"])
        if start > previous_end:
            kind = "lead" if index == 0 else "pause"
            limit = policy["maxLeadMs" if kind == "lead" else "maxPauseMs"]
            gaps.append({"kind": kind, "beforeId": previous_id, "afterId": row.get("id"),
                         "startMs": previous_end, "endMs": start, "durationMs": round(start - previous_end, 6),
                         "limitMs": limit, "exceedsLimit": start - previous_end > limit + 1e-6})
        end = float(row.get("windowEndMs", row["endMs"]) if windows else row["endMs"])
        if end > previous_end:
            previous_end, previous_id = end, row.get("id")
    if rows and duration_ms > previous_end:
        gap = duration_ms - previous_end
        gaps.append({"kind": "tail", "beforeId": previous_id, "afterId": None,
                     "startMs": previous_end, "endMs": duration_ms, "durationMs": round(gap, 6),
                     "limitMs": policy["maxTailMs"], "exceedsLimit": gap > policy["maxTailMs"] + 1e-6})
    return gaps


def inspect_audio(audio_path: Path, policy: dict | None = None, *, manifest: dict | None = None,
                  plan: dict | None = None) -> dict:
    policy = normalize_policy(policy)
    samples, source = load_pcm(audio_path)
    intervals, audible = waveform_intervals(samples, source["sampleRate"], policy)
    actual = segment_gaps((manifest or {}).get("segments", []), source["durationMs"], policy)
    window_rows = (plan or {}).get("segments")
    if window_rows is None:
        window_rows = (manifest or {}).get("segments", [])
    windows = segment_gaps(window_rows, source["durationMs"], policy, windows=True)
    long_silences = [interval for interval in intervals if interval["exceedsLimit"]]
    enforce = policy["profile"] == "continuous"
    violations = [{"check": "waveform-silence", **row} for row in long_silences] if enforce else []
    # Manifest placement gaps are reported separately. The actual narration
    # waveform is authoritative: metadata can never make mixed/foley qualify.
    if enforce and not audible:
        violations.append({"kind": "no-audible-narration", "thresholdDb": policy["thresholdDb"]})
    return {
        "schemaVersion": "handdrawn-explainer/audio-continuity/1", "passed": not violations,
        "checked": True, "analyzedTrack": "narration", "source": source,
        "narrationPolicy": policy, "enforced": enforce, "audibleNarrationDetected": audible,
        "analysis": {"method": "non-overlapping-window-rms", "measurementResolutionMs": round(max(1, round(source["sampleRate"] * policy["frameMs"] / 1000)) * 1000 / source["sampleRate"], 6),
                     "silenceThresholdDbfs": policy["thresholdDb"], "channelsCombinedAs": "mean-square"},
        "segmentWindowGaps": windows, "actualSegmentGaps": actual,
        "silentIntervals": intervals, "longSilentIntervals": long_silences, "violations": violations,
        "note": None if enforce else "Continuity limits are observational for an explicit intentional/silent profile.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--project", type=Path, help="Compiled project; reads timeline.narrationPolicy")
    source_group.add_argument("--audio", type=Path, help="Real narration WAV, not mixed audio")
    parser.add_argument("--output", type=Path, help="Defaults to PROJECT/audio-continuity.json")
    parser.add_argument("--profile", choices=("continuous", "intentional", "silent"))
    parser.add_argument("--max-pause-ms", type=float)
    parser.add_argument("--max-lead-ms", type=float)
    parser.add_argument("--max-tail-ms", type=float)
    parser.add_argument("--threshold-db", type=float)
    parser.add_argument("--frame-ms", type=float)
    args = parser.parse_args()
    if args.audio and not args.output:
        parser.error("--audio requires --output REPORT")
    root = args.project.resolve() if args.project else None
    output = args.output.resolve() if args.output else root / "audio-continuity.json"
    audio_path = root / "audio" / "narration.wav" if root else args.audio.resolve()
    policy = {}
    try:
        manifest, plan = {}, {}
        if root:
            timeline = read_json(root / "timeline.json")
            policy = timeline.get("narrationPolicy", {})
            if not isinstance(policy, dict):
                raise ValueError("timeline.narrationPolicy must be an object")
            for filename, name in (("audio_manifest.json", "manifest"), ("input-plan.json", "plan")):
                path = root / "audio" / filename
                if path.is_file():
                    if name == "manifest":
                        manifest = read_json(path)
                    else:
                        plan = read_json(path)
            if not plan and (root / "audio" / "plan.json").is_file():
                plan = read_json(root / "audio" / "plan.json")
            if "profile" not in policy and (plan.get("mode") == "silent" or manifest.get("mode") == "silent"):
                policy = {**policy, "profile": "silent"}
        overrides = {key: value for key, value in {
            "profile": args.profile, "maxPauseMs": args.max_pause_ms, "maxLeadMs": args.max_lead_ms,
            "maxTailMs": args.max_tail_ms, "thresholdDb": args.threshold_db, "frameMs": args.frame_ms,
        }.items() if value is not None}
        policy = normalize_policy({**policy, **overrides})
        if policy["profile"] == "silent" and not audio_path.is_file():
            report = {"schemaVersion": "handdrawn-explainer/audio-continuity/1", "passed": True,
                      "checked": False, "enforced": False, "analyzedTrack": "narration",
                      "narrationPolicy": policy, "source": None,
                      "note": "Explicit silent profile; no narration WAV is expected.", "violations": []}
        else:
            report = inspect_audio(audio_path, policy, manifest=manifest, plan=plan)
        if root:
            report["project"] = str(root)
            report["metadataSources"] = {
                "timeline": str(root / "timeline.json"),
                "manifest": str(root / "audio" / "audio_manifest.json") if manifest else None,
                "plan": str(root / "audio" / "input-plan.json") if (root / "audio" / "input-plan.json").is_file() else str(root / "audio" / "plan.json") if plan else None,
            }
    except (OSError, ValueError, TypeError, KeyError, wave.Error) as error:
        report = {"schemaVersion": "handdrawn-explainer/audio-continuity/1", "passed": False,
                  "checked": False, "analyzedTrack": "narration", "narrationPolicy": policy,
                  "source": {"path": str(audio_path)}, "errors": [str(error)]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # ASCII JSON keeps the CLI usable under Windows consoles with legacy encodings.
    print(json.dumps({"passed": report["passed"], "report": str(output), "profile": policy.get("profile"),
                      "longSilentIntervals": len(report.get("longSilentIntervals", [])),
                      "errors": report.get("errors", [])}, ensure_ascii=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
