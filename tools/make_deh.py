#!/usr/bin/env python3
"""Write a DeHackEd patch that turns Doom's keycard messages into Connect AI's.

  make_deh.py <mission.json> <out.deh> [--no-gore]

The red door's message is the refusal Connect AI actually returned in this run. Every record
monster (tools/themes.py) gets about half Doom's hit points, the big ones 100. With --no-gore
(raid.sh passes it for NOGORE=1), hits throw bullet puffs instead of blood, and a killed record
vanishes in green teleport fog instead of dying on the floor.
"""
import json
import sys

from themes import MONSTERS

# Original strings from d_englsh.h; DeHackEd matches them exactly.
PD_BLUEK = "You need a blue key to open this door"
PD_YELLOWK = "You need a yellow key to open this door"
PD_REDK = "You need a red key to open this door"
GOTBLUECARD = "Picked up a blue keycard."
GOTYELWCARD = "Picked up a yellow keycard."


def refusal(mission):
    if not mission.get("denied"):
        return "NO ACCESS"
    error = mission["denied"][0]["error"].splitlines()[0]
    return "CONNECT AI: " + error.removeprefix("Request blocked: ").upper()


# No gore. Frame and thing numbers are vanilla Doom's, which psdoom-ng keeps. The death frames keep
# their code pointers (A_Scream, A_Fall), so a kill still stops the monster being solid and psdoom
# still sees it; only the sprites change. Every monster a room can have (tools/themes.py) fogs.
SPR_PUFF, SPR_TFOG, BRIGHT = 17, 26, 32768
BLOOD_FRAMES = range(90, 93)        # S_BLOOD1..3
FOG_LAST = 9                        # TFOG's faintest frame, left where the corpse would lie


def no_gore():
    out = ""
    # Blood: P_SpawnBlood jumps straight to S_BLOOD2 or S_BLOOD3 for most hits, skipping the
    # thing's initial frame, so the three frames themselves become puff frames B..D.
    for i, n in enumerate(BLOOD_FRAMES):
        out += f"Frame {n}\nSprite number = {SPR_PUFF}\nSprite subnumber = {i + 1}\n\n"
    for monster in MONSTERS.values():
        first, last = monster["die"]
        frames = range(first, last + 1)
        steps = len(frames) - 1
        for i, n in enumerate(frames[:-1]):
            # spread the death over the fog's frames A..I (J ends it): a long death (a hell knight's
            # seven frames, a mancubus's ten) must not step past J, or the game quits mid-kill
            fog = round(i * (FOG_LAST - 1) / max(steps - 1, 1))
            out += f"Frame {n}\nSprite number = {SPR_TFOG}\nSprite subnumber = {BRIGHT + fog}\nDuration = 4\n\n"
        out += f"Frame {frames[-1]}\nSprite number = {SPR_TFOG}\nSprite subnumber = {BRIGHT + FOG_LAST}\nDuration = -1\n\n"
        if "xdie" in monster:   # its gib death fogs too
            out += f"Thing {monster['deh']} ({monster['name']})\nExploding frame = {first}\n\n"
    out += "Thing 39 (Blood)\nInitial frame = 93\n\n"                           # S_PUFF1
    return out


def patch(mission, gore=True):
    swaps = [
        (PD_BLUEK, "READ ACCESS REQUIRED. GRAB THE BLUE KEY"),
        (PD_YELLOWK, "WRITE ACCESS REQUIRED. GRAB THE YELLOW KEY"),
        (PD_REDK, refusal(mission)),
        (GOTBLUECARD, "READ ACCESS GRANTED BY CONNECT AI"),
        (GOTYELWCARD, "WRITE ACCESS GRANTED BY CONNECT AI"),
    ]
    # Chocolate Doom caps replacements at the original's length unless the patch says otherwise.
    out = "Patch File for DeHackEd v3.0\n# *allow-long-strings*\nDoom version = 19\nPatch format = 6\n\n"
    for old, new in swaps:
        out += f"Text {len(old)} {len(new)}\n{old}{new}\n\n"
    for m in MONSTERS.values():     # easier than Doom's, so a visitor with a pistol clears a room
        if "health" in m:
            out += f"Thing {m['deh']} ({m['name']})\nHit points = {m['health']}\n\n"
    if not gore:
        out += no_gore()
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--no-gore"]
    mission = json.load(open(args[0]))
    open(args[1], "w").write(patch(mission, gore="--no-gore" not in sys.argv[1:]))
