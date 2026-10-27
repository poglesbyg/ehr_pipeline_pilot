import base64
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

from hl7bridge.__main__ import _seed
from hl7bridge.mcp import McpServer
from hl7bridge.server import App
from hl7bridge.store import FhirStore

ROOT = Path(__file__).resolve().parent.parent
ICN = "1013579246V135792"


def rpc(mid, method, params=None):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class McpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App(FhirStore())
        _seed(cls.app, quiet=True)

    @classmethod
    def tearDownClass(cls):
        cls.app.store.close()

    def setUp(self):
        self.server = McpServer(self.app)
        self.server.handle(rpc(0, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "test-agent", "version": "1"}}))

    def call(self, name, **arguments):
        reply = self.server.handle(rpc(1, "tools/call", {"name": name, "arguments": arguments}))
        return reply["result"]

    def pid(self):
        return self.call("find_patient", identifier=ICN)["structuredContent"]["patients"][0]["patient_id"]

    def audits_by(self, who):
        return [a for a in self.app.store.search("AuditEvent", {"_count": 1000})
                if any(who in ag["who"]["display"] for ag in a["agent"])]

    # -- protocol -------------------------------------------------------------
    def test_initialize_negotiates_version(self):
        r = McpServer(self.app).handle(rpc(1, "initialize", {"protocolVersion": "2024-11-05"}))["result"]
        self.assertEqual(r["protocolVersion"], "2024-11-05")
        self.assertEqual(r["capabilities"], {"tools": {"listChanged": False}})
        r = McpServer(self.app).handle(rpc(1, "initialize", {"protocolVersion": "1999-01-01"}))["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")

    def test_errors_and_notifications(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.server.handle(rpc(1, "nope"))["error"]["code"], -32601)
        self.assertEqual(self.server.handle(rpc(1, "tools/call", {"name": "nope"}))["error"]["code"], -32602)
        self.assertEqual(self.server.handle({"id": 1})["error"]["code"], -32600)
        self.assertEqual(self.server.handle(rpc(1, "ping"))["result"], {})

    def test_read_only_by_default(self):
        tools = self.server.handle(rpc(1, "tools/list"))["result"]["tools"]
        self.assertEqual([t["name"] for t in tools],
                         ["find_patient", "search_record", "ask_record", "convert_hl7v2", "convert_ccda"])
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
        self.assertTrue(all(t["inputSchema"]["additionalProperties"] is False for t in tools))
        writable = McpServer(self.app, allow_write=True).handle(rpc(1, "tools/list"))["result"]["tools"]
        self.assertIn("ingest_ccda", [t["name"] for t in writable])

    # -- tools ----------------------------------------------------------------
    def test_find_patient_reports_merges_and_is_audited(self):
        dup = self.call("find_patient", identifier="1019999999V999999")["structuredContent"]["patients"][0]
        self.assertFalse(dup["active"])
        self.assertEqual(dup["replaced_by"], f"Patient/{self.pid()}")
        self.assertTrue(self.call("find_patient")["isError"])
        self.assertTrue(self.audits_by("mcp:test-agent"))

    def test_search_record_is_scoped_cited_and_screened(self):
        r = self.call("search_record", patient_id=self.pid(), query="allergies")
        data = r["structuredContent"]
        self.assertFalse(r["isError"])
        trusts = {c["trust"] for c in data["results"]}
        self.assertIn("hie", trusts)
        self.assertTrue(all(c["source"].startswith(("urn:hl7v2:", "urn:ccda:")) for c in data["results"]))
        self.assertEqual(len(data["quarantined"]), 1)  # the planted fax injection never reaches the agent
        self.assertNotIn("ignore previous instructions", json.dumps(data["results"]).lower())

    def test_ask_record_is_grounded_and_audited_with_query(self):
        pid = self.pid()  # find_patient is audited too, so count after it
        before = len(self.audits_by("mcp:test-agent"))
        r = self.call("ask_record", patient_id=f"Patient/{pid}", question="Was the potassium really elevated?")
        data = r["structuredContent"]
        self.assertIn("5.6", data["answer"])
        self.assertEqual(data["guardrails"]["groundedness"], 1.0)
        self.assertTrue(data["citations"])
        audits = self.audits_by("mcp:test-agent")
        self.assertEqual(len(audits), before + 1)
        query = next(e["query"] for a in audits for e in a["entity"] if "query" in e and "what" in e)
        self.assertTrue(base64.b64decode(query).decode())

    def test_unknown_patient_is_tool_error_not_crash(self):
        r = self.call("ask_record", patient_id="does-not-exist", question="anything")
        self.assertTrue(r["isError"])
        self.assertIn("find_patient", r["content"][0]["text"])

    def test_conversions_are_dry_runs(self):
        before = self.app.store.counts()
        hl7 = (ROOT / "samples" / "03_oru_r01_cardiac.hl7").read_text().replace("999-LAB-0043", "MCP-DRY-1")
        r = self.call("convert_hl7v2", message=hl7)["structuredContent"]
        self.assertEqual(r["status"], "accepted")
        self.assertIn("MSA|AA|MCP-DRY-1", r["ack"])
        self.assertEqual(r["bundle"]["type"], "transaction")
        ccd = (ROOT / "samples" / "hie" / "10_ccd_three_rivers.xml").read_text()
        c = self.call("convert_ccda", document=ccd, include_bundle=False)["structuredContent"]
        self.assertEqual(c["custodian"], "Three Rivers Community Hospital")
        self.assertNotIn("bundle", c)
        self.assertTrue(self.call("convert_ccda", document="<!DOCTYPE x><x/>")["isError"])
        counts = self.app.store.counts()
        self.assertEqual({k: v for k, v in counts.items() if k != "AuditEvent"},
                         {k: v for k, v in before.items() if k != "AuditEvent"})

    def test_ingest_only_with_allow_write(self):
        store = FhirStore()
        self.addCleanup(store.close)
        server = McpServer(App(store), allow_write=True)
        ccd = (ROOT / "samples" / "hie" / "10_ccd_three_rivers.xml").read_text()
        reply = server.handle(rpc(1, "tools/call", {"name": "ingest_ccda", "arguments": {"document": ccd}}))
        self.assertEqual(reply["result"]["structuredContent"]["status"], "accepted")
        self.assertEqual(store.counts()["Condition"], 3)
        self.assertEqual(self.server.handle(rpc(1, "tools/call", {"name": "ingest_ccda",
                                                                  "arguments": {"document": ccd}}))["error"]["code"],
                         -32602)

    def test_serve_loop_handles_parse_errors_and_batches(self):
        lines = "\n".join([
            "not json",
            json.dumps([rpc(5, "ping"), {"jsonrpc": "2.0", "method": "notifications/initialized"}]),
            json.dumps(rpc(6, "tools/list")),
        ]) + "\n"
        out = io.StringIO()
        self.server.serve(io.StringIO(lines), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1], [{"jsonrpc": "2.0", "id": 5, "result": {}}])
        self.assertEqual(replies[2]["id"], 6)


class StdioProcessTests(unittest.TestCase):
    def test_stdout_carries_only_protocol(self):
        msgs = [rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "subprocess", "version": "1"}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {"name": "ask_record", "arguments": {"patient_id": "x"}})]
        proc = subprocess.run([sys.executable, "-m", "hl7bridge", "mcp", "--seed"], cwd=ROOT, timeout=30,
                              input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True)
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        replies = [json.loads(line) for line in lines]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "hl7bridge")
        self.assertTrue(replies[1]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
