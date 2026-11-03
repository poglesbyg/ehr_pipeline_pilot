"""Entra ID tokens for Azure services without secrets in code or config.

Order of preference:
1. Managed identity (inside Azure): the App Service / Container Apps identity endpoint
   (``IDENTITY_ENDPOINT`` + ``IDENTITY_HEADER``), else the instance metadata service (IMDS) on
   VMs and container instances. ``AZURE_CLIENT_ID`` selects a user-assigned identity.
2. Azure CLI (a developer laptop): ``az account get-access-token``.

Commercial and Government clouds use different token audiences; using the commercial one
against an ``.azure.us`` endpoint fails, so the cloud is explicit (``AZURE_CLOUD``).
Standard library only, matching the rest of the pilot; in production azure-identity's
``DefaultAzureCredential`` does the same job.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

AUDIENCES = {
    "public": {"openai": "https://cognitiveservices.azure.com", "search": "https://search.azure.com"},
    "usgovernment": {"openai": "https://cognitiveservices.azure.us", "search": "https://search.azure.us"},
}
IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"


class AzureAuthError(Exception):
    pass


def cloud() -> str:
    name = os.environ.get("AZURE_CLOUD", "public").lower()
    if name not in AUDIENCES:
        raise AzureAuthError(f"AZURE_CLOUD must be one of {sorted(AUDIENCES)}")
    return name


class TokenProvider:
    """Callable returning a bearer token for one audience; refreshes 5 minutes before expiry."""

    def __init__(self, service: str, *, runner=subprocess.run, opener=urllib.request.urlopen):
        self.resource = AUDIENCES[cloud()][service]
        self._run, self._open = runner, opener
        self._token, self._expires = None, 0.0
        self._lock = threading.Lock()
        self.source = None

    def __call__(self) -> str:
        with self._lock:
            if self._token is None or time.time() > self._expires - 300:
                self._token, self._expires, self.source = self._fetch()
            return self._token

    def _fetch(self) -> tuple[str, float, str]:
        client_id = os.environ.get("AZURE_CLIENT_ID")
        if os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER"):
            params = {"resource": self.resource, "api-version": "2019-08-01"}
            if client_id:
                params["client_id"] = client_id
            url = f"{os.environ['IDENTITY_ENDPOINT']}?{urllib.parse.urlencode(params)}"
            return self._managed(url, {"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]}, "managed identity")
        if os.environ.get("AZURE_USE_IMDS") or client_id:
            params = {"resource": self.resource, "api-version": "2018-02-01"}
            if client_id:
                params["client_id"] = client_id
            return self._managed(f"{IMDS}?{urllib.parse.urlencode(params)}", {"Metadata": "true"}, "managed identity (IMDS)")
        if shutil.which("az") or self._run is not subprocess.run:
            out = self._run(["az", "account", "get-access-token", "--resource", self.resource, "-o", "json"],
                            capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                raise AzureAuthError(f"az account get-access-token failed: {out.stderr.strip()[:200]}")
            data = json.loads(out.stdout)
            expires = data.get("expires_on") or time.time() + 3000
            return data["accessToken"], float(expires), "azure cli"
        raise AzureAuthError("no Azure credential: run `az login`, or run inside Azure with a managed identity")

    def _managed(self, url: str, headers: dict, source: str) -> tuple[str, float, str]:
        try:
            with self._open(urllib.request.Request(url, headers=headers), timeout=10) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, OSError) as exc:
            raise AzureAuthError(f"{source} token request failed: {exc}") from exc
        return data["access_token"], float(data.get("expires_on", time.time() + 3000)), source
