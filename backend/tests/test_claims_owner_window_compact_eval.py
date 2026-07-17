"""Tests for the read-only frozen compact-floor selector and finalizer."""

from __future__ import annotations

import io
import json
from argparse import Namespace
from copy import deepcopy

import pytest

from scripts import run_claims_owner_window_compact_eval as compact
from scripts.run_claims_owner_window_compact_eval import (
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
) -> dict:
    return {
        "id": query_id,
        "shape": shape,
        "doc_hit": doc_hit,
        "answerability_ok": answerability_ok,
        "sources": [{"corpus_id": "corpus:test"}],
        "error": None,
        "done_received": True,
        "model_used": "anthropic/minimax-m2.7",
    }


def _green_rows() -> list[dict]:
    rows = [
        _row(query_id=f"direct:{index}", shape="direct_expert") for index in range(5)
    ]
    rows.append(_row(query_id="direct:fact", shape="direct_fact"))
    rows.extend(
        _row(query_id=f"lay:{index}", shape="lay_language") for index in range(4)
    )
    rows.extend(
        _row(query_id=f"negative:{index}", shape="negative_control")
        for index in range(3)
    )
    return rows


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
    monkeypatch.setattr(compact, "LOCK_PATH", lock_path)
    args = Namespace(
        lock_mode="assert-held",
        lock_owner="claims-window",
        lock_wait_seconds=0,
    )

    with pytest.raises(RuntimeError, match="requires the eval lock"):
        compact._lock_context(args)

    lock_path.write_text("other-window\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="owner mismatch"):
        compact._lock_context(args)

    lock_path.write_text("claims-window\n", encoding="utf-8")
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


def test_compact_chat_forces_temperature_zero_and_records_runtime_fields(
    monkeypatch,
):
    captured = {}
    payload = (
        'data: {"type":"token","content":"Answer"}\n\n'
        'data: {"type":"sources","sources":'
        '[{"corpus_id":"corpus:test","doc_name":"Expected.md"}]}\n\n'
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


def test_compact_finalizer_accepts_owner_floors():
    final = _finalize(
        _green_rows(),
        corpus_id="corpus:test",
        expected_model="anthropic/minimax-m2.7",
    )

    assert final["metrics"]["execution_count"] == 13
    assert final["metrics"]["direct_doc_hit_rate"] == 1.0
    assert final["metrics"]["lay_language_doc_hit_rate"] == 1.0
    assert final["metrics"]["original_negative_refusals"] == 3
    assert final["passed"] is True


def test_compact_finalizer_rejects_direct_regression():
    rows = _green_rows()
    rows[0]["doc_hit"] = False

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
