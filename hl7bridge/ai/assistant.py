"""Clinical search & summarization: retrieve -> screen -> generate -> verify -> audit."""

from __future__ import annotations

import base64
import re
import time
import uuid
from datetime import datetime, timezone

from ..store import FhirStore
from .guardrails import check_output, screen_evidence
from .index import EvidenceIndex
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


def attribute_outside(check, evidence) -> str:
    """Tag sentences supported only by non-VA evidence. Done in code, not left to the model:
    in evaluation no model reliably attributed outside sources on its own."""
    out = []
    for s in check.sentences:
        if not s.supported:
            continue
        trusts = {evidence[n - 1].trust for n in s.citations}
        text = s.text
        if trusts and trusts <= OUTSIDE_TRUST.keys():
            tag = OUTSIDE_TRUST["ocr" if "ocr" in trusts else "hie"]
            text = re.sub(r"(\s*(?:\[\d+(?:\s*,\s*\d+)*\]\s*)+)([.!?]?)$", rf" ({tag})\1\2", text, count=1)
        out.append(text)
    return " ".join(out)


class ClinicalAssistant:
    def __init__(self, store: FhirStore, provider: Provider | None = None, *, k: int = 6):
        self.store = store
        self.provider = provider or ExtractiveProvider(max_sentences=12)
        self.index = EvidenceIndex()
        self.k = k
        self.reindex()

    def reindex(self, refs: list[str] | None = None) -> None:
        if refs is None:
            for res in self.store.all_resources():
                self.index.upsert_resource(res)
            return
        for ref in refs:
            rtype, _, rid = ref.partition("/")
            if (res := self.store.read(rtype, rid)):
                self.index.upsert_resource(res)

    def identity(self, patient_id: str) -> list[str]:
        """The patient plus any records merged into it (Patient.link replaced-by)."""
        refs = [f"Patient/{patient_id}"]
        for other in self.store.all_resources(["Patient"]):
            for link in other.get("link", []):
                if link.get("type") == "replaced-by" and link["other"].get("reference") == refs[0]:
                    refs.append(f"Patient/{other['id']}")
        return refs

    def search(self, patient_id: str, query: str, *, k: int = 8, user: str = "pilot-clinician") -> dict:
        """Retrieval only: screened, patient-scoped evidence with lineage. Audited like ``ask``."""
        t0 = time.perf_counter()
        scope = self.identity(patient_id)
        hits = self.index.search(scope, query, k=k)
        hits.sort(key=lambda h: h[0].trust == "ocr")
        screen = screen_evidence([c for c, _ in hits])
        scores = dict((c.id, s) for c, s in hits)
        result = {
            "patient": f"Patient/{patient_id}",
            "identity_scope": scope,
            "question": query,
            "results": [{"n": i, **c.to_json(), "score": scores[c.id]} for i, c in enumerate(screen.allowed, 1)],
            "quarantined": screen.quarantined,
            "provider": "none (retrieval only)",
            "timing_ms": {"total": round((time.perf_counter() - t0) * 1000, 2)},
        }
        self._audit({**result, "citations": result["results"]}, user)
        return result

    def ask(self, patient_id: str, question: str = "", *, user: str = "pilot-clinician",
            strict: bool = True) -> dict:
        t0 = time.perf_counter()
        scope = self.identity(patient_id)
        summary = not question.strip() or question.strip().lower() in ("summarize", "summary")
        if summary:
            seen, hits = set(), []
            for facet, k in SUMMARY_FACETS:
                for chunk, score in self.index.search(scope, facet, k=k):
                    if chunk.id not in seen:
                        seen.add(chunk.id)
                        hits.append((chunk, score))
        else:
            hits = self.index.search(scope, question, k=self.k)
        # Authenticated VA sources first; OCR'd outside records are presented last.
        hits.sort(key=lambda h: h[0].trust == "ocr")
        screen = screen_evidence([c for c, _ in hits])
        evidence = screen.allowed
        t1 = time.perf_counter()
        try:
            draft = self.provider.answer(question or "Summarize this patient's current clinical picture.",
                                         evidence)
            provider = self.provider.name
        except Exception as exc:  # hosted model unavailable -> degrade to the grounded baseline
            draft = ExtractiveProvider().answer(question, evidence)
            provider = f"extractive (fallback: {type(exc).__name__})"
        t2 = time.perf_counter()
        check = check_output(draft, evidence, strict=strict)
        answer = attribute_outside(check, evidence) if strict else check.answer
        used = sorted({n for s in check.sentences if s.supported for n in s.citations})
        scores = dict((c.id, s) for c, s in hits)
        result = {
            "id": str(uuid.uuid4()),
            "patient": f"Patient/{patient_id}",
            "identity_scope": scope,
            "question": question or "(summary)",
            "answer": answer or "Not found in the record.",
            "disclaimer": DISCLAIMER,
            "citations": [{"n": n, **evidence[n - 1].to_json(), "score": scores.get(evidence[n - 1].id)}
                          for n in used],
            "evidence_considered": len(evidence),
            "guardrails": {**check.to_json(), "quarantined": screen.quarantined},
            "provider": provider,
            "timing_ms": {"retrieval": round((t1 - t0) * 1000, 2), "generation": round((t2 - t1) * 1000, 2),
                          "total": round((time.perf_counter() - t0) * 1000, 2)},
        }
        self._audit(result, user)
        return result

    def _audit(self, result: dict, user: str) -> None:
        """Record who asked what about whom and what was disclosed (NIST AU-2/AU-3)."""
        self.store.create({
            "resourceType": "AuditEvent",
            "type": {"system": "http://dicom.nema.org/resources/ontology/DCM", "code": "110112", "display": "Query"},
            "subtype": [{"system": "http://hl7.org/fhir/restful-interaction", "code": "search-type"}],
            "action": "E",
            "recorded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "outcome": "0",
            "agent": [{"who": {"display": user}, "requestor": True},
                      {"who": {"display": f"model:{result['provider']}"}, "requestor": False}],
            "source": {"observer": {"display": "hl7bridge clinical assistant"}},
            "entity": [{"what": {"reference": result["patient"]}, "role": {"code": "1", "display": "Patient"},
                        "query": base64.b64encode(result["question"].encode()).decode()},
                       *[{"what": {"reference": c["resource"]}, "role": {"code": "4", "display": "Domain Resource"}}
                         for c in result["citations"]]],
        })
