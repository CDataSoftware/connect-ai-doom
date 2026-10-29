"""web/levelkit.py: a browser visitor's level, built by the Mac version's own scripts from the answers
the page got from Connect AI. Each run is its own process: levelkit patches connect_ai_mcp and the
thread pool for Pyodide, which the other tests mustn't see."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT

ROOMS = [("Salesforce1", "Salesforce", "Deals slipping"), ("Zendesk1", "Zendesk", "Angry tickets"),
         ("Stripe1", "Stripe", "Unpaid invoices")]
PLAN = {"answer": "Acme is at risk.", "rooms": [
    {"connection": c, "app": app, "title": title, "query": f"SELECT [Id], [Name] AS [Label] FROM [{c}].[dbo].[t]"}
    for c, app, title in ROOMS]}
CONNECTIONS = [{"name": "Salesforce1", "permissions": ["Select", "Insert", "Update"]},
               {"name": "Zendesk1", "permissions": ["Select"]}, {"name": "Stripe1", "permissions": ["Select"]}]
# the model's Connect AI calls: one Data Security refused (the level's red door)
CALLS = [{"server": "connectai", "tool": "queryData", "args": {"query": "SELECT [SSN] FROM [PII1].[dbo].[people]"},
          "error": True, "text": "Error: Request blocked by Data Security rule PII"}]
ROWS = "Id,Label,Detail,Hot\r\n1,Acme Corp,\"$45,000, 12 days overdue\",1\r\n2,Globex,$9,000,0\r\n\nQuery ID: q1"

# what ai.js does, in Pyodide: plan(), then build() with the rooms' answers
RUN = """
import json, sys
import levelkit
args = json.loads(sys.argv[1])
mission = json.loads(levelkit.plan(args["question"], json.dumps(args["plan"]), json.dumps(args["calls"]),
                                   json.dumps(args["connections"])))
try:
    count = levelkit.build(json.dumps(args["answers"]))
except RuntimeError as e:
    count = str(e)
print(json.dumps({"mission": mission, "count": count}))
"""


@unittest.skipUnless((ROOT / "wads/freedoom2.wad").exists(), "needs ./setup.sh's Freedoom")
class LevelkitTest(unittest.TestCase):
    def build(self, answers):
        """Lay the kit out as web/build.sh does (links to the real files), run plan() and build(),
        and return their result and the level's folder."""
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        kit, level = tmp / "kit", tmp / "level"
        kit.mkdir()
        level.mkdir()
        for name in ("brief.py", "claude_run.py", "connect_ai_mcp.py", "connect_doom.py", "leaderboard.py", "tools", "assets"):
            (kit / name).symlink_to(ROOT / name)
        (kit / "levelkit.py").symlink_to(ROOT / "web/levelkit.py")
        (kit / "freedoom2.wad").symlink_to(ROOT / "wads/freedoom2.wad")
        args = {"question": "Who is at risk?", "plan": PLAN, "calls": CALLS, "connections": CONNECTIONS, "answers": answers}
        env = {**os.environ, "LEVELKIT_KIT": str(kit), "LEVELKIT_LEVEL": str(level)}
        out = subprocess.run([sys.executable, "-c", RUN, json.dumps(args)], cwd=kit, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.splitlines()[-1]), level

    def test_builds_the_level_from_the_answers(self):
        answers = [{"tool": "queryData", "args": {"query": r["query"]}, "text": ROWS, "error": False} for r in PLAN["rooms"]]
        answers[2] = {**answers[2], "text": "Error: Stripe1 timed out", "error": True}   # one room didn't answer
        result, level = self.build(answers)
        mission = result["mission"]
        self.assertEqual([r["writable"] for r in mission["rooms"]], [True, False, False])   # the doors: Connect AI's permissions
        self.assertEqual([d["connection"] for d in mission["denied"]], ["PII1"])           # the red door: the refused call
        self.assertEqual(result["count"], 4)                                                # two rooms' records
        for name in ("brand.wad", "level-raw.wad", "level.deh", "labels.tsv", "monsters.txt", "state.json", "run.jsonl"):
            self.assertTrue((level / name).stat().st_size > 0, name)
        cache = json.loads((level / "cache.json").read_text())
        self.assertEqual(cache["source"], "snapshot")
        self.assertEqual({m["label"] for m in cache["monsters"]}, {"Acme Corp", "Globex"})
        state = json.loads((level / "state.json").read_text())
        self.assertEqual([r["left"] for r in state["rooms_live"]], [2, 2, 0])
        self.assertTrue(any(e["kind"] == "mission" for e in state["events"]))              # the briefing tab's log

    def test_no_records_says_why(self):
        answers = [{"tool": "queryData", "args": {"query": r["query"]}, "text": "Error: no access", "error": True}
                   for r in PLAN["rooms"]]
        result, _ = self.build(answers)
        self.assertIn("Connect AI returned no records", result["count"])
        self.assertIn("Salesforce1: no access", result["count"])


if __name__ == "__main__":
    unittest.main()
