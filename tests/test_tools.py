"""tools/: the level (make_arena), keycard messages (make_deh) and branding WAD (make_branding)."""
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from helpers import ROOT, tiny_iwad, wad_lumps

import make_arena
import make_branding
import make_deh

ROOMS = [{"connection": "S", "app": "Salesforce", "title": "Deals slipping", "writable": True},
         {"connection": "Z", "app": "Zendesk", "title": "Open escalations", "writable": False},
         {"connection": "Q", "app": "QuickBooks", "title": "Overdue invoices", "writable": False}]
DENIED = [{"connection": "PostgreSQLPII", "error": "Request blocked: PII detected in inbound query\nmore"}]
MISSION = {"rooms": ROOMS, "denied": DENIED, "masked": []}


def parse_map(lumps):
    d = dict(lumps)
    return {
        "things": list(struct.iter_unpack("<hhhhh", d["THINGS"])),
        "lines": list(struct.iter_unpack("<hhhhhhh", d["LINEDEFS"])),
        "sides": [(u.rstrip(b"\0").decode(), lo.rstrip(b"\0").decode(), m.rstrip(b"\0").decode(), s)
                  for _, _, u, lo, m, s in struct.iter_unpack("<hh8s8s8sh", d["SIDEDEFS"])],
        "verts": list(struct.iter_unpack("<hh", d["VERTEXES"])),
        "sectors": list(struct.iter_unpack("<hh8s8shhh", d["SECTORS"])),
    }


