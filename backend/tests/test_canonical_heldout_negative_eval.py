from __future__ import annotations

import copy
import hashlib

import pytest

from scripts.run_canonical_heldout_negative_eval import (
    CLASSIFIER_VERSION,
    EXECUTION_SCHEMA,
    PROMPT_HASH_METHOD,
    SELECTION_PATH,
    SELECTION_SHA256,
    SPEC_PATH,
    SPEC_SHA256,
    _cost_envelope,
    classify_refusal,
    execution_completeness_errors,
)


@pytest.mark.parametrize(
    "answer",
    [
        "I cannot answer that from the selected corpus.",
        "I did not find source evidence for that claim.",
        "The provided sources do not mention Figure 9.4.",
        (
            "There is insufficient evidence in the corpus to answer. "
            "Please provide another source."
        ),
        (
            "The selected documents do not specify that tax rate, and I "
            "cannot determine it from this corpus."
        ),
        "That information is not in the selected corpus.",
        "The available sources are silent regarding the requested checklist.",
        "No selected source mentions the appendix interview.",
        "I don't have enough information in the corpus to answer.",
    ],
)
def test_classifier_marks_only_pure_model_refusals(answer):
    result = classify_refusal(answer, model_skipped=False)

    assert result["version"] == CLASSIFIER_VERSION
    assert result["state"] == "model_voiced_refusal"
    assert result["refused"] is True
    assert result["refusal_cue_ids"]
    assert result["substantive_clause_count"] == 0


@pytest.mark.parametrize(
    "answer",
    [
        (
            "The corpus does not name Roger Deakins, but it says wide lenses "
            "exaggerate depth."
        ),
        ("I cannot confirm the exact year. The technique was developed in " "1927."),
        (
            "The sources do not establish the checklist; however, the first "
            "step is to rehearse slowly."
        ),
        "Although evidence is limited, the federal rate is 21 percent.",
        "The selected corpus covers cinematography and visual storytelling.",
        (
            "The retrieved sources don't establish the camera body. They do "
            "mention the Angenieux and Zeiss lenses used on the production."
        ),
        "",
    ],
)
def test_classifier_treats_hedged_partial_or_nonrefusal_as_answered(answer):
    result = classify_refusal(answer, model_skipped=False)

    assert result["state"] == "answered"
    assert result["refused"] is False


def test_model_skipped_always_has_gate_blocked_precedence():
    result = classify_refusal(
        "The answer is 42 despite the evidence warning.",
        model_skipped=True,
    )

    assert result["state"] == "gate_blocked"
    assert result["refused"] is True


def _complete_execution() -> dict:
    answer = "I cannot answer that from the selected corpus."
    return {
        "schema_version": EXECUTION_SCHEMA,
        "execution_id": "q::qdrant_mongo_graph",
        "query_id": "q",
        "family": "F1",
        "question_sha256": hashlib.sha256(b"question").hexdigest(),
        "request": {
            "payload_sha256": hashlib.sha256(b"payload").hexdigest(),
            "temperature": 0.0,
            "top_k": 8,
            "conversation_id_sent": False,
        },
        "prior_call_state": {
            "process_run_id": "run",
            "request_ordinal": 1,
            "prior_call_count": 0,
            "session_mode": "fresh_conversation_per_probe_sequential_process",
        },
        "transport": {"done_received": True, "errors": []},
        "technical": {"status": "ok", "ok": True},
        "answerability": {
            "raw_answerable": False,
            "guard": {"eligible": True, "coverage": 0.0},
        },
        "model_skipped": True,
        "model_route": {"model": "anthropic/minimax-m2.7"},
        "system_prompt_template": {
            "method_version": PROMPT_HASH_METHOD,
            "sha256": hashlib.sha256(b"prompt").hexdigest(),
        },
        "classification": classify_refusal(answer, model_skipped=True),
        "answer": {
            "sha256": hashlib.sha256(answer.encode()).hexdigest(),
            "excerpt": answer,
        },
        "sources": {
            "membership_count": 0,
            "all_in_selected_corpus": True,
        },
    }


def test_complete_execution_contract_accepts_false_zero_and_none_telemetry():
    row = _complete_execution()
    row["answerability"]["raw_answerable"] = False
    row["answerability"]["guard"]["eligible"] = False
    row["answerability"]["guard"]["coverage"] = None

    assert execution_completeness_errors(row) == []


@pytest.mark.parametrize(
    "path",
    [
        ("answerability", "raw_answerable"),
        ("answerability", "guard", "eligible"),
        ("answerability", "guard", "coverage"),
        ("model_route", "model"),
        ("prior_call_state", "prior_call_count"),
        ("request", "payload_sha256"),
        ("answer", "excerpt"),
    ],
)
def test_execution_contract_rejects_each_missing_forensic_field(path):
    row = copy.deepcopy(_complete_execution())
    parent = row
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]

    errors = execution_completeness_errors(row)

    assert f"missing:{'.'.join(path)}" in errors


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (
            ("answerability", "raw_answerable"),
            None,
            "invalid:answerability.raw_answerable",
        ),
        (
            ("answerability", "guard", "eligible"),
            None,
            "invalid:answerability.guard.eligible",
        ),
        (
            ("answerability", "guard", "coverage"),
            1.5,
            "invalid:answerability.guard.coverage",
        ),
        (("model_skipped",), "false", "invalid:model_skipped"),
    ],
)
def test_execution_contract_rejects_invalid_forensic_field_types(path, value, expected):
    row = copy.deepcopy(_complete_execution())
    parent = row
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value

    assert expected in execution_completeness_errors(row)


def test_frozen_negative_spec_digest_is_immutable():
    assert hashlib.sha256(SPEC_PATH.read_bytes()).hexdigest() == SPEC_SHA256


def test_frozen_selection_digest_is_immutable():
    assert hashlib.sha256(SELECTION_PATH.read_bytes()).hexdigest() == SELECTION_SHA256


def test_two_attempt_cost_envelope_closes_below_authorized_ceiling():
    envelope = _cost_envelope()

    assert envelope["execution_count"] == 28
    assert envelope["attempt_bound"] == 56
    assert envelope["total_usd"] == pytest.approx(1.4324184)
    assert envelope["total_usd"] <= envelope["ceiling_usd"] == 1.5
