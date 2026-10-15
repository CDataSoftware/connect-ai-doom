"""connect_doom.py: the psdoom bridge (list/kill/hit), the poller, score/reset, and the panel server."""
import functools
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import PAT, ROOT, USER, FakeMCP, TempState, free_port, query

import connect_ai_mcp
import connect_doom as cd

ROOM = {"connection": "Salesforce1", "app": "Salesforce", "writable": True}


def bridge(state, *args, env=None):
    """Run the bridge the way psdoom does: a fresh process per call."""
    return subprocess.run([sys.executable, str(ROOT / "connect_doom.py"), *args], capture_output=True,
                          text=True, timeout=60, env={**os.environ, "CONNECT_DOOM_STATE": str(state), **(env or {})})


class MonsterTest(unittest.TestCase):
    def test_monster_id_is_stable_and_lands_in_its_room(self):
        for room in range(3):
            for rid in ("Salesforce1:006A", "x", "Zendesk1:" + "9" * 40):
                pid = cd.monster_id(rid, room)
                self.assertEqual(pid % 3, room)
                self.assertEqual(pid, cd.monster_id(rid, room))
                self.assertGreater(pid, 1000)        # clear of real low PIDs
                self.assertLess(pid, 2 ** 31)

    def test_monster_ids_rarely_collide(self):
        pids = {cd.monster_id(f"c:{i}", 0) for i in range(2000)}
        self.assertGreater(len(pids), 1990)

    def test_short_name(self):
        cases = {"Desert Rose Inc": "DESERT", "2025 Acme Renewal": "ACME", "Q4-REN-Globex": "REN",
                 "Wonkatronics Industries": "WONKATR", "AB": "AB", "": "X", None: "X", "42": "X",
                 "O'Brien & Sons": "OBRIEN"}
        for label, want in cases.items():
            with self.subTest(label=label):
                got = cd.short_name(label)
                self.assertEqual(got, want)
                self.assertLessEqual(len(got), 7)
                self.assertNotIn(" ", got)

    def test_to_monster(self):
        m = cd.to_monster({"Id": "006A", "Label": "Acme Corp", "Detail": "$45,000", "Hot": "1"}, 2, ROOM)
        self.assertEqual(m, {"pid": cd.monster_id("Salesforce1:006A", 2), "id": "Salesforce1:006A",
                             "app": "Salesforce", "writable": True, "label": "Acme Corp", "detail": "$45,000",
                             "name": "ACME", "demon": 1})

    def test_hot_values(self):
        for hot, demon in (("1", 1), ("True", 1), ("true", 1), (1, 1), (True, 1), (" 1 ", 1),
                           ("0", 0), (0, 0), ("False", 0), (None, 0), ("", 0)):
            with self.subTest(hot=hot):
                self.assertEqual(cd.to_monster({"Id": "1", "Hot": hot}, 0, ROOM)["demon"], demon)

    def test_to_monster_dropped_label_alias(self):
        row = {"Id": "287", "Subject": "Escalation - Desert Power", "Detail": "urgent open", "Hot": "1"}
        m = cd.to_monster(row, 0, ROOM)
        self.assertEqual((m["label"], m["name"]), ("Escalation - Desert Power", "ESCALAT"))

    def test_to_monster_defaults(self):
        m = cd.to_monster({"Id": "1"}, 0, {"connection": "Raw1"})
        self.assertEqual((m["app"], m["writable"], m["label"], m["detail"]), ("Raw1", False, "?", ""))

    def test_demo_mission_is_offline_and_deterministic(self):
        m = cd.demo_mission()
        self.assertEqual(m, cd.demo_mission())
        self.assertEqual(len(m["rooms"]), 3)
        self.assertTrue(all(len(r["rows"]) == 12 for r in m["rooms"]))
        self.assertTrue(m["denied"])
        monsters = cd.fetch(m)                       # never builds an MCP client
        self.assertEqual(len(monsters), 36)
        self.assertEqual({p["pid"] % 3 for p in monsters if p["app"] == "Demo Billing"}, {2})


