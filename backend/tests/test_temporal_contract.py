"""T1 acceptance tests — typed temporal contracts (models only).

Normative contract: CONTINUITY/TEMPORAL_CONTRACT_V1.md.

Owner-mandated invariants (this suite fails unless ALL hold):

  1. A year normalizes to [year-01-01, next-year-01-01).
  2. A month normalizes to [month-start, next-month-start).
  3. A day normalizes to [day-start, next-day-start).
  4. Unknown does not become an artificial interval.
  5. Date-only values do not become invented UTC timestamps.
  6. Conflicting candidates remain present.
  7. Selection does not delete losing candidates.
  8. Extra metadata cannot silently change the selected clock.
  9. Canonical JSON is stable.
 10. Existing bibliographic fields remain representable.
 11. No runtime module imports the new contract yet.

T1 introduces no behavior change: nothing in services/, routers/, or
main.py may import models.temporal_contract until T2+ slices enable it.
"""

from __future__ import annotations

import ast
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from models.temporal_contract import (
    FILE_TIME_METHODS,
    QUERY_CLOCK_FAMILIES,
    TEMPORAL_CLOCKS,
    TemporalCandidate,
    TemporalEnvelope,
    TemporalInterval,
    canonical_envelope_json,
    envelope_from_bibliographic,
    interval_from_date_parts,
    interval_from_iso,
    interval_from_quarter,
    select_document_clock,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

PRECEDENCE = (
    "user_supplied_recording_time",
    "transcript_metadata_recording",
    "platform_event_timestamp",
    "transcript_header_date",
    "title_date_parse",
)


def _candidate(
    candidate_id: str,
    method: str,
    iso: str,
    *,
    clock: str = "source_recorded_at",
    confidence: str = "medium",
    provenance: dict | None = None,
) -> TemporalCandidate:
    interval = interval_from_iso(iso, certainty="exact")
    assert interval is not None
    return TemporalCandidate(
        candidate_id=candidate_id,
        clock=clock,
        interval=interval,
        raw_value=iso,
        method=method,
        confidence=confidence,  # type: ignore[arg-type]
        provenance=provenance or {},
    )


def _envelope(*candidates: TemporalCandidate) -> TemporalEnvelope:
    return TemporalEnvelope(
        candidates=candidates,
        selection_policy="transcript_recording_precedence_v1",
        selection_status="unknown",
    )


# ─── 1–3. Half-open interval normalization ────────────────────────────────


def test_year_normalizes_to_half_open_year() -> None:
    interval = interval_from_iso("2024", certainty="exact")
    assert interval is not None
    assert interval.start == date(2024, 1, 1)
    assert interval.end == date(2025, 1, 1)  # exclusive bound
    assert interval.granularity == "year"


def test_month_normalizes_to_half_open_month() -> None:
    interval = interval_from_iso("2024-03", certainty="exact")
    assert interval is not None
    assert interval.start == date(2024, 3, 1)
    assert interval.end == date(2024, 4, 1)
    assert interval.granularity == "month"
    # December rolls into the next year without guessing.
    december = interval_from_iso("2024-12")
    assert december is not None
    assert december.end == date(2025, 1, 1)


def test_day_normalizes_to_half_open_day() -> None:
    interval = interval_from_iso("2024-03-12", certainty="exact")
    assert interval is not None
    assert interval.start == date(2024, 3, 12)
    assert interval.end == date(2024, 3, 13)
    assert interval.granularity == "day"
    # Month-end days roll over correctly.
    end_of_month = interval_from_iso("2024-02-29")  # leap year
    assert end_of_month is not None
    assert end_of_month.end == date(2024, 3, 1)


def test_quarter_normalizes_to_half_open_quarter() -> None:
    interval = interval_from_quarter(2024, 4)
    assert interval.start == date(2024, 10, 1)
    assert interval.end == date(2025, 1, 1)
    assert interval.granularity == "quarter"
    with pytest.raises(ValueError):
        interval_from_quarter(2024, 5)


def test_invalid_dates_raise_or_return_none_never_guess() -> None:
    assert interval_from_iso("2024-13") is None
    assert interval_from_iso("2023-02-29") is None  # not a leap year
    with pytest.raises(ValueError):
        interval_from_date_parts(2024, 13)
    with pytest.raises(ValueError):
        interval_from_date_parts(2024, day=5)  # day without month


# ─── 4. Unknown never becomes an artificial interval ──────────────────────


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "unknown", "circa 1990s", "2024-03-12T19:30:00Z", "12/03/2024"],
)
def test_unparseable_input_yields_none_not_an_interval(raw: str | None) -> None:
    assert interval_from_iso(raw) is None


