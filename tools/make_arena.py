#!/usr/bin/env python3
"""Generate the level as a Doom 2 MAP01 PWAD from a mission (no nodes; run zdbsp after).

  make_arena.py <out.wad> [--mission state/mission.json] [--records state/cache.json] [--brand]

psdoom-ng (with -nopslev) spawns data monsters into three 1024x1024 boxes whose
bottom-left corners are (0,0), (2048,0) and (4096,0). Each box is one app Claude
reached through Connect AI. Below them runs a hall with the lobby, and each
app's room opens off the hall through a keycard door:

  blue door    Connect AI gives this user read access to the app
  yellow door  read and write
  red door     the PII vault: a request Connect AI's Data Security blocked.
               There is no red key, so the door never opens.

Each room is themed after its app (tools/themes.py): its walls, floors, lights and
scenery. Inside the spawn box the floor only steps by 16 or 24 units (a dais, a pit, a
moat), so monsters spawn anywhere and Claude's straight-line moves never snag. The
solid scenery (pillars, alcoves, lamps) stands in a 192-unit ring around the box, flush
with the walls.

The map is drawn on a 16-unit grid of sectors; walls are generated wherever two
cells differ, so rooms, doors and alcoves are just rectangles.
"""
import json
import struct
import sys

from themes import FAMILIES, room_themes

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
RING = 192          # scenery around each spawn box: west, east and north

