import unittest
from pathlib import Path

from hl7bridge import vocab
from hl7bridge.ai.assistant import ClinicalAssistant
from hl7bridge.ccda import CcdaError, ingest, parse, to_bundle
from hl7bridge.pipeline import Pipeline
from hl7bridge.store import FhirStore
from hl7bridge.validate import has_errors, validate_bundle

ROOT = Path(__file__).resolve().parent.parent
CCD = (ROOT / "samples" / "hie" / "10_ccd_three_rivers.xml").read_text()
ICN = "urn:oid:2.16.840.1.113883.4.349|1013579246V135792"


def of_type(bundle, rtype):
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == rtype]


class CcdaMappingTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.warnings, _ = to_bundle(CCD)

    def test_validates_and_covers_sections(self):
        self.assertFalse(has_errors(validate_bundle(self.bundle)))
        counts = {t: len(of_type(self.bundle, t)) for t in
                  ("Patient", "AllergyIntolerance", "Condition", "MedicationStatement", "Observation", "Provenance")}
        self.assertEqual(counts, {"Patient": 1, "AllergyIntolerance": 1, "Condition": 3,
                                  "MedicationStatement": 3, "Observation": 2, "Provenance": 1})

    def test_patient_matched_on_icn_without_overwriting(self):
        entry = next(e for e in self.bundle["entry"] if e["resource"]["resourceType"] == "Patient")
        self.assertEqual(entry["request"], {"method": "POST", "url": "Patient", "ifNoneExist": f"identifier={ICN}"})
        systems = {i["system"] for i in entry["resource"]["identifier"]}
        self.assertNotIn(vocab.US_SSN, systems)
        self.assertIn("SSN removed at boundary (ssn_policy=omit)", self.warnings)

    def test_outside_assertions_are_unconfirmed(self):
        allergy = of_type(self.bundle, "AllergyIntolerance")[0]
        self.assertEqual(allergy["verificationStatus"]["coding"][0]["code"], "unconfirmed")
        self.assertEqual(allergy["code"]["coding"][0]["system"], "http://www.nlm.nih.gov/research/umls/rxnorm")
        self.assertEqual(allergy["reaction"][0]["severity"], "moderate")
        self.assertEqual(allergy["meta"]["source"],
                         "urn:ccda:2.16.840.1.113883.3.9999.1:TRCH-CCD-20260902-7781#allergies/entry[1]")

    def test_problem_med_and_result_details(self):
        ckd = next(c for c in of_type(self.bundle, "Condition") if c["code"]["coding"][0]["code"] == "N18.31")
        self.assertEqual(ckd["code"]["coding"][0]["system"], "http://hl7.org/fhir/sid/icd-10-cm")
        self.assertEqual(ckd["onsetDateTime"], "2025-08-20")
        carvedilol = of_type(self.bundle, "MedicationStatement")[0]
        self.assertEqual(carvedilol["dosage"][0]["text"], "Carvedilol 6.25 mg by mouth twice daily")
        self.assertEqual(carvedilol["informationSource"]["display"], "Three Rivers Community Hospital")
        a1c = next(o for o in of_type(self.bundle, "Observation") if o["code"]["coding"][0]["code"] == "4548-4")
        self.assertEqual(a1c["valueQuantity"]["value"], 8.1)
        self.assertEqual(a1c["interpretation"][0]["coding"][0]["code"], "H")

    def test_hostile_or_wrong_documents_rejected(self):
        bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ClinicalDocument xmlns="urn:hl7-org:v3"/>'
        for bad in (bomb, "<not-xml", '<Foo xmlns="urn:hl7-org:v3"/>'):
            with self.assertRaises(CcdaError):
                parse(bad)


class CcdaIngestTests(unittest.TestCase):
    def setUp(self):
        self.store = FhirStore()
        self.addCleanup(self.store.close)

    def test_attaches_to_existing_va_patient_and_is_idempotent(self):
        Pipeline(self.store).process((ROOT / "samples" / "01_adt_a01_admit.hl7").read_text())
        before = self.store.search("Patient", {"identifier": ICN})[0]
        first = ingest(self.store, CCD)
        self.assertEqual(first["status"], "accepted")
        after = self.store.search("Patient", {"identifier": ICN})
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["meta"]["versionId"], before["meta"]["versionId"])  # demographics untouched
        conditions = self.store.search("Condition", {"patient": before["id"]})
        self.assertEqual(len(conditions), 3)
        counts = self.store.counts()
        self.assertEqual(ingest(self.store, CCD)["status"], "duplicate")
        self.assertEqual(self.store.counts(), counts)

    def test_ledger_copy_is_redacted(self):
        ingest(self.store, CCD)
        raw = self.store.get_message("TRCH-CCD-20260902-7781")["raw_redacted"]
        self.assertNotIn("000123456", raw)
        self.assertNotIn("LIBERTY", raw)
        self.assertNotIn("555-0142", raw)

    def test_assistant_cites_hie_sources(self):
        Pipeline(self.store).process((ROOT / "samples" / "01_adt_a01_admit.hl7").read_text())
        ingest(self.store, CCD)
        assistant = ClinicalAssistant(self.store)
        pid = self.store.search("Patient", {"identifier": ICN})[0]["id"]
        r = assistant.ask(pid, "allergies")
        sulfa = next(c for c in r["citations"] if "sulfamethoxazole" in c["text"].lower())
        self.assertEqual(sulfa["trust"], "hie")
        self.assertTrue(sulfa["label"].startswith("C-CDA · TRCH-CCD-20260902-7781"))
        self.assertIn("unconfirmed", sulfa["text"])


if __name__ == "__main__":
    unittest.main()
