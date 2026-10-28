"""Clinical search & summarization: authorize -> retrieve -> check -> screen -> generate ->
verify -> audit.

The orchestrator is the single place patient scope and citations are enforced:

1. Access: the clinician must be allowed to see this patient (fails closed, and is audited).
2. Scope in the index: the patient identity and the 7332 labels the clinician may read are
   applied as filters before scoring. A prompt instruction is not a security control.
3. Scope after retrieval: every hit is checked again against the same identity and labels. Two
   independent checks, so one bug cannot leak a record.
4. Grounding: numbered, dated evidence goes to the model; every sentence is verified in code.
5. Audit: who asked, about whom, what was disclosed, which model and version answered.

Indexing is on demand: a patient's record is loaded into the index the first time it is
needed, cached, expired after ``cache_ttl``, and invalidated whenever new data commits. That
also means the working set for a request holds only that patient's chunks.
"""

from __future__ import annotations

import base64
import re
import time
import uuid
from datetime import datetime, timezone

from ..config import BridgeConfig
from ..store import FhirStore
from .access import AccessDenied, AccessPolicy, Clinician, ScopeViolation, valid_icn
from .guardrails import check_output, screen_evidence
from .index import Chunk, EvidenceIndex, _patient_of
from .providers import ExtractiveProvider, Provider

DISCLAIMER = "AI-generated from the cited record entries for clinician review. Verify against source before acting."

# Default "summarize this patient" retrieval: one query per clinical facet.
SUMMARY_FACETS = [
    ("admission inpatient encounter reason", 2),
    ("abnormal high low critical lab results", 5),
    ("hospital course discharge plan follow-up", 3),
    ("allergy reaction", 3),
    ("problem list medication", 4),
    ("outside community consult echo", 2),
]

OUTSIDE_TRUST = {"hie": "outside record", "ocr": "outside record, OCR"}


def attribute_outside(check, evidence: list[Chunk]) -> str:
    """Tag sentences supported only by non-VA evidence, and any sentence resting on low-confidence
    OCR. Done in code, not left to the model: in evaluation no model reliably did it on its own."""
    out = []
    for s in check.sentences:
        if not s.supported:
            continue
        cited = [evidence[n - 1] for n in s.citations]
        trusts = {c.trust for c in cited}
        tags = []
        if trusts and trusts <= OUTSIDE_TRUST.keys() and "outside record" not in s.text:
            tags.append(OUTSIDE_TRUST["ocr" if "ocr" in trusts else "hie"])
        low = [c for c in cited if any(f.startswith("low OCR confidence") for f in c.flags)]
        if low:
            tags.append(f"low OCR confidence {low[0].ocr_confidence:.2f}, verify against original page {low[0].page}")
        text = s.text
        if tags:
            text = re.sub(r"(\s*(?:\[\d+(?:\s*,\s*\d+)*\]\s*)+)([.!?]?)$", rf" ({'; '.join(tags)})\1\2",
                          text, count=1)
        out.append(text)
    return " ".join(out)


