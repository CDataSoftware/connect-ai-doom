#!/usr/bin/env python3
"""Claude plays: an optional mode where Claude drives the marine.

  play.py loop    started by raid.sh with every game: follows the Options menu's
                  "Claude plays" and "Auto replay" switches (see supervise())
  play.py run     start Claude Code with two MCP servers, Doom (this file) and Connect AI,
                  and stream what it does to state/play.jsonl for the side panel
  play.py replay  the offline stand-in for `run`, with no Claude and no network: a scripted
                  player that narrates with Claude's own lines from earlier runs (see Autopilot)
  play.py mcp     the Doom MCP server (stdio), started by Claude Code

The game side is the agent channel patched into psdoom (pr_process.c): we write a
command to state/agent/cmd and read state/agent/state.json. Tools are high level
(go to a room, attack a monster) because Claude thinks in seconds, not tics.
"""
import hashlib
import json
import os
import random
import re
import signal
import socket
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
OFFLINE_AFTER = float(os.environ.get("OFFLINE_AFTER", "20"))  # seconds without a network before Claude is swapped out
IDLE_SECONDS = float(os.environ.get("IDLE_SECONDS", "60"))   # with auto replay: no input this long, Claude plays
PLAYER = os.environ.get("PLAYER", "claude")                  # "replay": always the offline player
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
        return f"room {min(max(int((x + 512) // 2048), 0), 2) + 1}"   # a room and its scenery ring
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


def close_monsters(s, within=200):
    return [mm["pid"] for mm in sorted(s["monsters"], key=lambda mm: mm["distance"])
            if mm["health"] > 0 and mm["distance"] < within]


def open_floor(p):
    """Somewhere with room to turn, near the marine: the middle of its room, the lobby, or the hall."""
    here = where(p)
    if here.startswith("room"):
        return ARENA_X[int(here[-1]) - 1] + 512, 512
    if here == "lobby":
        return 2560, -704
    if here == "vault door":
        return 896, -256
    return p["x"], -256    # the hall, or a doorway off it


def move(game, x, y):
    """MOVE; if the marine is hung up on a door frame or a wall with no monster in the way
    (say, after chasing a target round a corner), step back to open floor and try once more."""
    result = game.do("MOVE", x, y)
    if result == "blocked" and not close_monsters(game.state()):
        game.do("MOVE", *open_floor(game.state()["player"]))
        result = game.do("MOVE", x, y)
    return result


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
            result = move(game, *step)
            if result != "arrived":
                s = game.state()
                need = None
                if "vault" in target and where(s["player"]) == "vault door":
                    return {"result": "The red door won't open. There is no red keycard: "
                                      f"Connect AI refused this request. {m['denied'][0]['error'].splitlines()[0]}",
                            "you_are_in": where(s["player"])}
                if -100 < step[1] < 300:
                    i = room_index(target, m)
                    need = "yellow" if i is not None and m["rooms"][i].get("writable") else "blue"
                    if not s["player"]["keys"].get(need):
                        return {"result": f"Door locked: it needs the {need} keycard. Go to the keycards first.",
                                "you_are_in": where(s["player"])}
                close = close_monsters(s)
                if close:
                    return {"result": f"Blocked: monsters in the way ({len(close)} close by). Attack them, then try again.",
                            "you_are_in": where(s["player"]), "closest_pids": close[:5]}
                return {"result": f"Couldn't get there ({result}).", "you_are_in": where(s["player"])}
    s = game.state()
    return {"result": "arrived", "you_are_in": where(s["player"]), "keycards": [k for k, v in s["player"]["keys"].items() if v]}


def tool_attack(game, args):
    pid = int(args.get("pid", 0))
    rec = records().get(pid, {})
    s = game.state()
    target = next((mm for mm in s["monsters"] if mm["pid"] == pid and mm["health"] > 0), None)
    if target and not target["visible"] and "x" in target:
        # ATTACK closes in on a target out of sight in a straight line; from the hall or another
        # room that runs into the wall beside a door, so go in through the door first
        there = where(target)
        if there.startswith("room") and there != where(s["player"]):
            tool_go_to(game, {"place": there})
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
Urgent records (urgent: true, the biggest monster in each room) matter most. Clear the urgent
records first, then whatever is left.

Use the doom tools: look, go_to, attack. Pick up the keycards first. Then go room by room.
People watch your feed live next to the game. Before EVERY tool call, write ONE short line, as if
narrating a stream, saying what you're doing and why. Before an attack, name the record and why it
matters (for example: "Desert Power's renewal ticket is urgent, taking it down."); before a move,
say where and why (for example: "Salesforce is cleared, heading to Zendesk next."). After a few
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
        sys.exit(2)    # the supervisor hands the game to the offline player
    elif out["finished"]:
        save_recording(m)


# ---------------------------------------------------------------- offline: Claude plays, replayed

