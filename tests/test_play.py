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
                 (-150, 600): "room 1", (1200, 600): "room 1", (1880, 300): "room 2", (3250, 1200): "room 2",
                 (3920, 300): "room 3",                                  # each room's scenery ring
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
                               "logged": "kill logged; the record is being read again, live, through Connect AI "
                                         "(nothing is written)"})
        out = play.tool_attack(FakeGame(game_state(), {("ATTACK", 1003): "lost sight"}), {"pid": 1003})
        self.assertNotIn("logged", out)

    def test_go_to_keycards_from_the_lobby(self):
        g = FakeGame(game_state())
        self.assertEqual(play.tool_go_to(g, {"place": "keycards"})["result"], "arrived")
        self.assertEqual(g.commands, [("MOVE", 2560, -600),
                                      ("MOVE", 2496, -656), ("MOVE", 2624, -656)])   # blue, yellow

    def test_go_to_keycards_skips_unneeded_keys(self):
        (self.state / "mission.json").write_text(json.dumps({**MISSION, "rooms": MISSION["rooms"][1:]}))
        g = FakeGame(game_state(y=-200))
        play.tool_go_to(g, {"place": "keycards"})
        self.assertIn(("MOVE", 2496, -656), g.commands)
        self.assertNotIn(("MOVE", 2624, -656), g.commands)

    def test_keycard_spots_match_the_level(self):
        """One key color in use sits at the first spot, whichever color it is (tools/make_arena.py)."""
        self.assertEqual(play.key_spots([{"writable": True}, {"writable": True}]), [(2496, -656)])
        self.assertEqual(play.key_spots([{"writable": False}, {"writable": True}]), [(2496, -656), (2624, -656)])

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
        g = FakeGame(game_state(x=896, y=-470), {("MOVE", 896, -560): "stuck"})   # at the red door
        out = play.tool_go_to(g, {"place": "the vault"})
        self.assertIn("There is no red keycard", out["result"])
        self.assertIn("Request blocked: PII detected in inbound query", out["result"])
        self.assertNotIn("\nx", out["result"])
        self.assertIn(("TURN", 270), g.commands)

    def test_vault_refusal_only_at_the_red_door(self):
        """Hung up on the way there, it says so, not that Connect AI refused."""
        g = FakeGame(game_state(x=2560, y=-200), {("MOVE", 896, -256): "stuck"})
        self.assertEqual(play.tool_go_to(g, {"place": "vault"})["result"], "Couldn't get there (stuck).")

    def test_a_blocked_move_steps_back_and_tries_again(self):
        results = iter(["blocked", "arrived", "arrived"])
        g = FakeGame(game_state(x=559, y=-2))           # wedged on room 1's door frame
        g.do = lambda verb, *a, timeout=25: (g.commands.append((verb, *a)), next(results))[1]
        self.assertEqual(play.move(g, 2560, -256), "arrived")
        self.assertEqual(g.commands, [("MOVE", 2560, -256), ("MOVE", 559, -256), ("MOVE", 2560, -256)])
        g = FakeGame(game_state(monsters=[monster(1002, distance=60)]), {("MOVE", 1, 2): "blocked"})
        self.assertEqual(play.move(g, 1, 2), "blocked")  # a monster in the way: the caller says so
        self.assertEqual(g.commands, [("MOVE", 1, 2)])

    def test_attack_goes_in_through_the_door(self):
        far = {**monster(1002, visible=False), "x": 512, "y": 500}               # in room 1, out of sight
        g = FakeGame(game_state(x=2560, y=-256, keys=("yellow",), monsters=[far]))
        play.tool_attack(g, {"pid": 1002})
        self.assertIn(("USE",), g.commands)                                      # room 1's door
        self.assertEqual(g.commands[-1], ("ATTACK", 1002))
        g = FakeGame(game_state(x=512, y=300, monsters=[far]))                  # already in its room
        play.tool_attack(g, {"pid": 1002})
        self.assertEqual(g.commands, [("ATTACK", 1002)])

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

    def test_seq_beats_another_clients(self):
        """A run that starts after another sent commands must not reuse its seqs."""
        g = play.Game()
        (self.state / "agent/state.json").write_text(json.dumps(
            {**game_state(done_seq=0), "command": {"seq": g.seq + 500, "done_seq": g.seq + 500, "result": "x"}}))
        g.do("STOP", timeout=0)
        self.assertEqual(int((self.state / "agent/cmd").read_text().split()[0]), g.seq)
        self.assertGreater(g.seq, int(time.time()) + 500)

    def test_no_game_running(self):
        with self.assertRaisesRegex(RuntimeError, "isn't running in Claude-plays mode"):
            play.Game().state()


