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
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import compat
import connect_ai_mcp

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
from themes import MONSTERS, room_themes  # noqa: E402  (each room's look and monsters)

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CONNECT_DOOM_STATE", ROOT / "state"))
CACHE = STATE / "cache.json"
KILLS = STATE / "kills.jsonl"
HITS = STATE / "hits.jsonl"
LOOKUPS = STATE / "lookups.jsonl"
VAULT = STATE / "vault.jsonl"  # each time the red door is tried: Connect AI's live answer
VAULT_CHECK = STATE / "vault-checking.json"   # while the red door waits on Connect AI
LABELS = STATE / "labels.tsv"  # psdoom's PSDOOM_LABELS: the record under the crosshair
MISSION = STATE / "mission.json"
SAVED = STATE / "saved-records.json"  # the last live refresh, for playing a level without Wi-Fi
GAME = STATE / "game.json"  # written by raid.sh while psdoom runs: {"mode": "player" | "claude", "started": epoch}
ASK = STATE / "ask.json"  # a question asked from the panel, for raid.sh's next level: {"question", "at"}
ASKING = STATE / "asking.json"  # raid.sh moves ask.json here while Claude briefs it
ASK_FAILED = STATE / "ask-failed.json"  # that briefing failed, and the last level came back
MAX_QUESTION = 500
RAID = "python raid.py" if compat.WINDOWS else "./raid.sh"   # the launcher, as the panel's messages name it
MAX_PER_ROOM = int(os.environ.get("MAX_PER_ROOM", "20"))
# Seconds to wait on each Connect AI call during a game. On bad conference Wi-Fi a short wait
# means the game starts on the last records fetched instead of hanging before it opens.
TIMEOUT = float(os.environ.get("CONNECT_AI_TIMEOUT", "30"))


# ---------------------------------------------------------------- monsters

def monster_id(record_id, room):
    """psdoom wants an int PID; derive a stable one whose remainder mod 3 is the room."""
    return (334 + int(hashlib.sha1(record_id.encode()).hexdigest(), 16) % 299000) * 3 + room


def short_name(label):
    """psdoom shows at most 12 characters and no spaces: the first real word, e.g. DESERT
    (skipping prefixes like '2025' or 'Q4-'), and the next one after a short word when both
    fit, e.g. GREAT-LAKES."""
    words = [w for w in ("".join(ch for ch in w if ch.isalpha()) for w in (label or "?").replace("-", " ").split()) if w]
    i = next((i for i, w in enumerate(words) if len(w) >= 3), None)
    if i is None:
        return words[0][:12].upper() if words else "X"
    name = words[i]
    if len(name) < 6 and i + 1 < len(words) and len(name) + 1 + len(words[i + 1]) <= 12:
        name += "-" + words[i + 1]
    return name[:12].upper()


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


