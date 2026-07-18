from __future__ import annotations

import ast
import copy
import hashlib
from types import SimpleNamespace

import pytest

import scripts.run_canonical_heldout_negative_eval as harness
from scripts.run_canonical_heldout_negative_eval import (
    CHAT_ORCHESTRATOR_PATH,
    CLASSIFIER_VERSION,
    COMPACT_BASELINE_QUERY_IDS,
    COMPACT_BASELINE_SELECTION_NAME,
    EXPECTED_CHAT_MODEL,
    EXECUTION_SCHEMA,
    PROMPT_HASH_METHOD,
    SELECTION_PATH,
    SELECTION_SHA256,
    SPEC_PATH,
    SPEC_SHA256,
    _cost_envelope,
    _build_execution,
    _eval_lock,
    _parser,
    _runtime_flags,
    _select_queries,
    _validate_local_api,
    _validate_same_container_runtime,
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
        "transport": {"done_received": True, "done_event_count": 1, "errors": []},
        "technical": {"status": "ok", "ok": True},
        "answerability": {
            "telemetry": {"status": "weak"},
            "raw_answerable": False,
            "guard": {"eligible": True, "coverage": 0.0},
        },
        "model_skipped": True,
        "model_route": {"model": EXPECTED_CHAT_MODEL},
        "trace_contract": {
            "ok": True,
            "assistant_final_trace_count": 1,
            "model_route_trace_count": 1,
            "expected_model": EXPECTED_CHAT_MODEL,
        },
        "system_prompt_template": {
            "method_version": PROMPT_HASH_METHOD,
            "sha256": hashlib.sha256(b"prompt").hexdigest(),
            "source_sha256": hashlib.sha256(b"source").hexdigest(),
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
        ("answerability", "telemetry"),
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
            ("answerability", "telemetry"),
            {},
            "invalid:answerability.telemetry",
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


def test_compact_baseline_selection_is_exact_and_preserves_registered_order():
    frozen = [
        {"id": query_id, "must_refuse": True}
        for query_id in reversed(COMPACT_BASELINE_QUERY_IDS)
    ]

    selected = _select_queries(frozen, COMPACT_BASELINE_SELECTION_NAME)

    assert [row["id"] for row in selected] == list(COMPACT_BASELINE_QUERY_IDS)
    assert len(selected) == 10
    assert _cost_envelope(len(selected))["total_usd"] < 0.52


def test_compact_baseline_selection_refuses_missing_or_unknown_contract():
    with pytest.raises(RuntimeError, match="missing from frozen spec"):
        _select_queries([], COMPACT_BASELINE_SELECTION_NAME)
    with pytest.raises(RuntimeError, match="unsupported probe selection"):
        _select_queries([], "ad-hoc")


@pytest.mark.parametrize(
    "api",
    [
        "https://example.com",
        "http://127.0.0.1:8000/api",
        "http://user:secret@localhost:8000",
        "http://localhost:8000?token=secret",
    ],
)
def test_endpoint_binding_rejects_nonlocal_paths_and_credentials(api):
    with pytest.raises(RuntimeError, match="loopback"):
        _validate_local_api(api)


def test_endpoint_binding_accepts_loopback_origin():
    receipt = _validate_local_api("http://127.0.0.1:8000")

    assert receipt["hostname"] == "127.0.0.1"
    assert receipt["loopback_required"] is True


def test_same_container_binding_requires_runtime_marker(tmp_path):
    endpoint = _validate_local_api("http://127.0.0.1:8000")
    missing = tmp_path / "missing"

    with pytest.raises(RuntimeError, match="inside the backend container"):
        _validate_same_container_runtime(endpoint, container_marker=missing)

    marker = tmp_path / ".dockerenv"
    marker.write_text("", encoding="utf-8")
    receipt = _validate_same_container_runtime(endpoint, container_marker=marker)
    assert receipt["same_container_prompt_binding"] is True


def test_assert_held_lock_requires_exact_owner_and_never_unlinks(tmp_path):
    lock = tmp_path / "eval.lock"
    lock.write_text("branch/name\n", encoding="utf-8")

    with _eval_lock("branch/name", 0, mode="assert-held", lock_path=lock):
        assert lock.exists()

    assert lock.read_text(encoding="utf-8") == "branch/name\n"


def test_assert_held_lock_rejects_owner_mismatch(tmp_path):
    lock = tmp_path / "eval.lock"
    lock.write_text("other/branch\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="owner mismatch"):
        with _eval_lock("branch/name", 0, mode="assert-held", lock_path=lock):
            pass

    assert lock.exists()


def test_acquired_lock_releases_only_its_exact_owner(tmp_path):
    lock = tmp_path / "eval.lock"

    with _eval_lock("branch/name", 0, lock_path=lock):
        assert lock.read_text(encoding="utf-8") == "branch/name\n"

    assert not lock.exists()


def test_runtime_vector_requires_every_dark_retrieval_flag_off(monkeypatch):
    expected = {
        "RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED": True,
        "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED": True,
        "TEMPORAL_QUERY_ROUTING_ENABLED": False,
        "RERANK_EVIDENCE_SUPPORT": False,
        "ATOMIC_CLAIM_ANCHORS_ENABLED": False,
        "PARENT_EXCERPT_ENABLED": False,
        "WATERFALL_ASSEMBLY": False,
        "TWO_LANE_ANCHORING": False,
        "TWO_LANE_ANCHORING_ENABLED": False,
        "HYDE_ENABLED": False,
        "SHELF_RESERVE_ENABLED": False,
        "GROUNDED_QUERY_PLANNER_ENABLED": False,
        "FOUR_LANE_TIER0_ROUTER_ENABLED": False,
        "FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED": False,
        "AGENTIC_MODE_ENABLED": False,
    }
    monkeypatch.setattr(harness, "get_settings", lambda: SimpleNamespace(**expected))

    assert _runtime_flags(False) == expected

    expected["WATERFALL_ASSEMBLY"] = True
    with pytest.raises(RuntimeError, match="runtime flags"):
        _runtime_flags(False)


def test_parser_rejects_cli_token():
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "--expected-temporal",
                "off",
                "--output",
                "/tmp/journal.json",
                "--token",
                "must-not-be-accepted",
            ]
        )


def test_parser_accepts_only_bounded_compact_concurrency():
    args = _parser().parse_args(
        [
            "--expected-temporal",
            "off",
            "--output",
            "/tmp/journal.json",
            "--probe-selection",
            COMPACT_BASELINE_SELECTION_NAME,
            "--concurrency",
            "3",
        ]
    )

    assert args.probe_selection == COMPACT_BASELINE_SELECTION_NAME
    assert args.concurrency == 3
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "--expected-temporal",
                "off",
                "--output",
                "/tmp/journal.json",
                "--concurrency",
                "4",
            ]
        )


