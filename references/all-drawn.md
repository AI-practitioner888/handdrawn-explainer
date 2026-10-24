# Entirely drawn blank boards

The default request means that the complete visible explanation grows through the tool's actions. A new board has no title, footer, illustration, labels, background diagram, or colored material already present. A continuous board can retain objects drawn earlier during that same board. Merely drawing an arrow on a prepared picture does not satisfy this style.

Author `drawPolicy:{"profile":"all-drawn"}`, `theme.paper:"#ffffff"`, `theme.dotSpacing:0`, empty global `elements`, and empty runtime `cues`. `build.py` supplies the all-drawn policy when absent, so omitting the flag cannot silently permit prepared pictures. Put all visible text in ordinary text elements with positive duration. Every object gets a full action on the single tool track. Avoid `instant`, zero duration, `fill`, passive image fades, and scene transitions that recall old boards. If a new scene is needed, leave its first encoded frame empty before starting its first stroke.

## Artwork and color

Native `stroke`, `paint`, `hatch`, and `crosshatch` draw geometry directly. Use paint for colored faces and regions, with layer order preserving foreground outlines. Erasing already drawn content can accompany a correction; replacement text still needs its own drawing interval.

For an existing complex transparent illustration:

```text
python <skill>/scripts/compile_draw_image.py --input <work>/original.png --output-prefix <work>/assets/subject --relative-to <work> --width 360 --brush-size 22
```

Use the output renderer fields and `mode:"draw"`. The native-sized paths cover the source alpha using a round, tool-driven mask. No final image replacement or fade is performed; a missing path would remain unpainted. The compiler reports coverage at several progress values and checks full coverage at completion. The original remains a source asset, not a prepared visible layer. This is a deliberate illustration-reveal technique, not reconstruction of an artist's original order or pigment physics. Inspect the nib speed and progression; simplify the source or give it more time if movement is uncomfortable.

## Export checks

`build.py --render` and direct `render.py --video` enforce the selected profile. Structure checks require one full serial tool interval per element. Lossless browser samples use a tools-hidden board: each scene's first encoded frame must match pure white; an element's start must not introduce an entire object; samples in both halves must show changed pixels. Painting over an existing drawn color is allowed.

Run separately with:

```text
python <skill>/scripts/check_all_drawn.py --project <work>/build
```

`all-drawn-check.json` distinguishes structural checks from rendered samples and states their scope. Tool count and frame count are separately checked throughout export. Look at the first frame and actual playback as well. Failed checks require a source/runtime correction, not switching off the profile. An explicitly requested collage or prepared slide can select `drawPolicy:{"profile":"legacy"}`, described accurately to the user. Older bundled mechanics fixtures mark this profile for regression replay, not as a new explainer default.
