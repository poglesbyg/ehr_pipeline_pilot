"""Azure OpenAI provider against a local mock of the chat-completions endpoint.

This proves the request contract (URL, auth header, deterministic settings, prompt shape) and
that guardrails still apply to hosted-model output. It is not a substitute for running the
eval against a real deployment.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from hl7bridge.ai.assistant import ClinicalAssistant
from hl7bridge.ai.providers import SYSTEM_PROMPT, AzureOpenAIProvider
from hl7bridge.pipeline import Pipeline
from hl7bridge.store import FhirStore

ROOT = Path(__file__).resolve().parent.parent
ICN = "urn:oid:2.16.840.1.113883.4.349|1013579246V135792"


class MockAzure(BaseHTTPRequestHandler):
    requests: list = []
    reply = ""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        MockAzure.requests.append({"path": self.path, "api_key": self.headers.get("api-key"), "body": body})
        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": MockAzure.reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AzureProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), MockAzure)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.store = FhirStore()
        Pipeline(cls.store).process((ROOT / "samples" / "02_oru_r01_bmp.hl7").read_text())
        cls.pid = cls.store.search("Patient", {"identifier": ICN})[0]["id"]
        provider = AzureOpenAIProvider(endpoint=f"http://127.0.0.1:{cls.server.server_port}/",
                                       deployment="gpt-4o-gov", api_key="test-key")
        cls.assistant = ClinicalAssistant(cls.store, provider)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def setUp(self):
        MockAzure.requests = []

    def test_request_contract(self):
        MockAzure.reply = "Potassium was 5.6 mmol/L [1]."
        self.assistant.ask(self.pid, "potassium")
        req = MockAzure.requests[0]
        self.assertEqual(req["path"], "/openai/deployments/gpt-4o-gov/chat/completions?api-version=2024-10-21")
        self.assertEqual(req["api_key"], "test-key")
        self.assertEqual(req["body"]["temperature"], 0)
        self.assertEqual(req["body"]["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})
        user = req["body"]["messages"][1]["content"]
        self.assertTrue(user.startswith("<evidence>\n[1] (lab, structured, 2026-10-20) Potassium 5.6 mmol/L"))
        self.assertTrue(user.endswith("Question: potassium"))

    def test_hosted_hallucination_is_removed_by_guardrails(self):
        MockAzure.reply = "Potassium was 5.6 mmol/L [1]. Potassium was 6.4 mmol/L on repeat [1]."
        r = self.assistant.ask(self.pid, "potassium")
        self.assertEqual(r["provider"], "azure-openai")
        self.assertEqual(r["answer"], "Potassium was 5.6 mmol/L [1].")
        self.assertEqual(r["guardrails"]["groundedness"], 0.5)
        self.assertEqual(r["guardrails"]["unsupported"][0]["reasons"], ["numbers not in cited evidence: ['6.4']"])

    def test_unreachable_endpoint_falls_back(self):
        down = ClinicalAssistant(self.store, AzureOpenAIProvider(endpoint="http://127.0.0.1:9", deployment="x",
                                                                 api_key="k", timeout=1))
        r = down.ask(self.pid, "potassium")
        self.assertTrue(r["provider"].startswith("extractive (fallback"))
        self.assertIn("5.6", r["answer"])


if __name__ == "__main__":
    unittest.main()
