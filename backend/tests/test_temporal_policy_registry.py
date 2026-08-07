"""T1 acceptance tests — temporal policy registry snapshot.

The FROZEN_HASH below pins the owner-ratifiable v1 policy snapshot; any
silent edit becomes tamper-evident (house pattern from
tests/test_registry_loader.py). The registry is descriptive data only:
nothing loads it at runtime until T2 adapter emission is enabled.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from models.temporal_contract import (
    FILE_TIME_METHODS,
    QUERY_CLOCK_FAMILIES,
    TEMPORAL_CLOCKS,
    validate_temporal_policy_registry,
)

REGISTRY_PATH = Path(__file__).resolve().parents[1] / (
    "registries/temporal_policy_registry.v1.json"
)

FROZEN_HASH = (
    "sha256:4599442a946428f61fa301af181e302818d874e83047ee60a660ed41b7be50ef"
)

EXPECTED_SOURCE_TYPES = frozenset(
    {
        "transcript",
        "meeting_transcript",
        "epub",
        "research_paper",
        "news_article",
        "markdown_notes",
        "pdf",
        "json_api_record",
    }
)


@pytest.fixture(scope="module")
def registry_bytes() -> bytes:
    return REGISTRY_PATH.read_bytes()


@pytest.fixture(scope="module")
def registry(registry_bytes: bytes) -> dict:
    return json.loads(registry_bytes)


def test_registry_file_exists_and_is_valid_json(registry: dict) -> None:
    assert isinstance(registry, dict)


def test_registry_snapshot_hash_is_frozen(registry_bytes: bytes) -> None:
    digest = hashlib.sha256(registry_bytes).hexdigest()
    assert f"sha256:{digest}" == FROZEN_HASH, (
        "temporal_policy_registry.v1.json changed — edits require a NEW "
        "version file, never a silent edit"
    )


def test_registry_passes_structural_validation(registry: dict) -> None:
    validate_temporal_policy_registry(registry)


def test_registry_declares_all_envelope_clocks(registry: dict) -> None:
    assert registry["clocks"] == list(TEMPORAL_CLOCKS)
    assert registry["query_clock_families"] == list(QUERY_CLOCK_FAMILIES)
    assert registry["interval_semantics"] == "half_open"


def test_registry_covers_all_mandated_source_types(registry: dict) -> None:
    assert set(registry["source_policies"]) == EXPECTED_SOURCE_TYPES


def test_every_policy_is_versioned_and_targets_a_known_clock(
    registry: dict,
) -> None:
    for source_type, policy in registry["source_policies"].items():
        assert policy["policy_id"].endswith("_v1"), source_type
        assert policy["target_clock"] in TEMPORAL_CLOCKS, source_type
        for clock in policy.get("additional_clocks", []):
            assert clock in TEMPORAL_CLOCKS, f"{source_type}:{clock}"


def test_ladders_are_non_empty_unique_and_confidence_typed(registry: dict) -> None:
    for source_type, policy in registry["source_policies"].items():
        ladder = policy["ladder"]
        assert ladder, source_type
        methods = [rung["method"] for rung in ladder]
        assert len(methods) == len(set(methods)), f"{source_type} repeats a method"
        for rung in ladder:
            assert rung["confidence"] in ("high", "medium", "low")


def test_no_file_time_method_appears_in_any_ladder(registry: dict) -> None:
    for source_type, policy in registry["source_policies"].items():
        for rung in policy["ladder"]:
            assert rung["method"] not in FILE_TIME_METHODS, (
                f"{source_type} ladder contains file-time method {rung['method']}"
            )


def test_global_rules_encode_the_anti_conflation_mandate(registry: dict) -> None:
    rules = registry["global_rules"]
    assert rules["file_time_never_publication_time"] is True
    assert rules["missing_dates_remain_null"] is True
    assert rules["no_llm_date_inference"] is True
    assert rules["no_network_date_resolution"] is True
    assert rules["conflicting_candidates_retained"] is True
    assert rules["selection_is_projection_not_destruction"] is True
    assert rules["relative_without_anchor"] == "unresolved_anchor"
    assert rules["ambiguous_locale"] == "do_not_guess"


def test_transcript_ladder_matches_owner_spec_order(registry: dict) -> None:
    ladder = registry["source_policies"]["transcript"]["ladder"]
    assert [rung["method"] for rung in ladder] == [
        "user_supplied_recording_time",
        "transcript_metadata_recording",
        "platform_event_timestamp",
        "transcript_header_date",
        "title_date_parse",
    ]
    assert registry["source_policies"]["transcript"]["policy_id"] == (
        "transcript_recording_precedence_v1"
    )


def test_validator_rejects_tampered_snapshots(registry: dict) -> None:
    tampered = json.loads(json.dumps(registry))
    tampered["version"] = "v2"
    with pytest.raises(ValueError):
        validate_temporal_policy_registry(tampered)

    tampered = json.loads(json.dumps(registry))
    tampered["source_policies"]["pdf"]["ladder"].append(
        {"method": "pdf_creation_date", "confidence": "low"}
    )
    with pytest.raises(ValueError):
        validate_temporal_policy_registry(tampered)

    tampered = json.loads(json.dumps(registry))
    tampered["global_rules"]["no_llm_date_inference"] = False
    with pytest.raises(ValueError):
        validate_temporal_policy_registry(tampered)
