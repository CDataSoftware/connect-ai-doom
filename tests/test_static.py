"""Checks for the parts that only run on a real machine: setup.sh, raid.sh, the psdoom patch, .gitignore."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from helpers import ROOT

SCRIPTS = ["raid.sh", "setup.sh"]


def psdoom_checkout():
    """vendor/src/psdoom-ng from this checkout, or the main checkout when running in a git worktree."""
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    for root in (ROOT, Path(common).parent if common else ROOT):
        if (root / "vendor/src/psdoom-ng/.git").exists():
            return root / "vendor/src/psdoom-ng"
    return None


class ShellTest(unittest.TestCase):
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
        for script in SCRIPTS:
            text = (ROOT / script).read_text()
            self.assertTrue(text.startswith("#!/bin/bash\n"), script)
            self.assertIn("set -euo pipefail", text, script)
            self.assertTrue((ROOT / script).stat().st_mode & 0o111, f"{script} is not executable")

    def test_raid_usage_matches_readme(self):
        """Every raid.sh mode the README shows is one raid.sh parses."""
        raid = (ROOT / "raid.sh").read_text()
        for flag in set(re.findall(r"\./raid\.sh (--?[a-z-]+|brief)", (ROOT / "README.md").read_text())):
            self.assertRegex(raid, rf"\n\s+(\S+\|)?{re.escape(flag)}(\|\S+)?\)", flag)

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

    def test_patch_is_well_formed(self):
        r = subprocess.run(["git", "apply", "--numstat", str(self.PATCH)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        files = [line.split("\t")[2] for line in r.stdout.splitlines()]
        self.assertTrue(files and all(f.startswith("trunk/src/") for f in files), files)

    def test_patch_matches_the_built_psdoom(self):
        src = psdoom_checkout()
        if not src:
            self.skipTest("psdoom-ng is not checked out (run ./setup.sh)")
        r = subprocess.run(["git", "apply", "--check", "--reverse", str(self.PATCH)], cwd=src,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"vendor/src/psdoom-ng doesn't carry the committed patch:\n{r.stderr}")


class RepoTest(unittest.TestCase):
    def test_secrets_and_build_output_stay_out_of_git(self):
        ignored = (ROOT / ".gitignore").read_text().split()
        for path in ("state/", "vendor/", "wads/", "brand/", "__pycache__/"):
            self.assertIn(path, ignored)


if __name__ == "__main__":
    unittest.main()
