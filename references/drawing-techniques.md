# Drawing techniques

All geometry uses canvas coordinates; all ink and color reserve the single tool. The default all-drawn board has no prepared background, words or pictures; all regions require paint/hatching and all images use mode draw. `d` is an SVG path. Closed paths are required for meaningful region masking. Array order controls compositing; place color layers before foreground outlines to keep finished edges crisp, independently of action timing.

| Effect | Use | Starting settings | Actual behavior |
|---|---|---|---|
| `stroke` | Outlines, arrows, underlines | width 2–3; restrained wobble | Progressive path trace; SVG M boundaries lift the pen |
| `hatch` | One shaded face, material, lightly selected area | angle 45; spacing 8–12; width 1–1.5 | Parallel paths clipped to the region, drawn sequentially |
| `crosshatch` | Darker shade or a second semantic category | angle 45; crossAngle 135; spacing 9–14 | Two serial sets of intersecting paths |
| `paint` | Fill a selected fraction, water/land, broad highlights | brushSize 16–24; spacing below brushSize | Sequential coverage strokes clipped to the region |
| `image`, mode draw | Portraits, complex illustration | compile native size, brushSize 18–24 | Tool-swept mask progressively reveals original colors; no fade or snap |
| legacy `fill` / image sketch | Explicit mixed style only | according to requested style | Passive fill or contour-plus-fade; excluded from all-drawn |
| `erase` | Correct a word or replace a concept | explicit targets; brushSize 24 | Removes target layers in the region, then supports a new element |

Settings are visual starting points, not mandatory style limits. `bounds:{x,y,width,height}` can accelerate generated region strokes; omit it to derive the path bounds. `hatch` also accepts authored `lines:["M ... L ...", ...]` for specific geometry.

See [the progressive effect sheet](examples/effects/effects-contact-sheet.png) and [its executable project](examples/effects/project.json) for hatching, crosshatching, paint, erasing, and pen/marker/pencil strokes. This is a mechanics fixture, not a finished lesson layout. Build it with explicit `--silent` to inspect its eight-second timeline.

Example genuine region coloring:

```json
{"id":"fraction-color","type":"paint","d":"M 300 250 L 420 250 L 420 410 L 300 410 Z","startMs":3000,"durationMs":900,"angle":0,"brushSize":20,"spacing":15,"color":"#43946b","opacity":0.4,"drawGroup":"fraction"}
```

Example crosshatched shade:

```json
{"id":"shade","type":"crosshatch","d":"M 700 300 L 830 300 L 830 410 L 700 410 Z","startMs":5000,"durationMs":1100,"angle":45,"crossAngle":135,"spacing":10,"width":1.1,"color":"#386d4d","opacity":0.5,"drawGroup":"object"}
```

Do not layer every technique on one picture. Use pale paint for the main selected region and light hatching for secondary texture. Crosshatching can remain legible without becoming a dark block. Preserve contrast between ink, texture, and caption.

Current image/text tracing uses approximate centerline reveals. The renderer does not implement authentic artist pressure, pigment physics, paper abrasion, arbitrary raster segmentation, or calligraphy order. Do not describe those as supported effects. Add new executable primitives and verify their visible behavior before documenting them as capabilities.
