"""HL7 v2 -> FHIR R4 transaction Bundles, profiled to US Core.

Every resource records the exact HL7 segment it came from in ``meta.source``
(``urn:hl7v2:<facility>:<control id>#<segment>[n]``). That lineage is what lets the search
layer cite a summary sentence all the way back to the VistA message that produced it.
"""

from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import vocab
from .config import BridgeConfig
from .hl7v2 import Message, Rep, Segment

_NS = uuid.UUID("6f1c5a52-6a43-4b0e-9a8e-0c1d7f0b8e11")

DOCUMENT_TYPES = {  # TXA-2 -> LOINC document type
    "DS": ("18842-5", "Discharge summary"),
    "PN": ("11506-3", "Progress note"),
    "CN": ("11488-4", "Consult note"),
    "HP": ("34117-2", "History and physical note"),
    "RAD": ("18748-4", "Diagnostic imaging study"),
}


class MappingError(Exception):
    """The message is well-formed HL7 but cannot be mapped; carries an HL7 0357 error code."""

    def __init__(self, message: str, *, code: str = "207", location: str = ""):
        super().__init__(message)
        self.code = code
        self.location = location


@dataclass
class MappingResult:
    bundle: dict
    warnings: list[str] = field(default_factory=list)


class _Context:
    def __init__(self, msg: Message, config: BridgeConfig):
        self.msg = msg
        self.config = config
        self.entries: list[dict] = []
        self.warnings: list[str] = []
        self._by_key: dict[tuple, str] = {}
        facility = msg.sending_facility or "unknown"
        self.source_base = f"urn:hl7v2:{facility}:{msg.control_id}"

    def require(self, name: str) -> Segment:
        seg = self.msg.segment(name)
        if seg is None:
            raise MappingError(f"required segment {name} is missing", code="100", location=name)
        return seg

    def loc(self, seg: Segment) -> str:
        n = sum(1 for s in self.msg.segments[: self.msg.segments.index(seg) + 1] if s.name == seg.name)
        return f"{seg.name}[{n}]"

    def dt(self, value: str, where: str) -> str | None:
        try:
            return vocab.to_fhir_datetime(value, self.config.default_tz)
        except ValueError as exc:
            raise MappingError(str(exc), code="102", location=where) from exc

    def add(self, resource: dict, seg: Segment | None, *, identifier: dict | None = None,
            mode: str = "upsert", patch_query: str | None = None) -> str:
        """Add a transaction entry and return its ``urn:uuid`` reference.

        ``mode``: ``upsert`` (conditional PUT on identifier), ``create-if-absent``
        (POST + ifNoneExist), ``create`` (plain POST) or ``patch`` (conditional FHIRPath PATCH).
        """
        rtype = resource["resourceType"] if mode != "patch" else patch_query.split("?")[0]
        key = (rtype, identifier["system"], identifier["value"]) if identifier else None
        if key and key in self._by_key:
            return self._by_key[key]
        seed = f"{self.msg.control_id}/{rtype}/{key or len(self.entries)}"
        full_url = f"urn:uuid:{uuid.uuid5(_NS, seed)}"
        if seg is not None and mode != "patch":
            resource.setdefault("meta", {})["source"] = f"{self.source_base}#{self.loc(seg)}"
        query = f"identifier={identifier['system']}|{identifier['value']}" if identifier else None
        if mode == "patch":
            request = {"method": "PATCH", "url": patch_query}
        elif mode == "upsert" and query:
            request = {"method": "PUT", "url": f"{rtype}?{query}"}
        elif mode == "create-if-absent" and query:
            request = {"method": "POST", "url": rtype, "ifNoneExist": query}
        else:
            request = {"method": "POST", "url": rtype}
        self.entries.append({"fullUrl": full_url, "resource": resource, "request": request})
        if key:
            self._by_key[key] = full_url
        return full_url

    def coding(self, rep: Rep, offset: int = 0) -> dict | None:
        """CE/CWE triplet (code^text^system) starting at component ``offset + 1``."""
        code, text, system = rep.c(offset + 1), rep.c(offset + 2), rep.c(offset + 3)
        if not code:
            return None
        coding = {"code": code}
        if (uri := self.config.coding_system(system)):
            coding["system"] = uri
        if text:
            coding["display"] = text
        return coding

    def concept(self, rep: Rep) -> dict | None:
        codings = [c for c in (self.coding(rep), self.coding(rep, 3)) if c]
        if not codings and not rep.c(2):
            return None
        concept: dict = {}
        if codings:
            concept["coding"] = codings
        if text := (rep.c(9) or rep.c(2) or rep.c(5)):
            concept["text"] = text
        return concept

    def identifier(self, rep: Rep, *, id_type: str | None = None) -> dict | None:
        """CX / XCN-style identifier. For XCN pass ``id_type`` and remap components first."""
        value = rep.c(1)
        if not value:
            return None
        type_code = id_type if id_type is not None else rep.c(5)
        system = self.config.identifier_system(rep.c(4, 1), rep.c(4, 2), rep.c(4, 3),
                                               type_code, rep.c(6, 2) or rep.c(6, 1))
        ident = {"system": system, "value": value}
        if type_code:
            ident["type"] = {"coding": [{"system": vocab.V2_0203, "code": type_code,
                                         **({"display": vocab.IDENTIFIER_TYPE_DISPLAY[type_code]}
                                            if type_code in vocab.IDENTIFIER_TYPE_DISPLAY else {})}]}
        if type_code in ("NI", "SS"):
            ident["use"] = "official"
        return ident

    def bundle(self) -> dict:
        return {"resourceType": "Bundle", "type": "transaction", "entry": self.entries}