class CommandsTest(unittest.TestCase):
    """list / kill / hit / score / reset, in-process."""

    def setUp(self):
        self.ctx = TempState()
        self.state = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__)
        rows = [{"Id": "a", "Label": "Acme Corp", "Detail": "$1", "Hot": 1},
                {"Id": "b", "Label": "Globex", "Detail": "$2", "Hot": 0}]
        self.monsters = [cd.to_monster(r, 0, ROOM) for r in rows]
        self.monsters.append(cd.to_monster({"Id": "c", "Label": "Initech"}, 1,
                                           {"connection": "Zendesk1", "app": "Zendesk"}))
        cd.write_cache(self.monsters, "test")

    def run_cmd(self, name, *args):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()):
            cd.COMMANDS[name](list(args))
        return out.getvalue()

    def test_list_format(self):
        lines = self.run_cmd("list").splitlines()
        self.assertEqual(lines[0], f"connectai {self.monsters[0]['pid']} ACME 1")
        self.assertEqual(len(lines), 3)
        for line in lines:
            user, pid, name, demon = line.split(" ")
            self.assertEqual(user, "connectai")
            self.assertIn(demon, ("0", "1"))

    def test_list_without_cache_is_empty(self):
        cd.CACHE.unlink()
        self.assertEqual(self.run_cmd("list"), "")
        cd.CACHE.write_text("{half a fi")
        self.assertEqual(self.run_cmd("list"), "")

    def test_kill_logs_hides_and_prints_the_in_game_message(self):
        acme, initech = self.monsters[0], self.monsters[2]
        out = self.run_cmd("kill", str(acme["pid"]))
        self.assertEqual(out.splitlines()[0], "SALESFORCE: ACME CORP, KILLED")
        self.assertEqual(self.run_cmd("kill", str(initech["pid"])).splitlines()[0], "ZENDESK: INITECH, KILLED")
        kills = cd.read_jsonl(cd.KILLS)
        self.assertEqual([k["id"] for k in kills], [acme["id"], initech["id"]])
        self.assertTrue(all(k["mode"] == "dry-run" and k["killed_at"] for k in kills))
        self.assertNotIn(str(acme["pid"]), self.run_cmd("list"))
        self.assertEqual(len(self.run_cmd("list").splitlines()), 1)

    def test_cache_writes_the_crosshair_labels(self):
        m = cd.to_monster({"Id": "1", "Label": "Acme\tCorp", "Detail": "$5,\nlate", "Hot": "1"}, 0, ROOM)
        cd.write_cache([m], "test")
        self.assertEqual(cd.LABELS.read_text(), f"{m['pid']}\tSalesforce: Acme Corp\tURGENT: $5, late\n")

    def test_each_game_starts_with_every_record_alive(self):
        m = self.monsters[0]
        self.run_cmd("kill", str(m["pid"]))
        self.assertNotIn(str(m["pid"]), self.run_cmd("list"))
        cd.GAME.write_text(json.dumps({"mode": "player", "started": time.time() + 1}))   # a later game
        self.assertIn(str(m["pid"]), self.run_cmd("list"))

    def test_kill_message_is_capped_for_the_hud(self):
        m = cd.to_monster({"Id": "z", "Label": "A" * 80}, 0, ROOM)
        cd.write_cache([m], "test")
        first = self.run_cmd("kill", str(m["pid"])).splitlines()[0]
        self.assertEqual(first, ("SALESFORCE: " + "A" * 80)[:44] + ", KILLED")

    def test_kill_and_hit_of_unknown_pid_do_nothing(self):
        self.assertEqual(self.run_cmd("kill", "12345"), "")
        self.run_cmd("hit", "12345")
        self.assertFalse(cd.KILLS.exists())
        self.assertFalse(cd.HITS.exists())

    def test_hit(self):
        self.run_cmd("hit", str(self.monsters[1]["pid"]))
        hits = cd.read_jsonl(cd.HITS)
        self.assertEqual(hits[0]["id"], self.monsters[1]["id"])
        self.assertAlmostEqual(hits[0]["at"], time.time(), delta=60)

    def test_score_and_reset(self):
        self.run_cmd("kill", str(self.monsters[0]["pid"]))
        self.run_cmd("hit", str(self.monsters[1]["pid"]))
        score = self.run_cmd("score")
        self.assertTrue(score.startswith("1 cleared, 2 still standing"))
        self.assertIn("Acme Corp", score)
        self.assertEqual(self.run_cmd("reset").strip(), "kills cleared")
        self.assertFalse(cd.KILLS.exists() or cd.HITS.exists())
        self.assertTrue(self.run_cmd("score").startswith("0 cleared, 3 still standing"))

    def test_write_cache_is_atomic(self):
        self.assertFalse(cd.CACHE.with_suffix(".tmp").exists())
        data = json.loads(cd.CACHE.read_text())
        self.assertEqual((data["source"], len(data["monsters"])), ("test", 3))

    def test_read_jsonl_skips_blank_lines(self):
        cd.KILLS.write_text('{"id": 1}\n\n{"id": 2}\n')
        self.assertEqual(cd.read_jsonl(cd.KILLS), [{"id": 1}, {"id": 2}])


