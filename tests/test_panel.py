"""panel/index.html: the side panel's script, rendered in Node from a real brief.py run's state."""
import datetime
import json
import os
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

    def render(self, expr=None, search="", **state):
        state = {"events": self.events, "play_events": [], "mission": self.mission, "kills": [], "standing": 0,
                 **state}
        path = Path(self.tmp.name) / "panel-state.json"
        path.write_text(json.dumps(state))
        r = subprocess.run([NODE, str(HARNESS), str(ROOT / "panel/index.html"), str(path), *([expr] if expr else [])],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "PANEL_SEARCH": search})
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.css = out["css"]
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

    def test_game_view(self):
        kills = [{"id": f"Zendesk:{i}", "app": "Zendesk", "label": f"K{i}", "detail": "high open", "writable": True,
                  "demon": i % 2, "killed_at": f"2026-10-15T13:54:{i}"} for i in range(18, 30)]
        hits = [{"id": "Stripe:9", "app": "Stripe", "label": "Acme <b>Corp</b>", "detail": "$5", "demon": 0,
                 "at": datetime.datetime(2026, 10, 15, 13, 54, 40).timestamp() + n} for n in range(3)]
        rooms = [{"app": "Zendesk", "writable": True, "title": "Escalated", "left": 3, "urgent_left": 1, "killed": 12,
                  "monster": {"key": "imp", "name": "Imp"}, "urgent_monster": {"key": "caco", "name": "Cacodemon"}},
                 {"app": "Stripe", "writable": False, "title": "Unpaid", "left": 0, "urgent_left": 0, "killed": 4,
                  "monster": {"key": "soldier", "name": "Shotgun soldier"},
                  "urgent_monster": {"key": "demon", "name": "Pink demon"}}]
        ids = self.render(kills=kills, kill_total=30, hits=hits, rooms_live=rooms,
                          game={"mode": "player", "started": 0})
        self.assertIn("in-game", ids["body"]["className"])              # the briefing sections hide
        self.assertEqual(ids["s-kills"]["text"], 30)
        self.assertEqual(ids["now-mode"]["text"], "YOU'RE PLAYING · LIVE")
        now = ids["now-big"]["html"]                                     # the latest event is the hits
        self.assertIn("SHOOTING A STRIPE RECORD", now)
        self.assertIn("Acme &lt;b&gt;Corp&lt;/b&gt;", now)                # escaped
        events = ids["events"]["html"]
        self.assertEqual(events.count("HIT ×3"), 1)                      # a run of hits is one line
        self.assertLess(events.index("HIT ×3"), events.index("K29"))     # newest first
        self.assertLess(events.index("K29"), events.index("K18"))
        self.assertIn("No live read came back", events)                  # minutes old, no lookup logged
        rl = ids["rooms-live"]["html"]
        self.assertIn("12<small> / 15 cleared", rl)
        self.assertIn("3 left, 1 urgent", rl)
        self.assertIn("Room clear", rl)
        self.assertIn("PII vault", rl)                                   # the mission's blocked lookup
        self.assertIn('sprites/caco.png" alt=""><span><b>Cacodemon</b> urgent', rl)   # each room's own monsters
        legend = ids["legend-monsters"]["html"]
        self.assertIn("<b>ZENDESK</b>Imp; urgent: Cacodemon", legend)
        self.assertIn("sprites/soldier.png", legend)
        self.assertTrue(ids["play"]["hidden"])

    def test_kill_shows_connect_ais_live_read(self):
        kill = {"id": "Zendesk:1", "app": "Zendesk", "label": "Initech", "detail": "high open", "writable": True,
                "demon": 0, "killed_at": "2026-10-15T13:54:10",
                "lookup": {"ms": 1850, "row": {"detail": "urgent pending"}, "at": 1}}
        gone = {**kill, "id": "Zendesk:2", "killed_at": "2026-10-15T13:54:05", "lookup": {"ms": 900, "row": None, "at": 1}}
        failed = {**kill, "id": "Zendesk:3", "killed_at": "2026-10-15T13:54:01",
                  "lookup": {"ms": 30000, "error": "timed <out>", "at": 1}}
        pending = {**kill, "id": "Zendesk:4", "lookup": None,
                   "killed_at": datetime.datetime.now().isoformat(timespec="seconds")}
        ids = self.render(kills=[failed, gone, kill, pending], kill_total=4, game={"mode": "player", "started": 0})
        self.assertIn("Reading this record live from Zendesk", ids["now-big"]["html"])  # just killed
        events = ids["events"]["html"]
        self.assertIn("Read live from Zendesk in 1.9 s: still there, urgent pending", events)
        self.assertIn("in 0.9 s: no longer returned by the room's query", events)
        self.assertIn("Live read failed after 30.0 s: timed &lt;out&gt;", events)

    def test_red_door_and_live_refresh(self):
        now = datetime.datetime.now().timestamp()
        vault = [{"connection": "PostgreSQLPII", "blocked": True, "ms": 1611, "at": now,
                  "text": "Request blocked: PII detected in inbound query",
                  "request": {"query": "SELECT x WHERE [ssn] = @ssn", "parameters": {"@ssn": "123-45-6789"}}}]
        rooms = [{"app": "Zendesk", "writable": True, "title": "T", "left": 3, "urgent_left": 0, "killed": 0,
                  "refresh": {"connection": "Zendesk", "rows": 15, "ms": 7249, "at": now - 4}}]
        ids = self.render(vault=vault, rooms_live=rooms, game={"mode": "player", "started": 0})
        now_card = ids["now-big"]["html"]
        self.assertIn("RED DOOR: CONNECT AI SAID NO, LIVE", now_card)
        self.assertIn("Data Security blocked it in 1.6 s", now_card)
        self.assertIn("@ssn = &#39;123-45-6789&#39;", now_card.replace("'", "&#39;"))
        self.assertIn("blocked it again, live in 1.6 s", ids["events"]["html"])
        self.assertIn("Tried 1× this game", ids["rooms-live"]["html"])
        self.assertRegex(ids["rooms-live"]["html"], r"Live: 15 rows [45]s ago, in 7\.2 s")

    def test_saved_records_say_so(self):
        """Connect AI didn't answer at launch: the rooms say the records are saved, not live."""
        fetched = datetime.datetime(2026, 10, 15, 15, 35).timestamp()
        rooms = [{"app": "Zendesk", "writable": True, "title": "T", "left": 3, "urgent_left": 0, "killed": 0,
                  "refresh": {"connection": "Zendesk", "rows": 15, "ms": 7249, "at": fetched}}]
        ids = self.render(rooms_live=rooms, records={"source": "saved", "fetched_at": fetched},
                          game={"mode": "player", "started": 0})
        self.assertIn("Saved Oct 15, 15:35: Connect AI isn't answering", ids["rooms-live"]["html"])
        self.assertNotIn("Live:", ids["rooms-live"]["html"])
        # the Options menu's saved data switch: by choice, so no error styling
        ids = self.render(rooms_live=rooms, records={"source": "saved-data", "fetched_at": fetched},
                          game={"mode": "player", "started": 0})
        self.assertIn('<div class="live">Saved data (Options menu): the records from Oct 15, 15:35',
                      ids["rooms-live"]["html"])
        self.assertNotIn("Live:", ids["rooms-live"]["html"])

    def test_all_rooms_clear(self):
        kill = {"id": "Zendesk:1", "app": "Zendesk", "label": "Initech", "detail": "", "writable": True,
                "demon": 0, "killed_at": "2026-10-15T13:54:10"}
        rooms = [{"app": "Zendesk", "writable": True, "title": "T", "left": 0, "urgent_left": 0, "killed": 1}]
        ids = self.render(kills=[kill], kill_total=1, rooms_live=rooms, game={"mode": "player", "started": 0})
        self.assertIn("ALL 1 ROOMS CLEAR", ids["now-big"]["html"])
        self.assertIn("Now try the red door.", ids["now-big"]["html"])          # the mission has a vault
        self.assertFalse(ids["now-keys"]["hidden"])

    def test_claude_plays_mode(self):
        play = [{"kind": "call", "id": "p1", "server": "doom", "tool": "look", "args": {}}]
        ids = self.render(play_events=play, game={"mode": "claude", "started": 0})
        self.assertEqual(ids["now-mode"]["text"], "CLAUDE IS PLAYING · LIVE")
        self.assertTrue(ids["now-keys"]["hidden"], "no controls hint while Claude has them")
        self.assertFalse(ids["play"]["hidden"])

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
        play = [{"kind": "call", "id": "p1", "server": "doom", "tool": "go_to", "args": {"place": "Acme"}},
                {"kind": "result", "id": "p1", "tool": "go_to", "text": "ok", "ms": 20}]
        ids = self.render(play_events=play, game={"mode": "claude", "started": 0})
        self.assertFalse(ids["play"]["hidden"])
        item = ids["playfeed"]["children"][0]
        self.assertIn("Headed to <b>Acme</b>", item["html"])
        self.assertIn("DOOM", item["html"])

    def test_play_feed_shows_each_call_and_decision(self):
        play = [{"kind": "session", "model": "claude-sonnet-5-5"},
                {"kind": "thought", "text": "Acme's renewal is urgent, taking it down."},
                {"kind": "call", "id": "p1", "server": "doom", "tool": "attack", "args": {"pid": 42}},
                {"kind": "call", "id": "p2", "server": "connectai", "tool": "queryData",
                 "args": {"query": "SELECT Id, Name FROM [Salesforce].[Account] WHERE Id = @id"}},
                {"kind": "reasoning", "text": "The vault comes last."},
                {"kind": "text_delta", "text": "Heading to <Zendesk>"}]
        ids = self.render(play_events=play, game={"mode": "claude", "started": 0})
        items = ids["playfeed"]["children"]
        self.assertIn("CLAUDE IS THINKING", items[0]["html"])           # live text stays on top
        self.assertIn("Heading to &lt;Zendesk&gt;", items[0]["html"])
        html = "".join(i["html"] + i["subs"].get(".what", "") for i in items)
        self.assertIn('<span class="srv">doom</span>.attack(pid: 42)', html)
        self.assertIn('<span class="srv">connect_ai</span>.queryData(query)', html)   # SQL is too long to inline
        self.assertIn("REASONS", html)
        self.assertIn("Acme&#39;s renewal".replace("&#39;", "'"), html)
        self.assertEqual(ids["play-meta"]["text"], "2 tool calls · sonnet-5-5")

    def test_full_screen_stage(self):
        search = "?stage=24,147,960,720,1008,32"
        ids = self.render(search=search, events=[e for e in self.events if e["kind"] != "mission"], mission=None)
        self.assertIn("stage", ids["body"]["className"])
        self.assertFalse(ids["stage"]["hidden"])
        self.assertEqual(self.css, {"--gx": "24px", "--gy": "147px", "--gw": "960px", "--gh": "720px",
                                    "--split": "1008px", "--top": "32px"})
        self.assertEqual(ids["stage-mode"]["text"], "CLAUDE IS BRIEFING · LIVE")
        self.assertEqual(ids["stage-q"]["text"], "Who is <at risk>?")
        self.assertRegex(ids["stage-progress"]["text"], r"^\d+ calls through Connect AI so far$")
        self.assertEqual(ids["stage-say"]["html"], "")

        ids = self.render(search=search, game={"mode": "player", "started": 0})
        self.assertEqual(ids["stage-mode"]["text"], "YOU'RE PLAYING · LIVE")
        self.assertIn("<b>WASD</b> move", ids["stage-say"]["html"])     # the controls, under the game

        play = [{"kind": "thought", "text": "Initech is <urgent>, taking it down."},
                {"kind": "call", "id": "p1", "server": "doom", "tool": "attack", "args": {"pid": 7}}]
        ids = self.render(search=search, play_events=play, game={"mode": "claude", "started": 0})
        self.assertEqual(ids["stage-mode"]["text"], "CLAUDE IS PLAYING · LIVE")
        self.assertIn("Initech is &lt;urgent&gt;, taking it down.", ids["stage-say"]["html"])  # Claude's latest line

    def test_offline_counts_the_level(self):
        rooms = [{"app": "Demo CRM", "writable": True, "title": "T", "left": 10, "urgent_left": 1, "killed": 2},
                 {"app": "Demo Billing", "writable": False, "title": "T", "left": 12, "urgent_left": 0, "killed": 0}]
        ids = self.render(events=[], rooms_live=rooms, game={"mode": "claude", "started": 0})
        self.assertEqual(ids["s-apps"]["text"], 2)                       # the mission's rooms
        self.assertEqual(ids["s-records"]["text"], 24)                   # every monster in the level
        self.assertEqual(ids["s-blocked"]["text"], 1)                    # the PII vault
        rl = ids["rooms-live"]["html"]
        self.assertIn('<span class="acc"> · READ + WRITE</span><span class="acc-s"> · R+W</span>', rl)
        self.assertIn('<span class="acc-s"> · READ</span>', rl)

    def test_no_stage_without_the_layout(self):
        ids = self.render()
        self.assertNotIn("stage", ids["body"]["className"])
        self.assertEqual(self.css, {})

    def test_empty_state(self):
        ids = self.render(events=[], mission=None)
        self.assertEqual(ids["s-apps"]["text"], 0)
        self.assertNotIn("in-game", ids["body"]["className"])

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
        for key in ("events", "play_events", "mission", "game", "kills", "kill_total", "hits", "rooms_live",
                    "records"):
            self.assertIn(f"s.{key}", script)
            self.assertIn(f'"{key}":', served)


if __name__ == "__main__":
    unittest.main()