def _human_name(rep: Rep, offset: int = 0) -> dict | None:
    """XPN (offset 0) or the name part of an XCN (offset 1)."""
    family = rep.c(offset + 1)
    given = [g for g in (rep.c(offset + 2), rep.c(offset + 3)) if g]
    if not family and not given:
        return None
    name: dict = {}
    if (use := vocab.NAME_USE.get(rep.c(offset + 7 if offset == 0 else 10))):
        name["use"] = use
    if family:
        name["family"] = family.title() if family.isupper() else family
    if given:
        name["given"] = [g.title() if g.isupper() else g for g in given]
    if suffix := rep.c(offset + 4):
        name["suffix"] = [suffix]
    if prefix := rep.c(offset + 5):
        name["prefix"] = [prefix]
    return name


def _address(rep: Rep) -> dict | None:
    lines = [x for x in (rep.c(1), rep.c(2)) if x]
    addr = {k: v for k, v in (("city", rep.c(3)), ("state", rep.c(4)), ("postalCode", rep.c(5)),
                              ("country", rep.c(6))) if v}
    if not lines and not addr:
        return None
    if lines:
        addr = {"line": lines, **addr}
    use, kind = vocab.ADDRESS_USE.get(rep.c(7), (None, None))
    if use:
        addr["use"] = use
    if kind:
        addr["type"] = kind
    return addr


def _telecom(rep: Rep, default_use: str) -> dict | None:
    if rep.c(3) in ("Internet", "X.400") or rep.c(2) == "NET":
        value = rep.c(4) or rep.c(1)
        return {"system": "email", "value": value, "use": default_use} if value else None
    if rep.c(6) and rep.c(7):
        local = re.sub(r"\D", "", rep.c(7))
        value = f"({rep.c(6)}) {local[:3]}-{local[3:]}"
    else:
        value = rep.c(1)
    if not value:
        return None
    use = "mobile" if rep.c(3) == "CP" else default_use
    return {"system": "fax" if rep.c(3) == "FX" else "phone", "value": value, "use": use}


