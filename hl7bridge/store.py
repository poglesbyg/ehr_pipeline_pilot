"""SQLite-backed FHIR R4 store: versioned resources, atomic transactions, conditional
create/update/patch, a small search API, and the inbound message ledger."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
  type TEXT, id TEXT, version INTEGER, last_updated TEXT, body TEXT, PRIMARY KEY (type, id));
CREATE TABLE IF NOT EXISTS history (
  type TEXT, id TEXT, version INTEGER, last_updated TEXT, body TEXT, PRIMARY KEY (type, id, version));
CREATE TABLE IF NOT EXISTS idx (type TEXT, id TEXT, param TEXT, system TEXT, value TEXT);
CREATE INDEX IF NOT EXISTS idx_lookup ON idx (type, param, value);
CREATE INDEX IF NOT EXISTS idx_owner ON idx (type, id);
CREATE TABLE IF NOT EXISTS messages (
  control_id TEXT PRIMARY KEY, received_at TEXT, sender TEXT, message_type TEXT, status TEXT,
  ack TEXT, error TEXT, raw_redacted TEXT, resources TEXT, elapsed_ms REAL);
"""


class StoreError(Exception):
    def __init__(self, status: int, message: str, code: str = "processing"):
        super().__init__(message)
        self.status = status
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _walk_refs(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        if isinstance(node.get("reference"), str):
            yield node
        for v in node.values():
            yield from _walk_refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_refs(v)


def _index_rows(res: dict) -> list[tuple[str, str | None, str]]:
    rows: list[tuple[str, str | None, str]] = []
    for ident in res.get("identifier", []):
        if ident.get("value"):
            rows.append(("identifier", ident.get("system"), ident["value"]))
    for coding in (res.get("code") or {}).get("coding", []):
        rows.append(("code", coding.get("system"), coding.get("code", "")))
    for coding in (res.get("type") or {}).get("coding", []) if isinstance(res.get("type"), dict) else []:
        rows.append(("type", coding.get("system"), coding.get("code", "")))
    for field in ("subject", "patient"):
        ref = (res.get(field) or {}).get("reference", "")
        if ref.startswith("Patient/"):
            rows.append(("patient", None, ref))
    if res.get("resourceType") == "Patient":
        rows.append(("patient", None, f"Patient/{res['id']}"))
        if res.get("gender"):
            rows.append(("gender", None, res["gender"]))
        for name in res.get("name", []):
            if name.get("family"):
                rows.append(("family", None, name["family"].lower()))
            for given in name.get("given", []):
                rows.append(("given", None, given.lower()))
    for target in res.get("target", []):
        rows.append(("target", None, target.get("reference", "")))
    for cat in res.get("category", []):
        for coding in cat.get("coding", []) if isinstance(cat, dict) else []:
            rows.append(("category", coding.get("system"), coding.get("code", "")))
    if (src := res.get("meta", {}).get("source")):
        rows.append(("_source", None, src.split("#")[0]))
    return rows


def _patch(resource: dict, params: dict) -> dict:
    """Apply the FHIRPath Patch subset the mapper emits (replace/add on top-level elements)."""

    def value_of(part: dict) -> Any:
        if "part" in part:
            return {p["name"]: value_of(p) for p in part["part"]}
        return next(v for k, v in part.items() if k.startswith("value"))

    out = copy.deepcopy(resource)
    for op in params.get("parameter", []):
        parts = {p["name"]: p for p in op.get("part", [])}
        kind, path = parts["type"]["valueCode"], parts["path"]["valueString"]
        segments = path.split(".")
        if segments[0] != resource["resourceType"] or len(segments) > 2:
            raise StoreError(422, f"unsupported patch path {path}", "not-supported")
        if kind == "replace" and len(segments) == 2:
            out[segments[1]] = value_of(parts["value"])
        elif kind == "add" and len(segments) == 1:
            out.setdefault(parts["name"]["valueString"], []).append(value_of(parts["value"]))
        else:
            raise StoreError(422, f"unsupported patch operation {kind} {path}", "not-supported")
    return out


class FhirStore:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- reads --------------------------------------------------------------
    def read(self, rtype: str, rid: str) -> dict | None:
        row = self._db.execute("SELECT body FROM resources WHERE type=? AND id=?", (rtype, rid)).fetchone()
        return json.loads(row["body"]) if row else None

    def history(self, rtype: str, rid: str) -> list[dict]:
        rows = self._db.execute("SELECT body FROM history WHERE type=? AND id=? ORDER BY version DESC",
                                (rtype, rid)).fetchall()
        return [json.loads(r["body"]) for r in rows]

    def search(self, rtype: str, params: dict[str, str]) -> list[dict]:
        sql = "SELECT r.body FROM resources r WHERE r.type=?"
        args: list[Any] = [rtype]
        for name, raw in params.items():
            if name.startswith("_") and name not in ("_id", "_source"):
                continue
            if name == "_id":
                sql += " AND r.id=?"
                args.append(raw)
                continue
            param = "patient" if name == "subject" else name
            if param in ("patient", "target") and "/" not in raw:
                raw = f"Patient/{raw}"
            # Drive each condition from the (type, param, value) index rather than probing it once
            # per candidate row; at a few thousand resources the difference is seconds per import.
            clause = "SELECT i.id FROM idx i WHERE i.type=? AND i.param=?"
            args += [rtype, param]
            if param in ("family", "given"):
                clause += " AND i.value LIKE ?"
                args.append(raw.lower() + "%")
            elif "|" in raw:
                system, value = raw.split("|", 1)
                clause += " AND i.value=?" + (" AND i.system=?" if system else "")
                args += [value] + ([system] if system else [])
            else:
                clause += " AND i.value=?"
                args.append(raw)
            sql += f" AND r.id IN ({clause})"
        count = int(params.get("_count", 100))
        sql += " ORDER BY r.last_updated DESC LIMIT ?"
        args.append(count)
        return [json.loads(r["body"]) for r in self._db.execute(sql, args).fetchall()]

    def _search_query(self, query: str) -> tuple[str, list[dict]]:
        rtype, _, qs = query.partition("?")
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        return rtype, self.search(rtype, params)

    def everything(self, patient_id: str) -> list[dict]:
        patient = self.read("Patient", patient_id)
        if patient is None:
            raise StoreError(404, f"Patient/{patient_id} not found", "not-found")
        rows = self._db.execute(
            "SELECT DISTINCT r.body FROM resources r JOIN idx i ON i.type=r.type AND i.id=r.id "
            "WHERE i.param IN ('patient','target') AND i.value=? AND r.type != 'Patient'",
            (f"Patient/{patient_id}",)).fetchall()
        resources = [patient] + [json.loads(r["body"]) for r in rows]
        seen = {f"{r['resourceType']}/{r['id']}" for r in resources}
        for res in list(resources):
            for ref in _walk_refs(res):
                target = ref["reference"]
                if target.startswith("Practitioner/") and target not in seen:
                    seen.add(target)
                    if (p := self.read(*target.split("/"))):
                        resources.append(p)
        return resources

    def counts(self) -> dict[str, int]:
        rows = self._db.execute("SELECT type, COUNT(*) n FROM resources GROUP BY type ORDER BY type")
        return {r["type"]: r["n"] for r in rows}

    def all_resources(self, types: Iterable[str] | None = None) -> list[dict]:
        rows = self._db.execute("SELECT type, body FROM resources").fetchall()
        wanted = set(types) if types else None
        return [json.loads(r["body"]) for r in rows if wanted is None or r["type"] in wanted]

    # -- writes ---------------------------------------------------------------
    def _write(self, res: dict, rtype: str, rid: str) -> dict:
        row = self._db.execute("SELECT version FROM resources WHERE type=? AND id=?", (rtype, rid)).fetchone()
        version = (row["version"] + 1) if row else 1
        res = {**res, "resourceType": rtype, "id": rid}
        res["meta"] = {**res.get("meta", {}), "versionId": str(version), "lastUpdated": _now()}
        body = json.dumps(res, separators=(",", ":"))
        updated = res["meta"]["lastUpdated"]
        self._db.execute("INSERT OR REPLACE INTO resources VALUES (?,?,?,?,?)", (rtype, rid, version, updated, body))
        self._db.execute("INSERT INTO history VALUES (?,?,?,?,?)", (rtype, rid, version, updated, body))
        self._db.execute("DELETE FROM idx WHERE type=? AND id=?", (rtype, rid))
        self._db.executemany("INSERT INTO idx VALUES (?,?,?,?,?)",
                             [(rtype, rid, p, s, v) for p, s, v in _index_rows(res)])
        return res

    def create(self, resource: dict) -> dict:
        with self._lock:
            return self._write(resource, resource["resourceType"], str(uuid.uuid4()))

    def transaction(self, bundle: dict) -> dict:
        """Process a transaction Bundle atomically (all entries commit or none do)."""
        if bundle.get("resourceType") != "Bundle" or bundle.get("type") not in ("transaction", "batch"):
            raise StoreError(400, "expected a transaction Bundle", "invalid")
        entries = bundle.get("entry", [])
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                plan, ref_map = self._resolve(entries)
                responses = []
                for entry, (action, rtype, rid) in zip(entries, plan):
                    res = copy.deepcopy(entry.get("resource", {}))
                    for ref in _walk_refs(res):
                        ref["reference"] = ref_map.get(ref["reference"], ref["reference"])
                    if action == "noop":
                        current = self.read(rtype, rid)
                        responses.append(self._response("200 OK", current))
                        continue
                    if action == "patch":
                        current = self.read(rtype, rid)
                        res = _patch(current, res)
                    written = self._write(res, rtype, rid)
                    status = "201 Created" if action == "create" else "200 OK"
                    responses.append(self._response(status, written))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return {"resourceType": "Bundle", "type": "transaction-response", "entry": responses}

    def _resolve(self, entries: list[dict]) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
        """First pass: decide create/update/noop for each entry and assign ids."""
        plan, ref_map = [], {}
        for entry in entries:
            req = entry.get("request", {})
            method, url = req.get("method", "POST").upper(), req.get("url", "")
            rtype = url.split("?")[0].split("/")[0]
            if method == "POST":
                matches = self._search_query(f"{rtype}?{req['ifNoneExist']}")[1] if req.get("ifNoneExist") else []
                if len(matches) > 1:
                    raise StoreError(412, f"ifNoneExist {req['ifNoneExist']} matched {len(matches)} resources")
                action, rid = ("noop", matches[0]["id"]) if matches else ("create", str(uuid.uuid4()))
            elif method in ("PUT", "PATCH") and "?" in url:
                matches = self._search_query(url)[1]
                if len(matches) > 1:
                    raise StoreError(412, f"conditional {method} {url} matched {len(matches)} resources",
                                     "multiple-matches")
                if method == "PATCH" and not matches:
                    raise StoreError(404, f"conditional PATCH {url} matched no resource", "not-found")
                action = ("patch" if method == "PATCH" else "update") if matches else "create"
                rid = matches[0]["id"] if matches else str(uuid.uuid4())
            elif method in ("PUT", "PATCH") and "/" in url:
                rid = url.split("/")[1]
                exists = self.read(rtype, rid) is not None
                action = ("patch" if method == "PATCH" else "update") if exists else "create"
                if method == "PATCH" and not exists:
                    raise StoreError(404, f"{url} not found", "not-found")
            else:
                raise StoreError(400, f"unsupported transaction method {method} {url}", "not-supported")
            plan.append((action, rtype, rid))
            if entry.get("fullUrl"):
                ref_map[entry["fullUrl"]] = f"{rtype}/{rid}"
        return plan, ref_map

    @staticmethod
    def _response(status: str, res: dict) -> dict:
        v = res["meta"]["versionId"]
        return {"response": {"status": status,
                             "location": f"{res['resourceType']}/{res['id']}/_history/{v}",
                             "etag": f'W/"{v}"', "lastModified": res["meta"]["lastUpdated"]}}

    # -- message ledger ---------------------------------------------------------
    def log_message(self, **row: Any) -> None:
        row.setdefault("received_at", _now())
        cols = ("control_id", "received_at", "sender", "message_type", "status", "ack", "error",
                "raw_redacted", "resources", "elapsed_ms")
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO messages VALUES ({','.join('?' * len(cols))})",
                             [row.get(c) for c in cols])

    def get_message(self, control_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM messages WHERE control_id=?", (control_id,)).fetchone()
        return dict(row) if row else None

    def list_messages(self, limit: int = 100) -> list[dict]:
        rows = self._db.execute("SELECT * FROM messages ORDER BY received_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows.fetchall()]

    def message_stats(self) -> dict[str, int]:
        rows = self._db.execute("SELECT status, COUNT(*) n FROM messages GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
