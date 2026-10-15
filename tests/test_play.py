"""play.py: the Doom MCP server, its game channel and tools, and the Claude-plays runner."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import PAT, ROOT, USER, FAKE_CLAUDE

import play

REAL_SLEEP = time.sleep     # play.time is the time module, so patches of it are global

MISSION = {"question": "Who is at risk?",
           "rooms": [{"connection": "Salesforce1", "app": "Salesforce", "title": "Deals slipping", "writable": True},
                     {"connection": "Zendesk1", "app": "Zendesk", "title": "Open escalations", "writable": False}],
           "denied": [{"connection": "PostgreSQLPII", "error": "Request blocked: PII detected in inbound query\nx"}]}


def monster(pid, health=100, visible=True, distance=300):
    return {"pid": pid, "health": health, "visible": visible, "distance": distance}


def game_state(x=2560, y=-896, keys=(), monsters=(), done_seq=0, result="arrived"):
    return {"player": {"x": x, "y": y, "health": 100, "ammo": 50,
                       "keys": {k: k in keys for k in ("blue", "yellow", "red")}},
            "monsters": list(monsters), "command": {"done_seq": done_seq, "result": result}}


class FakeGame:
    """Stands in for play.Game: records commands, answers MOVE with scripted results."""

    def __init__(self, state, results=None):
        self.s, self.results, self.commands = state, dict(results or {}), []

    def state(self):
        return self.s

    def do(self, verb, *args, timeout=25):
        self.commands.append((verb, *args))
        return self.results.get((verb, *args), "killed" if verb == "ATTACK" else "arrived")


class StateDirTest(unittest.TestCase):
    """Point play.py at a temp state dir holding the mission and the bridge's cache."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / "agent").mkdir()
        (self.state / "mission.json").write_text(json.dumps(MISSION))
        self.cache = [{"pid": 1002, "app": "Salesforce", "label": "Acme Corp", "detail": "$45,000", "demon": 1,
                       "writable": True},
                      {"pid": 1003, "app": "Zendesk", "label": "Initech", "detail": "3 tickets", "demon": 0,
                       "writable": False}]
        (self.state / "cache.json").write_text(json.dumps({"monsters": self.cache}))
        for name, value in (("STATE", self.state), ("AGENT", self.state / "agent"),
                            ("PLAY_LOG", self.state / "play.jsonl")):
            patcher = mock.patch.object(play, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = mock.patch.object(play.time, "sleep")     # door waits
        sleep.start()
        self.addCleanup(sleep.stop)


class HelpersTest(StateDirTest):
    def test_where(self):
        cases = {(100, 500): "room 1", (2100, 0): "room 2", (5000, 900): "room 3", (9000, 10): "room 3",
                 (2560, -200): "hall", (900, -600): "vault door", (2560, -896): "lobby", (300, -600): "lobby"}
        for (x, y), want in cases.items():
            self.assertEqual(play.where({"x": x, "y": y}), want, (x, y))

    def test_room_index(self):
        for target, want in (("1", 0), ("room 2", 1), ("Salesforce", 0), ("zendesk1", 1), (" sales ", 0),
                             ("QuickBooks", None), ("room 3", None)):
            self.assertEqual(play.room_index(target, MISSION), want, target)

    def test_records(self):
        self.assertEqual(play.records()[1002]["label"], "Acme Corp")
        (self.state / "cache.json").write_text("{")
        self.assertEqual(play.records(), {})


class ToolsTest(StateDirTest):
    def test_look(self):
        ms = [monster(1002, distance=500), monster(1005, distance=100), monster(1003, health=0),
              monster(1008, visible=False)]
        out = play.tool_look(FakeGame(game_state(keys=("blue",), monsters=ms)), {})
        self.assertEqual((out["you_are_in"], out["keycards"], out["pii_vault"]), ("lobby", ["blue"], True))
        self.assertEqual(out["rooms"], [
            {"room": 1, "app": "Salesforce", "found": "Deals slipping", "door": "yellow (read + write)",
             "monsters_left": 3},                                        # 1002, 1005, 1008: pid % 3 == 0
            {"room": 2, "app": "Zendesk", "found": "Open escalations", "door": "blue (read only)",
             "monsters_left": 0}])                                       # 1003 is dead
        self.assertEqual([v["pid"] for v in out["visible_monsters"]], [1005, 1002])   # nearest first
        self.assertEqual(out["visible_monsters"][1],
                         {"pid": 1002, "app": "Salesforce", "record": "Acme Corp", "detail": "$45,000",
                          "urgent": True, "distance": 500})
        self.assertIsNone(out["visible_monsters"][0]["record"])          # not in the cache yet

    def test_attack(self):
        out = play.tool_attack(FakeGame(game_state()), {"pid": 1002})
        self.assertEqual(out, {"result": "killed", "app": "Salesforce", "record": "Acme Corp", "detail": "$45,000",
                               "logged": "follow-up queued (dry run)"})
        self.assertEqual(play.tool_attack(FakeGame(game_state()), {"pid": 1003})["logged"], "flagged (dry run)")
        out = play.tool_attack(FakeGame(game_state(), {("ATTACK", 1003): "lost sight"}), {"pid": 1003})
        self.assertNotIn("logged", out)

    def test_go_to_keycards_from_the_lobby(self):
        g = FakeGame(game_state())
        self.assertEqual(play.tool_go_to(g, {"place": "keycards"})["result"], "arrived")
        self.assertEqual(g.commands, [("MOVE", 2560, -600),
                                      ("MOVE", *play.KEY_SPOTS["blue"]), ("MOVE", *play.KEY_SPOTS["yellow"])])

    def test_go_to_keycards_skips_unneeded_keys(self):
        (self.state / "mission.json").write_text(json.dumps({**MISSION, "rooms": MISSION["rooms"][1:]}))
        g = FakeGame(game_state(y=-200))
        play.tool_go_to(g, {"place": "keycards"})
        self.assertIn(("MOVE", *play.KEY_SPOTS["blue"]), g.commands)
        self.assertNotIn(("MOVE", *play.KEY_SPOTS["yellow"]), g.commands)

    def test_go_to_a_room_opens_its_door(self):
        g = FakeGame(game_state(keys=("yellow",)))
        out = play.tool_go_to(g, {"place": "Salesforce"})
        self.assertEqual((out["result"], out["keycards"]), ("arrived", ["yellow"]))
        cx = play.ARENA_X[0] + 512
        self.assertEqual(g.commands[-6:], [("MOVE", cx, -256), ("MOVE", cx, -78), ("TURN", 90), ("USE",),
                                           ("MOVE", cx, 150), ("MOVE", cx, 400)])

    def test_go_to_leaves_the_current_room_first(self):
        g = FakeGame(game_state(x=2100, y=500))
        play.tool_go_to(g, {"place": "lobby"})
        cx = play.ARENA_X[1] + 512
        self.assertEqual(g.commands[:3], [("MOVE", cx, 120), ("MOVE", cx, -150), ("MOVE", cx, -256)])

    def test_go_to_locked_door_names_the_keycard(self):
        cx = play.ARENA_X[1] + 512
        g = FakeGame(game_state(), {("MOVE", cx, 150): "stuck"})
        out = play.tool_go_to(g, {"place": "room 2"})
        self.assertEqual(out["result"], "Door locked: it needs the blue keycard. Go to the keycards first.")

    def test_go_to_blocked_by_monsters(self):
        cx = play.ARENA_X[0] + 512
        g = FakeGame(game_state(keys=("yellow",), monsters=[monster(1002, distance=50), monster(1005, distance=900)]),
                     {("MOVE", cx, 400): "stuck"})
        out = play.tool_go_to(g, {"place": "room 1"})
        self.assertTrue(out["result"].startswith("Blocked: monsters in the way (1 close by)"))
        self.assertEqual(out["closest_pids"], [1002])
        g = FakeGame(game_state(keys=("yellow",)), {("MOVE", cx, 400): "stuck"})
        self.assertEqual(play.tool_go_to(g, {"place": "room 1"})["result"], "Couldn't get there (stuck).")

    def test_vault_shows_connect_ais_refusal(self):
        g = FakeGame(game_state(y=-200), {("MOVE", 896, -560): "stuck"})
        out = play.tool_go_to(g, {"place": "the vault"})
        self.assertIn("There is no red keycard", out["result"])
        self.assertIn("Request blocked: PII detected in inbound query", out["result"])
        self.assertNotIn("\nx", out["result"])
        self.assertIn(("TURN", 270), g.commands)

    def test_no_vault_without_a_refusal(self):
        (self.state / "mission.json").write_text(json.dumps({**MISSION, "denied": []}))
        g = FakeGame(game_state())
        self.assertIn("There is no vault", play.tool_go_to(g, {"place": "vault"})["result"])
        self.assertEqual(g.commands, [])

    def test_unknown_place(self):
        self.assertIn("Unknown place 'mars'", play.tool_go_to(FakeGame(game_state()), {"place": "Mars"})["result"])


class GameChannelTest(StateDirTest):
    """play.Game against a fake psdoom agent loop: commands in agent/cmd, state out in agent/state.json."""

    def fake_psdoom(self, result="arrived", answer=True):
        agent, stop = self.state / "agent", threading.Event()
        self.seen = []

        def loop():
            done = 0
            (agent / "state.json").write_text(json.dumps(game_state(done_seq=0)))
            while not stop.is_set():
                try:
                    seq, verb, *args = (agent / "cmd").read_text().split()
                except (FileNotFoundError, ValueError):
                    seq = None
                if seq and int(seq) > done:
                    done = int(seq)
                    self.seen.append((verb, *args))
                    if answer or verb == "STOP":
                        tmp = agent / "state.tmp"
                        tmp.write_text(json.dumps(game_state(done_seq=done, result=result)))
                        tmp.replace(agent / "state.json")
                stop.wait(0.005)

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self.addCleanup(lambda: (stop.set(), t.join()))
        REAL_SLEEP(0.05)

    def test_command_round_trip(self):
        self.fake_psdoom(result="arrived")
        with mock.patch.object(play.time, "sleep", lambda s: REAL_SLEEP(min(s, 0.01))):
            g = play.Game()
            self.assertEqual(g.do("MOVE", 10.7, -20), "arrived")
            self.assertEqual(g.do("USE"), "arrived")
        self.assertEqual(self.seen, [("MOVE", "10", "-20"), ("USE",)])
        self.assertFalse((self.state / "agent/cmd.tmp").exists())

    def test_timeout_sends_stop(self):
        self.fake_psdoom(answer=False)
        with mock.patch.object(play.time, "sleep", lambda s: REAL_SLEEP(min(s, 0.01))):
            self.assertEqual(play.Game().do("ATTACK", 5, timeout=0.2), "timeout")
            REAL_SLEEP(0.1)
        self.assertEqual([s[0] for s in self.seen], ["ATTACK", "STOP"])

    def test_no_game_running(self):
        with self.assertRaisesRegex(RuntimeError, "isn't running in Claude-plays mode"):
            play.Game().state()


class MCPServerTest(StateDirTest):
    """`play.py mcp` over stdio, as Claude Code starts it."""

    def test_stdio_server(self):
        (self.state / "agent/state.json").write_text(json.dumps(game_state(monsters=[monster(1002)])))
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                "garbage",
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "look", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 5, "method": "resources/list"}]
        stdin = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in msgs)
        r = subprocess.run([sys.executable, str(ROOT / "play.py"), "mcp"], input=stdin, capture_output=True,
                           text=True, timeout=30, env={**os.environ, "CONNECT_DOOM_STATE": str(self.state)})
        replies = {m["id"]: m for m in map(json.loads, r.stdout.splitlines())}
        self.assertEqual(sorted(replies), [1, 2, 3, 4, 5])                  # nothing for the notification
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "doom")
        tools = {t["name"]: t for t in replies[2]["result"]["tools"]}
        self.assertEqual(set(tools), {"look", "go_to", "attack"})
        self.assertEqual(tools["attack"]["inputSchema"]["required"], ["pid"])
        look = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual(look["visible_monsters"][0]["record"], "Acme Corp")
        self.assertTrue(replies[4]["result"]["isError"])
        self.assertIn("Error: 'nope'", replies[4]["result"]["content"][0]["text"])
        self.assertEqual(replies[5]["error"]["code"], -32601)


