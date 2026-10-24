"""Build timed narration and optional single-tool sound from a portable JSON plan.

Input plan: durationMs, optional voice/maxSpeed, segments[{id,startMs,endMs,text,
spokenText?,audioFile?,trimSilence?}]. Files are relative to the plan directory.
Existing audioFile wins over TTS. Set mode="silent" deliberately for no speech.
The optional Edge TTS adapter is free to use and stores content-addressed sources.
Speech that cannot fit at maxSpeed raises an error; nothing is truncated.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 48000
SOURCE_LOCKS: dict[str, asyncio.Lock] = {}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stderr[-3000:]}")


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != SAMPLE_RATE:
            raise ValueError("Expected mono 48 kHz signed 16-bit WAV")
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float64) / 32768


def write_wav(path: Path, samples: np.ndarray) -> None:
    if not np.all(np.isfinite(samples)):
        raise ValueError("Non-finite audio samples")
    if np.max(np.abs(samples), initial=0) > 1.0:
        raise ValueError("Audio would clip; lower segment or effects gain")
    pcm = np.round(samples * 32767).astype("<i2")
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(pcm.tobytes())


def trim_silence(samples: np.ndarray) -> np.ndarray:
    frame = 480
    active = [i for i in range(0, len(samples), frame)
              if float(np.sqrt(np.mean(samples[i:i + frame] ** 2))) > 0.002]
    if not active:
        raise ValueError("Audio has no audible speech; disable trimSilence for intentional silence")
    pad = round(0.04 * SAMPLE_RATE)
    return samples[max(0, active[0] - pad):min(len(samples), active[-1] + frame + pad)]


def db(value: float) -> float:
    return round(20 * math.log10(max(value, 1e-9)), 2)


def metrics(samples: np.ndarray) -> dict:
    active = samples[np.abs(samples) > 0.001]
    return {
        "durationMs": round(len(samples) * 1000 / SAMPLE_RATE, 6),
        "peakDbfs": db(float(np.max(np.abs(samples), initial=0))),
        "rmsDbfs": db(float(np.sqrt(np.mean(samples ** 2)))) if len(samples) else -180,
        "activeRmsDbfs": db(float(np.sqrt(np.mean(active ** 2)))) if len(active) else -180,
        "clippedSamples": int(np.sum(np.abs(samples) >= 0.999)),
    }


def timecode(ms: float) -> str:
    h, rest = divmod(round(ms), 3600000)
    m, rest = divmod(rest, 60000)
    s, rest = divmod(rest, 1000)
    return f"{h:02}:{m:02}:{s:02},{rest:03}"


def validated_plan(plan: dict) -> list[dict]:
    duration = plan.get("durationMs")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Plan durationMs must be positive")
    mode = plan.get("mode", "auto")
    if mode not in ("auto", "files", "edge", "silent"):
        raise ValueError("mode must be auto, files, edge, or silent")
    max_speed = float(plan.get("maxSpeed", 1.15))
    if not 1 <= max_speed <= 1.5:
        raise ValueError("maxSpeed must be between 1 and 1.5")
    segments = [dict(segment) for segment in plan.get("segments", [])]
    ids = set()
    for i, segment in enumerate(segments):
        segment.setdefault("id", f"segment-{i + 1}")
        identifier = segment["id"]
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError("Segment ids must be unique nonempty strings")
        ids.add(identifier)
        start, end = segment.get("startMs"), segment.get("endMs")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError(f"{identifier}: explicit startMs/endMs required")
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration:
            raise ValueError(f"{identifier}: invalid time window")
        if mode != "silent" and not segment.get("audioFile") and not segment.get("spokenText", segment.get("text")):
            raise ValueError(f"{identifier}: provide text or audioFile")
        if mode == "files" and not segment.get("audioFile"):
            raise ValueError(f"{identifier}: files mode requires audioFile")
    segments.sort(key=lambda segment: segment["startMs"])
    for left, right in zip(segments, segments[1:]):
        if left["endMs"] > right["startMs"]:
            raise ValueError(f"Narration windows overlap: {left['id']} / {right['id']}")
    if mode != "silent" and not segments:
        raise ValueError("No speech segments supplied; choose mode=\"silent\" explicitly")
    return segments


async def get_source(segment: dict, plan: dict, plan_dir: Path, cache: Path) -> tuple[Path, dict]:
    if segment.get("audioFile"):
        original = (plan_dir / segment["audioFile"]).resolve()
        if not original.is_file():
            raise FileNotFoundError(f"Audio source does not exist: {original}")
        digest = sha256(original)
        source = cache / (digest[:24] + original.suffix.lower())
        if source.resolve() != original:
            shutil.copy2(original, source)
        return source, {"provider": "supplied-file", "sourceSha256": digest}
    try:
        import edge_tts
    except ImportError:
        raise RuntimeError("edge-tts is optional; install it for speech synthesis or provide audioFile") from None
    caption = segment.get("text", "")
    config = {
        "text": segment.get("spokenText", caption),
        "voice": segment.get("voice", plan.get("voice", "zh-CN-XiaoxiaoNeural")),
        "rate": segment.get("rate", "+0%"), "pitch": segment.get("pitch", "+0Hz"),
    }
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    source = cache / (digest[:24] + ".mp3")
    lock = SOURCE_LOCKS.setdefault(str(source.resolve()), asyncio.Lock())
    async with lock:
        if not source.exists() or source.stat().st_size < 1024:
            partial = source.with_name(source.stem + "-partial.mp3")
            for attempt in range(3):
                try:
                    await edge_tts.Communicate(config["text"], config["voice"],
                                               rate=config["rate"], pitch=config["pitch"]).save(str(partial))
                    partial.replace(source)
                    break
                except Exception:
                    partial.unlink(missing_ok=True)
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1 + attempt)
    return source, {"provider": "edge-tts", "config": config, "configSha256": digest,
                    "sourceSha256": sha256(source)}


def decode(source: Path) -> tuple[np.ndarray, float]:
    decoded = source.with_name(source.stem + "-decoded.wav")
    if not decoded.exists():
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
             "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(decoded)])
    samples = read_wav(decoded)
    return samples, len(samples) * 1000 / SAMPLE_RATE


def foley_events(timeline: dict, duration_ms: float) -> list[dict]:
    if float(timeline.get("durationMs", 0)) != float(duration_ms):
        raise ValueError("Timeline and audio plan durationMs must match")
    lookup = {("global", element["id"]): element for element in timeline.get("elements", [])}
    lookup.update({(scene["id"], element["id"]): element
                   for scene in timeline.get("scenes", []) for element in scene.get("elements", [])})
    events = []
    for action in timeline.get("toolTrack", []):
        tool = action.get("tool", "pencil")
        if tool not in ("pencil", "pen", "marker", "eraser"):
            raise ValueError(f"Unsupported foley tool: {tool}")
        start, end = float(action["startMs"]), float(action["endMs"])
        if not 0 <= start < end <= duration_ms:
            raise ValueError(f"Invalid toolTrack action: {action.get('elementId')}")
        element = lookup.get((action.get("sceneId", "global"), action.get("elementId")), {})
        if element.get("type") == "image" and element.get("mode") == "sketch":
            end = start + (end - start) * float(element.get("inkPhase", 0.82))
        events.append({"tool": tool, "startMs": start, "endMs": end,
                       "actionEndMs": action["endMs"], "sceneId": action.get("sceneId", "global"),
                       "elementId": action.get("elementId")})
    events.sort(key=lambda event: event["startMs"])
    for left, right in zip(events, events[1:]):
        if left["endMs"] > right["startMs"]:
            raise ValueError("Foley actions overlap; run serial scheduler first")
    return events


def make_sfx(duration_ms: float, events: list[dict], level: float = 1.0) -> np.ndarray:
    if not math.isfinite(level) or not 0 <= level <= 3:
        raise ValueError("sfxGain must be between 0 and 3")
    audio = np.zeros(round(duration_ms * SAMPLE_RATE / 1000), dtype=np.float64)
    rng = np.random.default_rng(29)
    for event in events:
        start = round(event["startMs"] * SAMPLE_RATE / 1000)
        end = round(event["endMs"] * SAMPLE_RATE / 1000)
        n = end - start
        if n <= 0:
            continue
        t = np.arange(n) / SAMPLE_RATE
        noise = rng.normal(0, 1, n)
        if event["tool"] == "eraser":
            texture = np.convolve(noise, np.ones(min(20, n)) / np.sqrt(min(20, n)), mode="same")
            motion = 0.25 + 0.75 * np.sin(2 * np.pi * 4.4 * t) ** 2
            amplitude = 0.0023
        elif event["tool"] == "marker":
            texture = np.convolve(noise, np.ones(min(5, n)) / np.sqrt(min(5, n)), mode="same")
            motion = 0.45 + 0.55 * np.sin(2 * np.pi * 2.0 * t) ** 2
            amplitude = 0.0013
        else:
            texture = noise if n == 1 else np.convolve(noise, np.array([1.0, -0.8]), mode="same")
            motion = 0.4 + 0.6 * np.sin(2 * np.pi * 2.8 * t) ** 2
            amplitude = 0.0014 if event["tool"] == "pencil" else 0.0008
        fade = np.minimum(1, t / 0.045) * np.minimum(1, np.maximum(0, (n - 1) / SAMPLE_RATE - t) / 0.045)
        audio[start:end] += texture * motion * fade * amplitude * level
    return audio


async def build(plan: dict, out: Path, *, plan_dir: Path, timeline_path: Path | None = None,
                plan_path: Path | None = None) -> dict:
    segments = validated_plan(plan)
    duration_ms = float(plan["durationMs"])
    silent = plan.get("mode") == "silent"
    if not silent and not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg must be on PATH to decode supplied audio or Edge TTS speech")
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "sources"
    cache.mkdir(exist_ok=True)
    write_json(out / "input-plan.json", plan)
    timeline = read_json(timeline_path) if timeline_path else None
    if timeline:
        write_json(out / "timeline.json", timeline)
    events = foley_events(timeline, duration_ms) if timeline else []
    track = np.zeros(round(duration_ms * SAMPLE_RATE / 1000), dtype=np.float64)
    manifest = {"durationMs": duration_ms, "sampleRate": SAMPLE_RATE,
                "mode": "silent" if silent else plan.get("mode", "auto"),
                "maxSpeed": plan.get("maxSpeed", 1.15), "segments": [], "foleyEvents": events,
                "travelIsSilent": True, "sketchColorFadeIsSilent": True,
                "inputPlanSha256": sha256(out / "input-plan.json")}
    generated = [] if silent else await asyncio.gather(*(get_source(segment, plan, plan_dir, cache)
                                                        for segment in segments))
    portable_plan = dict(plan)
    portable_plan["segments"] = [dict(segment) for segment in segments]
    if not silent:
        for segment, (source, provenance) in zip(portable_plan["segments"], generated):
            segment["audioFile"] = source.relative_to(out).as_posix()
            segment.setdefault("trimSilence", provenance["provider"] == "edge-tts")
    write_json(out / "plan.json", portable_plan)
    manifest["planSha256"] = sha256(out / "plan.json")
    srt = []
    for i, segment in enumerate(segments):
        caption = segment.get("text", "")
        start, limit = float(segment["startMs"]), float(segment["endMs"])
        if silent:
            if caption:
                srt.append(f"{len(srt) + 1}\n{timecode(start)} --> {timecode(limit)}\n{caption}\n")
            manifest["segments"].append({"id": segment["id"], "text": caption, "startMs": start,
                                         "endMs": limit, "speech": False})
            continue
        source, provenance = generated[i]
        samples, raw_ms = decode(source)
        do_trim = segment.get("trimSilence", provenance["provider"] == "edge-tts")
        if do_trim:
            samples = trim_silence(samples)
        trimmed_ms = len(samples) * 1000 / SAMPLE_RATE
        available_ms = limit - start
        max_speed = float(plan.get("maxSpeed", 1.15))
        required_speed = trimmed_ms / available_ms
        if required_speed > max_speed:
            raise ValueError(f"{segment['id']}: {trimmed_ms:.1f} ms speech does not fit "
                             f"{available_ms:.1f} ms at maxSpeed={max_speed}; shorten text or extend window")
        speed = 1.0 if required_speed <= 1 else min(max_speed, required_speed * 1.008)
        identifier = re.sub(r"[^A-Za-z0-9_-]+", "_", segment["id"]).strip("_") or f"segment-{i + 1}"
        final_file = out / f"{i + 1:02}-{identifier}.wav"
        if speed > 1:
            trim_file = cache / f"{source.stem}-trim-{i}.wav"
            write_wav(trim_file, samples)
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(trim_file),
                 "-af", f"atempo={speed:.8f}", "-ac", "1", "-ar", str(SAMPLE_RATE),
                 "-c:a", "pcm_s16le", str(final_file)])
            samples = read_wav(final_file)
        gain = 1.0
        if segment.get("normalize", plan.get("normalize", True)):
            rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0
            peak = float(np.max(np.abs(samples), initial=0))
            if rms > 0 and peak > 0:
                gain = min(10 ** (-19 / 20) / rms, 10 ** (-2 / 20) / peak)
        gain *= 10 ** (float(segment.get("gainDb", 0)) / 20)
        samples *= gain
        begin = round(start * SAMPLE_RATE / 1000)
        end = begin + len(samples)
        if end > round(limit * SAMPLE_RATE / 1000):
            raise ValueError(f"{segment['id']}: decoded speech overruns its window; no audio was truncated")
        track[begin:end] = samples
        write_wav(final_file, samples)
        end_ms = end * 1000 / SAMPLE_RATE
        if caption:
            srt.append(f"{len(srt) + 1}\n{timecode(start)} --> {timecode(end_ms)}\n{caption}\n")
        manifest["segments"].append({
            "id": segment["id"], "text": caption, "startMs": start, "endMs": round(end_ms, 6),
            "windowEndMs": limit, "sourceFile": source.relative_to(out).as_posix(),
            "file": final_file.relative_to(out).as_posix(), "sourceDurationMs": round(raw_ms, 6),
            "trimmedDurationMs": round(trimmed_ms, 6), "speed": round(speed, 8), "gainDb": db(gain),
            "fileSha256": sha256(final_file), **provenance, **metrics(samples),
        })
    effects = make_sfx(duration_ms, events, float(plan.get("sfxGain", 1)))
    mixed = track + effects
    write_wav(out / "narration.wav", track)
    write_wav(out / "sfx.wav", effects)
    write_wav(out / "mixed.wav", mixed)
    (out / "narration.srt").write_text("\n".join(srt), encoding="utf-8")
    manifest["narration"] = metrics(track)
    manifest["sfx"] = {"source": "Original synthesized tool textures; no sampled music", **metrics(effects)}
    manifest["mix"] = metrics(mixed)
    manifest["artifacts"] = {name: {"path": name, "sha256": sha256(out / name)}
                             for name in ("narration.wav", "sfx.wav", "mixed.wav", "narration.srt")}
    if timeline_path:
        manifest["timelineSha256"] = sha256(out / "timeline.json")
    write_json(out / "audio_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", type=Path)
    source.add_argument("--silent", action="store_true", help="Explicitly make a speechless track")
    parser.add_argument("--duration-ms", type=float, help="Required with --silent")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, help="Scheduled timeline for quiet tool foley")
    args = parser.parse_args()
    if args.silent:
        if args.duration_ms is None:
            parser.error("--silent requires --duration-ms")
        plan = {"durationMs": args.duration_ms, "mode": "silent", "segments": []}
        plan_dir = Path.cwd()
    else:
        if args.duration_ms is not None:
            parser.error("Use plan.durationMs with --plan")
        plan = read_json(args.plan)
        plan_dir = args.plan.resolve().parent
    manifest = asyncio.run(build(plan, args.output_dir.resolve(), plan_dir=plan_dir,
                                 timeline_path=args.timeline, plan_path=args.plan))
    print(json.dumps({"durationMs": manifest["durationMs"], "mode": manifest["mode"],
                      "segments": len(manifest["segments"]), "foleyEvents": len(manifest["foleyEvents"]),
                      "mix": manifest["mix"], "outputDir": str(args.output_dir.resolve())},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
