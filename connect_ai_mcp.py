"""Minimal client for the Connect AI MCP server (https://mcp.cloud.cdata.com/mcp).

Everything the game reads goes through the same MCP server Claude uses, with the
same user's permissions and Data Security rules.
"""
import base64
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

MCP_URL = os.environ.get("CONNECT_AI_MCP_URL", "https://mcp.cloud.cdata.com/mcp")


def basic_token():
    user, pat = os.environ.get("CONNECT_AI_USER"), os.environ.get("CONNECT_AI_PAT")
    if not (user and pat):
        sys.exit("Set CONNECT_AI_USER and CONNECT_AI_PAT (raid.sh reads them from the Keychain).")
    return base64.b64encode(f"{user}:{pat}".encode()).decode()


class MCPError(RuntimeError):
    pass


class Client:
    def __init__(self, url=MCP_URL, timeout=120):
        self.url, self.session, self.next_id, self.timeout = url, None, 1, timeout
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "connect-doom", "version": "1"}})
        self._rpc("notifications/initialized", notify=True)

    def _rpc(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"], self.next_id = self.next_id, self.next_id + 1
        headers = {"Authorization": f"Basic {basic_token()}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers)
        # urlopen's timeout is per read, so a reply that keeps trickling in could take forever:
        # the whole call gets `timeout` seconds too.
        deadline = time.monotonic() + self.timeout
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                chunks = []
                while chunk := resp.read1(65536):
                    chunks.append(chunk)
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"Connect AI didn't finish answering in {self.timeout:g} s")
                raw = b"".join(chunks).decode()
        except urllib.error.HTTPError as e:
            with e:
                raise MCPError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
        if notify or not raw.strip():
            return None
        if not raw.lstrip().startswith("{"):  # server-sent events: the last data line is the reply
            raw = [line[5:] for line in raw.splitlines() if line.startswith("data:")][-1]
        reply = json.loads(raw)
        if "error" in reply:
            raise MCPError(reply["error"].get("message", str(reply["error"])))
        return reply["result"]

    def call(self, tool, **args):
        """Call a tool; return its text output. Tool errors raise MCPError with the server's text."""
        result = self._rpc("tools/call", {"name": tool, "arguments": args})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError") or text.startswith("Error:"):
            raise MCPError(text.split("\n\n")[0].removeprefix("Error: "))
        return text

    def query(self, sql):
        """queryData -> list of row dicts. The text is CSV followed by a 'Query ID' line."""
        text = self.call("queryData", query=sql)
        body = text.split("\nQuery ID:")[0].strip()
        return list(csv.DictReader(io.StringIO(body))) if body else []

    def connections(self):
        """[{name, driver, permissions}] for every connection this user can see."""
        return json.loads(self.call("search_context", query="connections"))["connections"]
