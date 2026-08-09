"""D — Credit-pattern fixture regression tests.

Validates the TOKEN_PATTERN lane (credit_patterns.py) against adjudicated
positive and adversarial-negative fixtures.

Fixtures: tests/extraction/fixtures/credit_pattern_adjudicated.yaml
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from services.extraction.credit_patterns import extract_credit_patterns

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "credit_pattern_adjudicated.yaml"


def _load_fixtures() -> list[dict]:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data["fixtures"]


FIXTURES = _load_fixtures()
POSITIVE = [f for f in FIXTURES if f["expected_records"]]
NEGATIVE = [f for f in FIXTURES if not f["expected_records"]]


class TestCreditPatternPositives:
    """Positive cases must extract with exact offsets and correct predicates."""

    @pytest.mark.parametrize("fixture", POSITIVE, ids=[f["id"] for f in POSITIVE])
    def test_expected_record_count(self, fixture):
        records = extract_credit_patterns(
            fixture["text"], fixture["entities"], fixture["id"]
        )
        assert len(records) == len(fixture["expected_records"]), (
            f"{fixture['id']}: expected {len(fixture['expected_records'])} records, "
            f"got {len(records)}"
        )

    @pytest.mark.parametrize("fixture", POSITIVE, ids=[f["id"] for f in POSITIVE])
    def test_expected_subjects_and_objects(self, fixture):
        records = extract_credit_patterns(
            fixture["text"], fixture["entities"], fixture["id"]
        )
        actual_pairs = {(r["subject_text"], r["object_text"]) for r in records}
        for expected in fixture["expected_records"]:
            pair = (expected["subject"], expected["object"])
            assert pair in actual_pairs, (
                f"{fixture['id']}: expected ({pair[0]}, {pair[1]}) not found "
                f"in {actual_pairs}"
            )

    @pytest.mark.parametrize("fixture", POSITIVE, ids=[f["id"] for f in POSITIVE])
    def test_canonical_predicate(self, fixture):
        records = extract_credit_patterns(
            fixture["text"], fixture["entities"], fixture["id"]
        )
        for rec, expected in zip(records, fixture["expected_records"]):
            assert rec["canonical_predicate"] == expected["canonical_predicate"], (
                f"{fixture['id']}: expected canonical "
                f"{expected['canonical_predicate']}, got {rec['canonical_predicate']}"
            )

    @pytest.mark.parametrize("fixture", POSITIVE, ids=[f["id"] for f in POSITIVE])
    def test_exact_offsets(self, fixture):
        records = extract_credit_patterns(
            fixture["text"], fixture["entities"], fixture["id"]
        )
        for expected in fixture["expected_records"]:
            # Find the matching record
            matching = [
                r for r in records
                if r["subject_text"] == expected["subject"]
                and r["object_text"] == expected["object"]
            ]
            assert matching, (
                f"{fixture['id']}: no record for "
                f"({expected['subject']}, {expected['object']})"
            )
            rec = matching[0]
            assert [rec["subject_start"], rec["subject_end"]] == expected["subject_offsets"], (
                f"{fixture['id']}: subject offsets "
                f"[{rec['subject_start']}, {rec['subject_end']}] != "
                f"{expected['subject_offsets']}"
            )
            assert [rec["object_start"], rec["object_end"]] == expected["object_offsets"], (
                f"{fixture['id']}: object offsets "
                f"[{rec['object_start']}, {rec['object_end']}] != "
                f"{expected['object_offsets']}"
            )

    @pytest.mark.parametrize("fixture", POSITIVE, ids=[f["id"] for f in POSITIVE])
    def test_source_family(self, fixture):
        records = extract_credit_patterns(
            fixture["text"], fixture["entities"], fixture["id"]
        )
        for rec in records:
            assert rec["source_family"] == "TOKEN_PATTERN"
            assert rec["dependency_parse_used"] is False


class TestCreditPatternNegatives:
    """Adversarial negatives must produce zero records."""

    @pytest.mark.parametrize("fixture", NEGATIVE, ids=[f["id"] for f in NEGATIVE])
    def test_no_records_extracted(self, fixture):
        records = extract_credit_patterns(
            fixture["text"], fixture["entities"], fixture["id"]
        )
        assert records == [], (
            f"{fixture['id']}: expected 0 records but got {len(records)}: "
            f"{[(r['subject_text'], r['object_text']) for r in records]}. "
            f"Reason: {fixture.get('reason', 'N/A')}"
        )
