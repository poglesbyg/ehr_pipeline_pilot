import json
import threading
import unittest
import urllib.request
from pathlib import Path

from hl7bridge.mllp import MLLPServer, send
from hl7bridge.pipeline import Pipeline
from hl7bridge.server import App, serve_http
from hl7bridge.store import FhirStore

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
ICN = "urn:oid:2.16.840.1.113883.4.349|1013579246V135792"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.store = FhirStore()
        self.pipeline = Pipeline(self.store)
        self.addCleanup(self.store.close)

    def run_all(self):
        return {f.name: self.pipeline.process(f.read_text()) for f in sorted(SAMPLES.glob("*.hl7"))}

    def test_all_samples_ack_correctly(self):
        results = self.run_all()
        for name, r in results.items():
            expected = {"90": "error", "91": "rejected"}.get(name[:2], "accepted")
            self.assertEqual(r.status, expected, f"{name}: {r.ack}")
        self.assertIn("MSA|AE|999-ADT-0099", results["90_error_missing_identifier.hl7"].ack)
        self.assertIn("ERR|||101^", results["90_error_missing_identifier.hl7"].ack)
        self.assertIn("MSA|AR|999-SIU-0001", results["91_error_unsupported.hl7"].ack)

    def test_resend_is_idempotent(self):
        first = self.pipeline.process(sample("01_adt_a01_admit.hl7"))
        counts = self.store.counts()
        again = self.pipeline.process(sample("01_adt_a01_admit.hl7"))
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(again.ack, first.ack)
        self.assertEqual(self.store.counts(), counts)

    def test_update_versions_same_patient_and_lab_does_not_clobber(self):
        self.run_all()
        survivors = [p for p in self.store.search("Patient", {"identifier": ICN})]
        self.assertEqual(len(survivors), 1)
        p = survivors[0]
        self.assertEqual(p["address"][0]["city"], "CORAOPOLIS")  # A08 applied; ORU/MDM/A40 did not overwrite
        self.assertEqual(len(self.store.history("Patient", p["id"])), 3)  # A01, A08, A03

    def test_corrected_lab_result_updates_in_place(self):
        self.pipeline.process(sample("02_oru_r01_bmp.hl7"))
        corrected = sample("02_oru_r01_bmp.hl7").replace("999-LAB-0042", "999-LAB-0042C").replace(
            "|5.6|mmol/L|3.5-5.1|H|||F", "|4.7|mmol/L|3.5-5.1|N|||C")
        self.assertEqual(self.pipeline.process(corrected).status, "accepted")
        k = self.store.search("Observation", {"code": "http://loinc.org|2823-3"})
        self.assertEqual(len(k), 1)
        self.assertEqual((k[0]["valueQuantity"]["value"], k[0]["status"], k[0]["meta"]["versionId"]),
                         (4.7, "corrected", "2"))

    def test_merge_links_records(self):
        self.run_all()
        old = self.store.search("Patient", {"identifier": "1019999999V999999"})[0]
        new = self.store.search("Patient", {"identifier": ICN})[0]
        self.assertFalse(old["active"])
        self.assertEqual(old["link"], [{"other": {"reference": f"Patient/{new['id']}"}, "type": "replaced-by"}])

    def test_merge_of_unknown_patient_is_ae_and_rolls_back(self):
        before = self.store.counts()
        r = self.pipeline.process(sample("09_adt_a40_merge.hl7"))
        self.assertEqual(r.status, "error")
        self.assertIn("MSA|AE", r.ack)
        self.assertIn("|204^", r.ack)
        self.assertEqual(self.store.counts(), before)  # atomic: nothing partially written

    def test_ledger_is_redacted(self):
        self.pipeline.process(sample("01_adt_a01_admit.hl7"))
        raw = self.store.get_message("999-ADT-0001")["raw_redacted"]
        self.assertNotIn("000123456", raw)
        self.assertNotIn("LIBERTY", raw)
        self.assertIn("1013579246V135792", raw)

    def test_everything(self):
        self.run_all()
        pid = self.store.search("Patient", {"identifier": ICN})[0]["id"]
        types = {r["resourceType"] for r in self.store.everything(pid)}
        self.assertTrue({"Patient", "Encounter", "Observation", "DiagnosticReport", "DocumentReference",
                         "AllergyIntolerance", "Practitioner", "Provenance"} <= types)

    def test_garbage_is_rejected_with_ack(self):
        r = self.pipeline.process("this is not hl7")
        self.assertEqual(r.status, "rejected")
        self.assertIn("MSA|AR", r.ack)


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App(FhirStore())
        cls.mllp = MLLPServer(lambda t: cls.app.pipeline.process(t).ack, port=0).start_in_thread()
        cls.httpd = serve_http(cls.app, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.mllp.stop()
        cls.app.store.close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.load(r)

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=body.encode(), method="POST")
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def test_mllp_round_trip_then_fhir_search_and_ask(self):
        for name in ("01_adt_a01_admit.hl7", "02_oru_r01_bmp.hl7"):
            ack = send(sample(name), port=self.mllp.port)
            self.assertIn("MSA|AA|", ack)
        bundle = self.get(f"/fhir/Patient?identifier={ICN}")
        self.assertEqual(bundle["total"], 1)
        pid = bundle["entry"][0]["resource"]["id"]
        labs = self.get(f"/fhir/Observation?patient={pid}&category=laboratory")
        self.assertEqual(labs["total"], 8)
        answer = self.post("/ai/ask", json.dumps({"patient": pid, "question": "potassium"}))
        self.assertIn("5.6", answer["answer"])
        self.assertTrue(self.get("/fhir/AuditEvent")["total"] >= 1)
        self.assertEqual(self.get("/fhir/metadata")["fhirVersion"], "4.0.1")

    def test_dry_run_does_not_persist(self):
        r = self.post("/hl7/v2/$convert", sample("03_oru_r01_cardiac.hl7"))
        self.assertEqual(r["status"], "accepted")
        self.assertIsNone(self.app.store.get_message("999-LAB-0043"))


if __name__ == "__main__":
    unittest.main()
