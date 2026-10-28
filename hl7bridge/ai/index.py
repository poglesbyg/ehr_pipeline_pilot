"""Clinical evidence index: FHIR resources -> citable evidence chunks -> patient-scoped BM25.

Each chunk keeps its FHIR reference *and* the HL7 segment it came from, so any sentence a
model writes can be traced to ``VISTA LAB 999-LAB-0042 OBX[3]`` rather than "the record".
Retrieval is filtered to one patient identity, and to the 38 USC 7332 labels the clinician is
authorized for, *before* scoring. The assistant checks every hit again afterwards.

Other clinical-text rules applied here:
* each chunk is indexed with a short context header (kind, note type), so "denies chest pain"
  is never matched without knowing which note it came from;
* OCR confidence is kept per chunk, and low-confidence text is flagged instead of trusted;
* entered-in-error resources produce no chunks, so a retraction leaves the index on commit;
* copied-forward paragraphs collapse to the earliest copy, with the later copies counted.
"""

from __future__ import annotations

import base64
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..sensitivity import labels_of

LOW_OCR_CONFIDENCE = 0.80
_OCR_MARK = re.compile(r"^\[OCR(?: page (\d+))?,? confidence ([0-9.]+)\]\s*", re.I)

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "is", "was", "what",
         "does", "did", "has", "have", "patient", "patients", "any", "are", "be", "at", "by", "this",
         "their", "his", "her", "show", "me", "tell", "about", "from", "how", "taking", "being"}

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
    "medications": ["medication", "furosemide", "torsemide", "lisinopril", "metformin", "carvedilol"],
    "problems": ["problem"],
    "diagnoses": ["problem"],
    "conditions": ["problem"],
    "electrolytes": ["sodium", "potassium", "chloride", "carbon"],
    "k": ["potassium"],
    "echo": ["lvef", "echo", "regurgitation"],
    "followup": ["follow", "cardiology", "referral"],
    "plan": ["discharge", "follow", "referral"],
    "eligibility": ["service", "connected", "eligibility", "veteran"],
    "current": ["active"], "currently": ["active"],
    "latest": ["most", "recent"], "last": ["most", "recent"], "vaccine": ["immunization"],
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
    trust: str = "structured"  # structured | narrative | hie | ocr
    flags: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)  # 7332 labels, e.g. ["ETH"]
    ocr_confidence: float | None = None
    page: int | None = None
    copies: int = 1  # copied-forward duplicates collapsed into this chunk
    header: str = ""  # contextual header used for indexing, not shown as evidence

    def to_json(self) -> dict:
        return dict(self.__dict__)


def _concept_text(cc: dict | None) -> str:
    if not cc:
        return ""
    return cc.get("text") or next((c.get("display") or c.get("code") for c in cc.get("coding", [])), "")


def fmt_number(value) -> str:
    """Clinical precision: at most 2 decimals, no float noise (15.75137816157233 -> 15.75)."""
    if isinstance(value, float):
        return f"{round(value, 2):.2f}".rstrip("0").rstrip(".")
    return str(value)


def _quantity(q: dict) -> str:
    return f"{q.get('comparator', '')}{fmt_number(q['value'])} {q.get('unit', '')}".strip()


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
    if obs.get("component"):  # e.g. blood pressure: systolic + diastolic in one observation
        parts = [f"{_concept_text(c.get('code'))} {_value(c)}" for c in obs["component"]]
        return "; ".join(p for p in parts if not p.endswith("no value reported")) or "no value reported"
    return "no value reported"


def _status(cc: dict | None) -> str:
    return next((c.get("code", "") for c in (cc or {}).get("coding", [])), "")


