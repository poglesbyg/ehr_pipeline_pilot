"""Trustworthy-AI guardrails around the model call.

Input side: evidence from outside VA (faxes, HIE documents) is untrusted text. Chunks that
read like instructions to an AI are quarantined before they reach the prompt.

Output side: every sentence must cite evidence, every number and every medication named in a
sentence must appear in the evidence it cites, and direct identifiers are scrubbed.
Unsupported sentences are removed (strict mode) or marked, and the result carries a
groundedness score and a fabricated-medication count. These checks are deterministic code, so
they hold no matter which model, or which model version, wrote the answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .index import Chunk

_INJECTION = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|disregard (the )?(previous|above)|"
    r"\b(note|message) to (ai|llm|the model|assistant)|you are (now )?(an?|the) (ai|assistant|model)|"
    r"system prompt|do not (cite|mention)|report that the patient",
    re.I,
)
_SSN = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")
_CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")  # [1], [1, 2]
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[])")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def split_statements(answer: str) -> list[str]:
    """Statements to verify: sentences, with each line and list item its own statement. Models
    often answer in bullets; treating a list as one sentence would let one citation vouch for
    every item. Header lines ending in ':' are structure, not claims, and are dropped."""
    out = []
    for line in answer.splitlines():
        line = _BULLET.sub("", line).strip()
        if not line or (line.endswith(":") and not _CITE.search(line)):
            continue
        out += [s.strip() for s in _SENTENCE.split(line) if s.strip()]
    return out
# Medication names a summary might contain, as ingredient -> brand names. Matching is on the
# ingredient after brand normalization. A production system normalizes through RxNorm (RxNav)
# instead of a table; the check itself stays the same.
DRUGS = {
    "furosemide": ["lasix"], "torsemide": ["demadex"], "bumetanide": ["bumex"],
    "lisinopril": ["zestril", "prinivil"], "losartan": ["cozaar"], "valsartan": ["diovan"],
    "sacubitril": ["entresto"], "carvedilol": ["coreg"], "metoprolol": ["toprol", "lopressor"],
    "spironolactone": ["aldactone"], "eplerenone": ["inspra"], "digoxin": ["lanoxin"],
    "amlodipine": ["norvasc"], "hydrochlorothiazide": ["hctz"], "empagliflozin": ["jardiance"],
    "dapagliflozin": ["farxiga"], "metformin": ["glucophage"], "insulin": ["lantus", "humalog"],
    "glipizide": ["glucotrol"], "semaglutide": ["ozempic"], "atorvastatin": ["lipitor"],
    "rosuvastatin": ["crestor"], "aspirin": [], "clopidogrel": ["plavix"], "warfarin": ["coumadin"],
    "apixaban": ["eliquis"], "rivaroxaban": ["xarelto"], "heparin": [], "penicillin": [],
    "amoxicillin": [], "sulfamethoxazole": ["bactrim"], "naltrexone": ["vivitrol"],
    "buprenorphine": ["suboxone"], "methadone": [], "gabapentin": ["neurontin"],
    "prednisone": [], "levothyroxine": ["synthroid"], "omeprazole": ["prilosec"],
    "albuterol": [], "tamsulosin": ["flomax"], "sertraline": ["zoloft"], "trazodone": [],
}
_BRAND = {brand: ingredient for ingredient, brands in DRUGS.items() for brand in brands}
_WORD = re.compile(r"[a-z]+")


def medications_in(text: str) -> set[str]:
    words = _WORD.findall(text.lower())
    return {w for w in words if w in DRUGS} | {_BRAND[w] for w in words if w in _BRAND}


_DIRECTIVE = re.compile(r"\b(you should|recommend (starting|stopping|prescribing)|start (him|her|them) on)\b", re.I)


@dataclass
class InputScreen:
    allowed: list[Chunk]
    quarantined: list[dict] = field(default_factory=list)


def screen_evidence(chunks: list[Chunk]) -> InputScreen:
    screen = InputScreen(allowed=[])
    for chunk in chunks:
        if _INJECTION.search(chunk.text):
            screen.quarantined.append({"chunk": chunk.id, "label": chunk.label,
                                       "reason": "instruction-like text in untrusted evidence"})
        else:
            screen.allowed.append(chunk)
    return screen


@dataclass
class SentenceCheck:
    text: str
    citations: list[int]
    supported: bool
    reasons: list[str]


@dataclass
class OutputCheck:
    answer: str
    sentences: list[SentenceCheck]
    groundedness: float
    removed: int
    phi_redactions: int
    flags: list[str]
    fabricated_medications: int = 0
    medication_statements: int = 0

    def to_json(self) -> dict:
        return {
            "groundedness": self.groundedness,
            "sentences_total": len(self.sentences),
            "sentences_removed": self.removed,
            "phi_redactions": self.phi_redactions,
            "medication_statements": self.medication_statements,
            "fabricated_medications": self.fabricated_medications,
            "flags": self.flags,
            "unsupported": [s.__dict__ for s in self.sentences if not s.supported],
        }


def _numbers(text: str) -> set[str]:
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in _NUMBER.findall(text)}


def check_output(answer: str, evidence: list[Chunk], *, strict: bool = True) -> OutputCheck:
    phi_count = len(_SSN.findall(answer))
    answer = _SSN.sub("[REDACTED]", answer)
    flags = []
    if _DIRECTIVE.search(answer):
        flags.append("contains treatment directive; summaries must describe, not prescribe")
    checks: list[SentenceCheck] = []
    med_statements = fabricated = 0
    # "Fabricated" means named nowhere in the evidence the model was given. A real medication
    # cited to the wrong source (or not cited) is still removed, but it is a citation error.
    available = medications_in(" ".join(c.text for c in evidence))
    for raw in split_statements(answer):
        cites = [int(n) for group in _CITE.findall(raw) for n in group.split(",")]
        reasons = []
        if raw.lower().startswith("not found in the record"):
            checks.append(SentenceCheck(raw, [], True, []))
            continue
        if not cites:
            reasons.append("no citation")
        bad = [c for c in cites if not 1 <= c <= len(evidence)]
        if bad:
            reasons.append(f"cites unknown evidence {bad}")
        # The model sees each chunk's date in its evidence header, so the date counts as evidence too.
        cited_text = " ".join(f"{evidence[c - 1].date} {evidence[c - 1].text}"
                              for c in cites if 1 <= c <= len(evidence))
        claim = _CITE.sub("", raw)
        missing = _numbers(claim) - _numbers(cited_text)
        if cites and missing:
            reasons.append(f"numbers not in cited evidence: {sorted(missing)}")
        if (meds := medications_in(claim)):
            med_statements += 1
            if (invented := meds - available):
                fabricated += 1
                reasons.append(f"fabricated medication (in no evidence): {sorted(invented)}")
            elif cites and (miscited := meds - medications_in(cited_text)):
                reasons.append(f"medications not in cited evidence: {sorted(miscited)}")
        checks.append(SentenceCheck(raw, cites, not reasons, reasons))
    supported = [c for c in checks if c.supported]
    total = len(checks) or 1
    kept = supported if strict else checks
    text = " ".join(c.text if c.supported else f"{c.text} [unverified]" for c in kept)
    removed = len(checks) - len(kept)
    if removed:
        flags.append(f"{removed} unsupported sentence(s) removed")
    if fabricated:
        flags.append(f"{fabricated} statement(s) named a medication absent from cited evidence")
    return OutputCheck(text, checks, round(len(supported) / total, 3), removed, phi_count, flags,
                       fabricated, med_statements)