def level_key(m):
    """Which level a recording is for: the question and its rooms (a new briefing, a new key)."""
    rooms = [(r["app"], r["title"]) for r in m["rooms"]]
    return hashlib.sha1(json.dumps([m["question"], rooms]).encode()).hexdigest()[:12]


def save_recording(m):
    """Keep a finished run's feed in state/recordings/<level>/, so the offline player can narrate
    this level in Claude's words."""
    feed = PLAY_LOG.read_text()
    if '"tool": "attack"' not in feed:
        return
    folder = STATE / "recordings" / level_key(m)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{int(time.time())}.jsonl").write_text(feed)


def recorded_lines(m, recs):
    """pid -> what Claude wrote just before attacking that record in an earlier real run of this
    level: the sentences that name the record (a line can also mention the one before)."""
    lines = {}
    for path in sorted((STATE / "recordings" / level_key(m)).glob("*.jsonl")):
        said = None
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e["kind"] == "thought":
                said = e["text"]
            elif e["kind"] == "call":
                pid = e["args"].get("pid") if e["tool"] == "attack" else None
                label = recs.get(pid, {}).get("label")
                if said and label:
                    named = [t for t in re.split(r"(?<=[.!?])\s+", said) if label in t]
                    if named:
                        lines[pid] = " ".join(named)
                said = None
    return lines


URGENT_LINES = ["{label}: {detail}. Urgent, taking it down.", "{label} is urgent ({detail}), going for it.",
                "Next, {label}: {detail}. That one's urgent.", "{label} can't wait: {detail}."]
OTHER_LINES = ["{label}, {detail}. Taking it down.", "{label} next ({detail}).", "Clearing {label}: {detail}.",
               "Now {label}: {detail}."]
AGAIN_LINES = ["Another one from {label}: {detail}.", "{label} again: {detail}.",
               "{label} has more: {detail}."]


class Autopilot:
    """The offline player: the same Doom tools Claude uses, in a fixed plan (keycards, each room
    with the urgent records first, the vault, a summary), writing the same feed a Claude run
    writes. Each attack is narrated with Claude's own line for that record from an earlier
    real run of this level when there is one, else a line built from the record. It never
    calls Connect AI, so it needs no network."""

    def __init__(self, game, log=None, pace=1.0, sleep=time.sleep):
        self.game, self.log, self.pace, self.sleep = game, log or event, pace, sleep
        self.calls = 0
        self.rng = random.Random()

    def wait(self, lo, hi):
        self.sleep(self.rng.uniform(lo, hi) * self.pace)

    def say(self, text):
        """Stream a line into the feed a few words at a time, as Claude's text arrives."""
        self.wait(1.2, 2.6)    # thinking
        words = text.split(" ")
        for i in range(0, len(words), 3):
            self.log("text_delta", text=" ".join(words[i:i + 3]) + (" " if i + 3 < len(words) else ""))
            self.sleep(0.06 * self.pace)
        self.log("thought", text=text)

    def use(self, tool, **args):
        self.calls += 1
        cid = f"replay_{self.calls}"
        self.log("call", id=cid, server="doom", tool=tool, args=args, connection=None)
        start = time.time()
        out = TOOLS[tool][0](self.game, args)
        self.log("result", id=cid, tool=tool, connection=None, error=False, rows=0, text=json.dumps(out),
                 ms=round((time.time() - start) * 1000))
        self.wait(0.4, 1.0)
        return out

    def line(self, pid):
        self.turn += 1
        if pid in self.lines:
            return self.lines[pid]
        r = self.recs.get(pid)
        if not r:
            return "Another record in the way, taking it down."
        detail = str(r.get("detail") or "")[:80]
        seen = self.named.get(r["label"], 0)
        self.named[r["label"]] = seen + 1
        lines = AGAIN_LINES if seen else URGENT_LINES if r.get("demon") else OTHER_LINES
        return lines[self.turn % len(lines)].format(label=r["label"], detail=detail).replace(", .", ".")

    def attack(self, pid):
        self.say(self.line(pid))
        out = self.use("attack", pid=pid)
        if out["result"] == "killed":
            self.killed.append(pid)
        return out["result"]

    def alive(self, room=None):
        s = self.game.state()
        return [mm for mm in s["monsters"] if mm["health"] > 0 and (room is None or mm["pid"] % 3 == room)]

    def go(self, place, tries=4):
        for _ in range(tries):
            out = self.use("go_to", place=place)
            if out["result"] == "arrived" or "red door" in out["result"]:
                return out
            for pid in out.get("closest_pids", [])[:3]:
                self.attack(pid)
        return out

    def clear(self, room):
        failed = set()
        for n in range(60):
            left = [mm for mm in self.alive(room) if mm["pid"] not in failed]
            if not left:
                return
            if n % 5 == 4:
                self.use("look")
            # what Claude does: what it can see, urgent first, then the nearest
            left.sort(key=lambda mm: (not mm["visible"], not self.recs.get(mm["pid"], {}).get("demon"),
                                      mm["distance"]))
            if self.attack(left[0]["pid"]) not in ("killed", "gone"):
                failed.add(left[0]["pid"])

    def play(self):
        m = mission()
        self.recs, started = records(), time.time()
        self.lines, self.killed, self.named, self.turn = recorded_lines(m, self.recs), [], {}, 0
        self.log("question", text="Claude plays (offline replay): " + m["question"])
        self.log("session", model="offline replay", tools=["mcp__doom__attack", "mcp__doom__go_to", "mcp__doom__look"])
        total = len(self.alive())
        self.use("look")
        apps = [r["app"] for r in m["rooms"]]
        self.say(f"{len(apps)} rooms: {', '.join(apps)}. Keycards first, then the urgent records, room by room.")
        self.go("keycards")
        for i, r in enumerate(m["rooms"]):
            if not self.alive(i):
                continue
            self.say(f"Room {i + 1}, {r['app']}: {r['title']}.")
            self.go(f"room {i + 1}")
            self.clear(i)
        if m.get("denied"):
            self.say("The rooms are done. Trying the PII vault's red door once, to see what Connect AI does.")
            self.go("vault", tries=1)
            self.say("Connect AI refused the request: " + m["denied"][0]["error"].splitlines()[0]
                     + ". There's no red keycard, so the vault stays shut.")
        urgent = sum(1 for pid in self.killed if self.recs.get(pid, {}).get("demon"))
        summary = (f"Cleared {total - len(self.alive())} of {total} records across {', '.join(apps)}, "
                   f"{urgent} of them urgent.")
        if m.get("denied"):
            summary += "\nConnect AI blocked the PII vault request, so that data never reached the game."
        self.say(summary)
        self.log("done", cost_usd=None, duration_ms=round((time.time() - started) * 1000), turns=self.calls,
                 text=summary)


