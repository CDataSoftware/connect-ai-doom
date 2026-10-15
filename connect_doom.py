#!/usr/bin/env python3
"""Bridge between psdoom-ng and CData Connect AI.

psdoom-ng calls three commands (see vendor/src/psdoom-ng/trunk/src/pr_process.c):
  PSDOOMPSCMD      -> `connect_doom.py list`       one line per monster: user id name demon(0|1)
  PSDOOMKILLCMD    -> `connect_doom.py kill <id>`  a monster died
  PSDOOMRENICECMD  -> `connect_doom.py hit <id>`   a monster was wounded

psdoom runs these synchronously inside the game loop, so they only touch local
files. `poll` is the slow part: it queries Connect AI in the background and
writes state/cache.json.

The source data is never modified. A kill is logged to state/kills.jsonl and
hidden from later lists (otherwise psdoom resurrects the monster). With
SANDBOX=1 a kill also INSERTs a row into SANDBOX_TABLE through Connect AI.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CONNECT_DOOM_STATE", ROOT / "state"))
CACHE = STATE / "cache.json"
KILLS = STATE / "kills.jsonl"
HITS = STATE / "hits.jsonl"

API_URL = os.environ.get("CONNECT_AI_URL", "https://cloud.cdata.com/api/query")
MAX_MONSTERS = int(os.environ.get("MAX_MONSTERS", "40"))
DEMON_DAYS = int(os.environ.get("DEMON_DAYS", "90"))  # this overdue = pink demon
SOURCE_QUERY = os.environ.get(
    "SOURCE_QUERY",
    "SELECT [Id], [CustomerName], [AmountDue], [DueDate] "
    "FROM [Stripe1].[Stripe].[Invoices] WHERE [Status] = 'open' "
    f"ORDER BY [DueDate] ASC LIMIT {MAX_MONSTERS}",
)
SANDBOX_TABLE = os.environ.get("SANDBOX_TABLE", "[Snowflake_WH].[PUBLIC].[DOOM_FOLLOW_UPS]")


# ---------------------------------------------------------------- Connect AI

def credentials():
    user = os.environ.get("CONNECT_AI_USER")
    pat = os.environ.get("CONNECT_AI_PAT")
    if not (user and pat):
        sys.exit("Set CONNECT_AI_USER (account email) and CONNECT_AI_PAT (Settings > Access Tokens).")
    return base64.b64encode(f"{user}:{pat}".encode()).decode()


def run_sql(sql, params=None):
    """POST a SQL statement to the Connect AI query API; return a list of row dicts."""
    body = {"query": sql}
    if params:
        body["parameters"] = {k: {"dataType": 5, "value": v} for k, v in params.items()}
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Basic {credentials()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Connect AI HTTP {e.code}: {e.read().decode(errors='replace')[:500]}")
    if payload.get("error"):
        raise RuntimeError(f"Connect AI error: {payload['error']}")
    rows = []
    for result in payload.get("results", []):
        cols = [c["columnName"] for c in result.get("schema", [])]
        rows += [dict(zip(cols, r)) for r in result.get("rows", [])]
    return rows


# ---------------------------------------------------------------- monsters

def monster_id(record_id):
    """psdoom wants an int PID; derive a stable one from the record id."""
    return 1000 + int(hashlib.sha1(record_id.encode()).hexdigest(), 16) % 899000


def short_name(customer, dollars):
    """psdoom shows at most 7 characters and no spaces: initials + amount, e.g. CSI1499."""
    initials = "".join(w[0] for w in customer.split() if w[:1].isalnum()).upper()[:3] or "X"
    amount = f"{dollars // 1000}k" if dollars >= 10000 else str(dollars)
    return (initials + amount)[:7]


def to_monster(row, now):
    due = dt.datetime.fromisoformat(str(row["DueDate"]).replace(" ", "T").split(".")[0])
    overdue = (now - due).days
    dollars = int(row["AmountDue"]) // 100
    return {
        "pid": monster_id(row["Id"]),
        "id": row["Id"],
        "customer": row["CustomerName"],
        "dollars": dollars,
        "overdue_days": overdue,
        "name": short_name(row["CustomerName"], dollars),
        "demon": 1 if overdue >= DEMON_DAYS else 0,
    }


def demo_rows(n=MAX_MONSTERS):
    """Made-up invoices for offline play (no Connect AI, no real customer data)."""
    rng = random.Random(42)
    words = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Vandelay", "Wonka", "Stark",
             "Tyrell", "Cyberdyne", "Soylent", "Wayne", "Oscorp", "Gringotts", "Monarch"]
    kinds = ["Corp", "Labs", "Health", "Bank", "University", "Energy", "Systems"]
    today = dt.datetime.now()
    rows = []
    for i in range(n):
        due = today - dt.timedelta(days=rng.choice([20, 45, 75, 110, 140]))
        rows.append({
            "Id": f"in_demo_{i:03d}",
            "CustomerName": f"{rng.choice(words)} {rng.choice(kinds)}",
            "AmountDue": rng.choice([29900, 89900, 149900, 499900]),
            "DueDate": due.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return rows


def write_cache(monsters, source):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"source": source, "fetched_at": time.time(), "monsters": monsters}, indent=1))
    tmp.replace(CACHE)  # atomic, so `list` never reads a half-written file


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
    record = {**m, "killed_at": dt.datetime.now().isoformat(timespec="seconds"),
              "mode": "sandbox" if os.environ.get("SANDBOX") == "1" else "dry-run"}
    append(KILLS, record)
    print(f"\n  KILL  {m['customer']}  ${m['dollars']:,}  ({m['overdue_days']}d overdue)  "
          f"{m['id']}  [{record['mode']}]\n", file=sys.stderr)
    if record["mode"] == "sandbox":
        # Never block the game loop on the network: hand off to a detached process.
        subprocess.Popen([sys.executable, __file__, "sandbox-write", m["id"]],
                         stdout=subprocess.DEVNULL, stderr=open(STATE / "sandbox.log", "a"),
                         start_new_session=True)


def cmd_hit(args):
    m = find(int(args[0]))
    if m:
        append(HITS, {"id": m["id"], "at": time.time()})


def cmd_sandbox_write(args):
    m = next(k for k in read_jsonl(KILLS) if k["id"] == args[0])
    run_sql(f"INSERT INTO {SANDBOX_TABLE} ([RECORD_ID], [CUSTOMER], [AMOUNT_DUE], [KILLED_AT]) "
            "VALUES (@id, @customer, @amount, @at)",
            {"@id": m["id"], "@customer": m["customer"], "@amount": str(m["dollars"]), "@at": m["killed_at"]})
    print(f"{m['killed_at']} sandbox insert ok {m['id']}", file=sys.stderr)


def cmd_poll(args):
    """Refresh the cache. `poll --demo` uses made-up data; `poll --once` exits after one fetch."""
    interval = int(os.environ.get("POLL_SECONDS", "15"))
    while True:
        now = dt.datetime.now()
        try:
            if "--demo" in args:
                rows, source = demo_rows(), "demo"
            else:
                rows, source = run_sql(SOURCE_QUERY), "connect-ai"
            monsters = [to_monster(r, now) for r in rows]
            write_cache(monsters, source)
            print(f"{now:%H:%M:%S} {source}: {len(monsters)} monsters", file=sys.stderr)
        except Exception as e:  # keep the last good cache; the game keeps running
            print(f"{now:%H:%M:%S} poll failed: {e}", file=sys.stderr)
        if "--once" in args or "--demo" in args:
            return
        time.sleep(interval)


def cmd_score(_):
    kills = read_jsonl(KILLS)
    total = sum(k["dollars"] for k in kills)
    left = len([m for m in cached_monsters() if m["id"] not in killed_ids()])
    print(f"{len(kills)} invoices fragged, ${total:,} chased, {left} still standing")
    for k in kills[-10:]:
        print(f"  {k['killed_at']}  {k['customer']:<34} ${k['dollars']:>7,}  {k['overdue_days']:>3}d  [{k['mode']}]")


def cmd_reset(_):
    for p in (KILLS, HITS):
        p.unlink(missing_ok=True)
    print("kills cleared")


COMMANDS = {"list": cmd_list, "kill": cmd_kill, "hit": cmd_hit, "poll": cmd_poll,
            "score": cmd_score, "reset": cmd_reset, "sandbox-write": cmd_sandbox_write}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: {Path(sys.argv[0]).name} {{{'|'.join(COMMANDS)}}} [args]")
    COMMANDS[sys.argv[1]](sys.argv[2:])
