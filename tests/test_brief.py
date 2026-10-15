"""brief.py: the Claude run (against a fake `claude` CLI) and the mission it builds."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import BLOCKED, PAT, PII_SQL, PLAN, ROOT, FakeMCP, fake_claude_env, query

import brief


class HelpersTest(unittest.TestCase):
    def test_count_rows(self):
        self.assertEqual(brief.count_rows("Id,Label\n1,\"a,b\"\n2,\"multi\nline\"\n\nQuery ID: q"), 2)
        self.assertEqual(brief.count_rows("Id,Label\n"), 0)
        self.assertEqual(brief.count_rows(""), 0)
        self.assertEqual(brief.count_rows("CatalogName\nA\nB\nC"), 3)

    def test_catalog_of(self):
        self.assertEqual(brief.catalog_of({"catalogName": "Zendesk1", "query": query("Salesforce1")}), "Zendesk1")
        self.assertEqual(brief.catalog_of({"query": query("Salesforce1")}), "Salesforce1")
        self.assertEqual(brief.catalog_of({"query": "select x from [My Sheet] . [dbo].[t]"}), "My Sheet")
        self.assertIsNone(brief.catalog_of({"query": "SELECT 1"}))
        self.assertIsNone(brief.catalog_of({}))

    def test_tool_text(self):
        self.assertEqual(brief.tool_text("plain"), "plain")
        self.assertEqual(brief.tool_text([{"type": "text", "text": "a"}, "junk", {"type": "text", "text": "b"}]),
                         "a\nb")
        self.assertEqual(brief.tool_text(None), "")

    def test_refusal_pattern(self):
        for text in (BLOCKED, "Cannot obtain connection", "Permission denied", "User is not authorized"):
            self.assertRegex(text, brief.REFUSAL)
        self.assertNotRegex("Error: syntax error near FROM", brief.REFUSAL)

    def test_mcp_config_is_read_only_and_holds_no_secret(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(brief, "STATE", Path(tmp)), \
                mock.patch.dict(os.environ, {"CONNECT_AI_PAT": PAT}):
            cfg = json.loads(brief.mcp_config().read_text())
        server = cfg["mcpServers"]["connectai"]
        self.assertEqual(server["type"], "http")
        ops = server["url"].split("?ops=")[1].split(",")
        self.assertIn("queryData", ops)
        self.assertFalse([op for op in ops if any(w in op.lower() for w in ("insert", "update", "delete",
                                                                             "procedure", "execute"))])
        self.assertEqual(server["headers"]["Authorization"], "Basic ${CONNECT_AI_BASIC}")
        self.assertNotIn(PAT, json.dumps(cfg))

    def test_instructions_name_the_column_contract(self):
        for col in ("Id", "Label", "Detail", "Hot"):
            self.assertIn(f"\n     {col} ", brief.INSTRUCTIONS)
        self.assertIn(f"Pick the {brief.MAX_ROOMS} connections", brief.INSTRUCTIONS)


class BuildMissionTest(unittest.TestCase):
    """Governance comes from Connect AI and the tool results, not from Claude's summary."""

    def build(self, final, calls, connections=()):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            client = mock.Mock()
            client.return_value.connections.return_value = list(connections)
            with mock.patch.object(brief, "STATE", state), mock.patch.object(brief, "MISSION", state / "m.json"), \
                    mock.patch.object(brief, "RUN", state / "run.jsonl"), \
                    mock.patch.object(brief.connect_ai_mcp, "Client", client):
                mission = brief.build_mission("q?", final, calls)
                self.assertEqual(json.loads((state / "m.json").read_text()), mission)
                self.events = [json.loads(line) for line in (state / "run.jsonl").read_text().splitlines()]
        return mission

    def call(self, tool, text, error=False, **args):
        return {"tool": tool, "args": args, "text": text, "error": error}

    def test_rooms_permissions_refusals_and_masks(self):
        rooms = [{"connection": f"C{i}", "app": f"App{i}", "title": "t", "query": "q"} for i in range(5)]
        final = "Here you go:\n```json\n" + json.dumps({"answer": "A.", "rooms": rooms}) + "\n```\nBye"
        calls = [
            self.call("queryData", BLOCKED, error=True, query=PII_SQL),
            self.call("queryData", "Error: syntax error", error=True, query="SELECT"),       # not a refusal
            self.call("queryData", "Id,SSN\n1,XXXX\n", query=query("C1")),
            self.call("getTables", "XXXX", catalogName="C1"),                                # only queryData masks
            self.call("queryData", "Id\n1\n", query=query("C0")),
        ]
        perms = [{"name": "C0", "permissions": ["Select", "Update"]}, {"name": "C1", "permissions": ["Select"]}]
        m = self.build(final, calls, perms)
        self.assertEqual(m["answer"], "A.")
        self.assertEqual(len(m["rooms"]), brief.MAX_ROOMS)
        self.assertEqual([(r["permissions"], r["writable"]) for r in m["rooms"]],
                         [(["Select", "Update"], True), (["Select"], False), (["Select"], False)])
        self.assertEqual(m["denied"], [{"connection": "PostgreSQLPII", "tool": "queryData",
                                        "request": {"query": PII_SQL},
                                        "error": "Request blocked: PII detected in inbound query"}])
        self.assertEqual([d["connection"] for d in m["masked"]], ["C1"])
        self.assertEqual(len(m["calls"]), 5)
        self.assertEqual(set(m["calls"][0]), {"tool", "args", "error"})          # no result text kept
        self.assertEqual(self.events[-1]["kind"], "mission")
        self.assertEqual(self.events[-1]["denied"], 1)

    def test_insert_permission_alone_is_writable(self):
        final = json.dumps({"answer": "a", "rooms": [{"connection": "C", "app": "A", "title": "t"}]})
        m = self.build(final, [], [{"name": "C", "permissions": ["Select", "Insert"]}])
        self.assertTrue(m["rooms"][0]["writable"])

    def test_final_without_json_becomes_the_answer(self):
        m = self.build("I could not find anything.", [])
        self.assertEqual((m["answer"], m["rooms"], m["denied"]), ("I could not find anything.", [], []))


