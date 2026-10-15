#!/usr/bin/env python3
"""Export the side panel's images.

  make_sprites.py <freedoom2.wad> <out dir> [brand dir]

The game's own sprites as PNGs for the legend, so it shows exactly what's on
screen: the pink demon (an urgent record), the shotgun soldier (a record), the
keycards and the marine. With the brand dir (assets/brand), also the Connect AI
and CData logos, into <out dir>/../brand; the panel falls back to text without them.
"""
import shutil
import sys
from pathlib import Path

from PIL import Image

from make_branding import patch_pixels, read_wad

SPRITES = {
    "demon": "SARGA1",     # the pink demon: an urgent record
    "soldier": "SPOSA1",   # the shotgun soldier: a record
    "marine": "PLAYA1",
    "bluekey": "BKEYA0",
    "yellowkey": "YKEYA0",
    "redkey": "RKEYA0",
}


LOGOS = ["connect-ai.svg", "cdata.svg"]  # the brand kit's connect-ai-agility-clarity and cdata-logotype-clarity


def main(iwad, out, kit=None):
    lumps = read_wad(iwad)
    pal = lumps["PLAYPAL"][:768]
    palette = [tuple(pal[3 * i:3 * i + 3]) for i in range(256)]
    Path(out).mkdir(parents=True, exist_ok=True)
    for name, lump in SPRITES.items():
        w, h, px = patch_pixels(lumps[lump])
        img = Image.new("RGBA", (w, h))
        for (x, y), v in px.items():
            img.putpixel((x, y), palette[v] + (255,))
        scale = 4 if max(w, h) < 24 else 2  # keys are tiny; keep the pixels crisp
        img.resize((w * scale, h * scale), Image.NEAREST).save(Path(out) / f"{name}.png")
    if kit:
        brand = Path(out).parent / "brand"
        brand.mkdir(exist_ok=True)
        for name in LOGOS:
            if (Path(kit) / name).exists():
                shutil.copy(Path(kit) / name, brand / name)


if __name__ == "__main__":
    main(*sys.argv[1:4])
