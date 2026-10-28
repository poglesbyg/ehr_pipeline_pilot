"""Who may see which patient, decided on the server before any retrieval runs.

In production the clinician comes from VA single sign-on (PIV) and the patient from the EHR
launch (SMART on FHIR on the Oracle Health side, CPRS context on the VistA side), and the
access decision is the EHR's own, including break-the-glass. The pilot stands those in with a
small static policy so the controls around them can be built and tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# VA Integration Control Number: 10 digits, "V", 6-digit checksum. Validating the format before
# it is used anywhere also blocks filter injection (e.g. "...' or 1 eq 1").
ICN_PATTERN = re.compile(r"\d{10}V\d{6}")

ALL_PATIENTS = "*"


class AccessDenied(Exception):
    """Fail closed: the caller gets nothing, and the attempt is audited."""


class ScopeViolation(AccessDenied):
    """Evidence outside the authorized scope reached the assistant. Should never happen; if it
    does, the second check turns a filter bug into a refused answer instead of a leak."""


# Identifiers from other systems (Epic/Oracle MRNs, FHIR ids): conservative shapes only, so a
# value can never carry filter syntax, quotes or separators into a query.
_ID_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,63}")
_ID_SYSTEM = re.compile(r"(urn:[A-Za-z0-9.:\-]+|https?://[A-Za-z0-9.\-]+(:\d+)?(/[A-Za-z0-9._~/\-]*)?)")


def valid_icn(icn: str) -> bool:
    return bool(ICN_PATTERN.fullmatch(icn or ""))


def valid_identifier(system: str, value: str, icn_system: str | None = None) -> bool:
    if icn_system and system == icn_system:
        return valid_icn(value)
    return bool(_ID_SYSTEM.fullmatch(system or "") and _ID_VALUE.fullmatch(value or ""))


@dataclass(frozen=True)
class Clinician:
    user_id: str
    display: str
    # Panel entries: a bare ICN, "system|value" for any identifier, "system|*" for every patient
    # with an identifier in that system (e.g. a site's whole MRN space), or "*".
    panel: frozenset[str] = field(default_factory=frozenset)
    authorizations: frozenset[str] = field(default_factory=frozenset)  # 7332 labels: ETH, HIV, SCA

    def may_see(self, keys: set[str]) -> bool:
        """``keys`` are the patient's identifiers as "system|value" (plus bare ICNs)."""
        if ALL_PATIENTS in self.panel or self.panel & keys:
            return True
        systems = {k.split("|", 1)[0] for k in keys if "|" in k}
        return any(entry.endswith("|*") and entry[:-2] in systems for entry in self.panel)

    def may_read(self, labels: set[str] | frozenset[str]) -> bool:
        return set(labels) <= self.authorizations


SYNTHETIC_ICNS = frozenset({"1013579246V135792", "1019999999V999999"})
# Patients imported from the public SMART sandbox (Synthea-generated) carry this source system.
SANDBOX_SOURCE = "urn:fhir-source:r4.smarthealthit.org"

PILOT_CLINICIANS = {
    c.user_id: c for c in (
        Clinician("dr-primary", "Primary care physician, PACT team (station 999)",
                  SYNTHETIC_ICNS | {f"{SANDBOX_SOURCE}|*"}),
        Clinician("dr-sud-clinic", "Addiction psychiatry, SUD clinic (station 999)", SYNTHETIC_ICNS,
                  frozenset({"ETH"})),
        Clinician("dr-other-site", "Physician at another station (no relationship to this patient)",
                  frozenset({"9999999999V999999"})),
    )
}


class AccessPolicy:
    def __init__(self, clinicians: dict[str, Clinician] | None = None):
        self.clinicians = dict(PILOT_CLINICIANS if clinicians is None else clinicians)

    def clinician(self, user_id: str) -> Clinician:
        if user_id not in self.clinicians:
            raise AccessDenied(f"unknown user {user_id!r}")
        return self.clinicians[user_id]

    def authorize(self, user_id: str, keys: set[str]) -> Clinician:
        clinician = self.clinician(user_id)
        if not keys or not clinician.may_see(keys):
            raise AccessDenied(f"{user_id} has no access to this patient")
        return clinician

    def grant_launch(self, user_id: str, display: str, patient_keys: set[str]) -> Clinician:
        """A SMART launch carries the EHR's own access decision: the token is scoped to one patient.
        Record that as a clinician whose panel is exactly that patient (no 7332 authorizations)."""
        clinician = Clinician(user_id, display, frozenset(patient_keys))
        self.clinicians[user_id] = clinician
        return clinician
