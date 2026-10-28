"""Sensitivity labels for records protected under 38 USC 7332.

Section 7332 gives extra protection to records of drug abuse, alcoholism or alcohol abuse,
HIV infection and sickle cell anemia. Labels are applied once, at ingestion, as standard FHIR
``meta.security`` codes, so every downstream consumer (index, API, export) sees the same
decision. Retrieval then excludes labeled evidence unless the clinician is authorized.

The rules are deliberately conservative pattern matches for the pilot. In production the
labels come from VistA's own sensitivity flags and a terminology service, and the rules are
reviewed with the privacy officer.
"""

from __future__ import annotations

import base64
import re

ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"

LABELS = {
    "ETH": "substance use (38 USC 7332)",
    "HIV": "HIV (38 USC 7332)",
    "SCA": "sickle cell anemia (38 USC 7332)",
}

# ICD-10-CM prefixes. F17 (nicotine) is deliberately excluded: tobacco is not covered by 7332.
_ICD10 = [
    (re.compile(r"^F1[0-689]"), "ETH"),
    (re.compile(r"^(B20|Z21)"), "HIV"),
    (re.compile(r"^D57"), "SCA"),
]
_TEXT = [
    (re.compile(r"\b(alcohol (use disorder|dependence|abuse)|opioid use disorder|substance use (disorder|treatment|clinic)"
                r"|SUD clinic|naltrexone|buprenorphine|methadone maintenance)\b", re.I), "ETH"),
    (re.compile(r"\bHIV\b"), "HIV"),
    (re.compile(r"\bsickle cell\b", re.I), "SCA"),
]


def _texts(res: dict) -> list[str]:
    """Human-readable text that carries clinical meaning: codings, notes, document bodies."""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("display", "text", "conclusion", "title") and isinstance(value, str):
                    out.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk({k: v for k, v in res.items() if k not in ("meta", "subject", "patient", "identifier")})
    if res.get("resourceType") == "DocumentReference":
        for content in res.get("content", []):
            data = content.get("attachment", {}).get("data")
            if data:
                out.append(base64.b64decode(data).decode("utf-8", errors="replace"))
    return out


def _codes(res: dict) -> list[str]:
    codes = []
    for field in ("code", "medicationCodeableConcept"):
        for coding in (res.get(field) or {}).get("coding", []):
            if "icd-10" in coding.get("system", ""):
                codes.append(coding.get("code", ""))
    return codes


def classify(res: dict) -> set[str]:
    if res.get("resourceType") in ("Patient", "Practitioner", "Provenance", "Parameters", "AuditEvent"):
        return set()
    found = {label for code in _codes(res) for pattern, label in _ICD10 if pattern.match(code)}
    for text in _texts(res):
        found |= {label for pattern, label in _TEXT if pattern.search(text)}
    return found


def label_bundle(bundle: dict) -> dict[str, int]:
    """Add ``meta.security`` labels in place; returns a count per label for the ledger/warnings."""
    counts: dict[str, int] = {}
    for entry in bundle.get("entry", []):
        res = entry.get("resource", {})
        for code in sorted(classify(res)):
            security = res.setdefault("meta", {}).setdefault("security", [])
            if not any(s.get("code") == code for s in security):
                security.append({"system": ACT_CODE, "code": code, "display": LABELS[code]})
            counts[code] = counts.get(code, 0) + 1
    return counts


def labels_of(res: dict) -> set[str]:
    return {s["code"] for s in res.get("meta", {}).get("security", [])
            if s.get("system") == ACT_CODE and s.get("code") in LABELS}
