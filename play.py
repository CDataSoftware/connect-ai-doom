#!/usr/bin/env python3
"""Claude plays: an optional mode where Claude drives the marine.

  play.py loop    started by raid.sh with every game: follows the Options menu's
                  "Claude plays" and "Auto replay" switches (see supervise())
  play.py run     start Claude Code with two MCP servers, Doom (this file) and Connect AI,
                  and stream what it does to state/play.jsonl for the side panel
  play.py mcp     the Doom MCP server (stdio), started by Claude Code

The game side is the agent channel patched into psdoom (pr_process.c): we write a
command to state/agent/cmd and read state/agent/state.json. Tools are high level
(go to a room, attack a monster) because Claude thinks in seconds, not tics.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import claude_run

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CONNECT_DOOM_STATE", ROOT / "state"))
AGENT = STATE / "agent"
PLAY_LOG = STATE / "play.jsonl"
MODEL = os.environ.get("PLAY_MODEL", "sonnet")
MAX_TURNS = os.environ.get("PLAY_MAX_TURNS", "60")
REPLAY_GRACE = float(os.environ.get("REPLAY_GRACE", "45"))   # seconds Claude gets after the last kill
REPLAY_PAUSE = float(os.environ.get("REPLAY_PAUSE", "6"))    # between a run's summary and the restart
ARENA_X = (0, 2048, 4096)          # tools/make_arena.py


def key_spots(rooms):
    """Where the lobby's keycards lie: as tools/make_arena.py places them, one per color
    in use, sorted, 128 apart from x=2496."""
    keys = sorted({"yellow" if r.get("writable") else "blue" for r in rooms})
    return [(2496 + 128 * i, -656) for i in range(len(keys))]


# ---------------------------------------------------------------- the game channel

class Game:
    def __init__(self):
        self.seq = int(time.time())  # beats any seq a previous run left in the cmd file

    def state(self):
        for _ in range(20):
            try:
                return json.loads((AGENT / "state.json").read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.1)
        raise RuntimeError("the game isn't running in Claude-plays mode")

    def do(self, verb, *args, timeout=25):
        """Send one command and wait until the game reports it finished."""
        # past any seq another client (say, the run before this one) already sent
        self.seq = max(self.seq, self.state()["command"].get("seq", 0)) + 1
        tmp = AGENT / "cmd.tmp"
        tmp.write_text(f"{self.seq} {verb} {' '.join(str(int(a)) for a in args)}\n")
        tmp.replace(AGENT / "cmd")
        end = time.time() + timeout
        while time.time() < end:
            s = self.state()
            if s["command"]["done_seq"] >= self.seq:
                return s["command"]["result"]
            time.sleep(0.1)
        self.do_nowait("STOP")
        return "timeout"

    def do_nowait(self, verb):
        self.seq += 1
        (AGENT / "cmd").write_text(f"{self.seq} {verb}\n")


def mission():
    return json.loads((STATE / "mission.json").read_text())


def records():
    """pid -> monster record from the bridge's cache (app, label, detail, hot)."""
    try:
        return {m["pid"]: m for m in json.loads((STATE / "cache.json").read_text())["monsters"]}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {}


def where(p):
    x, y = p["x"], p["y"]
    if y >= 0:
        return f"room {min(int(x // 2048), 2) + 1}"
    if y > -448:
        return "hall"
    if 512 <= x <= 1280 and y < -448:
        return "vault door"
    return "lobby"


def room_index(target, m):
    rooms = m["rooms"]
    t = str(target).lower().strip()
    for i, r in enumerate(rooms):
        if t in (str(i + 1), f"room {i + 1}", r["app"].lower(), r["connection"].lower()) or t in r["app"].lower():
            return i
    return None


# ---------------------------------------------------------------- tools

