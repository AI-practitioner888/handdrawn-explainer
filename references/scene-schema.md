# Project schema and commands

Author UTF-8 JSON. Relative image paths are resolved from the authored project file's directory. Audio file paths in a narration plan are resolved from that plan's directory. The build copies needed media into its own editable project. Python >=3.10, core Python requirements, FFmpeg/ffprobe, and Chromium are needed. `doctor.py` checks dependencies without installing or reading credentials.

## Minimal project

```json
{
  "schemaVersion":"handdrawn-explainer/1",
  "title":"A new explanation",
  "canvas":{"width":1280,"height":720},
  "durationMs":20000,
  "fps":30,
  "defaultTool":"pencil",
  "drawPolicy":{"profile":"all-drawn"},
  "theme":{"paper":"#ffffff","ink":"#282b28","dotSpacing":0},
  "elements":[],
  "scenes":[{
    "id":"board","startMs":0,"endMs":20000,
    "elements":[
      {"id":"title","type":"text","text":"A new explanation","x":640,"y":40,"size":48,"align":"center","startMs":100,"durationMs":900,"drawGroup":"title"},
      {"id":"box","type":"stroke","d":"M 300 250 L 500 250 L 500 430 L 300 430 Z","startMs":2000,"durationMs":1200,"width":3,"color":"#282b28","drawGroup":"diagram"}
    ]
  }],
  "cues":[{"text":"Explain what this box represents.","startMs":2000,"endMs":5000}],
  "sources":[]
}
```

Canvas dimensions must be even integers for H.264. Duration must finish on a frame boundary for the chosen integer fps (1–60). Scenes are chronological, contiguous, and cover the duration. Geometry stays in canvas coordinates; re-layout for another ratio. All-drawn has empty global `elements`, no runtime `cues`, no wipe transition, and a pure white initial encoded frame for every new board. Each scene's visible text/objects have positive drawing duration. One continuous board can retain artwork actually drawn earlier during that board. See [all-drawn.md](all-drawn.md). Globals and wipes remain legacy options for an explicitly requested mixed style.

Keep stable scene/object IDs and a single authored project as the revision source. Optional scene `narrationText` records the matching excerpt for storyboard review. Optional global `narrationBeats:[{id,phrase,startMs,endMs,timingSource}]` records measured global phrase markers; `speechBeat` on an element links it to one marker. This metadata documents alignment and does not automatically replace/retime audio. Read [review-and-revisions.md](review-and-revisions.md) for reviewed patches, and [audio-timing.md](audio-timing.md) for actual-duration placement.

`narrationPolicy:{profile:"continuous",maxPauseMs:500,maxLeadMs:150,maxTailMs:300,thresholdDb:-45}` is the default for narrated builds. `intentional` and `silent` reflect an explicit user request. `check_audio.py` measures the narration waveform before export; do not fill empty speech intervals with drawing delays or background noise.

## Common element fields

`id` is unique within its scene. `startMs` and `durationMs` are **scene-local**; scene/cue/toolTrack times are global. `instant:true` and zero-duration visible objects are forbidden by all-drawn. Elements persist until their scene ends after being drawn; duration is not a visibility lifetime. Array order controls compositing and is preserved by scheduling.

- `drawGroup`: finish related actions together.
- `afterId`: a dependency ID or array of IDs (same scene or global header).
- `locked:true`: preserve exact authored drawing interval; conflicting intervals fail.
- `minDurationMs`: useful minimum when the scheduler compresses an unlocked action.
- `tool`: pencil, pen, marker, or eraser. `defaultTool` chooses the main drawing tool. Vector stroke width/opacity differ by profile; text retains compiled font geometry.
- `opacity`, `color`: compositing/style.
- `transform:{x,y,scale}`: geometry translation/uniform scale.
- `pencil:false`: does not remove an animated action from scheduling.

The scheduler writes `toolMode:"serial"`, `toolTrack`, and `serialSchedule`. Do not hand-author toolTrack. The diagnostic counts include compression and shifted starts; compare against narration and simplify when necessary.

## Element types

| Type | Required/specific fields |
|---|---|
| text | `text,x,y,size`; optional `align:"center"`; y is the top of the line em box |
| stroke | SVG `d`; optional width,wobble |
| fill | Legacy closed `d,color`; passive fade, excluded from all-drawn |
| hatch | Closed `d`; angle,spacing,width; or explicit lines array |
| crosshatch | Closed `d`; angle,crossAngle,spacing,width |
| paint | Closed `d`; angle,brushSize,spacing,color |
| image | x,y,width,src,inkSrc,traceSrc,mode:"draw"; paths mask source colors progressively; optional brushSize |
| erase | targets array; region:{x,y,width,height}; optional brushSize; new replacement must have a new ID |

`hatch`, `crosshatch`, `paint` optionally accept `bounds:{x,y,width,height}`. Image draw uses source pixels under a tool-swept mask for the complete action; compile it with `compile_draw_image.py`. Plain `fill`, image `fade`, and legacy `sketch` color emergence are passive and excluded from all-drawn.

