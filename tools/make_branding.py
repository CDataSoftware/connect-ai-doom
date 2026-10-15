#!/usr/bin/env python3
"""Build wads/brand.wad: light Connect AI branding from the CData brand kit.

  CAISIGN  256x128 wall texture: the stock TEKWALL4 tech wall with a
           Connect AI sign (logo on a Depth plate) baked in, for the 128-tall
           alcove in each arena's far wall (vanilla tiles walls every 128px).
           One patch, not a composite: vanilla caps composites at 64 KB.
  CAIWALL  128x128: TEKWALL4 recolored into the brand grays (Depth up to
           Gray 5) with a thin Agility yellow trim on the bottom rows. On the
           256-tall arena walls it tiles twice: trim at the floor and mid-wall.
  M_DOOM   the menu logo becomes the Connect AI logo.

With --mission (state/mission.json from brief.py) it also draws the run's signs:
  CAIRM0-2 one per app room: the app, what Claude found there, and the access
           Connect AI grants (keycard color: blue = read, yellow = read + write)
  CAIVAULT the PII vault's sign, with Connect AI's refusal
  CAIXXXX  the vault's walls: masked values

Usage: make_branding.py <freedoom2.wad> <brand kit dir> <out.wad> [--mission mission.json]
The brand kit dir is the extracted BRANDKIT folder (kept out of git).
"""
import json
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DEPTH = (21, 21, 28)          # brand Depth #15151C
AGILITY = (255, 229, 0)       # brand Agility #FFE500
GRAY_LO, GRAY_HI = 24, 140    # brand Gray 11 (Depth) .. Gray 5, as plain gray levels
TRIM_ROWS = 4  # thinner breaks into dashes at a distance
CLARITY = (241, 238, 233)     # brand Clarity #F1EEE9
KEY_BLUE, KEY_YELLOW, KEY_RED = (64, 96, 255), AGILITY, (255, 48, 32)
FONT = "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf"
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


def font(size):
    try:
        return ImageFont.truetype(FONT, size)
    except OSError:
        return ImageFont.load_default(size)


def fit(draw, text, size, width):
    """The largest font up to `size` that fits `text` in `width` pixels."""
    while size > 8 and draw.textlength(text, font=font(size)) > width:
        size -= 1
    return font(size)


def text_sign(wall, lines, border):
    """A 256x128 sign on the branded wall: a Depth plate with a keycard-colored
    border and centered lines of (text, size, color)."""
    sign = Image.new("RGBA", (256, 128))
    for x in range(0, 256, wall.width):
        sign.paste(wall, (x, 0))
    d = ImageDraw.Draw(sign)
    d.rectangle([6, 6, 249, 121], fill=DEPTH + (255,), outline=border + (255,), width=4)
    fonts = [fit(d, t, size, 220) for t, size, _ in lines]
    heights = [f.getbbox("XG")[3] for f in fonts]
    y = (128 - sum(heights) - 6 * (len(lines) - 1)) // 2
    for (t, _, color), f, h in zip(lines, fonts, heights):
        d.text((128, y), t, font=f, fill=color + (255,), anchor="mt")
        y += h + 6
    return sign


def mission_textures(wall, mission):
    """(name, image) for each sign and the vault walls of this run."""
    out = []
    for i, room in enumerate(mission.get("rooms", [])[:3]):
        key, access = (KEY_YELLOW, "READ + WRITE") if room.get("writable") else (KEY_BLUE, "READ ONLY")
        out.append((f"CAIRM{i}", text_sign(wall, [
            (room.get("app", room["connection"]).upper(), 40, AGILITY),
            (room.get("title", "").upper(), 24, CLARITY),
            (access, 18, key)], key)))
    if mission.get("denied"):
        out.append(("CAIVAULT", text_sign(wall, [
            ("PII VAULT", 40, KEY_RED),
            ("BLOCKED BY CONNECT AI", 22, CLARITY),
            ("DATA SECURITY", 18, AGILITY)], KEY_RED)))
        xxxx = Image.new("RGBA", (128, 128), DEPTH + (255,))
        d = ImageDraw.Draw(xxxx)
        for row, y in enumerate(range(4, 128, 16)):
            d.text((4 + (row % 2) * 10, y), "XXX-XX-XXXX" if row % 2 == 0 else "XXXX XXXX XXXX",
                   font=font(15), fill=(120, 120, 128, 255))
        out.append(("CAIXXXX", xxxx))
    return out


