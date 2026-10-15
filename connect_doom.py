#!/usr/bin/env python3
"""Bridge between psdoom-ng and CData Connect AI.

psdoom-ng calls three commands (see vendor/src/psdoom-ng/trunk/src/pr_process.c):
  PSDOOMPSCMD      -> `connect_doom.py list`       one line per monster: user id name demon(0|1)
  PSDOOMKILLCMD    -> `connect_doom.py kill <id>`  a monster died
  PSDOOMRENICECMD  -> `connect_doom.py hit <id>`   a monster was wounded

psdoom runs these synchronously inside the game loop, so they only touch local
files; `kill` hands the record's live read to a background `lookup`. `poll` is the slow part: it re-runs each room's query from
state/mission.json (written by brief.py) through the Connect AI MCP server and
writes state/cache.json. Room i's monsters get ids with id % 3 == i, which is
how psdoom picks the arena they spawn in.

The source data is never modified. A kill is logged to state/kills.jsonl and
hidden from later lists (otherwise psdoom resurrects the monster). Then `lookup`
reads that one record again through Connect AI, with the room's own query, and
logs the row and the round trip to state/lookups.jsonl for the side panel.
"""
import datetime as dt
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import connect_ai_mcp

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CONNECT_DOOM_STATE", ROOT / "state"))
CACHE = STATE / "cache.json"
KILLS = STATE / "kills.jsonl"
HITS = STATE / "hits.jsonl"
LOOKUPS = STATE / "lookups.jsonl"
LABELS = STATE / "labels.tsv"  # psdoom's PSDOOM_LABELS: the record under the crosshair
MISSION = STATE / "mission.json"
GAME = STATE / "game.json"  # written by raid.sh while psdoom runs: {"mode": "player" | "claude", "started": epoch}
MAX_PER_ROOM = int(os.environ.get("MAX_PER_ROOM", "20"))


# ---------------------------------------------------------------- monsters

def monster_id(record_id, room):
    """psdoom wants an int PID; derive a stable one whose remainder mod 3 is the room."""
    return (334 + int(hashlib.sha1(record_id.encode()).hexdigest(), 16) % 299000) * 3 + room


def short_name(label):
    """psdoom shows at most 7 characters and no spaces: the first real word, e.g. DESERT
    (skipping prefixes like '2025' or 'Q4-REN-')."""
    words = ["".join(ch for ch in w if ch.isalpha()) for w in (label or "?").replace("-", " ").split()]
    return next((w for w in words if len(w) >= 3), next((w for w in words if w), "X"))[:7].upper()


def to_monster(row, room_index, room):
    # Connect AI keeps a plain column's own name ("[Subject] AS [Label]" comes back as Subject),
    # so a missing Label is the one column that isn't Id, Detail or Hot.
    if not row.get("Label"):
        rest = [v for k, v in row.items() if k not in ("Id", "Label", "Detail", "Hot")]
        row = {**row, "Label": rest[0] if len(rest) == 1 else None}
    rid = f"{room['connection']}:{row.get('Id')}"
    return {
        "pid": monster_id(rid, room_index),
        "id": rid,
        "app": room.get("app", room["connection"]),
        "writable": room.get("writable", False),
        "label": row.get("Label") or "?",
        "detail": row.get("Detail") or "",
        "name": short_name(row.get("Label")),
        "demon": 1 if str(row.get("Hot", "0")).strip() in ("1", "True", "true") else 0,
    }


def demo_mission():
    """Made-up rooms for offline play (no Connect AI, no real customer data)."""
    rng = random.Random(42)
    words = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Vandelay", "Wonka", "Stark",
             "Tyrell", "Cyberdyne", "Soylent", "Wayne", "Oscorp", "Gringotts", "Monarch"]
    rooms = [{"connection": "DemoCRM", "app": "Demo CRM", "title": "Deals slipping", "writable": True},
             {"connection": "DemoDesk", "app": "Demo Helpdesk", "title": "Open escalations", "writable": True},
             {"connection": "DemoBilling", "app": "Demo Billing", "title": "Overdue invoices", "writable": False}]
    for r in rooms:
        r["rows"] = [{"Id": f"{r['connection']}{i}", "Label": f"{rng.choice(words)} Corp",
                      "Detail": f"${rng.choice([5969, 15127, 33077, 76718])}", "Hot": rng.choice([0, 1])}
                     for i in range(12)]
    return {"question": "(demo)", "answer": "Made-up data for offline play.", "rooms": rooms,
            "denied": [{"connection": "DemoPII", "error": "Request blocked: PII detected in inbound query"}],
            "masked": [], "calls": []}


