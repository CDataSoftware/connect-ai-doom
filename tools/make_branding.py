#!/usr/bin/env python3
"""Build wads/brand.wad: light Connect AI branding from the CData brand kit.

  CAISIGN  256x128 wall texture: the stock TEKWALL4 tech wall with a
           Connect AI sign (logo on a Depth plate) baked in, for the 128-tall
           alcove in each arena's far wall (vanilla tiles walls every 128px).
           One patch, not a composite: vanilla caps composites at 64 KB.
  M_DOOM   the menu logo becomes the Connect AI logo.

Usage: make_branding.py <freedoom2.wad> <brand kit dir> <out.wad>
The brand kit dir is the extracted BRANDKIT folder (kept out of git).
"""
import struct
import sys
from pathlib import Path

from PIL import Image

DEPTH = (21, 21, 28)          # brand Depth #15151C
LOGO = "02 Logos/Product Logos/Connect AI/PNG/connect-ai-agility-clarity.png"  # for dark backgrounds


def read_wad(path):
    d = Path(path).read_bytes()
    n, off = struct.unpack("<ii", d[4:12])
    lumps = {}
    for i in range(n):
        pos, size = struct.unpack("<ii", d[off + 16 * i:off + 16 * i + 8])
        lumps[d[off + 16 * i + 8:off + 16 * i + 16].rstrip(b"\0").decode()] = d[pos:pos + size]
    return lumps


def name8(s):
    return s.encode().ljust(8, b"\0")


def to_patch(img, palette, left=0, top=0):
    """RGBA image -> Doom patch lump; alpha < 128 is transparent."""
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    cache = {}

    def index(rgb):
        if rgb not in cache:
            cache[rgb] = min(range(256), key=lambda i: sum((a - b) ** 2 for a, b in zip(palette[i], rgb)))
        return cache[rgb]

    columns = []
    for x in range(w):
        col, y = b"", 0
        while y < h:
            if px[x, y][3] < 128:
                y += 1
                continue
            start = y
            while y < h and px[x, y][3] >= 128 and y - start < 128:
                y += 1
            data = bytes(index(px[x, yy][:3]) for yy in range(start, y))
            col += bytes([start, len(data), 0]) + data + b"\0"
        columns.append(col + b"\xff")
    header = struct.pack("<hhhh", w, h, left, top)
    offsets, pos = [], 8 + 4 * w
    for c in columns:
        offsets.append(pos)
        pos += len(c)
    return header + struct.pack(f"<{w}i", *offsets) + b"".join(columns)


def patch_pixels(lump):
    """Doom patch -> (w, h, {(x, y): palette index})."""
    w, h = struct.unpack("<hh", lump[:4])
    offsets = struct.unpack(f"<{w}i", lump[8:8 + 4 * w])
    px = {}
    for x, o in enumerate(offsets):
        while lump[o] != 0xFF:
            top, length = lump[o], lump[o + 1]
            for i in range(length):
                px[(x, top + i)] = lump[o + 3 + i]
            o += length + 4
    return w, h, px


def logo_image(kit, width):
    logo = Image.open(Path(kit) / LOGO).convert("RGBA")
    return logo.resize((width, round(logo.height * width / logo.width)), Image.LANCZOS)


def sign_image(kit):
    """Connect AI logo on a Depth plate."""
    logo = logo_image(kit, 224)
    plate = Image.new("RGBA", (256, logo.height + 20), DEPTH + (255,))
    plate.alpha_composite(logo, (16, 10))
    return plate


def menu_image(kit):
    """Logo on a Depth backing so anti-aliased edges don't fringe over the game."""
    logo = logo_image(kit, 196)
    img = Image.new("RGBA", (logo.width + 8, logo.height + 8), DEPTH + (255,))
    img.alpha_composite(logo, (4, 4))
    return img


def build(iwad, kit, out):
    lumps = read_wad(iwad)
    pal = lumps["PLAYPAL"][:768]
    palette = [tuple(pal[3 * i:3 * i + 3]) for i in range(256)]

    # The sign: TEKWALL4's patch tiled to 256x128, the plate centered on it.
    tw, th, wall_px = patch_pixels(lumps["W94_1"])
    sign = Image.new("RGBA", (256, 128))
    for x in range(256):
        for y in range(128):
            sign.putpixel((x, y), palette[wall_px[(x % tw, y % th)]] + (255,))
    plate = sign_image(kit)
    sign.alpha_composite(plate, ((256 - plate.width) // 2, (128 - plate.height) // 2))

    # PNAMES: everything from the IWAD + our sign patch.
    pn = lumps["PNAMES"]
    count = struct.unpack("<i", pn[:4])[0]
    names = [pn[4 + 8 * i:12 + 8 * i].rstrip(b"\0").decode().upper() for i in range(count)]
    names.append("CAILOGO")
    pnames = struct.pack("<i", len(names)) + b"".join(name8(n) for n in names)

    # TEXTURE1: a PWAD's TEXTURE1 replaces the IWAD's, so copy every entry, then append ours.
    tb = lumps["TEXTURE1"]
    tcount = struct.unpack("<i", tb[:4])[0]
    offs = list(struct.unpack(f"<{tcount}i", tb[4:4 + 4 * tcount]))
    entries = [tb[o:offs[i + 1]] if i + 1 < tcount else tb[o:] for i, o in enumerate(offs)]
    caisign = (name8("CAISIGN") + struct.pack("<ihhih", 0, 256, 128, 0, 1)
               + struct.pack("<hhhhh", 0, 0, len(names) - 1, 0, 0))
    entries.append(caisign)
    head = 4 + 4 * len(entries)
    table, pos = [], head
    for e in entries:
        table.append(pos)
        pos += len(e)
    texture1 = struct.pack("<i", len(entries)) + struct.pack(f"<{len(entries)}i", *table) + b"".join(entries)

    menu = menu_image(kit)
    out_lumps = [
        ("PNAMES", pnames),
        ("TEXTURE1", texture1),
        ("CAILOGO", to_patch(sign, palette)),
        # menu draws M_DOOM at (94, 2) minus offsets; these center it.
        ("M_DOOM", to_patch(menu, palette, left=94 - (320 - menu.width) // 2, top=-4)),
    ]
    data, directory, pos = b"", b"", 12
    for name, blob in out_lumps:
        directory += struct.pack("<ii", pos, len(blob)) + name8(name)
        data += blob
        pos += len(blob)
    Path(out).write_bytes(b"PWAD" + struct.pack("<ii", len(out_lumps), pos) + data + directory)


if __name__ == "__main__":
    build(*sys.argv[1:4])
