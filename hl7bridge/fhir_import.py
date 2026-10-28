"""Import a patient's record from an external FHIR R4 server (Epic, Oracle Health, the SMART
sandbox) into the pilot store, with the same guarantees as the HL7 v2 and C-CDA paths:

* lineage: ``meta.source = urn:fhir:<host>#<Type>/<id>`` on every resource, so a citation
  resolves to the exact resource on the source system;
* idempotent: each resource is a conditional upsert keyed on its source id, so re-importing
  (a chart re-opened, a nightly refresh) updates rather than duplicates;
* references are rewritten to the imported copies; references to resources that weren't
  imported become logical (identifier) references instead of dangling local ids;
* minimum necessary: SSN, driver's license and passport identifiers are dropped;
* 38 USC 7332 labels applied, invalid resources skipped with a warning, one ledger entry and a
  Provenance resource per import.
"""

from __future__ import annotations

import copy
import gzip
import json
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import vocab
from .config import BridgeConfig
from .sensitivity import LABELS, label_bundle
from .store import FhirStore, StoreError
from .validate import validate_resource

_NS = uuid.UUID("2f0b6f5e-87a9-4c55-9a0e-6d1f3c2b8a41")
_DROP_ID_TYPES = {"SS", "DL", "PPN", "SB"}
DATA_QUALITY = "https://fhir.pilot.example/CodeSystem/data-quality"


def source_system(base: str) -> str:
    return f"urn:fhir-source:{urllib.parse.urlsplit(base).netloc}"


def _walk_refs(node, fn) -> None:
    """Replace every Reference (a dict with a string ``reference``) in place via ``fn``."""
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for key, value in list(items):
        if isinstance(value, dict) and isinstance(value.get("reference"), str):
            node[key] = fn(value)
        elif isinstance(value, (dict, list)):
            _walk_refs(value, fn)


def build_bundle(resources: list[dict], base: str, config: BridgeConfig | None = None) -> tuple[dict, list[str]]:
    config = config or BridgeConfig()
    base = base.rstrip("/")
    host = urllib.parse.urlsplit(base).netloc
    system = source_system(base)
    warnings: list[str] = []
    keys = {}
    for r in resources:
        if r.get("id"):
            ref = f"{r['resourceType']}/{r['id']}"
            keys[ref] = f"urn:uuid:{uuid.uuid5(_NS, host + '/' + ref)}"

    def rewrite(ref: dict) -> dict:
        target = ref["reference"]
        if target.startswith(base + "/"):
            target = target[len(base) + 1:]
        if target in keys:
            return {**ref, "reference": keys[target]}
        if "/" in target and not target.startswith("urn:"):
            rtype, _, rid = target.rpartition("/")
            out = {"identifier": {"system": f"{system}/{rtype.rsplit('/', 1)[-1]}", "value": rid}}
            if ref.get("display"):
                out["display"] = ref["display"]
            return out
        return {k: v for k, v in ref.items() if k != "reference"} or {"display": target}

    entries, skipped, nonconformant = [], 0, {}
    for original in resources:
        rtype, rid = original.get("resourceType"), original.get("id")
        if not rtype or not rid:
            continue
        res = copy.deepcopy(original)
        res.pop("id", None)
        res.pop("text", None)  # narrative HTML is presentation, and may restate PHI we filtered
        meta = {k: v for k, v in res.get("meta", {}).items() if k in ("profile", "security")}
        meta["source"] = f"urn:fhir:{host}#{rtype}/{rid}"
        res["meta"] = meta
        idents = res.get("identifier", [])
        if rtype == "Patient":
            kept = [i for i in idents if i.get("system") != vocab.US_SSN and not any(
                c.get("code") in _DROP_ID_TYPES for c in (i.get("type") or {}).get("coding", []))]
            if len(kept) < len(idents):
                warnings.append(f"Patient/{rid}: {len(idents) - len(kept)} identifier(s) dropped (SSN, license, passport)")
            key = {"system": system, "value": rid}
        else:
            kept = list(idents)
            key = {"system": f"{system}/{rtype}", "value": rid}
        res["identifier"] = [key] + [i for i in kept if i != key]
        _walk_refs(res, rewrite)
        issues = [i for i in validate_resource(res, f"{rtype}/{rid}") if i["severity"] == "error"]
        if issues:
            # A certified EHR's data is kept even when it misses a US Core element: dropping it would
            # silently lose clinical facts. It is tagged so the gap is visible and countable.
            # Only resources nothing can use (no subject/patient, no code) are skipped.
            unusable = [i for i in issues if i["expression"][0].rsplit(".", 1)[-1] in ("subject", "patient", "code")]
            if unusable:
                skipped += 1
                warnings.append(f"{rtype}/{rid} skipped: {unusable[0]['diagnostics']}")
                continue
            nonconformant[rtype] = nonconformant.get(rtype, 0) + 1
            meta.setdefault("tag", []).append({"system": DATA_QUALITY, "code": "us-core-nonconformant",
                                               "display": "; ".join(i["diagnostics"] for i in issues)})
        entries.append({"fullUrl": keys[f"{rtype}/{rid}"], "resource": res,
                        "request": {"method": "PUT", "url": f"{rtype}?identifier={key['system']}|{key['value']}"}})
    for rtype, n in sorted(nonconformant.items()):
        warnings.append(f"{n} {rtype} resource(s) kept but tagged us-core-nonconformant")
    if skipped:
        warnings.append(f"{skipped} unusable resource(s) skipped (no subject or code)")
    entries.append({"fullUrl": f"urn:uuid:{uuid.uuid4()}", "request": {"method": "POST", "url": "Provenance"},
                    "resource": {
                        "resourceType": "Provenance",
                        "meta": {"source": f"urn:fhir:{host}"},
                        "target": [{"reference": e["fullUrl"]} for e in entries],
                        "recorded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "agent": [{"who": {"display": f"FHIR R4 API {host}"}}],
                        "entity": [{"role": "source", "what": {"identifier": {"system": "urn:ietf:rfc:3986", "value": base}}}],
                    }})
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": entries}
    for code, n in label_bundle(bundle).items():
        warnings.append(f"{n} resource(s) labeled {code}: {LABELS[code]}")
    return bundle, warnings


