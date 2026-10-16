#!/usr/bin/env python3
"""Each app room's look and monsters, picked from the app's name.

  themes.py monsters <mission.json>   psdoom's PSDOOM_MONSTERS for this mission (raid.sh)

A room is built from one of six layouts (FAMILIES): a CRM office with a dais, a support ops
center with a sunken pit, a billing treasury, a data lake, an ERP warehouse with crate aisles,
or the tech base. Each app Connect AI can reach (APPS) picks a layout and lightly brands it:
its own walls, alcove art, floor and lamp color, and its own pair of monsters, a record and an
urgent record (always the bigger one). An app not listed gets the layout its name suggests, or
the tech base. Three rooms of one level never share an app's look or a monster pair.
"""
import json
import sys

# Doom 2 monsters psdoom can spawn as records. `thing` is the doomednum (PSDOOM_MONSTERS),
# `deh` the DeHackEd thing number, `die`/`xdie` the frame ranges of its deaths (NOGORE fogs them),
# `sprite` the panel's picture (tools/make_sprites.py). `health` (tools/make_deh.py) brings the big
# ones down to about a pink demon's 150, so a room of urgent records clears as fast as it always
# did, and a booth visitor isn't facing a room of full-strength hell knights.
MONSTERS = {
    "zombie":      {"name": "Zombie", "thing": 3004, "deh": 2, "sprite": "POSSA1",
                    "die": (189, 193), "xdie": (194, 202)},
    "soldier":     {"name": "Shotgun soldier", "thing": 9, "deh": 3, "sprite": "SPOSA1",
                    "die": (222, 226), "xdie": (227, 235)},
    "chaingunner": {"name": "Chaingunner", "thing": 65, "deh": 11, "sprite": "CPOSA1",
                    "die": (422, 428), "xdie": (429, 434)},
    "imp":         {"name": "Imp", "thing": 3001, "deh": 12, "sprite": "TROOA1",
                    "die": (457, 461), "xdie": (462, 469)},
    "demon":       {"name": "Pink demon", "thing": 3002, "deh": 13, "sprite": "SARGA1",
                    "die": (490, 495)},
    "caco":        {"name": "Cacodemon", "thing": 3005, "deh": 15, "sprite": "HEADA1",
                    "die": (510, 515), "health": 200},
    "knight":      {"name": "Hell knight", "thing": 69, "deh": 18, "sprite": "BOS2A1",
                    "die": (571, 577), "health": 200},
    "baron":       {"name": "Baron", "thing": 3003, "deh": 16, "sprite": "BOSSA1",
                    "die": (542, 548), "health": 200},
    "revenant":    {"name": "Revenant", "thing": 66, "deh": 6, "sprite": "SKELA1D1",
                    "die": (345, 350), "health": 150},
    "mancubus":    {"name": "Mancubus", "thing": 67, "deh": 9, "sprite": "FATTA1",
                    "die": (388, 397), "health": 200},
}
RECORDS = ("zombie", "soldier", "chaingunner", "imp")
URGENT = ("demon", "caco", "knight", "baron", "revenant", "mancubus")

