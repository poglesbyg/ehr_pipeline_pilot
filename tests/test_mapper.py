import base64
import unittest
from pathlib import Path

from hl7bridge import vocab
from hl7bridge.config import BridgeConfig
from hl7bridge.hl7v2 import Message
from hl7bridge.mapper import Mapper, MappingError
from hl7bridge.validate import has_errors, validate_bundle

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def load(name: str) -> Message:
    return Message.parse((SAMPLES / name).read_text())


def resources(bundle: dict, rtype: str) -> list[dict]:
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == rtype]


class TimestampTests(unittest.TestCase):
    def test_precision_preserved(self):
        self.assertEqual(vocab.to_fhir_datetime("2026"), "2026")
        self.assertEqual(vocab.to_fhir_datetime("202610"), "2026-10")
        self.assertEqual(vocab.to_fhir_datetime("20261020"), "2026-10-20")
        self.assertEqual(vocab.to_fhir_datetime("202610200815", "-05:00"), "2026-10-20T08:15:00-05:00")
        self.assertEqual(vocab.to_fhir_datetime("20261020081501.25+0000"), "2026-10-20T08:15:01.25+00:00")

    def test_invalid(self):
        for bad in ("20261340", "2026-10-20", "abc"):
            with self.assertRaises(ValueError):
                vocab.to_fhir_datetime(bad)


