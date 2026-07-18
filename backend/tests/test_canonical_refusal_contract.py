from __future__ import annotations

import pytest

from evals.canonical_refusal_contract import (
    CLASSIFIER_VERSION,
    EXPECTED_CHAT_MODEL,
    classify_refusal,
    validate_chat_trace_contract,
)


@pytest.mark.parametrize(
    "answer",
    [
        "Sorry, but I cannot answer that from the selected corpus.",
        (
            "I cannot answer. The nearest retrieved material comes from: "
            "camera.md; optics.pdf."
        ),
        (
            "Apologies. The selected sources do not contain the answer. "
            "Sources checked: Camera Craft; Story Form."
        ),
        "The available sources are silent regarding the requested checklist.",
        "The retrieval found some related material. I cannot answer the question.",
    ],
)
def test_classifier_allows_only_courtesy_scope_and_source_lists(answer):
    result = classify_refusal(answer, model_skipped=False)

    assert result["version"] == CLASSIFIER_VERSION
    assert result["state"] == "model_voiced_refusal"
    assert result["substantive_clause_count"] == 0


@pytest.mark.parametrize(
    "answer",
    [
        "I cannot answer because the federal rate is 21 percent.",
        "I cannot answer, but wide lenses exaggerate depth.",
        "I cannot confirm it though the method was published in 1927.",
        "Despite insufficient evidence, the answer is probably 42.",
        "The corpus does not establish X while it clearly recommends Y.",
        (
            "I cannot answer because the corpus is incomplete; however, "
            "source A states Z."
        ),
    ],
)
def test_classifier_never_hides_residual_assertions_after_refusal_cues(answer):
    result = classify_refusal(answer, model_skipped=False)

    assert result["state"] == "answered"
    assert result["refused"] is False
    assert result["substantive_clause_count"] >= 1


def test_gate_blocked_precedes_answer_text():
    result = classify_refusal("The answer is 42.", model_skipped=True)

    assert result["state"] == "gate_blocked"


def _trace(title: str, metadata: dict | None = None) -> dict:
    return {"title": title, "metadata": metadata}


def test_trace_contract_requires_exact_final_and_route_with_boolean_skip():
    result = validate_chat_trace_contract(
        [
            _trace("Chat model route", {"model": EXPECTED_CHAT_MODEL}),
            _trace("Assistant final answer", {"model_skipped": False}),
        ],
        [{"model_used": EXPECTED_CHAT_MODEL}],
    )

    assert result["ok"] is True
    assert result["model_skipped"] is False
    assert result["done_event_count"] == 1


@pytest.mark.parametrize(
    ("traces", "done", "fragment"),
    [
        ([], [], "assistant final trace count"),
        (
            [
                _trace("Assistant final answer", {"model_skipped": False}),
                _trace("Assistant final answer", {"model_skipped": False}),
            ],
            [],
            "assistant final trace count",
        ),
        (
            [_trace("Assistant final answer", {"model_skipped": "false"})],
            [],
            "model_skipped must be boolean",
        ),
        (
            [
                _trace("Assistant final answer", {"model_skipped": False}),
                _trace("Chat model route", {"model": "wrong/model"}),
            ],
            [],
            "route mismatch",
        ),
        (
            [
                _trace("Assistant final answer", {"model_skipped": False}),
                _trace("Chat model route", {"model": EXPECTED_CHAT_MODEL}),
            ],
            [{"model_used": "wrong/model"}],
            "done/route model mismatch",
        ),
    ],
)
def test_trace_contract_rejects_missing_duplicate_invalid_and_model_drift(
    traces, done, fragment
):
    result = validate_chat_trace_contract(traces, done)

    assert result["ok"] is False
    assert any(fragment in error for error in result["errors"])
