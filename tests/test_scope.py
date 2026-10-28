"""Patient scope, 38 USC 7332, and the clinical-text risks: the attack tests that must hold on
every release."""

import unittest
from pathlib import Path

from hl7bridge.__main__ import _seed
from hl7bridge.ai.access import AccessDenied, ScopeViolation, valid_icn
from hl7bridge.ai.guardrails import check_output, medications_in
from hl7bridge.ai.index import Chunk
from hl7bridge.sensitivity import classify
from hl7bridge.server import App
from hl7bridge.store import FhirStore

ROOT = Path(__file__).resolve().parent.parent
ICN, DUP_ICN = "1013579246V135792", "1019999999V999999"


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.app = App(FhirStore())
        self.addCleanup(self.app.store.close)
        _seed(self.app, quiet=True)
        self.a = self.app.assistant
        self.pid = self.a.resolve_icn(ICN)

    def audits(self, outcome):
        return [e for e in self.app.store.search("AuditEvent", {"_count": 1000}) if e["outcome"] == outcome]

    # -- identity -----------------------------------------------------------------------
    def test_icn_validation_blocks_filter_injection(self):
        self.assertTrue(valid_icn(ICN))
        for bad in ("", "1013579246V13579", f"{ICN}' or 1 eq 1", "Patient/abc", f" {ICN}"):
            with self.assertRaises(AccessDenied, msg=bad):
                self.a.resolve_icn(bad)

    def test_merged_icn_resolves_to_surviving_record(self):
        self.assertEqual(self.a.resolve_icn(DUP_ICN), self.pid)

    # -- access ---------------------------------------------------------------------------
    def test_denials_fail_closed_and_are_audited(self):
        before = len(self.audits("8"))
        for user in ("dr-other-site", "nobody"):
            with self.assertRaises(AccessDenied):
                self.a.ask(self.pid, "medications", user=user)
        self.assertEqual(len(self.audits("8")), before + 2)

    def test_post_retrieval_check_catches_a_filter_bug(self):
        original = self.a.index.search
        foreign = Chunk("Observation/x", "Patient/someone-else", "lab", "Potassium 9.9 mmol/L.", "Observation/x")
        self.a.index.search = lambda *args, **kw: original(*args, **kw) + [(foreign, 1.0)]
        try:
            with self.assertRaises(ScopeViolation):
                self.a.ask(self.pid, "potassium", user="dr-primary")
        finally:
            self.a.index.search = original
        self.assertTrue(any("outside the authorized scope" in e.get("outcomeDesc", "") for e in self.audits("8")))

    # -- 38 USC 7332 ----------------------------------------------------------------------
    def test_labels_assigned_at_ingestion(self):
        cond = lambda code: {"resourceType": "Condition", "code": {"coding": [
            {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": code}]}}
        self.assertEqual(classify(cond("F10.21")), {"ETH"})
        self.assertEqual(classify(cond("B20")), {"HIV"})
        self.assertEqual(classify(cond("D57.1")), {"SCA"})
        self.assertEqual(classify(cond("F17.210")), set())  # tobacco is not covered by 7332
        sud = self.app.store.search("DocumentReference", {"_count": 100})
        labeled = [d for d in sud if any(s["code"] == "ETH" for s in d["meta"].get("security", []))]
        self.assertEqual(len(labeled), 1)

    def test_protected_records_hidden_without_authorization(self):
        q = "Is the patient taking naltrexone or being treated for alcohol use?"
        primary = self.a.ask(self.pid, q, user="dr-primary")
        self.assertTrue(primary["answer"].startswith("Not found"))
        self.assertNotIn("naltrexone", primary["answer"].lower())
        self.assertFalse(any(c["protected"] for c in self.a.search(self.pid, q, user="dr-primary")["results"]))
        sud = self.a.ask(self.pid, q, user="dr-sud-clinic")
        self.assertIn("naltrexone", sud["answer"].lower())
        self.assertTrue(any("ETH" in c["protected"] for c in sud["citations"]))

    # -- clinical-text risks --------------------------------------------------------------
    def test_low_confidence_ocr_is_flagged_not_trusted(self):
        r = self.a.ask(self.pid, "What anticoagulant was listed in the outside consult?", user="dr-primary")
        apixaban = next(c for c in r["citations"] if "apixaban" in c["text"])
        self.assertEqual((apixaban["ocr_confidence"], apixaban["page"]), (0.58, 2))
        self.assertIn("verify against original page 2", r["answer"])
        self.assertTrue(any(f.startswith("low OCR confidence") for f in r["guardrails"]["flags"]))
        self.assertFalse(apixaban["text"].startswith("[OCR"))  # marker stripped from the evidence text

    def test_copied_forward_text_collapses_to_earliest_copy(self):
        hits = self.a.search(self.pid, "hypertension lisinopril home BP log", user="dr-primary")["results"]
        copies = [c for c in hits if "home BP log" in c["text"]]
        self.assertEqual(len(copies), 1)
        self.assertEqual((copies[0]["date"], copies[0]["copies"]), ("2026-09-15", 2))

    def test_retracted_note_leaves_the_index(self):
        q = "torsemide discharge"
        self.assertTrue(any("torsemide" in c["text"] for c in self.a.ask(self.pid, q, user="dr-primary")["citations"]))
        cancel = "\n".join([
            "MSH|^~\\&|VISTA TIU|999^PILOT-VAMC.EXAMPLE^DNS|HL7BRIDGE|NAII-PILOT|20261027100000-0500||MDM^T11^MDM_T01|999-TIU-0199|P|2.4",
            "EVN|T11|20261027100000-0500",
            f"PID|1||{ICN}^^^USVHA&&0363^NI^VA FACILITY ID&200M&L||VETERAN^JAMES^ALAN||19580314|M",
            "TXA|1|DS^DISCHARGE SUMMARY|TX|20261027100000-0500||||||||TIU-88231|||||ER"])
        result = self.app.pipeline.process(cancel)
        self.assertEqual(result.status, "accepted", result.ack)
        doc = next(d for d in self.app.store.search("DocumentReference", {"identifier": "TIU-88231"}))
        self.assertEqual(doc["status"], "entered-in-error")
        after = self.a.ask(self.pid, q, user="dr-primary")
        self.assertFalse(any(c["resource"].startswith(f"DocumentReference/{doc['id']}") for c in after["citations"]))

    def test_indexing_is_on_demand_and_invalidated_on_commit(self):
        self.a.reindex()
        self.assertEqual((self.a.indexed_patients, len(self.a.index)), (0, 0))
        self.a.ask(self.pid, "potassium", user="dr-primary")
        self.assertGreater(len(self.a.index), 0)
        self.assertIn(f"Patient/{self.pid}", self.a.index.patients())
        self.app.pipeline.process((ROOT / "samples" / "02_oru_r01_bmp.hl7").read_text().replace("999-LAB-0042", "999-LAB-0777"))
        self.assertNotIn(f"Patient/{self.pid}", self.a.index.patients())  # dropped, re-read on next request


class MedicationCheckTests(unittest.TestCase):
    EVIDENCE = [Chunk("c", "Patient/1", "note", "Transitioned to oral torsemide 20 mg daily.", "DocumentReference/1"),
                Chunk("d", "Patient/1", "medication", "Medication (active): Lasix 40 mg daily.", "MedicationStatement/1")]

    def test_brand_names_normalize_to_ingredients(self):
        self.assertEqual(medications_in("Started Entresto and Coreg"), {"sacubitril", "carvedilol"})

    def test_fabricated_medication_removed_and_counted(self):
        r = check_output("Takes torsemide 20 mg daily [1]. Also takes spironolactone [1]. "
                         "Furosemide 40 mg daily [2].", self.EVIDENCE)
        self.assertEqual((r.medication_statements, r.fabricated_medications), (3, 1))
        self.assertNotIn("spironolactone", r.answer)
        self.assertIn("Furosemide 40 mg daily [2].", r.answer)  # Lasix in the source == furosemide

    def test_miscited_real_medication_is_removed_but_not_called_fabricated(self):
        r = check_output("Takes furosemide 40 mg daily [1].", self.EVIDENCE)  # it's in [2], not [1]
        self.assertEqual((r.groundedness, r.fabricated_medications), (0.0, 0))
        self.assertIn("medications not in cited evidence: ['furosemide']", r.sentences[0].reasons)


if __name__ == "__main__":
    unittest.main()