class AdtTests(unittest.TestCase):
    def setUp(self):
        self.result = Mapper().map(load("01_adt_a01_admit.hl7"))
        self.bundle = self.result.bundle
        self.patient = resources(self.bundle, "Patient")[0]

    def test_bundle_validates(self):
        self.assertFalse(has_errors(validate_bundle(self.bundle)), validate_bundle(self.bundle))

    def test_identifiers_icn_dfn_and_ssn_policy(self):
        systems = {i["system"]: i["value"] for i in self.patient["identifier"]}
        self.assertEqual(systems["urn:oid:2.16.840.1.113883.4.349"], "1013579246V135792")
        self.assertIn("7171", systems.values())
        self.assertTrue(any("/va-station/999/" in s for s in systems))
        self.assertNotIn(vocab.US_SSN, systems)
        self.assertIn("SSN removed at boundary (ssn_policy=omit)", self.result.warnings)

    def test_ssn_included_when_configured(self):
        cfg = BridgeConfig(ssn_policy="include")
        patient = resources(Mapper(cfg).map(load("01_adt_a01_admit.hl7")).bundle, "Patient")[0]
        self.assertIn(vocab.US_SSN, {i["system"] for i in patient["identifier"]})

    def test_demographics(self):
        p = self.patient
        self.assertEqual(p["name"][0], {"use": "official", "family": "Veteran", "given": ["James", "Alan"],
                                        "suffix": ["JR"]})
        self.assertEqual(p["gender"], "male")
        self.assertEqual(p["birthDate"], "1958-03-14")
        self.assertEqual(p["address"][0]["city"], "PITTSBURGH")
        self.assertIn({"system": "phone", "value": "(412) 555-0142", "use": "home"}, p["telecom"])
        self.assertIn({"system": "email", "value": "james.veteran@example.org", "use": "home"}, p["telecom"])
        race = next(e for e in p["extension"] if e["url"].endswith("us-core-race"))
        self.assertEqual(race["extension"][0]["valueCoding"]["code"], "2106-3")

    def test_zel_z_segment_extensions(self):
        ext = {e["url"].rsplit("/", 1)[-1]: e for e in self.patient["extension"]}
        self.assertTrue(ext["veteran-status"]["valueBoolean"])
        self.assertEqual(ext["service-connected-percent"]["valueInteger"], 70)

    def test_encounter_and_practitioners(self):
        enc = resources(self.bundle, "Encounter")[0]
        self.assertEqual(enc["class"]["code"], "IMP")
        self.assertEqual(enc["status"], "in-progress")
        self.assertEqual(enc["period"]["start"], "2026-10-20T08:15:00-05:00")
        self.assertEqual(enc["reasonCode"][0]["coding"][0]["code"], "I50.23")
        npis = {i["value"] for p in resources(self.bundle, "Practitioner") for i in p["identifier"]
                if i["system"] == vocab.US_NPI}
        self.assertEqual(npis, {"1234567893", "1679576722"})

    def test_allergies(self):
        allergies = resources(self.bundle, "AllergyIntolerance")
        pcn = next(a for a in allergies if a["code"]["coding"][0]["code"] == "70618")
        self.assertEqual(pcn["category"], ["medication"])
        self.assertEqual(pcn["reaction"][0]["severity"], "severe")

    def test_conditional_upsert_and_lineage(self):
        entry = next(e for e in self.bundle["entry"] if e["resource"]["resourceType"] == "Patient")
        self.assertEqual(entry["request"]["method"], "PUT")
        self.assertTrue(entry["request"]["url"].startswith("Patient?identifier=urn:oid:2.16.840.1.113883.4.349|"))
        self.assertEqual(self.patient["meta"]["source"], "urn:hl7v2:999:999-ADT-0001#PID[1]")

    def test_deterministic_output(self):
        again = Mapper().map(load("01_adt_a01_admit.hl7")).bundle
        self.assertEqual([e["fullUrl"] for e in again["entry"]], [e["fullUrl"] for e in self.bundle["entry"]])

    def test_merge_emits_fhirpath_patch(self):
        bundle = Mapper().map(load("09_adt_a40_merge.hl7")).bundle
        patch = next(e for e in bundle["entry"] if e["request"]["method"] == "PATCH")
        self.assertIn("1019999999V999999", patch["request"]["url"])
        self.assertEqual(patch["resource"]["resourceType"], "Parameters")
        survivor = next(e for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient")
        self.assertEqual(survivor["request"]["method"], "POST")  # never overwrite demographics from MPI


class OruTests(unittest.TestCase):
    def setUp(self):
        self.bundle = Mapper().map(load("02_oru_r01_bmp.hl7")).bundle
        self.obs = {o["code"]["coding"][0]["code"]: o for o in resources(self.bundle, "Observation")}

    def test_validates(self):
        self.assertFalse(has_errors(validate_bundle(self.bundle)))

    def test_lab_patient_is_create_if_absent(self):
        entry = next(e for e in self.bundle["entry"] if e["resource"]["resourceType"] == "Patient")
        self.assertEqual(entry["request"]["method"], "POST")
        self.assertIn("ifNoneExist", entry["request"])

    def test_numeric_value_flag_and_range(self):
        k = self.obs["2823-3"]
        self.assertEqual(k["valueQuantity"], {"value": 5.6, "unit": "mmol/L", "system": vocab.UCUM, "code": "mmol/L"})
        self.assertEqual(k["interpretation"][0]["coding"][0]["code"], "H")
        self.assertEqual(k["referenceRange"][0]["low"]["value"], 3.5)
        self.assertEqual(k["meta"]["source"], "urn:hl7v2:999:999-LAB-0042#OBX[3]")

    def test_obx_note_and_report(self):
        self.assertIn("Baseline creatinine", self.obs["2160-0"]["note"][0]["text"])
        report = resources(self.bundle, "DiagnosticReport")[0]
        self.assertEqual(len(report["result"]), 8)
        self.assertEqual(report["conclusion"], "Specimen slightly hemolyzed; potassium may be falsely elevated.")
        self.assertEqual(report["code"]["coding"][1]["code"], "CHEM7")  # VistA local code kept alongside LOINC

    def test_structured_numeric(self):
        cardiac = Mapper().map(load("03_oru_r01_cardiac.hl7")).bundle
        trop = next(o for o in resources(cardiac, "Observation") if o["code"]["coding"][0]["code"] == "10839-9")
        self.assertEqual(trop["valueQuantity"]["comparator"], "<")
        self.assertEqual(trop["valueQuantity"]["value"], 0.01)

    def test_bad_numeric_is_data_type_error(self):
        raw = (SAMPLES / "02_oru_r01_bmp.hl7").read_text().replace("|182|", "|HIGH|")
        with self.assertRaises(MappingError) as ctx:
            Mapper().map(Message.parse(raw))
        self.assertEqual(ctx.exception.code, "102")


class MdmTests(unittest.TestCase):
    def test_document_reference(self):
        bundle = Mapper().map(load("06_mdm_t02_discharge_summary.hl7")).bundle
        doc = resources(bundle, "DocumentReference")[0]
        self.assertEqual(doc["type"]["coding"][0]["code"], "18842-5")
        self.assertEqual(doc["docStatus"], "final")
        text = base64.b64decode(doc["content"][0]["attachment"]["data"]).decode()
        self.assertIn("torsemide 20 mg daily", text)


class ErrorTests(unittest.TestCase):
    def test_missing_identifier(self):
        with self.assertRaises(MappingError) as ctx:
            Mapper().map(load("90_error_missing_identifier.hl7"))
        self.assertEqual((ctx.exception.code, ctx.exception.location), ("101", "PID-3"))

    def test_unsupported_type(self):
        with self.assertRaises(MappingError) as ctx:
            Mapper().map(load("91_error_unsupported.hl7"))
        self.assertEqual(ctx.exception.code, "200")


if __name__ == "__main__":
    unittest.main()
