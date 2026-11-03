"""Azure prototype code against mock Azure endpoints: Entra token sources (managed identity,
IMDS, Azure CLI), the AI Search adapter's REST contract (schema, uploads, prefiltered hybrid
queries, deletes), and the assistant end to end on top of it, including a search service that
ignores the filter, which the assistant's second scope check must catch."""

import json
import os
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hl7bridge.ai.access import ScopeViolation
from hl7bridge.ai.assistant import ClinicalAssistant
from hl7bridge.ai.index import resource_to_chunks
from hl7bridge.ai.azure_search import AzureOpenAIEmbedder, AzureSearchError, AzureSearchIndex
from hl7bridge.azure_auth import AzureAuthError, TokenProvider
from hl7bridge.pipeline import Pipeline
from hl7bridge.store import FhirStore

ROOT = Path(__file__).resolve().parent.parent
ICN = "1013579246V135792"


class MockAzure(BaseHTTPRequestHandler):
    """Just enough of AI Search + Azure OpenAI embeddings to exercise the adapter."""
    docs: dict = {}
    requests: list = []
    ignore_filter = False
    schema = None

    def log_message(self, *a):
        pass

    def _send(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        MockAzure.requests.append(("PUT", self.path, body, self.headers.get("Authorization")))
        MockAzure.schema = body
        self._send(body, 201)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        MockAzure.requests.append(("POST", self.path, body, self.headers.get("Authorization")))
        if "/embeddings" in self.path:
            return self._send({"data": [{"index": i, "embedding": [float(len(t) % 7), 1.0, 0.5]}
                                        for i, t in enumerate(body["input"])]})
        if MockAzure.schema is None:
            return self._send({"error": {"message": "index not found"}}, 404)
        if self.path.split("?")[0].endswith("/docs/index"):
            for d in body["value"]:
                if d["@search.action"] == "delete":
                    MockAzure.docs.pop(d["key"], None)
                else:
                    MockAzure.docs[d["key"]] = d
            return self._send({"value": [{"key": d["key"], "status": True} for d in body["value"]]})
        # search: honor "search.in(patient, 'a,b', ',')" / "patient eq 'x'" and the protected clause
        f = body.get("filter", "")
        allowed = set(re.search(r"search\.in\(patient, '([^']*)'", f).group(1).split(",")) if "search.in(patient" in f \
            else {re.search(r"patient eq '([^']*)'", f).group(1)} if "patient eq" in f else None
        labels = re.search(r"protected/all\(p: search\.in\(p, '([^']*)'", f)
        readable = set(filter(None, labels.group(1).split(","))) if labels else None
        terms = set(body["search"].lower().split()) - {"*"}
        hits = []
        for d in MockAzure.docs.values():
            if not MockAzure.ignore_filter:
                if allowed is not None and d["patient"] not in allowed:
                    continue
                if readable is not None and not set(d["protected"]) <= readable:
                    continue
            score = sum(1 for t in terms if t in d["text"].lower()) or (1 if not terms else 0)
            if score:
                hits.append({**{k: d[k] for k in ("key", "chunk")}, "@search.score": score})
        hits.sort(key=lambda h: -h["@search.score"])
        self._send({"value": hits[: body.get("top", 50)]})


class AzureMockMixin:
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockAzure)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        MockAzure.docs, MockAzure.requests, MockAzure.ignore_filter, MockAzure.schema = {}, [], False, None

    def index(self, embed=True):
        token = lambda: "entra-token"  # noqa: E731
        embedder = AzureOpenAIEmbedder(self.base, "embed", token) if embed else None
        return AzureSearchIndex(self.base, "hl7bridge-evidence", token, embedder=embedder, sleep=lambda s: None)


class TokenProviderTests(unittest.TestCase):
    def opener(self, captured):
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"access_token": "mi-token", "expires_on": "9999999999"}).encode()

        def open_(req, timeout=None):
            captured.append(req)
            return Resp()
        return open_

    def test_app_service_container_apps_identity_endpoint(self):
        seen = []
        env = {"IDENTITY_ENDPOINT": "http://localhost:42356/msi/token", "IDENTITY_HEADER": "h", "AZURE_CLIENT_ID": "cid"}
        with mock.patch.dict(os.environ, env, clear=False):
            tp = TokenProvider("openai", opener=self.opener(seen))
            self.assertEqual((tp(), tp()), ("mi-token", "mi-token"))  # second call served from cache
        self.assertEqual(len(seen), 1)
        self.assertIn("resource=https%3A%2F%2Fcognitiveservices.azure.com", seen[0].full_url)
        self.assertIn("client_id=cid", seen[0].full_url)
        self.assertEqual(seen[0].get_header("X-identity-header"), "h")

    def test_imds_with_user_assigned_identity_and_government_audience(self):
        seen = []
        env = {"AZURE_CLIENT_ID": "cid", "AZURE_CLOUD": "usgovernment"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("IDENTITY_ENDPOINT", None)
            tp = TokenProvider("search", opener=self.opener(seen))
            tp()
        self.assertTrue(seen[0].full_url.startswith("http://169.254.169.254/metadata/identity/oauth2/token"))
        self.assertIn("resource=https%3A%2F%2Fsearch.azure.us", seen[0].full_url)
        self.assertEqual(seen[0].get_header("Metadata"), "true")

    def test_azure_cli_fallback(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"accessToken": "cli-token", "expires_on": 9999999999}),
                                   stderr="")
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("IDENTITY_ENDPOINT", "AZURE_CLIENT_ID", "AZURE_USE_IMDS", "AZURE_CLOUD"):
                os.environ.pop(k, None)
            tp = TokenProvider("openai", runner=run)
            self.assertEqual(tp(), "cli-token")
        self.assertEqual(calls[0][:4], ["az", "account", "get-access-token", "--resource"])

    def test_cli_failure_is_reported(self):
        run = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="Please run 'az login'")  # noqa: E731
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("IDENTITY_ENDPOINT", "AZURE_CLIENT_ID", "AZURE_USE_IMDS"):
                os.environ.pop(k, None)
            with self.assertRaises(AzureAuthError):
                TokenProvider("openai", runner=run)()


