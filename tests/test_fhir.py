"""FHIR R4 client, import and SMART launch against an in-process mock EHR that behaves like real
ones: HAPI-style paging links, 429 throttling, notes as Binary (HTML), medicationReference, a
cross-origin link that must not be followed, and a SMART authorization server checking PKCE."""

import base64
import hashlib
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hl7bridge import vocab
from hl7bridge.ai.access import AccessDenied
from hl7bridge.ai.index import Chunk, build_series
from hl7bridge.fhir_client import FhirClient, FhirError, html_to_text, rtf_to_text
from hl7bridge.fhir_import import build_bundle, import_resources, load_fixture
from hl7bridge.server import App, serve_http
from hl7bridge.smart import SmartError, SmartLauncher, pkce_pair
from hl7bridge.store import FhirStore

ROOT = Path(__file__).resolve().parent.parent
NOTE_HTML = "<html><body><h2>Progress note</h2><p>BP 150/95 on lisinopril 10 mg.</p><script>x()</script></body></html>"


def resources(origin):
    pid = "p1"
    obs = [{"resourceType": "Observation", "id": f"o{i}", "status": "final",
            "category": [{"coding": [{"system": vocab.OBS_CATEGORY, "code": "laboratory"}]}],
            "code": {"text": "Creatinine"}, "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": f"2020-0{1 + i % 9}-0{1 + i % 9}",
            "valueQuantity": {"value": 1.0 + i / 10, "unit": "mg/dL"}} for i in range(5)]
    return {
        "Patient": {"resourceType": "Patient", "id": pid, "gender": "female", "birthDate": "1950-01-01",
                    "name": [{"family": "Mock", "given": ["Ada"]}],
                    "identifier": [{"system": "urn:oid:1.2.840.114350.1.13.0.1.7.5.737384.14", "value": "E1234"},
                                   {"system": vocab.US_SSN, "value": "999-00-1234"}]},
        "Observation": obs,
        "MedicationRequest": [{"resourceType": "MedicationRequest", "id": "mr1", "status": "active",
                               "subject": {"reference": f"Patient/{pid}"}, "authoredOn": "2020-02-02",
                               "requester": {"reference": "Practitioner/dr1", "display": "Dr One"},
                               "medicationReference": {"reference": "Medication/m1"}}],
        "Condition": [{"resourceType": "Condition", "id": "c1", "subject": {"reference": f"Patient/{pid}"},
                       "code": {"text": "Hypertension"},
                       "clinicalStatus": {"coding": [{"code": "active"}]}}],  # no category: non-conformant
        "DocumentReference": [
            {"resourceType": "DocumentReference", "id": "d1", "status": "current",
             "type": {"text": "Progress note"}, "subject": {"reference": f"Patient/{pid}"},
             "category": [{"text": "clinical-note"}], "date": "2020-03-03T10:00:00Z",
             "content": [{"attachment": {"contentType": "text/html", "url": f"{origin}/fhir/Binary/b1"}}]},
            {"resourceType": "DocumentReference", "id": "d2", "status": "current",
             "type": {"text": "Outside note"}, "subject": {"reference": f"Patient/{pid}"},
             "category": [{"text": "clinical-note"}],
             "content": [{"attachment": {"contentType": "text/plain", "url": "https://evil.example/Binary/x"}}]}],
    }


