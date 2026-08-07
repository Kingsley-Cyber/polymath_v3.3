"""Typed value + temporal qualifier IR (#7, owner-ratified 2026-08-07).

Temporal and numeric information become QUALIFIERS on assertions — never a
predicate explosion (no LAUNCHES_ON / COSTS / HAS_LATENCY families):

    ENTITY ─ predicate ─ ENTITY/VALUE
                   ├── temporal {operator, value}
                   └── value    {value, unit, dimension}

Capture is deterministic and shape-based: fixed unit→dimension table, strong
date shapes, nearest governing preposition as the operator. Uncertain parses
abstain (None) — a wrong qualifier is worse than no qualifier.
"""
from __future__ import annotations

import re

VALUE_IR_RELEASE = "graphify-value-ir-v1"

_UNIT_DIMENSIONS: dict[str, str] = {
    "ms": "duration", "millisecond": "duration", "milliseconds": "duration",
    "s": "duration", "sec": "duration", "secs": "duration",
    "second": "duration", "seconds": "duration",
    "min": "duration", "mins": "duration", "minute": "duration", "minutes": "duration",
    "h": "duration", "hr": "duration", "hrs": "duration", "hour": "duration", "hours": "duration",
    "day": "duration", "days": "duration", "week": "duration", "weeks": "duration",
    "month": "duration", "months": "duration", "year": "duration", "years": "duration",
    "%": "ratio", "percent": "ratio", "percentage points": "ratio",
    "hz": "frequency", "khz": "frequency", "mhz": "frequency", "rpm": "frequency", "fps": "frequency",
    "b": "data", "kb": "data", "mb": "data", "gb": "data", "tb": "data",
    "byte": "data", "bytes": "data",
    "m": "length", "cm": "length", "mm": "length", "km": "length",
    "kg": "mass", "g": "mass", "mg": "mass",
}

_VALUE_RE = re.compile(
    r"^\s*(?:~|about |approximately |roughly |< ?|> ?|≤ ?|≥ ?)?"
    r"(?P<value>\d{1,3}(?:[ ,]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>[A-Za-zµ%][\w %./-]{0,40}?)?\s*$"
)

_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)
_WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
_DATE_SHAPE = (
    r"(?:" + "|".join(_MONTHS) + r")\s+\d{1,2}(?:,\s*\d{4})?"
    r"|\d{4}-\d{2}-\d{2}"
    r"|Q[1-4]\s+\d{4}"
    r"|(?:" + "|".join(_WEEKDAYS) + r")(?:\s+(?:noon|midnight|morning|evening|afternoon))?"
    r"|\d{4}"
)
_TEMPORAL_RE = re.compile(
    r"\b(?P<operator>on|by|before|after|during|at|in|until|since)\s+"
    r"(?P<value>" + _DATE_SHAPE + r")\b",
)


def parse_typed_value(surface: str) -> dict[str, object] | None:
    """Parse '420 ms' → {value: 420.0, unit: 'ms', dimension: 'duration'}.

    Unknown units keep the surface unit with dimension 'unknown'; a bare
    number carries no unit. Non-numeric surfaces abstain.
    """
    match = _VALUE_RE.match(surface.strip())
    if not match:
        return None
    raw_value = match.group("value").replace(",", "").replace(" ", "")
    try:
        value = float(raw_value)
    except ValueError:
        return None
    unit = (match.group("unit") or "").strip().rstrip(".")
    if not unit:
        return {"value": value, "unit": "", "dimension": "count", "ir_release": VALUE_IR_RELEASE}
    dimension = _UNIT_DIMENSIONS.get(unit.casefold(), "unknown")
    return {"value": value, "unit": unit, "dimension": dimension, "ir_release": VALUE_IR_RELEASE}


def extract_temporal_qualifier(evidence_text: str) -> dict[str, str] | None:
    """First strong prep+date-shape adjunct in the evidence, else None.

    {'operator': 'by', 'value': 'July 1, 2026'} — the operator is the
    governing preposition; interpretation (deadline vs event time) is the
    query layer's job, never encoded as a predicate.
    """
    match = _TEMPORAL_RE.search(evidence_text)
    if not match:
        return None
    return {
        "operator": match.group("operator"),
        "value": match.group("value"),
        "ir_release": VALUE_IR_RELEASE,
    }