# The offline level: the live demo's question, answered from a sample CRM, help desk and billing
# account in the same shapes the live queries return (brief.py), so it looks and plays like the
# real thing. Every company is invented. Some show up in more than one app, as at-risk customers
# do: a slipping deal, an angry ticket and an overdue invoice. Days are counted back from today.
SAMPLE_DEALS = [   # account, amount, stage, pushed count, or days past its close date
    ("Harborview Medical Group", 128400, "Negotiation/Review", 3, 0),
    ("Ironclad Manufacturing", 142000, "Id. Decision Makers", 4, 0),
    ("Meridian Hotel Partners", 112600, "Perception Analysis", 1, 24),
    ("Cascade Freight Logistics", 96750, "Proposal/Price Quote", 1, 16),
    ("Tidewater Ports Authority", 95000, "Id. Decision Makers", 0, 10),
    ("Lakeshore Insurance Group", 87500, "Negotiation/Review", 2, 0),
    ("Bluewater Credit Union", 74200, "Negotiation/Review", 2, 0),
    ("Oakmont Senior Living", 71400, "Negotiation/Review", 3, 0),
    ("Prairie State Utilities", 66300, "Value Proposition", 1, 0),
    ("Summit Ridge School District", 58900, "Perception Analysis", 0, 14),
    ("Copperline Mining", 51250, "Proposal/Price Quote", 1, 0),
    ("Granite Peak Energy", 45000, "Value Proposition", 0, 0),
    ("Redwood Analytics", 39800, "Qualification", 0, 0),
    ("Silverline Pharmacy", 33900, "Needs Analysis", 1, 0),
]
SAMPLE_TICKETS = [   # subject, priority, status, days open
    ("Harborview Medical: SSO logins failing for 40 users", "urgent", "open", 2),
    ("Ironclad Manufacturing: asking to cancel after a third outage", "urgent", "open", 1),
    ("Cascade Freight: renewal on hold until the outage credit lands", "urgent", "pending", 6),
    ("Tidewater Ports: API rate limits blocking dispatch", "urgent", "open", 3),
    ("Summit Ridge Schools: roster sync missing 300 students", "urgent", "open", 4),
    ("Lakeshore Insurance: audit log gaps flagged by compliance", "urgent", "hold", 9),
    ("Meridian Hotels: duplicate charges on the September invoice", "high", "open", 5),
    ("Bluewater CU: nightly data export failing", "high", "open", 7),
    ("Oakmont Senior Living: reports dashboard timing out", "high", "pending", 3),
    ("Prairie State Utilities: field app crashes on login", "high", "open", 2),
    ("Copperline Mining: webhook retries piling up", "high", "open", 8),
    ("Redwood Analytics: SAML certificate expires Friday", "high", "new", 1),
    ("Granite Peak Energy: invoice PDF missing the PO number", "high", "open", 11),
]
SAMPLE_INVOICES = [   # customer, amount due, status, payment attempts
    ("Harborview Medical Group", 18450, "open", 3),
    ("Lakeshore Insurance Group", 15600, "open", 1),
    ("Cascade Freight Logistics", 12300, "open", 1),
    ("Meridian Hotel Partners", 9870, "open", 2),
    ("Riverbend Credit Partners", 7325, "open", 2),
    ("Northgate Property Management", 6750, "open", 1),
    ("Atlas Field Services", 4200, "uncollectible", 4),
    ("Oakmont Senior Living", 3980, "open", 3),
    ("Sunbelt Roofing Supply", 2240, "open", 1),
    ("Pinecrest Dental Group", 1499, "open", 1),
    ("Brightwater Veterinary", 1499, "open", 2),
    ("Evergreen Pediatrics", 1499, "open", 1),
    ("Crescent City Bakery", 899, "uncollectible", 5),
]