Example correction, inside one scene:

```json
[
  {"id":"wrong","type":"text","text":"is","x":450,"y":300,"size":46,"startMs":1000,"durationMs":500,"locked":true},
  {"id":"wipe","type":"erase","targets":["wrong"],"region":{"x":438,"y":295,"width":90,"height":65},"startMs":4000,"durationMs":600,"locked":true,"afterId":"wrong"},
  {"id":"right","type":"text","text":"are","x":450,"y":300,"size":46,"startMs":4800,"durationMs":600,"locked":true,"afterId":"wipe","color":"#43946b"}
]
```

The region must cover the target's actual ink. The eraser removes only target layers in that region; nearby layers remain. Attach a same-scene target ID, not text content. No global erase is supported in this version.

## Narration plan

```json
{
  "durationMs":20000,
  "mode":"files",
  "voice":"zh-CN-XiaoxiaoNeural",
  "maxSpeed":1.08,
  "segments":[
    {"id":"intro","startMs":50,"endMs":8100,"text":"第一段完整讲解。","audioFile":"voice/intro.wav","trimSilence":false},
    {"id":"explain","startMs":8240,"endMs":19850,"text":"第二段完整讲解。","audioFile":"voice/explain.wav","trimSilence":false}
  ]
}
```

Modes: `auto`, `edge`, `files`, `silent`. A segment with `audioFile` imports that file instead of generating TTS. Optional `spokenText` controls spoken pronunciation while `text` is the caption; segment voice can override the plan voice. Imported clips can set `trimSilence:false`, `normalize:false`, and `gainDb`. Segments must not overlap or exceed duration. Speech that exceeds the available window is only sped up within `maxSpeed`; a remaining overrun fails without truncating speech. Increase the window/duration or shorten the script.

The example assumes measured recordings lasting 8050ms and 11610ms, with 140ms between them; the caption strings are illustrative. Replace both text and recordings with matching real content. For TTS omit audioFile and use mode edge/auto, audition first, then place clips using their actual duration. Allocated window length is not the spoken duration. An uninterrupted recording can use one segment for the whole explanation. Changing generated text requires a fresh audioFile binding or regeneration; the explicit file otherwise continues to win over new text.

```text
python <skill>/scripts/audio.py --plan <work>/narration.json --output-dir <work>/audio --timeline <work>/timeline.json
```

Outputs narration.wav, sfx.wav, mixed.wav, narration.srt, and audio_manifest.json. Drawing/eraser sound follows toolTrack; travel/passive image color phases are silent. When importing a complete recording, `build.py --narration <file>` creates a files plan with no acceleration. Explicit silence uses `--silent`; do not silently drop requested narration when a service fails.

To connect narration captions with the board, author the same cue intervals in project `cues`; SRT export alone does not add captions to the canvas. Leave cues empty when narration plus board labels are sufficient. Project `audio:{plan:"narration.json"}` or `audio:{file:"narration.wav"}` can supply the default build audio; CLI options override it.

## Commands and preview

```text
python <skill>/scripts/build.py --project <work>/project.json --output <work>/build --audio-plan <work>/narration.json --sheet
python <skill>/scripts/render.py --project <work>/build --sheet --video
python <skill>/scripts/validate.py --project <work>/build/timeline.json --scheduled
python <skill>/scripts/check_render.py --project <work>/build
python <skill>/scripts/check_audio.py --project <work>/build
python <skill>/scripts/review_project.py --project <work>/project.json --audio-plan <work>/narration.json --compiled <work>/build --output-dir <work>/review
python -m http.server --directory <work>/build 8765
```

Open `http://127.0.0.1:8765/index.html?audio=audio` for narrated preview. Without audio query, the timeline can play silently. The runtime exposes `window.ready`, `renderAt(seconds)` returning a JPEG data URL, `lastRenderStats`, and `layoutReport`. Exported projects contain the runtime and font; preview does not depend on the installed skill folder.

## Optional pixel acceptance checks

Project `qaChecks` may specify region-based assertions; these are for meaningful visual verification. The helper hides tool sprites while measuring ink, so a hovering eraser cannot be mistaken for remaining artwork. Select a post-erasure time before replacement ink begins. A check can also be supplied as a JSON array through `check_render.py --checks`.

```json
[
  {"kind":"erase","beforeMs":3500,"afterMs":4700,"region":{"x":438,"y":295,"width":90,"height":65}},
  {"kind":"preserve","beforeMs":3500,"afterMs":4700,"region":{"x":550,"y":295,"width":90,"height":65}},
  {"kind":"growth","timesMs":[5200,5500,5800],"region":{"x":300,"y":250,"width":200,"height":180},"minAddedPixels":10}
]
```

Erase checks require at least 20 original ink pixels and a remaining ratio <=0.08; optional `minBeforePixels`/`maxAfterRatio` adjust this. Preserve checks compare mean pixel difference (default <=1); growth checks require increasing ink coverage above the paper texture. The helper also verifies deterministic reverse seeking and text fit. A pass without authored pixel checks does not prove erasure or painting.
