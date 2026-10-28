"""C-CDA (HIE) -> FHIR R4: the Continuity of Care Documents that arrive from community
partners over eHealth Exchange / Carequality.

Covers the four sections that matter most for a clinician's picture of outside care -
allergies, problems, medications and results - and keeps the same lineage contract as the
HL7 v2 path: ``meta.source = urn:ccda:<document id>#<section>/entry[n]``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from . import vocab
from .config import BridgeConfig
from .sensitivity import LABELS, label_bundle
from .store import FhirStore, StoreError
from .validate import has_errors, validate_bundle

NS = {"v3": "urn:hl7-org:v3", "xsi": "http://www.w3.org/2001/XMLSchema-instance"}
_NS_UUID = uuid.UUID("0b8f8c35-27b3-4f27-9b55-2f5f1d5ad1e3")

OID_SYSTEMS = {
    "2.16.840.1.113883.6.1": "http://loinc.org",
    "2.16.840.1.113883.6.96": "http://snomed.info/sct",
    "2.16.840.1.113883.6.88": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "2.16.840.1.113883.6.90": "http://hl7.org/fhir/sid/icd-10-cm",
    "2.16.840.1.113883.6.238": vocab.CDC_RACE_ETH,
    "2.16.840.1.113883.5.83": vocab.V3_INTERPRETATION,
    "2.16.840.1.113883.4.1": vocab.US_SSN,
}

SECTIONS = {"48765-2": "allergies", "11450-4": "problems", "10160-0": "medications", "30954-2": "results"}
GENDER = {"M": "male", "F": "female", "UN": "other", "UNK": "unknown"}


class CcdaError(Exception):
    def __init__(self, message: str, status: str = "error"):
        super().__init__(message)
        self.status = status


def _ts(value: str | None, default_tz: str) -> str | None:
    if not value:
        return None
    try:
        return vocab.to_fhir_datetime(value, default_tz)
    except ValueError as exc:
        raise CcdaError(f"invalid C-CDA timestamp {value!r}") from exc


class _Doc:
    def __init__(self, root: ET.Element, config: BridgeConfig):
        self.root = root
        self.config = config
        doc_id = root.find("v3:id", NS)
        if doc_id is None or not doc_id.get("root"):
            raise CcdaError("ClinicalDocument/id is required")
        self.doc_id = doc_id.get("extension") or doc_id.get("root")
        self.source_base = f"urn:ccda:{doc_id.get('root')}:{self.doc_id}"
        self.entries: list[dict] = []
        self.warnings: list[str] = []
        self.custodian = self.text(root, "v3:custodian/v3:assignedCustodian/v3:representedCustodianOrganization/v3:name") \
            or "outside organization"

    def find(self, el: ET.Element | None, path: str) -> ET.Element | None:
        return el.find(path, NS) if el is not None else None

    def text(self, el: ET.Element | None, path: str = ".") -> str:
        node = el.find(path, NS) if el is not None and path != "." else el
        return "".join(node.itertext()).strip() if node is not None else ""

    def concept(self, el: ET.Element | None) -> dict | None:
        """CD/CE element -> CodeableConcept (with translations)."""
        if el is None:
            return None
        codings = []
        for node in [el, *el.findall("v3:translation", NS)]:
            if node.get("code"):
                coding = {"code": node.get("code")}
                oid = node.get("codeSystem", "")
                coding["system"] = OID_SYSTEMS.get(oid, f"urn:oid:{oid}" if oid else None)
                if node.get("displayName"):
                    coding["display"] = node.get("displayName")
                codings.append({k: v for k, v in coding.items() if v})
        text = self.text(el, "v3:originalText") or el.get("displayName", "")
        if not codings and not text:
            return None
        return {**({"coding": codings} if codings else {}), **({"text": text} if text else {})}

    def add(self, resource: dict, where: str | None, *, conditional: str | None = None,
            mode: str = "create") -> str:
        seed = f"{self.source_base}/{resource['resourceType']}/{where or len(self.entries)}"
        full_url = f"urn:uuid:{uuid.uuid5(_NS_UUID, seed)}"
        if where:
            resource.setdefault("meta", {})["source"] = f"{self.source_base}#{where}"
        if conditional and mode == "create-if-absent":
            request = {"method": "POST", "url": resource["resourceType"], "ifNoneExist": conditional}
        elif conditional:
            request = {"method": "PUT", "url": f"{resource['resourceType']}?{conditional}"}
        else:
            request = {"method": "POST", "url": resource["resourceType"]}
        self.entries.append({"fullUrl": full_url, "resource": resource, "request": request})
        return full_url


def _patient(doc: _Doc) -> str:
    role = doc.root.find("v3:recordTarget/v3:patientRole", NS)
    if role is None:
        raise CcdaError("recordTarget/patientRole is required")
    identifiers = []
    for i in role.findall("v3:id", NS):
        if not i.get("extension"):
            continue
        system = OID_SYSTEMS.get(i.get("root", ""), f"urn:oid:{i.get('root')}")
        if system == vocab.US_SSN and doc.config.ssn_policy != "include":
            doc.warnings.append("SSN removed at boundary (ssn_policy=omit)")
            continue
        identifiers.append({"system": system, "value": i.get("extension")})
    if not identifiers:
        raise CcdaError("patientRole/id has no usable identifier")
    pat = role.find("v3:patient", NS)
    patient: dict = {"resourceType": "Patient",
                     "meta": {"profile": [f"{vocab.US_CORE}/us-core-patient"]},
                     "identifier": identifiers}
    if (name := doc.find(pat, "v3:name")) is not None:
        patient["name"] = [{k: v for k, v in (
            ("family", doc.text(name, "v3:family").title()),
            ("given", [doc.text(g).title() for g in name.findall("v3:given", NS)])) if v}]
    if (g := doc.find(pat, "v3:administrativeGenderCode")) is not None:
        patient["gender"] = GENDER.get(g.get("code", ""), "unknown")
    if (b := doc.find(pat, "v3:birthTime")) is not None and b.get("value"):
        patient["birthDate"] = vocab.to_fhir_date(b.get("value"))
    # The gateway issued the query for a VA ICN, so prefer it as the match key; otherwise the
    # partner MRN creates (or finds) a separate record for MPI correlation to resolve.
    key = next((i for i in identifiers if i["system"] == doc.config.icn_system), identifiers[0])
    return doc.add(patient, "recordTarget", conditional=f"identifier={key['system']}|{key['value']}",
                   mode="create-if-absent")


def _entries(doc: _Doc, section: ET.Element, name: str):
    for n, entry in enumerate(section.findall("v3:entry", NS), 1):
        yield f"{name}/entry[{n}]", entry


def _allergies(doc: _Doc, section: ET.Element, patient: str) -> None:
    for where, entry in _entries(doc, section, "allergies"):
        obs = entry.find(".//v3:observation", NS)
        allergen = doc.concept(doc.find(obs, ".//v3:participant/v3:participantRole/v3:playingEntity/v3:code"))
        if allergen is None:
            doc.warnings.append(f"{where}: no allergen code; skipped")
            continue
        allergy: dict = {
            "resourceType": "AllergyIntolerance",
            "meta": {"profile": [f"{vocab.US_CORE}/us-core-allergyintolerance"]},
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                                           "code": "active"}]},
            "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification",
                                               "code": "unconfirmed"}]},  # outside assertion until reconciled
            "code": allergen,
            "patient": {"reference": patient},
        }
        reactions, severity = [], None
        for rel in obs.findall("v3:entryRelationship/v3:observation", NS):
            code = doc.find(rel, "v3:code")
            value = doc.concept(doc.find(rel, "v3:value"))
            if code is not None and code.get("code") == "SEV" and value:
                severity = (value.get("coding") or [{}])[0].get("display", "").lower() or None
            elif value:
                reactions.append(value)
        if reactions:
            reaction: dict = {"manifestation": reactions}
            if severity in ("mild", "moderate", "severe"):
                reaction["severity"] = severity
            allergy["reaction"] = [reaction]
        if (low := doc.find(obs, "v3:effectiveTime/v3:low")) is not None and low.get("value"):
            allergy["onsetDateTime"] = _ts(low.get("value"), doc.config.default_tz)
        doc.add(allergy, where)


def _problems(doc: _Doc, section: ET.Element, patient: str) -> None:
    for where, entry in _entries(doc, section, "problems"):
        obs = entry.find(".//v3:observation", NS)
        code = doc.concept(doc.find(obs, "v3:value"))
        if code is None:
            doc.warnings.append(f"{where}: no problem code; skipped")
            continue
        cond: dict = {
            "resourceType": "Condition",
            "meta": {"profile": [f"{vocab.US_CORE}/us-core-condition-problems-health-concerns"]},
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                           "code": "active"}]},
            "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                                               "code": "unconfirmed"}]},
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                      "code": "problem-list-item", "display": "Problem List Item"}]}],
            "code": code,
            "subject": {"reference": patient},
        }
        if (low := doc.find(obs, "v3:effectiveTime/v3:low")) is not None and low.get("value"):
            cond["onsetDateTime"] = _ts(low.get("value"), doc.config.default_tz)
        doc.add(cond, where)


def _medications(doc: _Doc, section: ET.Element, patient: str) -> None:
    for where, entry in _entries(doc, section, "medications"):
        sa = entry.find("v3:substanceAdministration", NS)
        med = doc.concept(doc.find(sa, ".//v3:manufacturedMaterial/v3:code"))
        if med is None:
            doc.warnings.append(f"{where}: no medication code; skipped")
            continue
        status = doc.find(sa, "v3:statusCode")
        stmt: dict = {
            "resourceType": "MedicationStatement",
            "status": "completed" if status is not None and status.get("code") == "completed" else "active",
            "medicationCodeableConcept": med,
            "subject": {"reference": patient},
            "informationSource": {"display": doc.custodian},
        }
        if (low := doc.find(sa, "v3:effectiveTime/v3:low")) is not None and low.get("value"):
            stmt["effectivePeriod"] = {"start": _ts(low.get("value"), doc.config.default_tz)}
        if (sig := doc.text(sa, "v3:text")):
            stmt["dosage"] = [{"text": sig}]
        doc.add(stmt, where)


def _results(doc: _Doc, section: ET.Element, patient: str) -> None:
    lab = {"coding": [{"system": vocab.OBS_CATEGORY, "code": "laboratory", "display": "Laboratory"}]}
    for n, obs in enumerate(section.findall(".//v3:organizer/v3:component/v3:observation", NS), 1):
        where = f"results/observation[{n}]"
        code = doc.concept(doc.find(obs, "v3:code"))
        if code is None:
            continue
        o: dict = {"resourceType": "Observation",
                   "meta": {"profile": [f"{vocab.US_CORE}/us-core-observation-lab"]},
                   "status": "final", "category": [lab], "code": code, "subject": {"reference": patient}}
        if (eff := doc.find(obs, "v3:effectiveTime")) is not None and eff.get("value"):
            o["effectiveDateTime"] = _ts(eff.get("value"), doc.config.default_tz)
        value = doc.find(obs, "v3:value")
        vtype = value.get(f"{{{NS['xsi']}}}type", "") if value is not None else ""
        if vtype == "PQ" and value.get("value"):
            unit = value.get("unit", "")
            o["valueQuantity"] = {"value": vocab.number(value.get("value")), "unit": unit,
                                  "system": vocab.UCUM, "code": unit}
        elif value is not None and (cc := doc.concept(value)):
            o["valueCodeableConcept"] = cc
        elif value is not None and doc.text(value):
            o["valueString"] = doc.text(value)
        else:
            o["dataAbsentReason"] = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/data-absent-reason",
                                                 "code": "unknown"}]}
        if (interp := doc.find(obs, "v3:interpretationCode")) is not None and interp.get("code") in vocab.INTERPRETATION:
            o["interpretation"] = [{"coding": [{"system": vocab.V3_INTERPRETATION, "code": interp.get("code"),
                                                "display": vocab.INTERPRETATION[interp.get("code")]}]}]
        if (rng := doc.text(obs, "v3:referenceRange/v3:observationRange/v3:text")):
            o["referenceRange"] = [{"text": rng}]
        doc.add(o, where)


HANDLERS = {"allergies": _allergies, "problems": _problems, "medications": _medications, "results": _results}


def parse(xml_text: str) -> ET.Element:
    # Reject DTDs outright: no entity expansion, no external entity resolution (SI-10).
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_text[:4096], re.I):
        raise CcdaError("DTD/ENTITY declarations are not accepted", status="rejected")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise CcdaError(f"malformed XML: {exc}", status="rejected") from exc
    if root.tag != f"{{{NS['v3']}}}ClinicalDocument":
        raise CcdaError("root element must be hl7-org:v3 ClinicalDocument", status="rejected")
    return root


def to_bundle(xml_text: str, config: BridgeConfig | None = None) -> tuple[dict, list[str], _Doc]:
    config = config or BridgeConfig()
    doc = _Doc(parse(xml_text), config)
    patient = _patient(doc)
    found = set()
    for section in doc.root.findall(".//v3:component/v3:section", NS):
        code = doc.find(section, "v3:code")
        name = SECTIONS.get(code.get("code") if code is not None else "")
        if name:
            found.add(name)
            HANDLERS[name](doc, section, patient)
    for missing in sorted(set(SECTIONS.values()) - found):
        doc.warnings.append(f"no {missing} section in document")
    title = doc.text(doc.root, "v3:title") or "C-CDA document"
    eff = doc.find(doc.root, "v3:effectiveTime")
    recorded = _ts(eff.get("value") if eff is not None else None, config.default_tz)
    if not recorded or "T" not in recorded:
        recorded = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doc.add({
        "resourceType": "Provenance",
        "meta": {"source": doc.source_base},
        "target": [{"reference": e["fullUrl"]} for e in doc.entries],
        "recorded": recorded,
        "agent": [{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                        "code": "author"}]}, "who": {"display": doc.custodian}}],
        "entity": [{"role": "source", "what": {"identifier": {"system": "urn:ietf:rfc:3986", "value": doc.source_base},
                                               "display": f"{title} from {doc.custodian} (HIE)"}}],
    }, None)
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": doc.entries}
    for code, n in label_bundle(bundle).items():
        doc.warnings.append(f"{n} resource(s) labeled {code}: {LABELS[code]}")
    return bundle, doc.warnings, doc


def ingest(store: FhirStore, xml_text: str, config: BridgeConfig | None = None, on_commit=None) -> dict:
    """Map, validate and commit one C-CDA; record it in the message ledger. Returns a result dict."""
    t0 = time.perf_counter()
    result: dict = {"status": "accepted", "warnings": [], "bundle": None, "response": None, "outcome": None}
    doc = None
    try:
        bundle, warnings, doc = to_bundle(xml_text, config)
        prior = store.get_message(doc.doc_id)
        if prior and prior["status"] == "accepted":
            # HIE gateways re-deliver on retry; same document id -> no new writes.
            return {**result, "status": "duplicate", "control_id": doc.doc_id,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)}
        result.update(bundle=bundle, warnings=warnings)
        outcome = validate_bundle(bundle)
        result["outcome"] = outcome
        if has_errors(outcome):
            result["status"] = "error"
        else:
            result["response"] = store.transaction(bundle)
    except (CcdaError, StoreError) as exc:
        result["status"] = getattr(exc, "status", "error") if isinstance(exc, CcdaError) else "error"
        result["outcome"] = {"resourceType": "OperationOutcome",
                             "issue": [{"severity": "error", "code": "processing", "diagnostics": str(exc)}]}
    result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    refs = [e["response"]["location"].rsplit("/_history", 1)[0] for e in (result["response"] or {}).get("entry", [])]
    if doc is not None:
        result["control_id"] = doc.doc_id
        error = next((i["diagnostics"] for i in result["outcome"]["issue"] if i["severity"] == "error"), None) \
            if result["status"] != "accepted" else None
        store.log_message(control_id=doc.doc_id, sender=f"HIE@{doc.custodian}", message_type="C-CDA CCD",
                          status=result["status"], ack=f"{result['status'].upper()} {len(refs)} resources",
                          error=error, raw_redacted=_redact(xml_text), resources=json.dumps(refs),
                          elapsed_ms=result["elapsed_ms"])
    if refs and on_commit:
        on_commit(refs)
    return result


def _redact(xml_text: str) -> str:
    """Ledger copy without SSN ids, street addresses or phone numbers."""
    xml_text = re.sub(r'(<id[^>]*root="2\.16\.840\.1\.113883\.4\.1"[^>]*extension=")[^"]*', r"\1***", xml_text)
    xml_text = re.sub(r"<streetAddressLine>[^<]*</streetAddressLine>", "<streetAddressLine>***</streetAddressLine>", xml_text)
    return re.sub(r'(<telecom[^>]*value=")[^"]*', r"\1***", xml_text)