class MockEHR(BaseHTTPRequestHandler):
    seen_auth: list = []
    throttled = False
    challenge = None

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200, headers=()):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/fhir+json")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        MockEHR.seen_auth.append(self.headers.get("Authorization"))
        data = resources(origin)
        if url.path == "/fhir/.well-known/smart-configuration":
            return self._json({"authorization_endpoint": f"{origin}/auth/authorize", "token_endpoint": f"{origin}/auth/token"})
        if url.path == "/fhir/Patient/p1":
            return self._json(data["Patient"])
        if url.path == "/fhir/Medication/m1":
            return self._json({"resourceType": "Medication", "id": "m1", "code": {"text": "Lisinopril 10 MG Oral Tablet"}})
        if url.path == "/fhir/Binary/b1":
            return self._json({"resourceType": "Binary", "contentType": "text/html",
                               "data": base64.b64encode(NOTE_HTML.encode()).decode()})
        if q.get("_getpages"):  # HAPI-style next page: served at the server root, no "/" before the query
            return self._json({"resourceType": "Bundle", "entry": [{"resource": r} for r in data["Observation"][3:]]})
        rtype = url.path.rsplit("/", 1)[-1]
        if rtype == "Observation":
            if not MockEHR.throttled:
                MockEHR.throttled = True
                return self._json({}, 429, [("Retry-After", "0")])
            return self._json({"resourceType": "Bundle", "entry": [{"resource": r} for r in data["Observation"][:3]],
                               "link": [{"relation": "next", "url": f"{origin}?_getpages=abc"}]})
        if rtype in data and rtype != "Patient":
            return self._json({"resourceType": "Bundle", "entry": [{"resource": r} for r in data[rtype]]})
        return self._json({"resourceType": "Bundle"})

    def do_POST(self):
        form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
        verifier_ok = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) \
            .rstrip(b"=").decode() == MockEHR.challenge
        if form.get("code") != "good-code" or not verifier_ok:
            return self._json({"error": "invalid_grant"}, 400)
        claims = base64.urlsafe_b64encode(json.dumps({"fhirUser": "Practitioner/dr1"}).encode()).decode().rstrip("=")
        return self._json({"access_token": "tok", "token_type": "Bearer", "patient": "p1",
                           "scope": "launch openid fhirUser patient/*.read", "id_token": f"e30.{claims}.sig"})


class MockServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.ehr = ThreadingHTTPServer(("127.0.0.1", 0), MockEHR)
        threading.Thread(target=cls.ehr.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.ehr.server_port}/fhir"

    @classmethod
    def tearDownClass(cls):
        cls.ehr.shutdown()
        cls.ehr.server_close()

    def setUp(self):
        MockEHR.seen_auth, MockEHR.throttled = [], False


class FhirClientTests(MockServerMixin, unittest.TestCase):
    def test_patient_record_paging_throttling_binary_and_medication(self):
        warnings, sleeps = [], []
        client = FhirClient(self.base, token="tok", sleep=sleeps.append)
        record = client.patient_record("p1", on_warning=warnings.append)
        by_type = {}
        for r in record:
            by_type.setdefault(r["resourceType"], []).append(r)
        self.assertEqual(len(by_type["Observation"]), 5)  # both pages, incl. HAPI-style next link
        self.assertEqual(sleeps, [0.0])  # 429 retried once
        self.assertEqual(by_type["MedicationRequest"][0]["medicationCodeableConcept"]["text"], "Lisinopril 10 MG Oral Tablet")
        note = base64.b64decode(by_type["DocumentReference"][0]["content"][0]["attachment"]["data"]).decode()
        self.assertEqual(note, "Progress note\nBP 150/95 on lisinopril 10 mg.")  # HTML stripped, script dropped
        self.assertTrue(any("evil.example" in w and "refusing" in w for w in warnings))
        self.assertTrue(all(a == "Bearer tok" for a in MockEHR.seen_auth))

    def test_origin_guard_is_exact(self):
        client = FhirClient("https://ehr.example/fhir")
        client._url("https://ehr.example?_getpages=1")  # same origin, no slash: allowed
        for bad in ("https://ehr.example.evil.com/fhir/x", "http://ehr.example/fhir/x", "https://evil.com/?https://ehr.example/"):
            with self.assertRaises(FhirError, msg=bad):
                client._url(bad)
        with self.assertRaises(FhirError):
            client.read("Patient", "../../etc")

    def test_note_formats(self):
        self.assertEqual(rtf_to_text(r"{\rtf1\ansi {\fonttbl\f0 Arial;}\f0 BP stable\par Continue meds\par}"),
                         "BP stable\nContinue meds")
        self.assertEqual(html_to_text("<p>a</p><style>p{}</style><p>b</p>"), "a\nb")


