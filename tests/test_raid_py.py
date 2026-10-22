"""raid.py, the Windows launcher: its switches, the booth's carried-over options, the build stamp,
and (on Windows) the token in Credential Manager. The game itself is exercised by playing it."""
import json
import os
import subprocess
import sys
import unittest
import uuid
from unittest import mock

from helpers import ROOT, TempState

import compat
import raid


class ParseTest(unittest.TestCase):
    def test_defaults_are_a_new_level_with_auto_replay(self):
        o, extra = raid.parse([])
        self.assertEqual((o["mode"], o["loop"], o["plays"], extra), ("live", True, False, []))

    def test_modes_and_switches(self):
        o, extra = raid.parse(["replay", "--claude-plays", "--no-loop", "--god-mode", "-warp", "2"])
        self.assertEqual((o["mode"], o["plays"], o["loop"], o["god"]), ("again", True, False, True))
        self.assertEqual(extra, ["-warp", "2"])                     # the rest go to the game
        o, _ = raid.parse(["offline", "--offline-plays"])
        self.assertEqual((o["mode"], o["plays"], o["offline"]), ("demo", True, True))
        o, _ = raid.parse(["new", "-q", "Who owes us?"])
        self.assertEqual((o["mode"], o["question"]), ("live", "Who owes us?"))
        o, _ = raid.parse(["brief-only", "Who", "owes", "us?"])
        self.assertEqual((o["mode"], o["question"]), ("brief", "Who owes us?"))

    def test_booth_replays_and_never_briefs(self):
        self.assertEqual(raid.parse(["booth"])[0]["mode"], "again")
        self.assertEqual(raid.parse(["booth", "offline"])[0]["mode"], "demo")

    def test_a_mistyped_mode_stops_instead_of_briefing(self):
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"), self.assertRaises(SystemExit) as e:
            raid.parse(["replya"])
        self.assertEqual(e.exception.code, 2)

    def test_booth_flags_round_trip(self):
        argv = ["replay", "--claude-plays", "--no-loop", "--god-mode", "--saved-data", "--no-music",
                "--no-sound-effects", "--offline-plays"]
        o, _ = raid.parse(argv)
        again, _ = raid.parse(["replay", *raid.switch_flags(o)])
        self.assertEqual(again, o)


class OptionsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = TempState()
        state = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__)
        for name in ("OPTIONS",):
            p = mock.patch.object(raid, name, state / "options.json")
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)

    def test_flags_become_the_games_switches(self):
        o, _ = raid.parse(["--god-mode", "--no-music"])
        raid.load_options(o)
        self.assertEqual((os.environ["PSDOOM_OPT_GOD"], os.environ["PSDOOM_OPT_MUSIC"], os.environ["PSDOOM_OPT_SFX"]),
                         ("1", "0", "1"))

    def test_a_booth_keeps_the_last_games_switches(self):
        raid.OPTIONS.write_text(json.dumps({"claude_plays": 1, "auto_replay": 0, "god_mode": 1, "saved_data": 0,
                                            "offline_claude": 1, "music": 0, "sound_effects": 1}))
        o, _ = raid.parse([])
        raid.load_options(o)
        self.assertEqual((o["plays"], o["loop"], o["god"], o["offline"], o["nomusic"], o["nosfx"]),
                         (True, False, True, True, True, False))
        self.assertEqual(os.environ["PSDOOM_OPT_OFFLINE"], "1")


class BuildTest(unittest.TestCase):
    def test_the_build_checksum_is_setup_win_sh_s(self):
        """raid.py and tools/setup-win.sh hash the same bytes: cat file... | sha256sum."""
        import hashlib
        data = b"".join((ROOT / p).read_bytes() for p in raid.BUILD_INPUTS)
        self.assertEqual(raid.build_sum(), hashlib.sha256(data).hexdigest())
        for p in raid.BUILD_INPUTS:
            self.assertNotIn(b"\r\n", (ROOT / p).read_bytes(), f"{p} has CRLF endings (see .gitattributes)")


@unittest.skipUnless(compat.WINDOWS, "Credential Manager is Windows'")
class CredentialTest(unittest.TestCase):
    def test_a_token_round_trips(self):
        user = f"raid-py-test-{uuid.uuid4().hex[:8]}@example.com"
        try:
            self.assertIsNone(raid.credential_read(user))
            raid.credential_write(user, "pat-π-123")
            self.assertEqual(raid.credential_read(user), "pat-π-123")
        finally:
            import ctypes
            ctypes.WinDLL("advapi32").CredDeleteW(f"{raid.CREDENTIAL}:{user}", 1, 0)


class CliTest(unittest.TestCase):
    @unittest.skipIf(compat.WINDOWS, "on Windows raid.py is the launcher")
    def test_elsewhere_it_points_at_raid_sh(self):
        r = subprocess.run([sys.executable, str(ROOT / "raid.py"), "offline"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("./raid.sh", r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(ROOT / "raid.py"), "--help"], capture_output=True, text=True,
                           encoding="utf-8")
        if compat.WINDOWS:
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("python raid.py booth", r.stdout)


if __name__ == "__main__":
    unittest.main()
