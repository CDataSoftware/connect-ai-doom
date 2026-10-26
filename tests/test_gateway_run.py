"""gateway_run.py: the agent loop over Connect AI's LLM Gateway, against scripted replies and a fake gateway."""
import http.server
import json
import threading
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts the repo on sys.path)
import gateway_run


def reply(text=None, calls=(), prompt_tokens=10, completion_tokens=5):
    """A chat completion as the gateway answers it; calls: (id, name, arguments)."""
    msg = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = [{"type": "function", "id": cid, "function": {"name": name, "arguments": json.dumps(args)}}
                             for cid, name, args in calls]
    return {"model": "gemini-3.7-flash", "choices": [{"message": msg}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


class RunTest(unittest.TestCase):
    def setUp(self):
        self.events, self.sent = [], []
        self.tools = {
            "doom__look": (lambda args: json.dumps({"you_are_in": "lobby"}), "Look around.",
                           {"type": "object", "properties": {}, "required": []}),
            "connectai__queryData": (lambda args: "Id,Label\n1,Acme\n2,Globex\n\nQuery ID: q-1", "Run SQL.",
                                     {"type": "object", "properties": {"query": {"type": "string"}}}),
            "doom__attack": (lambda args: 1 / 0, "Attack.", {"type": "object", "properties": {}}),
        }

    def run_loop(self, replies, max_turns=10):
        replies = iter(replies)

        def post(key, body):
            self.sent.append(json.loads(json.dumps(body)))     # a copy: the loop keeps appending
            return next(replies)
        return gateway_run.run("Clear the level.", "gemini-3.7-flash", self.tools,
                               lambda kind, **d: self.events.append({"kind": kind, **d}),
                               key="cgw_sk_test", max_turns=max_turns, post=post)

    def test_plays_until_the_model_stops_calling_tools(self):
        sql = "SELECT Id, Name AS Label FROM [Salesforce1].[dbo].[Account]"
        out = self.run_loop([
            reply("Keycards first.", [("c1", "doom__look", {}), ("c2", "connectai__queryData", {"query": sql})]),
            reply(None, [("c3", "doom__attack", {"pid": 7}), ("c4", "doom__fly", {})]),
            reply("Cleared 2 records.", prompt_tokens=30, completion_tokens=8),
        ])
        self.assertEqual(out, {"result": "Cleared 2 records.", "turns": 3, "tokens": {"input": 50, "output": 18}})
        kinds = [e["kind"] for e in self.events]
        self.assertEqual(kinds[0], "session")
        self.assertEqual(self.events[0]["tools"],
                         ["mcp__connectai__queryData", "mcp__doom__attack", "mcp__doom__look"])
        self.assertEqual(kinds[-1], "done")
        self.assertEqual((self.events[-1]["turns"], self.events[-1]["text"]), (3, "Cleared 2 records."))
        # the same events claude_run.run writes, so the panel shows it like a Claude run
        calls = [(e["server"], e["tool"], e["connection"]) for e in self.events if e["kind"] == "call"]
        self.assertEqual(calls, [("doom", "look", None), ("connectai", "queryData", "Salesforce1"),
                                 ("doom", "attack", None), ("doom", "fly", None)])
        results = {e["id"]: e for e in self.events if e["kind"] == "result"}
        self.assertEqual((results["c2"]["rows"], results["c2"]["error"]), (2, False))
        self.assertTrue(results["c3"]["error"])                     # a tool that raised
        self.assertIn("no tool called doom__fly", results["c4"]["text"])
        self.assertEqual([e["text"] for e in self.events if e["kind"] == "thought"],
                         ["Keycards first.", "Cleared 2 records."])

    def test_sends_each_reply_back_as_it_came_with_the_tool_results(self):
        self.run_loop([reply("Looking.", [("call_1__thought__abc", "doom__look", {})]), reply("Done.")])
        second = self.sent[1]
        self.assertEqual((second["model"], second["max_tokens"]), ("gemini-3.7-flash", gateway_run.MAX_TOKENS))
        self.assertEqual([t["function"]["name"] for t in second["tools"]],
                         ["doom__look", "connectai__queryData", "doom__attack"])
        user, assistant, tool = second["messages"]
        self.assertEqual(user, {"role": "user", "content": "Clear the level."})
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_1__thought__abc")   # Gemini's reasoning rides in the id
        self.assertEqual(tool, {"role": "tool", "tool_call_id": "call_1__thought__abc",
                                "content": json.dumps({"you_are_in": "lobby"})})

    def test_stops_at_max_turns(self):
        out = self.run_loop([reply(None, [(f"c{i}", "doom__look", {})]) for i in range(5)], max_turns=3)
        self.assertEqual((out["turns"], out["result"]), (3, ""))
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.events[-1]["kind"], "done")


class FakeGateway:
    """Answers chat completions with scripted (status, body) pairs."""

    def __init__(self, answers):
        self.answers, self.requests = list(answers), []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                fake.requests.append((self.path, self.headers.get("Authorization"),
                                      json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                status, body = fake.answers.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/gateway/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CompleteTest(unittest.TestCase):
    def gateway(self, answers):
        fake = FakeGateway(answers)
        self.addCleanup(fake.close)
        patcher = mock.patch.object(gateway_run, "GATEWAY_URL", fake.url)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_posts_a_chat_completion_with_the_virtual_key(self):
        fake = self.gateway([(200, reply("hi"))])
        out = gateway_run.complete("cgw_sk_test", {"model": "m", "messages": []})
        self.assertEqual(out["choices"][0]["message"]["content"], "hi")
        self.assertEqual(fake.requests[0][:2], ("/gateway/v1/chat/completions", "Bearer cgw_sk_test"))

    def test_retries_a_rate_limit_or_a_server_error(self):
        fake = self.gateway([(429, {"error": {"message": "slow down"}}), (502, {}), (200, reply("hi"))])
        waits = []
        out = gateway_run.complete("k", {}, sleep=waits.append)
        self.assertEqual((out["choices"][0]["message"]["content"], waits, len(fake.requests)), ("hi", [2, 5], 3))

    def test_a_refused_key_or_model_fails_at_once_with_the_gateways_message(self):
        self.gateway([(403, {"error": {"message": "The requested model 'x' is not available for this API key"}})])
        waits = []
        with self.assertRaisesRegex(gateway_run.GatewayError, "HTTP 403: The requested model 'x' is not available"):
            gateway_run.complete("k", {}, sleep=waits.append)
        self.assertEqual(waits, [])

    def test_gives_up_after_the_last_retry(self):
        self.gateway([(500, {"error": {"message": "down"}})] * 4)
        with self.assertRaisesRegex(gateway_run.GatewayError, "HTTP 500: down"):
            gateway_run.complete("k", {}, sleep=lambda _s: None)


if __name__ == "__main__":
    unittest.main()