class ArenaTest(unittest.TestCase):
    def level(self, mission, brand=False):
        return parse_map(make_arena.build(mission, brand).lumps())

    def assert_valid(self, m):
        """What a node builder and the engine need: every reference resolves, lines are sane."""
        nv, ns, nsec = len(m["verts"]), len(m["sides"]), len(m["sectors"])
        seen = Counter()
        for v1, v2, flags, special, tag, front, back in m["lines"]:
            self.assertTrue(0 <= v1 < nv and 0 <= v2 < nv and v1 != v2)
            (x1, y1), (x2, y2) = m["verts"][v1], m["verts"][v2]
            self.assertTrue(x1 == x2 or y1 == y2, "grid lines are axis aligned")
            self.assertTrue(0 <= front < ns)
            if back == -1:
                self.assertTrue(flags & make_arena.IMPASSABLE, "one-sided lines block")
                self.assertFalse(flags & make_arena.TWO_SIDED)
            else:
                self.assertTrue(0 <= back < ns and flags & make_arena.TWO_SIDED)
                self.assertNotEqual(m["sides"][front][3], m["sides"][back][3])
            seen[frozenset((v1, v2))] += 1
            self.assertEqual(tag, 0)
        self.assertEqual(max(seen.values()), 1, "no duplicate lines")
        self.assertTrue(all(0 <= s < nsec for *_, s in m["sides"]))
        self.assertEqual({s for *_, s in m["sides"]}, set(range(nsec)), "every sector has walls")
        for u, lo, mid, _ in m["sides"]:
            self.assertTrue(all(len(t) <= 8 for t in (u, lo, mid)))
        for floor, ceil, *_ in m["sectors"]:
            self.assertLessEqual(floor, ceil)
        # Every vertex is shared by an even number of line ends (closed outlines, no dangling walls).
        ends = Counter(v for v1, v2, *_ in m["lines"] for v in (v1, v2))
        self.assertTrue(all(n >= 2 for n in ends.values()))
        # No T-junctions: no vertex lies inside another line.
        for v1, v2, *_ in m["lines"]:
            (x1, y1), (x2, y2) = m["verts"][v1], m["verts"][v2]
            for p in m["verts"]:
                self.assertFalse(make_arena._on(p, x1, y1, x2, y2), (p, (x1, y1, x2, y2)))

    def things(self, m, type_):
        return [(x, y) for x, y, _, t, _ in m["things"] if t == type_]

    def specials(self, m):
        return Counter(sp for _, _, _, sp, *_ in m["lines"] if sp)

    def test_full_mission(self):
        m = self.level(MISSION)
        self.assert_valid(m)
        self.assertEqual(self.things(m, 1), [(2560, -896)])                 # one player start, in the lobby
        self.assertEqual(len(self.things(m, 5)), 1)                          # blue key: read-only rooms
        self.assertEqual(len(self.things(m, 6)), 1)                          # yellow key: the writable room
        self.assertEqual(self.things(m, 13), [])                             # never a red key
        sp = self.specials(m)
        self.assertEqual(set(sp), {32, 33, 34})
        self.assertEqual(sp[34] * 2, sp[32])                                 # one yellow door, two blue

    def test_spawn_boxes_are_open_floor(self):
        """psdoom drops monsters into 1024x1024 boxes at x = 0, 2048, 4096; each must be one sector."""
        mp = make_arena.build(MISSION)
        for bx in make_arena.ARENA_X:
            cells = {mp.cells.get((cx, cy)) for cx in range(bx // 16, (bx + 1024) // 16) for cy in range(64)}
            self.assertEqual(len(cells), 1)
            self.assertIsNotNone(cells.pop())

    def test_no_denials_no_vault(self):
        m = self.level({"rooms": ROOMS[:2], "denied": []})
        self.assert_valid(m)
        self.assertNotIn(33, self.specials(m))

    def test_read_only_rooms_have_only_blue_keys(self):
        m = self.level({"rooms": ROOMS[1:], "denied": DENIED})
        self.assertEqual((len(self.things(m, 5)), len(self.things(m, 6))), (1, 0))
        self.assertEqual(set(self.specials(m)), {32, 33})

    def test_empty_mission_still_builds(self):
        m = self.level({})
        self.assert_valid(m)
        self.assertEqual(self.specials(m), Counter())
        self.assertEqual(len(self.things(m, 1)), 1)

    def test_more_than_three_rooms_are_dropped(self):
        self.assertEqual(self.specials(self.level({"rooms": ROOMS * 2}))[32],
                         self.specials(self.level({"rooms": ROOMS}))[32])

    def test_brand_textures(self):
        m = self.level(MISSION, brand=True)
        self.assert_valid(m)
        textures = {t for u, lo, mid, _ in m["sides"] for t in (u, lo, mid)}
        self.assertTrue({"CAIWALL", "CAIRM0", "CAIRM1", "CAIRM2", "CAIVAULT", "CAIXXXX",
                         "DOORBLU", "DOORYEL", "DOORRED", "BIGDOOR2"} <= textures)
        self.assertNotIn("TEKWALL4", textures)
        plain = {t for u, lo, mid, _ in self.level(MISSION)["sides"] for t in (u, lo, mid)}
        self.assertFalse({t for t in plain if t.startswith("CAI")})

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            mission = Path(tmp) / "mission.json"
            mission.write_text(json.dumps(MISSION))
            out = Path(tmp) / "level.wad"
            r = subprocess.run([sys.executable, str(ROOT / "tools/make_arena.py"), str(out), "--mission",
                                str(mission), "--brand"], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            lumps = wad_lumps(out.read_bytes())
            self.assertEqual([n for n, _ in lumps], ["MAP01", "THINGS", "LINEDEFS", "SIDEDEFS", "VERTEXES", "SECTORS"])
            self.assertEqual(lumps, make_arena.build(MISSION, brand=True).lumps())
            # without a mission: three plain read-only rooms
            r = subprocess.run([sys.executable, str(ROOT / "tools/make_arena.py"), str(out)], timeout=60)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(self.specials(parse_map(wad_lumps(out.read_bytes())))[32], 6)


class DehTest(unittest.TestCase):
    def test_patch(self):
        text = make_deh.patch(MISSION)
        self.assertTrue(text.startswith("Patch File for DeHackEd v3.0\n# *allow-long-strings*\n"))
        blocks = text.split("\n\n")[1:-1]
        self.assertEqual(len(blocks), 5)
        swaps = {}
        for block in blocks:
            header, body = block.split("\n", 1)
            _, old_len, new_len = header.split()
            old, new = body[:int(old_len)], body[int(old_len):]
            self.assertEqual(len(new), int(new_len))
            swaps[old] = new
        self.assertEqual(swaps[make_deh.PD_REDK], "CONNECT AI: PII DETECTED IN INBOUND QUERY")
        self.assertEqual(swaps[make_deh.GOTBLUECARD], "READ ACCESS GRANTED BY CONNECT AI")
        self.assertEqual(swaps[make_deh.GOTYELWCARD], "WRITE ACCESS GRANTED BY CONNECT AI")
        self.assertEqual(set(swaps), {make_deh.PD_BLUEK, make_deh.PD_YELLOWK, make_deh.PD_REDK,
                                      make_deh.GOTBLUECARD, make_deh.GOTYELWCARD})

    def test_refusal(self):
        self.assertEqual(make_deh.refusal({}), "NO ACCESS")
        self.assertEqual(make_deh.refusal({"denied": [{"error": "Cannot obtain connection"}]}),
                         "CONNECT AI: CANNOT OBTAIN CONNECTION")

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "m.json").write_text(json.dumps(MISSION))
            r = subprocess.run([sys.executable, str(ROOT / "tools/make_deh.py"), f"{tmp}/m.json", f"{tmp}/l.deh"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((Path(tmp) / "l.deh").read_text(), make_deh.patch(MISSION))


class BrandingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.iwad = Path(cls.tmp.name) / "iwad.wad"
        cls.palette = tiny_iwad(cls.iwad)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_patch_round_trip(self):
        from PIL import Image
        img = Image.new("RGBA", (40, 200))     # taller than a 128-row post (vanilla tops out at 254)
        for x in range(40):
            for y in range(200):
                if (x + y) % 7:                                # holes are transparent
                    img.putpixel((x, y), self.palette[(x * 3 + y) % 128] + (255,))
        lump = make_branding.to_patch(img, self.palette, left=5, top=-4)
        self.assertEqual(struct.unpack("<hhhh", lump[:8]), (40, 200, 5, -4))
        w, h, px = make_branding.patch_pixels(lump)
        self.assertEqual((w, h), (40, 200))
        want = {(x, y): (x * 3 + y) % 128 for x in range(40) for y in range(200) if (x + y) % 7}
        self.assertEqual(px, want)

    def build(self, mission=None, kit="no-kit"):
        out = Path(self.tmp.name) / "brand.wad"
        make_branding.build(self.iwad, kit, out, mission=mission)
        return dict(wad_lumps(out.read_bytes()))

    def textures(self, lumps):
        tb = lumps["TEXTURE1"]
        n = struct.unpack("<i", tb[:4])[0]
        offs = struct.unpack(f"<{n}i", tb[4:4 + 4 * n])
        pn = lumps["PNAMES"]
        names = [pn[4 + 8 * i:12 + 8 * i].rstrip(b"\0").decode() for i in range(struct.unpack("<i", pn[:4])[0])]
        out = {}
        for o in offs:
            name = tb[o:o + 8].rstrip(b"\0").decode()
            _, w, h, _, count = struct.unpack("<ihhih", tb[o + 8:o + 22])
            patches = [names[struct.unpack("<hhhhh", tb[o + 22 + 10 * i:o + 32 + 10 * i])[2]] for i in range(count)]
            out[name] = (w, h, patches)
        return out

    def test_without_mission_or_kit(self):
        lumps = self.build()
        self.assertEqual(list(lumps), ["PNAMES", "TEXTURE1", "CAILOGO", "CAIWALLP"])   # no menu logo
        tex = self.textures(lumps)
        self.assertEqual(tex["TEKWALL4"], (64, 128, ["W94_1"]))                         # IWAD textures kept
        self.assertEqual(tex["CAISIGN"], (256, 128, ["CAILOGO"]))
        self.assertEqual(tex["CAIWALL"], (64, 128, ["CAIWALLP"]))
        w, h, px = make_branding.patch_pixels(lumps["CAIWALLP"])
        yellow = self.palette.index(make_branding.AGILITY)
        self.assertEqual({px[(x, h - 1)] for x in range(w)}, {yellow})                  # the trim
        self.assertNotIn(yellow, {px[(x, 0)] for x in range(w)})

    def test_mission_signs(self):
        lumps = self.build(MISSION)
        tex = self.textures(lumps)
        for name in ("CAIRM0", "CAIRM1", "CAIRM2", "CAIVAULT"):
            self.assertEqual(tex[name], (256, 128, [f"P{name[3:]}"]))
        self.assertEqual(tex["CAIXXXX"], (128, 128, ["PXXXX"]))
        for name, lump in lumps.items():
            self.assertLessEqual(len(name), 8)
            if name not in ("PNAMES", "TEXTURE1"):
                w, h, _ = make_branding.patch_pixels(lump)
                self.assertLess(len(lump), 65536, f"{name}: vanilla caps patches at 64 KB")
        # sign border is the keycard color: yellow for the writable room, blue for read only
        for name, color in (("PRM0", make_branding.KEY_YELLOW), ("PRM1", make_branding.KEY_BLUE),
                            ("PVAULT", make_branding.KEY_RED)):
            _, _, px = make_branding.patch_pixels(lumps[name])
            self.assertEqual(self.palette[px[(8, 64)]], color, name)

    def test_mission_without_denials_has_no_vault(self):
        tex = self.textures(self.build({"rooms": ROOMS[:1]}))
        self.assertIn("CAIRM0", tex)
        self.assertFalse({"CAIRM1", "CAIVAULT", "CAIXXXX"} & set(tex))

    def test_with_brand_kit_logo(self):
        from PIL import Image
        kit = Path(self.tmp.name) / "kit"
        logo = kit / make_branding.LOGO
        logo.parent.mkdir(parents=True)
        Image.new("RGBA", (400, 100), make_branding.AGILITY + (255,)).save(logo)
        lumps = self.build(kit=kit)
        w, h, left, top = struct.unpack("<hhhh", lumps["M_DOOM"][:8])
        self.assertEqual(w, 204)
        self.assertEqual(94 - left + w // 2, 160, "menu logo centered on the 320 px screen")

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "m.json").write_text(json.dumps(MISSION))
            r = subprocess.run([sys.executable, str(ROOT / "tools/make_branding.py"), str(self.iwad), "no-kit",
                                f"{tmp}/b.wad", "--mission", f"{tmp}/m.json"], capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("PVAULT", dict(wad_lumps((Path(tmp) / "b.wad").read_bytes())))


if __name__ == "__main__":
    unittest.main()
