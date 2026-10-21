"""Inbound message pipeline: parse -> dedupe -> map -> validate -> commit -> acknowledge.

Every outcome produces an HL7 ACK: AA when committed, AE when the message was understood
but could not be applied, AR when it was rejected outright (unparseable/unsupported).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from .config import BridgeConfig
from .hl7v2 import HL7ParseError, Message, build_ack
from .mapper import Mapper, MappingError
from .store import FhirStore, StoreError
from .validate import has_errors, validate_bundle

log = logging.getLogger("hl7bridge.pipeline")

_STORE_ERROR_CODES = {404: "204", 409: "205", 412: "205"}


@dataclass
class ProcessResult:
    status: str  # accepted | duplicate | error | rejected
    ack: str
    control_id: str | None = None
    message_type: str | None = None
    bundle: dict | None = None
    response: dict | None = None
    outcome: dict | None = None
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def redact(msg: Message) -> str:
    """ER7 for the ledger with direct identifiers that aren't needed for replay removed."""
    copy = Message.parse(msg.to_er7())
    for pid in copy.all("PID"):
        pid.set_field(19, [])  # SSN
        pid.set_field(3, [rep for rep in pid.fields[3] if len(rep) < 5 or rep[4][0] != "SS"]
                      if len(pid.fields) > 3 else [])
        for n in (11, 13, 14):  # address, phones
            if len(pid.fields) > n and pid.fields[n]:
                pid.set_field(n, [[["***"]]])
    return copy.to_er7("\n")


class Pipeline:
    def __init__(self, store: FhirStore, config: BridgeConfig | None = None, on_commit=None):
        self.store = store
        self.config = config or BridgeConfig()
        self.mapper = Mapper(self.config)
        self.on_commit = on_commit  # callback(list[dict]) after resources are written

    def _ack(self, msg: Message | None, code: str, text: str, err: str | None = None) -> str:
        return build_ack(msg, code, text, err, app=self.config.receiving_app,
                         facility=self.config.receiving_facility).to_er7()

    def convert(self, raw: str) -> ProcessResult:
        """Map and validate without persisting (used by the dry-run API and CLI)."""
        return self.process(raw, persist=False)

    def process(self, raw: str, *, persist: bool = True) -> ProcessResult:
        t0 = time.perf_counter()
        result = self._process(raw, persist, t0)
        result.elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        # Log identifiers and outcomes only; message content is PHI and never hits the log.
        log.info("hl7 %s %s -> %s (%.1f ms)", result.message_type, result.control_id,
                 result.status, result.elapsed_ms)
        return result

    def _process(self, raw: str, persist: bool, t0: float) -> ProcessResult:
        try:
            msg = Message.parse(raw)
        except HL7ParseError as exc:
            return ProcessResult("rejected", self._ack(None, "AR", str(exc), "100"),
                                 outcome=_outcome("structure", str(exc)))
        ctrl, mtype = msg.control_id, msg.type_label

        def done(status: str, ack: str, **kw) -> ProcessResult:
            res = ProcessResult(status, ack, ctrl, mtype, **kw)
            if persist and ctrl and status != "duplicate":
                refs = [e["response"]["location"].rsplit("/_history", 1)[0]
                        for e in (res.response or {}).get("entry", [])]
                err = None if status == "accepted" else _first_issue(res.outcome)
                self.store.log_message(
                    control_id=ctrl, sender=f"{msg.sending_app}@{msg.sending_facility}",
                    message_type=mtype, status=status, ack=ack, error=err,
                    raw_redacted=redact(msg), resources=json.dumps(refs),
                    elapsed_ms=round((time.perf_counter() - t0) * 1000, 2))
            return res

        if not ctrl:
            return done("rejected", self._ack(msg, "AR", "MSH-10 message control id is required", "101"),
                        outcome=_outcome("required", "MSH-10 message control id is required"))
        if msg.version not in self.config.supported_versions:
            text = f"HL7 version {msg.version!r} is not supported"
            return done("rejected", self._ack(msg, "AR", text, "203"), outcome=_outcome("not-supported", text))
        if persist and (prior := self.store.get_message(ctrl)) and prior["status"] == "accepted":
            # At-least-once senders resend on timeout; replay the original ACK, write nothing.
            return done("duplicate", prior["ack"], outcome=_outcome(
                "duplicate", f"control id {ctrl} already processed", severity="information"))
        try:
            mapped = self.mapper.map(msg)
        except MappingError as exc:
            code = "AR" if exc.code in ("200", "201") else "AE"
            status = "rejected" if code == "AR" else "error"
            return done(status, self._ack(msg, code, str(exc), exc.code),
                        outcome=_outcome("processing", str(exc), exc.location))
        outcome = validate_bundle(mapped.bundle)
        if has_errors(outcome):
            text = _first_issue(outcome)
            return done("error", self._ack(msg, "AE", text, "101"), bundle=mapped.bundle,
                        outcome=outcome, warnings=mapped.warnings)
        if not persist:
            return ProcessResult("accepted", self._ack(msg, "AA", "Validated (dry run)"), ctrl, mtype,
                                 bundle=mapped.bundle, outcome=outcome, warnings=mapped.warnings)
        try:
            response = self.store.transaction(mapped.bundle)
        except StoreError as exc:
            code = _STORE_ERROR_CODES.get(exc.status, "207")
            return done("error", self._ack(msg, "AE", str(exc), code), bundle=mapped.bundle,
                        outcome=_outcome(exc.code, str(exc)), warnings=mapped.warnings)
        written = len(response["entry"])
        result = done("accepted", self._ack(msg, "AA", f"Accepted; {written} FHIR resources committed"),
                      bundle=mapped.bundle, response=response, outcome=outcome, warnings=mapped.warnings)
        if self.on_commit:
            self.on_commit([e["response"]["location"].rsplit("/_history", 1)[0] for e in response["entry"]])
        return result


def _outcome(code: str, text: str, location: str = "", severity: str = "error") -> dict:
    issue = {"severity": severity, "code": code, "diagnostics": text}
    if location:
        issue["location"] = [location]
    return {"resourceType": "OperationOutcome", "issue": [issue]}


def _first_issue(outcome: dict | None) -> str | None:
    for issue in (outcome or {}).get("issue", []):
        if issue["severity"] in ("error", "fatal"):
            return issue.get("diagnostics")
    return None