class AzureSearchAdapterTests(AzureMockMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = FhirStore()
        self.addCleanup(self.store.close)
        p = Pipeline(self.store)
        for f in sorted((ROOT / "samples").glob("*.hl7")):
            p.process(f.read_text())

    def test_scope_filter_validates_instead_of_escaping(self):
        f = AzureSearchIndex.scope_filter(["Patient/a1", "Patient/b-2"], frozenset({"ETH"}))
        self.assertEqual(f, "search.in(patient, 'Patient/a1,Patient/b-2', ',') and "
                            "protected/all(p: search.in(p, 'ETH', ','))")
        self.assertIn("search.in(p, '', ',')", AzureSearchIndex.scope_filter(["Patient/a"], frozenset()))
        for bad in (["Patient/a' or true or 'x"], [], ["Practitioner/1"]):
            with self.assertRaises(AzureSearchError):
                AzureSearchIndex.scope_filter(bad, frozenset())
        with self.assertRaises(AzureSearchError):
            AzureSearchIndex.scope_filter(["Patient/a"], frozenset({"XYZ"}))

    def test_rest_contract_schema_upload_and_prefiltered_hybrid_query(self):
        assistant = ClinicalAssistant(self.store, index=self.index())
        pid = assistant.resolve_icn(ICN)
        r = assistant.ask(pid, "Was the potassium really elevated?", user="dr-primary")
        self.assertIn("5.6", r["answer"])
        puts = [q for q in MockAzure.requests if q[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        vector = next(f for f in MockAzure.schema["fields"] if f["name"] == "content_vector")
        self.assertEqual(vector["dimensions"], 3)  # from the first embeddings batch
        search = next(b for m, path, b, _ in MockAzure.requests if path.startswith("/indexes/hl7bridge-evidence/docs/search")
                      and "vectorQueries" in b)
        self.assertEqual(search["vectorFilterMode"], "preFilter")
        self.assertTrue(search["filter"].startswith("search.in(patient, 'Patient/"))
        self.assertIn("protected/all(p: search.in(p, '', ','))", search["filter"])
        self.assertTrue(all(auth == "Bearer entra-token" for *_, auth in MockAzure.requests))
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_\-=]+", k) for k in MockAzure.docs))

    def test_protected_records_filtered_in_the_service(self):
        Pipeline(self.store).process((ROOT / "samples" / "10_mdm_t02_sud_clinic_note.hl7").read_text())
        assistant = ClinicalAssistant(self.store, index=self.index(embed=False))
        pid = assistant.resolve_icn(ICN)
        primary = assistant.search(pid, "naltrexone alcohol", user="dr-primary")
        self.assertFalse(any(c["protected"] for c in primary["results"]))
        sud = assistant.search(pid, "naltrexone alcohol", user="dr-sud-clinic")
        self.assertTrue(any("ETH" in c["protected"] for c in sud["results"]))

    def test_service_ignoring_the_filter_is_caught_by_the_second_check(self):
        assistant = ClinicalAssistant(self.store, index=self.index(embed=False))
        pid = assistant.resolve_icn(ICN)
        other = self.store.create({"resourceType": "Patient", "identifier": [
            {"system": assistant.icn_system, "value": "1000000003V000003"}]})
        self.store.create({"resourceType": "Observation", "status": "final", "code": {"text": "Potassium"},
                           "subject": {"reference": f"Patient/{other['id']}"}, "valueQuantity": {"value": 9.9}})
        assistant.index.add_patient_chunks(f"Patient/{other['id']}", [
            c for r in self.store.everything(other["id"]) for c in resource_to_chunks(r)])
        assistant.search(pid, "potassium", user="dr-primary")  # index this patient with filtering intact
        MockAzure.ignore_filter = True  # now simulate a bug in the managed service's query filtering
        with self.assertRaises(ScopeViolation):
            assistant.ask(pid, "potassium", user="dr-primary")

    def test_drop_patient_deletes_remote_documents_even_in_a_fresh_process(self):
        index = self.index(embed=False)
        assistant = ClinicalAssistant(self.store, index=index)
        pid = assistant.resolve_icn(ICN)
        assistant.ask(pid, "potassium", user="dr-primary")
        self.assertTrue(MockAzure.docs)
        fresh = self.index(embed=False)  # new process: knows nothing locally
        fresh.drop_patient(f"Patient/{pid}")
        self.assertFalse([d for d in MockAzure.docs.values() if d["patient"] == f"Patient/{pid}"])

    def test_drop_before_index_exists_is_a_no_op(self):
        self.index(embed=False).drop_patient("Patient/abc")  # mock returns 404: nothing to delete


if __name__ == "__main__":
    unittest.main()