class FetchTest(unittest.TestCase):
    """fetch() re-runs each room's query through the MCP server."""

    def test_live_rooms(self):
        mission = {"rooms": [
            {"connection": "Salesforce1", "app": "Salesforce", "query": query("Salesforce1"), "writable": True},
            {"connection": "PostgreSQLPII", "app": "Postgres", "query": "SELECT * FROM [PostgreSQLPII].[x].[y]"},
            {"connection": "Zendesk1", "app": "Zendesk", "query": query("Zendesk1")},
            {"connection": "QuickBooks1", "app": "QuickBooks", "query": query("QuickBooks1")},  # 4th: ignored
        ]}
        with FakeMCP() as mcp, mock.patch.dict(os.environ, {"CONNECT_AI_USER": USER, "CONNECT_AI_PAT": PAT}), \
                mock.patch.object(connect_ai_mcp, "Client", functools.partial(connect_ai_mcp.Client, mcp.url)):
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                monsters = cd.fetch(mission)
        self.assertEqual([m["id"] for m in monsters], ["Salesforce1:006A", "Salesforce1:006B", "Zendesk1:Z1"])
        self.assertEqual([m["pid"] % 3 for m in monsters], [0, 0, 2])     # Zendesk keeps its room index
        self.assertEqual(monsters[2]["demon"], 1)
        self.assertIn("Postgres: Request blocked", err.getvalue())         # a failing room is skipped

    def test_max_per_room(self):
        mission = {"rooms": [{"connection": "Salesforce1", "query": query("Salesforce1")}]}
        fake = mock.Mock()
        fake.return_value.query.return_value = [{"Id": str(i)} for i in range(50)]
        with mock.patch.object(cd.connect_ai_mcp, "Client", fake), mock.patch.object(cd, "MAX_PER_ROOM", 7):
            self.assertEqual(len(cd.fetch(mission)), 7)