# The layouts. Light specials (sector type): 0 steady, 8 glow, 17 flicker. `wall` None is the
# level's own wall (branded or not). The terrain key picks the layout: dais, pit, lake or aisles.
FAMILIES = {
    "crm": {
        "label": "CRM office", "match": ("crm", "hubspot", "pipedrive", "zoho", "sugar", "insightly"),
        "monsters": ("zombie", "knight"),
        "wall": "WOOD9", "trim": "WOODMET1", "alcove": "PANBOOK",
        "floor": "FLAT5_2", "ring_floor": "FLAT5_1", "ceil": "CEIL3_1", "lamp": "CEIL3_4",
        "dais": ("FLOOR0_2", "AQF048"), "height": 256, "light": 200, "special": 0,
        "decor": (2028, 35),
    },
    "support": {
        "label": "Support ops center", "match": ("desk", "ticket", "support", "intercom", "help", "chat",
                                                 "mail", "message"),
        "monsters": ("imp", "caco"),
        "wall": "SPACEW3", "trim": "COMPBLUE", "alcove": "COMPTALL",
        "floor": "FLOOR0_6", "ring_floor": "FLAT14", "ceil": "CEIL4_3", "lamp": "TLITE6_5",
        "pit": "CONS1_5", "height": 224, "light": 192, "special": 17,
        "decor": (85, 86),
    },
    "billing": {
        "label": "Billing treasury", "match": ("invoice", "billing", "payment", "finance", "account", "ledger",
                                               "paypal", "square", "chargebee", "zuora"),
        "monsters": ("soldier", "demon"),
        "wall": "BRONZE1", "trim": "BRONZE2", "alcove": "SILVER2",
        "floor": "AQF076", "ring_floor": "FLOOR5_1", "ceil": "CEIL5_2", "lamp": "TLITE6_6",
        "dais": ("FLAT23", "GATE4"), "height": 256, "light": 200, "special": 8,
        "decor": (2028, 48),
    },
    "data": {
        "label": "Data lake", "match": ("sql", "postgres", "oracle", "database", "db", "warehouse", "lake",
                                        "redshift", "databricks", "mongo", "hana"),
        "monsters": ("chaingunner", "revenant"),
        "wall": "SILVER3", "trim": "METAL1", "alcove": "COMPUTE2",
        "floor": "FCGRATE2", "ring_floor": "FWATER1", "ceil": "FLAT4", "lamp": "FLAT17",
        "lake": "FWATER1", "height": 256, "light": 192, "special": 8,
        "decor": (86, 48),
    },
    "erp": {
        "label": "ERP warehouse", "match": ("erp", "inventory", "order", "shop", "commerce", "drive", "file",
                                            "excel", "sheet", "sharepoint", "box", "dropbox"),
        "monsters": ("soldier", "baron"),
        "wall": "METAL2", "trim": "SUPPORT2", "alcove": "CRATWIDE",
        "floor": "AQF050", "ring_floor": "FLAT5_4", "ceil": "FLAT20", "lamp": "FLAT17",
        "aisles": "CRATOP1", "height": 256, "light": 192, "special": 0,
        "decor": (2028, 70),
    },
    "tech": {
        "label": "Tech base", "match": (),
        "monsters": ("soldier", "demon"),
        "wall": None, "trim": "TEKLITE", "alcove": "COMPSTA1",
        "floor": "FLOOR4_8", "ring_floor": "FLOOR5_1", "ceil": "CEIL3_5", "lamp": "TLITE6_1",
        "dais": ("FLOOR0_6", "FLOOR0_3"), "height": 256, "light": 200, "special": 0,
        "decor": (2028, 85),
    },
}
FALLBACK = ("tech", "data", "crm", "support", "billing", "erp")   # for an unknown app, or a repeat

