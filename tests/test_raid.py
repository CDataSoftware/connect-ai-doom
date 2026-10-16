"""raid.sh end to end, in a scratch copy of the repo with fake psdoom, zdbsp, claude, security, op and open.

The fake psdoom calls the bridge exactly as the real one does (list, then kill and hit
a monster) and checks that the level files it was given exist.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from helpers import PAT, ROOT, USER, FakeMCP, fake_claude_env, free_port, tiny_iwad, wad_lumps

FAKE_PSDOOM = r'''#!/bin/bash
set -euo pipefail
printf '%s\n' "$@" > state/psdoom-args.txt
echo "${PSDOOM_AGENT_DIR#"$PWD/"} ${PSDOOM_AGENT_CLAUDE:-} ${PSDOOM_AGENT_LOOP:-} ${PSDOOM_OPT_GOD:-} ${PSDOOM_OPT_SAVED:-} ${PSDOOM_OPT_OFFLINE:-} ${PSDOOM_OPT_MUSIC:-} ${PSDOOM_OPT_SFX:-}" > state/psdoom-agent.txt
echo "${PSDOOM_FRAMELESS:-} ${SDL_VIDEO_WINDOW_POS:-}" > state/psdoom-env.txt
echo "${PSDOOM_STAGE_PID:-}" > state/psdoom-stage.txt
for f in state/level.wad state/brand.wad state/level.deh wads/freedoom2.wad; do [[ -s $f ]] || exit 9; done
$PSDOOMPSCMD > state/psdoom-list.txt
pid=$(head -1 state/psdoom-list.txt | cut -d' ' -f2)
$PSDOOMRENICECMD "$pid"
$PSDOOMKILLCMD "$pid" > state/psdoom-kill.txt
if [[ -n "${PANEL_PORT:-}" ]]; then
  python3 -c "import sys, urllib.request; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:$PANEL_PORT/state.json').read().decode())" > state/panel-state.json
fi
echo "game over"
'''
FAKE_ZDBSP = '#!/bin/bash\n[[ $1 == -o ]] && cp "$3" "$2"\n'
FAKE_OPEN = '#!/bin/bash\necho "$@" >> "$FAKE_LOG_DIR/open.txt"\n'
# The screen query answers like a 14" MacBook Pro: 1512x982 with a 32 px notch strip. Other calls
# (the stage) are logged, and stay up like the real stage until raid.sh kills it: raid.sh ends the
# game when the stage quits.
FAKE_OSASCRIPT = r'''#!/bin/bash
if [[ "$*" == *NSScreen* ]]; then echo "${FAKE_SCREEN:-0 0 1512 982 32}"; exit; fi
printf '%s\n' "$*" >> "$FAKE_LOG_DIR/osascript.txt"
exec /bin/sleep 120
'''
FAKE_SECURITY = r'''#!/bin/bash
# a one-item Keychain in $FAKE_LOG_DIR/keychain
echo "$@" >> "$FAKE_LOG_DIR/security.txt"
case $1 in
  find-generic-password) [[ -f $FAKE_LOG_DIR/keychain ]] && cat "$FAKE_LOG_DIR/keychain" || exit 44 ;;
  add-generic-password) while [[ $# -gt 0 ]]; do [[ $1 == -w ]] && printf '%s' "$2" > "$FAKE_LOG_DIR/keychain"; shift; done ;;
esac
'''
FAKE_OP = '#!/bin/bash\necho "$@" >> "$FAKE_LOG_DIR/op.txt"\nprintf "%s\\n" "$FAKE_OP_SECRET"\n'

SOURCES = ["raid.sh", "connect_doom.py", "connect_ai_mcp.py", "claude_run.py", "brief.py", "tools", "panel"]


class RaidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        for name in SOURCES:
            src = ROOT / name
            if src.is_dir():
                # only what git tracks: panel/sprites and panel/brand are generated (like in CI)
                shutil.copytree(src, self.repo / name,
                                ignore=shutil.ignore_patterns("__pycache__", "sprites", "brand"))
            else:
                shutil.copy2(src, self.repo / name)
        (self.repo / "wads").mkdir()
        tiny_iwad(self.repo / "wads/freedoom2.wad")
        self.install("vendor/src/psdoom-ng/trunk/src/psdoom", FAKE_PSDOOM)
        self.install("vendor/src/zdbsp/build/zdbsp", FAKE_ZDBSP)
        self.state = self.repo / "state"
        self.logs = self.base / "logs"
        self.logs.mkdir()

    def install(self, rel, script, root=None):
        path = (root or self.repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script)
        path.chmod(0o755)

    def raid(self, *args, env, panel=False, stdin=None):
        for name, script in (("open", FAKE_OPEN), ("security", FAKE_SECURITY), ("op", FAKE_OP),
                             ("osascript", FAKE_OSASCRIPT)):
            self.install(f"bin/{name}", script, root=self.base)
        env = {**env, "PATH": f"{self.base / 'bin'}{os.pathsep}{env['PATH']}", "FAKE_LOG_DIR": str(self.logs),
               "FAKE_OP_SECRET": PAT, "POLL_SECONDS": "1", "LAYOUT": "off", **env.get("_layout", {})}
        env.pop("_layout", None)
        env.pop("CONNECT_DOOM_STATE", None)              # raid.sh uses the repo's state/
        if panel:
            env["PANEL_PORT"] = str(free_port())
            env.pop("NO_PANEL", None)
        else:
            env["NO_PANEL"] = "1"
        return subprocess.run(["bash", str(self.repo / "raid.sh"), *args], capture_output=True, text=True,
                              env=env, timeout=120, input=stdin)

    def assert_played(self, r):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        args = (self.state / "psdoom-args.txt").read_text().split("\n")
        self.assertEqual(args[:6], ["-iwad", "wads/freedoom2.wad", "-file", "state/level.wad", "state/brand.wad",
                                    "-deh"])
        for flag in ("-nopslev", "-psallusers", "-window"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("-config") + 1], "state/controls.cfg")
        controls = (self.state / "controls.cfg").read_text()
        self.assertIn("key_up 17", controls)        # W
        self.assertIn("key_fire 57", controls)      # Space
        self.assertIn("key_use 18", controls)       # E
        listed = (self.state / "psdoom-list.txt").read_text().splitlines()
        self.assertTrue(listed and all(line.startswith("connectai ") for line in listed))
        self.assertRegex((self.state / "psdoom-kill.txt").read_text().splitlines()[0], r", \d+ LEFT$")
        self.assertEqual(len((self.state / "hits.jsonl").read_text().splitlines()), 1)
        self.assertIn("1 cleared", r.stdout)                   # the closing score
        self.assertIn("KILL", r.stderr)                         # psdoom's stderr is filtered to kills
        self.assertEqual((self.state / "game.log").read_text(), "game over\n")
        lumps = dict(wad_lumps((self.state / "level.wad").read_bytes()))
        self.assertIn("LINEDEFS", lumps)
        self.assertIn(b"CAIRM0", dict(wad_lumps((self.state / "brand.wad").read_bytes()))["TEXTURE1"])
        return len(listed)

    def test_demo(self):
        r = self.raid("offline", env=dict(os.environ))
        self.assertEqual(self.assert_played(r), 40)
        self.assertFalse((self.logs / "security.txt").exists(), "demo never looks up a token")
        self.assertIn("CONNECT AI: PII DETECTED IN INBOUND QUERY", (self.state / "level.deh").read_text())
        # the agent channel is always open, so Claude plays can be switched on from the Options menu
        self.assertEqual((self.state / "psdoom-agent.txt").read_text(), "state/agent 0 0 0 0 0 1 1\n")

    def test_claude_plays_on_a_loop(self):
        r = self.raid("offline", "--claude-plays", "--loop", env=dict(os.environ))
        self.assert_played(r)
        self.assertEqual((self.state / "psdoom-agent.txt").read_text(), "state/agent 1 1 0 0 0 1 1\n")

    def test_offline_plays(self):
        r = self.raid("offline", "--offline-plays", env=dict(os.environ))
        self.assert_played(r)
        self.assertEqual((self.state / "psdoom-agent.txt").read_text(), "state/agent 1 1 0 0 1 1 1\n")   # loops too

    def test_claude_plays_once_with_no_loop(self):
        """Claude playing turns auto replay on (a cleared level starts over); --no-loop plays it once."""
        r = self.raid("offline", "--claude-plays", "--no-loop", env=dict(os.environ))
        self.assert_played(r)
        self.assertEqual((self.state / "psdoom-agent.txt").read_text(), "state/agent 1 0 0 0 0 1 1\n")

    def test_god_mode_and_saved_data_flags(self):
        r = self.raid("offline", "--god-mode", "--saved-data", env=dict(os.environ))
        self.assert_played(r)
        self.assertEqual((self.state / "psdoom-agent.txt").read_text(), "state/agent 0 0 1 1 0 1 1\n")

    def test_music_and_sound_effects_flags(self):
        r = self.raid("offline", "--no-music", "--no-sound-effects", env=dict(os.environ))
        self.assert_played(r)
        self.assertEqual((self.state / "psdoom-agent.txt").read_text(), "state/agent 0 0 0 0 0 0 0\n")

    def test_a_booth_keeps_the_menus_switches(self):
        """Booth games each start psdoom again: the switches staff set in the Options menu
        (play.py saves them in state/options.json) carry over; a game outside a booth starts fresh."""
        options = self.state / "options.json"
        self.state.mkdir(exist_ok=True)
        options.write_text(json.dumps({"claude_plays": 1, "auto_replay": 0, "god_mode": 1, "saved_data": 0,
                                       "offline_claude": 1, "music": 0, "sound_effects": 1}))
        r = self.raid("offline", env={**os.environ, "CONNECT_DOOM_BOOTH": "1"})
        self.assert_played(r)
        self.assertEqual((self.state / "psdoom-agent.txt").read_text(), "state/agent 1 0 1 0 1 0 1\n")
        r = self.raid("offline", env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.state / "psdoom-agent.txt").read_text(), "state/agent 0 0 0 0 0 1 1\n")
        self.assertFalse(options.exists())

    def test_live_run_with_panel(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            r = self.raid("new", "-q", "Who is late?", "-extra", env=env, panel=True)
            self.assertEqual(self.assert_played(r), 3)          # Salesforce 2 + Zendesk 1, re-queried live
        self.assertEqual(json.loads((self.base / "claude-call.json").read_text())["argv"][1], "Who is late?")
        self.assertEqual((self.state / "psdoom-args.txt").read_text().split("\n")[-2], "-extra")   # passed through
        panel = json.loads((self.state / "panel-state.json").read_text())
        self.assertEqual(panel["mission"]["question"], "Who is late?")
        self.assertEqual(panel["standing"], 2)                 # the fake psdoom already killed one
        self.assertEqual(len(panel["kills"]), 1)
        self.assertEqual(panel["game"]["mode"], "player")      # the panel switches to the game view
        self.assertEqual(sum(r["killed"] for r in panel["rooms_live"]), 1)
        self.assertFalse((self.state / "game.json").exists(), "the game view ends with the game")
        self.assertEqual(panel["events"][-1]["kind"], "mission")
        self.assertIn("http://localhost:", (self.logs / "open.txt").read_text())
        self.assertFalse((self.logs / "security.txt").exists(), "a PAT in the environment wins")
        cache = json.loads((self.state / "cache.json").read_text())
        self.assertEqual(cache["source"], "connect-ai-mcp")

    def built_from_this_patch(self):
        """What setup.sh leaves: the patch, and the checksum of the one the game was built from."""
        (self.repo / "patches").mkdir()
        patch = shutil.copy2(ROOT / "patches/psdoom-ng-connect-doom.patch", self.repo / "patches")
        (self.repo / "vendor/src/psdoom-ng/.connect-doom-patch").write_text(
            hashlib.sha256(Path(patch).read_bytes()).hexdigest() + "\n")

    def test_full_screen_layout(self):
        self.built_from_this_patch()
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            r = self.raid(env={**env, "_layout": {"LAYOUT": "full"}}, panel=True)
        self.assert_played(r)
        args = (self.state / "psdoom-args.txt").read_text().split("\n")
        # 2/3 of a 1512 px screen is 1008; 960x720 is psdoom's biggest mode that fits it and the bars
        self.assertEqual(args[args.index("-geometry") + 1], "960x720")
        # centered in the stage, below the 32 px notch strip: borderless, at (24, 32 + (950 - 720) / 2)
        self.assertEqual((self.state / "psdoom-env.txt").read_text().split(), ["1", "24,147"])
        stage = (self.logs / "osascript.txt").read_text()
        self.assertIn("tools/stage.js", stage)
        self.assertRegex(stage, r"http://localhost:\d+/\?stage=24,147,960,720,1008,32")
        self.assertFalse((self.logs / "open.txt").exists(), "the panel is on the stage, not in a tab")
        self.assertRegex((self.state / "psdoom-stage.txt").read_text(), r"^\d+\n$")   # floats while the stage is in front
        self.assertIn("grabmouse 1", (self.state / "controls-extra.cfg").read_text())

    def test_full_screen_layout_on_a_second_display(self):
        self.built_from_this_patch()
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            # a 1920x1080 display to the right of the laptop and 200 px higher, no notch
            r = self.raid(env={**env, "_layout": {"LAYOUT": "full", "FAKE_SCREEN": "1512 -200 1920 1080 0"}},
                          panel=True)
        self.assert_played(r)
        args = (self.state / "psdoom-args.txt").read_text().split("\n")
        self.assertEqual(args[args.index("-geometry") + 1], "1280x960")
        self.assertEqual((self.state / "psdoom-env.txt").read_text().split(), ["1", "1512,-140"])
        self.assertRegex((self.logs / "osascript.txt").read_text(), r"\?stage=0,60,1280,960,1280,0")

    def test_an_older_build_is_refused(self):
        """The themed level needs the patched renderer limits: an old build would crash mid-game."""
        self.built_from_this_patch()
        (self.repo / "vendor/src/psdoom-ng/.connect-doom-patch").write_text("an older patch\n")
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            r = self.raid(env={**env, "_layout": {"LAYOUT": "full"}}, panel=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("built from an older patch: run ./setup.sh again first", r.stderr)
        self.assertFalse((self.state / "psdoom-env.txt").exists())

    def test_again_replays_without_claude(self):
        self.raid("--demo", env=dict(os.environ))
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            (self.base / "claude-call.json").unlink(missing_ok=True)
            r = self.raid("replay", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.base / "claude-call.json").exists())

    def test_again_without_a_briefing(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            r = self.raid("--again", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("No level yet: run ./raid.sh new first.", r.stderr)

    def test_brief_only_and_token_from_1password_then_keychain(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            del env["CONNECT_AI_PAT"]
            env["CONNECT_AI_USER"] = USER
            env["CONNECT_AI_OP_ITEM"] = "op://Private/Connect AI PAT"
            r = self.raid("brief-only", "Which", "vendors?", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["answer"], json.loads(
                (self.state / "mission.json").read_text())["answer"])
            self.assertFalse((self.state / "level.wad").exists(), "brief stops before the level")
            self.assertEqual(json.loads((self.base / "claude-call.json").read_text())["argv"][1], "Which vendors?")
            self.assertEqual((self.logs / "keychain").read_text(), PAT)      # cached for next time
            self.assertIn("Connect AI PAT/credential", (self.logs / "op.txt").read_text())

            (self.logs / "op.txt").unlink()
            r = self.raid("brief", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((self.logs / "op.txt").exists(), "second run reads the Keychain only")
            self.assertIn(f"find-generic-password -s connect-doom -a {USER} -w",
                          (self.logs / "security.txt").read_text())

    def test_login_then_play_with_the_saved_account(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            del env["CONNECT_AI_PAT"], env["CONNECT_AI_USER"]
            r = self.raid("login", env=env, stdin=f"{USER}\n{PAT}\n")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((self.state / "account").read_text().strip(), USER)
            self.assertEqual((self.logs / "keychain").read_text(), PAT)
            r = self.raid("replay", env=env)
        self.assertNotIn("No Connect AI token", r.stderr)
        self.assertIn(f"find-generic-password -s connect-doom -a {USER} -w", (self.logs / "security.txt").read_text())

    def test_no_token_says_how_to_log_in(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            del env["CONNECT_AI_PAT"]
            r = self.raid("--again", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("run ./raid.sh login", r.stderr)
        self.assertFalse((self.logs / "op.txt").exists(), "1Password only when CONNECT_AI_OP_ITEM names an item")

    def test_unbuilt_game_says_run_setup(self):
        (self.repo / "vendor/src/psdoom-ng/trunk/src/psdoom").unlink()
        r = self.raid("--demo", env=dict(os.environ))
        self.assertEqual(r.returncode, 1)
        self.assertIn("run ./setup.sh first", r.stderr)

    def test_failed_briefing_stops_the_raid(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url, fail=True) as env:
            r = self.raid(env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.state / "psdoom-args.txt").exists())

    def test_no_wifi_plays_the_saved_records(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            self.assertEqual(self.raid("new", env=env).returncode, 0)
            offline = {**env, "CONNECT_AI_MCP_URL": f"http://127.0.0.1:{free_port()}/mcp"}   # nothing listens
            (self.state / "hits.jsonl").unlink()      # assert_played counts one game's hits
            time.sleep(1.1)                           # and kills from the game's start, to the second
            r = self.raid("replay", env=offline)
        self.assertEqual(self.assert_played(r), 3)               # the same records as the live game
        self.assertIn("Can't reach Connect AI: playing the records saved", r.stderr)
        self.assertEqual(json.loads((self.state / "cache.json").read_text())["source"], "saved")

    def test_no_wifi_and_nothing_saved_plays_offline_then_keeps_the_level(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            self.assertEqual(self.raid("new", env=env).returncode, 0)
            (self.state / "saved-records.json").unlink()
            (self.state / "hits.jsonl").unlink()
            time.sleep(1.1)
            r = self.raid("replay", env={**env, "CONNECT_AI_MCP_URL": f"http://127.0.0.1:{free_port()}/mcp"})
        self.assertEqual(self.assert_played(r), 40)              # the offline sample level's records
        self.assertIn("playing offline instead", r.stderr)
        self.assertIn("query", json.loads((self.state / "mission.json").read_text())["rooms"][0])
        self.assertFalse((self.state / "mission-live.json").exists())

    def test_offline_keeps_the_real_level_for_the_next_replay(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            self.assertEqual(self.raid("new", "-q", "Who is late?", env=env).returncode, 0)
            (self.state / "hits.jsonl").unlink()
            time.sleep(1.1)  # a game counts kills from its start, to the second
            self.assertEqual(self.assert_played(self.raid("offline", env=env)), 40)
            self.assertEqual(json.loads((self.state / "mission.json").read_text())["question"], "Who is late?")
            (self.state / "hits.jsonl").unlink()
            time.sleep(1.1)
            self.assertEqual(self.assert_played(self.raid("replay", env=env)), 3)

    def test_booth_plays_game_after_game(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            self.raid("offline", env=env)
            time.sleep(1.1)
            r = self.raid("booth", env={**env, "BOOTH_GAMES": "2", "BOOTH_PAUSE": "1.1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("1 cleared"), 2)                    # two games, each from a full level
        self.assertEqual(r.stdout.count("Next game in 1.1 seconds"), 1)

    def test_booth_without_an_account_plays_offline(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CONNECT_AI_USER", "CONNECT_AI_PAT")}
        r = self.raid("booth", env={**env, "BOOTH_GAMES": "1"})       # no token and no level
        self.assertEqual(self.assert_played(r), 40)                    # the sample records
        self.assertIn("No Connect AI account on this Mac, so booth mode plays offline", r.stderr)

    def test_booth_with_an_account_but_no_level_plays_offline(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            r = self.raid("booth", env={**env, "BOOTH_GAMES": "1"})
        self.assertEqual(self.assert_played(r), 40)
        self.assertIn("No level on this Mac yet", r.stderr)

    def test_booth_offline_never_looks_up_a_token(self):
        r = self.raid("booth", "offline", env={**os.environ, "BOOTH_GAMES": "1"})
        self.assertEqual(self.assert_played(r), 40)
        self.assertFalse((self.logs / "security.txt").exists())

    def test_booth_stops_when_the_game_cant_start(self):
        (self.repo / "vendor/src/zdbsp/build/zdbsp").unlink()          # the level can't be built
        r = self.raid("booth", "offline", env={**os.environ, "BOOTH_PAUSE": "0"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("booth mode stopped", r.stderr)

    def test_doctor_says_ready(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            r = self.raid("doctor", env={**env, "PANEL_PORT": str(free_port())})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("✓ game, map builder and Freedoom art built", r.stdout)
        self.assertRegex(r.stdout, r"✓ Connect AI answers: \d+ connections")
        self.assertIn("no level yet", r.stdout)
        self.assertIn("Ready:", r.stdout)

    def test_doctor_times_each_rooms_query(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            self.assertEqual(self.raid("new", env=env).returncode, 0)
            r = self.raid("doctor", env={**env, "PANEL_PORT": str(free_port())})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"✓ Salesforce: 2 records in \d+\.\d s")
        self.assertIn("without Wi-Fi, games play this level's records saved", r.stdout)

    def test_doctor_without_a_token_is_ready_to_play_offline(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CONNECT_AI_USER", "CONNECT_AI_PAT")}
        r = self.raid("doctor", env={**env, "PANEL_PORT": str(free_port())})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("- no Connect AI token: ./raid.sh offline and ./raid.sh booth play sample data", r.stdout)
        self.assertIn("Ready to play offline", r.stdout)


if __name__ == "__main__":
    unittest.main()