class FhirImportTests(MockServerMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = FhirStore()
        self.addCleanup(self.store.close)
        self.record = FhirClient(self.base, token="tok", sleep=lambda s: None).patient_record("p1")

    def test_import_lineage_privacy_and_idempotency(self):
        first = import_resources(self.store, self.record, self.base)
        self.assertEqual(first["status"], "accepted")
        patient = self.store.read("Patient", first["patient"])
        systems = {i["system"] for i in patient["identifier"]}
        self.assertNotIn(vocab.US_SSN, systems)
        self.assertIn("urn:oid:1.2.840.114350.1.13.0.1.7.5.737384.14", systems)  # Epic-style MRN kept
        obs = self.store.search("Observation", {"patient": first["patient"]})
        self.assertEqual(len(obs), 5)
        self.assertTrue(all(o["meta"]["source"].startswith(f"urn:fhir:127.0.0.1:{self.ehr.server_port}#Observation/")
                            for o in obs))
        med = self.store.search("MedicationRequest", {})[0]
        self.assertEqual(med["requester"], {"identifier": {"system": f"urn:fhir-source:127.0.0.1:{self.ehr.server_port}/Practitioner",
                                                           "value": "dr1"}, "display": "Dr One"})
        cond = self.store.search("Condition", {})[0]
        self.assertEqual(cond["meta"]["tag"][0]["code"], "us-core-nonconformant")  # kept, flagged
        counts = self.store.counts()
        import_resources(self.store, self.record, self.base)
        self.assertEqual({k: v for k, v in self.store.counts().items() if k != "Provenance"},
                         {k: v for k, v in counts.items() if k != "Provenance"})

    def test_bundle_without_patient_is_rejected(self):
        with self.assertRaises(ValueError):
            import_resources(self.store, [r for r in self.record if r["resourceType"] != "Patient"], self.base)

    def test_synthea_fixtures_import_cleanly(self):
        total = 0
        for path in sorted((ROOT / "samples" / "synthea").glob("*.json.gz")):
            base, res = load_fixture(path)
            bundle, warnings = build_bundle(res, base)
            self.assertFalse([w for w in warnings if "skipped:" in w], path.name)
            total += len(bundle["entry"])
        self.assertGreater(total, 5000)


class SmartLaunchTests(MockServerMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = App(FhirStore())
        self.app.smart = SmartLauncher(client_id="test-app", issuers=(self.base,))
        self.httpd = serve_http(self.app, port=0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.web = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def get(self, path, cookie=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(self.web + path, headers={"Cookie": cookie} if cookie else {})
        try:
            with opener.open(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            e.close()
            return e.code, dict(e.headers), body

    def test_ehr_launch_end_to_end(self):
        status, headers, _ = self.get(f"/smart/launch?iss={urllib.parse.quote(self.base)}&launch=abc")
        self.assertEqual(status, 302)
        auth = urllib.parse.urlsplit(headers["Location"])
        params = dict(urllib.parse.parse_qsl(auth.query))
        self.assertEqual(auth.path, "/auth/authorize")
        self.assertEqual((params["launch"], params["aud"], params["code_challenge_method"], params["client_id"]),
                         ("abc", self.base, "S256", "test-app"))
        self.assertIn("patient/*.read", params["scope"])
        MockEHR.challenge = params["code_challenge"]
        status, headers, _ = self.get(f"/smart/callback?code=good-code&state={params['state']}")
        self.assertEqual((status, headers["Location"]), (302, "/?launched=1"))
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, body = self.get("/api/session", cookie)
        context = json.loads(body)
        self.assertEqual(context["user"], "smart:Practitioner/dr1")
        self.assertEqual(context["imported"]["Observation"], 5)
        self.assertNotIn("access_token", json.dumps(context))
        # the session can ask about exactly the launched patient
        req = urllib.request.Request(self.web + "/ai/ask", method="POST", headers={"Cookie": cookie},
                                     data=json.dumps({"question": "most recent creatinine"}).encode())
        with urllib.request.urlopen(req) as r:
            answer = json.load(r)
        self.assertIn("Creatinine", answer["answer"])
        # replaying the state is refused (single use)
        status, _, _ = self.get(f"/smart/callback?code=good-code&state={params['state']}")
        self.assertEqual(status, 403)

    def test_launch_refuses_unlisted_issuer_and_bad_pkce(self):
        status, _, _ = self.get("/smart/launch?iss=https%3A%2F%2Fevil.example%2Ffhir&launch=x")
        self.assertEqual(status, 400)
        status, headers, _ = self.get(f"/smart/launch?iss={urllib.parse.quote(self.base)}&launch=abc")
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(headers["Location"]).query))["state"]
        MockEHR.challenge = "not-the-challenge"
        status, _, _ = self.get(f"/smart/callback?code=good-code&state={state}")
        self.assertEqual(status, 403)

    def test_launched_user_sees_only_the_launched_patient(self):
        _, headers, _ = self.get(f"/smart/launch?iss={urllib.parse.quote(self.base)}&launch=abc")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(headers["Location"]).query))
        MockEHR.challenge = params["code_challenge"]
        self.get(f"/smart/callback?code=good-code&state={params['state']}")
        other = self.app.store.create({"resourceType": "Patient", "identifier": [
            {"system": "urn:fhir-source:other", "value": "x"}]})
        with self.assertRaises(AccessDenied):
            self.app.assistant.ask(other["id"], "anything", user="smart:Practitioner/dr1")


class IdentityAndSeriesTests(unittest.TestCase):
    def test_pkce_pair(self):
        verifier, challenge = pkce_pair()
        self.assertEqual(challenge, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode())

    def test_smart_issuer_allowlist(self):
        with self.assertRaises(SmartError):
            SmartLauncher(issuers=("https://ok.example/fhir",)).authorize_url("https://evil.example/fhir", "http://x/cb")

    def test_identifier_validation_and_system_wide_panels(self):
        app = App(FhirStore())
        self.addCleanup(app.store.close)
        for system, value in (("urn:x", "a' or 1 eq 1"), ("javascript:alert(1)", "1"), ("urn:x", "")):
            with self.assertRaises(AccessDenied):
                app.assistant.resolve_identifier(system, value)
        p = app.store.create({"resourceType": "Patient", "identifier": [
            {"system": "urn:fhir-source:r4.smarthealthit.org", "value": "abc"}]})
        clinician, _ = app.assistant.authorize("dr-primary", p["id"])  # panel grants the sandbox's whole id space
        self.assertEqual(clinician.user_id, "dr-primary")
        with self.assertRaises(AccessDenied):
            app.assistant.authorize("dr-other-site", p["id"])

    def test_series_newest_first_and_one_offs_untouched(self):
        labs = [Chunk(f"Observation/{i}", "Patient/1", "lab", f"Creatinine {v} mg/dL on 2020-0{i}-01.", f"Observation/{i}",
                      date=f"2020-0{i}-01") for i, v in ((1, "1.1"), (3, "1.4"), (2, "1.2"))]
        single = Chunk("Observation/9", "Patient/1", "lab", "Potassium 5.6 mmol/L on 2020-01-01.", "Observation/9",
                       date="2020-01-01")
        out = build_series(labs + [single])
        series = next(c for c in out if c.id.endswith("#series"))
        self.assertTrue(series.text.startswith("Creatinine: 3 results, most recent first: 1.4 mg/dL on 2020-03-01"))
        self.assertEqual(series.resource, "Observation/3")  # cites the latest result
        self.assertIn(single, out)


if __name__ == "__main__":
    unittest.main()
