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
        """psdoom drops monsters into 1024x1024 boxes at x = 0, 2048, 4096, and Claude crosses them in
        straight lines: every cell is open, roomy floor, and no step between neighbors tops 24 units."""
        for rooms in (ROOMS, [{"connection": c, "app": c} for c in ("Snowflake", "Acme", "NetSuite")]):
            mp = make_arena.build({"rooms": rooms})
            for bx in make_arena.ARENA_X:
                box = {(cx, cy) for cx in range(bx // 16, (bx + 1024) // 16) for cy in range(64)}
                for cell in box:
                    s = mp.sectors[mp.cells[cell]]
                    self.assertGreaterEqual(s["ceil"] - s["floor"], 128, (rooms[0]["app"], cell))
                    for n in ((cell[0] + 1, cell[1]), (cell[0], cell[1] + 1)):
                        if n in box:
                            self.assertLessEqual(abs(s["floor"] - mp.sectors[mp.cells[n]]["floor"]), 24)

    def test_hall_stays_open_and_level(self):
        """Claude's routes run along the hall and straight down from each door: the hall's scenery
        (pillars, lamps) stays within 32 units of its walls, and its floor stays level."""
        mp = make_arena.build(MISSION, brand=True)
        for cx in range(0, 5120 // 16):
            for cy in range((make_arena.HALL_Y1 + 32) // 16, (make_arena.HALL_Y2 - 32) // 16):
                s = mp.sectors[mp.cells[(cx, cy)]]
                self.assertEqual(s["floor"], 0, (cx * 16, cy * 16))
                self.assertGreaterEqual(s["ceil"], 128)
        for t, x, y, _ in mp.things:
            if make_arena.HALL_Y1 < y < make_arena.HALL_Y2:
                self.assertTrue(y < make_arena.HALL_Y1 + 32 or y > make_arena.HALL_Y2 - 32, (t, x, y))

    def test_rooms_are_themed_by_app(self):
        """Each room gets its app's look, and the three rooms never share one."""
        m = self.level(MISSION)
        flats = {f.rstrip(b"\0").decode() for _, _, f, *_ in m["sectors"]}
        self.assertTrue({"FLAT5_2", "FLOOR0_6", "AQF076"} <= flats)          # office, ops center, treasury
        textures = {t for u, lo, mid, _ in m["sides"] for t in (u, lo, mid)}
        self.assertTrue({"WOOD9", "PANBOOK", "SPACEW3", "COMPTALL", "BROWNGRN", "SILVER2"} <= textures)  # QuickBooks green
        self.assertEqual([t["key"] for t in make_arena.room_themes([ROOMS[0]] * 3)], ["salesforce", "tech", "data"])

    def test_windows_are_glass(self):
        """Each room has a window onto the hall that stops sight, shots and sound: monsters
        don't wake, and nobody fights through it."""
        m = self.level(MISSION, brand=True)
        self.assert_valid(m)
        glass = [(f, b) for _, _, flags, _, _, f, b in m["lines"] if flags & make_arena.GLASS]
        self.assertTrue(all(b != -1 for _, b in glass))
        for _, _, flags, *_ in m["lines"]:
            if flags & make_arena.GLASS:
                self.assertTrue(flags & make_arena.IMPASSABLE and flags & make_arena.SOUND_BLOCK)
        windows = {m["sides"][s][3] for pair in glass for s in pair
                   if m["sectors"][m["sides"][s][3]][0] in (32, 40)}       # the window sills
        self.assertEqual(len(windows), 4)                                    # three rooms and the vault

    def test_door_landings_keep_monsters_off(self):
        """Inside each door, a raised landing that monsters can't step onto, so they can't jam the
        doorway; psdoom's spawn rows start above it (y = 192, the patch)."""
        mp = make_arena.build(MISSION)
        for bx in make_arena.ARENA_X:
            s = mp.sectors[mp.cells[((bx + 512) // 16, 4)]]
            self.assertTrue(s.get("landing"))
            self.assertEqual(s["floor"], 24)
        m = parse_map(mp.lumps())
        blocked = [flags for _, _, flags, *_ in m["lines"] if flags & make_arena.BLOCK_MONSTERS]
        self.assertGreaterEqual(len(blocked), 3 * 3)               # three sides of each landing at least
        self.assertIn("mt_ext.y = 192 + ( pid % 13 ) * 64;", (ROOT / "patches/psdoom-ng-connect-doom.patch").read_text())

    def test_loot_follows_the_records(self):
        """Each record leaves shells in its room's ring, each urgent one a rocket and a stimpack too."""
        records = [{"pid": 3 * k + 0, "demon": int(k < 2)} for k in range(5)] + [{"pid": 3 * k + 2, "demon": 0}
                                                                                  for k in range(3)]
        base = Counter(t for t, *_ in make_arena.build(MISSION).things)
        mp = make_arena.build(MISSION, records=records)
        self.assertEqual(Counter(t for t, *_ in mp.things) - base, Counter({2008: 8, 2010: 2, 2011: 2}))
        rooms = Counter((t, (x + make_arena.RING) // 2048) for t, x, y, _ in mp.things if t in (2008, 2010) and y >= 0)
        self.assertEqual(rooms, Counter({(2008, 0): 5, (2008, 2): 3, (2010, 0): 2}))
        # without a cache, the offline mission's own rows
        rows = {**ROOMS[1], "rows": [{"Id": 1, "Hot": 1}, {"Id": 2, "Hot": 0}]}
        mp = make_arena.build({"rooms": [rows]})
        self.assertEqual(Counter(t for t, *_ in mp.things if t in (2008, 2010)), Counter({2008: 2, 2010: 1}))

    def test_alcove_caches_and_set_pieces(self):
        mp = make_arena.build(MISSION)
        things = Counter(t for t, *_ in mp.things)
        for item in (*make_arena.CACHE_WEST, *make_arena.CACHE_EAST, *make_arena.CACHE_NORTH):
            self.assertGreaterEqual(things[item], 3, item)                   # in every room
        for t in make_arena.room_themes(ROOMS):
            self.assertGreaterEqual(things[t["extra"]], 3, t["key"])

    def test_hall_lights_ripple(self):
        """The hall's light strips glow slowly (psdoom's special 21), each starting a little later."""
        m = self.level(MISSION)
        tags = sorted({tag for *_, light, special, tag in m["sectors"] if special == make_arena.SLOW_GLOW})
        self.assertEqual(tags, [8 * i for i in range(20)])

    def test_screens_only_with_the_brand(self):
        """The animated dashboards are the branding WAD's: without it, DBRAIN is the stock Icon of Sin."""
        def screens(brand):
            return sum(make_arena.SCREEN in side[:3] for side in self.level(MISSION, brand)["sides"])
        self.assertGreater(screens(True), 0)
        self.assertEqual(screens(False), 0)

    # Every app on the demo's Connect AI account (getCatalogs: connection, app), as of 2026-10.
    ACCOUNT = [("Acumatica", "Acumatica"), ("ADP", "ADP"), ("BullhornCRM_Somya", "Bullhorn CRM"),
               ("Dynamics3651", "Dynamics 365"), ("Dynamics365BC", "Dynamics 365 Business Central"),
               ("EpicorKinetic1", "Epicor Kinetic"), ("ExcelOnline1", "Excel Online"), ("Gmail1", "Gmail"),
               ("GoogleAds", "Google Ads"), ("GoogleBigQuery1", "Google BigQuery"), ("GoogleDrive", "Google Drive"),
               ("ITSM_ServiceNow", "ServiceNow"), ("Jira", "Jira"), ("MicrosoftTeams_Somya", "Microsoft Teams"),
               ("MYOB", "MYOB"), ("MySQL1", "MySQL"), ("NetSuite", "NetSuite"), ("Office3651_Somya", "Office 365"),
               ("Oracle1", "Oracle"), ("PostgreSQL1", "PostgreSQL"), ("QuickBooksOnline", "QuickBooks Online"),
               ("SageIntacct1", "Sage Intacct"), ("Salesforce", "Salesforce"), ("SAPHANA", "SAP HANA"),
               ("Shopify", "Shopify"), ("Slack1", "Slack"), ("Snowflake", "Snowflake"), ("SQLServer", "SQL Server"),
               ("Stripe1", "Stripe"), ("Workday", "Workday"), ("Xero", "Xero"),
               ("YouTubeAnalytics1", "YouTube Analytics"), ("Zendesk", "Zendesk")]

    def test_every_account_app_has_its_own_look(self):
        themes = [make_arena.room_themes([{"connection": c, "app": a}])[0] for c, a in self.ACCOUNT]
        self.assertEqual(len({t["key"] for t in themes}), len(self.ACCOUNT), [t["key"] for t in themes])
        for (_, app), t in zip(self.ACCOUNT, themes):
            self.assertNotIn(t["key"], make_arena.FAMILIES, app)     # its own entry, not just a layout
        by_key = {t["key"]: t for t in themes}
        self.assertEqual(by_key["dynamics"]["family"], "crm")
        self.assertEqual(by_key["business central"]["family"], "erp")   # not taken for Dynamics 365 CRM
        self.assertEqual(by_key["sql server"]["family"], "data")
        # each one builds, and three rooms of a level never share a monster pair
        for i in range(0, len(self.ACCOUNT), 3):
            rooms = [{"connection": c, "app": a} for c, a in self.ACCOUNT[i:i + 3]]
            self.assert_valid(self.level({"rooms": rooms}))
            pairs = [t["monsters"] for t in make_arena.room_themes(rooms)]
            self.assertEqual(len(set(pairs)), len(pairs))
        # the same app twice: the second room falls back to another layout and pair
        twice = make_arena.room_themes([{"connection": "Salesforce", "app": "Salesforce"},
                                        {"connection": "Salesforce_CRM", "app": "Salesforce"}])
        self.assertNotEqual(twice[0]["monsters"], twice[1]["monsters"])
        self.assertNotEqual(twice[0]["key"], twice[1]["key"])

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


class SpritesTest(unittest.TestCase):
    def test_missing_sprites_are_skipped_and_logos_copied(self):
        """A WAD without the legend's sprites (like the tests' tiny one) mustn't stop raid.sh."""
        import make_sprites
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tiny_iwad(tmp / "tiny.wad")
            make_sprites.main(tmp / "tiny.wad", tmp / "panel/sprites", ROOT / "assets/brand")
            self.assertEqual(sorted(p.name for p in (tmp / "panel/brand").iterdir()), ["cdata.svg", "connect-ai.svg"])


class DehTest(unittest.TestCase):
    def test_patch(self):
        text = make_deh.patch(MISSION)
        self.assertTrue(text.startswith("Patch File for DeHackEd v3.0\n# *allow-long-strings*\n"))
        blocks = text.split("\n\n")[1:-1]
        things = [b for b in blocks if b.startswith("Thing ")]
        self.assertIn("Thing 18 (Hell knight)\nHit points = 100", things)   # every record monster, easier than Doom's
        self.assertIn("Thing 12 (Imp)\nHit points = 30", things)
        self.assertEqual(len(things), sum("health" in m for m in make_deh.MONSTERS.values()))
        blocks = [b for b in blocks if b not in things]
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

    def test_no_gore(self):
        text = make_deh.patch(MISSION, gore=False)
        self.assertTrue(text.startswith(make_deh.patch(MISSION)))   # the keycard messages are unchanged
        frames = {}
        for block in text.split("\n\n"):
            lines = block.strip().split("\n")
            if lines[0].startswith("Frame "):
                frames[int(lines[0].split()[1])] = dict(line.split(" = ") for line in lines[1:])
        for n in make_deh.BLOOD_FRAMES:   # every blood frame, not just the first: P_SpawnBlood skips it
            self.assertEqual(frames[n]["Sprite number"], str(make_deh.SPR_PUFF))
        for monster in make_deh.MONSTERS.values():   # every monster a room can have
            first, last = monster["die"]
            fog = []
            for n in range(first, last + 1):
                self.assertEqual(frames[n]["Sprite number"], str(make_deh.SPR_TFOG))
                fog.append(int(frames[n]["Sprite subnumber"]) - make_deh.BRIGHT)
            # TFOG has frames A..J: one past J and the game quits mid-kill ("invalid sprite frame")
            self.assertTrue(all(0 <= f <= make_deh.FOG_LAST for f in fog), (monster["name"], fog))
            self.assertEqual(fog, sorted(fog), monster["name"])                  # it only fades
            self.assertEqual(fog[-1], make_deh.FOG_LAST)
            self.assertEqual(frames[last]["Duration"], "-1")   # the death still ends, so the kill counts
        self.assertIn("Thing 3 (Shotgun soldier)\nExploding frame = 222", text)
        self.assertIn("Thing 12 (Imp)\nExploding frame = 457", text)
        self.assertNotIn("Frame ", make_deh.patch(MISSION))

    def test_cli_no_gore(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "m.json").write_text(json.dumps(MISSION))
            r = subprocess.run([sys.executable, str(ROOT / "tools/make_deh.py"), f"{tmp}/m.json", f"{tmp}/l.deh",
                                "--no-gore"], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((Path(tmp) / "l.deh").read_text(), make_deh.patch(MISSION, gore=False))


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

    def test_too_tall_patch_is_a_clear_error(self):
        from PIL import Image
        make_branding.to_patch(Image.new("RGBA", (2, 255), (1, 1, 1, 255)), self.palette)
        with self.assertRaisesRegex(ValueError, "256 px tall"):
            make_branding.to_patch(Image.new("RGBA", (2, 256), (1, 1, 1, 255)), self.palette)

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

    def test_options_menu_labels(self):
        """Shaped like the stock M_MESSG: about as tall, top offset 1, red letters edged in black."""
        lumps = self.build()
        for name, _ in make_branding.MENU_SWITCHES:
            w, h, px = make_branding.patch_pixels(lumps[name])
            self.assertEqual(struct.unpack("<hh", lumps[name][4:8]), (0, 1), name)
            self.assertTrue(12 <= h <= 15 and 40 < w < 200, (name, w, h))    # leaves room for ON / OFF in 320
            colors = {self.palette[i] for i in px.values()}
            self.assertIn((0, 0, 0), colors)
            self.assertTrue(all(c == (0, 0, 0) or c[0] > 2 * max(c[1:]) for c in colors), colors)   # reds

    def test_without_mission_or_kit(self):
        lumps = self.build()
        self.assertEqual(list(lumps), ["PNAMES", "TEXTURE1", "CAILOGO", "CAIWALLP",     # no menu logo,
                                       "CAISCRN1", "CAISCRN2", "CAISCRN3", "CAISCRN4",
                                       *[n for n, _ in make_branding.MENU_SWITCHES]])  # but the switches
        tex = self.textures(lumps)
        self.assertEqual(tex["TEKWALL4"], (64, 128, ["W94_1"]))                         # IWAD textures kept
        # the dashboard's frames take DBRAIN1-4's places, in order, so Doom animates them
        self.assertEqual(list(tex)[1:5], ["DBRAIN1", "DBRAIN2", "DBRAIN3", "DBRAIN4"])
        self.assertEqual([tex[f"DBRAIN{i}"] for i in range(1, 5)], [(128, 128, [f"CAISCRN{i}"]) for i in range(1, 5)])
        frames = [make_branding.patch_pixels(lumps[f"CAISCRN{i}"])[2] for i in range(1, 5)]
        self.assertEqual(len({tuple(sorted(f.items())) for f in frames}), 4)            # every frame moves
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
        w, h, left, top = struct.unpack("<hhhh", lumps["M_PAUSE"][:8])
        self.assertEqual(126 - left + w // 2, 160, "pause graphic centered where Doom puts the stock 68 px one")
        self.assertLessEqual(4 - top + h, 168, "fits in the view above the status bar")
        w, h = struct.unpack("<hh", lumps["CAICORNR"][:4])
        self.assertLessEqual((w, h), (72, 20), "small enough for a corner")

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
