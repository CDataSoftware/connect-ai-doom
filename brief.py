#!/usr/bin/env python3
"""The briefing: Claude answers a question through the Connect AI MCP server.

  brief.py "<question>"

Runs Claude Code headless with only the Connect AI MCP server (read-only tools),
streams every step to state/run.jsonl for the side panel, and writes
state/mission.json: the rooms Claude found (one per app, with the query that
fills it with monsters) and the doors Connect AI kept shut (real refusals
taken from the tool results, never from Claude's own summary).
"""
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import connect_ai_mcp

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CONNECT_DOOM_STATE", ROOT / "state"))
RUN = STATE / "run.jsonl"
MISSION = STATE / "mission.json"
MAX_ROOMS = 3  # psdoom spawns monsters into exactly three arenas
# The agent gets Connect AI's read-only tools only. Writes happen in the game,
# through the bridge, and only where the connection allows them.
READ_ONLY_OPS = "get_catalogs,get_schemas,get_tables,get_columns,get_instructions,queryData"
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

REFUSAL = re.compile(r"Request blocked|Cannot obtain connection|permission|not authorized|denied", re.I)


def event(kind, **data):
    with RUN.open("a") as f:
        f.write(json.dumps({"t": time.time(), "kind": kind, **data}) + "\n")


def mcp_config():
    """The token stays in the environment; Claude Code expands ${CONNECT_AI_BASIC} itself."""
    path = STATE / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"connectai": {
        "type": "http",
        "url": f"{connect_ai_mcp.MCP_URL}?ops={READ_ONLY_OPS}",
        "headers": {"Authorization": "Basic ${CONNECT_AI_BASIC}"},
    }}}))
    return path


def tool_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def count_rows(text):
    """Rows in a CSV tool result (the text ends with a 'Query ID' line)."""
    body = text.split("\nQuery ID:")[0].strip()
    try:
        return max(len(list(csv.reader(io.StringIO(body)))) - 1, 0)
    except csv.Error:
        return max(len(body.splitlines()) - 1, 0)


def catalog_of(args):
    if args.get("catalogName"):
        return args["catalogName"]
    m = re.search(r"\[([^\]]+)\]\s*\.\s*\[", args.get("query", ""))
    return m.group(1) if m else None


def run(question):
    STATE.mkdir(parents=True, exist_ok=True)
    RUN.write_text("")
    event("question", text=question)
    env = {**os.environ, "CONNECT_AI_BASIC": connect_ai_mcp.basic_token()}
    cmd = ["claude", "-p", question, "--model", MODEL,
           "--mcp-config", str(mcp_config()), "--strict-mcp-config",
           "--allowedTools", "mcp__connectai",
           "--append-system-prompt", INSTRUCTIONS,
           "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env, cwd=STATE)
    calls, pending, final = [], {}, ""
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "init":
            event("session", model=msg.get("model"),
                  tools=[t for t in msg.get("tools", []) if t.startswith("mcp__")])
        elif kind == "stream_event":
            delta = msg.get("event", {}).get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                event("text_delta", text=delta["text"])
        elif kind == "result":
            final = msg.get("result", "")
            event("done", cost_usd=msg.get("total_cost_usd"), duration_ms=msg.get("duration_ms"),
                  turns=msg.get("num_turns"))
        for c in (msg.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c["name"].startswith("mcp__connectai__"):
                tool = c["name"].removeprefix("mcp__connectai__")
                args = c.get("input", {})
                pending[c["id"]] = {"tool": tool, "args": args, "start": time.time()}
                event("call", id=c["id"], server="connectai", tool=tool, args=args,
                      connection=catalog_of(args))
            elif c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                call = pending.pop(c["tool_use_id"])
                text = tool_text(c.get("content"))
                call.update(text=text[:4000], error=bool(c.get("is_error")) or text.startswith("Error:"))
                calls.append(call)
                event("result", id=c["tool_use_id"], tool=call["tool"], connection=catalog_of(call["args"]),
                      error=call["error"], rows=0 if call["error"] else count_rows(text),
                      text=text[:4000], ms=round((time.time() - call["start"]) * 1000))
            elif c.get("type") == "text" and kind == "assistant":
                event("thought", text=c["text"][:2000])
    proc.wait()
    if proc.returncode:
        event("failed", text=proc.stderr.read()[-500:])
        sys.exit(f"claude exited {proc.returncode}")
    return build_mission(question, final, calls)


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
