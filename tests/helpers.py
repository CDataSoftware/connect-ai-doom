"""Shared test fixtures: a fake Connect AI MCP server, a fake `claude` CLI, and a tiny IWAD.

Nothing here touches the network or the real Connect AI account.
"""
import contextlib
import http.server
import json
import os
import socket
import struct
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

USER, PAT = "tester@example.com", "secret-pat"

# What the fake server knows: one room per catalog, plus a PII catalog Data Security blocks.
TABLES = {
    "Salesforce1": "Id,Label,Detail,Hot\n006A,Acme Corp,\"$45,000, 12 days late\",1\n006B,Globex,$9,0\n",
    "Zendesk1": "Id,Label,Detail,Hot\nZ1,Initech,3 escalations,True\n",
    "QuickBooks1": "Id,Label,Detail,Hot\n",
}
CONNECTIONS = [
    {"name": "Salesforce1", "driver": "Salesforce", "permissions": ["Select", "Insert", "Update"]},
    {"name": "Zendesk1", "driver": "Zendesk", "permissions": ["Select"]},
    {"name": "QuickBooks1", "driver": "QuickBooksOnline", "permissions": ["Select"]},
]
BLOCKED = "Error: Request blocked: PII detected in inbound query\n\nDetails: US Social Security Number"


def catalog(sql):
    return next((c for c in [*TABLES, "PostgreSQLPII"] if f"[{c}]" in sql), None)


class FakeMCP:
    """The Connect AI MCP server, as far as connect_ai_mcp.Client and brief.py use it.

    sse=True answers in server-sent events, like the real server does for some clients."""

    def __init__(self, sse=False):
        self.sse, self.requests, self.auth = sse, [], set()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"body": body, "session": self.headers.get("Mcp-Session-Id")})
                fake.auth.add(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != fake.expected_auth():
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"bad credentials")
                    return
                if "id" not in body:                       # a notification
                    self.send_response(202)
                    self.end_headers()
                    return
                reply = {"jsonrpc": "2.0", "id": body["id"]}
                reply.update(fake.answer(body["method"], body.get("params", {})))
                raw = json.dumps(reply)
                if fake.sse:
                    raw = f"event: message\ndata: {raw}\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if fake.sse else "application/json")
                self.send_header("Mcp-Session-Id", "sess-1")
                self.end_headers()
                self.wfile.write(raw.encode())

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    @staticmethod
    def expected_auth():
        import base64
        return "Basic " + base64.b64encode(f"{USER}:{PAT}".encode()).decode()

    def answer(self, method, params):
        if method == "initialize":
            return {"result": {"protocolVersion": "2025-06-18", "capabilities": {}}}
        if method != "tools/call":
            return {"error": {"code": -32601, "message": f"no method {method}"}}
        name, args = params["name"], params["arguments"]
        if name == "search_context":
            return {"result": {"content": [{"type": "text", "text": json.dumps({"connections": CONNECTIONS})}]}}
        if name == "queryData":
            c = catalog(args["query"])
            if c == "PostgreSQLPII":
                return {"result": {"isError": True, "content": [{"type": "text", "text": BLOCKED}]}}
            if c is None:
                return {"result": {"content": [{"type": "text", "text": "Error: Cannot obtain connection"}]}}
            return {"result": {"content": [{"type": "text", "text": TABLES[c] + "\nQuery ID: q-123"}]}}
        return {"error": {"code": -32602, "message": f"Unknown tool {name}"}}

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def query(cat):
    return f"SELECT Id, Name AS Label, Amount AS Detail, 0 AS Hot FROM [{cat}].[dbo].[Things]"


# The stream-json a `claude -p ... --output-format stream-json --verbose --include-partial-messages`
# run prints, trimmed to what brief.py reads.
PLAN = {"answer": "Acme is at risk. Initech has escalations.",
        "rooms": [{"connection": "Salesforce1", "app": "Salesforce", "title": "Deals slipping",
                   "query": query("Salesforce1")},
                  {"connection": "Zendesk1", "app": "Zendesk", "title": "Open escalations",
                   "query": query("Zendesk1")}]}
PII_SQL = "SELECT * FROM [PostgreSQLPII].[public].[customers] WHERE ssn = '123-45-6789'"


def stream_json():
    def use(i, tool, **args):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"tu{i}", "name": f"mcp__connectai__{tool}", "input": args}]}}

    def result(i, text, error=False):
        return {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"tu{i}", "is_error": error,
             "content": [{"type": "text", "text": text}]}]}}

    return [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5",
         "tools": ["mcp__connectai__queryData", "mcp__connectai__getCatalogs", "Bash"]},
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Let me look."}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me look."}]}},
        use(1, "getCatalogs"),
        result(1, "CatalogName\nSalesforce1\nZendesk1\nPostgreSQLPII\n"),
        use(2, "queryData", query=query("Salesforce1")),
        result(2, TABLES["Salesforce1"] + "\nQuery ID: q-1"),
        use(3, "queryData", query=PII_SQL),
        result(3, BLOCKED, error=True),
        use(4, "queryData", query=query("Zendesk1").replace("Detail", "Detail, 'XXXX' AS Masked")),
        result(4, "Id,Label,Masked\nZ1,Initech,XXXX\n\nQuery ID: q-2"),
        use(5, "getColumns", catalogName="Zendesk1", tableName="Tickets"),
        result(5, "Error: permission denied for table Tickets", error=True),
        "not json, ignored",
        {"type": "result", "result": "Done.\n```json\n" + json.dumps(PLAN) + "\n```",
         "total_cost_usd": 0.12, "duration_ms": 61000, "num_turns": 7},
    ]


FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Stands in for the Claude Code CLI: records how it was called, then prints a canned run."""
import json, os, sys
log = os.environ["FAKE_CLAUDE_LOG"]
json.dump({"argv": sys.argv[1:], "basic": os.environ.get("CONNECT_AI_BASIC"), "cwd": os.getcwd()},
          open(log, "w"))
if os.environ.get("FAKE_CLAUDE_FAIL"):
    print("boom: model overloaded", file=sys.stderr)
    sys.exit(3)
for line in json.load(open(os.environ["FAKE_CLAUDE_STREAM"])):
    print(line if isinstance(line, str) else json.dumps(line), flush=True)
'''


@contextlib.contextmanager
def fake_claude_env(tmp, mcp_url, fail=False):
    """Environment for running brief.py / raid.sh against the fakes, with state in `tmp`."""
    tmp = Path(tmp)
    bin_ = tmp / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "claude").write_text(FAKE_CLAUDE)
    (bin_ / "claude").chmod(0o755)
    (tmp / "stream.json").write_text(json.dumps(stream_json()))
    env = {**os.environ,
           "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
           "CONNECT_DOOM_STATE": str(tmp / "state"),
           "CONNECT_AI_MCP_URL": mcp_url,
           "CONNECT_AI_USER": USER, "CONNECT_AI_PAT": PAT,
           "FAKE_CLAUDE_LOG": str(tmp / "claude-call.json"),
           "FAKE_CLAUDE_STREAM": str(tmp / "stream.json")}
    env.pop("QUESTION", None)
    if fail:
        env["FAKE_CLAUDE_FAIL"] = "1"
    yield env


# ---------------------------------------------------------------- WADs

def wad_lumps(data):
    """[(name, bytes)] in directory order; asserts the header is sane."""
    assert data[:4] in (b"PWAD", b"IWAD"), data[:4]
    n, off = struct.unpack("<ii", data[4:12])
    assert off + 16 * n == len(data), "directory must end the file"
    out = []
    for i in range(n):
        pos, size = struct.unpack("<ii", data[off + 16 * i:off + 16 * i + 8])
        assert 12 <= pos and pos + size <= off
        out.append((data[off + 16 * i + 8:off + 16 * i + 16].rstrip(b"\0").decode(), data[pos:pos + size]))
    return out


def tiny_iwad(path):
    """Just enough of freedoom2.wad for make_branding.py: PLAYPAL, PNAMES, TEXTURE1 and W94_1."""
    from make_branding import to_patch
    from PIL import Image

    palette = [(i, i, i) for i in range(0, 256, 2)] + [(255, 229, 0), (21, 21, 28), (64, 96, 255),
                                                         (255, 48, 32), (241, 238, 233)]
    palette += [(i, 0, 0) for i in range(256 - len(palette))]
    playpal = bytes(c for rgb in palette for c in rgb) * 14
    wall = Image.new("RGBA", (64, 128))
    for x in range(64):
        for y in range(128):
            wall.putpixel((x, y), ((x * 4) % 256, (y * 2) % 256, 90, 255))
    pnames = struct.pack("<i", 1) + b"W94_1\0\0\0"
    entry = b"TEKWALL4" + struct.pack("<ihhih", 0, 64, 128, 0, 1) + struct.pack("<hhhhh", 0, 0, 0, 0, 0)
    texture1 = struct.pack("<ii", 1, 8) + entry
    lumps = [("PLAYPAL", playpal), ("PNAMES", pnames), ("TEXTURE1", texture1),
             ("W94_1", to_patch(wall, palette))]
    data, directory, pos = b"", b"", 12
    for name, blob in lumps:
        directory += struct.pack("<ii", pos, len(blob)) + name.encode().ljust(8, b"\0")
        data += blob
        pos += len(blob)
    Path(path).write_bytes(b"IWAD" + struct.pack("<ii", len(lumps), pos) + data + directory)
    return palette


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TempState:
    """Point connect_doom (imported in-process) at a fresh state directory."""

    def __enter__(self):
        import connect_doom as cd
        self.tmp = tempfile.TemporaryDirectory()
        paths = {"CACHE": "cache.json", "KILLS": "kills.jsonl", "HITS": "hits.jsonl", "MISSION": "mission.json",
                 "GAME": "game.json", "LOOKUPS": "lookups.jsonl", "LABELS": "labels.tsv"}
        self.saved = {k: getattr(cd, k) for k in ("STATE", *paths)}
        state = Path(self.tmp.name)
        cd.STATE = state
        for k, name in paths.items():
            setattr(cd, k, state / name)
        return state

    def __exit__(self, *exc):
        import connect_doom as cd
        for k, v in self.saved.items():
            setattr(cd, k, v)
        self.tmp.cleanup()