def build(iwad, kit, out, mission=None):
    lumps = read_wad(iwad)
    pal = lumps["PLAYPAL"][:768]
    palette = [tuple(pal[3 * i:3 * i + 3]) for i in range(256)]

    # The wall: TEKWALL4's patch in brand grays, yellow trim at the bottom.
    tw, th, wall_px = patch_pixels(lumps["W94_1"])
    lum = {k: sum(c * w for c, w in zip(palette[v], (0.299, 0.587, 0.114))) for k, v in wall_px.items()}
    lo, hi = min(lum.values()), max(lum.values())
    wall = Image.new("RGBA", (tw, th))
    for (x, y), l in lum.items():
        g = round(GRAY_LO + (l - lo) / (hi - lo) * (GRAY_HI - GRAY_LO))
        wall.putpixel((x, y), (g, g, g, 255))
    for y in range(th - TRIM_ROWS, th):
        for x in range(tw):
            wall.putpixel((x, y), AGILITY + (255,))

    # The sign: the branded wall tiled to 256x128, the plate centered on it.
    sign = Image.new("RGBA", (256, 128))
    for x in range(0, 256, tw):
        sign.paste(wall, (x, 0))
    has_kit = (Path(kit) / LOGO).exists()  # without the brand kit: text instead of the logo
    if has_kit:
        plate = sign_image(kit)
        sign.alpha_composite(plate, ((256 - plate.width) // 2, (128 - plate.height) // 2))
    else:
        sign = text_sign(wall, [("CONNECT AI", 48, AGILITY)], AGILITY)

    # PNAMES: everything from the IWAD + our sign patch.
    pn = lumps["PNAMES"]
    count = struct.unpack("<i", pn[:4])[0]
    names = [pn[4 + 8 * i:12 + 8 * i].rstrip(b"\0").decode().upper() for i in range(count)]
    extra = mission_textures(wall, mission) if mission else []
    names += ["CAILOGO", "CAIWALLP"] + [f"P{n[3:]}"[:8] for n, _ in extra]
    pnames = struct.pack("<i", len(names)) + b"".join(name8(n) for n in names)

    # TEXTURE1: a PWAD's TEXTURE1 replaces the IWAD's, so copy every entry, then append ours.
    tb = lumps["TEXTURE1"]
    tcount = struct.unpack("<i", tb[:4])[0]
    offs = list(struct.unpack(f"<{tcount}i", tb[4:4 + 4 * tcount]))
    entries = [tb[o:offs[i + 1]] if i + 1 < tcount else tb[o:] for i, o in enumerate(offs)]
    textures = [("CAISIGN", sign.size, "CAILOGO"), ("CAIWALL", wall.size, "CAIWALLP")]
    textures += [(n, img.size, f"P{n[3:]}"[:8]) for n, img in extra]
    for tex, (w, h), patch in textures:
        entries.append(name8(tex) + struct.pack("<ihhih", 0, w, h, 0, 1)
                       + struct.pack("<hhhhh", 0, 0, names.index(patch), 0, 0))
    head = 4 + 4 * len(entries)
    table, pos = [], head
    for e in entries:
        table.append(pos)
        pos += len(e)
    texture1 = struct.pack("<i", len(entries)) + struct.pack(f"<{len(entries)}i", *table) + b"".join(entries)

    out_lumps = [
        ("PNAMES", pnames),
        ("TEXTURE1", texture1),
        ("CAILOGO", to_patch(sign, palette)),
        ("CAIWALLP", to_patch(wall, palette)),
    ]
    if has_kit:
        menu = menu_image(kit)
        # menu draws M_DOOM at (94, 2) minus offsets; these center it.
        out_lumps.append(("M_DOOM", to_patch(menu, palette, left=94 - (320 - menu.width) // 2, top=-4)))
    out_lumps += [(f"P{n[3:]}"[:8], to_patch(img, palette)) for n, img in extra]
    data, directory, pos = b"", b"", 12
    for name, blob in out_lumps:
        directory += struct.pack("<ii", pos, len(blob)) + name8(name)
        data += blob
        pos += len(blob)
    Path(out).write_bytes(b"PWAD" + struct.pack("<ii", len(out_lumps), pos) + data + directory)


if __name__ == "__main__":
    args = sys.argv[1:]
    mission = None
    if "--mission" in args:
        i = args.index("--mission")
        mission = json.loads(Path(args[i + 1]).read_text())
        del args[i:i + 2]
    build(*args[:3], mission=mission)
