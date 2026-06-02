"""Generate assets/icon.ico for the TTS Dialogue App.

Draws a rounded-square gradient background with two overlapping speech bubbles
(multi-voice dialogue) and a small sound-wave, then exports a multi-size .ico.
Run from the project root:  python tools/make_icon.py
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

SIZE = 256
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "assets", "icon.ico")


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def make() -> str:
    # vertical gradient background (purple -> blue)
    top = (106, 90, 224)    # #6A5AE0
    bottom = (79, 195, 247)  # #4FC3F7
    bg = Image.new("RGB", (SIZE, SIZE))
    px = bg.load()
    for y in range(SIZE):
        color = _lerp(top, bottom, y / (SIZE - 1))
        for x in range(SIZE):
            px[x, y] = color

    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    img.paste(bg, (0, 0), _rounded_mask(SIZE, 52))
    d = ImageDraw.Draw(img)

    # back speech bubble (light, character B)
    d.rounded_rectangle([70, 56, 196, 150], radius=26, fill=(255, 255, 255, 110))
    d.polygon([(170, 146), (200, 176), (150, 150)], fill=(255, 255, 255, 110))

    # front speech bubble (white, character A) with sound-wave bars
    d.rounded_rectangle([54, 104, 184, 198], radius=26, fill=(255, 255, 255, 245))
    d.polygon([(78, 194), (52, 224), (104, 198)], fill=(255, 255, 255, 245))

    # sound-wave bars inside the front bubble
    bar_color = (106, 90, 224, 255)
    heights = [22, 40, 56, 40, 22]
    cx0 = 74
    cy = 151
    for i, h in enumerate(heights):
        x = cx0 + i * 18
        d.rounded_rectangle([x, cy - h // 2, x + 9, cy + h // 2], radius=4, fill=bar_color)

    # export multi-size .ico
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(OUT, format="ICO", sizes=sizes)
    return OUT


if __name__ == "__main__":
    print("Wrote", make())
