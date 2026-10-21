"""Pre-commit validation of mapped Bundles against the US Core rules this pilot relies on.

Deliberately small and dependency-free; in a production pipeline the official HL7 FHIR
Validator (with the US Core package) runs in CI against the golden outputs as well.
"""

from __future__ import annotations

import re

_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_DATETIME = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2}))?)?)?$")
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

REQUIRED = {
    "Patient": ["identifier", "name", "gender"],
    "Practitioner": ["name"],
    "Encounter": ["status", "class", "type", "subject"],
    "Observation": ["status", "category", "code", "subject"],
    "DiagnosticReport": ["status", "category", "code", "subject"],
    "DocumentReference": ["status", "type", "category", "subject", "content"],
    "AllergyIntolerance": ["clinicalStatus", "verificationStatus", "code", "patient"],
    "Provenance": ["target", "recorded", "agent"],
    "Condition": ["clinicalStatus", "category", "code", "subject"],
    "MedicationStatement": ["status", "medicationCodeableConcept", "subject"],
}

VALUE_SETS = {
    ("Patient", "gender"): {"male", "female", "other", "unknown"},
    ("Observation", "status"): {"registered", "preliminary", "final", "amended", "corrected",
                                "cancelled", "entered-in-error", "unknown"},
    ("DiagnosticReport", "status"): {"registered", "partial", "preliminary", "final", "amended",
                                     "corrected", "appended", "cancelled", "entered-in-error", "unknown"},
    ("Encounter", "status"): {"planned", "arrived", "triaged", "in-progress", "onleave", "finished",
                              "cancelled", "entered-in-error", "unknown"},
}


def _issue(severity: str, code: str, text: str, expr: str) -> dict:
    return {"severity": severity, "code": code, "diagnostics": text, "expression": [expr]}


def validate_resource(res: dict, where: str) -> list[dict]:
    rtype = res.get("resourceType", "")
    issues = []
    for element in REQUIRED.get(rtype, []):
        if res.get(element) in (None, [], {}, ""):
            issues.append(_issue("error", "required", f"{rtype}.{element} is required (US Core)",
                                 f"{where}.{element}"))
    for (vtype, element), allowed in VALUE_SETS.items():
        if rtype == vtype and element in res and res[element] not in allowed:
            issues.append(_issue("error", "code-invalid", f"{res[element]!r} not in {vtype}.{element} value set",
                                 f"{where}.{element}"))
    if rtype == "Observation" and not any(k.startswith("value") for k in res) and "dataAbsentReason" not in res:
        issues.append(_issue("error", "required", "Observation needs value[x] or dataAbsentReason",
                             f"{where}.value[x]"))
    if rtype == "Patient" and not any(i.get("system") and i.get("value") for i in res.get("identifier", [])):
        issues.append(_issue("error", "required", "Patient.identifier needs system and value",
                             f"{where}.identifier"))
    for key, pattern, label in (("birthDate", _DATE, "date"), ("effectiveDateTime", _DATETIME, "dateTime"),
                                ("issued", _INSTANT, "instant"), ("recorded", _INSTANT, "instant"),
                                ("date", _INSTANT, "instant")):
        if key in res and not pattern.match(str(res[key])):
            issues.append(_issue("error", "value", f"{key} {res[key]!r} is not a valid FHIR {label}",
                                 f"{where}.{key}"))
    return issues


def validate_bundle(bundle: dict) -> dict:
    """Return an OperationOutcome; any ``error`` issue blocks the commit."""
    issues: list[dict] = []
    full_urls = {e.get("fullUrl") for e in bundle.get("entry", [])}
    for i, entry in enumerate(bundle.get("entry", [])):
        res = entry.get("resource", {})
        where = f"Bundle.entry[{i}].resource"
        if entry.get("request", {}).get("method") != "PATCH":
            issues += validate_resource(res, where)
        for ref in _references(res):
            if ref.startswith("urn:uuid:") and ref not in full_urls:
                issues.append(_issue("error", "not-found", f"unresolved reference {ref}", where))
    if not issues:
        issues.append({"severity": "information", "code": "informational",
                       "diagnostics": "All resources passed pre-commit validation"})
    return {"resourceType": "OperationOutcome", "issue": issues}


def has_errors(outcome: dict) -> bool:
    return any(i["severity"] in ("error", "fatal") for i in outcome.get("issue", []))


def _references(node) -> list[str]:
    out = []
    if isinstance(node, dict):
        if isinstance(node.get("reference"), str):
            out.append(node["reference"])
        for v in node.values():
            out += _references(v)
    elif isinstance(node, list):
        for v in node:
            out += _references(v)
    return out
