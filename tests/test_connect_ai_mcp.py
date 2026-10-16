"""connect_ai_mcp.py: the MCP client, against a fake Connect AI MCP server."""
import base64
import http.server
import os
import threading
import time
import unittest
from unittest import mock

from helpers import CONNECTIONS, PAT, USER, FakeMCP, query

import connect_ai_mcp

CREDS = {"CONNECT_AI_USER": USER, "CONNECT_AI_PAT": PAT}


class BasicTokenTest(unittest.TestCase):
    def test_encodes_user_and_pat(self):
        with mock.patch.dict(os.environ, CREDS):
            self.assertEqual(base64.b64decode(connect_ai_mcp.basic_token()).decode(), f"{USER}:{PAT}")

    def test_missing_credentials_exit_with_a_hint(self):
        with mock.patch.dict(os.environ, {"CONNECT_AI_USER": USER, "CONNECT_AI_PAT": ""}):
            with self.assertRaises(SystemExit) as e:
                connect_ai_mcp.basic_token()
        self.assertIn("CONNECT_AI_PAT", str(e.exception))


class ClientTest(unittest.TestCase):
    sse = False

    def setUp(self):
        patcher = mock.patch.dict(os.environ, CREDS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mcp = FakeMCP(sse=self.sse).__enter__()
        self.addCleanup(self.mcp.__exit__)
        self.client = connect_ai_mcp.Client(self.mcp.url)

    def test_handshake_then_reuses_the_session(self):
        self.client.query(query("Zendesk1"))
        methods = [r["body"]["method"] for r in self.mcp.requests]
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call"])
        self.assertIsNone(self.mcp.requests[0]["session"])
        self.assertEqual({r["session"] for r in self.mcp.requests[1:]}, {"sess-1"})
        self.assertNotIn("id", self.mcp.requests[1]["body"])          # notifications carry no id
        ids = [r["body"]["id"] for r in self.mcp.requests if "id" in r["body"]]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertEqual(self.mcp.auth, {FakeMCP.expected_auth()})

    def test_query_parses_csv_and_drops_the_query_id(self):
        rows = self.client.query(query("Salesforce1"))
        self.assertEqual(rows, [
            {"Id": "006A", "Label": "Acme Corp", "Detail": "$45,000, 12 days late", "Hot": "1"},
            {"Id": "006B", "Label": "Globex", "Detail": "$9", "Hot": "0"}])

    def test_query_with_no_rows(self):
        self.assertEqual(self.client.query(query("QuickBooks1")), [])

    def test_tool_error_raises_with_the_servers_first_paragraph(self):
        with self.assertRaises(connect_ai_mcp.MCPError) as e:
            self.client.query("SELECT * FROM [PostgreSQLPII].[public].[customers]")
        self.assertEqual(str(e.exception), "Request blocked: PII detected in inbound query")

    def test_error_text_without_is_error_flag_still_raises(self):
        with self.assertRaisesRegex(connect_ai_mcp.MCPError, "^Cannot obtain connection$"):
            self.client.query("SELECT 1")

    def test_jsonrpc_error_raises(self):
        with self.assertRaisesRegex(connect_ai_mcp.MCPError, "Unknown tool nope"):
            self.client.call("nope")

    def test_connections(self):
        self.assertEqual(self.client.connections(), CONNECTIONS)


class SSEClientTest(ClientTest):
    """Same behavior when the server answers with server-sent events."""
    sse = True


class HTTPErrorTest(unittest.TestCase):
    def test_bad_credentials_raise_mcp_error_with_status(self):
        with FakeMCP() as mcp, mock.patch.dict(os.environ, {**CREDS, "CONNECT_AI_PAT": "wrong"}):
            with self.assertRaisesRegex(connect_ai_mcp.MCPError, "HTTP 401: bad credentials"):
                connect_ai_mcp.Client(mcp.url)


class SlowAnswerTest(unittest.TestCase):
    def test_keepalives_dont_stretch_the_timeout(self):
        """While a query runs, Connect AI sends ': keepalive' lines; each read succeeds, so only
        a deadline on the whole call stops a game from waiting on a query that never ends."""
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for _ in range(50):
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    pass  # the client gave up

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        start = time.monotonic()
        with mock.patch.dict(os.environ, CREDS), self.assertRaisesRegex(TimeoutError, "in 0.5 s"):
            connect_ai_mcp.Client(f"http://127.0.0.1:{server.server_port}/mcp", timeout=0.5)
        self.assertLess(time.monotonic() - start, 2)


if __name__ == "__main__":
    unittest.main()
