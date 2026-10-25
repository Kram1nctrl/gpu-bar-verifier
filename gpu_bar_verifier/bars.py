"""Measure utilization bars by their pixels.

The verifier deliberately reads the *rendered* pixels rather than the data
binding: a bar that is bound to the right number but never painted (clipped,
zero-width, wrong colour, hidden container) still fails the user, and only a
pixel measurement catches that.

Defaults match the Performance Monitor theme:
    track  #1E272C  (Avalonia ProgressBar Background)
    fill   #00A8FF  (ProgressBar Foreground)
Both are overridable, so the same tool works on other applications.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

DEFAULT_TRACK = (30, 39, 44)
DEFAULT_FILL = (0, 168, 255)


@dataclass(frozen=True)
class BarStyle:
    track: tuple[int, int, int] = DEFAULT_TRACK
    fill: tuple[int, int, int] = DEFAULT_FILL
    track_tolerance: int = 6
    fill_tolerance: int = 70
    min_width: int = 60          # px; ignore short colour runs (text, borders)
    min_height: int = 6          # px; a bar row band must be at least this tall
    max_height: int = 60         # px; taller bands are panels, not bars


@dataclass
class Bar:
    """One detected utilization bar."""

    x0: int
    x1: int
    y: int
    fill_ratio: float            # 0.0 - 1.0, blue pixels / track pixels
    fill_px: int
    track_px: int

    @property
    def percent(self) -> float:
        return round(self.fill_ratio * 100.0, 1)

    def __str__(self) -> str:
        return (f"bar at x={self.x0}..{self.x1} y={self.y} "
                f"fill={self.percent}% ({self.fill_px}/{self.track_px} px)")


def _close(pixel: tuple[int, int, int], target: tuple[int, int, int],
           tolerance: int) -> bool:
    return (abs(pixel[0] - target[0]) <= tolerance
            and abs(pixel[1] - target[1]) <= tolerance
            and abs(pixel[2] - target[2]) <= tolerance)


def _is_fill(pixel: tuple[int, int, int], style: BarStyle) -> bool:
    """True for the painted part of the bar.

    A plain colour-distance test is enough here: the fill is a flat accent
    colour, and antialiased edges land far outside the tolerance.
    """
    return _close(pixel, style.fill, style.fill_tolerance)


def _is_track(pixel: tuple[int, int, int], style: BarStyle) -> bool:
    """The progress-bar trough.

    A plain colour-distance test is not enough: the track (#1E272C) sits very
    close to the card background (#252526) used all over the app, and matching
    that turns every row into a "bar". The track is slightly blue-shifted, so
    require b > r and b - r >= 6 as well.
    """
    r, g, b = pixel
    if not _close(pixel, style.track, style.track_tolerance):
        return False
    # Only demand the blue shift when the configured track actually has one,
    # so custom (neutral grey) themes still work.
    if style.track[2] - style.track[0] >= 6:
        return b - r >= 6 and b >= g
    return True


def _runs(columns: list[int], gap: int = 1) -> list[tuple[int, int]]:
    """Group sorted x coordinates into contiguous [start, end] runs."""
    out: list[tuple[int, int]] = []
    if not columns:
        return out
    start = prev = columns[0]
    for x in columns[1:]:
        if x - prev <= gap:
            prev = x
            continue
        out.append((start, prev))
        start = prev = x
    out.append((start, prev))
    return out


def measure_bars(image: Image.Image, style: BarStyle | None = None) -> list[Bar]:
    """Detect utilization bars in a screenshot, left to right.

    A bar row is any row holding a long run of track-or-fill pixels, so a bar
    that is completely full (no trough left) is still detected. Consecutive
    such rows form one band; the band's middle row is measured, which avoids
    the antialiased top/bottom edges of the rounded track.
    """
    style = style or BarStyle()
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()

    # rows that contain at least one bar-length run, with that run's columns
    candidates: dict[int, list[tuple[int, int]]] = {}
    for y in range(height):
        occupied: list[int] = []
        for x in range(width):
            px = pixels[x, y]
            if _is_track(px, style) or _is_fill(px, style):
                occupied.append(x)
        long_runs = [r for r in _runs(occupied) if r[1] - r[0] + 1 >= style.min_width]
        if long_runs:
            candidates[y] = long_runs

    if not candidates:
        return []

    rows = sorted(candidates)
    bands: list[tuple[int, int]] = []
    start = prev = rows[0]
    for y in rows[1:]:
        if y - prev <= 1:
            prev = y
            continue
        bands.append((start, prev))
        start = prev = y
    bands.append((start, prev))

    bars: list[Bar] = []
    for y0, y1 in bands:
        if not style.min_height <= y1 - y0 + 1 <= style.max_height:
            continue
        row = (y0 + y1) // 2
        for x0, x1 in candidates[row]:
            blue = sum(1 for x in range(x0, x1 + 1)
                       if _is_fill(pixels[x, row], style))
            total = x1 - x0 + 1
            bars.append(Bar(x0=x0, x1=x1, y=row,
                            fill_ratio=blue / total if total else 0.0,
                            fill_px=blue, track_px=total))

    bars.sort(key=lambda b: b.x0)
    return bars


@dataclass
class BarReport:
    bars: list[Bar] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.bars) == 2

    def integrated(self) -> Bar | None:
        return self.bars[0] if len(self.bars) >= 1 else None

    def dedicated(self) -> Bar | None:
        return self.bars[1] if len(self.bars) >= 2 else None