def _label(source: str) -> str:
    # urn:hl7v2:999:999-LAB-0042#OBX[3] -> "999 · 999-LAB-0042 · OBX[3]"
    # urn:ccda:<oid>:<doc id>#problems/entry[2] -> "C-CDA · <doc id> · problems/entry[2]"
    if source.startswith("urn:ccda:"):
        head, _, where = source.partition("#")
        return " · ".join(x for x in ("C-CDA", head.rsplit(":", 1)[-1], where) if x)
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
    if patient is None or res.get("status") == "entered-in-error":
        return []
    source = res.get("meta", {}).get("source", "")
    base = dict(patient=patient, resource=ref, source=source, label=_label(source),
                protected=sorted(labels_of(res)))
    if source.startswith("urn:ccda:"):
        base["trust"] = "hie"

    if rtype == "Observation":
        category = next((c.get("code") for cat in res.get("category", []) for c in cat.get("coding", [])), "laboratory")
        kind = {"laboratory": "lab", "vital-signs": "vital"}.get(category, "observation")
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
        return [Chunk(f"{ref}", kind=kind, text=text + ".", date=date, **base)]

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
        if any(c.get("code") == "unconfirmed" for c in res.get("verificationStatus", {}).get("coding", [])):
            text += " (unconfirmed outside record; not yet reconciled)"
        return [Chunk(ref, kind="allergy", text=text + ".", **base)]

    if rtype == "Condition":
        onset = res.get("onsetDateTime", "")[:10]
        status = _status(res.get("clinicalStatus"))
        ended = res.get("abatementDateTime", "")[:10]
        text = f"Problem list: {_concept_text(res['code'])}"
        details = [d for d in (status, f"onset {onset}" if onset else "", f"resolved {ended}" if ended else "") if d]
        text += f" ({', '.join(details)})" if details else ""
        return [Chunk(ref, kind="problem", text=text + ".", date=onset, **base)]

    if rtype == "MedicationRequest":
        start = res.get("authoredOn", "")[:10]
        med = _concept_text(res.get("medicationCodeableConcept")) or "unspecified medication"
        dose = next((d.get("text") for d in res.get("dosageInstruction", []) if d.get("text")), "")
        text = f"Medication order ({res.get('status', 'unknown')}): {med}"
        text += f", {dose}" if dose else ""
        text += f", ordered {start}" if start else ""
        return [Chunk(ref, kind="medication", text=text + ".", date=start, **base)]

    if rtype == "Immunization":
        date = res.get("occurrenceDateTime", "")[:10]
        text = f"Immunization: {_concept_text(res.get('vaccineCode'))}"
        text += f" ({res['status']})" if res.get("status") not in (None, "completed") else ""
        text += f" on {date}" if date else ""
        return [Chunk(ref, kind="immunization", text=text + ".", date=date, **base)]

    if rtype == "Procedure":
        date = (res.get("performedDateTime") or res.get("performedPeriod", {}).get("start", ""))[:10]
        text = f"Procedure: {_concept_text(res.get('code'))}"
        text += f" on {date}" if date else ""
        return [Chunk(ref, kind="procedure", text=text + ".", date=date, **base)]

    if rtype == "MedicationStatement":
        start = res.get("effectivePeriod", {}).get("start", "")[:10]
        dose = (res.get("dosage") or [{}])[0].get("text")
        text = f"Medication ({res['status']}): {dose or _concept_text(res['medicationCodeableConcept'])}"
        text += f", since {start}" if start else ""
        source_org = res.get("informationSource", {}).get("display")
        text += f"; reported by {source_org}" if source_org else ""
        return [Chunk(ref, kind="medication", text=text + ".", date=start, **base)]

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
        chunks, confidence, page = [], None, None
        for i, line in enumerate(s for s in re.split(r"\n+", body) if s.strip()):
            line = line.strip()
            if (mark := _OCR_MARK.match(line)):  # confidence applies until the next page marker
                page = int(mark[1]) if mark[1] else (page or 1)
                confidence = float(mark[2])
                line = line[mark.end():]
            flags = []
            if confidence is not None and confidence < LOW_OCR_CONFIDENCE:
                flags.append(f"low OCR confidence {confidence:.2f}: verify against original page {page}")
            where = f"p{page} ¶{i + 1}" if page else f"¶{i + 1}"
            chunks.append(Chunk(f"{ref}#p{i + 1}", kind="note", text=line, date=date, trust=trust,
                                ocr_confidence=confidence, page=page, flags=flags, header=f"note {title}",
                                **{**base, "label": f"{title} · {base['label']} · {where}"}))
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


def _shingles(text: str, n: int = 4) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9.]+", text.lower())
    return {tuple(words[i:i + n]) for i in range(max(1, len(words) - n + 1))}


def near_duplicate(a: str, b: str, threshold: float = 0.8) -> bool:
    sa, sb = _shingles(a), _shingles(b)
    return bool(sa and sb) and len(sa & sb) / len(sa | sb) >= threshold


SERIES_MIN = 3
SERIES_SHOWN = 8


def _series_key(chunk: Chunk) -> str | None:
    """What makes two chunks the same measurement / product over time."""
    if chunk.kind in ("lab", "vital", "observation"):
        return f"{chunk.kind}:" + re.split(r" [<>=]?-?\d", chunk.text, maxsplit=1)[0].lower()
    if chunk.kind == "immunization":
        return "imm:" + chunk.text.split(" on ")[0].lower()
    if chunk.kind == "medication" and chunk.text.startswith("Medication order"):
        name = chunk.text.split("): ", 1)[-1].split(",")[0]
        return "med:" + re.sub(r"[^a-z]+", " ", name.lower()).strip()
    return None


