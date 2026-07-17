"""Static contract tests for the preregistered waterfall pressure runner."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_waterfall_pressure_diagnostic.py"
)
_SPEC = importlib.util.spec_from_file_location("waterfall_pressure_runner", _SCRIPT)
assert _SPEC and _SPEC.loader
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


class _Chunk:
    def __init__(
        self,
        *,
        corpus_id: str = "corpus",
        doc_id: str = "doc",
        parent_id: str = "parent",
        chunk_id: str = "chunk",
        text: str = "evidence",
        doc_name: str = "Title.md",
    ) -> None:
        self.corpus_id = corpus_id
        self.doc_id = doc_id
        self.parent_id = parent_id
        self.chunk_id = chunk_id
        self.text = text
        self.doc_name = doc_name


def _results() -> list[dict]:
    return [
        {
            "quality_preserved": True,
            "packet_hash_stable": True,
            "hydration_levels_recorded": True,
        }
        for _ in range(6)
    ]


def _authorized_primary_artifact() -> dict:
    results = [
        {
            "hydration_decisions": [
                {
                    "rank": 0,
                    "parent_id": f"parent-{index}",
                    "hydration_level": "full",
                }
            ]
        }
        for index in range(6)
    ]
    return {
        "schema_version": "polymath.waterfall_pressure_results.v1",
        "preregistration_sha256": runner.BRIDGE_PREREG_SHA256,
        "selection_sha256": runner.SELECTION_SHA256,
        "runtime": {"budget_tokens": 1500},
        "results": results,
        "summary": {
            "fallback_authorized": True,
            "all_ranked_parent_decisions_full": True,
            "full_decisions": 6,
            "summary_decisions": 0,
            "skip_decisions": 0,
        },
    }


def test_primary_budget_is_exact_and_needs_no_fallback_artifact() -> None:
    assert runner.validate_budget(1500, None) is None
    with pytest.raises(RuntimeError, match="exactly 1,500"):
        runner.validate_budget(1499, None)
    with pytest.raises(RuntimeError, match="primary run cannot"):
        runner.validate_budget(1500, Path("unexpected.json"))


def test_750_requires_saved_all_full_primary_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "primary.json"
    artifact.write_text(json.dumps(_authorized_primary_artifact()))
    loaded = runner.validate_budget(750, artifact)
    assert loaded and loaded["summary"]["fallback_authorized"] is True

    invalid = _authorized_primary_artifact()
    invalid["summary"]["summary_decisions"] = 1
    artifact.write_text(json.dumps(invalid))
    with pytest.raises(RuntimeError, match="already exercised"):
        runner.validate_budget(750, artifact)


def test_primary_all_full_authorizes_only_preregistered_fallback() -> None:
    summary = runner.summarize_gate(
        budget_tokens=1500,
        results=_results(),
        hydration_counts={"full": 18, "summary": 0, "skip": 0},
    )
    assert summary["stage_valid"] is True
    assert summary["acceptance_passed"] is False
    assert summary["fallback_authorized"] is True


def test_partial_lower_tier_coverage_is_red_and_cannot_trigger_fallback() -> None:
    summary_only = runner.summarize_gate(
        budget_tokens=1500,
        results=_results(),
        hydration_counts={"full": 12, "summary": 6, "skip": 0},
    )
    assert summary_only["stage_valid"] is False
    assert summary_only["fallback_authorized"] is False

    skip_only = runner.summarize_gate(
        budget_tokens=1500,
        results=_results(),
        hydration_counts={"full": 12, "summary": 0, "skip": 6},
    )
    assert skip_only["stage_valid"] is False
    assert skip_only["fallback_authorized"] is False


def test_summary_and_skip_with_stable_quality_passes() -> None:
    summary = runner.summarize_gate(
        budget_tokens=1500,
        results=_results(),
        hydration_counts={"full": 8, "summary": 5, "skip": 4},
    )
    assert summary["acceptance_passed"] is True
    assert summary["stage_valid"] is True
    assert summary["fallback_authorized"] is False


def test_any_quality_or_repeat_regression_fails() -> None:
    results = _results()
    results[2]["quality_preserved"] = False
    results[3]["packet_hash_stable"] = False
    summary = runner.summarize_gate(
        budget_tokens=1500,
        results=results,
        hydration_counts={"full": 8, "summary": 5, "skip": 4},
    )
    assert summary["acceptance_passed"] is False
    assert summary["stage_valid"] is False


def test_all_full_does_not_mask_an_integrity_failure() -> None:
    results = _results()
    results[0]["quality_preserved"] = False
    summary = runner.summarize_gate(
        budget_tokens=1500,
        results=results,
        hydration_counts={"full": 18, "summary": 0, "skip": 0},
    )
    assert summary["all_ranked_parent_decisions_full"] is True
    assert summary["fallback_authorized"] is False
    assert summary["stage_valid"] is False


def test_source_signature_is_hydration_independent_but_evidence_exact() -> None:
    first = _Chunk()
    same = _Chunk()
    changed_text = _Chunk(text="different evidence")
    assert runner._chunk_signature([first]) == runner._chunk_signature([same])
    assert runner._chunk_signature([first]) != runner._chunk_signature([changed_text])
    assert runner._signature_sha256(
        runner._chunk_signature([first])
    ) == runner._signature_sha256(runner._chunk_signature([same]))


def test_bridge_title_scorer_preserves_frozen_winner_contract() -> None:
    case = {
        "expected_title_any": ["Directing.md"],
        "forbidden_rank1": ["Camera Lens.md"],
    }
    assert runner.score_bridge_titles(case, ["Directing.md", "Editing.md"])["passed"]
    forbidden = runner.score_bridge_titles(case, ["Camera Lens.md", "Directing.md"])
    assert forbidden["expected_hit_top_three"] is True
    assert forbidden["forbidden_rank1"] is True
    assert forbidden["passed"] is False


def test_distinct_title_window_uses_document_identity_order() -> None:
    chunks = [
        _Chunk(doc_id="d1", chunk_id="c1"),
        _Chunk(doc_id="d1", chunk_id="c2"),
        _Chunk(doc_id="d2", chunk_id="c3"),
        _Chunk(doc_id="d3", chunk_id="c4"),
        _Chunk(doc_id="d4", chunk_id="c5"),
    ]
    titles = runner._top_distinct_titles(
        chunks,
        {"d1": "One.md", "d2": "Two.md", "d3": "Three.md", "d4": "Four.md"},
    )
    assert titles == ["One.md", "Two.md", "Three.md"]