def write_cache(monsters, source):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"source": source, "fetched_at": time.time(), "monsters": monsters}, indent=1))
    tmp.replace(CACHE)  # atomic, so `list` never reads a half-written file
    tmp = LABELS.with_suffix(".tmp")
    tmp.write_text("".join(f"{m['pid']}\t{line}\n" for m in monsters for line in [aim_label(m)]))
    tmp.replace(LABELS)


def aim_label(m):
    """The two lines psdoom shows while the crosshair is on this monster."""
    clean = lambda s: " ".join(str(s).split())  # no tabs or newlines in the file's fields
    return clean(f"{m['app']}: {m['label']}") + "\t" + clean(("URGENT: " if m["demon"] else "") + m["detail"])


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def killed_ids():
    return {k["id"] for k in read_jsonl(KILLS)}


def cached_monsters():
    try:
        return json.loads(CACHE.read_text())["monsters"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return []


def find(pid):
    return next((m for m in cached_monsters() if m["pid"] == pid), None)


def append(path, record):
    STATE.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------- commands

def cmd_list(_):
    dead = killed_ids()
    for m in cached_monsters():
        if m["id"] not in dead:
            print(f"connectai {m['pid']} {m['name']} {m['demon']}")


def cmd_kill(args):
    m = find(int(args[0]))
    if not m:
        return
    record = {**m, "killed_at": dt.datetime.now().isoformat(timespec="seconds"), "mode": "dry-run"}
    append(KILLS, record)
    # stdout's first line is shown in-game (patched pr_kill); Doom's font is upper case.
    print(f"{m['app']}: {m['label']}"[:44].upper() + ", KILLED", flush=True)
    print(f"\n  KILL  {m['app']}  {m['label']}  ({m['detail']})  {m['id']}  [{record['mode']}]\n",
          file=sys.stderr)
    mission = read_json(MISSION)
    live = mission and not all("rows" in r for r in mission["rooms"]) and os.environ.get("CONNECT_AI_PAT")
    if live:  # the demo has no Connect AI to ask; the game must not wait on the network
        subprocess.Popen([sys.executable, __file__, "lookup", m["id"]], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def lookup_sql(room, record_id):
    rid = record_id.replace("'", "''")
    return f"SELECT * FROM ({room['query']}) AS q WHERE q.[Id] = '{rid}'"


def cmd_lookup(args):
    """Read one killed record live through Connect AI, with the query that built its room."""
    record_id = args[0]
    connection = record_id.split(":", 1)[0]
    room = next((r for r in read_json(MISSION)["rooms"] if r["connection"] == connection), None)
    if not room:
        return
    sql = lookup_sql(room, record_id.split(":", 1)[1])
    entry = {"id": record_id, "connection": connection, "sql": sql}
    start = time.time()
    try:
        rows = connect_ai_mcp.Client().query(sql)
        entry["row"] = to_monster(rows[0], 0, room) if rows else None
    except Exception as e:  # report it on the panel; the game is unaffected
        entry["error"] = str(e).split("\n")[0][:200]
    entry["ms"] = round((time.time() - start) * 1000)
    entry["at"] = time.time()
    append(LOOKUPS, entry)


def cmd_hit(args):
    m = find(int(args[0]))
    if m:
        append(HITS, {"id": m["id"], "at": time.time()})


def fetch(mission):
    """Re-run every room's query through the Connect AI MCP server."""
    if all("rows" in r for r in mission["rooms"]):     # the demo mission carries its rows
        return [to_monster(row, i, r) for i, r in enumerate(mission["rooms"]) for row in r["rows"]]
    mcp = connect_ai_mcp.Client()
    monsters = []
    for i, room in enumerate(mission["rooms"][:3]):
        try:
            rows = mcp.query(room["query"])[:MAX_PER_ROOM]
        except connect_ai_mcp.MCPError as e:
            print(f"{room['app']}: {e}", file=sys.stderr)
            continue
        monsters += [to_monster(row, i, room) for row in rows]
    return monsters


def cmd_poll(args):
    """Refresh the cache. `poll --demo` uses made-up data; `poll --once` exits after one fetch."""
    interval = int(os.environ.get("POLL_SECONDS", "15"))
    if "--demo" in args:
        MISSION.write_text(json.dumps(demo_mission(), indent=1))
    while True:
        now = dt.datetime.now()
        try:
            mission = json.loads(MISSION.read_text())
            monsters = fetch(mission)
            source = "demo" if "--demo" in args else "connect-ai-mcp"
            write_cache(monsters, source)
            print(f"{now:%H:%M:%S} {source}: {len(monsters)} monsters", file=sys.stderr)
        except Exception as e:  # keep the last good cache; the game keeps running
            print(f"{now:%H:%M:%S} poll failed: {e}", file=sys.stderr)
        if "--once" in args or "--demo" in args:
            return
        time.sleep(interval)


def cmd_score(_):
    kills = read_jsonl(KILLS)
    left = len([m for m in cached_monsters() if m["id"] not in killed_ids()])
    print(f"{len(kills)} cleared, {left} still standing")
    for k in kills[-10:]:
        print(f"  {k['killed_at']}  {k['app']:<18} {k['label'][:40]:<40} {k['detail'][:30]}  [{k['mode']}]")


def cmd_panel(args):
    """Serve the side panel (panel/index.html) and the run's state on localhost."""
    import http.server

    port = int(args[0]) if args else 8666

    def live_events(path):
        """A run's event log; skips a last line that's still being written."""
        events = []
        for line in path.read_text().splitlines() if path.exists() else []:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return events

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(ROOT / "panel"), **kw)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.split("?")[0] != "/state.json":
                return super().do_GET()
            try:
                mission = json.loads(MISSION.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                mission = None
            game = read_json(GAME)
            kills = read_jsonl(KILLS)
            dead = {k["id"] for k in kills}
            monsters = cached_monsters()
            # While a game runs, the panel shows only that game's kills and hits.
            since = game["started"] if game else 0
            kills = [k for k in kills if dt.datetime.fromisoformat(k["killed_at"]).timestamp() >= since]
            by_id = {m["id"]: m for m in monsters}
            hits = [{**by_id[h["id"]], "at": h["at"]} for h in read_jsonl(HITS)
                    if h["at"] >= since and h["id"] in by_id]
            looked = {e["id"]: e for e in read_jsonl(LOOKUPS)}
            kills = [{**k, "lookup": looked.get(k["id"])} for k in kills]
            killed_now = {k["id"] for k in kills}
            rooms = []
            for i, room in enumerate((mission or {}).get("rooms", [])[:3]):
                mine = [m for m in monsters if m["pid"] % 3 == i]   # monster_id puts the room in pid mod 3
                alive = [m for m in mine if m["id"] not in dead]
                rooms.append({"app": room.get("app", room["connection"]), "writable": room.get("writable", False),
                              "title": room.get("title", ""), "left": len(alive),
                              "urgent_left": sum(m["demon"] for m in alive),
                              "killed": sum(m["id"] in killed_now for m in mine)})
            body = json.dumps({
                "events": live_events(STATE / "run.jsonl"),
                "play_events": live_events(STATE / "play.jsonl"),
                "mission": mission and {k: mission[k] for k in ("question", "answer", "rooms", "denied", "masked")},
                "game": game,
                "kills": kills[-12:],
                "kill_total": len(kills),  # the list is capped, so the panel counts and redraws by this
                "hits": hits[-40:],
                "rooms_live": rooms,
                "standing": len([m for m in monsters if m["id"] not in dead]),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    print(f"panel on http://localhost:{port}", file=sys.stderr)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def cmd_reset(_):
    for p in (KILLS, HITS, LOOKUPS):
        p.unlink(missing_ok=True)
    print("kills cleared")


COMMANDS = {"list": cmd_list, "kill": cmd_kill, "hit": cmd_hit, "lookup": cmd_lookup, "poll": cmd_poll,
            "score": cmd_score, "reset": cmd_reset, "panel": cmd_panel}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: {Path(sys.argv[0]).name} {{{'|'.join(COMMANDS)}}} [args]")
    COMMANDS[sys.argv[1]](sys.argv[2:])