def _us_core_category_ext(url: str, rep: Rep, table: dict[str, str], label: str) -> dict | None:
    code = rep.c(1)
    if not code:
        return None
    text = table.get(code, rep.c(2) or label)
    ext: list[dict] = []
    if code in table:
        ext.append({"url": "ombCategory",
                    "valueCoding": {"system": vocab.CDC_RACE_ETH, "code": code, "display": table[code]}})
    ext.append({"url": "text", "valueString": text})
    return {"url": url, "extension": ext}


class Mapper:
    def __init__(self, config: BridgeConfig | None = None):
        self.config = config or BridgeConfig()
        self.handlers: dict[tuple[str, str], Callable[[_Context], None]] = {
            **{("ADT", ev): self._adt for ev in ("A01", "A02", "A03", "A04", "A08", "A11", "A40")},
            ("ORU", "R01"): self._oru,
            ("MDM", "T02"): self._mdm,
            ("MDM", "T04"): self._mdm,
        }
        # Locally defined Z-segments. VistA feeds carry site-specific Z-segments; each one
        # gets an explicit handler rather than being silently dropped.
        self.z_handlers: dict[str, Callable[[_Context, Segment, dict], None]] = {"ZEL": self._zel}

    @property
    def supported(self) -> list[str]:
        return sorted(f"{m}^{e}" for m, e in self.handlers)

    def map(self, msg: Message) -> MappingResult:
        handler = self.handlers.get((msg.message_code, msg.trigger_event))
        if handler is None:
            known = any(m == msg.message_code for m, _ in self.handlers)
            raise MappingError(f"{msg.type_label} is not supported by this interface",
                               code="201" if known else "200", location="MSH-9")
        ctx = _Context(msg, self.config)
        handler(ctx)
        ctx.add(self._provenance(ctx), None, mode="create")
        return MappingResult(ctx.bundle(), ctx.warnings)

    # -- message handlers -------------------------------------------------
    def _adt(self, ctx: _Context) -> None:
        pid = ctx.require("PID")
        trigger = ctx.msg.trigger_event
        # An MPI merge carries only identifying PID fields, so it must not replace demographics.
        patient = self._patient(ctx, pid, mode="create-if-absent" if trigger == "A40" else "upsert")
        if trigger == "A40":
            self._merge(ctx, patient)
            return
        if (pv1 := ctx.msg.segment("PV1")) is not None and pv1.get(2) not in ("", "N"):
            self._encounter(ctx, pv1, patient, trigger)
        for al1 in ctx.msg.all("AL1"):
            self._allergy(ctx, al1, patient)

    def _oru(self, ctx: _Context) -> None:
        pid = ctx.require("PID")
        # Lab systems send thin demographics; never let them overwrite the registration record.
        patient = self._patient(ctx, pid, mode="create-if-absent")
        groups = list(ctx.msg.groups("OBR", {"OBX", "NTE"}))
        if not groups:
            raise MappingError("ORU^R01 contains no OBR order group", code="100", location="OBR")
        for obr, children in groups:
            self._report(ctx, obr, children, patient)

    def _mdm(self, ctx: _Context) -> None:
        pid = ctx.require("PID")
        txa = ctx.require("TXA")
        patient = self._patient(ctx, pid, mode="create-if-absent")
        lines = [r.c(1) for obx in ctx.msg.all("OBX") for r in obx.reps(5)]
        text = "\n".join(lines).strip()
        if not text:
            raise MappingError("MDM document has no OBX-5 text content", code="101", location="OBX-5")
        doc_type = txa.get(2)
        loinc, display = DOCUMENT_TYPES.get(doc_type, ("34109-9", "Note"))
        status = txa.get(17)
        doc: dict = {
            "resourceType": "DocumentReference",
            "meta": {"profile": [f"{vocab.US_CORE}/us-core-documentreference"]},
            "status": "current",
            "docStatus": "final" if status in ("AU", "LA") else "preliminary",
            "type": {"coding": [{"system": "http://loinc.org", "code": loinc, "display": display}],
                     "text": txa.get(2, 2) or display},
            "category": [{"coding": [{
                "system": "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
                "code": "clinical-note", "display": "Clinical Note"}]}],
            "subject": {"reference": patient},
            "content": [{"attachment": {
                "contentType": "text/plain; charset=utf-8",
                "data": base64.b64encode(text.encode()).decode(),
                "title": txa.get(2, 2) or display}}],
        }
        if when := ctx.dt(txa.get(4) or txa.get(6), "TXA-4"):
            if "T" in when:  # DocumentReference.date is an instant
                doc["date"] = when
            doc["context"] = {"period": {"start": when}}
        if (author := self._practitioner(ctx, txa.rep(5), txa)):
            doc["author"] = [{"reference": author}]
        ident = None
        if txa.get(12):
            ident = {"system": ctx.config.local_code_system(f"tiu-{ctx.msg.sending_facility}"),
                     "value": txa.get(12)}
            doc["identifier"] = [ident]
        ctx.add(doc, txa, identifier=ident)

    # -- resource builders ------------------------------------------------
    def _patient(self, ctx: _Context, pid: Segment, mode: str = "upsert") -> str:
        identifiers = [i for r in pid.reps(3) if (i := ctx.identifier(r))]
        if pid.get(19):
            identifiers.append({"system": vocab.US_SSN, "value": pid.get(19)})
        if ctx.config.ssn_policy != "include":
            if any(i["system"] == vocab.US_SSN for i in identifiers):
                ctx.warnings.append("SSN removed at boundary (ssn_policy=omit)")
            identifiers = [i for i in identifiers if i["system"] != vocab.US_SSN]
        if not identifiers:
            raise MappingError("PID-3 has no usable patient identifier", code="101", location="PID-3")
        patient: dict = {
            "resourceType": "Patient",
            "meta": {"profile": [f"{vocab.US_CORE}/us-core-patient"]},
            "identifier": identifiers,
            "active": True,
        }
        if names := [n for r in pid.reps(5) if (n := _human_name(r))]:
            patient["name"] = names
        if gender := vocab.GENDER.get(pid.get(8)):
            patient["gender"] = gender
        elif pid.get(8):
            raise MappingError(f"PID-8 value {pid.get(8)!r} not in table 0001", code="103", location="PID-8")
        if pid.get(7):
            try:
                patient["birthDate"] = vocab.to_fhir_date(pid.get(7))
            except ValueError as exc:
                raise MappingError(str(exc), code="102", location="PID-7") from exc
        telecom = [t for r in pid.reps(13) if (t := _telecom(r, "home"))]
        telecom += [t for r in pid.reps(14) if (t := _telecom(r, "work"))]
        if telecom:
            patient["telecom"] = telecom
        if addresses := [a for r in pid.reps(11) if (a := _address(r))]:
            patient["address"] = addresses
        if (marital := vocab.MARITAL.get(pid.get(16))):
            patient["maritalStatus"] = {"coding": [{"system": vocab.V3_MARITAL, "code": marital[0],
                                                    "display": marital[1]}]}
        if pid.get(30) == "Y" or pid.get(29):
            patient["deceasedDateTime" if pid.get(29) else "deceasedBoolean"] = (
                ctx.dt(pid.get(29), "PID-29") if pid.get(29) else True)
        ext = [e for e in (
            _us_core_category_ext(f"{vocab.US_CORE}/us-core-race", pid.rep(10), vocab.OMB_RACE, "Unknown"),
            _us_core_category_ext(f"{vocab.US_CORE}/us-core-ethnicity", pid.rep(22),
                                  vocab.OMB_ETHNICITY, "Unknown"),
        ) if e]
        for seg in ctx.msg.segments:
            if seg.name.startswith("Z"):
                if (handler := self.z_handlers.get(seg.name)):
                    handler(ctx, seg, patient)
                else:
                    ctx.warnings.append(f"unhandled Z-segment {seg.name} ignored")
        if ext:
            patient["extension"] = ext + patient.get("extension", [])
        key = next((i for i in identifiers if i["system"] == ctx.config.icn_system), identifiers[0])
        return ctx.add(patient, pid, identifier={"system": key["system"], "value": key["value"]}, mode=mode)

    def _zel(self, ctx: _Context, seg: Segment, patient: dict) -> None:
        """Pilot-defined eligibility segment: ZEL|set|eligibility^text^sys|SC%|veteran Y/N|period."""
        base = ctx.config.extension_base
        ext = patient.setdefault("extension", [])
        if seg.get(4) in ("Y", "N"):
            ext.append({"url": f"{base}/veteran-status", "valueBoolean": seg.get(4) == "Y"})
        if (elig := ctx.concept(seg.rep(2))):
            ext.append({"url": f"{base}/va-eligibility", "valueCodeableConcept": elig})
        if seg.get(3):
            ext.append({"url": f"{base}/service-connected-percent", "valueInteger": int(seg.get(3))})
        if (period := ctx.concept(seg.rep(5))):
            ext.append({"url": f"{base}/period-of-service", "valueCodeableConcept": period})

    def _practitioner(self, ctx: _Context, rep: Rep, seg: Segment) -> str | None:
        if not rep:
            return None
        # XCN: 1 id, 2-7 name, 9 assigning authority, 13 identifier type
        id_type = rep.c(13) or ("NPI" if rep.c(9) == "NPI" else "PRN")
        cx = Rep([[rep.c(1)], [], [], rep.parts[8] if len(rep.parts) > 8 else [""], [id_type]])
        practitioner: dict = {"resourceType": "Practitioner",
                              "meta": {"profile": [f"{vocab.US_CORE}/us-core-practitioner"]}}
        ident = ctx.identifier(cx, id_type=id_type)
        if ident:
            practitioner["identifier"] = [ident]
        if (name := _human_name(rep, offset=1)):
            practitioner["name"] = [name]
        if not ident and "name" not in practitioner:
            return None
        return ctx.add(practitioner, seg, identifier=ident)

    def _encounter(self, ctx: _Context, pv1: Segment, patient: str, trigger: str) -> None:
        klass = pv1.get(2)
        code, display = vocab.PATIENT_CLASS.get(klass, (None, None))
        if code is None:
            raise MappingError(f"PV1-2 patient class {klass!r} not in table 0004",
                               code="103", location="PV1-2")
        start, end = ctx.dt(pv1.get(44), "PV1-44"), ctx.dt(pv1.get(45), "PV1-45")
        status = {"A01": "in-progress", "A02": "in-progress", "A03": "finished",
                  "A04": "arrived", "A11": "cancelled"}.get(trigger, "finished" if end else "in-progress")
        enc: dict = {
            "resourceType": "Encounter",
            "meta": {"profile": [f"{vocab.US_CORE}/us-core-encounter"]},
            "status": status,
            "class": {"system": vocab.V3_ACT_CODE, "code": code, "display": display},
            "type": [{"text": pv1.get(10) or display}],
            "subject": {"reference": patient},
        }
        if start or end:
            enc["period"] = {k: v for k, v in (("start", start), ("end", end)) if v}
        locations = []
        if (loc := pv1.rep(3)):
            label = " ".join(x for x in (loc.c(1), loc.c(2), loc.c(3)) if x)
            locations.append({"location": {"display": label},
                              "status": "completed" if status == "finished" else "active"})
        if trigger == "A02" and (prior := pv1.rep(6)):
            label = " ".join(x for x in (prior.c(1), prior.c(2), prior.c(3)) if x)
            locations.append({"location": {"display": label}, "status": "completed"})
        if locations:
            enc["location"] = locations
        participants = []
        for field_no, role, role_display in ((7, "ATND", "attender"), (17, "ADM", "admitter"),
                                             (8, "REF", "referrer")):
            if (ref := self._practitioner(ctx, pv1.rep(field_no), pv1)):
                participants.append({"type": [{"coding": [{"system": vocab.V3_PARTICIPATION,
                                                            "code": role, "display": role_display}]}],
                                     "individual": {"reference": ref}})
        if participants:
            enc["participant"] = participants
        if (pv2 := ctx.msg.segment("PV2")) and (reason := ctx.concept(pv2.rep(3))):
            enc["reasonCode"] = [reason]
        if (dispo := pv1.get(36)):
            code_, disp = vocab.DISCHARGE_DISPOSITION.get(dispo, (None, None))
            enc.setdefault("hospitalization", {})["dischargeDisposition"] = (
                {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/discharge-disposition",
                             "code": code_, "display": disp}]} if code_ else {"text": dispo})
        if ctx.msg.sending_facility:
            enc["serviceProvider"] = {"display": ctx.msg.sending_facility}
        ident = None
        if (visit := pv1.rep(19)):
            ident = ctx.identifier(visit, id_type=visit.c(5) or "VN")
            enc["identifier"] = [ident]
        ctx.add(enc, pv1, identifier=ident and {"system": ident["system"], "value": ident["value"]})

    def _allergy(self, ctx: _Context, al1: Segment, patient: str) -> None:
        code = ctx.concept(al1.rep(3))
        if code is None:
            ctx.warnings.append(f"{ctx.loc(al1)} has no allergen code; skipped")
            return
        allergy: dict = {
            "resourceType": "AllergyIntolerance",
            "meta": {"profile": [f"{vocab.US_CORE}/us-core-allergyintolerance"]},
            "clinicalStatus": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                "code": "active"}]},
            "verificationStatus": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification",
                "code": "confirmed"}]},
            "code": code,
            "patient": {"reference": patient},
        }
        if (cat := vocab.ALLERGY_CATEGORY.get(al1.get(2))):
            allergy["category"] = [cat]
        reactions = [r.c(1) for r in al1.reps(5) if r.c(1)]
        if reactions or al1.get(4):
            reaction: dict = {"manifestation": [{"text": r} for r in reactions] or [{"text": "unspecified"}]}
            if (sev := vocab.ALLERGY_SEVERITY.get(al1.get(4))):
                reaction["severity"] = sev
            allergy["reaction"] = [reaction]
        if al1.get(6):
            allergy["onsetDateTime"] = ctx.dt(al1.get(6), "AL1-6")
        ctx.add(allergy, al1, mode="create")

    def _merge(self, ctx: _Context, survivor: str) -> None:
        mrg = ctx.require("MRG")
        prior = ctx.identifier(mrg.rep(1))
        if prior is None:
            raise MappingError("MRG-1 prior patient identifier is empty", code="101", location="MRG-1")
        query = f"Patient?identifier={prior['system']}|{prior['value']}"

        def op(kind: str, path: str, **parts) -> dict:
            body = [{"name": "type", "valueCode": kind}, {"name": "path", "valueString": path}]
            return {"name": "operation", "part": body + [{"name": k, **v} for k, v in parts.items()]}

        patch = {"resourceType": "Parameters", "parameter": [
            op("replace", "Patient.active", value={"valueBoolean": False}),
            op("add", "Patient", name={"valueString": "link"}, value={"part": [
                {"name": "other", "valueReference": {"reference": survivor}},
                {"name": "type", "valueCode": "replaced-by"}]}),
        ]}
        ctx.add(patch, mrg, mode="patch", patch_query=query)
        # Only lands if the survivor is new; an existing record keeps its demographics untouched.
        survivor_entry = next(e for e in ctx.entries if e["fullUrl"] == survivor)
        prior_ref = {"identifier": {"system": prior["system"], "value": prior["value"]}}
        survivor_entry["resource"]["link"] = [{"other": prior_ref, "type": "replaces"}]

    def _report(self, ctx: _Context, obr: Segment, children: list[Segment], patient: str) -> None:
        filler = obr.rep(3) or obr.rep(2)
        if not filler:
            raise MappingError("OBR-3 filler order number is required", code="101", location="OBR-3")
        order_system = ctx.config.local_code_system(f"order-{filler.c(2) or ctx.msg.sending_facility}")
        code = ctx.concept(obr.rep(4))
        if code is None:
            raise MappingError("OBR-4 universal service identifier is required", code="101", location="OBR-4")
        effective = ctx.dt(obr.get(7), "OBR-7")
        lab_cat = {"coding": [{"system": vocab.OBS_CATEGORY, "code": "laboratory", "display": "Laboratory"}]}
        results, notes = [], []
        seen_obx = False
        for seg in children:
            if seg.name == "NTE" and not seen_obx:  # NTEs after an OBX belong to that OBX
                notes.extend(r.c(1) for r in seg.reps(3) if r.c(1))
            elif seg.name == "OBX":
                seen_obx = True
                ref = self._observation(ctx, seg, patient, order_system, filler.c(1), effective, lab_cat)
                if ref:
                    results.append({"reference": ref})
        report: dict = {
            "resourceType": "DiagnosticReport",
            "meta": {"profile": [f"{vocab.US_CORE}/us-core-diagnosticreport-lab"]},
            "identifier": [{"system": order_system, "value": filler.c(1),
                            "type": {"coding": [{"system": vocab.V2_0203, "code": "FILL",
                                                 "display": "Filler Identifier"}]}}],
            "status": vocab.REPORT_STATUS.get(obr.get(25), "final"),
            "category": [{"coding": [{"system": vocab.V2_0074, "code": "LAB", "display": "Laboratory"}]}],
            "code": code,
            "subject": {"reference": patient},
            "result": results,
        }
        if effective:
            report["effectiveDateTime"] = effective
        if (issued := ctx.dt(obr.get(22), "OBR-22")) and "T" in issued:
            report["issued"] = issued
        if (orderer := self._practitioner(ctx, obr.rep(16), obr)):
            report["resultsInterpreter"] = [{"reference": orderer}]
        if notes:
            report["conclusion"] = " ".join(notes)
        ctx.add(report, obr, identifier={"system": order_system, "value": filler.c(1)})

    def _observation(self, ctx: _Context, obx: Segment, patient: str, order_system: str,
                     filler: str, effective: str | None, category: dict) -> str | None:
        where = ctx.loc(obx)
        code = ctx.concept(obx.rep(3))
        if code is None:
            raise MappingError(f"{where} OBX-3 observation identifier is required",
                               code="101", location=f"{where}-3")
        obs: dict = {
            "resourceType": "Observation",
            "meta": {"profile": [f"{vocab.US_CORE}/us-core-observation-lab"]},
            "status": vocab.OBSERVATION_STATUS.get(obx.get(11), "final"),
            "category": [category],
            "code": code,
            "subject": {"reference": patient},
        }
        if (when := ctx.dt(obx.get(14), f"{where}-14") or effective):
            obs["effectiveDateTime"] = when
        self._value(ctx, obx, obs, where)
        if (flag := obx.get(8)) in vocab.INTERPRETATION:
            obs["interpretation"] = [{"coding": [{"system": vocab.V3_INTERPRETATION, "code": flag,
                                                  "display": vocab.INTERPRETATION[flag]}]}]
        if (rng := obx.get(7)):
            obs["referenceRange"] = [self._range(rng, obx)]
        if (notes := [r.c(1) for n in ctx.msg.following(obx, {"NTE"}) for r in n.reps(3) if r.c(1)]):
            obs["note"] = [{"text": " ".join(notes)}]
        obs_key = f"{filler}-{code['coding'][0]['code'] if 'coding' in code else obx.get(1)}"
        if obx.get(4):
            obs_key += f"-{obx.get(4)}"
        obs["identifier"] = [{"system": order_system, "value": obs_key}]
        return ctx.add(obs, obx, identifier={"system": order_system, "value": obs_key})

    def _quantity(self, value: int | float, obx: Segment) -> dict:
        q: dict = {"value": value}
        unit = obx.rep(6)
        if unit.c(1):
            q["unit"] = unit.c(2) or unit.c(1)
            if unit.c(3).upper() in ("", "UCUM", "ANS+"):
                q["system"], q["code"] = vocab.UCUM, unit.c(1)
        return q

    def _value(self, ctx: _Context, obx: Segment, obs: dict, where: str) -> None:
        vtype, reps = obx.get(2), obx.reps(5)
        if not reps or not reps[0]:
            obs["dataAbsentReason"] = {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/data-absent-reason", "code": "unknown"}]}
            return
        first = reps[0]
        try:
            if vtype == "NM":
                obs["valueQuantity"] = self._quantity(vocab.number(first.c(1)), obx)
            elif vtype == "SN":
                comparator, n1, sep, n2 = first.c(1), first.c(2), first.c(3), first.c(4)
                if sep in (":", "/") and n2:
                    obs["valueRatio"] = {"numerator": {"value": vocab.number(n1)},
                                         "denominator": {"value": vocab.number(n2)}}
                elif sep == "-" and n2:
                    obs["valueRange"] = {"low": self._quantity(vocab.number(n1), obx),
                                         "high": self._quantity(vocab.number(n2), obx)}
                else:
                    q = self._quantity(vocab.number(n1), obx)
                    if comparator in ("<", "<=", ">", ">="):
                        q = {"value": q.pop("value"), "comparator": comparator, **q}
                    obs["valueQuantity"] = q
            elif vtype in ("CE", "CWE", "CNE"):
                obs["valueCodeableConcept"] = ctx.concept(first)
            elif vtype in ("DT", "DTM", "TS"):
                obs["valueDateTime"] = ctx.dt(first.c(1), f"{where}-5")
            else:
                if vtype not in ("ST", "TX", "FT"):
                    ctx.warnings.append(f"{where}: value type {vtype!r} mapped as string")
                obs["valueString"] = "\n".join(r.c(1) for r in reps)
        except ValueError as exc:
            raise MappingError(f"{where} OBX-5 is not a valid {vtype}: {exc}",
                               code="102", location=f"{where}-5") from exc

    def _range(self, text: str, obx: Segment) -> dict:
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*", text)
        if m:
            return {"low": self._quantity(vocab.number(m[1]), obx),
                    "high": self._quantity(vocab.number(m[2]), obx), "text": text}
        return {"text": text}

    def _provenance(self, ctx: _Context) -> dict:
        msg = ctx.msg
        try:
            recorded = vocab.to_fhir_datetime(msg.msh.get(7), ctx.config.default_tz)
        except ValueError:
            recorded = None
        if not recorded or "T" not in recorded:
            recorded = datetime.now(timezone.utc).isoformat(timespec="seconds")
        targets = [{"reference": e["fullUrl"]} for e in ctx.entries if e["request"]["method"] != "PATCH"]
        return {
            "resourceType": "Provenance",
            "meta": {"profile": [f"{vocab.US_CORE}/us-core-provenance"], "source": ctx.source_base},
            "target": targets,
            "recorded": recorded,
            "activity": {"coding": [{"system": vocab.V3_DATA_OPERATION, "code": "UPDATE",
                                     "display": "revise"}]},
            "agent": [{
                "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                     "code": "transmitter", "display": "Transmitter"}]},
                "who": {"display": f"{msg.sending_app}@{msg.sending_facility}"},
            }],
            "entity": [{"role": "source", "what": {
                "identifier": {"system": "urn:hl7v2:msh-10", "value": msg.control_id},
                "display": f"HL7 v{msg.version} {msg.type_label} control id {msg.control_id}"}}],
        }
