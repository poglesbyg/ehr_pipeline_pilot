"""Terminology tables and datatype conversions shared by the HL7 v2 -> FHIR mappings.

Table choices follow the HL7 v2-to-FHIR Implementation Guide concept maps where one exists.
"""

from __future__ import annotations

import re
from datetime import datetime

V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
V3_ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
V3_MARITAL = "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus"
V3_INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
V3_PARTICIPATION = "http://terminology.hl7.org/CodeSystem/v3-ParticipationType"
V3_DATA_OPERATION = "http://terminology.hl7.org/CodeSystem/v3-DataOperation"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
V2_0074 = "http://terminology.hl7.org/CodeSystem/v2-0074"
CDC_RACE_ETH = "urn:oid:2.16.840.1.113883.6.238"
UCUM = "http://unitsofmeasure.org"
US_SSN = "http://hl7.org/fhir/sid/us-ssn"
US_NPI = "http://hl7.org/fhir/sid/us-npi"
US_CORE = "http://hl7.org/fhir/us/core/StructureDefinition"

GENDER = {"M": "male", "F": "female", "O": "other", "A": "other", "U": "unknown", "N": "unknown"}

PATIENT_CLASS = {
    "I": ("IMP", "inpatient encounter"),
    "O": ("AMB", "ambulatory"),
    "R": ("AMB", "ambulatory"),
    "E": ("EMER", "emergency"),
    "P": ("PRENC", "pre-admission"),
}

# HL7 table 0002 -> v3 MaritalStatus
MARITAL = {
    "S": ("S", "Never Married"), "M": ("M", "Married"), "D": ("D", "Divorced"),
    "W": ("W", "Widowed"), "A": ("L", "Legally Separated"), "E": ("L", "Legally Separated"),
    "N": ("A", "Annulled"), "I": ("I", "Interlocutory"), "P": ("T", "Domestic partner"),
    "R": ("T", "Domestic partner"), "B": ("U", "unmarried"), "U": ("UNK", "unknown"),
}

INTERPRETATION = {
    "H": "High", "L": "Low", "HH": "Critical high", "LL": "Critical low",
    "N": "Normal", "A": "Abnormal", "AA": "Critical abnormal",
}

# OBX-11 -> Observation.status
OBSERVATION_STATUS = {
    "F": "final", "P": "preliminary", "C": "corrected", "R": "preliminary",
    "I": "registered", "X": "cancelled", "D": "entered-in-error", "W": "entered-in-error",
}

# OBR-25 -> DiagnosticReport.status
REPORT_STATUS = {
    "F": "final", "P": "preliminary", "C": "corrected", "R": "partial",
    "A": "partial", "I": "registered", "O": "registered", "X": "cancelled",
}

NAME_USE = {"L": "official", "D": "usual", "M": "maiden", "N": "nickname", "A": "anonymous"}

ADDRESS_USE = {"H": ("home", None), "B": ("work", None), "O": ("work", None),
               "M": (None, "postal"), "C": ("temp", None), "BA": ("old", None)}

# PV1-36 discharge disposition (table 0112) -> FHIR discharge-disposition
DISCHARGE_DISPOSITION = {
    "01": ("home", "Home"), "02": ("other-hcf", "Other healthcare facility"),
    "03": ("snf", "Skilled nursing facility"), "07": ("aadvice", "Left against advice"),
    "20": ("exp", "Expired"), "51": ("hosp", "Hospice"),
}

ALLERGY_CATEGORY = {"DA": "medication", "MA": "medication", "FA": "food", "EA": "environment",
                    "AA": "environment", "PA": "environment"}
ALLERGY_SEVERITY = {"SV": "severe", "MO": "moderate", "MI": "mild"}

OMB_RACE = {
    "1002-5": "American Indian or Alaska Native", "2028-9": "Asian",
    "2054-5": "Black or African American", "2076-8": "Native Hawaiian or Other Pacific Islander",
    "2106-3": "White",
}
OMB_ETHNICITY = {"2135-2": "Hispanic or Latino", "2186-5": "Not Hispanic or Latino"}

CODING_SYSTEMS = {
    "LN": "http://loinc.org",
    "SCT": "http://snomed.info/sct",
    "SNM": "http://snomed.info/sct",
    "I10": "http://hl7.org/fhir/sid/icd-10-cm",
    "I10C": "http://hl7.org/fhir/sid/icd-10-cm",
    "ICD10CM": "http://hl7.org/fhir/sid/icd-10-cm",
    "C4": "http://www.ama-assn.org/go/cpt",
    "CPT": "http://www.ama-assn.org/go/cpt",
    "RXN": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "RXNORM": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "NDFRT": "http://hl7.org/fhir/ndfrt",
    "UCUM": UCUM,
    "CDCREC": CDC_RACE_ETH,
    "HL70005": CDC_RACE_ETH,
    "HL70189": CDC_RACE_ETH,
}

IDENTIFIER_TYPE_DISPLAY = {
    "MR": "Medical record number", "NI": "National unique individual identifier",
    "SS": "Social Security number", "PI": "Patient internal identifier",
    "NPI": "National provider identifier", "VN": "Visit number", "PRN": "Provider number",
    "FILL": "Filler Identifier", "PLAC": "Placer Identifier",
}


_TS = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\.\d{1,4})?([+-]\d{4})?$")


def to_fhir_datetime(ts: str, default_tz: str = "+00:00") -> str | None:
    """HL7 DTM/TS -> FHIR dateTime, preserving the sender's precision.

    FHIR requires a zone offset whenever a time is present; ``default_tz`` supplies the
    sending facility's offset when the HL7 value omits one.
    """
    ts = ts.strip()
    if not ts:
        return None
    m = _TS.match(ts)
    if not m:
        raise ValueError(f"invalid HL7 timestamp {ts!r}")
    y, mo, d, h, mi, s, frac, tz = m.groups()
    datetime(int(y), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(s or 0))
    if mo is None:
        return y
    if d is None:
        return f"{y}-{mo}"
    date = f"{y}-{mo}-{d}"
    if h is None:
        return date
    offset = f"{tz[:3]}:{tz[3:]}" if tz else default_tz
    return f"{date}T{h}:{mi or '00'}:{s or '00'}{frac or ''}{offset}"


def to_fhir_date(ts: str) -> str | None:
    value = to_fhir_datetime(ts[:8]) if ts.strip() else None
    return value


def number(text: str) -> int | float:
    """Parse an HL7 NM value, keeping integers as integers."""
    text = text.strip()
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    return float(text)
