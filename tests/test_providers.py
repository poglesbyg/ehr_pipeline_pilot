"""Azure OpenAI provider against a local mock of the chat-completions endpoint.

This proves the request contract (URL, auth header, deterministic settings, prompt shape) and
that guardrails still apply to hosted-model output. It is not a substitute for running the
eval against a real deployment.
"""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from hl7bridge.ai.assistant import ClinicalAssistant
from hl7bridge.ai.providers import SYSTEM_PROMPT, AzureOpenAIProvider, OllamaProvider, make_provider
from hl7bridge.pipeline import Pipeline
from hl7bridge.store import FhirStore

ROOT = Path(__file__).resolve().parent.parent
ICN = "urn:oid:2.16.840.1.113883.4.349|1013579246V135792"


class MockAzure(BaseHTTPRequestHandler):
    requests: list = []
    reply = ""
    throttle = 0  # respond 429 this many times first

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        MockAzure.requests.append({"path": self.path, "api_key": self.headers.get("api-key"),
                                   "authorization": self.headers.get("Authorization"), "body": body})
        if MockAzure.throttle:
            MockAzure.throttle -= 1
            self.send_response(429)
            self.send_header("Retry-After", "2")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/api/chat":  # Ollama shape
            data = json.dumps({"model": body["model"], "done": True,
                               "message": {"role": "assistant", "content": MockAzure.reply}}).encode()
        else:
            data = json.dumps({"model": "gpt-4o-2024-08-06",
                               "choices": [{"message": {"role": "assistant", "content": MockAzure.reply}}]}).encode()
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
        self.assistant.ask(self.pid, "potassium", user="dr-primary")
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
        r = self.assistant.ask(self.pid, "potassium", user="dr-primary")
        self.assertEqual(r["provider"], "azure-openai")
        self.assertEqual(r["answer"], "Potassium was 5.6 mmol/L [1].")
        self.assertEqual(r["guardrails"]["groundedness"], 0.5)
        self.assertEqual(r["guardrails"]["unsupported"][0]["reasons"], ["numbers not in cited evidence: ['6.4']"])

    def test_entra_token_retry_on_throttling_and_model_version_audited(self):
        sleeps = []
        provider = AzureOpenAIProvider(endpoint=f"http://127.0.0.1:{self.server.server_port}", deployment="gpt-4o-gov",
                                       token_provider=lambda: "entra-token", sleep=sleeps.append)
        MockAzure.reply, MockAzure.throttle = "Potassium was 5.6 mmol/L [1].", 2
        r = ClinicalAssistant(self.store, provider).ask(self.pid, "potassium", user="dr-primary")
        self.assertEqual(sleeps, [2.0, 2.0])  # honored Retry-After twice, then succeeded
        self.assertEqual(MockAzure.requests[-1]["authorization"], "Bearer entra-token")
        self.assertIsNone(MockAzure.requests[-1]["api_key"])
        self.assertEqual(r["model_version"], "gpt-4o-2024-08-06")
        audit = self.store.search("AuditEvent", {"_count": 1})[0]
        self.assertIn("model:azure-openai (gpt-4o-2024-08-06)", [a["who"]["display"] for a in audit["agent"]])

    def test_unreachable_endpoint_falls_back(self):
        down = ClinicalAssistant(self.store, AzureOpenAIProvider(endpoint="http://127.0.0.1:9", deployment="x",
                                                                 api_key="k", timeout=1))
        r = down.ask(self.pid, "potassium", user="dr-primary")
        self.assertTrue(r["provider"].startswith("extractive (fallback"))
        self.assertIn("5.6", r["answer"])


class OllamaProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), MockAzure)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.store = FhirStore()
        Pipeline(cls.store).process((ROOT / "samples" / "02_oru_r01_bmp.hl7").read_text())
        cls.pid = cls.store.search("Patient", {"identifier": ICN})[0]["id"]
        cls.provider = OllamaProvider("llama3.1:8b", host=f"127.0.0.1:{cls.server.server_port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def setUp(self):
        MockAzure.requests = []

    def test_request_contract_and_reasoning_stripped(self):
        MockAzure.reply = "<think>check evidence 1</think>Potassium was 5.6 mmol/L [1]."
        r = ClinicalAssistant(self.store, self.provider).ask(self.pid, "potassium", user="dr-primary")
        req = MockAzure.requests[0]
        self.assertEqual(req["path"], "/api/chat")
        self.assertEqual(req["body"]["model"], "llama3.1:8b")
        self.assertFalse(req["body"]["stream"])
        self.assertEqual(req["body"]["options"]["temperature"], 0)
        self.assertEqual(req["body"]["messages"][0]["content"], SYSTEM_PROMPT)
        self.assertEqual(r["answer"], "Potassium was 5.6 mmol/L [1].")
        self.assertEqual(r["provider"], "ollama:llama3.1:8b")

    def test_reasoning_model_output_normalized(self):
        MockAzure.reply = "Potassium was **5.6\u202fmmol/L**【1】."
        provider = OllamaProvider("gpt-oss:20b", host=f"127.0.0.1:{self.server.server_port}")
        r = ClinicalAssistant(self.store, provider).ask(self.pid, "potassium", user="dr-primary")
        self.assertEqual(MockAzure.requests[0]["body"]["think"], "low")
        self.assertEqual(r["answer"], "Potassium was 5.6 mmol/L [1].".replace(" [1]", "[1]"))
        self.assertEqual(r["guardrails"]["groundedness"], 1.0)

    def test_empty_answer_falls_back(self):
        MockAzure.reply = ""
        r = ClinicalAssistant(self.store, self.provider).ask(self.pid, "potassium", user="dr-primary")
        self.assertIn("fallback: RuntimeError", r["provider"])
        self.assertIn("5.6", r["answer"])

    def test_cloud_models_refused_by_default(self):
        for model in ("gpt-oss:120b-cloud", "minimax-m2.5:cloud"):
            with self.assertRaises(ValueError):
                OllamaProvider(model)
        self.assertEqual(OllamaProvider("gpt-oss:120b-cloud", allow_cloud=True).name, "ollama:gpt-oss:120b-cloud")

    def test_auto_selection(self):
        saved = {k: os.environ.pop(k, None) for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "OLLAMA_MODEL")}
        try:
            self.assertEqual(make_provider().name, "extractive")
            os.environ["OLLAMA_MODEL"] = "qwen3:8b"
            self.assertEqual(make_provider().name, "ollama:qwen3:8b")
        finally:
            os.environ.pop("OLLAMA_MODEL", None)
            os.environ.update({k: v for k, v in saved.items() if v is not None})


if __name__ == "__main__":
    unittest.main()
