"""B1 semantic-parent eligibility contracts and exact known-row golden."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.semantic_parent_eligibility import ParentEligibilityDecisionV2
from services.ingestion import semantic_parent_eligibility
from services.ingestion.semantic_parent_eligibility import (
    ParentEligibilityRegistryError,
    classify_parent_text_v2,
    load_parent_eligibility_recipe,
    parent_eligibility_recipe_hash,
)

GOLDEN_PATH = (
    Path(__file__).resolve().parents[1] / "evals" / "mark_heading_only_known_v1.json"
)
RECIPE_GOLDEN = (
    "sha256:b0f5dc398777d03ce4b3bfebac8888ab956a225ae20fe8fd65d712897b62b87f"
)


def test_recipe_identity_and_hash_are_frozen() -> None:
    recipe = load_parent_eligibility_recipe()

    assert recipe["schema_version"] == "semantic_parent_eligibility.v2"
    assert recipe["substantive_byte_min"] == 256
    assert recipe["comparison"] == "greater_than_or_equal"
    assert parent_eligibility_recipe_hash() == RECIPE_GOLDEN


def test_heading_only_and_mixed_heading_body_are_separate() -> None:
    heading = classify_parent_text_v2("# One\n\n## Two")
    mixed = classify_parent_text_v2("## One\n" + "body " * 70)

    assert heading.model_dump() == {
        "schema_version": "semantic_parent_eligibility.v2",
        "eligible": False,
        "reason": "heading_only",
        "heading_only": True,
        "substantive_bytes": 0,
        "recipe_version": "v2",
        "recipe_hash": RECIPE_GOLDEN,
    }
    assert mixed.eligible is True
    assert mixed.heading_only is False
    assert mixed.substantive_bytes >= 256


def test_url_only_and_empty_text_are_below_threshold() -> None:
    assert classify_parent_text_v2("## Links\nhttps://example.test/path").reason == (
        "below_substantive_byte_min"
    )
    assert classify_parent_text_v2("\n\t").reason == "below_substantive_byte_min"


def test_threshold_boundary_is_255_reject_256_accept() -> None:
    rejected = classify_parent_text_v2("## Context\n" + "a" * 255)
    accepted = classify_parent_text_v2("## Context\n" + "a" * 256)

    assert rejected.substantive_bytes == 255
    assert rejected.reason == "below_substantive_byte_min"
    assert accepted.substantive_bytes == 256
    assert accepted.reason == "eligible"


def test_nfkc_normalization_precedes_utf8_byte_count() -> None:
    decision = classify_parent_text_v2("Ａ" * 256)

    assert decision.substantive_bytes == 256
    assert decision.eligible is True


def test_exact_eight_known_rows_are_caught_by_the_generic_rule() -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    text = golden["text"]

    assert golden["schema_version"] == "polymath.mark_heading_only_known.v1"
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == golden["text_sha256"]
    assert len(golden["rows"]) == 8
    assert len({row["parent_id"] for row in golden["rows"]}) == 8
    for row in golden["rows"]:
        assert row["parent_id"].startswith(row["doc_id"] + "_parent_")
        decision = classify_parent_text_v2(text)
        assert decision.reason == golden["expected_reason"]
        assert decision.substantive_bytes == 0


def test_production_rule_contains_no_known_ids_or_section_name_blacklist() -> None:
    source = inspect.getsource(semantic_parent_eligibility)
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    assert "Transcript" not in source
    assert "Description" not in source
    assert all(row["parent_id"] not in source for row in golden["rows"])


def test_recipe_validation_fails_closed_on_threshold_or_field_drift() -> None:
    recipe = load_parent_eligibility_recipe()
    changed = copy.deepcopy(recipe)
    changed["substantive_byte_min"] = 255
    with pytest.raises(ParentEligibilityRegistryError, match="drifted"):
        semantic_parent_eligibility._validate_recipe(changed)

    extra = {**recipe, "corpus_id": "forbidden"}
    with pytest.raises(ParentEligibilityRegistryError, match="fields are not exact"):
        semantic_parent_eligibility._validate_recipe(extra)


def test_decision_contract_rejects_inconsistent_reasons() -> None:
    with pytest.raises(ValidationError, match="boolean and reason disagree"):
        ParentEligibilityDecisionV2(
            schema_version="semantic_parent_eligibility.v2",
            eligible=True,
            reason="below_substantive_byte_min",
            heading_only=False,
            substantive_bytes=10,
            recipe_version="v2",
            recipe_hash=RECIPE_GOLDEN,
        )
