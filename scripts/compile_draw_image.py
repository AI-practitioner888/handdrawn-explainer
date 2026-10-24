"""Compile transparent color artwork into complete, tool-driven reveal paths.

Each path reveals the original RGBA pixels under a moving round brush.  Empty
alpha regions are skipped.  The paths cover every visible source pixel, so a
renderer must not replace the accumulated mask with the complete image at the
end.  This compiles supplied pixels; it does not create new artwork.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def grouped_intervals(xs: np.ndarray, maximum_gap: int) -> list[tuple[int, int]]:
    if len(xs) == 0:
        return []
    boundaries = np.flatnonzero(np.diff(xs) > maximum_gap) + 1
    groups = np.split(xs, boundaries)
    return [(int(group[0]), int(group[-1])) for group in groups]


def coverage_paths(alpha: np.ndarray, brush_size: float) -> tuple[list, dict]:
    """Sweep only alpha-bearing row bands with alternating short strokes.

    Band spacing is less than the brush radius.  Even a diagonal alpha boundary
    receives the solid central part of the round brush, rather than depending
    on a partially transparent brush edge at the final frame.
    """
    height, width = alpha.shape
    spacing = max(1, int(math.floor(brush_size * 0.5)))
    rows = list(range(0, height, spacing))
    if rows[-1] != height - 1:
        rows.append(height - 1)
    paths = []
    occupied_rows = 0
    for row_index, y in enumerate(rows):
        low = max(0, y - spacing // 2 - 1)
        high = min(height, y + (spacing + 1) // 2 + 2)
        xs = np.flatnonzero(np.any(alpha[low:high] > 0, axis=0))
        intervals = grouped_intervals(xs, max(2, int(brush_size)))
        if not intervals:
            continue
        occupied_rows += 1
        reverse = bool(row_index % 2)
        if reverse:
            intervals.reverse()
        for left, right in intervals:
            left = max(0.0, left - 0.5)
            right = min(float(width - 1), right + 0.5)
            if right == left:
                right = min(float(width - 1), left + 0.5)
                left = max(0.0, right - 0.5)
            start, stop = (right, left) if reverse else (left, right)
            count = max(2, math.ceil(abs(stop - start) / 8.0) + 1)
            paths.append([[round(float(x), 3), float(y)]
                          for x in np.linspace(start, stop, count)])
    return paths, {"rowSpacing": spacing, "sweepRows": occupied_rows,
                   "candidateRows": len(rows)}


def path_length(path: list) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))


def render_mask(size: tuple[int, int], paths: list, brush_size: float,
                progress: float) -> Image.Image:
    """CPU check of the same round strokes and length progress as the canvas."""
    mask = Image.new("L", size)
    if progress <= 0:
        return mask
    draw = ImageDraw.Draw(mask)
    budget = sum(path_length(path) for path in paths) * min(1.0, progress)
    radius = brush_size / 2.0
    for path in paths:
        if budget <= 0:
            break
        points = [tuple(path[0])]
        for start, end in zip(path, path[1:]):
            length = math.hypot(end[0] - start[0], end[1] - start[1])
            if length == 0:
                continue
            fraction = min(1.0, budget / length)
            points.append((start[0] + (end[0] - start[0]) * fraction,
                           start[1] + (end[1] - start[1]) * fraction))
            budget -= length * fraction
            if fraction < 1:
                break
        draw.line(points, fill=255, width=max(1, round(brush_size)), joint="curve")
        # Explicit caps mirror canvas lineCap=round, including very short paths.
        for x, y in (points[0], points[-1]):
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    return mask


def compile_draw_image(source: Path, prefix: Path, *, width: int = 360,
                       brush_size: float = 22, relative_to: Path | None = None) -> dict:
    if not source.is_file():
        raise FileNotFoundError(f"Image does not exist: {source}")
    if width < 32 or brush_size < 2 or brush_size > width:
        raise ValueError("Width must be >=32 and brush size between 2 and width")
    original = ImageOps.exif_transpose(Image.open(source)).convert("RGBA")
    original_size = list(original.size)
    if original.getchannel("A").getbbox() is None:
        raise ValueError("Input image has no visible pixels")
    height = max(1, round(original.height * width / original.width))
    image = original.resize((width, height), Image.Resampling.LANCZOS)
    alpha = np.asarray(image.getchannel("A"))
    paths, sweep = coverage_paths(alpha, brush_size)
    if not paths:
        raise ValueError("No drawable image pixels")
    visible = alpha > 0
    covered = np.asarray(render_mask(image.size, paths, brush_size, 1.0)) == 255
    missing = visible & ~covered
    repair_count = 0
    # A conservative repair catches tiny isolated alpha pixels and rounding at
    # row endpoints.  Repairs remain ordinary strokes in the same tool track.
    while np.any(missing):
        y, x = np.argwhere(missing)[0]
        right = min(width - 1.0, float(x) + 0.5)
        left = max(0.0, right - 0.5)
        paths.append([[left, float(y)], [right, float(y)]])
        repair_count += 1
        covered = np.asarray(render_mask(image.size, paths, brush_size, 1.0)) == 255
        missing = visible & ~covered
        if repair_count > int(visible.sum()):
            raise RuntimeError("Unable to compile a complete image mask")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    color_path = Path(str(prefix) + ".png")
    ink_path = Path(str(prefix) + "-ink.png")
    trace_path = Path(str(prefix) + "-trace.json")
    source_copy = Path(str(prefix) + "-source" + source.suffix.lower())
    if source.resolve() != source_copy.resolve():
        shutil.copy2(source, source_copy)
    image.save(color_path)
    black = Image.new("RGBA", image.size, (40, 43, 40, 0))
    black.putalpha(image.getchannel("A"))
    black.save(ink_path)
    total_length = sum(path_length(path) for path in paths)
    trace = {"width": width, "height": height, "paths": paths,
             "brushSize": brush_size, "mode": "draw", "progressBasis": "pathLength",
             "sourceSha256": sha256(source), "totalPathLength": round(total_length, 3),
             "strokeOrder": "Alpha-clipped serpentine color strokes",
             "colorBehavior": "Original pixels revealed only under the moving tool"}
    write_json(trace_path, trace)
    checks = []
    for progress in (0.0, 0.25, 0.5, 0.75, 1.0):
        mask = render_mask(image.size, paths, brush_size, progress)
        mask_array = np.asarray(mask)
        covered_pixels = int((visible & (mask_array == 255)).sum())
        checks.append({"progress": progress, "solidCoveredVisiblePixels": covered_pixels,
                       "solidCoverageRatio": covered_pixels / int(visible.sum())})
        preview = image.copy()
        preview.putalpha(Image.fromarray(np.minimum(alpha, mask_array)))
        preview.save(Path(str(prefix) + f"-progress-{round(progress * 100):03d}.png"))
    if checks[0]["solidCoveredVisiblePixels"] != 0 or checks[-1]["solidCoverageRatio"] < 0.999:
        raise RuntimeError("Image coverage validation failed")
    if any(a["solidCoveredVisiblePixels"] > b["solidCoveredVisiblePixels"]
           for a, b in zip(checks, checks[1:])):
        raise RuntimeError("Image coverage must grow monotonically")
    base = relative_to.resolve() if relative_to else prefix.parent.resolve()
    def rel(path: Path) -> str:
        try:
            return path.resolve().relative_to(base).as_posix()
        except ValueError:
            raise ValueError(f"Output must be inside --relative-to: {path}") from None
    result = {"kind": "draw-image", "sourceFile": rel(source_copy),
              "sourceSha256": sha256(source), "originalSize": original_size,
              "width": width, "height": height, "brushSize": brush_size,
              "visiblePixels": int(visible.sum()), "pathCount": len(paths),
              "totalPathLength": round(total_length, 3), "repairPathCount": repair_count,
              **sweep, "coverageChecks": checks,
              "rendererFields": {"type": "image", "mode": "draw",
                                 "src": rel(color_path), "inkSrc": rel(ink_path),
                                 "traceSrc": rel(trace_path), "inkPhase": 1},
              "colorBehavior": "Single tool reveals original pixels; no fade or final snap",
              "outputs": {rel(path): sha256(path)
                          for path in (color_path, ink_path, trace_path)}}
    write_json(Path(str(prefix) + "-asset.json"), result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--relative-to", type=Path)
    parser.add_argument("--width", type=int, default=360)
    parser.add_argument("--brush-size", "--brushSize", dest="brush_size", type=float, default=22)
    args = parser.parse_args()
    result = compile_draw_image(args.input, args.output_prefix, width=args.width,
                                brush_size=args.brush_size, relative_to=args.relative_to)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
