#!/usr/bin/env python3
"""Write a DeHackEd patch that turns Doom's keycard messages into Connect AI's.

  make_deh.py <mission.json> <out.deh>

The red door's message is the refusal Connect AI actually returned in this run.
"""
import json
import sys

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


def patch(mission):
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
    return out


if __name__ == "__main__":
    mission = json.load(open(sys.argv[1]))
    open(sys.argv[2], "w").write(patch(mission))
