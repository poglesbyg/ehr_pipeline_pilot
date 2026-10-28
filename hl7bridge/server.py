"""HTTP surface: a FHIR R4 REST API, HL7 v2 ingest over HTTP, the clinical assistant, and
the operator dashboard. Standard library only, so the pilot runs on any VA-approved Python.

Binds to localhost by default. The clinical assistant follows the launch pattern: ``POST
/launch`` stands in for VA single sign-on plus the EHR launch, checks access server-side and
sets an HttpOnly session; ``/ai/ask`` then takes the patient from that session only, never
from the request body. The raw FHIR REST API has no end-user authorization in the pilot; in
deployment it sits behind SSOi / Entra ID with SMART on FHIR scopes (docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .ai.access import AccessDenied
from .ai.assistant import ClinicalAssistant
from .ai.providers import Provider
from .ccda import ingest as ingest_ccda
from .config import BridgeConfig
from .pipeline import Pipeline
from .sensitivity import label_bundle
from .store import FhirStore, StoreError

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).resolve().parent.parent / "samples"
FHIR_JSON = "application/fhir+json"
RESOURCE_TYPES = ["Patient", "Practitioner", "Encounter", "Observation", "DiagnosticReport",
                  "DocumentReference", "AllergyIntolerance", "Condition", "MedicationStatement",
                  "Provenance", "AuditEvent"]
SEARCH_PARAMS = ["_id", "identifier", "patient", "subject", "code", "category", "family", "given",
                 "gender", "type", "_source", "_count"]

log = logging.getLogger("hl7bridge.http")


class App:
    def __init__(self, store: FhirStore, config: BridgeConfig | None = None, provider: Provider | None = None):
        self.store = store
        self.config = config or BridgeConfig()
        self.assistant = ClinicalAssistant(store, provider)
        self.pipeline = Pipeline(store, self.config, on_commit=self.assistant.reindex)
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def launch(self, user: str, icn: str) -> tuple[str, dict]:
        """SSO + EHR launch stand-in: resolve and authorize, then bind a server-side session."""
        pid = self.assistant.resolve_icn(icn)
        clinician, scope = self.assistant.authorize(user, pid, agent="launch")
        context = {"user": clinician.user_id, "clinician": clinician.display, "icn": icn,
                   "patient": pid, "identity_scope": scope,
                   "authorizations": sorted(clinician.authorizations)}
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._sessions[token] = context
        return token, context

    def session(self, token: str | None) -> dict | None:
        with self._lock:
            return self._sessions.get(token or "")

    def capability_statement(self) -> dict:
        return {
            "resourceType": "CapabilityStatement", "status": "active", "kind": "instance",
            "date": "2026-10-21", "fhirVersion": "4.0.1", "format": ["json"],
            "software": {"name": "hl7bridge (VA NAII interoperability pilot)", "version": "0.1.0"},
            "implementationGuide": ["http://hl7.org/fhir/us/core/ImplementationGuide/hl7.fhir.us.core"],
            "rest": [{
                "mode": "server",
                "interaction": [{"code": "transaction"}],
                "resource": [{
                    "type": t,
                    "interaction": [{"code": "read"}, {"code": "vread"}, {"code": "search-type"},
                                    {"code": "history-instance"}],
                    "searchParam": [{"name": p, "type": "token"} for p in SEARCH_PARAMS],
                    **({"operation": [{"name": "everything",
                                       "definition": "http://hl7.org/fhir/OperationDefinition/Patient-everything"}]}
                       if t == "Patient" else {}),
                } for t in RESOURCE_TYPES],
            }],
        }

    def patients(self) -> list[dict]:
        out = []
        for p in self.store.all_resources(["Patient"]):
            name = (p.get("name") or [{}])[0]
            icn = next((i["value"] for i in p.get("identifier", []) if i["system"] == self.config.icn_system), None)
            out.append({"id": p["id"], "name": f"{' '.join(name.get('given', []))} {name.get('family', '')}".strip(),
                        "birthDate": p.get("birthDate"), "gender": p.get("gender"), "icn": icn,
                        "active": p.get("active", True),
                        "replacedBy": next((l["other"].get("reference") for l in p.get("link", [])
                                            if l["type"] == "replaced-by"), None)})
        return sorted(out, key=lambda x: (not x["active"], x["name"]))


def searchset(resources: list[dict], base: str) -> dict:
    return {"resourceType": "Bundle", "type": "searchset", "total": len(resources),
            "entry": [{"fullUrl": f"{base}/{r['resourceType']}/{r['id']}", "resource": r,
                       "search": {"mode": "match"}} for r in resources]}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hl7bridge/0.1"

        def log_message(self, fmt, *args):  # route through logging; no query strings (may hold PHI)
            log.info("%s %s -> %s", self.command, urlsplit(self.path).path, args[1] if len(args) > 1 else "")

        def _cookie(self) -> str | None:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            return jar["pilot_launch"].value if "pilot_launch" in jar else None

        def _send(self, status: int, body, ctype: str = "application/json", cookie: str | None = None) -> None:
            data = body if isinstance(body, bytes) else (
                body.encode() if isinstance(body, str) else json.dumps(body, indent=1).encode())
            self.send_response(status)
            if cookie:
                self.send_header("Set-Cookie", f"pilot_launch={cookie}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline' "
                             "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                             "script-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)

        def _outcome(self, status: int, text: str, code: str = "processing") -> None:
            self._send(status, {"resourceType": "OperationOutcome",
                                "issue": [{"severity": "error", "code": code, "diagnostics": text}]}, FHIR_JSON)

        def _body(self) -> str:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length).decode("utf-8") if length else ""

        def _base(self) -> str:
            return f"http://{self.headers.get('Host', 'localhost')}/fhir"

        # -- GET -----------------------------------------------------------
        def do_GET(self):
            url = urlsplit(self.path)
            path, query = url.path.rstrip("/") or "/", dict(parse_qsl(url.query))
            try:
                if path == "/":
                    return self._send(200, (STATIC / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
                if path == "/fhir/metadata":
                    return self._send(200, app.capability_statement(), FHIR_JSON)
                if m := re.fullmatch(r"/fhir/Patient/([\w-]+)/\$everything", path):
                    return self._send(200, searchset(app.store.everything(m[1]), self._base()), FHIR_JSON)
                if m := re.fullmatch(r"/fhir/(\w+)/([\w-]+)/_history", path):
                    hist = app.store.history(m[1], m[2])
                    return self._send(200, {**searchset(hist, self._base()), "type": "history"}, FHIR_JSON)
                if m := re.fullmatch(r"/fhir/(\w+)/([\w-]+)", path):
                    res = app.store.read(m[1], m[2])
                    return self._send(200, res, FHIR_JSON) if res else self._outcome(
                        404, f"{m[1]}/{m[2]} not found", "not-found")
                if m := re.fullmatch(r"/fhir/(\w+)", path):
                    if m[1] not in RESOURCE_TYPES:
                        return self._outcome(404, f"resource type {m[1]} not supported", "not-supported")
                    return self._send(200, searchset(app.store.search(m[1], query), self._base()), FHIR_JSON)
                if path == "/api/stats":
                    return self._send(200, {"resources": app.store.counts(), "messages": app.store.message_stats(),
                                            "evidence_chunks": len(app.assistant.index),
                                            "indexed_patients": app.assistant.indexed_patients,
                                            "provider": app.assistant.provider.name,
                                            "supported_messages": app.pipeline.mapper.supported})
                if path == "/api/messages":
                    return self._send(200, app.store.list_messages(int(query.get("limit", 100))))
                if path == "/api/patients":  # EHR patient list stand-in for the launch picker
                    return self._send(200, app.patients())
                if path == "/api/users":  # SSO stand-in: the pilot's clinicians
                    return self._send(200, [{"user": c.user_id, "display": c.display,
                                             "authorizations": sorted(c.authorizations)}
                                            for c in app.assistant.policy.clinicians.values()])
                if path == "/api/samples":
                    return self._send(200, sorted(p.name for p in SAMPLES.glob("*.hl7"))
                                      + sorted(f"hie/{p.name}" for p in (SAMPLES / "hie").glob("*.xml")))
                if m := re.fullmatch(r"/api/samples/((?:hie/)?[\w.-]+\.(?:hl7|xml))", path):
                    f = SAMPLES / m[1]
                    return self._send(200, f.read_text(), "text/plain") if f.is_file() else self._outcome(404, "no sample")
                return self._outcome(404, f"no route for {path}", "not-found")
            except StoreError as exc:
                return self._outcome(exc.status, str(exc), exc.code)

        # -- POST ----------------------------------------------------------
        def do_POST(self):
            path = urlsplit(self.path).path.rstrip("/")
            body = self._body()
            try:
                if path in ("/hl7/v2", "/hl7/v2/$convert"):
                    result = app.pipeline.process(body, persist=not path.endswith("$convert"))
                    return self._send(200, result.to_json())
                if path == "/hie/ccda":
                    return self._send(200, ingest_ccda(app.store, body, app.config,
                                                       on_commit=app.assistant.reindex))
                if path == "/fhir":
                    bundle = json.loads(body)
                    label_bundle(bundle)
                    response = app.store.transaction(bundle)
                    app.assistant.reindex([e["response"]["location"].rsplit("/_history", 1)[0]
                                           for e in response["entry"]])
                    return self._send(200, response, FHIR_JSON)
                if path == "/launch":
                    req = json.loads(body or "{}")
                    token, context = app.launch(str(req.get("user", "")), str(req.get("icn", "")))
                    return self._send(200, context, cookie=token)
                if path == "/ai/ask":
                    context = app.session(self._cookie())
                    if context is None:
                        return self._outcome(401, "no launch context: POST /launch first", "login")
                    # Patient scope comes only from the server-side launch; any patient in the body is ignored.
                    req = json.loads(body or "{}")
                    return self._send(200, app.assistant.ask(context["patient"], str(req.get("question", "")),
                                                             user=context["user"], agent="dashboard"))
                return self._outcome(404, f"no route for {path}", "not-found")
            except AccessDenied as exc:
                return self._outcome(403, str(exc), "forbidden")
            except json.JSONDecodeError as exc:
                return self._outcome(400, f"invalid JSON: {exc}", "structure")
            except StoreError as exc:
                return self._outcome(exc.status, str(exc), exc.code)

    return Handler


def serve_http(app: App, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd

