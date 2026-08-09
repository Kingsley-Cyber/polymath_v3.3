"""Typed temporal contracts (T1 scaffolding — models only).

Normative design contract: CONTINUITY/TEMPORAL_CONTRACT_V1.md.

The temporal envelope records WHICH clocks a source carries and HOW each
date was selected. At this stage it is purely descriptive:

- non-authoritative — nothing permits or denies behavior based on envelope
  contents; the categorical release state (models/release_state.py) remains
  the sole normative authority for readiness decisions;
- additive — no adapter, extractor, storage layer, or retriever reads this
  contract yet (enforced by an import-isolation regression test);
- never-guessing — unknown stays unknown; date-only values never become
  invented UTC timestamps; file times never populate publication clocks;
- projection, not destruction — selecting a document clock marks a winner
  but NEVER deletes or rewrites losing candidates; conflicts stay visible;
- deterministic — selection reads only (method precedence rank, input
  order); provenance contents can never influence the selected clock.

Slice map: T1 (this module) → T2 adapter emission (shadow) → T3
post-extraction claim enrichment → T4 storage/hydration → T5 Query IR
clocks + recency. Runtime enforcement: DISABLED.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

TEMPORAL_CONTRACT_SCHEMA_VERSION = "polymath.temporal_contract.v1"

#: Owner-mandated envelope clocks — exact set and canonical order.
TEMPORAL_CLOCKS: tuple[str, ...] = (
    "source_published_at",
    "source_recorded_at",
    "original_work_published_at",
    "edition_published_at",
    "electronic_edition_published_at",
    "source_updated_at",
    "transcript_generated_at",
    "source_acquired_at",
    "system_recorded_at",
)

#: Query-facing semantic clock families. The envelope provides source
#: detail; Query IR (T5) selects one of these families per query.
QUERY_CLOCK_FAMILIES: tuple[str, ...] = (
    "valid_time",
    "source_time",
    "transaction_time",
)

#: Capture methods that carry NO publication authority (mirrors the
#: T-HOOK-3 de-conflation rule in services/ingestion/bibliographic.py).
#: These are recorded as candidates but can never be selected.
FILE_TIME_METHODS: frozenset[str] = frozenset(
    {
        "frontmatter_created",
        "frontmatter_modified",
        "docx_core_created",
        "docx_core_modified",
        "pdf_creation_date",
        "pdf_mod_date",
        "filesystem_mtime",
    }
)

Granularity = Literal[
    "second", "minute", "hour", "day", "month", "quarter", "year", "unknown"
]
Certainty = Literal["exact", "approximate", "inferred", "unknown"]
TimezoneStatus = Literal["known", "date_only", "not_applicable", "unknown"]
Confidence = Literal["high", "medium", "low"]
SelectionStatus = Literal[
    "selected", "conflicting", "unknown", "file_date_only", "unparseable"
]

_ISO_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


class TemporalInterval(BaseModel):
    """Half-open interval [start, end) with granularity and certainty.

    ``end`` is EXCLUSIVE: year 2024 → [2024-01-01, 2025-01-01). Open ends
    stay None — an absent bound is never fabricated. Date-only inputs keep
    ``date`` values end-to-end; they never become invented UTC datetimes.
    """

    model_config = ConfigDict(frozen=True)

    start: date | datetime | None = None
    end: date | datetime | None = None
    granularity: Granularity = "unknown"
    certainty: Certainty = "unknown"
    timezone_status: TimezoneStatus = "unknown"


class TemporalCandidate(BaseModel):
    """One observed date with its clock, method, and provenance.

    Conflicting candidates remain present in the envelope; ``selected``
    marks the projection winner without deleting losers.
    """

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    clock: str
    interval: TemporalInterval
    raw_value: str | None = None
    method: str
    confidence: Confidence
    provenance: dict[str, Any] = {}
    selected: bool = False
    conflict_group: str | None = None

    @field_validator("clock")
    @classmethod
    def _known_clock(cls, value: str) -> str:
        if value not in TEMPORAL_CLOCKS:
            raise ValueError(f"unknown temporal clock: {value!r}")
        return value


class TemporalEnvelope(BaseModel):
    """Canonical multi-clock temporal envelope for one source document.

    All clocks nullable — unknown stays null. ``selected_document_clock``
    is a deterministic projection under the named ``selection_policy``;
    it never rewrites or drops candidate evidence.
    """

    model_config = ConfigDict(frozen=True)

    source_published_at: TemporalInterval | None = None
    source_recorded_at: TemporalInterval | None = None
    original_work_published_at: TemporalInterval | None = None
    edition_published_at: TemporalInterval | None = None
    electronic_edition_published_at: TemporalInterval | None = None
    source_updated_at: TemporalInterval | None = None
    transcript_generated_at: TemporalInterval | None = None
    source_acquired_at: TemporalInterval | None = None
    system_recorded_at: TemporalInterval | None = None

    candidates: tuple[TemporalCandidate, ...] = ()
    selected_document_clock: str | None = None
    selection_policy: str
    selection_status: SelectionStatus

    @field_validator("selected_document_clock")
    @classmethod
    def _known_selected_clock(cls, value: str | None) -> str | None:
        if value is not None and value not in TEMPORAL_CLOCKS:
            raise ValueError(f"unknown temporal clock: {value!r}")
        return value


# ─── Interval normalization ────────────────────────────────────────────────


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def interval_from_date_parts(
    year: int,
    month: int | None = None,
    day: int | None = None,
    *,
    certainty: Certainty = "unknown",
) -> TemporalInterval:
    """Normalize date parts to a half-open interval.

    Year  → [Y-01-01, Y+1-01-01), granularity year.
    Month → [Y-M-01, next month start), granularity month.
    Day   → [Y-M-D, Y-M-(D+1)), granularity day.

    Bounds stay ``date`` values: date-only input never becomes an invented
    UTC timestamp. No bound inference beyond the stated granularity.
    """

    if month is not None and not (1 <= month <= 12):
        raise ValueError(f"month out of range: {month!r}")
    if day is not None and month is None:
        raise ValueError("day precision requires month")
    if day is not None:
        # Raises ValueError for invalid day-of-month — never silently fixed.
        start = date(year, month or 1, day)
        # Day end = next calendar day (ordinal arithmetic stays exact).
        end = date.fromordinal(start.toordinal() + 1)
        return TemporalInterval(
            start=start,
            end=end,
            granularity="day",
            certainty=certainty,
            timezone_status="date_only",
        )
    if month is not None:
        start = date(year, month, 1)
        return TemporalInterval(
            start=start,
            end=_add_months(start, 1),
            granularity="month",
            certainty=certainty,
            timezone_status="date_only",
        )
    return TemporalInterval(
        start=date(year, 1, 1),
        end=date(year + 1, 1, 1),
        granularity="year",
        certainty=certainty,
        timezone_status="date_only",
    )


def interval_from_iso(
    raw: str | None, *, certainty: Certainty = "unknown"
) -> TemporalInterval | None:
    """Parse ``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD`` into a half-open interval.

    Anything else — garbage, partial nonsense, or full datetime strings —
    returns None. T1 deliberately does NOT mint timestamps from date-only
    material, and sub-day parsing is reserved for later slices with an
    explicit timezone policy.
    """

    if not raw or not isinstance(raw, str):
        return None
    m = _ISO_DATE_RE.match(raw.strip())
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else None
    day = int(m.group(3)) if m.group(3) else None
    if month is not None and not (1 <= month <= 12):
        return None
    try:
        return interval_from_date_parts(year, month, day, certainty=certainty)
    except ValueError:
        return None


def interval_from_quarter(year: int, quarter: int) -> TemporalInterval:
    """Quarter q of year → [first day of quarter, first day of next quarter)."""

    if not (1 <= quarter <= 4):
        raise ValueError(f"quarter out of range: {quarter!r}")
    start_month = 3 * quarter - 2
    start = date(year, start_month, 1)
    return TemporalInterval(
        start=start,
        end=_add_months(start, 3),
        granularity="quarter",
        certainty="unknown",
        timezone_status="date_only",
    )


# ─── Deterministic selection ───────────────────────────────────────────────


def select_document_clock(
    envelope: TemporalEnvelope,
    precedence_methods: tuple[str, ...] | list[str],
) -> TemporalEnvelope:
    """Project the winning document clock under a fixed method precedence.

    Determinism contract: the decision reads ONLY each candidate's
    ``method`` rank in ``precedence_methods`` and its input order. Raw
    values, provenance dicts, and any other metadata can never influence
    the outcome — extra metadata cannot silently change the selected clock.

    Losing candidates are preserved verbatim (only ``selected`` flags
    move). Status semantics:

      unknown         no candidates at all
      file_date_only  candidates exist but every method is a file-time one
      unparseable     candidates exist, none eligible under the precedence
      conflicting     eligible candidates disagree on normalized interval
      selected        single unambiguous winner
    """

    precedence = tuple(precedence_methods)
    rank = {method: i for i, method in enumerate(precedence)}
    candidates = envelope.candidates

    if not candidates:
        return envelope.model_copy(
            update={
                "selected_document_clock": None,
                "selection_status": "unknown",
            }
        )

    eligible = [
        (index, cand)
        for index, cand in enumerate(candidates)
        if cand.method in rank
    ]
    if not eligible:
        if all(cand.method in FILE_TIME_METHODS for cand in candidates):
            status: SelectionStatus = "file_date_only"
        else:
            status = "unparseable"
        return envelope.model_copy(
            update={"selected_document_clock": None, "selection_status": status}
        )

    winner_index, winner = min(
        eligible, key=lambda pair: (rank[pair[1].method], pair[0])
    )

    # Conflicts are recorded, never normalized away: eligible candidates
    # disagreeing on clock or normalized interval mark the envelope
    # conflicting while the precedence winner still projects.
    winner_intervals = {
        (cand.clock, cand.interval.model_dump_json()) for _, cand in eligible
    }
    status = "conflicting" if len(winner_intervals) > 1 else "selected"

    updated = tuple(
        cand.model_copy(update={"selected": index == winner_index})
        for index, cand in enumerate(candidates)
    )
    return envelope.model_copy(
        update={
            "candidates": updated,
            "selected_document_clock": winner.clock,
            "selection_status": status,
        }
    )


# ─── Legacy compatibility projection ───────────────────────────────────────


def envelope_from_bibliographic(
    *,
    document_date: str | None,
    date_confidence: str | None,
    provenance: dict[str, Any] | None,
    selection_policy: str = "bibliographic_compatibility_v1",
) -> TemporalEnvelope:
    """Represent the existing T-HOOK-3 bibliographic fields as an envelope.

    Compatibility rule: ``document_date`` stays representable as a
    ``source_published_at`` candidate; unknown dates map to the honest
    status of the recorded reason code — never to a guessed value.
    """

    prov = dict(provenance or {})
    reason = str(prov.get("reason") or "")
    precision = prov.get("precision")
    if precision not in (None, "day", "month", "year"):
        precision = None

    candidates: tuple[TemporalCandidate, ...] = ()
    if document_date:
        interval = interval_from_iso(document_date, certainty="exact")
        if interval is not None:
            if precision and precision != interval.granularity:
                # The recorded precision wins over string-shape inference.
                interval = interval.model_copy(
                    update={"granularity": precision}  # type: ignore[arg-type]
                )
            confidence = (
                date_confidence if date_confidence in ("high", "medium", "low") else "low"
            )
            candidates = (
                TemporalCandidate(
                    candidate_id="time-candidate:1",
                    clock="source_published_at",
                    interval=interval,
                    raw_value=document_date,
                    method=str(prov.get("method") or "legacy_document_date"),
                    confidence=confidence,  # type: ignore[arg-type]
                    provenance=prov,
                    selected=False,
                ),
            )
            status: SelectionStatus = "selected"
            selected_clock: str | None = "source_published_at"
            return TemporalEnvelope(
                source_published_at=interval,
                candidates=candidates,
                selected_document_clock=selected_clock,
                selection_policy=selection_policy,
                selection_status=status,
            )

    if reason == "file_date_only":
        status = "file_date_only"
    elif reason == "unparseable_date":
        status = "unparseable"
    else:
        status = "unknown"
    return TemporalEnvelope(
        candidates=(),
        selected_document_clock=None,
        selection_policy=selection_policy,
        selection_status=status,
    )


# ─── Canonical serialization ───────────────────────────────────────────────


def canonical_envelope_json(envelope: TemporalEnvelope) -> str:
    """Deterministic envelope serialization.

    Sorted keys, compact separators, UTF-8 preserved. Same envelope ⇒
    byte-identical output; required for roundtrip and hash stability.
    """

    return json.dumps(
        envelope.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


# ─── Policy registry validation (pure; loader wiring is a T2 concern) ────


_REGISTRY_REQUIRED_KEYS = frozenset(
    {
        "registry",
        "version",
        "authority",
        "owner_ratification_required",
        "source",
        "changes_require_new_version",
        "policy_note",
        "clocks",
        "query_clock_families",
        "interval_semantics",
        "global_rules",
        "source_policies",
    }
)

_GLOBAL_RULE_KEYS = frozenset(
    {
        "file_time_never_publication_time",
        "missing_dates_remain_null",
        "no_llm_date_inference",
        "no_network_date_resolution",
        "conflicting_candidates_retained",
        "selection_is_projection_not_destruction",
        "relative_without_anchor",
        "ambiguous_locale",
    }
)

_POLICY_ID_RE = re.compile(r"^[a-z0-9_]+_v\d+$")


def validate_temporal_policy_registry(payload: dict[str, Any]) -> None:
    """Structural validation for temporal_policy_registry.v1.json.

    Raises ValueError on any violation. Unknown ids are hard errors, never
    silent defaults — consistent with the house registry-loader rules.
    """

    if not isinstance(payload, dict):
        raise ValueError("temporal policy registry must be a JSON object")
    if set(payload) != _REGISTRY_REQUIRED_KEYS:
        raise ValueError("temporal policy registry keys are not exact")
    if payload.get("registry") != "temporal_policy":
        raise ValueError("temporal policy registry has the wrong identity")
    if payload.get("version") != "v1":
        raise ValueError("temporal policy registry version must be v1")
    if payload.get("authority") != "executor-proposed, owner-ratifiable":
        raise ValueError("temporal policy registry authority mark is missing")
    if payload.get("owner_ratification_required") is not True:
        raise ValueError("temporal policy registry must remain owner-ratifiable")
    if payload.get("changes_require_new_version") is not True:
        raise ValueError("temporal policy registry must require monotonic versions")

    clocks = payload.get("clocks")
    if list(clocks or []) != list(TEMPORAL_CLOCKS):
        raise ValueError("registry clocks must match TEMPORAL_CLOCKS exactly")
    families = payload.get("query_clock_families")
    if list(families or []) != list(QUERY_CLOCK_FAMILIES):
        raise ValueError("registry query_clock_families must match exactly")
    if payload.get("interval_semantics") != "half_open":
        raise ValueError("registry interval_semantics must be half_open")

    rules = payload.get("global_rules")
    if not isinstance(rules, dict) or set(rules) != _GLOBAL_RULE_KEYS:
        raise ValueError("registry global_rules keys are not exact")
    for rule_key in (
        "file_time_never_publication_time",
        "missing_dates_remain_null",
        "no_llm_date_inference",
        "no_network_date_resolution",
        "conflicting_candidates_retained",
        "selection_is_projection_not_destruction",
    ):
        if rules.get(rule_key) is not True:
            raise ValueError(f"global rule {rule_key} must be true")
    if rules.get("relative_without_anchor") != "unresolved_anchor":
        raise ValueError("relative_without_anchor must resolve to unresolved_anchor")
    if rules.get("ambiguous_locale") != "do_not_guess":
        raise ValueError("ambiguous_locale must be do_not_guess")

    policies = payload.get("source_policies")
    if not isinstance(policies, dict) or not policies:
        raise ValueError("registry source_policies must be a non-empty object")
    for source_type, policy in policies.items():
        if not isinstance(policy, dict):
            raise ValueError(f"policy for {source_type} must be an object")
        policy_id = policy.get("policy_id")
        if not isinstance(policy_id, str) or not _POLICY_ID_RE.match(policy_id):
            raise ValueError(f"policy {source_type} must carry a versioned policy_id")
        if policy.get("target_clock") not in TEMPORAL_CLOCKS:
            raise ValueError(f"policy {source_type} target_clock is unknown")
        ladder = policy.get("ladder")
        if not isinstance(ladder, list) or not ladder:
            raise ValueError(f"policy {source_type} ladder must be non-empty")
        seen_methods: set[str] = set()
        for rung in ladder:
            if not isinstance(rung, dict):
                raise ValueError(f"policy {source_type} ladder rungs must be objects")
            method = rung.get("method")
            if not isinstance(method, str) or not method:
                raise ValueError(f"policy {source_type} ladder rung missing method")
            if method in FILE_TIME_METHODS:
                raise ValueError(
                    f"policy {source_type} ladder may not include file-time "
                    f"method {method}"
                )
            if method in seen_methods:
                raise ValueError(f"policy {source_type} ladder repeats {method}")
            seen_methods.add(method)
            if rung.get("confidence") not in ("high", "medium", "low"):
                raise ValueError(f"policy {source_type} rung {method} needs confidence")