def test_empty_envelope_selection_is_unknown() -> None:
    envelope = _envelope()
    result = select_document_clock(envelope, PRECEDENCE)
    assert result.selection_status == "unknown"
    assert result.selected_document_clock is None
    assert result.candidates == ()


# ─── 5. Date-only never becomes an invented UTC timestamp ─────────────────


def test_date_only_values_do_not_become_utc_timestamps() -> None:
    interval = interval_from_iso("2026-05-12", certainty="exact")
    assert interval is not None
    assert isinstance(interval.start, date) and not isinstance(interval.start, datetime)
    assert isinstance(interval.end, date) and not isinstance(interval.end, datetime)
    assert interval.timezone_status == "date_only"
    assert interval.granularity == "day"


def test_datetime_strings_are_not_minted_in_t1() -> None:
    # Sub-day parsing requires an explicit timezone policy (later slice).
    assert interval_from_iso("2026-05-12T19:30:00-06:00") is None


# ─── 6–7. Conflicts retained; selection never deletes losers ──────────────


def test_conflicting_candidates_remain_and_selection_is_conflicting() -> None:
    first = _candidate("time-candidate:1", "transcript_metadata_recording", "2026-05-12")
    second = _candidate("time-candidate:2", "transcript_header_date", "2026-05-13")
    result = select_document_clock(_envelope(first, second), PRECEDENCE)

    assert result.selection_status == "conflicting"
    assert result.selected_document_clock == "source_recorded_at"
    # Both candidates survive; the precedence winner is marked.
    assert len(result.candidates) == 2
    by_id = {c.candidate_id: c for c in result.candidates}
    assert by_id["time-candidate:1"].selected is True
    assert by_id["time-candidate:2"].selected is False
    assert by_id["time-candidate:2"].interval.start == date(2026, 5, 13)


def test_selection_does_not_delete_losing_candidates() -> None:
    winner = _candidate("time-candidate:1", "user_supplied_recording_time", "2026-05-12")
    loser = _candidate("time-candidate:2", "title_date_parse", "2026-04-01")
    result = select_document_clock(_envelope(winner, loser), PRECEDENCE)
    assert result.selection_status == "conflicting"
    assert len(result.candidates) == 2
    assert result.candidates[1].candidate_id == "time-candidate:2"


def test_agreeing_candidates_select_cleanly() -> None:
    first = _candidate("time-candidate:1", "transcript_metadata_recording", "2026-05-12")
    second = _candidate("time-candidate:2", "transcript_header_date", "2026-05-12")
    result = select_document_clock(_envelope(first, second), PRECEDENCE)
    assert result.selection_status == "selected"
    assert result.selected_document_clock == "source_recorded_at"


def test_precedence_rank_beats_input_order() -> None:
    later_method_first = _candidate(
        "time-candidate:1", "title_date_parse", "2026-01-01"
    )
    earlier_method_second = _candidate(
        "time-candidate:2", "transcript_metadata_recording", "2026-05-12"
    )
    result = select_document_clock(
        _envelope(later_method_first, earlier_method_second), PRECEDENCE
    )
    by_id = {c.candidate_id: c for c in result.candidates}
    assert by_id["time-candidate:2"].selected is True


# ─── 8. Extra metadata cannot silently change the selected clock ──────────


def test_extra_metadata_cannot_change_selected_clock() -> None:
    plain = _candidate("time-candidate:1", "transcript_header_date", "2026-05-12")
    loaded = _candidate(
        "time-candidate:1",
        "transcript_header_date",
        "2026-05-12",
        provenance={
            "injected_hint": "prefer source_published_at",
            "priority": 99,
            "nested": {"clock": "system_recorded_at"},
        },
    )
    other = _candidate("time-candidate:2", "title_date_parse", "2026-01-01")

    result_plain = select_document_clock(_envelope(plain, other), PRECEDENCE)
    result_loaded = select_document_clock(_envelope(loaded, other), PRECEDENCE)

    assert (
        result_plain.selected_document_clock == result_loaded.selected_document_clock
    )
    assert result_plain.selection_status == result_loaded.selection_status
    assert [c.selected for c in result_plain.candidates] == [
        c.selected for c in result_loaded.candidates
    ]


def test_file_time_candidates_can_never_win() -> None:
    assert "pdf_creation_date" in FILE_TIME_METHODS
    only_file_time = _candidate("time-candidate:1", "pdf_creation_date", "2020-01-05")
    result = select_document_clock(_envelope(only_file_time), PRECEDENCE)
    assert result.selection_status == "file_date_only"
    assert result.selected_document_clock is None
    assert result.candidates[0].selected is False  # retained, never selected