def test_all_production_final_traces_emit_explicit_boolean_model_skipped():
    tree = ast.parse(CHAT_ORCHESTRATOR_PATH.read_text(encoding="utf-8"))
    observed: list[bool] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        title = next((kw.value for kw in node.keywords if kw.arg == "title"), None)
        if not (
            isinstance(title, ast.Constant) and title.value == "Assistant final answer"
        ):
            continue
        metadata = next(
            (kw.value for kw in node.keywords if kw.arg == "metadata"),
            None,
        )
        assert isinstance(metadata, ast.Dict)
        pairs = {
            key.value: value
            for key, value in zip(metadata.keys, metadata.values)
            if isinstance(key, ast.Constant)
        }
        value = pairs.get("model_skipped")
        assert isinstance(value, ast.Constant)
        assert type(value.value) is bool
        observed.append(value.value)

    assert sorted(observed) == [False, False, True]


def test_missing_final_trace_is_both_technical_and_journal_red():
    raw = {
        "answer": "I cannot answer that from the selected corpus.",
        "traces": [
            {
                "title": "Chat model route",
                "metadata": {"model": EXPECTED_CHAT_MODEL},
            },
            {
                "title": "Answerability gate",
                "metadata": {
                    "status": "weak",
                    "raw_answerable": False,
                    "corpus_scope_guard": {
                        "eligible": True,
                        "coverage": 0.0,
                    },
                },
            },
            {
                "title": "Local RAG retrieval",
                "metadata": {"effective_tier": "qdrant_mongo_graph"},
            },
        ],
        "done_events": [{"model_used": EXPECTED_CHAT_MODEL}],
        "done": {"model_used": EXPECTED_CHAT_MODEL},
        "sources": [],
        "errors": [],
        "payload": {
            "overrides": {
                "temperature": 0.0,
                "retrieval_k": 8,
                "final_top_k": 8,
            }
        },
        "conversation_ids": [],
        "event_counts": {"done": 1},
        "elapsed_seconds": 1.0,
    }

    row = _build_execution(
        case={"id": "q", "family": "F1", "question": "Question?"},
        ordinal=1,
        process_run_id="run",
        raw=raw,
        prompt_receipt={
            "method_version": PROMPT_HASH_METHOD,
            "sha256": hashlib.sha256(b"prompt").hexdigest(),
            "source_sha256": hashlib.sha256(b"source").hexdigest(),
        },
        document_names={},
        selected_filenames=set(),
    )

    assert row["technical"]["ok"] is False
    assert row["journal_complete"] is False
    assert any(
        "assistant final trace count" in error for error in row["technical"]["errors"]
    )
    assert "invalid:model_skipped" in row["journal_completeness_errors"]
