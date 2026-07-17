"""Tests for the read-only frozen compact-floor selector and finalizer."""

from __future__ import annotations

import io
import hashlib
import json
from argparse import Namespace
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from scripts import run_claims_owner_window_compact_eval as compact
from scripts.run_claims_owner_window_compact_eval import (
    STANDARD_TIERS,
    _finalize,
    _load_compact_queries,
)
from scripts.run_two_lane_anchoring_ab import _score_frozen


def _row(
    *,
    query_id: str,
    shape: str,
    doc_hit: bool = True,
    answerability_ok: bool = True,
    tier: str = "qdrant_mongo_graph",
) -> dict:
    return {
        "id": query_id,
        "shape": shape,
        "tier": tier,
        "doc_hit": doc_hit,
        "answerability_ok": answerability_ok,
        "sources": [{"corpus_id": "corpus:test"}],
        "error": None,
        "done_received": True,
        "model_used": "anthropic/minimax-m2.7",
        "request_temperature": 0,
        "journal_complete": True,
    }


def _green_rows() -> list[dict]:
    rows = []
    for tier in STANDARD_TIERS:
        rows.extend(
            _row(
                query_id=f"{tier}:direct:{index}",
                shape="direct_expert",
                tier=tier,
            )
            for index in range(5)
        )
        rows.append(
            _row(
                query_id=f"{tier}:direct:fact",
                shape="direct_fact",
                tier=tier,
            )
        )
        rows.extend(
            _row(
                query_id=f"{tier}:lay:{index}",
                shape="lay_language",
                tier=tier,
            )
            for index in range(4)
        )
        rows.extend(
            _row(
                query_id=f"{tier}:negative:{index}",
                shape="negative_control",
                tier=tier,
            )
            for index in range(3)
        )
    return rows


def _runtime(*, claims: bool) -> dict:
    return {
        "RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED": True,
        "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED": True,
        "TEMPORAL_QUERY_ROUTING_ENABLED": True,
        "RERANK_EVIDENCE_SUPPORT": False,
        "ATOMIC_CLAIM_ANCHORS_ENABLED": claims,
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
        "HYDRATION_MODE": "parent",
    }


def _claims_on_artifact(tmp_path):
    captured = datetime.now(timezone.utc)
    window = {
        "lock_owner": "claims-window",
        "window_nonce": "claims-window-nonce-0001",
        "window_not_before_utc": (captured - timedelta(seconds=1)).isoformat(),
    }
    artifact = {
        "schema_version": compact.OWNER_WINDOW_SCHEMA,
        "captured_at_utc": captured.isoformat(),
        "passed": True,
        "failures": [],
        "provider_calls": 0,
        "outer_host_lock": window,
        "corpus_id": "corpus:test",
        "corpus_fingerprint_equal": True,
        "fresh_off_corpus_fingerprint_equal": True,
        "raw_claim_store_byte_unchanged": True,
        "model_contract": "anthropic/minimax-m2.7",
        "off_system_prompt_template": {
            "method_version": "polymath.chat_system_prompt_render.v1",
            "sha256": "c" * 64,
            "source_sha256": "d" * 64,
        },
        "off_endpoint_binding": {
            "loopback_required": True,
            "same_container_prompt_binding": True,
        },
        "off_runtime": _runtime(claims=False),
        "runtime": _runtime(claims=True),
    }
    path = tmp_path / "claims-on.json"
    path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
    args = Namespace(
        claims_on_artifact=path,
        claims_on_artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        lock_owner=window["lock_owner"],
        window_nonce=window["window_nonce"],
        window_not_before_utc=window["window_not_before_utc"],
        corpus_id="corpus:test",
        expected_model="anthropic/minimax-m2.7",
        max_window_artifact_age_seconds=1800,
    )
    return path, artifact, args


def test_compact_selector_uses_exact_frozen_13_query_subset():
    queries, hashes = _load_compact_queries()

    assert len(queries) == 13
    assert sum(query["shape"].startswith("direct_") for query in queries) == 6
    assert sum(query["shape"] == "lay_language" for query in queries) == 4
    assert sum(query["shape"] == "negative_control" for query in queries) == 3
    assert not any(query["shape"] == "relationship_multi_document" for query in queries)
    assert len(hashes) == 2


