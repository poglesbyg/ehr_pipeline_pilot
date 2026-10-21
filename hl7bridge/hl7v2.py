"""HL7 v2.x message model: parse ER7 text, address values by path, escape, serialize, acknowledge.

Values are held fully decomposed (field -> repetition -> component -> subcomponent) and
unescaped, so mapping code never has to think about delimiters or escape sequences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

MLLP_START = "\x0b"
MLLP_END = "\x1c\x0d"

# HL7 table 0357 - message error condition codes used in ERR-3.
ERROR_CODES = {
    "0": "Message accepted",
    "100": "Segment sequence error",
    "101": "Required field missing",
    "102": "Data type error",
    "103": "Table value not found",
    "200": "Unsupported message type",
    "201": "Unsupported event code",
    "202": "Unsupported processing id",
    "203": "Unsupported version id",
    "204": "Unknown key identifier",
    "205": "Duplicate key identifier",
    "207": "Application internal error",
}

_SEGMENT_NAME = re.compile(r"^[A-Z][A-Z0-9]{2}$")
_PATH = re.compile(
    r"^(?P<seg>[A-Z][A-Z0-9]{2})(?:\[(?P<occ>\d+)\])?-(?P<field>\d+)"
    r"(?:\((?P<rep>\d+)\))?(?:\.(?P<comp>\d+))?(?:\.(?P<sub>\d+))?$"
)

Component = list[str]
Repetition = list[Component]
Field = list[Repetition]


class HL7ParseError(ValueError):
    """Text could not be parsed as an HL7 v2 message."""


@dataclass(frozen=True)
class Delimiters:
    field: str = "|"
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"

    @classmethod
    def from_msh(cls, line: str) -> "Delimiters":
        if not line.startswith("MSH") or len(line) < 8:
            raise HL7ParseError("message must begin with an MSH segment")
        fs = line[3]
        end = line.find(fs, 4)
        chars = line[4 : end if end != -1 else None]
        if len(chars) < 4 or len(set(chars[:4] + fs)) != 5:
            raise HL7ParseError(f"invalid MSH-2 encoding characters {chars!r}")
        return cls(fs, chars[0], chars[1], chars[2], chars[3])

    @property
    def encoding_chars(self) -> str:
        return self.component + self.repetition + self.escape + self.subcomponent

    def unescape(self, s: str) -> str:
        e = self.escape
        if e not in s:
            return s
        out: list[str] = []
        i = 0
        while i < len(s):
            if s[i] != e:
                out.append(s[i])
                i += 1
                continue
            j = s.find(e, i + 1)
            if j == -1:
                out.append(s[i:])
                break
            out.append(self._decode(s[i + 1 : j], s[i : j + 1]))
            i = j + 1
        return "".join(out)

    def _decode(self, seq: str, literal: str) -> str:
        simple = {
            "F": self.field, "S": self.component, "T": self.subcomponent,
            "R": self.repetition, "E": self.escape, ".br": "\n", "H": "", "N": "",
        }
        if seq in simple:
            return simple[seq]
        if seq.startswith("X") and len(seq) > 1 and len(seq) % 2 == 1:
            try:
                return bytes.fromhex(seq[1:]).decode("utf-8", errors="replace")
            except ValueError:
                return literal
        return literal

    def escape_text(self, s: str) -> str:
        e = self.escape
        s = s.replace(e, f"{e}E{e}")
        for ch, code in ((self.field, "F"), (self.component, "S"),
                         (self.subcomponent, "T"), (self.repetition, "R")):
            s = s.replace(ch, f"{e}{code}{e}")
        return s.replace("\r\n", "\n").replace("\n", f"{e}.br{e}")


class Rep:
    """One repetition of a field, with 1-based component/subcomponent access."""

    __slots__ = ("parts",)

    def __init__(self, parts: Repetition):
        self.parts = parts

    def c(self, comp: int, sub: int = 1) -> str:
        try:
            return self.parts[comp - 1][sub - 1]
        except IndexError:
            return ""

    def __str__(self) -> str:
        return self.c(1)

    def __bool__(self) -> bool:
        return any(s for comp in self.parts for s in comp)

    def __repr__(self) -> str:
        return f"Rep({self.parts!r})"


def _parse_field(raw: str, d: Delimiters) -> Field:
    if raw == "":
        return []
    return [
        [[d.unescape(s) for s in comp.split(d.subcomponent)] for comp in rep.split(d.component)]
        for rep in raw.split(d.repetition)
    ]


def _format_field(value: Field, d: Delimiters) -> str:
    return d.repetition.join(
        d.component.join(d.subcomponent.join(d.escape_text(s) for s in comp) for comp in rep)
        for rep in value
    )


class Segment:
    """A segment. ``fields[0]`` is the segment name so field numbers index directly."""

    def __init__(self, name: str, fields: list):
        self.name = name
        self.fields = fields

    @classmethod
    def parse(cls, line: str, d: Delimiters) -> "Segment":
        parts = line.split(d.field)
        name = parts[0]
        if not _SEGMENT_NAME.match(name):
            raise HL7ParseError(f"invalid segment name {name!r}")
        if name == "MSH":
            # MSH-1 is the field separator itself and MSH-2 is taken literally.
            fields = [name, [[[d.field]]], [[[parts[1]]]]]
            fields += [_parse_field(p, d) for p in parts[2:]]
        else:
            fields = [name] + [_parse_field(p, d) for p in parts[1:]]
        return cls(name, fields)

    def reps(self, n: int) -> list[Rep]:
        if n >= len(self.fields) or n < 1:
            return []
        return [Rep(r) for r in self.fields[n]]

    def rep(self, n: int, index: int = 0) -> Rep:
        reps = self.reps(n)
        return reps[index] if index < len(reps) else Rep([])

    def get(self, n: int, comp: int = 1, sub: int = 1, rep: int = 0) -> str:
        return self.rep(n, rep).c(comp, sub)

    def __getitem__(self, n: int) -> str:
        return self.get(n)

    def set_field(self, n: int, value: Field) -> None:
        while len(self.fields) <= n:
            self.fields.append([])
        self.fields[n] = value

    def raw(self, n: int, d: Delimiters) -> str:
        """ER7 text of field ``n`` (escaped), suitable for copying into another message."""
        if self.name == "MSH" and n in (1, 2):
            return self.fields[n][0][0][0]
        return _format_field(self.fields[n], d) if n < len(self.fields) else ""

    def to_er7(self, d: Delimiters) -> str:
        if self.name == "MSH":
            rest = "".join(d.field + _format_field(f, d) for f in self.fields[3:])
            return f"MSH{d.field}{d.encoding_chars}{rest}"
        return d.field.join([self.name] + [_format_field(f, d) for f in self.fields[1:]])

    def __repr__(self) -> str:
        return f"Segment({self.to_er7(Delimiters())!r})"


class Message:
    def __init__(self, segments: list[Segment], delimiters: Delimiters):
        self.segments = segments
        self.delimiters = delimiters

    @classmethod
    def parse(cls, text: str) -> "Message":
        text = text.strip().strip(MLLP_START).rstrip("\x1c\r\n")
        lines = [ln for ln in re.split(r"\r\n|\r|\n", text) if ln.strip()]
        if not lines:
            raise HL7ParseError("empty message")
        d = Delimiters.from_msh(lines[0])
        return cls([Segment.parse(ln, d) for ln in lines], d)

    # -- navigation -------------------------------------------------------
    def segment(self, name: str, occurrence: int = 1) -> Segment | None:
        found = [s for s in self.segments if s.name == name]
        return found[occurrence - 1] if len(found) >= occurrence else None

    def all(self, name: str) -> list[Segment]:
        return [s for s in self.segments if s.name == name]

    def get(self, path: str) -> str:
        """Value at a path like ``PID-5.1``, ``OBX[2]-5``, or ``PID-3(2).4.2``."""
        m = _PATH.match(path)
        if not m:
            raise ValueError(f"invalid HL7 path {path!r}")
        seg = self.segment(m["seg"], int(m["occ"] or 1))
        if seg is None:
            return ""
        return seg.get(int(m["field"]), int(m["comp"] or 1), int(m["sub"] or 1),
                       int(m["rep"] or 1) - 1)

    def groups(self, head: str, members: set[str]) -> Iterator[tuple[Segment, list[Segment]]]:
        """Yield each ``head`` segment with the ``members`` segments that follow it."""
        current: Segment | None = None
        children: list[Segment] = []
        for seg in self.segments:
            if seg.name == head:
                if current is not None:
                    yield current, children
                current, children = seg, []
            elif current is not None and seg.name in members:
                children.append(seg)
            elif current is not None and seg.name not in members:
                yield current, children
                current, children = None, []
        if current is not None:
            yield current, children

    def following(self, seg: Segment, names: set[str]) -> list[Segment]:
        """Segments named in ``names`` immediately after ``seg`` (e.g. NTEs after an OBX)."""
        idx = next(i for i, s in enumerate(self.segments) if s is seg)
        out = []
        for s in self.segments[idx + 1 :]:
            if s.name not in names:
                break
            out.append(s)
        return out

    # -- header shortcuts -------------------------------------------------
    @property
    def msh(self) -> Segment:
        return self.segments[0]

    @property
    def message_code(self) -> str:
        return self.msh.get(9, 1)

    @property
    def trigger_event(self) -> str:
        return self.msh.get(9, 2)

    @property
    def type_label(self) -> str:
        return f"{self.message_code}^{self.trigger_event}".strip("^")

    @property
    def control_id(self) -> str:
        return self.msh.get(10)

    @property
    def version(self) -> str:
        return self.msh.get(12)

    @property
    def sending_app(self) -> str:
        return self.msh.get(3)

    @property
    def sending_facility(self) -> str:
        return self.msh.get(4)

    def to_er7(self, sep: str = "\r") -> str:
        return sep.join(s.to_er7(self.delimiters) for s in self.segments)

    def __str__(self) -> str:
        return self.to_er7("\n")


def build_ack(
    original: Message | None,
    code: str = "AA",
    text: str = "",
    error_code: str | None = None,
    *,
    app: str = "HL7BRIDGE",
    facility: str = "PILOT",
    now: datetime | None = None,
) -> Message:
    """Build an original-mode ACK (MSA, plus ERR when ``error_code`` is given).

    With ``original=None`` (the inbound text was unparseable) a generic reject is produced.
    """
    d = original.delimiters if original else Delimiters()
    esc = d.escape_text
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d%H%M%S%z")
    ctrl = "ACK" + now.strftime("%Y%m%d%H%M%S%f")
    f = d.field
    if original:
        msh = original.msh
        receiver_app, receiver_fac = msh.raw(3, d), msh.raw(4, d)
        sender_app, sender_fac = msh.raw(5, d) or esc(app), msh.raw(6, d) or esc(facility)
        trigger, orig_ctrl = esc(original.trigger_event), esc(original.control_id)
        processing, version = msh.raw(11, d) or "P", msh.raw(12, d) or "2.5.1"
    else:
        receiver_app = receiver_fac = trigger = orig_ctrl = ""
        sender_app, sender_fac = esc(app), esc(facility)
        processing, version = "P", "2.5.1"
    lines = [
        f.join(["MSH", d.encoding_chars, sender_app, sender_fac, receiver_app, receiver_fac, ts, "",
                f"ACK{d.component}{trigger}{d.component}ACK", ctrl, processing, version]),
        f.join(["MSA", code, orig_ctrl, esc(text[:80])]),
    ]
    if error_code:
        label = ERROR_CODES.get(error_code, "")
        cwe = d.component.join([error_code, esc(label), "HL70357"])
        severity = "E" if code != "AA" else "W"
        lines.append(f.join(["ERR", "", "", cwe, severity, "", "", "", esc(text)]))
    return Message.parse("\r".join(lines))
