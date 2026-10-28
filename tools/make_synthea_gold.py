"""Generate eval/gold-synthea.json from the Synthea fixtures. Every expected answer is computed
from the patient's structured data, so the gold set is checkable rather than hand-written.

Per patient:
  latest-lab   most recent value of the most-repeated lab (dozens of near-identical results:
               tests whether retrieval finds the *latest*, not just *a* value)
  meds         currently active medications (the same drug often appears stopped and active)
  condition    an active chronic problem
  resolved     a resolved problem: the answer must say it's resolved, not current
  absent       a condition whose distinctive words appear nowhere in the record (expect "not found")
  vaccine      date of the most recent influenza vaccine

Usage: python tools/make_synthea_gold.py
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hl7bridge.ai.index import fmt_number  # noqa: E402
from hl7bridge.fhir_import import load_fixture, source_system  # noqa: E402

ABSENT_CANDIDATES = ["Epilepsy", "Non-small cell lung cancer", "Childhood asthma", "Chronic intractable migraine",
                     "Osteoarthritis of knee", "Alzheimer's disease", "Cystic fibrosis"]
GENERIC = {"disease", "disorder", "of", "chronic", "the", "s"}


def text(cc):
    return (cc or {}).get("text") or next((c.get("display") for c in (cc or {}).get("coding", [])), "")


def num(v):
    return fmt_number(v)  # same precision the index presents


NOT_DRUG = {"actuat", "oral", "tablet", "injection", "capsule", "unt", "auto", "injector", "metered", "dose", "inhaler"}


def drug(name):
    """First real drug word: skips doses and package codes ("0.25 ML Leuprolide", "NDA020800 ...")."""
    return next((w.capitalize() for w in re.findall(r"[A-Za-z]{4,}", name)
                 if w.lower() not in NOT_DRUG and not w.upper().startswith("NDA")), name.split()[0])


def words(s):
    return {w for w in re.findall(r"[a-z]+", s.lower()) if w not in GENERIC and len(w) > 2}


def cases_for(base, resources):
    patient = next(r for r in resources if r["resourceType"] == "Patient")
    key = f"{source_system(base)}|{patient['id']}"
    tag = patient["id"][:8]
    by_type = defaultdict(list)
    for r in resources:
        by_type[r["resourceType"]].append(r)
    record_words = words(json.dumps(resources))
    out = []

    def case(kind, question, **expect):
        out.append({"id": f"{tag}-{kind}", "stratum": f"synthea-{kind}", "patient": key, "question": question, **expect})

    labs = defaultdict(list)
    for o in by_type["Observation"]:
        cats = {c.get("code") for cat in o.get("category", []) for c in cat.get("coding", [])}
        if "laboratory" in cats and "valueQuantity" in o and o.get("effectiveDateTime"):
            labs[text(o["code"])].append(o)
    if labs:
        name, series = max(labs.items(), key=lambda kv: (len(kv[1]), kv[0]))
        if len(series) >= 5:
            latest = max(series, key=lambda o: o["effectiveDateTime"])
            value, date = num(latest["valueQuantity"]["value"]), latest["effectiveDateTime"][:10]
            case("latest-lab", f"What was the most recent {name} result?",
                 expect_cited=[name, value, date], expect_answer=[value])

    active = sorted({drug(text(m.get("medicationCodeableConcept")))
                     for m in by_type["MedicationRequest"] if m.get("status") == "active"
                     and text(m.get("medicationCodeableConcept"))})
    if active:
        names = active[:3]
        case("meds", "What medications is the patient currently taking?", expect_cited=names, expect_answer=names)

    def status(c):
        return next((x.get("code") for x in c.get("clinicalStatus", {}).get("coding", [])), "")

    chronic = [c for c in by_type["Condition"] if status(c) == "active" and "abatementDateTime" not in c
               and "finding" not in text(c["code"]).lower()]
    if chronic:
        name = text(chronic[0]["code"])
        case("condition", f"Does the patient have {name.replace(' (disorder)', '')}?",
             expect_cited=[name], expect_answer=[name.split(" (")[0]])
    resolved = [c for c in by_type["Condition"] if status(c) == "resolved"]
    if resolved:
        name = text(resolved[0]["code"])
        case("resolved", f"Is {name.replace(' (disorder)', '')} a current problem?",
             expect_cited=[name], expect_answer=["resolved"])
    absent = next((c for c in ABSENT_CANDIDATES if not words(c) & record_words), None)
    if absent:
        case("absent", f"Does the patient have {absent}?", expect_not_found=True)

    flu = [i for i in by_type["Immunization"] if "influenza" in text(i.get("vaccineCode")).lower()
           and i.get("occurrenceDateTime")]
    if flu:
        date = max(i["occurrenceDateTime"] for i in flu)[:10]
        case("vaccine", "When did the patient last receive an influenza vaccine?",
             expect_cited=[date], expect_answer=[date])
    return out


def main():
    cases = []
    for path in sorted((ROOT / "samples" / "synthea").glob("*.json.gz")):
        base, resources = load_fixture(path)
        cases += cases_for(base, resources)
    gold = {"description": "Generated by tools/make_synthea_gold.py from Synthea patients (SMART sandbox)",
            "default_user": "dr-primary", "scope_attacks": False, "cases": cases}
    out = ROOT / "eval" / "gold-synthea.json"
    out.write_text(json.dumps(gold, indent=2) + "\n")
    print(f"{len(cases)} cases -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
