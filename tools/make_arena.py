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
FLOOR = "FLOOR4_8"
CEIL = "CEIL3_5"

# One sector, outlined clockwise (y up) so the right side of every line faces in.
OUTLINE = [
    (0, 0), (0, 1024), (1024, 1024), (1024, 576), (2048, 576), (2048, 1024),
    (3072, 1024), (3072, 576), (4096, 576), (4096, 1024), (5120, 1024),
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


def build():
    verts = b"".join(struct.pack("<hh", x, y) for x, y in OUTLINE)
    n = len(OUTLINE)
    # flags 1 = impassable; front sidedef i, no back side (-1 = 0xFFFF)
    lines = b"".join(struct.pack("<hhhhhhh", i, (i + 1) % n, 1, 0, 0, i, -1) for i in range(n))
    sides = b"".join(struct.pack("<hh8s8s8sh", 0, 0, name8("-"), name8("-"), name8(WALL), 0) for _ in range(n))
    sectors = struct.pack("<hh8s8shhh", 0, 192, name8(FLOOR), name8(CEIL), 192, 0, 0)
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
    write_wad(sys.argv[1] if len(sys.argv) > 1 else "arena-raw.wad", build())
