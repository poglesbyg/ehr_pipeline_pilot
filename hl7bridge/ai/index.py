"""Clinical evidence index: FHIR resources -> citable evidence chunks -> patient-scoped BM25.

Each chunk keeps its FHIR reference *and* the HL7 segment it came from, so any sentence a
model writes can be traced to ``VISTA LAB 999-LAB-0042 OBX[3]`` rather than "the record".
Retrieval is always filtered to one patient before scoring - cross-patient leakage is
structurally impossible, not merely unlikely.
"""

from __future__ import annotations

import base64
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "is", "was", "what",
         "does", "did", "has", "have", "patient", "patients", "any", "are", "be", "at", "by", "this",
         "their", "his", "her", "show", "me", "tell", "about", "from", "how"}

# Lay / clinical phrasing -> terms that actually appear in VistA-derived records.
SYNONYMS = {
    "kidney": ["creatinine", "egfr", "urea", "renal"],
    "renal": ["creatinine", "egfr", "urea"],
    "heart": ["bnp", "cardiac", "troponin", "lvef", "cardiology"],
    "failure": ["bnp", "diuresed", "furosemide", "torsemide", "lvef"],
    "diabetes": ["glucose", "a1c", "metformin", "endocrinology"],
    "sugar": ["glucose", "a1c"],
    "allergies": ["allergy"],
    "allergic": ["allergy"],
    "meds": ["medication", "furosemide", "torsemide", "lisinopril", "metformin", "carvedilol"],
    "medications": ["furosemide", "torsemide", "lisinopril", "metformin", "carvedilol"],
    "electrolytes": ["sodium", "potassium", "chloride", "carbon"],
    "k": ["potassium"],
    "echo": ["lvef", "echo", "regurgitation"],
    "followup": ["follow", "cardiology", "referral"],
    "plan": ["discharge", "follow", "referral"],
    "eligibility": ["service", "connected", "eligibility", "veteran"],
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass
class Chunk:
    id: str
    patient: str
    kind: str  # lab | note | allergy | encounter | demographics
    text: str
    resource: str
    date: str = ""
    source: str = ""  # urn:hl7v2:<facility>:<control id>#<segment>
    label: str = ""
    trust: str = "structured"  # structured | narrative | ocr
    flags: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return dict(self.__dict__)


def _concept_text(cc: dict | None) -> str:
    if not cc:
        return ""
    return cc.get("text") or next((c.get("display") or c.get("code") for c in cc.get("coding", [])), "")


def _quantity(q: dict) -> str:
    return f"{q.get('comparator', '')}{q['value']} {q.get('unit', '')}".strip()


def _value(obs: dict) -> str:
    if "valueQuantity" in obs:
        return _quantity(obs["valueQuantity"])
    if "valueString" in obs:
        return obs["valueString"]
    if "valueCodeableConcept" in obs:
        return _concept_text(obs["valueCodeableConcept"])
    if "valueRange" in obs:
        return f"{_quantity(obs['valueRange']['low'])} to {_quantity(obs['valueRange']['high'])}"
    if "valueRatio" in obs:
        return f"{obs['valueRatio']['numerator']['value']}:{obs['valueRatio']['denominator']['value']}"
    return "no value reported"


def _label(source: str) -> str:
    # urn:hl7v2:999:999-LAB-0042#OBX[3] -> "999 · 999-LAB-0042 · OBX[3]"
    if not source.startswith("urn:hl7v2:"):
        return source
    head, _, seg = source[len("urn:hl7v2:"):].partition("#")
    facility, _, ctrl = head.partition(":")
    return " · ".join(x for x in (facility, ctrl, seg) if x)


def _patient_of(res: dict) -> str | None:
    if res["resourceType"] == "Patient":
        return f"Patient/{res['id']}"
    ref = (res.get("subject") or res.get("patient") or {}).get("reference", "")
    return ref if ref.startswith("Patient/") else None


def resource_to_chunks(res: dict) -> list[Chunk]:
    rtype, rid = res["resourceType"], res["id"]
    ref = f"{rtype}/{rid}"
    patient = _patient_of(res)
    if patient is None:
        return []
    source = res.get("meta", {}).get("source", "")
    base = dict(patient=patient, resource=ref, source=source, label=_label(source))

    if rtype == "Observation":
        flag = next((c.get("display") for i in res.get("interpretation", []) for c in i.get("coding", [])), "")
        rng = (res.get("referenceRange") or [{}])[0].get("text", "")
        date = res.get("effectiveDateTime", "")[:10]
        text = f"{_concept_text(res['code'])} {_value(res)}"
        if flag and flag != "Normal":
            text += f" ({flag.lower()}"
            text += f", reference {rng})" if rng else ")"
        elif rng:
            text += f" (reference {rng})"
        text += f" on {date}" if date else ""
        if res.get("status") not in ("final", None):
            text += f" [{res['status']}]"
        if res.get("note"):
            text += "; lab comment: " + " ".join(n["text"] for n in res["note"])
        return [Chunk(f"{ref}", kind="lab", text=text + ".", date=date, **base)]

    if rtype == "DiagnosticReport":
        if not res.get("conclusion"):
            return []
        date = res.get("effectiveDateTime", "")[:10]
        text = f"{_concept_text(res['code'])} report comment ({date}): {res['conclusion']}"
        return [Chunk(ref, kind="lab", text=text, date=date, **base)]

    if rtype == "AllergyIntolerance":
        reaction = res.get("reaction", [{}])[0]
        manifest = ", ".join(m.get("text", "") for m in reaction.get("manifestation", []))
        cat = "/".join(res.get("category", []))
        text = f"Allergy to {_concept_text(res['code']).lower()}"
        text += f" ({cat})" if cat else ""
        text += f": reaction {manifest.lower()}" if manifest and manifest != "unspecified" else ""
        text += f", severity {reaction['severity']}" if reaction.get("severity") else ""
        return [Chunk(ref, kind="allergy", text=text + ".", **base)]

    if rtype == "Encounter":
        period = res.get("period", {})
        date = period.get("start", "")[:10]
        kind = res["class"].get("display", res["class"].get("code", ""))
        text = kind[:1].upper() + kind[1:] + ("" if kind.endswith("encounter") else " encounter")
        text += f" ({res['status']})"
        if res.get("location"):
            text += f" at {res['location'][0]['location'].get('display')}"
        text += f", started {date}" if date else ""
        if period.get("end"):
            text += f", ended {period['end'][:10]}"
        if res.get("reasonCode"):
            text += f"; reason: {_concept_text(res['reasonCode'][0])}"
        return [Chunk(ref, kind="encounter", text=text + ".", date=date, **base)]

    if rtype == "DocumentReference":
        att = res["content"][0]["attachment"]
        body = base64.b64decode(att.get("data", "")).decode("utf-8", errors="replace")
        title = att.get("title") or _concept_text(res.get("type"))
        date = (res.get("date") or res.get("context", {}).get("period", {}).get("start", ""))[:10]
        trust = "ocr" if "OCR" in title.upper() or "[OCR" in body else "narrative"
        chunks = []
        for i, line in enumerate(s for s in re.split(r"\n+", body) if s.strip()):
            chunks.append(Chunk(f"{ref}#p{i + 1}", kind="note", text=line.strip(), date=date,
                                trust=trust, **{**base, "label": f"{title} · {base['label']} · ¶{i + 1}"}))
        return chunks

    if rtype == "Patient":
        facts = []
        for ext in res.get("extension", []):
            url = ext["url"].rsplit("/", 1)[-1]
            if url == "veteran-status":
                facts.append("Veteran" if ext["valueBoolean"] else "Non-veteran")
            elif url == "service-connected-percent":
                facts.append(f"{ext['valueInteger']}% service connected")
            elif url == "period-of-service":
                facts.append(f"period of service {_concept_text(ext['valueCodeableConcept']).title()}")
        if not facts:
            return []
        return [Chunk(ref, kind="demographics", text="Eligibility: " + ", ".join(facts) + ".", **base)]
    return []


class EvidenceIndex:
    """BM25 over evidence chunks, scoped per patient. Swappable for Azure AI Search."""

    def __init__(self, k1: float = 1.4, b: float = 0.7):
        self.k1, self.b = k1, b
        self.chunks: dict[str, Chunk] = {}
        self._tokens: dict[str, Counter] = {}
        self._by_patient: dict[str, set[str]] = defaultdict(set)
        self._by_resource: dict[str, set[str]] = defaultdict(set)

    def __len__(self) -> int:
        return len(self.chunks)

    def upsert_resource(self, res: dict) -> None:
        ref = f"{res['resourceType']}/{res['id']}"
        for cid in self._by_resource.pop(ref, set()):
            chunk = self.chunks.pop(cid)
            self._tokens.pop(cid, None)
            self._by_patient[chunk.patient].discard(cid)
        for chunk in resource_to_chunks(res):
            self.chunks[chunk.id] = chunk
            self._tokens[chunk.id] = Counter(tokenize(chunk.text))
            self._by_patient[chunk.patient].add(chunk.id)
            self._by_resource[ref].add(chunk.id)

    def _ids(self, patients: str | list[str]) -> set[str]:
        patients = [patients] if isinstance(patients, str) else patients
        return set().union(*(self._by_patient.get(p, set()) for p in patients))

    def patient_chunks(self, patients: str | list[str]) -> list[Chunk]:
        return sorted((self.chunks[c] for c in self._ids(patients)), key=lambda c: (c.date, c.id))

    def search(self, patients: str | list[str], query: str, k: int = 8) -> list[tuple[Chunk, float]]:
        """``patients`` is one reference, or a merged identity's set of references."""
        ids = self._ids(patients)
        if not ids:
            return []
        terms = tokenize(query)
        expanded = Counter(terms)
        for t in terms:
            for syn in SYNONYMS.get(t, []):
                expanded[syn] += 0.6
        n = len(ids)
        avg = sum(sum(self._tokens[c].values()) for c in ids) / n
        df = Counter(t for c in ids for t in self._tokens[c])
        scored = []
        for cid in ids:
            tf, length = self._tokens[cid], sum(self._tokens[cid].values()) or 1
            score = 0.0
            for term, weight in expanded.items():
                if term not in tf:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += weight * idf * tf[term] * (self.k1 + 1) / (
                    tf[term] + self.k1 * (1 - self.b + self.b * length / avg))
            if score > 0:
                chunk = self.chunks[cid]
                if chunk.trust == "ocr":
                    score *= 0.75  # prefer structured/authenticated sources on ties
                scored.append((chunk, round(score, 4)))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]