class RunTest(unittest.TestCase):
    """brief.py end to end: a fake `claude` CLI streams a run, a fake MCP server answers the permissions."""

    def brief(self, *args, fail=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with FakeMCP() as mcp, fake_claude_env(self.tmp.name, mcp.url, fail=fail) as env:
            r = subprocess.run([sys.executable, str(ROOT / "brief.py"), *args], capture_output=True, text=True,
                               env=env, timeout=60)
        state = Path(self.tmp.name) / "state"
        self.claude = json.loads((Path(self.tmp.name) / "claude-call.json").read_text())
        self.events = [json.loads(line) for line in (state / "run.jsonl").read_text().splitlines()]
        return r, state

    def test_run(self):
        r, state = self.brief("Who is at risk?")
        self.assertEqual(r.returncode, 0, r.stderr)

        # how Claude Code was launched: only the Connect AI server, token via the environment
        argv = self.claude["argv"]
        self.assertEqual(argv[:2], ["-p", "Who is at risk?"])
        for flag in ("--strict-mcp-config", "--verbose", "--include-partial-messages"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__connectai")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--model") + 1], brief.MODEL)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], brief.INSTRUCTIONS)
        self.assertEqual(Path(argv[argv.index("--mcp-config") + 1]), state / "mcp.json")
        self.assertEqual(Path(self.claude["cwd"]).resolve(), state.resolve())
        self.assertEqual("Basic " + self.claude["basic"], FakeMCP.expected_auth())
        self.assertNotIn(PAT, (state / "mcp.json").read_text())

        # the panel's feed
        kinds = [e["kind"] for e in self.events]
        self.assertEqual(kinds[:3], ["question", "session", "text_delta"])
        self.assertEqual(kinds[-2:], ["done", "mission"])
        self.assertEqual(self.events[1]["tools"], ["mcp__connectai__queryData", "mcp__connectai__getCatalogs"])
        self.assertEqual(self.events[1]["model"], "claude-sonnet-5-5")
        self.assertEqual(kinds.count("call"), 5)
        self.assertEqual(kinds.count("result"), 5)
        self.assertEqual(kinds.count("thought"), 1)
        results = {e["id"]: e for e in self.events if e["kind"] == "result"}
        self.assertEqual((results["tu1"]["rows"], results["tu1"]["connection"]), (3, None))
        self.assertEqual((results["tu2"]["rows"], results["tu2"]["connection"]), (2, "Salesforce1"))
        self.assertEqual((results["tu3"]["error"], results["tu3"]["rows"]), (True, 0))
        self.assertEqual(results["tu5"]["connection"], "Zendesk1")
        self.assertTrue(all(e["ms"] >= 0 for e in results.values()))
        done = self.events[-2]
        self.assertEqual((done["cost_usd"], done["duration_ms"], done["turns"]), (0.12, 61000, 7))

        # the mission
        mission = json.loads((state / "mission.json").read_text())
        self.assertEqual(json.loads(r.stdout), {k: mission[k] for k in ("answer", "rooms", "denied", "masked")})
        self.assertEqual(mission["question"], "Who is at risk?")
        self.assertEqual(mission["answer"], PLAN["answer"])
        self.assertEqual([(r["app"], r["writable"]) for r in mission["rooms"]],
                         [("Salesforce", True), ("Zendesk", False)])
        self.assertEqual([d["connection"] for d in mission["denied"]], ["PostgreSQLPII", "Zendesk1"])
        self.assertEqual(mission["denied"][0]["error"], "Request blocked: PII detected in inbound query")
        self.assertEqual([m["connection"] for m in mission["masked"]], ["Zendesk1"])

    def test_default_question(self):
        r, _ = self.brief()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.claude["argv"][1], brief.DEFAULT_QUESTION)
        self.assertIn("123-45-6789", brief.DEFAULT_QUESTION)

    def test_claude_failure(self):
        r, state = self.brief("q", fail=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("claude exited 3", r.stderr)
        self.assertEqual(self.events[-1], {**self.events[-1], "kind": "failed", "text": "boom: model overloaded\n"})
        self.assertFalse((state / "mission.json").exists())


if __name__ == "__main__":
    unittest.main()
