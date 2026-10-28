"""SMART App Launch (authorization code + PKCE) as a public client.

EHR launch: the EHR opens ``/smart/launch?iss=<fhir base>&launch=<opaque>``.
Standalone launch: the user opens ``/smart/start?iss=<fhir base>`` and picks a patient at the EHR.

Either way the EHR decides who the user is and which patient they may see; the token response
carries that patient. The pilot honors it as the access decision (the session can see exactly
that patient), pulls the record through the FHIR client, and then discards the access token:
nothing keeps a credential to the EHR after the import.

Only issuers on an allowlist are accepted, so the launch endpoint can't be used to make the
server fetch arbitrary URLs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .fhir_client import FhirClient, FhirError

DEFAULT_ISSUERS = ("https://launch.smarthealthit.org/v/r4/fhir", "https://r4.smarthealthit.org")
STATE_TTL = 600


class SmartError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def jwt_claims(token: str | None) -> dict:
    """Read an id_token's claims. Signature isn't checked: the token came straight from the token
    endpoint over TLS, which OpenID Connect Core 3.1.3.7 allows in place of signature validation.
    Claims are used for display and audit, never for the access decision (that's the token's patient)."""
    if not token or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    try:
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, json.JSONDecodeError):
        return {}


class SmartLauncher:
    def __init__(self, client_id: str | None = None, issuers: tuple[str, ...] | None = None,
                 scope: str = "openid fhirUser patient/*.read"):
        self.client_id = client_id or os.environ.get("BRIDGE_SMART_CLIENT_ID", "hl7bridge-pilot")
        env = os.environ.get("BRIDGE_SMART_ISSUERS")
        self.issuers = tuple(i.rstrip("/") for i in (issuers or (env.split(",") if env else DEFAULT_ISSUERS)))
        self.scope = scope
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()

    def authorize_url(self, iss: str, redirect_uri: str, launch: str | None = None) -> str:
        iss = (iss or "").rstrip("/")
        if iss not in self.issuers:
            raise SmartError(f"issuer {iss!r} is not on the allowlist")
        config = FhirClient(iss).smart_configuration()
        if not config.get("authorization_endpoint") or not config.get("token_endpoint"):
            raise SmartError("issuer does not advertise SMART authorization endpoints")
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        with self._lock:
            now = time.monotonic()
            self._pending = {k: v for k, v in self._pending.items() if now - v["created"] < STATE_TTL}
            self._pending[state] = {"iss": iss, "verifier": verifier, "redirect_uri": redirect_uri,
                                    "token_endpoint": config["token_endpoint"], "created": now}
        params = {
            "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
            "scope": f"launch {self.scope}" if launch else f"launch/patient {self.scope}",
            "state": state, "aud": iss, "code_challenge": challenge, "code_challenge_method": "S256",
        }
        if launch:
            params["launch"] = launch
        return f"{config['authorization_endpoint']}?{urllib.parse.urlencode(params)}"

    def exchange(self, code: str, state: str) -> dict:
        """Single-use state; returns the token response plus the issuer it belongs to."""
        with self._lock:
            pending = self._pending.pop(state or "", None)
        if pending is None or time.monotonic() - pending["created"] > STATE_TTL:
            raise SmartError("unknown or expired launch state")
        form = urllib.parse.urlencode({
            "grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
            "client_id": self.client_id, "code_verifier": pending["verifier"]}).encode()
        req = urllib.request.Request(pending["token_endpoint"], data=form, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                token = json.load(resp)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise SmartError(f"token exchange failed: HTTP {exc.code}") from exc
        if not token.get("access_token") or not token.get("patient"):
            raise SmartError("token response has no access token or patient context")
        return {**token, "iss": pending["iss"]}

    def fetch_record(self, token: dict, on_warning=lambda msg: None) -> list[dict]:
        try:
            return FhirClient(token["iss"], token=token["access_token"]).patient_record(
                token["patient"], on_warning=on_warning)
        except FhirError as exc:
            raise SmartError(str(exc)) from exc
