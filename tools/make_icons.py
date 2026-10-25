#!/usr/bin/env python3
"""Render the application icon at every size an icon theme needs.

The source of truth is assets/gpu-bar-verifier.svg, but icon themes want PNGs
and gdk-pixbuf on a minimal system may not have an SVG loader, so the icon is
redrawn here with Pillow instead of being rasterised from the SVG. That keeps
the build reproducible without librsvg/Inkscape.

Usage:
    python3 tools/make_icons.py            # writes assets/icons/*.png
    python3 tools/make_icons.py --out DIR
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
SUPERSAMPLE = 4

BG_TOP = (43, 43, 48, 255)        # #2b2b30
BG_BOTTOM = (22, 22, 25, 255)     # #161619
TRACK = (30, 39, 44, 255)         # #1e272c
FILL_START = (0, 168, 255, 255)   # #00a8ff
FILL_END = (79, 210, 255, 255)    # #4fd2ff


def _vertical_gradient(size: int, top, bottom) -> Image.Image:
    ramp = Image.linear_gradient("L").resize((size, size))
    return Image.composite(Image.new("RGBA", (size, size), bottom),
                           Image.new("RGBA", (size, size), top), ramp)


def _horizontal_gradient(size: tuple[int, int], left, right) -> Image.Image:
    width, height = size
    ramp = Image.linear_gradient("L").rotate(90, expand=True).resize((width, height))
    return Image.composite(Image.new("RGBA", size, right),
                           Image.new("RGBA", size, left), ramp)


def _rounded(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1],
                                           radius=radius, fill=255)
    return mask


def _bar(canvas: Image.Image, box, radius: int, fill: Image.Image) -> None:
    mask = _rounded((box[2] - box[0], box[3] - box[1]), radius)
    canvas.paste(fill.crop((0, 0, box[2] - box[0], box[3] - box[1])),
                 (box[0], box[1]), mask)


def render(size: int) -> Image.Image:
    """Draw the icon at `size` pixels, supersampled for clean edges."""
    s = size * SUPERSAMPLE
    icon = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # --- rounded, gradient background plate
    inset = round(s * 0.031)
    plate_size = s - 2 * inset
    plate = _vertical_gradient(plate_size, BG_TOP, BG_BOTTOM)
    icon.paste(plate, (inset, inset), _rounded((plate_size, plate_size),
                                               round(s * 0.20)))

    # --- utilization bars
    # Small sizes get two thicker bars so the icon still reads at 16 px.
    if size <= 32:
        rows = (0.34, 0.58)
        bar_h = 0.17
        fills = (0.62, 0.30)
    else:
        rows = (0.266, 0.453, 0.641)
        bar_h = 0.109
        fills = (0.62, 0.30, 0.81)

    left = round(s * 0.172)
    width = round(s * 0.656)
    height = max(2, round(s * bar_h))
    radius = height // 2
    track = Image.new("RGBA", (width, height), TRACK)
    fill_grad = _horizontal_gradient((width, height), FILL_START, FILL_END)

    for row, ratio in zip(rows, fills):
        top = round(s * row)
        _bar(icon, (left, top, left + width, top + height), radius, track)
        filled = max(radius * 2, round(width * ratio))
        _bar(icon, (left, top, left + filled, top + height), radius, fill_grad)

    return icon.resize((size, size), Image.LANCZOS)


def main() -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(here, "assets", "icons"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    for size in SIZES:
        path = os.path.join(args.out, f"gpu-bar-verifier-{size}.png")
        render(size).save(path, optimize=True)
        print(f"  {path}  ({os.path.getsize(path)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
