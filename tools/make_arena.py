#!/usr/bin/env python3
"""Generate the level as a Doom 2 MAP01 PWAD from a mission (no nodes; run zdbsp after).

  make_arena.py <out.wad> [--mission state/mission.json] [--brand]

psdoom-ng (with -nopslev) spawns data monsters into three 1024x1024 boxes whose
bottom-left corners are (0,0), (2048,0) and (4096,0). Each box is one app Claude
reached through Connect AI. Below them runs a hall with the lobby, and each
app's room opens off the hall through a keycard door:

  blue door    Connect AI gives this user read access to the app
  yellow door  read and write
  red door     the PII vault: a request Connect AI's Data Security blocked.
               There is no red key, so the door never opens.

The map is drawn on a 16-unit grid of sectors; walls are generated wherever two
cells differ, so rooms, doors and alcoves are just rectangles.
"""
import json
import struct
import sys

GRID = 16
FLOOR, CEIL = "FLOOR4_8", "CEIL3_5"
WALL = "TEKWALL4"
BRAND_WALL = "CAIWALL"     # tools/make_branding.py
DOOR = "BIGDOOR2"
JAMB = {"blue": "DOORBLU", "yellow": "DOORYEL", "red": "DOORRED"}
# Keyed doors that stay open once unlocked (D1 open-stay), so the room stays reachable
DOOR_SPECIAL = {"blue": 32, "yellow": 34, "red": 33, None: 31}
KEY_THING = {"blue": 5, "yellow": 6}
ARENA_X = (0, 2048, 4096)

# linedef flags
IMPASSABLE, BLOCK_MONSTERS, TWO_SIDED, UPPER_UNPEGGED, LOWER_UNPEGGED = 1, 2, 4, 8, 16


