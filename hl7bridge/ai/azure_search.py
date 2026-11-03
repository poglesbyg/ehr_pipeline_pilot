"""Azure AI Search as the evidence index: a drop-in for ``EvidenceIndex``.

* Hybrid retrieval: keyword (BM25) plus, when an embeddings deployment is configured, a vector
  query over ``content_vector``; Azure merges them with reciprocal rank fusion. The semantic
  ranker can be layered on (``AZURE_SEARCH_SEMANTIC=1``).
* Patient scope and 38 USC 7332 are an OData filter applied with ``vectorFilterMode: preFilter``,
  i.e. before the nearest-neighbor search, never after. Every value placed in the filter is
  validated first, so nothing from a request can widen it. The assistant still re-checks every
  hit afterwards.
* Series chunks (repeated labs, refills) are built before upload, and copied-forward text is
  collapsed after search, exactly as the local index does.
* Auth is Entra ID only (keys disabled on the service); see ``hl7bridge.azure_auth``.

REST API, standard library only. The Python SDK (``azure-search-documents``) is the production
choice; the REST payloads here are what it sends.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable

from ..sensitivity import LABELS
from .index import SYNONYMS, Chunk, EvidenceIndex, build_series, tokenize

API_VERSION = "2024-07-01"
_PATIENT_REF = re.compile(r"Patient/[A-Za-z0-9.\-]{1,64}")
RETRY = {429, 500, 502, 503, 504}


class AzureSearchError(Exception):
    pass


def _http(method: str, url: str, body: dict | None, token: Callable[[], str], *, retries: int = 3,
          sleep=time.sleep, opener=urllib.request.urlopen) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {token()}"})
        try:
            with opener(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode(errors="replace")
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            exc.close()
            if exc.code not in RETRY or attempt == retries:
                raise AzureSearchError(f"{method} {url.split('?')[0]} -> HTTP {exc.code}: {detail}") from exc
            sleep(min(float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt, 30))
    raise AzureSearchError("unreachable")


class AzureOpenAIEmbedder:
    """Batch embeddings from an Azure OpenAI deployment (e.g. text-embedding-3-small)."""

    def __init__(self, endpoint: str, deployment: str, token: Callable[[], str],
                 api_version: str | None = None, batch: int = 64, **http):
        self.url = (f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/embeddings"
                    f"?api-version={api_version or os.environ.get('AZURE_OPENAI_API_VERSION', '2024-10-21')}")
        self.token, self.batch, self.http = token, batch, http
        self.calls = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            self.calls += 1
            data = _http("POST", self.url, {"input": texts[i:i + self.batch]}, self.token, **self.http)
            out += [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
        return out


def _key(chunk_id: str) -> str:
    """Search document keys allow letters, digits, '-', '_' and '='."""
    return base64.urlsafe_b64encode(chunk_id.encode()).decode()


class AzureSearchIndex:
    def __init__(self, endpoint: str, index: str, token: Callable[[], str], *,
                 embedder: AzureOpenAIEmbedder | None = None, semantic: bool = False, **http):
        self.base = endpoint.rstrip("/")
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,126}", index):
            raise AzureSearchError(f"invalid index name {index!r}")
        self.index, self.token, self.embedder, self.semantic, self.http = index, token, embedder, semantic, http
        self._ready = False
        self._patients: dict[str, int] = {}
        self.requests = 0

    # -- plumbing ---------------------------------------------------------------------------
    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        self.requests += 1
        return _http(method, f"{self.base}/{path}?api-version={API_VERSION}", body, self.token, **self.http)

    def ensure_index(self, dimensions: int | None) -> None:
        if self._ready:
            return
        fields = [
            {"name": "key", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "patient", "type": "Edm.String", "filterable": True},
            {"name": "protected", "type": "Collection(Edm.String)", "filterable": True},
            {"name": "kind", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "date", "type": "Edm.String", "filterable": True, "sortable": True},
            {"name": "header", "type": "Edm.String", "searchable": True},
            {"name": "text", "type": "Edm.String", "searchable": True},
            {"name": "chunk", "type": "Edm.String", "searchable": False, "retrievable": True},
        ]
        schema: dict = {"name": self.index, "fields": fields}
        if dimensions:
            fields.append({"name": "content_vector", "type": "Collection(Edm.Single)", "searchable": True,
                           "retrievable": False, "dimensions": dimensions, "vectorSearchProfile": "hnsw-profile"})
            schema["vectorSearch"] = {"algorithms": [{"name": "hnsw", "kind": "hnsw"}],
                                      "profiles": [{"name": "hnsw-profile", "algorithm": "hnsw"}]}
        if self.semantic:
            schema["semantic"] = {"configurations": [{"name": "default", "prioritizedFields": {
                "titleField": {"fieldName": "header"}, "prioritizedContentFields": [{"fieldName": "text"}]}}]}
        self._call("PUT", f"indexes/{self.index}", schema)
        self._ready = True

    # -- EvidenceIndex interface ---------------------------------------------------------------
    def __len__(self) -> int:
        return sum(self._patients.values())

    def patients(self) -> set[str]:
        return set(self._patients)

    def add_patient_chunks(self, patient: str, chunks: list[Chunk]) -> None:
        chunks = build_series(chunks)
        for c in chunks:
            c.header = c.header or c.kind
        vectors = self.embedder([f"{c.header}: {c.text}" for c in chunks]) if self.embedder and chunks else None
        self.ensure_index(len(vectors[0]) if vectors else None)
        docs = []
        for i, c in enumerate(chunks):
            doc = {"@search.action": "mergeOrUpload", "key": _key(c.id), "patient": c.patient,
                   "protected": list(c.protected), "kind": c.kind, "date": c.date, "header": c.header,
                   "text": c.text, "chunk": json.dumps(c.to_json())}
            if vectors:
                doc["content_vector"] = vectors[i]
            docs.append(doc)
        for i in range(0, len(docs), 500):
            self._call("POST", f"indexes/{self.index}/docs/index", {"value": docs[i:i + 500]})
        self._patients[patient] = len(docs)

    def drop_patient(self, patient: str) -> None:
        """Delete every remote document for the patient (a retracted note must leave the index),
        even if this process hasn't touched the index yet."""
        self._patients.pop(patient, None)
        if not _PATIENT_REF.fullmatch(patient):
            return
        while True:
            try:
                found = self._call("POST", f"indexes/{self.index}/docs/search",
                                   {"search": "*", "filter": f"patient eq '{patient}'", "select": "key", "top": 1000})
            except AzureSearchError as exc:
                if "HTTP 404" in str(exc):  # no index yet: nothing to delete
                    return
                raise
            keys = [d["key"] for d in found.get("value", [])]
            if not keys:
                break
            self._call("POST", f"indexes/{self.index}/docs/index",
                       {"value": [{"@search.action": "delete", "key": k} for k in keys]})
            if len(keys) < 1000:
                break

    @staticmethod
    def scope_filter(patients: list[str], allow: frozenset[str] | None) -> str:
        """OData filter for one patient identity and readable 7332 labels. Values are validated,
        not escaped: anything that isn't a plain Patient reference or a known label is refused."""
        bad = [p for p in patients if not _PATIENT_REF.fullmatch(p)]
        if bad or not patients:
            raise AzureSearchError(f"refusing to build a filter from {bad or 'no patients'}")
        clause = f"search.in(patient, '{','.join(patients)}', ',')"
        if allow is not None:
            unknown = set(allow) - LABELS.keys()
            if unknown:
                raise AzureSearchError(f"unknown sensitivity labels {sorted(unknown)}")
            # all() over an empty collection is true: unlabeled chunks always pass; labeled ones only
            # when every label is authorized ('' matches nothing, so no authorization -> excluded).
            clause += f" and protected/all(p: search.in(p, '{','.join(sorted(allow))}', ','))"
        return clause

    def search(self, patients: str | list[str], query: str, k: int = 8,
               allow: frozenset[str] | None = frozenset()) -> list[tuple[Chunk, float]]:
        patients = [patients] if isinstance(patients, str) else list(patients)
        if not any(p in self._patients for p in patients):
            return []
        terms = tokenize(query)
        expanded = terms + [s for t in terms for s in SYNONYMS.get(t, [])]
        body: dict = {"search": " ".join(expanded) or "*", "searchMode": "any",
                      "filter": self.scope_filter(patients, allow), "select": "chunk", "top": k * 3}
        if self.embedder and query.strip():
            body["vectorQueries"] = [{"kind": "vector", "vector": self.embedder([query])[0],
                                      "fields": "content_vector", "k": 50}]
            body["vectorFilterMode"] = "preFilter"
        if self.semantic:
            body.update(queryType="semantic", semanticConfiguration="default")
        found = self._call("POST", f"indexes/{self.index}/docs/search", body)
        hits = [(Chunk(**json.loads(d["chunk"])), round(float(d.get("@search.score", 0)), 4))
                for d in found.get("value", [])]
        hits = [(c, s) for c, s in hits if s > 0 or not terms]
        return EvidenceIndex._collapse_copies(hits)[:k]


def from_env() -> AzureSearchIndex:
    from ..azure_auth import TokenProvider
    endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT")
    if not endpoint:
        raise AzureSearchError("set AZURE_SEARCH_ENDPOINT (e.g. https://<name>.search.windows.net)")
    embedder = None
    if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT"):
        embedder = AzureOpenAIEmbedder(os.environ["AZURE_OPENAI_ENDPOINT"], os.environ["AZURE_OPENAI_EMBED_DEPLOYMENT"],
                                       TokenProvider("openai"))
    return AzureSearchIndex(endpoint, os.environ.get("AZURE_SEARCH_INDEX", "hl7bridge-evidence"),
                            TokenProvider("search"), embedder=embedder,
                            semantic=os.environ.get("AZURE_SEARCH_SEMANTIC") == "1")
