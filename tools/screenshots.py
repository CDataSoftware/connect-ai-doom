#!/usr/bin/env python3
"""Screenshot the game's screens headless, to check branding and HUD changes by eye.

Runs psdoom with no window (SDL_VIDEODRIVER=dummy) on the current state/ level and
brand WAD, drives it through the agent channel (see docs/internals.md) and saves:

  play.png   the lobby, as the game starts
  aim.png    the first room with a monster in sight, crosshair on it (the record readout)
  menu.png   Esc: the main menu
  options.png  Esc, O: the Options menu, with the Claude plays and Auto replay switches
  pause.png  P: the pause screen

Kills and hits go to no-op commands, so the kill log is untouched.
Usage: tools/screenshots.py <out dir> [--brand path/to/brand.wad]
"""
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import play  # noqa: E402  (the agent channel client)

PSDOOM = ROOT / "vendor/src/psdoom-ng/trunk/src/psdoom"
KEY_ESCAPE, KEY_ENTER, KEY_DOWN = 27, 13, 0xaf   # doomkeys.h


def main(out, brand):
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        agent = Path(tmp)
        play.AGENT = agent
        env = {**os.environ, "SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy",
               "PSDOOM_AGENT_DIR": str(agent), "PSDOOM_AGENT_CLAUDE": "1",
               "PSDOOM_LABELS": str(ROOT / "state/labels.tsv"),
               "PSDOOMPSCMD": f"{sys.executable} {ROOT / 'connect_doom.py'} list",
               "PSDOOMKILLCMD": "true", "PSDOOMRENICECMD": "true"}
        game = subprocess.Popen(
            [str(PSDOOM), "-iwad", str(ROOT / "wads/freedoom2.wad"), "-file", str(ROOT / "state/level.wad"),
             str(brand), "-deh", str(ROOT / "state/level.deh"), "-nopslev", "-psallusers", "-warp", "1",
             "-skill", "3", "-nosound", "-nomusic"], cwd=tmp, env=env,  # SHOT saves into cwd
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            g = play.Game()
            g.state()

            def shot(name):
                g.do("SHOT")
                pcx = sorted(Path(tmp).glob("DOOM*.pcx"))[-1]
                Image.open(pcx).resize((960, 600), Image.NEAREST).save(out / name)
                pcx.unlink()
                print(out / name)

            time.sleep(1)
            shot("play.png")
            play.tool_go_to(g, {"place": "keycards"})
            for room in ("room 1", "room 2", "room 3"):   # the first room with a monster in sight
                play.tool_go_to(g, {"place": room})
                s = g.state()
                p = s["player"]
                seen = sorted((m for m in s["monsters"] if m["visible"] and m["health"] > 0),
                              key=lambda m: m["distance"])
                if seen:
                    break
            if seen:
                g.do("TURN", math.degrees(math.atan2(seen[0]["y"] - p["y"], seen[0]["x"] - p["x"])) % 360)
            shot("aim.png")
            g.do("KEY", KEY_ESCAPE)
            time.sleep(0.5)
            shot("menu.png")
            g.do("KEY", KEY_DOWN)       # Options
            g.do("KEY", KEY_ENTER)
            time.sleep(0.5)
            shot("options.png")
            g.do("KEY", KEY_ESCAPE)     # closes every menu level at once
            g.do("KEY", ord("p"))
            time.sleep(0.5)
            shot("pause.png")
        finally:
            game.kill()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    brand = ROOT / "state/brand.wad"
    if "--brand" in args:
        brand = Path(args[args.index("--brand") + 1]).resolve()
    main(Path(args[0]).resolve(), brand)