class RunTest(StateDirTest):
    """`play.py run` with a fake `claude` CLI."""

    STREAM = [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5",
         "tools": ["mcp__doom__look", "mcp__connectai__queryData", "Read"]},
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Taking down Acme."}}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Taking down Acme."},
            {"type": "tool_use", "id": "t1", "name": "mcp__doom__attack", "input": {"pid": 1002}},
            {"type": "tool_use", "id": "t2", "name": "mcp__connectai__queryData",
             "input": {"query": "SELECT 1", "catalogName": "Zendesk1"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": '{"result": "killed"}'},
            {"type": "tool_result", "tool_use_id": "t2", "is_error": False,
             "content": [{"type": "text", "text": "Id\n1\n2\n\nQuery ID: q"}]}]}},
        {"type": "result", "result": "Cleared.", "total_cost_usd": 0.3, "duration_ms": 9000, "num_turns": 4},
    ]

    def play(self, creds, fail=False):
        bin_ = self.state / "bin"
        bin_.mkdir(exist_ok=True)
        (bin_ / "claude").write_text(FAKE_CLAUDE)
        (bin_ / "claude").chmod(0o755)
        (self.state / "stream.json").write_text(json.dumps(self.STREAM if not fail else []))
        env = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
               "CONNECT_DOOM_STATE": str(self.state), "FAKE_CLAUDE_LOG": str(self.state / "call.json"),
               "FAKE_CLAUDE_STREAM": str(self.state / "stream.json")}
        env.pop("CONNECT_AI_USER", None)
        env.pop("CONNECT_AI_PAT", None)
        env.pop("FAKE_CLAUDE_FAIL", None)
        if creds:
            env.update(CONNECT_AI_USER=USER, CONNECT_AI_PAT=PAT)
        if fail:
            env["FAKE_CLAUDE_FAIL"] = "1"
        r = subprocess.run([sys.executable, str(ROOT / "play.py"), "run"], env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.call = json.loads((self.state / "call.json").read_text())
        self.config = json.loads((self.state / "play-mcp.json").read_text())
        return [json.loads(line) for line in (self.state / "play.jsonl").read_text().splitlines()]

    def test_run_with_connect_ai(self):
        events = self.play(creds=True)
        argv = self.call["argv"]
        prompt = argv[1]
        self.assertIn("Who is at risk?", prompt)
        self.assertIn("room 1: Salesforce, Deals slipping (yellow door, read + write)", prompt)
        self.assertIn("room 2: Zendesk, Open escalations (blue door, read only)", prompt)
        self.assertIn("PII vault behind a red door", prompt)
        self.assertEqual(argv[argv.index("--allowedTools") + 1:argv.index("--allowedTools") + 3],
                         ["mcp__doom", "mcp__connectai"])
        self.assertIn("--strict-mcp-config", argv)
        servers = self.config["mcpServers"]
        self.assertEqual(servers["doom"]["args"][1], "mcp")
        self.assertIn("?ops=", servers["connectai"]["url"])
        self.assertNotIn("execute", servers["connectai"]["url"].lower())
        self.assertNotIn(PAT, json.dumps(self.config))
        self.assertTrue(self.call["basic"])

        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["question", "session", "text_delta", "thought", "call", "call", "result", "result",
                                 "done"])
        self.assertEqual(events[0]["text"], "Claude plays: Who is at risk?")
        self.assertEqual(events[1]["tools"], ["mcp__doom__look", "mcp__connectai__queryData"])
        calls = [e for e in events if e["kind"] == "call"]
        self.assertEqual([(c["server"], c["tool"], c["connection"]) for c in calls],
                         [("doom", "attack", None), ("connectai", "queryData", "Zendesk1")])
        results = {e["id"]: e for e in events if e["kind"] == "result"}
        self.assertEqual((results["t1"]["text"], results["t2"]["rows"]), ('{"result": "killed"}', 2))
        self.assertEqual(events[-1]["text"], "Cleared.")

    def test_demo_mode_has_only_doom_tools(self):
        self.play(creds=False)
        self.assertEqual(list(self.config["mcpServers"]), ["doom"])
        self.assertIsNone(self.call["basic"])

    def test_failed_claude_is_logged(self):
        events = self.play(creds=False, fail=True)
        self.assertEqual(events[-1]["kind"], "failed")
        self.assertIn("boom", events[-1]["text"])


if __name__ == "__main__":
    unittest.main()
