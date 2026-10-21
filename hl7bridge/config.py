"""Deployment configuration. Every naming-system URI is a pilot placeholder to be confirmed
against the VA terminology / naming-system registry before production use."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import vocab


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")


@dataclass
class BridgeConfig:
    # FHIR requires an offset on any dateTime with a time; VistA rarely sends one.
    default_tz: str = "-05:00"
    # Minimum-necessary: SSNs are dropped at the boundary unless explicitly enabled.
    ssn_policy: str = "omit"  # "omit" | "include"
    pilot_base: str = "https://fhir.pilot.example"
    # VA root OID; ICNs (MPI national identifiers) are issued under it.
    icn_system: str = "urn:oid:2.16.840.1.113883.4.349"
    receiving_app: str = "HL7BRIDGE"
    receiving_facility: str = "NAII-PILOT"
    supported_versions: frozenset = field(
        default_factory=lambda: frozenset({"2.3", "2.3.1", "2.4", "2.5", "2.5.1", "2.6", "2.7", "2.8"}))

    @property
    def extension_base(self) -> str:
        return f"{self.pilot_base}/StructureDefinition"

    def local_code_system(self, name: str) -> str:
        return f"{self.pilot_base}/CodeSystem/{_slug(name)}"

    def coding_system(self, name: str) -> str | None:
        if not name:
            return None
        return vocab.CODING_SYSTEMS.get(name.upper()) or self.local_code_system(name)

    def identifier_system(self, authority: str, oid: str, oid_type: str, id_type: str,
                          facility: str) -> str:
        """Resolve a CX assigning authority / identifier type to a FHIR identifier system."""
        if oid and oid_type.upper() == "ISO":
            return f"urn:oid:{oid}"
        auth = authority.upper()
        if id_type == "SS" or auth in ("USSSA", "SSA"):
            return vocab.US_SSN
        if id_type == "NPI" or auth == "NPI":
            return vocab.US_NPI
        if auth == "USVHA" and id_type == "NI":
            return self.icn_system
        if auth == "USVHA":
            # DFNs are only unique within a VistA instance, so the station is part of the system.
            return f"{self.pilot_base}/sid/va-station/{_slug(facility or 'unknown')}/{_slug(id_type or 'id')}"
        return f"{self.pilot_base}/sid/{_slug(auth or 'local')}/{_slug(id_type or 'id')}"

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        cfg = cls()
        cfg.default_tz = os.environ.get("BRIDGE_DEFAULT_TZ", cfg.default_tz)
        cfg.ssn_policy = os.environ.get("BRIDGE_SSN_POLICY", cfg.ssn_policy)
        cfg.pilot_base = os.environ.get("BRIDGE_PILOT_BASE", cfg.pilot_base)
        return cfg