class Map:
    def __init__(self, wall):
        self.wall = wall
        self.sectors = []   # dicts: floor, ceil, light, kind, ...
        self.cells = {}     # (cx, cy) -> sector index; later rects overwrite earlier ones
        self.things = []

    def rect(self, x1, y1, x2, y2, floor, ceil, light, **kind):
        self.sectors.append({"floor": floor, "ceil": ceil, "light": light, **kind})
        s = len(self.sectors) - 1
        for cx in range(x1 // GRID, x2 // GRID):
            for cy in range(y1 // GRID, y2 // GRID):
                self.cells[(cx, cy)] = s
        return s

    def thing(self, type_, x, y, angle=0):
        self.things.append((type_, x, y, angle))

    # ------------------------------------------------------------ wall rules

    def side_textures(self, front, back, facing):
        """Textures for the side that belongs to `front`, looking at `back` (None = void).
        facing: compass direction from front into back, for picking an alcove's back wall."""
        f = self.sectors[front]
        if back is None:
            if f.get("sign") and facing == f["sign_dir"]:
                return "-", "-", f["sign"]
            if f.get("jamb"):
                return "-", "-", f["jamb"]
            return "-", "-", f.get("wall", self.wall)
        b = self.sectors[back]
        if b.get("door"):
            return DOOR, "-", "-"
        upper = self.wall if b["ceil"] < f["ceil"] else "-"
        lower = self.wall if b["floor"] > f["floor"] else "-"
        return upper, lower, "-"

    def line_kind(self, front, back):
        """(flags, special) for the line between two cells (back None = void)."""
        if back is None:
            jamb = self.sectors[front].get("jamb")
            return (IMPASSABLE | (LOWER_UNPEGGED if jamb else 0), 0)
        f, b = self.sectors[front], self.sectors[back]
        if b.get("door"):
            return (TWO_SIDED, DOOR_SPECIAL[b["key"]])
        if f.get("window") or b.get("window"):
            return (IMPASSABLE | TWO_SIDED, 0)
        return (TWO_SIDED, 0)

    # ------------------------------------------------------------ build

    def edges(self):
        """Yield (x1, y1, x2, y2, front, back) for every grid edge between different cells,
        oriented so the front sector is on the right (Doom's convention)."""
        xs = [c[0] for c in self.cells]
        ys = [c[1] for c in self.cells]
        for cy in range(min(ys), max(ys) + 2):           # horizontal edges at y = cy
            for cx in range(min(xs), max(xs) + 1):
                below, above = self.cells.get((cx, cy - 1)), self.cells.get((cx, cy))
                if below != above:
                    yield from self._orient(cx, cy, cx + 1, cy, right=below, left=above, horizontal=True)
        for cx in range(min(xs), max(xs) + 2):           # vertical edges at x = cx
            for cy in range(min(ys), max(ys) + 1):
                west, east = self.cells.get((cx - 1, cy)), self.cells.get((cx, cy))
                if west != east:
                    yield from self._orient(cx, cy, cx, cy + 1, right=east, left=west, horizontal=False)

    def _orient(self, x1, y1, x2, y2, right, left, horizontal):
        """Going (x1,y1)->(x2,y2) east or north, `right` is on the right side.
        Swap direction when the other sector should be the front."""
        def is_front(a, b):
            if a is None:
                return False
            if b is None:
                return True
            if self.sectors[a].get("door"):
                return False      # a door's lines face out, so they're used from the rooms
            if self.sectors[b].get("door"):
                return True
            return a < b
        # direction names for the front looking into the back
        if is_front(right, left):
            facing = "N" if horizontal else "W"
            yield (x1, y1, x2, y2, right, left, facing)
        else:
            facing = "S" if horizontal else "E"
            yield (x2, y2, x1, y1, left, right, facing)

    def compile(self):
        verts, vindex, lines, sides = [], {}, [], []

        def vert(x, y):
            if (x, y) not in vindex:
                vindex[(x, y)] = len(verts)
                verts.append((x * GRID, y * GRID))
            return vindex[(x, y)]

        # Merge consecutive unit edges with the same front/back/facing into one line.
        runs = {}
        for x1, y1, x2, y2, front, back, facing in self.edges():
            kind = self.line_kind(front, back)
            key = (front, back, facing, kind)
            runs.setdefault(key, []).append((x1, y1, x2, y2))
        segments = []
        for key, edges in runs.items():
            # chain unit edges that continue in the same direction
            by_start = {(e[0], e[1], e[2] - e[0], e[3] - e[1]): e for e in edges}
            for x1, y1, x2, y2 in edges:
                dx, dy = x2 - x1, y2 - y1
                if (x1 - dx, y1 - dy, dx, dy) in by_start:
                    continue  # not the first edge of its straight run
                while (x2, y2, dx, dy) in by_start:
                    x2, y2 = x2 + dx, y2 + dy
                segments.append((x1, y1, x2, y2, key))
        # Split lines at every vertex that lies on them (T-junctions upset the renderer).
        points = {(s[0], s[1]) for s in segments} | {(s[2], s[3]) for s in segments}
        for x1, y1, x2, y2, (front, back, facing, (flags, special)) in segments:
            cuts = sorted((p for p in points if p not in ((x1, y1), (x2, y2)) and _on(p, x1, y1, x2, y2)),
                          key=lambda p: abs(p[0] - x1) + abs(p[1] - y1))
            chain = [(x1, y1), *cuts, (x2, y2)]
            for (ax, ay), (bx, by) in zip(chain, chain[1:]):
                fs = len(sides)
                u, lo, m = self.side_textures(front, back, facing)
                sides.append((u, lo, m, front))
                bs = -1
                if back is not None:
                    bs = len(sides)
                    u, lo, m = self.side_textures(back, front, _opposite(facing))
                    sides.append((u, lo, m, back))
                lines.append((vert(ax, ay), vert(bx, by), flags, special, fs, bs))
        return verts, lines, sides

    def lumps(self):
        verts, lines, sides = self.compile()
        n8 = lambda s: s.encode().ljust(8, b"\0")
        return [
            ("MAP01", b""),
            ("THINGS", b"".join(struct.pack("<hhhhh", x, y, a, t, 7) for t, x, y, a in self.things)),
            ("LINEDEFS", b"".join(struct.pack("<hhhhhhh", v1, v2, fl, sp, 0, fr, bk)
                                  for v1, v2, fl, sp, fr, bk in lines)),
            ("SIDEDEFS", b"".join(struct.pack("<hh8s8s8sh", 0, 0, n8(u), n8(lo), n8(m), s)
                                  for u, lo, m, s in sides)),
            ("VERTEXES", b"".join(struct.pack("<hh", x, y) for x, y in verts)),
            ("SECTORS", b"".join(struct.pack("<hh8s8shhh", s["floor"], s["ceil"], n8(FLOOR), n8(CEIL),
                                             s["light"], 0, 0) for s in self.sectors)),
        ]


def _on(p, x1, y1, x2, y2):
    px, py = p
    if x1 == x2 == px:
        return min(y1, y2) < py < max(y1, y2)
    if y1 == y2 == py:
        return min(x1, x2) < px < max(x1, x2)
    return False


def _opposite(d):
    return {"N": "S", "S": "N", "E": "W", "W": "E"}[d]


# ---------------------------------------------------------------- the level

HALL_Y1, HALL_Y2 = -448, -64     # the hall south of the arenas
LOBBY = (2304, -960, 2816, -448)


def build(mission, brand=False):
    m = Map(BRAND_WALL if brand else WALL)
    rooms = mission.get("rooms", [])[:3]
    denied = mission.get("denied", [])

    m.rect(0, HALL_Y1, 5120, HALL_Y2, 0, 192, 176)          # hall
    m.rect(*LOBBY, 0, 192, 192)                                # lobby
    m.thing(1, 2560, -896, 90)                                 # player 1 start, facing the hall
    for t, x, y in ((2001, 2400, -800), (2002, 2720, -800), (2003, 2560, -720),
                    (2049, 2400, -640), (2049, 2720, -640), (2048, 2400, -560), (2048, 2720, -560),
                    (2046, 2480, -512), (2046, 2640, -512), (2018, 2560, -560),
                    (2012, 2480, -896), (2012, 2640, -896)):
        m.thing(t, x, y)

    keys = set()
    for i, room in enumerate(rooms):
        bx = ARENA_X[i]
        key = "yellow" if room.get("writable") else "blue"
        keys.add(key)
        m.rect(bx, 0, bx + 1024, 1024, 0, 256, 192)                         # the app's arena
        if brand:
            m.rect(bx + 384, 1024, bx + 640, 1040, 24, 152, 255, sign=f"CAIRM{i}", sign_dir="N")
            m.rect(bx + 128, -64, bx + 384, -48, 24, 152, 255, sign=f"CAIRM{i}", sign_dir="N")
        door(m, bx + 448, -64, 0, key)
        for x, y in ((bx + 32, 32), (bx + 992, 32), (bx + 32, 992), (bx + 992, 992)):
            m.thing(2028, x, y)                                               # lamps

    for i, key in enumerate(sorted(keys)):                                    # keycards in the lobby
        m.thing(KEY_THING[key], 2496 + 128 * i, -656)

    if denied:
        # The PII vault, west of the lobby: a red door with no red key anywhere,
        # and a window onto walls of masked values.
        m.rect(512, -1088, 1280, -576, 0, 128, 255, wall="CAIXXXX" if brand else WALL)
        door(m, 832, -576, HALL_Y1, "red")
        m.rect(1088, -576, 1216, -448, 40, 104, 255, window=True)
        if brand:
            m.rect(544, -464, 800, -448, 24, 152, 255, sign="CAIVAULT", sign_dir="S")
    return m


def door(m, x, y1, y2, key):
    """A 128-wide keycard door joining the spaces south of y1 and north of y2:
    a passage with a 32-deep door in the middle. The door's edges carry the key color."""
    m.rect(x, y1, x + 128, y2, 0, 128, 160, jamb=JAMB.get(key))
    mid = (y1 + y2) // 2
    m.rect(x, mid - 16, x + 128, mid + 16, 0, 0, 160, door=True, key=key, jamb=JAMB.get(key))


def write_wad(path, lumps):
    data, directory, pos = b"", b"", 12
    for name, blob in lumps:
        directory += struct.pack("<ii", pos, len(blob)) + name.encode().ljust(8, b"\0")
        data += blob
        pos += len(blob)
    with open(path, "wb") as f:
        f.write(b"PWAD" + struct.pack("<ii", len(lumps), pos) + data + directory)


if __name__ == "__main__":
    args = sys.argv[1:]
    mission = {"rooms": [{"writable": False}] * 3}   # without a mission: three plain rooms
    if "--mission" in args:
        i = args.index("--mission")
        mission = json.load(open(args[i + 1]))
        del args[i:i + 2]
    brand = "--brand" in args
    args = [a for a in args if a != "--brand"]
    write_wad(args[0] if args else "arena-raw.wad", build(mission, brand).lumps())