class PollCLITest(unittest.TestCase):
    """`poll` as raid.sh runs it."""

    def test_poll_demo_writes_mission_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = bridge(tmp, "poll", "--demo")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertRegex(r.stderr, r"demo: 36 monsters")
            self.assertEqual(json.loads((Path(tmp) / "mission.json").read_text()), cd.demo_mission())
            cache = json.loads((Path(tmp) / "cache.json").read_text())
            self.assertEqual((cache["source"], len(cache["monsters"])), ("demo", 36))
            listed = bridge(tmp, "list").stdout.splitlines()
            self.assertEqual(len(listed), 36)
            pid = listed[0].split()[1]
            self.assertTrue(bridge(tmp, "kill", pid).stdout.endswith(", KILLED\n"))
            self.assertFalse((Path(tmp) / "lookups.jsonl").exists(), "the demo has no Connect AI to read from")
            self.assertEqual(len(bridge(tmp, "list").stdout.splitlines()), 35)
            self.assertIn("1 cleared, 35 still standing", bridge(tmp, "score").stdout)

    def test_poll_once_against_the_mcp_server(self):
        with tempfile.TemporaryDirectory() as tmp, FakeMCP() as mcp:
            mission = {"rooms": [{"connection": "Zendesk1", "app": "Zendesk", "query": query("Zendesk1")}]}
            (Path(tmp) / "mission.json").write_text(json.dumps(mission))
            env = {"CONNECT_AI_MCP_URL": mcp.url, "CONNECT_AI_USER": USER, "CONNECT_AI_PAT": PAT}
            r = bridge(tmp, "poll", "--once", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("connect-ai-mcp: 1 monsters", r.stderr)
            self.assertEqual(bridge(tmp, "list").stdout.split()[2:], ["INITECH", "1"])

    def test_kill_reads_the_record_live_in_the_background(self):
        with tempfile.TemporaryDirectory() as tmp, FakeMCP() as mcp:
            mission = {"rooms": [{"connection": "Zendesk1", "app": "Zendesk", "query": query("Zendesk1")}]}
            (Path(tmp) / "mission.json").write_text(json.dumps(mission))
            env = {"CONNECT_AI_MCP_URL": mcp.url, "CONNECT_AI_USER": USER, "CONNECT_AI_PAT": PAT}
            bridge(tmp, "poll", "--once", env=env)
            pid = bridge(tmp, "list").stdout.split()[1]
            r = bridge(tmp, "kill", pid, env=env)
            self.assertEqual(r.stdout.splitlines()[0], "ZENDESK: INITECH, KILLED")   # without waiting on the read
            lookups = Path(tmp) / "lookups.jsonl"
            for _ in range(100):
                if lookups.exists():
                    break
                time.sleep(0.05)
            entry = json.loads(lookups.read_text())
            self.assertEqual((entry["id"], entry["row"]["label"]), ("Zendesk1:Z1", "Initech"))
            self.assertIn("AS q WHERE q.[Id] = 'Z1'", entry["sql"])
            self.assertGreaterEqual(entry["ms"], 0)

    def test_lookup_sql_quotes_the_id(self):
        self.assertTrue(cd.lookup_sql({"query": "SELECT 1"}, "o'neil").endswith("q.[Id] = 'o''neil'"))

    def test_poll_failure_keeps_the_last_good_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge(tmp, "poll", "--demo")
            before = (Path(tmp) / "cache.json").read_text()
            (Path(tmp) / "mission.json").write_text("not json")
            r = bridge(tmp, "poll", "--once")
            self.assertEqual(r.returncode, 0)
            self.assertIn("poll failed", r.stderr)
            self.assertEqual((Path(tmp) / "cache.json").read_text(), before)

    def test_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            for args in ((), ("bogus",)):
                r = bridge(tmp, *args)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("usage: connect_doom.py {list|kill|hit|lookup|poll|score|reset|panel}", r.stderr)


class PanelServerTest(unittest.TestCase):
    """`panel` serves panel/index.html and /state.json."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name)
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "connect_doom.py"), "panel", str(cls.port)],
                                    env={**os.environ, "CONNECT_DOOM_STATE": str(cls.state)},
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + "/state.json", timeout=1)
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.proc.stderr.close()
        cls.tmp.cleanup()

    def setUp(self):
        for p in self.state.iterdir():
            p.unlink()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.headers, r.read()

    def state_json(self):
        status, headers, body = self.get("/state.json?t=1")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        return json.loads(body)

    def test_serves_the_panel(self):
        for path in ("/", "/index.html"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertEqual(body, (ROOT / "panel" / "index.html").read_bytes())

    def test_does_not_serve_files_outside_the_panel(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/../connect_doom.py")
        self.assertEqual(e.exception.code, 404)
        e.exception.close()

    def test_binds_localhost_only(self):
        import socket
        ip = socket.gethostbyname(socket.gethostname())
        if ip.startswith("127."):
            self.skipTest("no non-loopback address")
        with self.assertRaises(OSError):
            socket.create_connection((ip, self.port), timeout=1).close()

    def test_empty_state(self):
        self.assertEqual(self.state_json(), {"events": [], "play_events": [], "mission": None,
                                             "game": None, "kills": [], "kill_total": 0, "hits": [],
                                             "rooms_live": [], "standing": 0})

    def test_full_state(self):
        mission = {**cd.demo_mission(), "calls": [{"tool": "queryData", "args": {}, "error": False}]}
        (self.state / "mission.json").write_text(json.dumps(mission))
        cache_monsters = cd.fetch(mission)
        (self.state / "cache.json").write_text(json.dumps({"monsters": cache_monsters}))
        kills = [{**m, "killed_at": "2026-10-15T13:52:33", "mode": "dry-run"} for m in cache_monsters[:14]]
        (self.state / "kills.jsonl").write_text("".join(json.dumps(k) + "\n" for k in kills))
        (self.state / "run.jsonl").write_text('{"kind": "question", "text": "q"}\n{"kind": "ca')  # mid-write
        (self.state / "play.jsonl").write_text('{"kind": "call", "server": "doom", "tool": "look"}\n')
        s = self.state_json()
        self.assertEqual(s["events"], [{"kind": "question", "text": "q"}])
        self.assertEqual(s["play_events"][0]["tool"], "look")
        self.assertEqual(set(s["mission"]), {"question", "answer", "rooms", "denied", "masked"})  # no raw calls
        self.assertEqual([k["id"] for k in s["kills"]], [k["id"] for k in kills[-12:]])
        self.assertEqual(s["kill_total"], 14)
        self.assertEqual([r["left"] + r["killed"] for r in s["rooms_live"]], [12, 12, 12])
        self.assertEqual(sum(r["killed"] for r in s["rooms_live"]), 14)
        self.assertIsNone(s["game"])
        self.assertEqual(s["standing"], 36 - 14)

    def test_game_shows_only_this_games_kills_and_hits(self):
        mission = cd.demo_mission()
        (self.state / "mission.json").write_text(json.dumps(mission))
        monsters = cd.fetch(mission)
        (self.state / "cache.json").write_text(json.dumps({"monsters": monsters}))
        start = cd.dt.datetime(2026, 10, 15, 14, 0).timestamp()
        (self.state / "game.json").write_text(json.dumps({"mode": "player", "started": start}))
        old, new = monsters[0], monsters[1]
        (self.state / "kills.jsonl").write_text(
            json.dumps({**old, "killed_at": "2026-10-15T13:00:00"}) + "\n" +
            json.dumps({**new, "killed_at": "2026-10-15T14:00:05"}) + "\n")
        (self.state / "hits.jsonl").write_text(json.dumps({"id": old["id"], "at": start - 60}) + "\n" +
                                               json.dumps({"id": new["id"], "at": start + 4}) + "\n")
        s = self.state_json()
        self.assertEqual(s["game"]["mode"], "player")
        self.assertEqual([k["id"] for k in s["kills"]], [new["id"]])
        self.assertEqual(s["kill_total"], 1)
        self.assertEqual([(h["id"], h["label"]) for h in s["hits"]], [(new["id"], new["label"])])
        self.assertEqual(sum(r["killed"] for r in s["rooms_live"]), 1)
        self.assertEqual(s["standing"], 36 - 1)                  # earlier games' kills come back

    def test_corrupt_mission_is_null(self):
        (self.state / "mission.json").write_text("{")
        self.assertIsNone(self.state_json()["mission"])


if __name__ == "__main__":
    unittest.main()