class FakeProc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, _timeout=None):
        return self.returncode


class SupervisorTest(StateDirTest):
    """play.py loop: follows the Options menu's Claude plays and Auto replay switches."""

    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.procs = []
        self.game = FakeGame(game_state(monsters=[monster(1002), monster(1003)]))
        self.game.do_nowait = lambda verb: self.game.commands.append((verb,))
        self.net = True
        self.players = []
        self.sup = play.Supervisor(self.game, start=self.start, clock=lambda: self.now, online=lambda: self.net)

    def start(self, player):
        self.players.append(player)
        self.procs.append(FakeProc())
        return self.procs[-1]

    def switches(self, claude, replay=False):
        self.game.s["options"] = {"claude_plays": int(claude), "auto_replay": int(replay)}

    def tick(self, seconds=0.5):
        self.now += seconds
        self.sup.tick()

    def mode(self):
        return json.loads((self.state / "game.json").read_text())["mode"]

    def test_switching_claude_on_and_off(self):
        self.switches(False)
        self.tick()
        self.assertEqual((self.procs, self.mode()), ([], "player"))
        self.switches(True)
        self.tick()
        self.tick()
        self.assertEqual((len(self.procs), self.mode()), (1, "claude"))     # one run, not one per tick
        self.switches(False)
        self.tick()
        self.assertEqual((self.procs[0].returncode, self.mode()), (-15, "player"))
        self.assertEqual(self.game.commands, [("STOP",)])                    # the marine stops where it is
        self.switches(True)
        self.tick()
        self.assertEqual(len(self.procs), 2)                                 # back on: a new run

    def test_a_finished_run_waits_without_auto_replay(self):
        self.switches(True)
        self.tick()
        self.procs[0].returncode = 0
        self.tick(600)
        self.assertEqual(len(self.procs), 1)
        self.assertNotIn(("RESTART",), self.game.commands)

    def test_auto_replay_after_the_level_is_clear(self):
        self.switches(True, replay=True)
        self.tick()
        started = json.loads((self.state / "game.json").read_text())["started"]
        for m in self.game.s["monsters"]:
            m["health"] = 0
        self.tick()
        self.tick(play.REPLAY_GRACE - 1)                 # time for the vault and a summary
        self.assertEqual((len(self.procs), self.game.commands), (1, []))
        self.tick(2)
        self.assertEqual(self.game.commands, [("STOP",), ("RESTART",)])
        self.assertEqual(self.procs[0].returncode, -15)
        self.assertEqual(len(self.procs), 2)
        self.assertGreater(json.loads((self.state / "game.json").read_text())["started"], started)  # records back

    def test_auto_replay_after_a_run_ends(self):
        self.switches(True, replay=True)
        self.tick()
        self.tick(120)
        self.procs[0].returncode = 0                     # out of turns, level not clear
        self.tick()
        self.tick(play.REPLAY_PAUSE)
        self.assertEqual(self.game.commands, [("RESTART",)])
        self.assertEqual(len(self.procs), 2)

    def test_a_claude_run_that_dies_at_once_hands_over_to_the_offline_player(self):
        self.switches(True)
        self.tick()
        self.procs[0].returncode = 1                     # say, claude isn't signed in
        self.tick()
        self.assertEqual((self.players, self.mode()), (["claude", "replay"], "replay"))
        self.procs[1].returncode = 0                     # the offline run ends; Claude is tried again later
        self.tick(400)
        self.switches(False)
        self.tick()
        self.switches(True)
        self.tick()
        self.assertEqual(self.players[-1], "claude")

    def test_a_claude_run_that_fails_mid_game_hands_over_without_auto_replay(self):
        self.switches(True)
        self.tick()
        self.tick(300)
        self.procs[0].returncode = 2                     # play.py run: claude exited with an error
        self.tick()
        self.assertEqual(self.players, ["claude", "replay"])
        self.assertNotIn(("RESTART",), self.game.commands)  # it carries on from where the marine stands

    def test_losing_the_network_mid_run_swaps_claude_out(self):
        self.switches(True)
        self.tick()
        self.tick(60)
        self.net = False
        self.tick(10)                                    # offline: noticed
        self.tick(10)
        self.assertEqual(len(self.procs), 1)             # a blip is fine
        self.tick(10)                                    # 20 s without a network
        self.assertEqual(self.procs[0].returncode, -15)
        self.assertEqual((self.players, self.mode()), (["claude", "replay"], "replay"))

    def test_a_network_blip_keeps_claude(self):
        self.switches(True)
        self.tick()
        self.tick(60)
        self.net = False
        self.tick(10)
        self.net = True
        self.tick(10)
        self.tick(30)
        self.assertEqual(self.players, ["claude"])

    def test_an_offline_run_that_dies_at_once_backs_off(self):
        self.net = False
        self.switches(True, replay=True)
        self.tick()
        self.procs[0].returncode = 1
        self.tick()
        self.tick(30)
        self.assertEqual(len(self.procs), 1)
        self.tick(31)
        self.assertEqual(self.players, ["replay", "replay"])

    def test_the_offline_player_without_a_network(self):
        self.net = False
        self.switches(True)
        self.tick()
        self.assertEqual((self.players, self.mode()), (["replay"], "replay"))

    def test_the_offline_player_on_the_offline_level(self):
        (self.state / "mission.json").write_text(json.dumps(
            {**MISSION, "rooms": [{**r, "rows": []} for r in MISSION["rooms"]]}))
        self.switches(True)
        self.tick()
        self.assertEqual(self.players, ["replay"])

    def test_player_replay_forces_the_offline_player(self):
        with mock.patch.object(play, "PLAYER", "replay"):
            self.switches(True)
            self.tick()
        self.assertEqual(self.players, ["replay"])

    def test_idle_hands_the_game_to_claude(self):
        self.switches(False, replay=True)
        self.game.s["idle_seconds"] = play.IDLE_SECONDS - 1
        self.tick()
        self.assertEqual(self.game.commands, [])
        self.game.s["idle_seconds"] = play.IDLE_SECONDS
        self.tick()
        self.assertEqual(self.game.commands, [("OPTION", 0, 1)])
        self.switches(False)                             # without auto replay, the game waits for a person
        self.game.commands.clear()
        self.tick()
        self.assertEqual(self.game.commands, [])

    def test_a_level_without_records_is_never_clear(self):
        self.game.s["monsters"] = []
        self.switches(True, replay=True)
        self.tick()
        self.tick(600)
        self.assertEqual((len(self.procs), self.game.commands), (1, []))

    def test_waits_for_the_game(self):
        self.game.state = mock.Mock(side_effect=RuntimeError("not yet"))
        self.tick()
        self.assertEqual(self.procs, [])


