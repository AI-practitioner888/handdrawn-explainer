"""Compile font glyphs or supplied raster art into portable drawing assets.

This script does not generate or edit artwork. The image command keeps source
color/alpha pixels and compiles a separate ink mask and approximate centerlines.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from vendor import ink_trace

SKILL_ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def paths(mask: np.ndarray, *, min_points: int, spacing: float) -> list:
    skeleton = ink_trace._zhang_suen_skeleton(mask)
    strokes = ink_trace.trace_8connected(skeleton, min_points=min_points)
    strokes = ink_trace._order_skeleton_strokes(strokes)
    return [[[
        round(float(x), 2), round(float(y), 2)
    ] for x, y in ink_trace._resample_stroke_points(stroke, spacing)] for stroke in strokes]


def text_strings(doc: dict) -> list[str]:
    elements = doc.get("elements", []) + [
        element for scene in doc.get("scenes", []) for element in scene.get("elements", [])
    ]
    strings = [element["text"] for element in elements if isinstance(element.get("text"), str)]
    strings.extend(caption["text"] for caption in doc.get("captions", [])
                   if isinstance(caption.get("text"), str))
    return strings


_MEDIANS_CACHE: dict | None = None


def load_medians() -> dict:
    """Load the Make Me a Hanzi stroke-order medians (real calligraphy order).

    Keyed by the character; each value is a list of strokes, every stroke a list
    of [x, y] points in a 1024x1024 em box (y-down). Lazily cached per process.
    """
    global _MEDIANS_CACHE
    if _MEDIANS_CACHE is not None:
        return _MEDIANS_CACHE
    p = SKILL_ROOT / "assets/stroke-order/medians.json"
    _MEDIANS_CACHE = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    return _MEDIANS_CACHE


def medians_to_strokes(char: str, atlas_bbox, *, spacing: float = 2.5):
    """Map medians onto the atlas glyph bounding box so strokes align with the
    rendered WenKai bitmap and are emitted in real calligraphy order.

    Returns a list of resampled strokes in atlas-tile pixel coordinates, or None
    when the character is not covered (caller falls back to skeleton ordering).
    """
    medians = load_medians().get(char)
    if not medians or not atlas_bbox:
        return None
    ax0, ay0, ax1, ay1 = atlas_bbox
    aw, ah = max(1.0, ax1 - ax0), max(1.0, ay1 - ay0)
    pts = [p for stroke in medians for p in stroke]
    mnx = min(p[0] for p in pts); mny = min(p[1] for p in pts)
    mxx = max(p[0] for p in pts); myy = max(p[1] for p in pts)
    mw, mh = max(1.0, mxx - mnx), max(1.0, myy - mny)
    sx, sy = aw / mw, ah / mh
    out = []
    for stroke in medians:
        mapped = [[ax0 + (x - mnx) * sx, ay0 + (y - mny) * sy] for x, y in stroke]
        out.append(ink_trace._resample_stroke_points(mapped, spacing))
    return out


def _gap_mask(mask_arr: np.ndarray, strokes: list, *, dilate: int = 5) -> np.ndarray:
    """Ink pixels not covered by any stroke centerline (with tolerance).

    The Make Me a Hanzi medians are derived from the Arphic Kai face; WenKai is a
    redraw of it, so a few glyph shapes (notably up-swept horizontal serifs) are
    not represented by the median lines. This finds those uncovered ink pixels.
    """
    from PIL import ImageFilter
    line_img = Image.new("L", (mask_arr.shape[1], mask_arr.shape[0]))
    draw = ImageDraw.Draw(line_img)
    for stroke in strokes:
        if len(stroke) >= 2:
            draw.line([tuple(p) for p in stroke], fill=255, width=3, joint="curve")
    covered = np.asarray(line_img.filter(ImageFilter.MaxFilter(dilate * 2 + 1))) > 50
    return mask_arr & ~covered


def _gap_domains(gap: np.ndarray) -> list:
    """8-connected domains of gap pixels as lists of (y, x)."""
    visited = np.zeros_like(gap, dtype=bool)
    domains = []
    ys, xs = np.where(gap)
    h, w = gap.shape
    for y, x in zip(ys, xs):
        if visited[y, x]:
            continue
        stack = [(int(y), int(x))]
        visited[y, x] = True
        pts = []
        while stack:
            cy, cx = stack.pop()
            pts.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and gap[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
        domains.append(pts)
    return domains


def _patch_gap_strokes(mask_arr: np.ndarray, gap: np.ndarray, *, min_domain: int = 12) -> list:
    """Extract skeleton segments inside each sizable gap domain as patch strokes."""
    patches = []
    for pts in _gap_domains(gap):
        if len(pts) < min_domain:
            continue
        domain = np.zeros_like(mask_arr, dtype=bool)
        for y, x in pts:
            domain[y, x] = True
        from PIL import ImageFilter
        ctx = np.asarray(
            Image.fromarray((domain * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5))
        ) > 50
        ctx = ctx & mask_arr
        if not ctx.any():
            continue
        segments = ink_trace.trace_8connected(ink_trace._zhang_suen_skeleton(ctx), min_points=2)
        segments = ink_trace._order_skeleton_strokes(segments)
        for seg in segments:
            pts2 = [[float(x), float(y)] for x, y in ink_trace._resample_stroke_points(seg, 2.5)]
            if len(pts2) >= 2:
                patches.append(pts2)
    return patches


def _point_seg_distance(pt, stroke) -> float:
    best = float("inf")
    for i in range(len(stroke) - 1):
        (x1, y1), (x2, y2) = stroke[i], stroke[i + 1]
        dx, dy = x2 - x1, y2 - y1
        l2 = dx * dx + dy * dy
        if l2 == 0:
            continue
        t = max(0.0, min(1.0, ((pt[0] - x1) * dx + (pt[1] - y1) * dy) / l2))
        best = min(best, (pt[0] - (x1 + t * dx)) ** 2 + (pt[1] - (y1 + t * dy)) ** 2)
    return best ** 0.5


def _stroke_feature(stroke) -> dict:
    xs = [p[0] for p in stroke]
    ys = [p[1] for p in stroke]
    (x0, y0), (x1, y1) = stroke[0], stroke[-1]
    length = sum(
        math.hypot(stroke[i + 1][0] - stroke[i][0], stroke[i + 1][1] - stroke[i][1])
        for i in range(len(stroke) - 1)
    )
    return {
        "mid": ((x0 + x1) / 2.0, (y0 + y1) / 2.0),
        "len": length,
        "ang": math.atan2(y1 - y0, x1 - x0),
    }


def _reorder_skeleton_by_medians(skeleton_strokes: list, median_strokes: list, *, max_dist: float = 35.0):
    """Reorder complete skeleton strokes to follow the medians' calligraphy order.

    Used when medians geometry deviates too far from the WenKai face (gaps >= 25%):
    the skeleton keeps the glyph 100% complete while the medians supply the order.
    Returns None when matching is unreliable (caller keeps the plain skeleton).
    """
    if not skeleton_strokes or not median_strokes:
        return None
    used = set()
    order = []
    for med in median_strokes:
        fm = _stroke_feature(med)
        best_j, best_score = None, -1e18
        for j, sk in enumerate(skeleton_strokes):
            if j in used:
                continue
            fs = _stroke_feature(sk)
            d = math.hypot(fm["mid"][0] - fs["mid"][0], fm["mid"][1] - fs["mid"][1])
            da = abs((fm["ang"] - fs["ang"] + math.pi) % (2 * math.pi) - math.pi)
            score = -d - abs(fm["len"] - fs["len"]) * 0.3 - da * 20
            if score > best_score:
                best_score, best_j = score, j
        if best_j is None:
            continue
        fs = _stroke_feature(skeleton_strokes[best_j])
        d = math.hypot(fm["mid"][0] - fs["mid"][0], fm["mid"][1] - fs["mid"][1])
        if d >= max_dist:
            continue
        used.add(best_j)
        sk = skeleton_strokes[best_j]
        # adopt the medians' writing direction when clearly opposite
        if abs((fm["ang"] - fs["ang"] + math.pi) % (2 * math.pi) - math.pi) > math.pi / 2:
            sk = list(reversed(sk))
        order.append(sk)
    if not order:
        return None
    # append unmatched skeleton strokes right after their spatial neighbour
    remaining = [j for j in range(len(skeleton_strokes)) if j not in used]
    for j in remaining:
        sk = skeleton_strokes[j]
        fs = _stroke_feature(sk)
        best_i, best_d = len(order), float("inf")
        for i, placed in enumerate(order):
            fp = _stroke_feature(placed)
            d = math.hypot(fs["mid"][0] - fp["mid"][0], fs["mid"][1] - fp["mid"][1])
            if d < best_d:
                best_d, best_i = d, i
        order.insert(min(best_i + 1, len(order)), sk)
    return order


def build_glyphs(timeline: Path, out: Path, font_path: Path | None = None,
                 font_license: Path | None = None) -> dict:
    font_path = font_path or SKILL_ROOT / "assets/fonts/LXGWWenKai-Regular.ttf"
    if not font_path.is_file():
        raise FileNotFoundError(f"Font does not exist: {font_path}")
    chars = sorted(set("".join(text_strings(read_json(timeline)))) - {"\n", "\r"})
    font = ImageFont.truetype(str(font_path), 80)
    tile_w, tile_h, cols = 112, 120, 16
    out.mkdir(parents=True, exist_ok=True)
    font_dir = out / "fonts"
    font_dir.mkdir(exist_ok=True)
    copied_font = font_dir / font_path.name
    if copied_font.resolve() != font_path.resolve():
        shutil.copy2(font_path, copied_font)
    if font_license is None and font_path.resolve() == (SKILL_ROOT / "assets/fonts/LXGWWenKai-Regular.ttf").resolve():
        font_license = SKILL_ROOT / "LICENSES/LXGWWenKai-OFL.txt"
    copied_license = None
    if font_license:
        if not font_license.is_file():
            raise FileNotFoundError(f"Font license does not exist: {font_license}")
        copied_license = font_dir / (font_path.stem + "-LICENSE.txt")
        if copied_license.resolve() != font_license.resolve():
            shutil.copy2(font_license, copied_license)
    atlas = Image.new("RGBA", (cols * tile_w, max(1, (len(chars) + cols - 1) // cols) * tile_h))
    glyphs = {}
    for i, char in enumerate(chars):
        mask = Image.new("L", (tile_w, tile_h))
        ImageDraw.Draw(mask).text((10, 92), char, font=font, fill=255, anchor="ls")
        x, y = (i % cols) * tile_w, (i // cols) * tile_h
        rgba = Image.new("RGBA", (tile_w, tile_h), (255, 255, 255, 0))
        rgba.putalpha(mask)
        atlas.paste(rgba, (x, y))
        atlas_bbox = mask.getbbox()
        mask_arr = np.asarray(mask) > 50
        skeleton_strokes = paths(mask_arr, min_points=2, spacing=2.5)
        medians_strokes = medians_to_strokes(char, atlas_bbox)
        if medians_strokes is not None:
            gap = _gap_mask(mask_arr, medians_strokes)
            total_ink = max(1, int(mask_arr.sum()))
            gap_pct = int(gap.sum()) / total_ink * 100
            if gap_pct >= 25.0:
                # medians geometry deviates too far from WenKai: keep the complete
                # skeleton shape, adopt the medians' calligraphy order where matchable
                reordered = _reorder_skeleton_by_medians(skeleton_strokes, medians_strokes)
                if reordered is not None:
                    stroke_src = "skeleton+real-order"
                    char_strokes = reordered
                else:
                    stroke_src = "approx"  # fallback: spatial skeleton order
                    char_strokes = skeleton_strokes
            else:
                # real calligraphy order + patch strokes for uncovered ink
                # (e.g. WenKai's up-swept horizontal serifs)
                char_strokes = list(medians_strokes)
                for patch in _patch_gap_strokes(mask_arr, gap):
                    mid = patch[len(patch) // 2]
                    best_i, best_d = 0, float("inf")
                    for i, s in enumerate(char_strokes):
                        d = _point_seg_distance(mid, s)
                        if d < best_d:
                            best_d, best_i = d, i
                    char_strokes.insert(best_i + 1, patch)
                stroke_src = "real+patched"
        else:
            stroke_src = "approx"  # fallback: spatial skeleton order
            char_strokes = skeleton_strokes
        glyphs[char] = {
            "x": x, "y": y, "w": tile_w, "h": tile_h,
            "inkBounds": atlas_bbox,
            "advance": round(font.getlength(char), 3),
            "strokes": char_strokes,
            "strokeOrderSource": stroke_src,
        }
    atlas.save(out / "glyph-atlas.png")
    data = {
        "fontSize": 80, "baseline": 92, "leftPad": 10,
        "font": "fonts/" + font_path.name,
        "atlasTileWidth": tile_w, "atlasTileHeight": tile_h,
        "skeletonSource": "scripts/vendor/ink_trace.py (MIT)",
        "strokeOrder": "Real calligraphy stroke order via Make Me a Hanzi medians; gap-domain patch strokes cover WenKai serifs the medians lack; chars with large geometry deviation fall back to skeleton shape with medians-guided ordering; approx spatial fallback otherwise",
        "glyphs": glyphs,
    }
    save_json(out / "glyphs.json", data)
    result = {
        "kind": "glyphs", "glyphCount": len(chars), "fontSha256": sha256(font_path),
        "timelineSha256": sha256(timeline), "atlas": "glyph-atlas.png", "data": "glyphs.json",
        "font": data["font"], "atlasSha256": sha256(out / "glyph-atlas.png"),
        "dataSha256": sha256(out / "glyphs.json"),
    }
    if copied_license:
        result["fontLicense"] = "fonts/" + copied_license.name
    save_json(out / "glyph-manifest.json", result)
    return result


def build_image(source: Path, prefix: Path, *, relative_to: Path | None = None,
                max_size: int = 360, threshold: int = 110, contrast: float = 7.0,
                min_points: int = 4, spacing: float = 2.3,
                mode: str = "local-contrast", allow_empty_ink: bool = False) -> dict:
    import cv2

    if not source.is_file():
        raise FileNotFoundError(f"Image does not exist: {source}")
    if max_size < 32:
        raise ValueError("--max-size must be at least 32")
    if not (0 <= threshold <= 255) or contrast < 0 or min_points < 2 or spacing <= 0:
        raise ValueError("Invalid ink extraction parameters")
    src = ImageOps.exif_transpose(Image.open(source)).convert("RGBA")
    bbox = src.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError("Input image has no visible pixels")
    original_size = list(src.size)
    src = src.crop(bbox)
    src.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    rgba = np.asarray(src)
    gray = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2GRAY)
    gray = np.where(rgba[:, :, 3] > 50, gray, 255).astype(np.uint8)
    if mode == "local-contrast":
        blurred = cv2.GaussianBlur(gray, (15, 15), 0)
        local = blurred.astype(np.float32) - gray.astype(np.float32)
        ink = (rgba[:, :, 3] > 70) & (gray < threshold) & (local > contrast)
    else:
        # Use threshold mode for supplied pure line art without painted shadows.
        ink = (rgba[:, :, 3] > 70) & (gray < threshold)
    ink = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_CLOSE,
                           np.ones((2, 2), dtype=np.uint8)) > 0
    strokes = paths(ink, min_points=min_points, spacing=spacing)
    if not strokes and not allow_empty_ink:
        raise ValueError("No drawable ink paths found; use sparse dark line art, tune --threshold/--contrast, "
                         "or choose a prepared static image instead")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    color_path = Path(str(prefix) + ".png")
    ink_path = Path(str(prefix) + "-ink.png")
    trace_path = Path(str(prefix) + "-trace.json")
    source_copy = Path(str(prefix) + "-source" + source.suffix.lower())
    if source_copy.resolve() != source.resolve():
        shutil.copy2(source, source_copy)
    src.save(color_path)
    black = np.zeros((*ink.shape, 4), dtype=np.uint8)
    black[:, :, :3] = (40, 43, 40)
    black[:, :, 3] = ink.astype(np.uint8) * 255
    Image.fromarray(black).save(ink_path)
    save_json(trace_path, {
        "width": src.width, "height": src.height, "strokes": strokes,
        "inkPixels": int(ink.sum()), "sourceSha256": sha256(source),
        "traceSource": "scripts/vendor/ink_trace.py (MIT)",
        "strokeOrder": "Approximate spatial order, not an artist's original stroke sequence",
    })
    base = relative_to.resolve() if relative_to else prefix.parent.resolve()
    def rel(path: Path) -> str:
        try:
            return path.resolve().relative_to(base).as_posix()
        except ValueError:
            raise ValueError(f"Output asset must be inside --relative-to: {path}") from None
    result = {
        "kind": "image", "sourceFile": rel(source_copy), "sourceSha256": sha256(source),
        "originalSize": original_size, "alphaCrop": list(bbox),
        "width": src.width, "height": src.height, "pathCount": len(strokes),
        "inkPixels": int(ink.sum()),
        "parameters": {"maxSize": max_size, "threshold": threshold, "contrast": contrast,
                       "minPoints": min_points, "spacing": spacing, "mode": mode},
        "rendererFields": {"type": "image", "mode": "sketch", "src": rel(color_path),
                           "inkSrc": rel(ink_path), "traceSrc": rel(trace_path), "inkPhase": 0.82},
        "colorBehavior": "Contour drawing followed by color fade; not simulated physical coloring",
        "outputs": {rel(path): sha256(path) for path in (color_path, ink_path, trace_path)},
    }
    save_json(Path(str(prefix) + "-asset.json"), result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    glyphs = sub.add_parser("glyphs", help="Compile all timeline text and caption characters")
    glyphs.add_argument("--timeline", required=True, type=Path)
    glyphs.add_argument("--output-dir", required=True, type=Path)
    glyphs.add_argument("--font", type=Path)
    glyphs.add_argument("--font-license", type=Path, help="License to retain with a supplied custom font")
    image = sub.add_parser("image", help="Compile supplied transparent artwork; no image generation")
    image.add_argument("--input", required=True, type=Path)
    image.add_argument("--output-prefix", required=True, type=Path)
    image.add_argument("--relative-to", type=Path, help="Project root for rendererFields paths")
    image.add_argument("--max-size", type=int, default=360)
    image.add_argument("--threshold", type=int, default=110)
    image.add_argument("--contrast", type=float, default=7.0)
    image.add_argument("--min-points", type=int, default=4)
    image.add_argument("--spacing", type=float, default=2.3)
    image.add_argument("--mode", choices=("local-contrast", "threshold"), default="local-contrast")
    image.add_argument("--allow-empty-ink", action="store_true")
    args = parser.parse_args()
    if args.command == "glyphs":
        result = build_glyphs(args.timeline, args.output_dir, args.font, args.font_license)
    else:
        result = build_image(args.input, args.output_prefix, relative_to=args.relative_to,
                             max_size=args.max_size, threshold=args.threshold, contrast=args.contrast,
                             min_points=args.min_points, spacing=args.spacing, mode=args.mode,
                             allow_empty_ink=args.allow_empty_ink)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