def demo_mission(today=None):
    """The offline level (no Connect AI): the sample account above, as the live demo would find it."""
    from brief import DEFAULT_QUESTION   # here, not at the top: the game runs this file every tick
    today = today or dt.date.today()
    day = lambda n: (lambda d: f"{d:%b} {d.day}")(today - dt.timedelta(days=n))   # %-d is glibc/BSD only
    deals = [{"Id": f"006S{i:03d}", "Label": name,
              "Detail": f"${amount:,}, {stage}, " + (f"close date passed {day(late)}" if late else f"pushed {pushed}x"),
              "Hot": int(late > 0 or pushed >= 2)}
             for i, (name, amount, stage, pushed, late) in enumerate(SAMPLE_DEALS)]
    tickets = [{"Id": str(48210 + i), "Label": subject, "Detail": f"{priority} {status}, since {day(days)}",
                "Hot": int(priority == "urgent")}
               for i, (subject, priority, status, days) in enumerate(SAMPLE_TICKETS)]
    invoices = [{"Id": f"in_S{i:03d}", "Label": name,
                 "Detail": f"${amount:,} unpaid, {status}, {attempts} attempt{'s' * (attempts != 1)}",
                 "Hot": int(attempts >= 2 or status == "uncollectible")}
                for i, (name, amount, status, attempts) in enumerate(SAMPLE_INVOICES)]
    rooms = [{"connection": "Salesforce", "app": "Salesforce", "title": "Deals slipping", "writable": True,
              "rows": deals},
             {"connection": "Zendesk", "app": "Zendesk", "title": "Escalated tickets", "writable": True,
              "rows": tickets},
             {"connection": "Stripe", "app": "Stripe", "title": "Unpaid invoices", "writable": False,
              "rows": invoices}]
    answer = ("Harborview Medical Group is the clearest risk: a $128K deal pushed three times, an urgent ticket "
              "with SSO logins failing for 40 users, and an $18K invoice three payment attempts overdue. "
              "Ironclad Manufacturing is asking to cancel after a third outage, Cascade Freight's renewal is on "
              "hold until an outage credit lands, and Meridian Hotel Partners has a $113K deal past its close "
              "date and a disputed invoice. The SSN lookup in PostgreSQL PII was not run: Connect AI blocked it "
              "with 'PII detected in inbound query', and I did not try to get around that.")
    request = {"query": "SELECT [customer_id], [full_name], [email] FROM [PostgreSQLPII].[public].[customers] "
                        "WHERE [ssn_text] = @ssn",
               "parameters": {"@ssn": "123-45-6789"}, "parameterTypes": {"@ssn": "VARCHAR"}}
    return {"question": DEFAULT_QUESTION, "answer": answer, "rooms": rooms,
            "denied": [{"connection": "PostgreSQLPII", "tool": "queryData", "request": request,
                        "error": "Request blocked: PII detected in inbound query"}],
            "masked": [], "calls": []}


def saved_data():
    """The Options menu's saved data switch: play the last saved records and call Connect AI for
    nothing. From the running game (agent/state.json), else the booth session's last switches
    (options.json, before the game is up), else the launch flag."""
    for path in (STATE / "agent" / "state.json", STATE / "options.json"):
        opts = read_json(path)
        if opts is not None:
            return bool(opts.get("options", opts).get("saved_data"))
    return os.environ.get("PSDOOM_OPT_SAVED", "0") not in ("", "0")


def is_offline(mission):
    """The offline mission (poll --demo) carries its sample rows; a live one has queries."""
    return bool(mission and mission.get("rooms")) and all("rows" in r for r in mission["rooms"])


def write_cache(monsters, source, rooms=None, fetched_at=None):
    """rooms: each room's last refresh through Connect AI ({connection, ms, rows, error})."""
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"source": source, "fetched_at": fetched_at or time.time(), "rooms": rooms or [],
                               "monsters": monsters}, indent=1))
    compat.replace(tmp, CACHE)  # atomic, so `list` never reads a half-written file
    tmp = LABELS.with_suffix(".tmp")
    tmp.write_text("".join(f"{m['pid']}\t{line}\n" for m in monsters for line in [aim_label(m)]))
    compat.replace(tmp, LABELS)


def aim_label(m):
    """The two lines psdoom shows while the crosshair is on this monster."""
    clean = lambda s: " ".join(str(s).split())  # no tabs or newlines in the file's fields
    return clean(f"{m['app']}: {m['label']}") + "\t" + clean(("URGENT: " if m["demon"] else "") + m["detail"])


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):  # PermissionError: Windows, mid-replace
        return None


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def game_kills():
    """The kills that count: this game's while one runs (state/game.json), so every game
    starts with every record alive; otherwise all of them."""
    game = read_json(GAME)
    since = game["started"] if game else 0
    return [k for k in read_jsonl(KILLS) if dt.datetime.fromisoformat(k["killed_at"]).timestamp() >= since]


