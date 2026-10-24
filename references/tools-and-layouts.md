# Tools and layout

Default drawing style: one pencil, target-specific eraser, warm dotted paper, dark ink, and a small set of green/blue/red/yellow accents. The pencil can draw in several colors. Tool changes happen between serial actions, not via additional simultaneous cursors.

Implemented tools are `pencil`, `eraser`, `pen`, and `marker`, each with a distinct sprite. Default vector stroke widths are pencil 2.8 px, pen 2.2 px, and marker 16 px; marker adds opacity 0.38, including overlap darkening. Explicit stroke `width` overrides the default. Paint uses `brushSize`; hatch uses its own width. Text and raster images retain their compiled glyph/artwork shape when the tool changes. These profiles do not simulate fluid or pressure physics.

## Useful board structures

- Comparison: aligned left/right diagrams with matched size, a central question, a conclusion below. Keep the drawing focus on one side at a time.
- Cause and effect: two or three related nodes, short arrows, a final outcome. Draw each node before its outgoing arrow.
- Timeline: two or three event labels with clear dates; prioritize the turning point rather than many tiny events.
- Classroom correction: split text into independently addressable tokens. Draw an error, erase that token's target layer, then write a replacement ID. Keep neighboring punctuation outside the erase region.
- Spatial teaching: one large diagram with short labels placed near the relevant object. Preserve occlusion through compositing order or a compiled transparent illustration.

At 1280×720, start with margins of roughly 60–80 pixels, a title area above the diagram, and an optional caption band near the bottom. Body labels around 30–40 pixels and key concept text around 44–58 pixels often work. Measure actual text widths with the renderer; handwritten fonts are not monospaced. Treat these values as suggestions and scale them with the canvas.

The renderer accepts other even-sized canvases. Re-layout the content for portrait output; changing canvas size cannot rearrange a horizontal composition. Caption x/y/maxWidth can be explicit. The camera stays fixed unless the authored scene transition requests a paper wipe.

Use `drawGroup` to finish one diagram before another; retain element array order for final occlusion. `afterId` specifies dependency and `locked:true` preserves narration synchronization. Passive fills can follow an outline without introducing a second tool. `instant:true` means the object was already drawn; it should not be used to disguise concurrent drawing.

Allow a final completed board hold long enough to read the takeaway. If compression makes strokes frantic, simplify geometry or narration, or extend the scene. The scheduler enforces serialization, but visual comfort requires agent judgment.