def build_series(chunks: list[Chunk]) -> list[Chunk]:
    """Layered summary for long records: repeated results of the same lab, vaccine or medication
    (77 creatinines, 24 refills) collapse into one chunk, newest first with dates, citing the most
    recent resource. Without this, retrieval returns an arbitrary few of many near-identical
    chunks and misses the one that answers "most recent" or "current". One-off results are kept
    as they are."""
    groups: dict[str, list[Chunk]] = {}
    out = []
    for chunk in chunks:
        key = _series_key(chunk)
        if key is None:
            out.append(chunk)
        else:
            groups.setdefault(key, []).append(chunk)
    for members in groups.values():
        if len(members) < SERIES_MIN:
            out += members
            continue
        members.sort(key=lambda c: c.date or "", reverse=True)
        latest = members[0]
        if latest.kind == "medication":
            label = latest.text.split("): ", 1)[-1].split(",")[0]
            statuses = re.findall(r"Medication order \(([^)]+)\)", " ".join(c.text for c in members))
            text = (f"Medication order history for {label}: {len(members)} orders; most recent "
                    f"({latest.date}) is {statuses[0]}" + (f"; earlier orders {', '.join(sorted(set(statuses[1:])))}"
                                                            if len(set(statuses)) > 1 else "") + ".")
        else:
            name = re.split(r" [<>=]?-?\d", latest.text, maxsplit=1)[0] if latest.kind != "immunization" \
                else latest.text.split(" on ")[0]
            shown = [re.sub(r"^.*?(?= [<>=]?-?\d)", "", c.text, count=1).strip().rstrip(".") if c.kind != "immunization"
                     else (c.date or c.text) for c in members[:SERIES_SHOWN]]
            text = (f"{name}: {len(members)} results, most recent first: " + "; ".join(shown)
                    + (f"; {len(members) - SERIES_SHOWN} earlier not shown" if len(members) > SERIES_SHOWN else "") + ".")
        out.append(Chunk(**{**latest.__dict__, "id": f"{latest.id}#series", "text": text,
                            "copies": 1, "protected": sorted({p for c in members for p in c.protected}),
                            "header": f"{latest.header} series history"}))
    return out


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
            chunk.header = chunk.header or chunk.kind
            self.chunks[chunk.id] = chunk
            self._tokens[chunk.id] = Counter(tokenize(f"{chunk.header} {chunk.text}"))
            self._by_patient[chunk.patient].add(chunk.id)
            self._by_resource[ref].add(chunk.id)

    def add_patient_chunks(self, patient: str, chunks: list[Chunk]) -> None:
        """Index one patient's whole record at once (on-demand path), so series can be built."""
        for chunk in build_series(chunks):
            chunk.header = chunk.header or chunk.kind
            self.chunks[chunk.id] = chunk
            self._tokens[chunk.id] = Counter(tokenize(f"{chunk.header} {chunk.text}"))
            self._by_patient[chunk.patient].add(chunk.id)
            self._by_resource[chunk.resource.split("#")[0]].add(chunk.id)

    def drop_patient(self, patient: str) -> None:
        for cid in self._by_patient.pop(patient, set()):
            chunk = self.chunks.pop(cid, None)
            self._tokens.pop(cid, None)
            if chunk:
                self._by_resource.get(chunk.resource.split("#")[0], set()).discard(cid)

    def patients(self) -> set[str]:
        return {p for p, ids in self._by_patient.items() if ids}

    def _ids(self, patients: str | list[str], allow: frozenset[str] | None = None) -> set[str]:
        patients = [patients] if isinstance(patients, str) else patients
        ids = set().union(*(self._by_patient.get(p, set()) for p in patients))
        if allow is not None:  # 7332: protected evidence is filtered out before scoring
            ids = {c for c in ids if set(self.chunks[c].protected) <= allow}
        return ids

    def patient_chunks(self, patients: str | list[str]) -> list[Chunk]:
        return sorted((self.chunks[c] for c in self._ids(patients)), key=lambda c: (c.date, c.id))

    def search(self, patients: str | list[str], query: str, k: int = 8,
               allow: frozenset[str] | None = frozenset()) -> list[tuple[Chunk, float]]:
        """``patients`` is one reference, or a merged identity's set of references.
        ``allow`` is the set of 7332 labels the clinician may read; ``None`` disables the
        filter (index maintenance only, never a clinician request)."""
        ids = self._ids(patients, allow)
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
        return self._collapse_copies(scored)[:k]

    @staticmethod
    def _collapse_copies(scored: list[tuple[Chunk, float]]) -> list[tuple[Chunk, float]]:
        """Copied-forward text: keep the earliest copy (with its date), count the rest."""
        kept: list[tuple[Chunk, float]] = []
        for chunk, score in scored:
            twin = next((i for i, (c, _) in enumerate(kept)
                         if c.kind == chunk.kind == "note" and near_duplicate(c.text, chunk.text)), None)
            if twin is None:
                kept.append((chunk, score))
                continue
            first, best = kept[twin]
            earliest = min((first, chunk), key=lambda c: c.date or "9999")
            copies = first.copies + 1
            flags = [f for f in earliest.flags if not f.startswith("copied forward")]
            flags.append(f"copied forward into {copies - 1} later note(s); earliest copy shown")
            kept[twin] = (Chunk(**{**earliest.__dict__, "copies": copies, "flags": flags}), max(best, score))
        return kept
