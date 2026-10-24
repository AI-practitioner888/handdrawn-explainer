# Artwork compilation

Use native vector paths for boxes, arrows, charts, and diagram geometry whose spatial relationships matter. For faces and complex objects, choose a supplied illustration, a reusable asset with known origin, or a newly generated image. The compiled image pipeline is provider-independent.

## Suitable source image

Prefer a transparent PNG with one recognizable subject, a few clean dark outlines, pale separated colors, and clear empty space. Photographic textures and dense hair/suit shading generate hundreds of tiny paths, slowing the reveal and obscuring the face. Request a simplified editorial/teaching illustration rather than tracing every photographic edge.

Example prompt to adapt for the actual person:

> Recognizable waist-up editorial illustration of [person], based on their characteristic face shape, hairstyle, and clothing. Sparse dark handdrawn outlines, a few broad pale color regions, restrained details, transparent background, isolated subject, no text or watermark. Keep the eyes and facial features clear; avoid solid black masses and photo texture.

Use the environment's supported image generation/editing workflow. Do not assume API credentials, a particular model, or automatic approval of paid operations. Preserve the original image and source/generation prompt in the user's work project.

## All-drawn compilation and playback

For the default blank-board style, compile tool-swept color paths:

```text
python <skill>/scripts/compile_draw_image.py --input <work>/original.png --output-prefix <work>/assets/person --relative-to <work> --width 360 --brush-size 22
```

Use its renderer fields with `mode:"draw"`. The compiler resizes the original to its intended display size and builds paths covering the complete nonzero alpha area. The runtime reveals original pixels only where the progressive brush mask has passed, keeping the tool active for the whole interval. There is no prepared image, passive fade, or final swap. Inspect the coverage report and nib motion; give a dense image enough time. The path order approximates hatching/reveal rather than the original artist's order. Read [all-drawn.md](all-drawn.md).

## Legacy contour-plus-fade compatibility

Use this only for an explicitly requested mixed/prepared style, not an all-drawn delivery.

```text
python <skill>/scripts/compile_assets.py image --input <work>/original.png --output-prefix <work>/assets/person --relative-to <work>
```

The compiler crops alpha bounds, rescales, extracts dark local-contrast ink, skeletonizes it, and traces connected paths. Outputs:

- `person.png`: cropped color artwork.
- `person-ink.png`: visible dark strokes with alpha.
- `person-trace.json`: image dimensions and ordered polyline strokes.
- `person-asset.json`: renderer fields and processing metadata.

Legacy `mode:"sketch"` uses `inkPhase` (default 0.82): the initial fraction traces outlines and the remainder fades original color. It is rejected by all-drawn. For progressive coloring of simple vector regions use `paint`.

Trace order is a geometric heuristic; it is not the artist's original stroke order. Alpha/ink thresholds can omit faint facial lines or include unwanted dark fill. Inspect the ink PNG and resulting animation. Prefer simplifying the source and recompiling over assigning a tiny duration to hundreds of strokes. Separate face, hair, and clothing assets when semantic drawing order matters.

Do not imply that this compiler generates likeness. Hand-authored portrait SVGs are possible but require purposeful design and visual validation. The two bundled news portraits demonstrate the raster pipeline, not an exhaustive person library.
