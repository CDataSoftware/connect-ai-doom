"""Checks for the parts that only run on a real machine: setup.sh, raid.sh, their Windows
counterparts (setup.ps1, tools/setup-win.sh, raid.py), the psdoom patches, .gitignore."""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from helpers import ROOT

import raid
import themes

SCRIPTS = ["raid.sh", "setup.sh"]
WINDOWS = os.name == "nt"


def psdoom_checkout():
    """vendor/src/psdoom-ng from this checkout, or the main checkout when running in a git worktree."""
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    for root in (ROOT, Path(common).parent if common else ROOT):
        if (root / "vendor/src/psdoom-ng/.git").exists():
            return root / "vendor/src/psdoom-ng"
    return None


class ShellTest(unittest.TestCase):
    @unittest.skipIf(WINDOWS, "raid.sh and setup.sh are the Mac's; bash here may be WSL's")
    def test_syntax(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                r = subprocess.run(["bash", "-n", str(ROOT / script)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck(self):
        r = subprocess.run(["shellcheck", "-S", "warning", *SCRIPTS], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_strict_mode_and_executable(self):
        for script in [*SCRIPTS, "tools/setup-win.sh"]:
            text = (ROOT / script).read_text()
            self.assertTrue(text.startswith("#!/bin/bash\n"), script)
            self.assertIn("set -euo pipefail", text, script)
            if not WINDOWS:   # Windows has no executable bit
                self.assertTrue((ROOT / script).stat().st_mode & 0o111, f"{script} is not executable")

    def test_windows_setup_builds_what_raid_py_runs(self):
        setup = (ROOT / "tools/setup-win.sh").read_text()
        self.assertEqual(raid.PSDOOM.relative_to(ROOT).as_posix(), "vendor/bin/psdoom.exe")
        self.assertEqual(raid.ZDBSP.relative_to(ROOT).as_posix(), "vendor/bin/zdbsp.exe")
        self.assertIn('collect "$SRC/psdoom-ng/trunk/src/psdoom.exe"', setup)
        self.assertIn('collect "$SRC/zdbsp/build/zdbsp.exe"', setup)
        # the build checksum: the same files, in the same order, on both sides
        self.assertIn("cat " + " ".join(raid.BUILD_INPUTS) + " | sha256sum", setup)
        self.assertIn("tools/setup-win.sh", (ROOT / "setup.ps1").read_text())
        self.assertIn("setup.ps1", (ROOT / "setup.cmd").read_text())
        for tool in re.findall(r'"(tools/\S+\.py)"', (ROOT / "raid.py").read_text()):
            self.assertTrue((ROOT / tool).exists(), tool)

    def test_windows_files_keep_their_line_endings(self):
        """MSYS2's git apply and bash need LF; cmd.exe needs CRLF."""
        attrs = (ROOT / ".gitattributes").read_text()
        for rule in ("*.patch text eol=lf", "*.sh text eol=lf", "*.cmd text eol=crlf"):
            self.assertIn(rule, attrs)

    def test_raid_usage_matches_readme(self):
        """Every raid.sh mode and flag the README and CONTRIBUTING show is one raid.sh parses."""
        raid = (ROOT / "raid.sh").read_text()
        docs = (ROOT / "README.md").read_text() + (ROOT / "CONTRIBUTING.md").read_text()
        words = re.findall(r"\./raid\.sh ((?:--?)?[a-z][a-z-]*)", docs) + re.findall(r"(--claude-plays)", docs)
        self.assertIn("new", words)
        for word in set(words):
            self.assertRegex(raid, rf"\n\s+(\S+\|)?{re.escape(word)}(\|\S+)?\)", word)

    def test_raid_py_usage_matches_readme(self):
        """Every python raid.py mode and flag the README and CONTRIBUTING show is one raid.py parses."""
        docs = (ROOT / "README.md").read_text(encoding="utf-8") + (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        words = set(re.findall(r"python raid\.py ((?:--?)?[a-z][a-z-]*)", docs))
        self.assertTrue({"offline", "login", "new"} <= words, words)
        for word in words - {"login"}:   # login prompts, so it's checked by name only
            with self.subTest(word=word):
                raid.parse([word])        # an unknown word exits

    def test_open_source_files(self):
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text())
        for path in ("CONTRIBUTING.md", ".github/pull_request_template.md", ".github/ISSUE_TEMPLATE/bug_report.md"):
            self.assertTrue((ROOT / path).exists(), path)

    def test_setup_builds_what_raid_runs(self):
        setup, raid = (ROOT / "setup.sh").read_text(), (ROOT / "raid.sh").read_text()
        self.assertIn("PSDOOM=vendor/src/psdoom-ng/trunk/src/psdoom", raid)
        self.assertIn("ZDBSP=vendor/src/zdbsp/build/zdbsp", raid)
        self.assertIn('-x "$SRC/zdbsp/build/zdbsp"', setup)
        self.assertIn("wads/freedoom2.wad", setup)
        self.assertIn("patches/psdoom-ng-connect-doom.patch", setup)
        for tool in re.findall(r"python3 (tools/\S+\.py)", raid):
            self.assertTrue((ROOT / tool).exists(), tool)


class PatchTest(unittest.TestCase):
    PATCH = ROOT / "patches/psdoom-ng-connect-doom.patch"
    WINDOWS_PATCH = ROOT / "patches/psdoom-ng-windows.patch"

    def test_patch_is_well_formed(self):
        for patch in (self.PATCH, self.WINDOWS_PATCH):
            with self.subTest(patch=patch.name):
                r = subprocess.run(["git", "apply", "--numstat", str(patch)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                files = [line.split("\t")[2] for line in r.stdout.splitlines()]
                self.assertTrue(files and all(f.startswith("trunk/src/") for f in files), files)

    def test_patch_matches_the_built_psdoom(self):
        src = psdoom_checkout()
        if not src:
            self.skipTest("psdoom-ng is not checked out (run ./setup.sh, or setup.cmd on Windows)")
        if WINDOWS:
            # Both patches are on the checkout, so neither reverses alone: the build's stamp says
            # which patches and recipe it was made from.
            self.assertTrue(raid.build_current(), "vendor/src/psdoom-ng was built from other patches: run setup.cmd")
            return
        r = subprocess.run(["git", "apply", "--check", "--reverse", str(self.PATCH)], cwd=src,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"vendor/src/psdoom-ng doesn't carry the committed patch:\n{r.stderr}")

    def test_monster_numbers_match_the_game(self):
        """tools/themes.py hardcodes each monster's doomednum, DeHackEd number and death frames."""
        src = psdoom_checkout()
        if not src:
            self.skipTest("psdoom-ng is not checked out (run ./setup.sh)")
        info = (src / "trunk/src/info.c").read_text().splitlines()
        frames = [m.group(1) for line in info if line.strip().startswith("{SPR")
                  and (m := re.search(r"//\s*(S_\w+)\s*$", line))]
        enum = re.search(r"typedef enum\s*{\s*(MT_PLAYER.*?)NUMMOBJTYPES",
                         (src / "trunk/src/info.h").read_text(), re.S).group(1)
        mobjs = re.findall(r"MT_\w+", enum)
        # mobjinfo entries, in mobjtype order: each one's doomednum is its first field
        nums = [int(n) for n in re.findall(r"{\s*// MT_\w+\s*\n\s*(-?\d+),", "\n".join(info))]
        for key, m in themes.MONSTERS.items():
            with self.subTest(monster=key):
                spr = m["sprite"][:4]
                for kind in ("die", "xdie"):
                    if kind in m:
                        first, last = m[kind]
                        self.assertEqual(frames[first], f"S_{spr}_{kind.upper()}1")
                        self.assertRegex(frames[last], rf"S_{spr}_{kind.upper()}\d+")
                        self.assertNotRegex(frames[last + 1], rf"S_{spr}_{kind.upper()}\d+")
                self.assertEqual(nums[m["deh"] - 1], m["thing"], mobjs[m["deh"] - 1])


class RepoTest(unittest.TestCase):
    def test_brand_assets_are_in_the_repo(self):
        """Self-contained: the logos the game and panel use come from assets/brand, not a local kit."""
        for name in ("connect-ai.png", "connect-ai.svg", "cdata.svg", "cdata.png"):
            self.assertTrue((ROOT / "assets/brand" / name).stat().st_size > 500, name)
        for script in ("raid.sh", "setup.sh"):
            self.assertNotIn("BRANDKIT", (ROOT / script).read_text(), script)

    def test_panel_needs_no_network(self):
        """A booth without Wi-Fi looks the same: the panel loads its fonts and scripts from panel/."""
        html = (ROOT / "panel/index.html").read_text()
        self.assertEqual(re.findall(r'<(?:link|script|img)[^>]+(?:href|src)="https?://[^"]+"', html), [])
        css = (ROOT / "panel/fonts/fonts.css").read_text()
        self.assertNotIn("http", css.split("*/", 1)[1])
        for font in re.findall(r"url\(([^)]+)\)", css):
            self.assertGreater((ROOT / "panel/fonts" / font).stat().st_size, 2000, font)
        for family in ("DM Sans", "DM Mono", "Press Start 2P", "VT323"):
            self.assertIn(f"font-family: '{family}'", css)
        self.assertEqual(len(list((ROOT / "panel/fonts").glob("OFL-*.txt"))), 4)

    def test_secrets_and_build_output_stay_out_of_git(self):
        ignored = (ROOT / ".gitignore").read_text().split()
        for path in ("state/", "vendor/", "wads/", "/brand/", "__pycache__/"):
            self.assertIn(path, ignored)


if __name__ == "__main__":
    unittest.main()