def tool_look(game, _args):
    s = game.state()
    m, recs, p = mission(), records(), s["player"]
    keys = [k for k, v in p["keys"].items() if v]
    rooms = []
    for i, r in enumerate(m["rooms"]):
        alive = [mm for mm in s["monsters"] if mm["health"] > 0 and mm["pid"] % 3 == i]
        rooms.append({"room": i + 1, "app": r["app"], "found": r["title"],
                      "door": "yellow (read + write)" if r.get("writable") else "blue (read only)",
                      "monsters_left": len(alive)})
    visible = []
    for mm in sorted(s["monsters"], key=lambda mm: mm["distance"]):
        if mm["health"] > 0 and mm["visible"]:
            r = recs.get(mm["pid"], {})
            visible.append({"pid": mm["pid"], "app": r.get("app"), "record": r.get("label"),
                            "detail": r.get("detail"), "urgent": bool(r.get("demon")),
                            "distance": mm["distance"]})
    return {"you_are_in": where(p), "health": p["health"], "ammo": p["ammo"], "keycards": keys,
            "rooms": rooms, "pii_vault": bool(m.get("denied")), "visible_monsters": visible[:12]}


def walk(game, points):
    for x, y in points:
        result = game.do("MOVE", x, y)
        if result != "arrived":
            return result, (x, y)
    return "arrived", None


def tool_go_to(game, args):
    target = str(args.get("place", "")).lower()
    s = game.state()
    p = s["player"]
    here = where(p)
    route = []
    # Leave the room you're in first: back out through its door.
    if here.startswith("room"):
        cx = ARENA_X[int(here[-1]) - 1] + 512
        route += [(cx, 120), (cx, -150), (cx, -256)]
    elif here == "lobby":
        route += [(2560, -600), (2560, -256)]
    elif here == "hall":
        route += [(p["x"], -256)]

    m = mission()
    if "key" in target:
        route = [(2560, -600)] if here == "lobby" else route + [(2560, -256), (2560, -600)]
        route += key_spots(m["rooms"])
    elif "vault" in target:
        if not m.get("denied"):
            return {"result": "There is no vault: Connect AI didn't block anything this run."}
        route += [(896, -256), (896, -470), ("TURN", 270), ("USE",), "WAIT", (896, -560)]
    elif "lobby" in target:
        route += [(2560, -256), (2560, -600)]
    else:
        i = room_index(target, m)
        if i is None:
            return {"result": f"Unknown place {target!r}. Try 'keycards', 'room 1', an app name, 'vault' or 'lobby'."}
        cx = ARENA_X[i] + 512
        # Doom's use range is 64 units: stop just short of the door (its face is at y = -48)
        route += [(cx, -256), (cx, -78), ("TURN", 90), ("USE",), "WAIT", (cx, 150), (cx, 400)]

    for step in route:
        if step == "WAIT":
            time.sleep(1.2)  # the door takes about a second to open
        elif step == ("USE",):
            game.do("USE")
        elif step[0] == "TURN":
            game.do("TURN", step[1])  # face the door: use only reaches straight ahead
        else:
            result = game.do("MOVE", *step)
            if result != "arrived":
                s = game.state()
                need = None
                if "vault" in target:
                    return {"result": "The red door won't open. There is no red keycard: "
                                      f"Connect AI refused this request. {m['denied'][0]['error'].splitlines()[0]}",
                            "you_are_in": where(s["player"])}
                if -100 < step[1] < 300:
                    i = room_index(target, m)
                    need = "yellow" if i is not None and m["rooms"][i].get("writable") else "blue"
                    if not s["player"]["keys"].get(need):
                        return {"result": f"Door locked: it needs the {need} keycard. Go to the keycards first.",
                                "you_are_in": where(s["player"])}
                close = [mm["pid"] for mm in sorted(s["monsters"], key=lambda mm: mm["distance"])
                         if mm["health"] > 0 and mm["distance"] < 200]
                if close:
                    return {"result": f"Blocked: monsters in the way ({len(close)} close by). Attack them, then try again.",
                            "you_are_in": where(s["player"]), "closest_pids": close[:5]}
                return {"result": f"Couldn't get there ({result}).", "you_are_in": where(s["player"])}
    s = game.state()
    return {"result": "arrived", "you_are_in": where(s["player"]), "keycards": [k for k, v in s["player"]["keys"].items() if v]}