# linedef flags; GLASS is psdoom's (the patch): seen through, but sight, shots and moves stop there
IMPASSABLE, BLOCK_MONSTERS, TWO_SIDED, UPPER_UNPEGGED, LOWER_UNPEGGED = 1, 2, 4, 8, 16
SOUND_BLOCK, GLASS = 64, 1024
SLOW_GLOW = 21      # psdoom's (the patch): a slow glow, the sector's tag its head start in tics
SCREEN = "DBRAIN1"  # tools/make_branding.py redraws DBRAIN1-4, which Doom animates, as a live dashboard


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

    def overlay(self, x1, y1, x2, y2, **changes):
        """Change some properties (flats, light) of whatever is already drawn in the rectangle,
        keeping its heights: one new sector per sector it covers."""
        made = {}
        for cx in range(x1 // GRID, x2 // GRID):
            for cy in range(y1 // GRID, y2 // GRID):
                s = self.cells.get((cx, cy))
                if s is None:
                    continue
                if s not in made:
                    self.sectors.append({**self.sectors[s], **changes})
                    made[s] = len(self.sectors) - 1
                self.cells[(cx, cy)] = made[s]

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
        # A step or a lowered ceiling shows the trim of whichever side is the feature (a dais,
        # a pit, a pillar); plain steps show the wall.
        step = b.get("trim") or f.get("trim") or self.wall
        upper = step if b["ceil"] < f["ceil"] else "-"
        lower = step if b["floor"] > f["floor"] else "-"
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
            return (IMPASSABLE | TWO_SIDED | GLASS | SOUND_BLOCK, 0)
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
            ("SECTORS", b"".join(struct.pack("<hh8s8shhh", s["floor"], s["ceil"], n8(s.get("ftex", FLOOR)),
                                             n8(s.get("ctex", CEIL)), s["light"], s.get("special", 0), s.get("tag", 0))
                                 for s in self.sectors)),
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


def build(mission, brand=False, records=None):
    """records: the monsters this level starts with (connect_doom.py's cache), for each room's loot."""
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
    for i, (room, theme) in enumerate(zip(rooms, room_themes(rooms))):
        bx = ARENA_X[i]
        key = "yellow" if room.get("writable") else "blue"
        keys.add(key)
        themed_room(m, bx, theme, brand)
        loot(m, bx, *room_records(room, i, records))
        if brand:
            m.rect(bx + 384, 1024 + RING, bx + 640, 1040 + RING, 24, 152, 255, sign=f"CAIRM{i}", sign_dir="N")
            m.rect(bx + 128, -64, bx + 384, -48, 24, 152, 255, sign=f"CAIRM{i}", sign_dir="N")
        door(m, bx + 448, -64, 0, key)
        # A window from the hall into the room: you see the records waiting, but the glass stops
        # sight, shots and sound both ways, so nothing wakes or fights through it.
        m.rect(bx + 800, -64, bx + 960, 0, 32, 120, theme["light"], window=True, wall="SUPPORT3", trim="SUPPORT3")
        # the hall in front of the door takes the room's floor and light
        m.overlay(bx + 384, -192, bx + 640, -64, ftex=theme["floor"], light=theme["light"] + 32)

    for i, key in enumerate(sorted(keys)):                                    # keycards in the lobby
        m.thing(KEY_THING[key], 2496 + 128 * i, -656)

    if denied:
        # The PII vault, west of the lobby: a red door with no red key anywhere,
        # and a window onto walls of masked values.
        m.rect(512, -1088, 1280, -576, 0, 128, 255, wall="CAIXXXX" if brand else WALL,
               ftex="LAVA1", ctex="FLOOR1_7", special=8)
        door(m, 832, -576, HALL_Y1, "red")
        m.rect(1088, -576, 1216, -448, 40, 104, 255, window=True)
        if brand:
            m.rect(544, -464, 800, -448, 24, 152, 255, sign="CAIVAULT", sign_dir="S")
    hall(m, bool(denied), brand)
    return m


def room_records(room, i, records):
    """(records, urgent records) room i starts with: the cache's, else the offline mission's rows."""
    if records is not None:
        mine = [r for r in records if r.get("pid", -1) % 3 == i]
        return len(mine), sum(1 for r in mine if r.get("demon"))
    rows = room.get("rows") or []
    return len(rows), sum(1 for r in rows if str(r.get("Hot", "0")).strip() in ("1", "True", "true"))


LOOT_ROWS = 17      # rows of loot down each side of the ring, 64 apart


def loot(m, bx, records, urgent):
    """What the room's records drop, in rows down both sides of the ring: shells for every
    record, and a rocket and a stimpack for every urgent one. More records, more loot."""
    west, east = bx - RING, bx + 1024 + RING
    for j in range(min(records, 2 * LOOT_ROWS)):
        x, inward = (west + 112, 40) if j % 2 == 0 else (east - 112, -40)
        y = 64 + 64 * (j // 2)
        m.thing(2008, x, y)                                     # 4 shells
        if j < urgent:
            m.thing(2010, x + inward, y)                        # a rocket
            m.thing(2011, x + inward, y + 32)                   # a stimpack


def hall(m, vault, brand=False):
    """Dress the hall and lobby. Claude walks the hall's middle (y = -256) and straight down from
    each door, so the floor stays level and everything solid stands flush with the walls."""
    m.overlay(0, -288, 5120, -224, ftex="STEP1")                          # a metal walkway down the middle
    for i, x in enumerate(range(96, 5120, 256)):                         # beams across, light strips between
        m.overlay(x, HALL_Y1, x + 32, HALL_Y2, ceil=160, ctex="FLAT20", trim="SUPPORT3")
        # The strips breathe slowly, each a little behind the last: a ripple runs down the hall.
        m.overlay(x + 112, -320, x + 144, -192, ctex="TLITE6_1", light=255, special=SLOW_GLOW, tag=8 * i)
    if vault:                                                            # hazard stripes at the red door, pulsing
        m.overlay(768, HALL_Y1, 1280, -384, ftex="AQF018", light=208, special=8)
    for x, y in ((2432, -832), (2688, -832), (2432, -576), (2688, -576)):   # the lobby's lamps
        m.overlay(x - 32, y - 32, x + 32, y + 32, ctex="TLITE6_1", light=240)
    if brand:                                                            # live dashboards in the south wall
        for x in (1312, 1600, 3360, 3776, 4480):
            m.rect(x, HALL_Y1 - 16, x + 128, HALL_Y1, 24, 152, 208, sign=SCREEN, sign_dir="S")
    pillar = {"ftex": "FLAT20", "ctex": "FLAT20", "trim": "SUPPORT3", "wall": "SUPPORT3"}
    north = [x for bx in ARENA_X for x in (bx + 704, bx + 1600) if x + 64 <= 5120]
    south = (256, 1472, 1920, 3200, 3648, 4352, 4800)    # clear of the vault door, window and lobby
    for x in north:
        m.rect(x, HALL_Y2 - 32, x + 64, HALL_Y2, 192, 192, 160, **pillar)
        m.thing(85, x + 96, HALL_Y2 - 20)                                # tech lamps beside them
    for x in south:
        m.rect(x, HALL_Y1, x + 64, HALL_Y1 + 32, 192, 192, 160, **pillar)
        m.thing(85, x + 96, HALL_Y1 + 20)


# What each room's alcoves keep: health on the west side, ammo on the east, armor and a backpack north.
CACHE_WEST, CACHE_EAST, CACHE_NORTH = (2012, 2011, 2012), (2049, 2048, 2046), (2018, 8)


def themed_room(m, bx, t, brand=False):
    """One app's room: the spawn box at x = bx, the ring of scenery around it, its terrain and lamps."""
    h, light, trim = t["height"], t["light"], t["trim"]
    wall = t["wall"] or m.wall
    base = {"ctex": t["ceil"], "wall": wall}
    # The ring is a walkway a step up from the box (a lake's is its shallows, a step down).
    ring_floor = -8 if t.get("lake") else 16
    m.rect(bx - RING, 0, bx + 1024 + RING, 1024 + RING, ring_floor, h, light - 16, ftex=t["ring_floor"], trim=trim,
           **base)
    m.rect(bx, 0, bx + 1024, 1024, 0, h, light, ftex=t["floor"], **base)

    # Terrain inside the spawn box: every step is 24 units or less.
    if t.get("dais"):                  # stairs up to a two-tier dais under a lowered, lit ceiling
        m.rect(bx + 224, 288, bx + 800, 736, 8, h, light + 8, ftex=t["dais"][0], trim=trim, **base)
        m.rect(bx + 256, 320, bx + 768, 704, 16, h, light + 16, ftex=t["dais"][0], trim=trim, **base)
        m.rect(bx + 352, 384, bx + 672, 640, 24, h, light + 24, ftex=t["dais"][0], trim=trim, **base)
        m.rect(bx + 384, 416, bx + 640, 608, 32, h - 64, light + 32, ftex=t["dais"][1], trim=trim,
               ctex=t["lamp"], wall=wall)
    if t.get("pit"):                   # a sunken ops pit, terraced down, its walls lined with screens
        m.rect(bx + 256, 256, bx + 768, 768, -8, h, light - 8, ftex=t["floor"], trim=trim, **base)
        m.rect(bx + 288, 288, bx + 736, 736, -16, h, light - 12, ftex=t["floor"], trim=trim, **base)
        m.rect(bx + 320, 320, bx + 704, 704, -24, h, light - 16, ftex=t["pit"], trim=trim, **base)
    if t.get("lake"):                  # a moat of data around a raised, grated island and its causeways
        m.rect(bx + 192, 192, bx + 832, 832, -16, h, light, ftex=t["lake"], trim=trim, special=8, **base)
        for x1, y1, x2, y2 in ((480, 192, 544, 320), (480, 704, 544, 832), (192, 480, 320, 544), (704, 480, 832, 544)):
            m.rect(bx + x1, y1, bx + x2, y2, 8, h, light + 8, ftex=t["floor"], trim=trim, **base)
        m.rect(bx + 320, 320, bx + 704, 704, 8, h, light + 16, ftex=t["floor"], trim=trim, **base)
    if t.get("aisles"):                # three crate runs, the middle one stacked higher, aisles between them
        for x, z in ((160, 16), (448, 24), (736, 16)):
            m.rect(bx + x, 192, bx + x + 128, 832, z, h, light - 16, ftex=t["aisles"], trim=trim, **base)

    # Ceiling lamps over the box: pools of light, flickering or glowing as the theme says.
    for cx in (128, 384, 640, 896):
        for cy in (128, 384, 640, 896):
            m.overlay(bx + cx - 32, cy - 32, bx + cx + 32, cy + 32, ctex=t["lamp"], light=min(255, light + 48),
                      special=t["special"])

    # The ring: pillars flush with the walls, lit alcoves (bookshelves, screens, vault panels)
    # set into the walls between them, lamps in the corners. Nothing stands where Claude or a
    # monster could get behind it. Each alcove keeps a cache, and the far wall's two alcoves
    # are live dashboards.
    pillar = {"ftex": t["ceil"], "ctex": t["ceil"], "trim": trim, "wall": trim}
    alcove = {"ftex": t["ring_floor"], "ctex": t["lamp"], "wall": t["alcove"], "trim": trim}
    sill = max(ring_floor, 0) + 16
    west, east, north = bx - RING, bx + 1024 + RING, 1024 + RING
    for y in (128, 512, 896):
        m.rect(west, y, west + 64, y + 64, h, h, light, **pillar)
        m.rect(east - 64, y, east, y + 64, h, h, light, **pillar)
    for y, west_item, east_item in zip((256, 640, 1024), CACHE_WEST, CACHE_EAST):
        m.rect(west - 32, y, west, y + 128, sill, sill + 128, light + 16, **alcove)
        m.rect(east, y, east + 32, y + 128, sill, sill + 128, light + 16, **alcove)
        m.thing(west_item, west - 16, y + 64)
        m.thing(east_item, east + 16, y + 64)
    for x in (256, 704):
        m.rect(bx + x, north - 64, bx + x + 64, north, h, h, light, **pillar)
    for x, item in zip((64, 832), CACHE_NORTH):
        m.rect(bx + x, north, bx + x + 128, north + 32, sill, sill + 128, light + 16,
               **{**alcove, "wall": SCREEN if brand else t["alcove"]})
        m.thing(item, bx + x + 64, north + 16)
    corner, side = t["decor"]
    for x, y in ((west + 32, 32), (east - 32, 32), (west + 32, north - 32), (east - 32, north - 32)):
        m.thing(corner, x, y)
    for y in (224, 608, 992):
        m.thing(side, west + 24, y)
        m.thing(side, east - 24, y)
    for x in (160, 512, 864):          # the theme's set piece along the far wall
        m.thing(t["extra"], bx + x, north - 112)


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
    records = None
    if "--records" in args:            # connect_doom.py's cache: the records this level starts with
        i = args.index("--records")
        try:
            records = json.load(open(args[i + 1]))["monsters"]
        except (OSError, ValueError, KeyError):
            pass
        del args[i:i + 2]
    brand = "--brand" in args
    args = [a for a in args if a != "--brand"]
    write_wad(args[0] if args else "arena-raw.wad", build(mission, brand, records).lumps())
