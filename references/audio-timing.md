# Continuous speech and narration-led drawing

Use this reference for narration placement and timing revisions. The visible storyboard should carry exact words and objects; this file describes the timing checks, not an additional story template.

## Audition before locking the board

1. Write complete useful content for the requested duration, in the requested natural voice. Generate/import an audition. `audio.py` stores content-addressed TTS sources and trims boundary silence by default; it never truncates speech to fit.
2. Measure real spoken duration. Prefer word/sentence boundaries from the speech provider or a verified alignment. If phrase markers are estimated, label them as estimates and check the audio against the visual preview.
3. Place scene boundaries at semantic changes in the spoken explanation. Audio can continue uninterrupted over a scene cut. Place object contours, color, labels, and flow indicators around their corresponding phrases.
4. Finish useful drawing in the available beat. Simplify small details or retain art already drawn on the same continuous board. A new all-drawn board must draw that object again. Preserve one tool and short travel gaps. Do not add empty speech windows to wait for the drawing.
5. Run waveform continuity checks and review important phrase/reveal matches. A 30-second container holding 20 seconds of narration is not a continuous 30-second explanation.

An uninterrupted recording is often convenient for a short explainer. Multiple clips are also supported: place the next clip after the preceding clip's **actual end**, with only the selected natural pause. Do not chain clips from their allocated window ends. For local revision, retained sentence/phrase clips may be easier to replace; continuity rules still apply.

## Project policy

```json
{
  "narrationPolicy": {
    "profile": "continuous",
    "maxPauseMs": 500,
    "maxLeadMs": 150,
    "maxTailMs": 300,
    "thresholdDb": -45
  }
}
```

`continuous` is the default for newly built narrated explainers. Use `intentional` when the user wants pauses to solve a problem, reflect, or read; use `silent` for an explicitly speechless piece. Preserve user-selected policy on revision. Never change a failed continuous project to intentional merely to bypass a gap.

The defaults allow normal breaths while rejecting multi-second silence. `check_audio.py` measures 10ms RMS windows in `audio/narration.wav`, with the configured threshold. It does not examine the mixed soundtrack for continuity, so tool texture or music cannot hide a missing explanation. Very quiet imported voices may require gain adjustment or an appropriate documented threshold; waveform quiet detection is not semantic speech recognition.

```text
python <skill>/scripts/check_audio.py --project <work>/build
python <skill>/scripts/check_audio.py --audio <voice.wav> --output <report.json>
```

The project command reads `timeline.json`, the audio manifest, and the portable plan. It writes `audio-continuity.json`, including waveform intervals, the voice file hash, policy, manifest segment gaps, and separately labelled plan-window gaps. A failed continuous check exits with code 2. `build.py` and video export invoke this check; correct the source/audio placement before exporting.

## Maintain phrase evidence and stable revisions

Optional `narrationBeats` in project JSON can record `{id,phrase,startMs,endMs,timingSource}` using **global** measured times. `speechBeat` on an element associates it with a beat. These are editorial metadata, not an automatic aligner; verify the visible action against the marked phrase. Keep the source of timing evidence with the project, e.g. provider word boundaries after known trimming/rate changes.

If a narration sentence changes, update its source recording and timing evidence. An explicit `audioFile` wins over text in `audio.py`; editing only the text would otherwise play the old clip. Keep generated-recording provenance and replace the corresponding binding, or explicitly request regeneration through the revision helper. Do not discard a user's supplied recording without a replacement decision.

## Keep color on the tool track

All-drawn uses image `mode:"draw"` or native `paint`/hatching for the whole color interval. The tool must finish the current image/region before drawing a separate label. If that does not fit the spoken beat, simplify the source, reduce unnecessary on-board text, or keep one continuous board so a drawn object can remain visible. Do not introduce a passive color fade while another object is being drawn. See [all-drawn.md](all-drawn.md).