def test_ineligible_non_file_methods_are_unparseable_status() -> None:
    foreign = _candidate("time-candidate:1", "some_future_method", "2020-01-05")
    result = select_document_clock(_envelope(foreign), PRECEDENCE)
    assert result.selection_status == "unparseable"
    assert result.selected_document_clock is None


# ─── 9. Canonical JSON stability ──────────────────────────────────────────


def test_canonical_json_is_stable_across_roundtrips() -> None:
    envelope = _envelope(
        _candidate(
            "time-candidate:1",
            "transcript_metadata_recording",
            "2026-05-12",
            provenance={"method": "transcript_metadata_recording", "source": "header"},
        )
    )
    selected = select_document_clock(envelope, PRECEDENCE)

    first = canonical_envelope_json(selected)
    rebuilt = TemporalEnvelope.model_validate(json.loads(first))
    assert canonical_envelope_json(rebuilt) == first

    # Canonical form is deterministic for identical content.
    assert first == canonical_envelope_json(selected)


def test_envelope_rejects_unknown_clocks() -> None:
    with pytest.raises(ValueError):
        TemporalCandidate(
            candidate_id="x",
            clock="not_a_clock",
            interval=interval_from_iso("2024", certainty="exact"),
            method="m",
            confidence="low",
        )
    with pytest.raises(ValueError):
        TemporalEnvelope(
            selection_policy="p",
            selection_status="unknown",
            selected_document_clock="not_a_clock",
        )


def test_clock_families_and_clocks_are_frozen_constants() -> None:
    assert TEMPORAL_CLOCKS[0] == "source_published_at"
    assert len(TEMPORAL_CLOCKS) == 9
    assert QUERY_CLOCK_FAMILIES == ("valid_time", "source_time", "transaction_time")
    # Clock fields of the envelope mirror the constant exactly.
    clock_fields = set(TEMPORAL_CLOCKS)
    envelope_fields = set(TemporalEnvelope.model_fields)
    assert clock_fields <= envelope_fields


# ─── 10. Legacy bibliographic fields remain representable ─────────────────


def test_bibliographic_fields_remain_representable() -> None:
    envelope = envelope_from_bibliographic(
        document_date="2020-06-01",
        date_confidence="high",
        provenance={
            "method": "frontmatter_published",
            "source": "published:",
            "captured_at": "2026-08-03T00:00:00Z",
            "precision": "day",
        },
    )
    assert envelope.selected_document_clock == "source_published_at"
    assert envelope.selection_status == "selected"
    assert len(envelope.candidates) == 1
    candidate = envelope.candidates[0]
    assert candidate.method == "frontmatter_published"
    assert candidate.confidence == "high"
    assert candidate.interval.start == date(2020, 6, 1)
    assert envelope.source_published_at == candidate.interval


@pytest.mark.parametrize(
    ("reason", "expected_status"),
    [
        ("no_date_source", "unknown"),
        ("file_date_only", "file_date_only"),
        ("unparseable_date", "unparseable"),
    ],
)
def test_bibliographic_unknown_dates_map_to_honest_statuses(
    reason: str, expected_status: str
) -> None:
    envelope = envelope_from_bibliographic(
        document_date=None,
        date_confidence=None,
        provenance={"reason": reason},
    )
    assert envelope.selection_status == expected_status
    assert envelope.selected_document_clock is None
    assert envelope.candidates == ()


def test_bibliographic_precision_overrides_string_shape() -> None:
    envelope = envelope_from_bibliographic(
        document_date="2020-06-01",
        date_confidence="low",
        provenance={"method": "filename_year", "precision": "year"},
    )
    assert envelope.candidates[0].interval.granularity == "year"


# ─── 11. No runtime module imports the contract yet ───────────────────────


def test_no_runtime_module_imports_the_temporal_contract_yet() -> None:
    """Import isolation: only models/ and tests/ may reference T1 today."""

    offenders: list[str] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        relative = path.relative_to(BACKEND_ROOT)
        top = relative.parts[0]
        if top in ("models", "tests", "__pycache__", ".cache", ".pytest_cache"):
            continue
        if "__pycache__" in relative.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = (node.module,)
            if any(name.split(".")[0:2] == ["models", "temporal_contract"] or
                   name == "models.temporal_contract" for name in names):
                offenders.append(str(relative))
    assert offenders == [], f"runtime modules import temporal_contract: {offenders}"