def killed_ids():
    return {k["id"] for k in game_kills()}


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
    record = {**m, "killed_at": dt.datetime.now().isoformat(timespec="seconds"), "mode": "read-only"}
    append(KILLS, record)
    print(kill_message(m), flush=True)
    print(f"\n  KILL  {m['app']}  {m['label']}  ({m['detail']})  {m['id']}  [{record['mode']}]\n",
          file=sys.stderr)
    mission = read_json(MISSION)
    if mission and not is_offline(mission) and os.environ.get("CONNECT_AI_PAT") and not saved_data():
        # offline there is no Connect AI to ask; and the game must not wait on the network
        subprocess.Popen([sys.executable, __file__, "lookup", m["id"]], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill_message(m):
    """The in-game line for a kill (patched pr_kill shows stdout's first line; Doom's font
    is upper case): the record and how many are left in its room, or a room or level clear."""
    mission = read_json(MISSION) or {}
    dead = killed_ids()
    alive = [x for x in cached_monsters() if x["id"] not in dead]
    in_room = [x for x in alive if x["pid"] % 3 == m["pid"] % 3]
    if not alive:
        return "ALL RECORDS CLEARED!" + (" NOW TRY THE RED DOOR" if mission.get("denied") else "")
    if not in_room:
        return f"{m['app']} room clear! {len(alive)} left elsewhere".upper()
    return f"{m['app']}: {m['label']}"[:36].upper() + f", {len(in_room)} LEFT"


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
        rows = connect_ai_mcp.Client(timeout=TIMEOUT).query(sql)
        entry["row"] = to_monster(rows[0], 0, room) if rows else None
    except Exception as e:  # report it on the panel; the game is unaffected
        entry["error"] = str(e).split("\n")[0][:200]
    entry["ms"] = round((time.time() - start) * 1000)
    entry["at"] = time.time()
    append(LOOKUPS, entry)


def cmd_vault(_):
    """The red door was tried: send the request Connect AI blocked during the briefing again,
    live, and log what it says now (the side panel shows it)."""
    mission = read_json(MISSION) or {}
    d = next((d for d in mission.get("denied", []) if d.get("request")), None)
    if not d or is_offline(mission) or saved_data():   # offline and saved data ask Connect AI nothing
        return
    entry = {"connection": d["connection"], "tool": d["tool"], "request": d["request"]}
    start = time.time()
    # Connect AI can take half a minute to refuse: the panel says it's checking meanwhile
    VAULT_CHECK.write_text(json.dumps({"connection": d["connection"], "at": start}))
    try:
        entry["text"] = connect_ai_mcp.Client(timeout=TIMEOUT).call(d["tool"], **d["request"])[:300]
        entry["blocked"] = False
    except Exception as e:
        entry["text"] = str(e).split("\n")[0][:200]
        entry["blocked"] = isinstance(e, connect_ai_mcp.MCPError)
    finally:
        VAULT_CHECK.unlink(missing_ok=True)
    entry["ms"] = round((time.time() - start) * 1000)
    entry["at"] = time.time()
    append(VAULT, entry)


def cmd_hit(args):
    m = find(int(args[0]))
    if m:
        append(HITS, {"id": m["id"], "at": time.time()})


def fetch(mission, stats=None):
    """Re-run every room's query through the Connect AI MCP server. With a stats list,
    append each room's round trip to it."""
    if is_offline(mission):
        return [to_monster(row, i, r) for i, r in enumerate(mission["rooms"]) for row in r["rows"]]
    def one(i, room):
        start = time.time()
        stat, found = {"connection": room["connection"]}, []
        try:
            rows = connect_ai_mcp.Client(timeout=TIMEOUT).query(room["query"])[:MAX_PER_ROOM]
            stat["rows"] = len(rows)
            found = [to_monster(row, i, room) for row in rows]
        except connect_ai_mcp.MCPError as e:
            print(f"{room['app']}: {e}", file=sys.stderr)
            stat["error"] = str(e).split("\n")[0][:200]
        stat["ms"] = round((time.time() - start) * 1000)
        stat["at"] = time.time()
        return found, stat

    # the rooms at once: a slow Wi-Fi costs one time limit, not one per room
    rooms = mission["rooms"][:3]
    with concurrent.futures.ThreadPoolExecutor(len(rooms) or 1) as pool:
        results = [f.result() for f in [pool.submit(one, i, r) for i, r in enumerate(rooms)]]
    monsters = []
    for found, stat in results:
        monsters += found
        if stats is not None:
            stats.append(stat)
    return monsters


def mission_key(mission):
    """Which level a set of records belongs to: its rooms' queries."""
    rooms = [(r["connection"], r.get("query")) for r in (mission or {}).get("rooms", [])]
    return hashlib.sha1(json.dumps(rooms).encode()).hexdigest()[:12]


def restore_saved(mission, source="saved"):
    """No Connect AI: put back the records this level's last good refresh saved. Returns
    when they were fetched, or None if this level has none. source "saved-data" is the
    Options menu's saved data switch rather than Connect AI not answering."""
    saved = read_json(SAVED)
    if not saved or saved.get("mission") != mission_key(mission):
        return None
    write_cache(saved["monsters"], source, saved.get("rooms"), fetched_at=saved["fetched_at"])
    return saved["fetched_at"]


def cmd_poll(args):
    """Refresh the cache. `poll --demo` plays the offline sample level; `poll --once` exits after one fetch,
    and if Connect AI can't be reached, falls back to the records this level last fetched
    (exit 3 when there are none)."""
    interval = int(os.environ.get("POLL_SECONDS", "15"))
    if "--demo" in args:
        MISSION.write_text(json.dumps(demo_mission(), indent=1))
    while True:
        started = time.time()
        now = dt.datetime.now()
        mission = None
        try:
            mission = json.loads(MISSION.read_text())
            if "--demo" not in args and not is_offline(mission) and saved_data():
                at = restore_saved(mission, "saved-data")
                if at is not None:   # the menu's saved data switch: Connect AI isn't asked
                    print(f"{now:%H:%M:%S} saved data: the records from {dt.datetime.fromtimestamp(at):%b %d %H:%M}",
                          file=sys.stderr)
                    if "--once" in args:
                        return
                    time.sleep(interval)
                    continue
                print(f"{now:%H:%M:%S} saved data is on, but this level has no saved records: asking Connect AI",
                      file=sys.stderr)
            stats = []
            monsters = fetch(mission, stats)
            if stats and all("error" in s for s in stats):
                raise connect_ai_mcp.MCPError(stats[0]["error"])  # nothing answered: keep what we have
            source = "demo" if "--demo" in args else "connect-ai-mcp"
            write_cache(monsters, source, stats)
            if source == "connect-ai-mcp" and monsters:  # the fallback for a game without Wi-Fi
                tmp = SAVED.with_suffix(".tmp")
                tmp.write_text(json.dumps({"mission": mission_key(mission), "fetched_at": time.time(),
                                           "rooms": stats, "monsters": monsters}))
                compat.replace(tmp, SAVED)
            print(f"{now:%H:%M:%S} {source}: {len(monsters)} monsters", file=sys.stderr)
        except Exception as e:  # keep the last good cache; the game keeps running
            print(f"{now:%H:%M:%S} poll failed: {e}", file=sys.stderr)
            if "--once" in args:
                at = restore_saved(mission)
                if at is None:
                    sys.exit(3)
                print(f"Can't reach Connect AI: playing the records saved {dt.datetime.fromtimestamp(at):%b %d %H:%M}.",
                      file=sys.stderr)
        if "--once" in args or "--demo" in args:
            return
        time.sleep(max(1, interval - (time.time() - started)))  # a refresh every 15 s, not 15 s apart


def cmd_check(_):
    """For raid.sh doctor: run each room's query once, timed, and say what a game would play
    without Connect AI. One `status<TAB>text` line each; status is ok, slow, error or note."""
    mission = read_json(MISSION)
    if not mission or is_offline(mission):
        return
    mcp = connect_ai_mcp.Client(timeout=TIMEOUT)
    for room in mission["rooms"][:3]:
        start = time.time()
        try:
            rows = mcp.query(room["query"])
            secs = time.time() - start
            status = "slow" if secs > TIMEOUT / 2 else "ok"
            print(f"{status}\t{room['app']}: {len(rows)} records in {secs:.1f} s"
                  + (f", close to the game's {TIMEOUT:g} s limit" if status == "slow" else ""), flush=True)
        except Exception as e:
            print(f"error\t{room['app']}: {str(e).splitlines()[0][:120]}", flush=True)
    saved = read_json(SAVED)
    if saved and saved.get("mission") == mission_key(mission):
        print(f"note\twithout Wi-Fi, games play this level's records saved "
              f"{dt.datetime.fromtimestamp(saved['fetched_at']):%b %d %H:%M}")
    else:
        print("note\twithout Wi-Fi, games play offline: play this level once with Wi-Fi to save its records")


def cmd_score(_):
    kills = game_kills()
    left = len([m for m in cached_monsters() if m["id"] not in killed_ids()])
    print(f"{len(kills)} cleared, {left} still standing")
    for k in kills[-10:]:
        print(f"  {k['killed_at']}  {k['app']:<18} {k['label'][:40]:<40} {k['detail'][:30]}  [{k['mode']}]")


def can_ask():
    """Why a new question can't be asked from the panel right now, or None if it can. raid.sh
    sets RAID_PID: it runs the briefing between games, which needs Connect AI and Claude Code."""
    if not os.environ.get("RAID_PID"):
        return f"New questions work while {RAID} runs the game."
    if not os.environ.get("CONNECT_AI_PAT"):
        return f"New questions need a Connect AI account: run {RAID} login, then {RAID} new."
    if not shutil.which("claude"):
        return "New questions need Claude Code's CLI (claude), signed in."
    return None


def ask_state():
    """The panel's question box: can it ask, the default question, and a question underway or failed."""
    from brief import DEFAULT_QUESTION   # here, not at the top: the game runs this file every tick
    failed = read_json(ASK_FAILED)
    return {"unavailable": can_ask(), "default": DEFAULT_QUESTION,
            "pending": (read_json(ASKING) or read_json(ASK) or {}).get("question"),
            "failed": failed if failed and time.time() - failed.get("at", 0) < 120 else None}


def ask(question):
    """A question from the panel: hand it to raid.sh and end this game, so the next one is the
    level Claude builds for it. Returns an HTTP status and a reply."""
    why = can_ask()
    if why:
        return 409, {"error": why}
    if not question:
        return 400, {"error": "Type a question first."}
    if len(question) > MAX_QUESTION:
        return 400, {"error": f"Keep the question under {MAX_QUESTION} characters."}
    if ASK.exists() or ASKING.exists():
        return 409, {"error": "Claude is already building a level for another question."}
    if not GAME.exists():
        return 409, {"error": "Wait for the game to start, then ask."}
    tmp = ASK.with_suffix(".tmp")
    tmp.write_text(json.dumps({"question": question, "at": time.time()}))
    compat.replace(tmp, ASK)
    ASK_FAILED.unlink(missing_ok=True)
    # psdoom ignores SIGTERM; raid.sh sees ask.json once the game is gone. On Windows there's no
    # pkill: raid.py keeps the game's pid in state/psdoom.pid.
    if compat.WINDOWS:
        pid = (STATE / "psdoom.pid").read_text().strip() if (STATE / "psdoom.pid").exists() else ""
        if pid:
            subprocess.run(["taskkill", "/F", "/PID", pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
    else:
        subprocess.run(["pkill", "-9", "-P", os.environ["RAID_PID"], "-x", "psdoom"], check=False)
    return 202, {"question": question}


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

        def end_headers(self):
            # The stage's web view otherwise keeps an old panel page after an update.
            if self.path.split("?")[0] != "/state.json":
                self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def do_GET(self):
            if self.path.split("?")[0] != "/state.json":
                return super().do_GET()
            try:
                mission = json.loads(MISSION.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                mission = None
            game = read_json(GAME)
            checking = read_json(VAULT_CHECK)
            kills = game_kills()      # while a game runs, only that game's kills and hits
            dead = {k["id"] for k in kills}
            monsters = cached_monsters()
            since = game["started"] if game else 0
            by_id = {m["id"]: m for m in monsters}
            hits = [{**by_id[h["id"]], "at": h["at"]} for h in read_jsonl(HITS)
                    if h["at"] >= since and h["id"] in by_id]
            looked = {e["id"]: e for e in read_jsonl(LOOKUPS)}
            kills = [{**k, "lookup": looked.get(k["id"])} for k in kills]
            refreshed = {r["connection"]: r for r in (read_json(CACHE) or {}).get("rooms", [])}
            rooms = []
            mission_rooms = (mission or {}).get("rooms", [])[:3]
            for i, (room, theme) in enumerate(zip(mission_rooms, room_themes(mission_rooms))):
                mine = [m for m in monsters if m["pid"] % 3 == i]   # monster_id puts the room in pid mod 3
                alive = [m for m in mine if m["id"] not in dead]
                record, urgent = ({"key": k, "name": MONSTERS[k]["name"]} for k in theme["monsters"])
                rooms.append({"app": room.get("app", room["connection"]), "writable": room.get("writable", False),
                              "title": room.get("title", ""), "left": len(alive),
                              "theme": theme["label"], "monster": record, "urgent_monster": urgent,
                              "urgent_left": sum(m["demon"] for m in alive),
                              "killed": sum(m["id"] in dead for m in mine),
                              "refresh": refreshed.get(room["connection"])})
            body = json.dumps({
                # the briefing log belongs to the real level, not the offline sample in its place
                "events": [] if is_offline(mission) else live_events(STATE / "run.jsonl"),
                "play_events": live_events(STATE / "play.jsonl"),
                "mission": mission and {k: mission[k] for k in ("question", "answer", "rooms", "denied", "masked")},
                "game": game,
                "kills": kills[-12:],
                "kill_total": len(kills),  # the list is capped, so the panel counts and redraws by this
                "hits": hits[-40:],
                "rooms_live": rooms,
                # where the monsters came from: connect-ai-mcp (live), saved (Connect AI unreachable) or demo
                "records": {k: (read_json(CACHE) or {}).get(k) for k in ("source", "fetched_at")},
                "vault": [v for v in read_jsonl(VAULT) if v["at"] >= since][-5:],
                "vault_checking": checking if checking and time.time() - checking["at"] < 120 else None,
                "standing": len([m for m in monsters if m["id"] not in dead]),
                "ask": ask_state(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.split("?")[0] != "/ask":
                return self.send_error(404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                question = " ".join(str(body.get("question", "")).split())
            except (ValueError, AttributeError):
                question = ""
            status, reply = ask(question)
            data = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    print(f"panel on http://localhost:{port}", file=sys.stderr)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def cmd_reset(_):
    for p in (KILLS, HITS, LOOKUPS, VAULT):
        p.unlink(missing_ok=True)
    print("kills cleared")


COMMANDS = {"list": cmd_list, "kill": cmd_kill, "hit": cmd_hit, "lookup": cmd_lookup, "vault": cmd_vault,
            "poll": cmd_poll, "check": cmd_check,
            "score": cmd_score, "reset": cmd_reset, "panel": cmd_panel}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: {Path(sys.argv[0]).name} {{{'|'.join(COMMANDS)}}} [args]")
    COMMANDS[sys.argv[1]](sys.argv[2:])