class AutopilotTest(StateDirTest):
    """play.py replay: the offline player, against a fake game where every attack kills."""

    def setUp(self):
        super().setUp()
        self.game = FakeGame(game_state(monsters=[monster(1002, distance=500), monster(1003, distance=200)]))
        self.feed = []
        real_do = self.game.do

        def do(verb, *args, timeout=25):
            if verb == "ATTACK":
                next(m for m in self.game.s["monsters"] if m["pid"] == args[0])["health"] = 0
            return real_do(verb, *args, timeout=timeout)
        self.game.do = do

    def play(self):
        play.Autopilot(self.game, log=lambda kind, **d: self.feed.append({"kind": kind, **d}),
                       sleep=lambda _s: None).play()
        return [e["text"] for e in self.feed if e["kind"] == "thought"]

    def record(self, events):
        folder = self.state / "recordings" / play.level_key(MISSION)
        folder.mkdir(parents=True)
        (folder / "1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    def test_plays_the_level_and_writes_a_claude_feed(self):
        said = self.play()
        kinds = [e["kind"] for e in self.feed]
        self.assertEqual(kinds[:2], ["question", "session"])
        self.assertEqual(kinds[-1], "done")
        self.assertIn("offline replay", self.feed[0]["text"])
        self.assertIn("text_delta", kinds)                                   # streamed, like Claude's text
        calls = [(e["tool"], e["args"]) for e in self.feed if e["kind"] == "call"]
        self.assertEqual(calls[1], ("go_to", {"place": "keycards"}))
        self.assertIn(("attack", {"pid": 1002}), calls)
        self.assertEqual(calls[-1], ("go_to", {"place": "vault"}))
        self.assertTrue(any("Acme Corp" in t and "rgent" in t for t in said))  # urgent: a demon
        self.assertTrue(any("Initech" in t for t in said))
        self.assertIn("PII detected in inbound query", said[-2])
        self.assertTrue(said[-1].startswith("Cleared 2 of 2 records across Salesforce, Zendesk, 1 of them urgent."))
        self.assertEqual(self.feed[-1]["turns"], len(calls))

    def test_narrates_with_claudes_lines_from_a_real_run(self):
        self.record([{"kind": "thought", "text": "Done with that. Acme Corp's $45K renewal is urgent, taking it down."},
                     {"kind": "call", "tool": "attack", "args": {"pid": 1002}},
                     {"kind": "thought", "text": "Looking around."},
                     {"kind": "call", "tool": "look", "args": {}},
                     {"kind": "call", "tool": "attack", "args": {"pid": 1003}}])   # no line right before it
        self.assertEqual(play.recorded_lines(MISSION, play.records()),
                         {1002: "Acme Corp's $45K renewal is urgent, taking it down."})
        said = self.play()
        self.assertIn("Acme Corp's $45K renewal is urgent, taking it down.", said)

    def test_finished_runs_are_saved_for_the_level(self):
        (self.state / "play.jsonl").write_text('{"kind": "call", "tool": "look"}\n')
        play.save_recording(MISSION)                                     # nothing worth keeping
        self.assertFalse((self.state / "recordings").exists())
        (self.state / "play.jsonl").write_text('{"kind": "call", "tool": "attack", "args": {"pid": 1}}\n')
        play.save_recording(MISSION)
        saved = list((self.state / "recordings" / play.level_key(MISSION)).glob("*.jsonl"))
        self.assertEqual(len(saved), 1)
        self.assertNotEqual(play.level_key(MISSION), play.level_key({**MISSION, "question": "Other?"}))


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
        {"type": "stream_event", "event": {"delta": {"type": "thinking_delta", "thinking": "Acme first."}}},
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Taking down Acme."}}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "Acme first.", "signature": "x"},
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
        self.assertEqual(r.returncode, 2 if fail else 0, r.stderr)    # 2: the supervisor swaps Claude out
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
        self.assertEqual(kinds, ["question", "session", "reasoning_delta", "text_delta", "reasoning", "thought",
                                 "call", "call", "result", "result", "done"])
        self.assertIn("Before EVERY tool call", prompt)                # the feed narrates each decision
        self.assertEqual(events[0]["text"], "Claude plays: Who is at risk?")
        self.assertEqual(events[1]["tools"], ["mcp__doom__look", "mcp__connectai__queryData"])
        calls = [e for e in events if e["kind"] == "call"]
        self.assertEqual([(c["server"], c["tool"], c["connection"]) for c in calls],
                         [("doom", "attack", None), ("connectai", "queryData", "Zendesk1")])
        results = {e["id"]: e for e in events if e["kind"] == "result"}
        self.assertEqual((results["t1"]["text"], results["t2"]["rows"]), ('{"result": "killed"}', 2))
        self.assertEqual(events[-1]["text"], "Cleared.")
        self.assertEqual(len(list((self.state / "recordings").glob("*/*.jsonl"))), 1)   # for the offline player

    def test_demo_mode_has_only_doom_tools(self):
        self.play(creds=False)
        self.assertEqual(list(self.config["mcpServers"]), ["doom"])
        self.assertIsNone(self.call["basic"])

    def test_failed_claude_is_logged(self):
        events = self.play(creds=False, fail=True)
        self.assertEqual(events[-1]["kind"], "failed")
        self.assertIn("boom", events[-1]["text"])
        self.assertFalse((self.state / "recordings").exists())


if __name__ == "__main__":
    unittest.main()
