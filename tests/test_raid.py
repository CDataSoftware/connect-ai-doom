"""raid.sh end to end, in a scratch copy of the repo with fake psdoom, zdbsp, claude, security, op and open.

The fake psdoom calls the bridge exactly as the real one does (list, then kill and hit
a monster) and checks that the level files it was given exist.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import PAT, ROOT, USER, FakeMCP, fake_claude_env, free_port, tiny_iwad, wad_lumps

FAKE_PSDOOM = r'''#!/bin/bash
set -euo pipefail
printf '%s\n' "$@" > state/psdoom-args.txt
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
FAKE_SECURITY = r'''#!/bin/bash
# a one-item Keychain in $FAKE_LOG_DIR/keychain
echo "$@" >> "$FAKE_LOG_DIR/security.txt"
case $1 in
  find-generic-password) [[ -f $FAKE_LOG_DIR/keychain ]] && cat "$FAKE_LOG_DIR/keychain" || exit 44 ;;
  add-generic-password) while [[ $# -gt 0 ]]; do [[ $1 == -w ]] && printf '%s' "$2" > "$FAKE_LOG_DIR/keychain"; shift; done ;;
esac
'''
FAKE_OP = '#!/bin/bash\necho "$@" >> "$FAKE_LOG_DIR/op.txt"\nprintf "%s\\n" "$FAKE_OP_SECRET"\n'

SOURCES = ["raid.sh", "connect_doom.py", "connect_ai_mcp.py", "brief.py", "tools", "panel"]


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
                shutil.copytree(src, self.repo / name, ignore=shutil.ignore_patterns("__pycache__"))
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

    def raid(self, *args, env, panel=False):
        for name, script in (("open", FAKE_OPEN), ("security", FAKE_SECURITY), ("op", FAKE_OP)):
            self.install(f"bin/{name}", script, root=self.base)
        env = {**env, "PATH": f"{self.base / 'bin'}{os.pathsep}{env['PATH']}", "FAKE_LOG_DIR": str(self.logs),
               "FAKE_OP_SECRET": PAT, "POLL_SECONDS": "1"}
        env.pop("CONNECT_DOOM_STATE", None)              # raid.sh uses the repo's state/
        if panel:
            env["PANEL_PORT"] = str(free_port())
            env.pop("NO_PANEL", None)
        else:
            env["NO_PANEL"] = "1"
        return subprocess.run(["bash", str(self.repo / "raid.sh"), *args], capture_output=True, text=True,
                              env=env, timeout=120)

    def assert_played(self, r):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        args = (self.state / "psdoom-args.txt").read_text().split("\n")
        self.assertEqual(args[:6], ["-iwad", "wads/freedoom2.wad", "-file", "state/level.wad", "state/brand.wad",
                                    "-deh"])
        for flag in ("-nopslev", "-psallusers", "-window"):
            self.assertIn(flag, args)
        listed = (self.state / "psdoom-list.txt").read_text().splitlines()
        self.assertTrue(listed and all(line.startswith("connectai ") for line in listed))
        self.assertRegex((self.state / "psdoom-kill.txt").read_text().splitlines()[0], r", (FOLLOW-UP QUEUED|FLAGGED)$")
        self.assertEqual(len((self.state / "hits.jsonl").read_text().splitlines()), 1)
        self.assertIn("1 cleared", r.stdout)                   # the closing score
        self.assertIn("KILL", r.stderr)                         # psdoom's stderr is filtered to kills
        self.assertEqual((self.state / "game.log").read_text(), "game over\n")
        lumps = dict(wad_lumps((self.state / "level.wad").read_bytes()))
        self.assertIn("LINEDEFS", lumps)
        self.assertIn(b"CAIRM0", dict(wad_lumps((self.state / "brand.wad").read_bytes()))["TEXTURE1"])
        return len(listed)

    def test_demo(self):
        r = self.raid("--demo", env=dict(os.environ))
        self.assertEqual(self.assert_played(r), 36)
        self.assertFalse((self.logs / "security.txt").exists(), "demo never looks up a token")
        self.assertIn("CONNECT AI: PII DETECTED IN INBOUND QUERY", (self.state / "level.deh").read_text())

    def test_live_run_with_panel(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            r = self.raid("-q", "Who is late?", "-extra", env=env, panel=True)
            self.assertEqual(self.assert_played(r), 3)          # Salesforce 2 + Zendesk 1, re-queried live
        self.assertEqual(json.loads((self.base / "claude-call.json").read_text())["argv"][1], "Who is late?")
        self.assertEqual((self.state / "psdoom-args.txt").read_text().split("\n")[-2], "-extra")   # passed through
        panel = json.loads((self.state / "panel-state.json").read_text())
        self.assertEqual(panel["mission"]["question"], "Who is late?")
        self.assertEqual(panel["standing"], 2)                 # the fake psdoom already killed one
        self.assertEqual(len(panel["kills"]), 1)
        self.assertEqual(panel["events"][-1]["kind"], "mission")
        self.assertIn("http://localhost:", (self.logs / "open.txt").read_text())
        self.assertFalse((self.logs / "security.txt").exists(), "a PAT in the environment wins")
        cache = json.loads((self.state / "cache.json").read_text())
        self.assertEqual(cache["source"], "connect-ai-mcp")

    def test_again_replays_without_claude(self):
        self.raid("--demo", env=dict(os.environ))
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            (self.base / "claude-call.json").unlink(missing_ok=True)
            r = self.raid("--again", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.base / "claude-call.json").exists())

    def test_again_without_a_briefing(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            r = self.raid("--again", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("No briefing yet", r.stderr)

    def test_brief_only_and_token_from_1password_then_keychain(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url) as env:
            del env["CONNECT_AI_PAT"]
            env["CONNECT_AI_USER"] = USER
            r = self.raid("brief", "Which", "vendors?", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["answer"], json.loads(
                (self.state / "mission.json").read_text())["answer"])
            self.assertFalse((self.state / "level.wad").exists(), "brief stops before the level")
            self.assertEqual(json.loads((self.base / "claude-call.json").read_text())["argv"][1], "Which vendors?")
            self.assertEqual((self.logs / "keychain").read_text(), PAT)      # cached for next time
            self.assertIn("Connect AI Dev Edition PAT/credential", (self.logs / "op.txt").read_text())

            (self.logs / "op.txt").unlink()
            r = self.raid("brief", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((self.logs / "op.txt").exists(), "second run reads the Keychain only")
            self.assertIn(f"find-generic-password -s connect-doom -a {USER} -w",
                          (self.logs / "security.txt").read_text())

    def test_failed_briefing_stops_the_raid(self):
        with FakeMCP() as mcp, fake_claude_env(self.base, mcp.url, fail=True) as env:
            r = self.raid(env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.state / "psdoom-args.txt").exists())


if __name__ == "__main__":
    unittest.main()