def test_compact_runner_can_assert_a_surrounding_atomic_window_lock(
    monkeypatch,
    tmp_path,
):
    lock_path = tmp_path / "polymath-eval.lock"
    nonce_path = tmp_path / "polymath-eval.lock.nonce"
    monkeypatch.setattr(compact, "LOCK_PATH", lock_path)
    monkeypatch.setattr(compact, "LOCK_NONCE_PATH", nonce_path)
    args = Namespace(
        lock_mode="assert-held",
        lock_owner="claims-window",
        lock_wait_seconds=0,
        window_nonce="claims-window-nonce-0001",
    )

    with pytest.raises(RuntimeError, match="requires the eval lock"):
        with compact._lock_context(args):
            pass

    lock_path.write_text("other-window\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="owner mismatch"):
        with compact._lock_context(args):
            pass

    lock_path.write_text("claims-window\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="requires the eval lock nonce"):
        with compact._lock_context(args):
            pass

    nonce_path.write_text("wrong-nonce-value\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="nonce mismatch"):
        with compact._lock_context(args):
            pass

    nonce_path.write_text("claims-window-nonce-0001\n", encoding="utf-8")
    with compact._lock_context(args):
        assert lock_path.exists()


def test_compact_runner_reuses_existing_frozen_scorer():
    query = {
        "shape": "direct_fact",
        "expected_any": ["Expected.md"],
        "expected_min_distinct": 1,
    }
    result = {
        "answer": "Grounded answer.",
        "sources": [{"doc_name": "Expected.md"}],
        "two_lane_anchoring": None,
    }

    scored = _score_frozen(query, result)

    assert scored["doc_hit"] is True
    assert scored["answerability_ok"] is True


def test_compact_binds_to_green_claims_on_artifact_and_exact_window(tmp_path):
    _, _, args = _claims_on_artifact(tmp_path)

    receipt = compact._validate_claims_on_artifact(args)

    assert receipt["runtime"]["ATOMIC_CLAIM_ANCHORS_ENABLED"] is True
    assert receipt["off_runtime"]["ATOMIC_CLAIM_ANCHORS_ENABLED"] is False
    assert receipt["outer_host_lock"]["window_nonce"] == args.window_nonce


def test_compact_rejects_non_claim_runtime_delta(tmp_path):
    path, artifact, args = _claims_on_artifact(tmp_path)
    artifact["runtime"]["WATERFALL_ASSEMBLY"] = True
    path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
    args.claims_on_artifact_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="runtime mismatch"):
        compact._validate_claims_on_artifact(args)


def test_compact_chat_forces_temperature_zero_and_records_runtime_fields(
    monkeypatch,
):
    captured = {}
    payload = (
        'data: {"type":"token","content":"Answer"}\n\n'
        'data: {"type":"sources","sources":'
        '[{"corpus_id":"corpus:test","doc_name":"Expected.md"}]}\n\n'
        'data: {"type":"trace_event","trace_event":'
        '{"title":"Answerability gate","metadata":'
        '{"raw_answerable":true,"corpus_scope_guard":'
        '{"eligible":true,"coverage":1.0}}}}\n\n'
        'data: {"type":"trace_event","trace_event":'
        '{"title":"Assistant final answer","metadata":{"model_skipped":false}}}\n\n'
        'data: {"type":"trace_event","trace_event":'
        '{"title":"Chat model route","metadata":'
        '{"model":"anthropic/minimax-m2.7"}}}\n\n'
        'data: {"type":"done","model_used":"anthropic/minimax-m2.7"}\n\n'
    ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return io.BytesIO(payload)

    monkeypatch.setattr(compact.urllib.request, "urlopen", fake_urlopen)

    result = compact._chat_temperature_zero(
        api="http://example.test",
        token="test-token",
        question="Question?",
        corpus_id="corpus:test",
        tier="qdrant_mongo_graph",
        timeout=12.0,
    )

    assert captured["body"]["overrides"] == {"temperature": 0}
    assert captured["timeout"] == 12.0
    assert result["answer"] == "Answer"
    assert result["done_received"] is True
    assert result["model_used"] == "anthropic/minimax-m2.7"
    assert result["request_temperature"] == 0
    assert result["trace_contract"]["ok"] is True
    assert result["answerability"]["ok"] is True


def test_compact_chat_marks_missing_final_trace_incomplete(monkeypatch):
    payload = (
        'data: {"type":"token","content":"Answer"}\n\n'
        'data: {"type":"trace_event","trace_event":'
        '{"title":"Answerability gate","metadata":'
        '{"raw_answerable":true,"corpus_scope_guard":'
        '{"eligible":true,"coverage":1.0}}}}\n\n'
        'data: {"type":"trace_event","trace_event":'
        '{"title":"Chat model route","metadata":'
        '{"model":"anthropic/minimax-m2.7"}}}\n\n'
        'data: {"type":"done","model_used":"anthropic/minimax-m2.7"}\n\n'
    ).encode("utf-8")

    monkeypatch.setattr(
        compact.urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(payload),
    )

    result = compact._chat_temperature_zero(
        api="http://example.test",
        token="test-token",
        question="Question?",
        corpus_id="corpus:test",
        tier="qdrant_mongo_graph",
        timeout=12.0,
    )

    assert result["model_skipped"] is None
    assert result["trace_contract"]["ok"] is False
    assert any(
        "assistant final trace count" in error
        for error in result["trace_contract"]["errors"]
    )


def test_compact_finalizer_accepts_owner_floors():
    final = _finalize(
        _green_rows(),
        corpus_id="corpus:test",
        expected_model="anthropic/minimax-m2.7",
    )

    assert final["metrics"]["execution_count"] == 39
    assert final["metrics"]["direct_doc_hit_rate"] == 1.0
    assert final["metrics"]["lay_language_doc_hit_rate"] == 1.0
    assert final["metrics"]["original_negative_refusals"] == 9
    assert final["passed"] is True


def test_compact_finalizer_rejects_direct_regression():
    rows = _green_rows()
    direct_rows = [
        row for row in rows if row["shape"] in {"direct_expert", "direct_fact"}
    ]
    for row in direct_rows[:3]:
        row["doc_hit"] = False

    final = _finalize(
        rows,
        corpus_id="corpus:test",
        expected_model="anthropic/minimax-m2.7",
    )

    assert final["metrics"]["direct_doc_hit_rate"] == 0.8333
    assert final["gates"]["direct"] is False
    assert final["passed"] is False


def test_compact_finalizer_rejects_lay_negative_and_membership_regressions():
    rows = _green_rows()
    for row in rows:
        if row["shape"] == "lay_language":
            row["doc_hit"] = False
    negative = next(row for row in rows if row["shape"] == "negative_control")
    negative["answerability_ok"] = False
    negative["sources"][0]["corpus_id"] = "corpus:foreign"

    final = _finalize(
        rows,
        corpus_id="corpus:test",
        expected_model="anthropic/minimax-m2.7",
    )

    assert final["gates"]["lay"] is False
    assert final["gates"]["original_negatives"] is False
    assert final["gates"]["corpus_citation_membership"] is False


def test_compact_finalizer_rejects_missing_execution():
    rows = deepcopy(_green_rows())
    rows.pop()

    final = _finalize(
        rows,
        corpus_id="corpus:test",
        expected_model="anthropic/minimax-m2.7",
    )

    assert final["gates"]["execution_closure"] is False


def test_compact_finalizer_rejects_missing_standard_tier():
    rows = [row for row in _green_rows() if row["tier"] != "qdrant_only"]

    final = _finalize(
        rows,
        corpus_id="corpus:test",
        expected_model="anthropic/minimax-m2.7",
    )

    assert final["gates"]["execution_closure"] is False
    assert final["passed"] is False


def test_compact_finalizer_rejects_missing_done_or_model_drift():
    rows = _green_rows()
    rows[0]["done_received"] = False
    rows[1]["model_used"] = "wrong/model"

    final = _finalize(
        rows,
        corpus_id="corpus:test",
        expected_model="anthropic/minimax-m2.7",
    )

    assert final["gates"]["technical_success"] is False


def test_compact_parser_has_no_cli_token_option():
    option_strings = {
        option
        for action in compact._parser()._actions
        for option in action.option_strings
    }

    assert "--token" not in option_strings