class ClinicalAssistant:
    def __init__(self, store: FhirStore, provider: Provider | None = None, *, k: int = 6,
                 policy: AccessPolicy | None = None, icn_system: str | None = None, cache_ttl: float = 3600):
        self.store = store
        self.provider = provider or ExtractiveProvider(max_sentences=12)
        self.policy = policy or AccessPolicy()
        self.icn_system = icn_system or BridgeConfig().icn_system
        self.index = EvidenceIndex()
        self.k = k
        self.cache_ttl = cache_ttl
        self._loaded: dict[str, float] = {}  # Patient/<id> -> monotonic load time

    # -- on-demand indexing -----------------------------------------------------
    def reindex(self, refs: list[str] | None = None) -> None:
        """Invalidate the patients touched by newly committed resources (all, if ``refs`` is None).
        They are re-indexed lazily on their next request."""
        if refs is None:
            for patient in list(self._loaded) + list(self.index.patients()):
                self._invalidate(patient)
            return
        for ref in refs:
            rtype, _, rid = ref.partition("/")
            if (res := self.store.read(rtype, rid)) and (patient := _patient_of(res)):
                self._invalidate(patient)

    def _invalidate(self, patient: str) -> None:
        self._loaded.pop(patient, None)
        self.index.drop_patient(patient)

    def _ensure_indexed(self, scope: list[str]) -> None:
        now = time.monotonic()
        for patient in scope:
            loaded = self._loaded.get(patient)
            if loaded is not None and now - loaded < self.cache_ttl:
                continue
            self._invalidate(patient)
            for res in self.store.everything(patient.split("/", 1)[1]):
                if _patient_of(res) == patient:
                    self.index.upsert_resource(res)
            self._loaded[patient] = now

    @property
    def indexed_patients(self) -> int:
        return len(self._loaded)

    # -- identity and access ----------------------------------------------------------
    def identity(self, patient_id: str) -> list[str]:
        """The patient plus any records merged into it (Patient.link replaced-by)."""
        refs = [f"Patient/{patient_id}"]
        for other in self.store.all_resources(["Patient"]):
            for link in other.get("link", []):
                if link.get("type") == "replaced-by" and link["other"].get("reference") == refs[0]:
                    refs.append(f"Patient/{other['id']}")
        return refs

    def icns(self, scope: list[str]) -> set[str]:
        out = set()
        for ref in scope:
            res = self.store.read("Patient", ref.split("/", 1)[1]) or {}
            out |= {i["value"] for i in res.get("identifier", []) if i.get("system") == self.icn_system}
        return out

    def resolve_icn(self, icn: str) -> str:
        """Launch context -> the active Patient id. The ICN is validated before it is used."""
        if not valid_icn(icn):
            raise AccessDenied("invalid patient identifier")
        matches = self.store.search("Patient", {"identifier": f"{self.icn_system}|{icn}"})
        if not matches:
            raise AccessDenied("patient not found")
        patient = matches[0]
        for link in patient.get("link", []):
            if link.get("type") == "replaced-by" and (ref := link["other"].get("reference", "")).startswith("Patient/"):
                return ref.split("/", 1)[1]
        return patient["id"]

    def authorize(self, user: str, patient_id: str, *, agent: str | None = None,
                  question: str = "") -> tuple[Clinician, list[str]]:
        scope = self.identity(patient_id)
        try:
            return self.policy.authorize(user, self.icns(scope)), scope
        except AccessDenied as exc:
            self._audit(f"Patient/{patient_id}", question, user, agent, None, [], outcome="8", detail=str(exc))
            raise

    def _retrieve(self, scope: list[str], clinician: Clinician, query: str, k: int) -> list[tuple[Chunk, float]]:
        self._ensure_indexed(scope)
        hits = self.index.search(scope, query, k=k, allow=clinician.authorizations)
        self._verify_scope(hits, scope, clinician)
        return hits

    @staticmethod
    def _verify_scope(hits: list[tuple[Chunk, float]], scope: list[str], clinician: Clinician) -> None:
        """Second, independent check: every hit belongs to this identity and is readable."""
        allowed = set(scope)
        for chunk, _ in hits:
            if chunk.patient not in allowed or not clinician.may_read(chunk.protected):
                raise ScopeViolation(f"retrieved evidence {chunk.id} is outside the authorized scope")

    # -- tools -------------------------------------------------------------------------
    def search(self, patient_id: str, query: str, *, user: str, k: int = 8, agent: str | None = None) -> dict:
        """Retrieval only: screened, patient-scoped evidence with lineage. Audited like ``ask``."""
        t0 = time.perf_counter()
        clinician, scope = self.authorize(user, patient_id, agent=agent, question=query)
        try:
            hits = self._retrieve(scope, clinician, query, k)
        except ScopeViolation as exc:
            self._audit(f"Patient/{patient_id}", query, user, agent, None, [], outcome="8", detail=str(exc))
            raise
        hits.sort(key=lambda h: h[0].trust == "ocr")
        screen = screen_evidence([c for c, _ in hits])
        scores = {c.id: s for c, s in hits}
        result = {
            "patient": f"Patient/{patient_id}",
            "identity_scope": scope,
            "question": query,
            "results": [{"n": i, **c.to_json(), "score": scores[c.id]} for i, c in enumerate(screen.allowed, 1)],
            "quarantined": screen.quarantined,
            "provider": "none (retrieval only)",
            "timing_ms": {"total": round((time.perf_counter() - t0) * 1000, 2)},
        }
        self._audit(result["patient"], query, user, agent, None, result["results"])
        return result

    def ask(self, patient_id: str, question: str = "", *, user: str, agent: str | None = None,
            strict: bool = True) -> dict:
        t0 = time.perf_counter()
        clinician, scope = self.authorize(user, patient_id, agent=agent, question=question)
        summary = not question.strip() or question.strip().lower() in ("summarize", "summary")
        try:
            if summary:
                seen, hits = set(), []
                for facet, k in SUMMARY_FACETS:
                    for chunk, score in self._retrieve(scope, clinician, facet, k):
                        if chunk.id not in seen:
                            seen.add(chunk.id)
                            hits.append((chunk, score))
            else:
                hits = self._retrieve(scope, clinician, question, self.k)
        except ScopeViolation as exc:
            self._audit(f"Patient/{patient_id}", question, user, agent, None, [], outcome="8", detail=str(exc))
            raise
        # Authenticated VA sources first; OCR'd outside records are presented last.
        hits.sort(key=lambda h: h[0].trust == "ocr")
        screen = screen_evidence([c for c, _ in hits])
        evidence = screen.allowed
        t1 = time.perf_counter()
        model_version = None
        try:
            draft = self.provider.answer(question or "Summarize this patient's current clinical picture.",
                                         evidence)
            provider = self.provider.name
            model_version = getattr(self.provider, "last_model", None)
        except Exception as exc:  # hosted model unavailable -> degrade to the grounded baseline
            draft = ExtractiveProvider().answer(question, evidence)
            provider = f"extractive (fallback: {type(exc).__name__})"
        t2 = time.perf_counter()
        check = check_output(draft, evidence, strict=strict)
        answer = attribute_outside(check, evidence) if strict else check.answer
        used = sorted({n for s in check.sentences if s.supported for n in s.citations})
        scores = {c.id: s for c, s in hits}
        flags = list(check.flags)
        flags += sorted({f for n in used for f in evidence[n - 1].flags})
        result = {
            "id": str(uuid.uuid4()),
            "patient": f"Patient/{patient_id}",
            "identity_scope": scope,
            "clinician": clinician.user_id,
            "question": question or "(summary)",
            "answer": answer or "Not found in the record.",
            "disclaimer": DISCLAIMER,
            "citations": [{"n": n, **evidence[n - 1].to_json(), "score": scores.get(evidence[n - 1].id)}
                          for n in used],
            "evidence_considered": len(evidence),
            "guardrails": {**check.to_json(), "flags": flags, "quarantined": screen.quarantined},
            "provider": provider,
            "model_version": model_version,
            "timing_ms": {"retrieval": round((t1 - t0) * 1000, 2), "generation": round((t2 - t1) * 1000, 2),
                          "total": round((time.perf_counter() - t0) * 1000, 2)},
        }
        self._audit(result["patient"], result["question"], user, agent,
                    f"{provider}" + (f" ({model_version})" if model_version else ""), result["citations"])
        return result

    # -- audit --------------------------------------------------------------------------
    def _audit(self, patient: str, question: str, user: str, agent: str | None, model: str | None,
               citations: list[dict], *, outcome: str = "0", detail: str | None = None) -> None:
        """Who asked what about whom, what was disclosed, which model answered (NIST AU-2/AU-3).
        The audit store holds PHI by design and is protected as a PHI system."""
        agents = [{"who": {"display": user}, "requestor": True}]
        if agent:
            agents.append({"who": {"display": agent}, "requestor": False})
        if model:
            agents.append({"who": {"display": f"model:{model}"}, "requestor": False})
        event = {
            "resourceType": "AuditEvent",
            "type": {"system": "http://dicom.nema.org/resources/ontology/DCM", "code": "110112", "display": "Query"},
            "subtype": [{"system": "http://hl7.org/fhir/restful-interaction", "code": "search-type"}],
            "action": "E",
            "recorded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "outcome": outcome,
            "agent": agents,
            "source": {"observer": {"display": "hl7bridge clinical assistant"}},
            "entity": [{"what": {"reference": patient}, "role": {"code": "1", "display": "Patient"},
                        "query": base64.b64encode((question or "").encode()).decode()},
                       *[{"what": {"reference": c["resource"]}, "role": {"code": "4", "display": "Domain Resource"}}
                         for c in citations]],
        }
        if detail:
            event["outcomeDesc"] = detail
        self.store.create(event)
