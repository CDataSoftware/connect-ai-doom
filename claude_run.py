"""Running Claude Code headless against MCP servers, shared by brief.py and play.py.

run() starts `claude -p ... --output-format stream-json` and turns its stream into the
event log the side panel reads (state/run.jsonl for the briefing, state/play.jsonl for
Claude plays): the session, live text, each thought, each MCP call and its result, and
the final result. connect_ai_server() is the Connect AI MCP server, read-only tools only.
"""
import csv
import io
import json
import re
import subprocess
import time

import connect_ai_mcp

# Connect AI's read-only tools: Claude can explore and query, never write.
READ_ONLY_OPS = "get_catalogs,get_schemas,get_tables,get_columns,get_instructions,queryData"


def connect_ai_server():
    """The Connect AI MCP server for an --mcp-config. The token stays in the environment:
    Claude Code expands ${CONNECT_AI_BASIC} itself (see env())."""
    return {"type": "http", "url": f"{connect_ai_mcp.MCP_URL}?ops={READ_ONLY_OPS}",
            "headers": {"Authorization": "Basic ${CONNECT_AI_BASIC}"}}


def env(environ):
    """The environment for `claude` with connect_ai_server(): adds CONNECT_AI_BASIC."""
    return {**environ, "CONNECT_AI_BASIC": connect_ai_mcp.basic_token()}


class EventLog:
    """A JSON-lines event log for the side panel; each line gets a timestamp."""

    def __init__(self, path):
        self.path = path

    def reset(self):
        self.path.write_text("")

    def __call__(self, kind, **data):
        with self.path.open("a") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, **data}) + "\n")


def tool_text(content):
    """A tool result's text (results come as a string or a list of content blocks)."""
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
    """The Connect AI connection a call is about: its catalogName, or the query's first [catalog]."""
    if args.get("catalogName"):
        return args["catalogName"]
    m = re.search(r"\[([^\]]+)\]\s*\.\s*\[", args.get("query", ""))
    return m.group(1) if m else None


def run(cmd, log, *, env=None, cwd=None, on_start=None):
    """Run a `claude -p ... --output-format stream-json --verbose --include-partial-messages`
    command, logging each step to `log` (an EventLog). on_start(proc) runs once it's started.

    Returns {"result": the final text, "finished": whether a result arrived, "calls": every MCP
    call as {server, tool, args, text, error}, "returncode", "stderr"}."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=cwd)
    if on_start:
        on_start(proc)
    calls, pending, final, finished = [], {}, "", False
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "init":
            log("session", model=msg.get("model"), tools=[t for t in msg.get("tools", []) if t.startswith("mcp__")])
        elif kind == "stream_event":
            delta = msg.get("event", {}).get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                log("text_delta", text=delta["text"])
        elif kind == "result":
            final, finished = msg.get("result", ""), True
            log("done", cost_usd=msg.get("total_cost_usd"), duration_ms=msg.get("duration_ms"),
                turns=msg.get("num_turns"), text=final)
        for c in (msg.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and kind == "assistant":
                log("thought", text=c["text"][:2000])
            elif c.get("type") == "tool_use" and c["name"].startswith("mcp__"):
                _, server, tool = c["name"].split("__", 2)
                args = c.get("input", {})
                pending[c["id"]] = {"server": server, "tool": tool, "args": args, "start": time.time()}
                log("call", id=c["id"], server=server, tool=tool, args=args, connection=catalog_of(args))
            elif c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                call = pending.pop(c["tool_use_id"])
                text = tool_text(c.get("content"))
                call.update(text=text[:4000], error=bool(c.get("is_error")) or text.startswith("Error:"))
                calls.append(call)
                log("result", id=c["tool_use_id"], tool=call["tool"], connection=catalog_of(call["args"]),
                    error=call["error"], rows=0 if call["error"] else count_rows(text),
                    text=call["text"], ms=round((time.time() - call.pop("start")) * 1000))
    proc.wait()
    return {"result": final, "finished": finished, "calls": calls, "returncode": proc.returncode,
            "stderr": proc.stderr.read()}
