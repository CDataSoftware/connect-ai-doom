"""panel/index.html: the side panel's script, rendered in Node from a real brief.py run's state."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, FakeMCP, fake_claude_env

HARNESS = Path(__file__).with_name("panel_harness.js")
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class PanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        with FakeMCP() as mcp, fake_claude_env(tmp, mcp.url) as env:
            subprocess.run([sys.executable, str(ROOT / "brief.py"), "Who is <at risk>?"], env=env, check=True,
                           capture_output=True, timeout=60)
        state = tmp / "state"
        cls.events = [json.loads(line) for line in (state / "run.jsonl").read_text().splitlines()]
        mission = json.loads((state / "mission.json").read_text())
        cls.mission = {k: mission[k] for k in ("question", "answer", "rooms", "denied", "masked")}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, expr=None, **state):
        state = {"events": self.events, "play_events": [], "mission": self.mission, "kills": [], "standing": 0,
                 **state}
        path = Path(self.tmp.name) / "panel-state.json"
        path.write_text(json.dumps(state))
        r = subprocess.run([NODE, str(HARNESS), str(ROOT / "panel/index.html"), str(path), *([expr] if expr else [])],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        return (out["ids"], out.get("value")) if expr else out["ids"]

    def test_statusbar_and_mission(self):
        kills = [{"app": "Salesforce", "label": "Acme <b>Corp</b>", "detail": "$1", "writable": True},
                 {"app": "Zendesk", "label": "Initech", "detail": "3", "writable": False}]
        ids = self.render(kills=kills, standing=5)
        self.assertEqual(ids["ask"]["text"], "Who is <at risk>?")
        self.assertEqual(ids["s-apps"]["text"], 2)                       # rooms in the mission
        self.assertEqual(ids["s-records"]["text"], 3)                    # 2 Salesforce rows + 1 masked row
        self.assertEqual(ids["s-blocked"]["text"], 2)                    # the PII block + permission denied
        self.assertEqual(ids["s-kills"]["text"], 2)
        self.assertEqual(ids["s-model"]["text"], "SONNET-5-5")
        self.assertIn("Acme is at risk", ids["answer"]["html"])
        self.assertIn("7 turns, 61 s, $0.12", ids["answer"]["html"])
        rooms = ids["rooms"]["html"]
        self.assertIn("YELLOW KEY: READ + WRITE", rooms)
        self.assertIn("BLUE KEY: READ ONLY", rooms)
        self.assertEqual(rooms.count("RED DOOR: NO KEY EXISTS"), 1)
        self.assertIn("Request blocked: PII detected in inbound query", rooms)
        self.assertEqual(ids["standing"]["text"], "5 still standing, latest first")
        self.assertLess(ids["kills"]["html"].index("Initech"), ids["kills"]["html"].index("Acme"))  # newest first
        self.assertIn("Acme &lt;b&gt;Corp&lt;/b&gt;", ids["kills"]["html"])                         # escaped
        self.assertIn("FOLLOW-UP QUEUED (DRY RUN)", ids["kills"]["html"])
        self.assertTrue(ids["play"]["hidden"])

    def test_feed(self):
        feed = self.render()["feed"]["children"][::-1]                   # newest first on the page
        self.assertEqual(len(feed), 6)                                   # 1 thought + 5 calls, no duplicates
        self.assertEqual([c["className"].split() for c in feed][0], ["item", "thought"])
        classes = [c["className"] for c in feed[1:]]
        self.assertEqual(classes, ["item ", "item ", "item blocked", "item masked", "item blocked"])
        status = [c["subs"][".res"] for c in feed[1:]]
        self.assertIn("3 apps", status[0])
        self.assertIn("2 rows", status[1])
        self.assertIn("BLOCKED BY CONNECT AI", status[2])
        pii = feed[3]["subs"][".detail"]
        self.assertIn("REFUSAL", pii)
        self.assertIn("123-45-6789", pii)
        self.assertIn("claude-sonnet-5-5", pii)

    def test_kill_feed_past_the_cap(self):
        """The server sends the last 12 kills; the count and the redraw follow kill_total."""
        kills = [{"app": "Zendesk", "label": f"K{i}", "detail": "", "writable": False} for i in range(18, 30)]
        ids = self.render(kills=kills, kill_total=30)
        self.assertEqual(ids["s-kills"]["text"], 30)
        html = ids["kills"]["html"]
        self.assertLess(html.index("K29"), html.index("K18"))

    def test_live_text_while_running(self):
        running = [e for e in self.events if e["kind"] in ("question", "session", "text_delta")]
        ids = self.render(events=running, mission=None)
        live = ids["feed"]["children"]
        self.assertEqual(len(live), 1)
        self.assertIn("CLAUDE IS THINKING", live[0]["html"])
        self.assertIn("Let me look.", live[0]["html"])
        self.assertNotIn("answer", ids, "the answer placeholder stays until the mission is in")
        self.assertEqual(ids["s-model"]["text"], "SONNET-5-5")

    def test_play_feed(self):
        play = [{"kind": "call", "id": "p1", "server": "doom", "tool": "go_to", "args": {"target": "Acme"}},
                {"kind": "result", "id": "p1", "tool": "go_to", "text": "ok", "ms": 20}]
        ids = self.render(play_events=play)
        self.assertFalse(ids["play"]["hidden"])
        item = ids["playfeed"]["children"][0]
        self.assertIn("Headed to <b>Acme</b>", item["html"])
        self.assertIn("DOOM", item["html"])

    def test_empty_state(self):
        ids = self.render(events=[], mission=None)
        self.assertEqual(ids["s-apps"]["text"], 0)
        self.assertEqual(ids["standing"]["text"], "0 still standing, none cleared yet")

    def test_format_sql_and_parse_csv(self):
        sql = "select a, 'from x' from [S].[t] where a = 1 and (b or c) order by a"
        csv = 'Id,Label\n1,"a, ""b"""\r\n2,"two\nlines"\n'
        _, value = self.render(expr=f"""[
          formatSQL({json.dumps(sql)}),
          parseCSV({json.dumps(csv)}),
          describe({{tool: "getColumns", connection: "Z<1>", args: {{tableName: "T"}}}}),
          status({{tool: "queryData", result: {{error: true, text: "Cannot obtain connection"}}}}),
        ]""")
        sql, csv, desc, stat = value
        self.assertEqual(sql.split("\n")[1:], ['<span class="kw">FROM</span> [S].[t]',
                                               '<span class="kw">WHERE</span> a = 1',
                                               '  <span class="kw">AND</span> (b <span class="kw">or</span> c)',
                                               '<span class="kw">ORDER</span> <span class="kw">BY</span> a'])
        self.assertIn("<span class=\"str\">'from x'</span>", sql)       # keywords inside strings stay plain
        self.assertEqual(csv, [["Id", "Label"], ["1", 'a, "b"'], ["2", "two\nlines"]])
        self.assertEqual(desc, "Checked the fields of <b>T</b> in <b>Z&lt;1&gt;</b>")
        self.assertEqual(stat, ["blocked", "BLOCKED BY CONNECT AI"])

    def test_state_keys_match_the_server(self):
        """Every key the panel reads from /state.json is one connect_doom.py serves."""
        script = (ROOT / "panel/index.html").read_text()
        served = (ROOT / "connect_doom.py").read_text()
        for key in ("events", "play_events", "mission", "kills", "kill_total", "standing"):
            self.assertIn(f"s.{key}", script)
            self.assertIn(f'"{key}":', served)


if __name__ == "__main__":
    unittest.main()
