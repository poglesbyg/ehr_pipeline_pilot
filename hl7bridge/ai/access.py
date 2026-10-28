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


def valid_icn(icn: str) -> bool:
    return bool(ICN_PATTERN.fullmatch(icn or ""))


@dataclass(frozen=True)
class Clinician:
    user_id: str
    display: str
    panel: frozenset[str] = field(default_factory=frozenset)  # ICNs, or {"*"}
    authorizations: frozenset[str] = field(default_factory=frozenset)  # 7332 labels: ETH, HIV, SCA

    def may_see(self, icns: set[str]) -> bool:
        return ALL_PATIENTS in self.panel or bool(self.panel & icns)

    def may_read(self, labels: set[str] | frozenset[str]) -> bool:
        return set(labels) <= self.authorizations


SYNTHETIC_ICNS = frozenset({"1013579246V135792", "1019999999V999999"})

PILOT_CLINICIANS = {
    c.user_id: c for c in (
        Clinician("dr-primary", "Primary care physician, PACT team (station 999)", SYNTHETIC_ICNS),
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

    def authorize(self, user_id: str, icns: set[str]) -> Clinician:
        clinician = self.clinician(user_id)
        if not icns or not clinician.may_see(icns):
            raise AccessDenied(f"{user_id} has no access to this patient")
        return clinician
