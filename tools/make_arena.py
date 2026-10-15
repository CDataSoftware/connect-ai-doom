#!/usr/bin/env python3
"""Generate the Revenue Raid arena as a Doom 2 MAP01 PWAD (no nodes; run zdbsp after).

psdoom-ng (with -nopslev) spawns data monsters into three open 1024x1024 boxes
whose bottom-left corners are (0,0), (2048,0) and (4096,0). This map is exactly
that: three arenas joined by corridors, plus a lobby to the south where the
player starts with weapons.
"""
import struct
import sys

WALL = "TEKWALL4"
# --brand: textures from wads/brand.wad (tools/make_branding.py)
BRAND_WALL = "CAIWALL"
SIGN = "CAISIGN"
# The middle 256 units of each arena's far (north) wall, facing the player.
SIGN_SPANS = {(384, 1024), (2432, 1024), (4480, 1024)}
ALCOVE_DEPTH = 16
ALCOVE_FLOOR = 24
FLOOR = "FLOOR4_8"
CEIL = "CEIL3_5"

# One sector, outlined clockwise (y up) so the right side of every line faces in.
OUTLINE = [
    (0, 0), (0, 1024), (384, 1024), (640, 1024), (1024, 1024), (1024, 576),
    (2048, 576), (2048, 1024), (2432, 1024), (2688, 1024), (3072, 1024),
    (3072, 576), (4096, 576), (4096, 1024), (4480, 1024), (4736, 1024), (5120, 1024),
    (5120, 0), (4096, 0), (4096, 448), (3072, 448), (3072, 0), (2624, 0),
    (2624, -256), (2816, -256), (2816, -768), (2304, -768), (2304, -256),
    (2496, -256), (2496, 0), (2048, 0), (2048, 448), (1024, 448), (1024, 0),
]

# (type, x, y, angle)
THINGS = [
    (1, 2560, -704, 90),      # player 1 start, facing the arenas
    (2001, 2400, -640, 0),    # shotgun
    (2002, 2720, -640, 0),    # chaingun
    (2003, 2560, -560, 0),    # rocket launcher
    (2049, 2400, -480, 0),    # box of shells
    (2049, 2720, -480, 0),
    (2048, 2400, -400, 0),    # box of bullets
    (2048, 2720, -400, 0),
    (2046, 2480, -320, 0),    # box of rockets
    (2046, 2640, -320, 0),
    (2018, 2560, -400, 0),    # green armor
    (2012, 2480, -704, 0),    # medikit
    (2012, 2640, -704, 0),
    # Lamps in each arena's corners so the arenas read as rooms.
    *[(2028, x, y, 0) for bx in (0, 2048, 4096) for x, y in
      ((bx + 32, 32), (bx + 992, 32), (bx + 32, 992), (bx + 992, 992))],
]


def name8(s):
    return s.encode().ljust(8, b"\0")


def build(brand=False):
    wall = BRAND_WALL if brand else WALL
    verts = list(OUTLINE)
    sides = []    # (upper, lower, middle, sector)
    lines = []    # (v1, v2, flags, front side, back side)
    sectors = [(0, 256, 192)]  # (floor, ceiling, light); sector 0 is the whole arena

    def side(sector, middle="-", upper="-", lower="-"):
        sides.append((upper, lower, middle, sector))
        return len(sides) - 1

    def vert(xy):
        verts.append(xy)
        return len(verts) - 1

    n = len(OUTLINE)
    for i in range(n):
        x, y = OUTLINE[i]
        if brand and (x, y) in SIGN_SPANS:
            # Vanilla tiles wall textures every 128px vertically, so the sign
            # sits in a 128-tall alcove (floor 16, ceiling 144) instead of on
            # the 256-tall wall. flags 1|4 = impassable, two-sided.
            # full brightness so the sign's yellow isn't dimmed toward brown
            sectors.append((ALCOVE_FLOOR, ALCOVE_FLOOR + 128, 255))
            alcove = len(sectors) - 1
            lines.append((i, (i + 1) % n, 5, side(0, upper=wall, lower=wall), side(alcove)))
            x2 = OUTLINE[(i + 1) % n][0]
            a, b = vert((x, y + ALCOVE_DEPTH)), vert((x2, y + ALCOVE_DEPTH))
            lines.append((i, a, 1, side(alcove, wall), -1))
            lines.append((a, b, 1, side(alcove, SIGN), -1))
            lines.append((b, (i + 1) % n, 1, side(alcove, wall), -1))
        else:
            # flags 1 = impassable; one-sided (back side -1)
            lines.append((i, (i + 1) % n, 1, side(0, wall), -1))

    verts = b"".join(struct.pack("<hh", x, y) for x, y in verts)
    lines = b"".join(struct.pack("<hhhhhhh", v1, v2, f, 0, 0, fr, bk) for v1, v2, f, fr, bk in lines)
    sides = b"".join(struct.pack("<hh8s8s8sh", 0, 0, name8(u), name8(lo), name8(m), s) for u, lo, m, s in sides)
    sectors = b"".join(struct.pack("<hh8s8shhh", f, c, name8(FLOOR), name8(CEIL), light, 0, 0) for f, c, light in sectors)
    # thing flags 7 = all skill levels
    things = b"".join(struct.pack("<hhhhh", x, y, a, t, 7) for t, x, y, a in THINGS)
    return [("MAP01", b""), ("THINGS", things), ("LINEDEFS", lines), ("SIDEDEFS", sides),
            ("VERTEXES", verts), ("SECTORS", sectors)]


def write_wad(path, lumps):
    data, directory, pos = b"", b"", 12
    for name, blob in lumps:
        directory += struct.pack("<ii", pos, len(blob)) + name8(name)
        data += blob
        pos += len(blob)
    with open(path, "wb") as f:
        f.write(b"PWAD" + struct.pack("<ii", len(lumps), pos) + data + directory)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--brand"]
    write_wad(args[0] if args else "arena-raw.wad", build(brand="--brand" in sys.argv))