def replay():
    claude_run.EventLog(PLAY_LOG).reset()
    Autopilot(Game()).play()


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


def online():
    """Can Claude Code reach Anthropic's API? A plain TCP connect, three seconds at most."""
    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


class Supervisor:
    """Follows the Options menu: Claude plays on starts a run, off stops it. A run is Claude
    (`play.py run`) when it can be, else the offline player (`play.py replay`): with PLAYER=replay,
    on the offline level, with no network, or for 5 minutes after a Claude run failed (it exited
    with an error or within 30 s, say because claude isn't signed in, or the network was gone for
    OFFLINE_AFTER seconds mid-run): the offline player then carries on from where the marine
    stands, auto replay or not. With auto replay on, a level that's clear (after
    REPLAY_GRACE for the vault and a summary) or a run that ended starts over: every record
    back (a new game.json start, so the bridge lists them all again), the level restarted, and
    a new run; and with Claude plays off, IDLE_SECONDS without a key or the mouse hands the
    game to Claude. One step per tick()."""

    def __init__(self, game, start=None, clock=time.time, online=online):
        self.game, self.clock, self.online = game, clock, online
        self.start = start or (lambda player: subprocess.Popen(
            [sys.executable, str(ROOT / "play.py"), "run" if player == "claude" else "replay"]))
        self.proc = self.mode = self.player = self.claude_failed_at = None
        self.started = self.cleared_at = self.ended_at = self.offline_since = None
        self.checked = 0           # when the network was last checked during a Claude run
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
            failed = self.proc.returncode != 0 or now - self.started < 30
            self.proc, self.ended_at = None, now
            if self.player == "claude" and failed:
                self.claude_failed_at, self.ended_at = now, None    # straight to the offline player
        if self.proc and self.player == "claude" and now - self.checked >= 10:
            # claude retries a dead network for minutes; don't leave the marine standing that long
            self.checked = now
            self.offline_since = None if self.online() else self.offline_since or now
            if self.offline_since and now - self.offline_since >= OFFLINE_AFTER:
                self.stop()
                self.claude_failed_at, self.offline_since = now, None

        if not opts.get("claude_plays"):
            if self.proc:
                self.stop()
            self.ended_at = None
            if self.mode != "player":
                set_game(mode="player")
                self.mode = "player"
            if opts.get("auto_replay") and s.get("idle_seconds", 0) >= IDLE_SECONDS:
                self.game.do("OPTION", 0, 1)    # nobody's playing: Claude takes over
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

    def choose(self):
        failed = self.claude_failed_at and self.clock() - self.claude_failed_at < 300
        try:
            offline_level = all("rows" in r for r in mission()["rooms"])   # connect_doom.is_offline
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            offline_level = False
        if PLAYER == "replay" or failed or offline_level or not self.online():
            return "replay"
        return "claude"

    def launch(self):
        self.player = self.choose()
        self.mode = "claude" if self.player == "claude" else "replay"
        set_game(mode=self.mode)
        self.proc, self.started, self.ended_at = self.start(self.player), self.clock(), None

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
    {"run": run, "mcp": serve_mcp, "loop": supervise, "replay": replay}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
