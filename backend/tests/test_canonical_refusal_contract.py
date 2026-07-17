from __future__ import annotations

from datetime import datetime, timezone

import pytest

from evals.canonical_refusal_contract import (
    CLASSIFIER_VERSION,
    EXPECTED_CHAT_MODEL,
    build_system_prompt_receipt,
    classify_refusal,
    extract_answerability_contract,
    model_answer_content_errors,
    prompt_render_context_is_stable,
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
            _trace(
                "Assistant final answer",
                {"model_skipped": False, "model": EXPECTED_CHAT_MODEL},
            ),
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
                _trace(
                    "Assistant final answer",
                    {"model_skipped": False, "model": EXPECTED_CHAT_MODEL},
                ),
                _trace(
                    "Assistant final answer",
                    {"model_skipped": False, "model": EXPECTED_CHAT_MODEL},
                ),
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
                _trace(
                    "Assistant final answer",
                    {"model_skipped": False, "model": EXPECTED_CHAT_MODEL},
                ),
                _trace("Chat model route", {"model": "wrong/model"}),
            ],
            [],
            "route mismatch",
        ),
        (
            [
                _trace(
                    "Assistant final answer",
                    {"model_skipped": False, "model": EXPECTED_CHAT_MODEL},
                ),
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


@pytest.mark.parametrize(
    ("final_metadata", "done", "fragment"),
    [
        (
            {"model_skipped": False},
            [{"model_used": EXPECTED_CHAT_MODEL}],
            "assistant final trace model mismatch",
        ),
        (
            {"model_skipped": False, "model": EXPECTED_CHAT_MODEL},
            [],
            "done event count must be 1",
        ),
        (
            {"model_skipped": False, "model": EXPECTED_CHAT_MODEL},
            [{}, {"model_used": EXPECTED_CHAT_MODEL}],
            "done event count must be 1",
        ),
        (
            {"model_skipped": False, "model": EXPECTED_CHAT_MODEL},
            [{}],
            "done model mismatch",
        ),
    ],
)
def test_trace_contract_requires_explicit_single_model_receipts(
    final_metadata, done, fragment
):
    result = validate_chat_trace_contract(
        [
            _trace("Chat model route", {"model": EXPECTED_CHAT_MODEL}),
            _trace("Assistant final answer", final_metadata),
        ],
        done,
    )

    assert result["ok"] is False
    assert any(fragment in error for error in result["errors"])


def test_answerability_contract_requires_exact_trace_and_typed_guard():
    result = extract_answerability_contract(
        [
            _trace(
                "Answerability gate",
                {
                    "raw_answerable": False,
                    "corpus_scope_guard": {
                        "eligible": True,
                        "coverage": 0.25,
                        "matched_terms": [],
                        "missing_terms": ["missing"],
                    },
                },
            )
        ]
    )

    assert result["ok"] is True
    assert result["raw_answerable"] is False
    assert result["guard"]["eligible"] is True
    assert result["guard"]["coverage"] == 0.25


@pytest.mark.parametrize(
    "traces",
    [
        [],
        [_trace("Answerability gate", None)],
        [
            _trace(
                "Answerability gate",
                {
                    "raw_answerable": None,
                    "corpus_scope_guard": {"eligible": True, "coverage": 0.0},
                },
            )
        ],
        [
            _trace(
                "Answerability gate",
                {
                    "raw_answerable": True,
                    "corpus_scope_guard": {"eligible": None, "coverage": 2.0},
                },
            )
        ],
    ],
)
def test_answerability_contract_rejects_missing_or_untyped_state(traces):
    assert extract_answerability_contract(traces)["ok"] is False


@pytest.mark.parametrize(
    "guard",
    [
        {"eligible": True, "matched_terms": [], "missing_terms": []},
        {
            "eligible": True,
            "coverage": None,
            "matched_terms": [],
            "missing_terms": [],
        },
        {
            "eligible": True,
            "coverage": 0.5,
            "missing_terms": [],
        },
        {
            "eligible": True,
            "coverage": 0.5,
            "matched_terms": [],
        },
        {
            "eligible": True,
            "coverage": 0.5,
            "matched_terms": "term",
            "missing_terms": [],
        },
    ],
)
def test_answerability_eligible_guard_requires_complete_typed_term_lists(guard):
    result = extract_answerability_contract(
        [
            _trace(
                "Answerability gate",
                {
                    "raw_answerable": True,
                    "corpus_scope_guard": guard,
                },
            )
        ]
    )

    assert result["ok"] is False


def test_model_called_empty_or_whitespace_answer_is_red():
    assert model_answer_content_errors("", model_skipped=False)
    assert model_answer_content_errors(" \n\t", model_skipped=False)
    assert model_answer_content_errors("answer", model_skipped=False) == []
    assert model_answer_content_errors("", model_skipped=True) == []


def test_prompt_receipt_hashes_exact_builder_and_context():
    def builder(value: datetime) -> str:
        return f"Prompt for {value:%Y-%m-%d} in {value.tzname()}"

    rendered_at = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    receipt = build_system_prompt_receipt(builder, rendered_at)

    assert len(receipt["sha256"]) == 64
    assert receipt["rendered_for_local_date"] == "2026-07-18"
    assert prompt_render_context_is_stable(receipt, rendered_at) is True
    assert (
        prompt_render_context_is_stable(
            receipt,
            datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
        )
        is False
    )
