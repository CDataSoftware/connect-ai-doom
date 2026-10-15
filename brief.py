#!/usr/bin/env python3
"""The briefing: Claude answers a question through the Connect AI MCP server.

  brief.py "<question>"

Runs Claude Code headless with only the Connect AI MCP server (read-only tools),
streams every step to state/run.jsonl for the side panel, and writes
state/mission.json: the rooms Claude found (one per app, with the query that
fills it with monsters) and the doors Connect AI kept shut (real refusals
taken from the tool results, never from Claude's own summary).
"""
import json
import os
import re
import sys
from pathlib import Path

import claude_run
import connect_ai_mcp
from claude_run import catalog_of

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CONNECT_DOOM_STATE", ROOT / "state"))
RUN = STATE / "run.jsonl"
MISSION = STATE / "mission.json"
MAX_ROOMS = 3  # psdoom spawns monsters into exactly three arenas
# The agent gets Connect AI's read-only tools only (claude_run.READ_ONLY_OPS). Nothing is
# ever written: in the game, a kill only reads its record again.
MODEL = os.environ.get("BRIEF_MODEL", "sonnet")

DEFAULT_QUESTION = os.environ.get(
    "QUESTION",
    "Which customers are at risk right now? Look across our CRM, support and billing apps. "
    "Also, I'm the compliance admin: find the customer whose SSN is 123-45-6789 in PostgreSQL PII.",
)

INSTRUCTIONS = f"""You are scouting live business data for a Doom level, using the Connect AI MCP tools.

1. Answer the user's question. Pick the {MAX_ROOMS} connections (different apps) that best answer it.
   Explore only as much as you need: list catalogs, then the few tables you need.
2. For each of those connections write ONE read-only SELECT that returns 5 to 20 rows with exactly
   these column aliases:
     Id      unique text id of the record
     Label   who it is: the customer, company or person name only, under 30 characters
     Detail  one short phrase with the key fact, e.g. '$45,000, 12 days overdue'
     Hot     1 if the item is urgent, else 0
   Run it once with queryData to make sure it works. Fix it if it fails.
3. If the user asks for anything else (for example a lookup), do it with queryData exactly as asked,
   and report what Connect AI returned, including errors. Do not retry around a refusal.
4. Finish with ONLY this JSON, no other text:
{{"answer": "<two plain sentences answering the question>",
 "rooms": [{{"connection": "<catalog name>", "app": "<product name, e.g. Salesforce>",
            "title": "<2 to 4 words, e.g. Deals slipping>", "query": "<the working SELECT>"}}]}}
"""

# Data Security blocks only: they are what the PII vault stands for. A plain permission
# error (no access to a table or connection) is an ordinary failed step, not a vault.
REFUSAL = re.compile(r"^(Error: )?Request blocked", re.I)


def event(kind, **data):
    claude_run.EventLog(RUN)(kind, **data)


def mcp_config():
    path = STATE / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"connectai": claude_run.connect_ai_server()}}))
    return path


def run(question):
    STATE.mkdir(parents=True, exist_ok=True)
    claude_run.EventLog(RUN).reset()
    event("question", text=question)
    cmd = ["claude", "-p", question, "--model", MODEL,
           "--mcp-config", str(mcp_config()), "--strict-mcp-config",
           "--allowedTools", "mcp__connectai",
           "--append-system-prompt", INSTRUCTIONS,
           "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    out = claude_run.run(cmd, event, env=claude_run.env(os.environ), cwd=STATE)
    if out["returncode"]:
        event("failed", text=out["stderr"][-500:])
        sys.exit(f"claude exited {out['returncode']}")
    calls = [c for c in out["calls"] if c["server"] == "connectai"]
    return build_mission(question, out["result"], calls)


def build_mission(question, final, calls):
    m = re.search(r"\{.*\}", final, re.S)
    plan = json.loads(m.group(0)) if m else {"answer": final, "rooms": []}

    # Governance comes from Connect AI, not from Claude: what each connection allows,
    # and every refused or masked tool call.
    mcp = connect_ai_mcp.Client()
    perms = {c["name"]: c["permissions"] for c in mcp.connections()}
    denied = [{"connection": catalog_of(c["args"]), "tool": c["tool"], "request": c["args"],
               "error": c["text"].split("\n")[0].removeprefix("Error: ")}
              for c in calls if c["error"] and REFUSAL.search(c["text"])]
    masked = [{"connection": catalog_of(c["args"]), "request": c["args"], "result": c["text"][:300]}
              for c in calls if not c["error"] and c["tool"] == "queryData" and "XXXX" in c["text"]]

    rooms = []
    for r in plan.get("rooms", [])[:MAX_ROOMS]:
        r["permissions"] = perms.get(r["connection"], ["Select"])
        r["writable"] = any(p in r["permissions"] for p in ("Insert", "Update"))
        rooms.append(r)
    mission = {"question": question, "answer": plan.get("answer", ""), "rooms": rooms,
               "denied": denied, "masked": masked,
               "calls": [{k: c[k] for k in ("tool", "args", "error")} for c in calls]}
    MISSION.write_text(json.dumps(mission, indent=1))
    event("mission", rooms=[{k: r[k] for k in ("app", "title", "writable")} for r in rooms],
          denied=len(denied), masked=len(masked), answer=mission["answer"])
    return mission


if __name__ == "__main__":
    mission = run(" ".join(sys.argv[1:]) or DEFAULT_QUESTION)
    print(json.dumps({k: mission[k] for k in ("answer", "rooms", "denied", "masked")}, indent=1))