def import_resources(store: FhirStore, resources: list[dict], base: str, *, config: BridgeConfig | None = None,
                     on_commit=None) -> dict:
    t0 = time.perf_counter()
    patient = next((r for r in resources if r.get("resourceType") == "Patient"), None)
    if patient is None:
        raise ValueError("record has no Patient resource")
    host = urllib.parse.urlsplit(base).netloc
    control_id = f"fhir:{host}:Patient/{patient['id']}:{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}"
    bundle, warnings = build_bundle(resources, base, config)
    try:
        response = store.transaction(bundle)
        status = "accepted"
    except StoreError as exc:
        response, status = None, "error"
        warnings.append(str(exc))
    refs = [e["response"]["location"].rsplit("/_history", 1)[0] for e in (response or {}).get("entry", [])]
    elapsed = round((time.perf_counter() - t0) * 1000, 2)
    counts: dict[str, int] = {}
    for e in bundle["entry"]:
        counts[e["resource"]["resourceType"]] = counts.get(e["resource"]["resourceType"], 0) + 1
    store.log_message(control_id=control_id, sender=f"FHIR@{host}", message_type="FHIR R4 import", status=status,
                      ack=f"{status.upper()} {len(refs)} resources", error=None if status == "accepted" else warnings[-1],
                      raw_redacted=json.dumps({"source": base, "patient": patient["id"], "counts": counts}, indent=1),
                      resources=json.dumps(refs), elapsed_ms=elapsed)
    if refs and on_commit:
        on_commit(refs)
    local_patient = next((r.split("/", 1)[1] for r in refs if r.startswith("Patient/")), None)
    return {"status": status, "control_id": control_id, "patient": local_patient,
            "patient_identifier": f"{source_system(base)}|{patient['id']}", "counts": counts,
            "warnings": warnings, "elapsed_ms": elapsed}


# -- fixtures: a fetched record saved for offline, deterministic use ---------------------------
def save_fixture(path: str | Path, base: str, resources: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"source_base": base, "resources": resources}, fh, separators=(",", ":"))


def load_fixture(path: str | Path) -> tuple[str, list[dict]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    return data["source_base"], data["resources"]