def tool_attack(game, args):
    pid = int(args.get("pid", 0))
    rec = records().get(pid, {})
    result = game.do("ATTACK", pid, timeout=16)
    out = {"result": result, "app": rec.get("app"), "record": rec.get("label"), "detail": rec.get("detail")}
    if result == "killed":
        out["logged"] = "kill logged; the record is being read again, live, through Connect AI (nothing is written)"
    return out


TOOLS = {
    "look": (tool_look, "See where you are, your keycards, each room's app and monsters left, "
                        "and the monsters you can see right now, each with the business record it stands for.",
             {}),
    "go_to": (tool_go_to, "Walk somewhere. place: 'keycards' (pick up the keycards in the lobby), "
                          "'room 1'..'room 3' or the app's name, 'vault' (the PII vault's red door), or 'lobby'. "
                          "Doors open on arrival if you hold the right keycard.",
              {"place": {"type": "string"}}),
    "attack": (tool_attack, "Aim at a visible monster by pid and fire until it dies (about 10 s at most).",
               {"pid": {"type": "integer"}}),
}


def serve_mcp():
    """A minimal MCP server over stdio (newline-delimited JSON-RPC)."""
    game = Game()
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            continue  # notifications
        if method == "initialize":
            result = {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "doom", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d,
                                 "inputSchema": {"type": "object", "properties": props,
                                                 "required": list(props)}}
                                for n, (_, d, props) in TOOLS.items()]}
        elif method == "tools/call":
            name, args = msg["params"]["name"], msg["params"].get("arguments", {})
            try:
                out = TOOLS[name][0](game, args)
                result = {"content": [{"type": "text", "text": json.dumps(out)}]}
            except Exception as e:
                result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        else:
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": method}}
            print(json.dumps(reply), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)


# ---------------------------------------------------------------- the player

PROMPT = """You're playing Doom, and every monster is a live business record that Connect AI found
for this question: {question}

The level: a lobby with keycards, a hall, and one room per app behind a keycard door.
{rooms}
Pink demons (urgent: true) matter most. Clear the urgent records first, then whatever is left.

Use the doom tools: look, go_to, attack. Pick up the keycards first. Then go room by room.
Before each attack, write ONE short line, as if narrating a stream, naming the record and why
it matters (for example: "Desert Power's renewal ticket is urgent, taking it down."). After a few
kills in a room, you may use the Connect AI tools once to check something related in another app.
{vault}
Keep going until the rooms are clear or you run out of turns. End with a two-line summary."""


def event(kind, **data):
    claude_run.EventLog(PLAY_LOG)(kind, **data)


