"""Unit tests for bar detection. No GPU, no X server, no app required.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gpu_bar_verifier.bars import (  # noqa: E402
    DEFAULT_FILL,
    DEFAULT_TRACK,
    BarStyle,
    measure_bars,
)

CARD = (37, 37, 38)        # #252526 card background
CONTAINER = (12, 16, 19)   # #0C1013 bar container
BORDER = (43, 48, 54)      # #2B3036 container border

BAR_W = 208
BAR_H = 16


def make_image(fills: list[float], bars: bool = True,
               width: int = 800, height: int = 500) -> Image.Image:
    """Draw a Performance Monitor-like GPU page with N bars."""
    image = Image.new("RGB", (width, height), CARD)
    draw = ImageDraw.Draw(image)
    for index, fill in enumerate(fills):
        left = 260 + index * 280
        top = 180
        # container with border, like the real layout
        draw.rectangle([left - 12, top - 40, left + BAR_W + 12, top + 45],
                       fill=CONTAINER, outline=BORDER)
        if not bars:
            continue
        draw.rectangle([left, top, left + BAR_W - 1, top + BAR_H - 1],
                       fill=DEFAULT_TRACK)
        filled = int(round(BAR_W * fill))
        if filled > 0:
            draw.rectangle([left, top, left + filled - 1, top + BAR_H - 1],
                           fill=DEFAULT_FILL)
    return image


class MeasureBarsTest(unittest.TestCase):
    def test_zero_percent_is_detected_but_empty(self):
        bars = measure_bars(make_image([0.0]))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].percent, 0.0)
        # the trough must still be reported: "no fill" != "no bar"
        self.assertGreater(bars[0].track_px, BAR_W - 5)

    def test_half_filled(self):
        bars = measure_bars(make_image([0.5]))
        self.assertEqual(len(bars), 1)
        self.assertAlmostEqual(bars[0].percent, 50.0, delta=2.0)

    def test_fully_filled_has_no_trough_left(self):
        bars = measure_bars(make_image([1.0]))
        self.assertEqual(len(bars), 1)
        self.assertAlmostEqual(bars[0].percent, 100.0, delta=1.0)

    def test_two_bars_are_ordered_left_to_right(self):
        bars = measure_bars(make_image([0.25, 0.75]))
        self.assertEqual(len(bars), 2)
        self.assertLess(bars[0].x0, bars[1].x0)
        self.assertAlmostEqual(bars[0].percent, 25.0, delta=2.0)
        self.assertAlmostEqual(bars[1].percent, 75.0, delta=2.0)

    def test_arbitrary_values(self):
        for expected in (5.0, 12.0, 33.0, 66.0, 91.0, 99.0):
            with self.subTest(expected=expected):
                bars = measure_bars(make_image([expected / 100.0]))
                self.assertEqual(len(bars), 1)
                self.assertAlmostEqual(bars[0].percent, expected, delta=1.5)

    def test_card_background_is_not_a_bar(self):
        """Regression: #252526 is close to the track colour and used to match."""
        image = Image.new("RGB", (800, 500), CARD)
        self.assertEqual(measure_bars(image), [])

    def test_no_bars_when_page_has_no_progressbar(self):
        self.assertEqual(measure_bars(make_image([0.5], bars=False)), [])

    def test_custom_colours(self):
        style = BarStyle(track=(20, 20, 20), fill=(255, 0, 0), fill_tolerance=40)
        image = Image.new("RGB", (400, 120), (60, 60, 60))
        draw = ImageDraw.Draw(image)
        draw.rectangle([40, 40, 40 + BAR_W - 1, 40 + BAR_H - 1], fill=(20, 20, 20))
        draw.rectangle([40, 40, 40 + 51, 40 + BAR_H - 1], fill=(255, 0, 0))
        bars = measure_bars(image, style)
        self.assertEqual(len(bars), 1)
        self.assertAlmostEqual(bars[0].percent, 25.0, delta=2.0)


if __name__ == "__main__":
    unittest.main()