# Every app on the Connect AI account, most specific name first. `family` is the layout; the rest
# overrides its look: walls, alcove art, floor, lamp color, light, in the app's colors where Doom
# has them.
APPS = [
    ("business central", "erp", ("imp", "knight"), {"wall": "MARBGRAY", "alcove": "PANBLUE"}),
    ("salesforce", "crm", ("zombie", "knight"), {}),
    ("dynamics", "crm", ("imp", "baron"), {"wall": "MARBGRAY", "alcove": "PANBLUE", "floor": "FLOOR0_6"}),
    ("bullhorn", "crm", ("imp", "mancubus"), {"wall": "BRICK5", "alcove": "PANCASE2", "lamp": "TLITE6_6"}),
    ("workday", "crm", ("soldier", "caco"), {"wall": "PANCASE2", "alcove": "PANBLUE", "floor": "FLOOR0_2"}),
    ("adp", "crm", ("chaingunner", "baron"), {"wall": "BRICK7", "alcove": "PANRED", "floor": "FLAT5_1"}),
    ("zendesk", "support", ("imp", "caco"), {}),
    ("servicenow", "support", ("zombie", "revenant"), {"wall": "TEKGREN2", "trim": "TEKGREN5", "lamp": "TLITE6_6"}),
    ("jira", "support", ("chaingunner", "baron"), {"wall": "STARGR1", "alcove": "COMPOHSO", "special": 0}),
    ("slack", "support", ("imp", "revenant"), {"wall": "STARG3", "alcove": "COMPUTE3", "lamp": "TLITE6_1"}),
    ("teams", "support", ("zombie", "caco"), {"wall": "SILVER1", "alcove": "PANBLUE", "trim": "SHAWN2"}),
    ("gmail", "support", ("soldier", "knight"), {"wall": "GRAY7", "alcove": "PANRED", "lamp": "TLITE6_5"}),
    ("office", "support", ("imp", "demon"), {"wall": "ICKWALL3", "alcove": "COMPSTA2", "trim": "SHAWN2"}),
    ("stripe", "billing", ("soldier", "demon"), {}),
    ("quickbooks", "billing", ("zombie", "mancubus"), {"wall": "BROWNGRN", "trim": "TEKGREN5"}),
    ("xero", "billing", ("imp", "knight"), {"wall": "STARGR1", "alcove": "LITEBLU4", "trim": "SHAWN2"}),
    ("sage", "billing", ("chaingunner", "caco"), {"wall": "TEKGREN5", "trim": "TEKGREN2"}),
    ("myob", "billing", ("chaingunner", "revenant"), {"wall": "MODWALL1", "alcove": "COMPWERD"}),
    ("netsuite", "erp", ("soldier", "baron"), {}),
    ("acumatica", "erp", ("zombie", "demon"), {"wall": "STARBR2", "alcove": "CRATE2"}),
    ("epicor", "erp", ("chaingunner", "mancubus"), {"wall": "BROWN1", "trim": "WOODMET1"}),
    ("excel", "erp", ("zombie", "caco"), {"wall": "TEKGREN1", "alcove": "CRATE1", "lamp": "TLITE6_5"}),
    ("google drive", "erp", ("imp", "revenant"), {"wall": "STARG3", "alcove": "CRATE1", "ring_floor": "FLAT5_1"}),
    ("shopify", "erp", ("soldier", "knight"), {"wall": "CRLWDL6", "alcove": "TEKGREN5", "floor": "FLAT5_2"}),
    ("snowflake", "data", ("chaingunner", "revenant"), {}),
    ("hana", "data", ("chaingunner", "knight"), {"wall": "SILVER1", "alcove": "COMPUTE1"}),
    ("bigquery", "data", ("zombie", "baron"), {"wall": "STARGR1", "alcove": "COMPBLUE", "lamp": "TLITE6_5"}),
    ("mysql", "data", ("soldier", "mancubus"), {"wall": "STARTAN3", "alcove": "COMPUTE3"}),
    ("postgres", "data", ("zombie", "knight"), {"wall": "GRAY7", "alcove": "PANBLUE"}),
    ("oracle", "data", ("chaingunner", "demon"), {"wall": "BSTONE2", "alcove": "PANRED", "lamp": "TLITE6_5"}),
    ("sql server", "data", ("soldier", "caco"), {"wall": "STONE4", "alcove": "COMPUTE1"}),
    ("google ads", "tech", ("imp", "knight"), {"wall": "STARGR1", "alcove": "COMPSTA2", "lamp": "TLITE6_6"}),
    ("youtube", "tech", ("zombie", "revenant"), {"wall": "BRICK7", "alcove": "PANRED", "lamp": "TLITE6_5"}),
]


def _app(name):
    """The APPS entry whose key is in the name, ignoring spaces (\"SQLServer1\" is SQL Server)."""
    flat = name.replace(" ", "")
    return next((a for a in APPS if a[0].replace(" ", "") in flat), None)


def room_themes(rooms):
    """Each (up to three) room's resolved theme: a FAMILIES entry with its app's overrides, plus
    `key` (the app, or the family) and `family`."""
    used_keys, used_pairs, out = set(), set(), []
    for room in rooms[:3]:
        name = f"{room.get('app', '')} {room.get('connection', '')}".lower()
        app = _app(name)
        if app and app[0] not in used_keys:
            key, family, pair, look = app
        else:
            family = next((f for f, th in FAMILIES.items() if f not in used_keys and any(w in name for w in th["match"])),
                          None) or next(f for f in FALLBACK if f not in used_keys)
            key, pair, look = family, FAMILIES[family]["monsters"], {}
        if pair in used_pairs:    # two rooms never share a pair: take the first free one
            pair = next((r, u) for u in URGENT for r in RECORDS if (r, u) not in used_pairs)
        used_keys.add(key)
        used_pairs.add(pair)
        out.append({**FAMILIES[family], **look, "monsters": pair, "key": key, "family": family})
    return out


def psdoom_monsters(mission):
    """PSDOOM_MONSTERS: six doomednums, a record's then an urgent record's for rooms 1-3.
    psdoom puts record pid in room pid % 3 and falls back to its stock pair for a missing room."""
    pairs = [t["monsters"] for t in room_themes(mission.get("rooms", []))]
    pairs += [FAMILIES["tech"]["monsters"]] * (3 - len(pairs))
    return " ".join(str(MONSTERS[k]["thing"]) for pair in pairs for k in pair)


if __name__ == "__main__":
    if sys.argv[1:2] != ["monsters"] or len(sys.argv) != 3:
        sys.exit(__doc__)
    print(psdoom_monsters(json.load(open(sys.argv[2]))))
