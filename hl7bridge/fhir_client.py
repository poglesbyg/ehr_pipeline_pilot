"""FHIR R4 client for pulling a patient's record from an EHR's FHIR API (Epic, Oracle Health,
HAPI, the SMART sandbox).

Handles what real EHR APIs do that test fixtures don't: paged searches (``Bundle.link`` next),
throttling (429 with Retry-After), notes stored as a separate ``Binary`` referenced by
``DocumentReference.content.attachment.url`` (common in Epic), HTML/RTF note bodies, and
``MedicationRequest.medicationReference`` pointing at a ``Medication`` resource.

Every URL the client follows (next links, Binary urls, references) must stay on the FHIR
server's own origin, so a hostile or misconfigured server can't redirect the bearer token to a
third party.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Callable, Iterator

# Resource types pulled for a patient, with the search parameter that scopes them.
PATIENT_TYPES = [
    ("Encounter", "patient"), ("Condition", "patient"), ("Observation", "patient"),
    ("MedicationRequest", "patient"), ("MedicationStatement", "patient"), ("AllergyIntolerance", "patient"),
    ("DiagnosticReport", "patient"), ("DocumentReference", "patient"), ("Immunization", "patient"),
    ("Procedure", "patient"),
]
RETRY_STATUS = {429, 500, 502, 503, 504}


class FhirError(Exception):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


_RTF_METADATA = re.compile(r"\{\\(\*|fonttbl|colortbl|stylesheet|info|listtable|listoverridetable|generator)")


def _drop_rtf_groups(rtf: str) -> str:
    """Remove metadata groups ({\\fonttbl ...} etc.), which can nest, by brace matching."""
    out, i = [], 0
    while (m := _RTF_METADATA.search(rtf, i)):
        out.append(rtf[i:m.start()])
        depth, j = 0, m.start()
        while j < len(rtf):
            if rtf[j] == "\\":
                j += 2
                continue
            depth += {"{": 1, "}": -1}.get(rtf[j], 0)
            j += 1
            if depth == 0:
                break
        i = j
    return "".join(out) + rtf[i:]


def rtf_to_text(rtf: str) -> str:
    """Good-enough RTF stripping for clinical notes (no embedded objects)."""
    text = re.sub(r"\\par[d]?\b ?", "\n", _drop_rtf_groups(rtf))
    text = re.sub(r"\{\\\*[^{}]*\}|\\'[0-9a-f]{2}|\\[a-z]+-?\d* ?|[{}]", "", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def attachment_text(content_type: str, data: bytes) -> str | None:
    kind = (content_type or "").split(";")[0].strip().lower()
    text = data.decode("utf-8", errors="replace")
    if kind in ("text/plain", ""):
        return text
    if kind in ("text/html", "application/xhtml+xml"):
        return html_to_text(text)
    if kind in ("text/rtf", "application/rtf"):
        return rtf_to_text(text)
    return None  # PDFs and images need OCR (Document Intelligence in production)


class FhirClient:
    def __init__(self, base: str, *, token: str | None = None, token_provider: Callable[[], str] | None = None,
                 timeout: float = 30, page_size: int = 200, max_pages: int = 100, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep):
        self.base = base.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise FhirError(f"invalid FHIR base URL {base!r}")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.token, self.token_provider = token, token_provider
        self.timeout, self.page_size, self.max_pages = timeout, page_size, max_pages
        self.max_retries, self._sleep = max_retries, sleep
        self.requests = 0

    # -- HTTP -----------------------------------------------------------------------------
    def _url(self, path_or_url: str, params: dict | None = None) -> str:
        url = path_or_url if "://" in path_or_url else f"{self.base}/{path_or_url.lstrip('/')}"
        parts = urllib.parse.urlsplit(url)
        # Exact scheme + host + port match. (A string-prefix check would both reject legitimate
        # links such as HAPI's "https://host?_getpages=..." and accept "https://host.evil.com/".)
        if f"{parts.scheme}://{parts.netloc}".lower() != self.origin.lower():
            raise FhirError(f"refusing to follow {url!r}: not on the FHIR server's origin {self.origin}")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def _headers(self, accept: str) -> dict:
        headers = {"Accept": accept, "User-Agent": "hl7bridge-pilot/0.1"}
        bearer = self.token_provider() if self.token_provider else self.token
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    def _open(self, url: str, accept: str) -> tuple[bytes, str]:
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, headers=self._headers(accept))
            self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read(), resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if exc.code not in RETRY_STATUS or attempt == self.max_retries:
                    raise FhirError(f"GET {url} -> HTTP {exc.code}") from exc
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() \
                    else 2 ** attempt
                self._sleep(min(delay, 30))
        raise FhirError("unreachable")

    def get(self, path_or_url: str, params: dict | None = None) -> dict:
        body, _ = self._open(self._url(path_or_url, params), "application/fhir+json")
        return json.loads(body)

    # -- FHIR operations ------------------------------------------------------------------------
    def smart_configuration(self) -> dict:
        """SMART App Launch discovery: .well-known first, CapabilityStatement security as fallback."""
        try:
            return self.get(".well-known/smart-configuration")
        except (FhirError, json.JSONDecodeError):
            meta = self.get("metadata")
            ext = next((e for r in meta.get("rest", []) for e in r.get("security", {}).get("extension", [])
                        if e.get("url", "").endswith("oauth-uris")), {})
            uris = {e["url"]: e.get("valueUri") for e in ext.get("extension", [])}
            return {"authorization_endpoint": uris.get("authorize"), "token_endpoint": uris.get("token")}

    def read(self, rtype: str, rid: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9\-.]{1,64}", rid or ""):
            raise FhirError(f"invalid resource id {rid!r}")
        return self.get(f"{rtype}/{rid}")

    def search(self, rtype: str, params: dict) -> Iterator[dict]:
        bundle = self.get(rtype, {**params, "_count": self.page_size})
        for _ in range(self.max_pages):
            for entry in bundle.get("entry", []):
                if entry.get("search", {}).get("mode", "match") == "match" and "resource" in entry:
                    yield entry["resource"]
            nxt = next((link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"), None)
            if not nxt:
                return
            bundle = self.get(nxt)
        raise FhirError(f"{rtype} search exceeded {self.max_pages} pages")

    def binary_text(self, url: str, content_type: str = "") -> str | None:
        body, served_type = self._open(self._url(url), content_type or "*/*")
        served_type = served_type or content_type
        if "fhir+json" in served_type or "application/json" in served_type:  # a Binary resource, not raw bytes
            binary = json.loads(body)
            body, served_type = base64.b64decode(binary.get("data", "")), binary.get("contentType", content_type)
        return attachment_text(served_type, body)

    def patient_record(self, patient_id: str, types: list[tuple[str, str]] = PATIENT_TYPES,
                       on_warning: Callable[[str], None] = lambda msg: None) -> list[dict]:
        """The patient plus every resource of ``types`` that references them, with medications and
        note bodies resolved inline so the record is self-contained."""
        patient = self.read("Patient", patient_id)
        resources = [patient]
        medications: dict[str, dict | None] = {}
        for rtype, param in types:
            try:
                found = list(self.search(rtype, {param: patient_id}))
            except FhirError as exc:  # a server may not support every type; record it and go on
                on_warning(f"{rtype}: {exc}")
                continue
            for res in found:
                if rtype == "MedicationRequest" and "medicationReference" in res:
                    self._inline_medication(res, medications, on_warning)
                if rtype == "DocumentReference":
                    self._inline_note(res, on_warning)
            resources += found
        return resources

    def _inline_medication(self, req: dict, cache: dict, warn) -> None:
        ref = req["medicationReference"].get("reference", "")
        if ref not in cache:
            try:
                cache[ref] = self.get(ref) if ref.startswith("Medication/") else None
            except FhirError as exc:
                warn(f"{ref}: {exc}")
                cache[ref] = None
        if cache[ref] and cache[ref].get("code"):
            req["medicationCodeableConcept"] = cache[ref]["code"]

    def _inline_note(self, doc: dict, warn) -> None:
        for content in doc.get("content", []):
            att = content.get("attachment", {})
            if att.get("data") or not att.get("url"):
                continue
            try:
                text = self.binary_text(att["url"], att.get("contentType", ""))
            except FhirError as exc:
                warn(f"note {doc.get('id')}: {exc}")
                continue
            if text is None:
                warn(f"note {doc.get('id')}: {att.get('contentType')} needs OCR; skipped")
                continue
            att["data"] = base64.b64encode(text.encode()).decode()
            att["contentType"] = "text/plain; charset=utf-8"
            return  # one readable rendition is enough