def run():
    m = mission()
    rooms = "\n".join(f"  room {i + 1}: {r['app']}, {r['title']} "
                      f"({'yellow door, read + write' if r.get('writable') else 'blue door, read only'})"
                      for i, r in enumerate(m["rooms"]))
    vault = ("There is also a PII vault behind a red door. Try it once, near the end, "
             "to see what Connect AI does.") if m.get("denied") else ""
    prompt = PROMPT.format(question=m["question"], rooms=rooms, vault=vault)

    claude_run.EventLog(PLAY_LOG).reset()
    servers = {"doom": {"type": "stdio", "command": sys.executable, "args": [str(ROOT / "play.py"), "mcp"]}}
    env = dict(os.environ)
    if os.environ.get("CONNECT_AI_USER") and os.environ.get("CONNECT_AI_PAT"):   # offline: Doom tools only
        servers["connectai"] = claude_run.connect_ai_server()
        env = claude_run.env(os.environ)
    config = STATE / "play-mcp.json"
    config.write_text(json.dumps({"mcpServers": servers}))
    cmd = ["claude", "-p", prompt, "--model", MODEL, "--max-turns", MAX_TURNS,
           "--mcp-config", str(config), "--strict-mcp-config",
           "--allowedTools", "mcp__doom", "mcp__connectai",
           "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    event("question", text="Claude plays: " + m["question"])
    # raid.sh stops us when the game closes; take Claude down with us.
    out = claude_run.run(cmd, event, env=env, cwd=STATE, on_start=lambda proc: signal.signal(
        signal.SIGTERM, lambda *_: (proc.terminate(), sys.exit(0))))
    if out["returncode"] and not out["finished"]:   # hitting --max-turns also exits non-zero, after a result
        event("failed", text=out["stderr"][-500:])


# ---------------------------------------------------------------- the switches

def set_game(**fields):
    """Update state/game.json, the panel's view of the game (raid.sh writes it at launch)."""
    path = STATE / "game.json"
    try:
        game = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        game = {"mode": "player", "started": time.time()}
    tmp = STATE / "game.json.tmp"
    tmp.write_text(json.dumps({**game, **fields}) + "\n")
    tmp.replace(path)


class Supervisor:
    """Follows the Options menu: Claude plays on starts `play.py run`, off stops it. With
    auto replay on, a level that's clear (after REPLAY_GRACE for the vault and a summary) or
    a run that ended starts over: every record back (a new game.json start, so the bridge
    lists them all again), the level restarted, and a new run. One step per tick()."""

    def __init__(self, game, start=None, clock=time.time):
        self.game, self.clock = game, clock
        self.start = start or (lambda: subprocess.Popen([sys.executable, str(ROOT / "play.py"), "run"]))
        self.proc = self.mode = None
        self.started = self.cleared_at = self.ended_at = None
        self.seen_alive = False    # a level with no records never counts as clear

    def tick(self):
        try:
            s = self.game.state()
        except RuntimeError:
            return  # the game isn't up yet
        opts = s.get("options", {})
        now = self.clock()
        alive = [m for m in s["monsters"] if m["health"] > 0]
        self.seen_alive = self.seen_alive or bool(alive)
        if self.seen_alive and not alive:
            self.cleared_at = self.cleared_at or now
        else:
            self.cleared_at = None
        if self.proc and self.proc.poll() is not None:
            self.proc, self.ended_at = None, now

        if not opts.get("claude_plays"):
            if self.proc:
                self.stop()
            self.ended_at = None
            if self.mode != "player":
                set_game(mode="player")
                self.mode = "player"
            return
        if opts.get("auto_replay"):
            ran_out = self.ended_at and now - self.ended_at >= self.pause()
            won = self.cleared_at and now - self.cleared_at >= REPLAY_GRACE
            if ran_out or won:
                self.replay()
                return
        if not self.proc and not self.ended_at:
            self.launch()

    def pause(self):
        """Wait a bit after a run before the next; much longer if it died right away (say,
        claude isn't signed in), so a booth doesn't spin."""
        quick = self.started and self.ended_at and self.ended_at - self.started < 30
        return 60 if quick else REPLAY_PAUSE

    def launch(self):
        set_game(mode="claude")
        self.mode = "claude"
        self.proc, self.started, self.ended_at = self.start(), self.clock(), None

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        self.game.do_nowait("STOP")

    def replay(self):
        if self.proc:
            self.stop()
        set_game(started=time.time())   # the kill log starts over: the bridge lists every record
        time.sleep(0.5)
        self.game.do("RESTART")
        self.cleared_at = self.ended_at = None
        self.seen_alive = False
        time.sleep(2)                   # the level loads and spawns the records
        self.launch()


def supervise():
    sup = Supervisor(Game())
    signal.signal(signal.SIGTERM, lambda *_: (sup.proc and sup.proc.terminate(), sys.exit(0)))
    while True:
        sup.tick()
        time.sleep(0.5)


if __name__ == "__main__":
    {"run": run, "mcp": serve_mcp, "loop": supervise}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
