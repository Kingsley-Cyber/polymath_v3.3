"""Contract tests for the fresh-baseline claims owner-window harness."""

from __future__ import annotations

import io
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts import run_claim_anchor_owner_window as harness
from scripts.run_claim_anchor_additivity_replay import V2_SPEC
from scripts.run_claim_anchor_micro_ab import _source_fingerprint


def _runtime(*, claims: bool, temporal: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        ATOMIC_CLAIM_ANCHORS_ENABLED=claims,
        TEMPORAL_QUERY_ROUTING_ENABLED=temporal,
        RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED=True,
        ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED=True,
        TWO_LANE_ANCHORING_ENABLED=False,
        TWO_LANE_ANCHORING=False,
        FOUR_LANE_TIER0_ROUTER_ENABLED=False,
        FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED=False,
        WATERFALL_ASSEMBLY=False,
        HYDRATION_MODE="parent",
        RERANK_EVIDENCE_SUPPORT=False,
        PARENT_EXCERPT_ENABLED=False,
        HYDE_ENABLED=False,
        SHELF_RESERVE_ENABLED=False,
        GROUNDED_QUERY_PLANNER_ENABLED=False,
        AGENTIC_MODE_ENABLED=False,
    )


def _window(now: datetime | None = None) -> dict[str, str]:
    current = now or datetime(2026, 7, 18, tzinfo=timezone.utc)
    return harness._window_identity(
        lock_owner="claims-window",
        window_nonce="claims-window-nonce-0001",
        window_not_before_utc=(current - timedelta(seconds=1)).isoformat(),
    )


def _source(query_id: str) -> dict:
    return {
        "corpus_id": "corpus:test",
        "doc_id": f"doc:{query_id}",
        "chunk_id": f"chunk:{query_id}",
        "parent_id": "",
        "text": f"Evidence for {query_id}.",
        "score": 0.9,
        "metadata": {},
    }


def _off_payload(spec: dict) -> dict:
    fingerprint = {
        "collections": {
            "semantic_digest_claim_compilations": {
                "count": 6,
                "sha256": "a" * 64,
            }
        },
        "combined_sha256": "b" * 64,
    }
    prompt_receipt = {
        "method_version": "polymath.chat_system_prompt_render.v1",
        "sha256": "c" * 64,
        "source_sha256": "d" * 64,
        "source_path": "services/chat_orchestrator.py",
        "rendered_for_local_date": "2026-07-18",
        "rendered_for_timezone_name": "UTC",
        "utf8_bytes": 100,
        "builder": "services.chat_orchestrator._build_polymath_system_prompt",
    }
    rows = []
    for query_id in spec["query_ids"]:
        source = _source(query_id)
        rows.append(
            {
                "query_id": query_id,
                "source_keys": [
                    {
                        "corpus_id": source["corpus_id"],
                        "doc_id": source["doc_id"],
                        "chunk_id": source["chunk_id"],
                        "parent_id": source["parent_id"],
                    }
                ],
                "selected_sources": [source],
                "selected_evidence_sha256_without_anchors": _source_fingerprint(
                    [source]
                ),
                "anchor_count": 0,
                "prompt_render_count": 0,
                "model_used": spec["model_contract"],
                "model_skipped": False,
                "model_route": {"model": spec["model_contract"]},
                "answerability": {
                    "ok": True,
                    "errors": [],
                    "telemetry": {
                        "raw_answerable": True,
                        "corpus_scope_guard": {
                            "eligible": True,
                            "coverage": 1.0,
                        },
                    },
                    "raw_answerable": True,
                    "guard": {"eligible": True, "coverage": 1.0},
                },
                "request_temperature": 0,
                "system_prompt_template": prompt_receipt,
                "prior_call_session_state": {
                    "history_turn_count": 0,
                    "conversation_id_sent": False,
                    "history_receipts": [],
                },
                "done_received": True,
                "journal_complete": True,
                "errors": [],
            }
        )
    return {
        "schema_version": "claim_anchor_join_micro_ab_arm.v1",
        "arm": "off",
        "runtime_flag_enabled": False,
        "spec": spec,
        "corpus_id": "corpus:test",
        "model_contract": spec["model_contract"],
        "request_temperature": 0,
        "endpoint_binding": {
            "loopback_required": True,
            "same_container_prompt_binding": True,
        },
        "system_prompt_template": prompt_receipt,
        "prompt_render_context_stable": True,
        "corpus_fingerprint_before": fingerprint,
        "corpus_fingerprint_after": deepcopy(fingerprint),
        "corpus_fingerprint_equal": True,
        "results": rows,
        "failures": [],
        "passed": True,
    }


def _green_results(spec: dict) -> list[dict]:
    results = []
    for index, query_id in enumerate(spec["query_ids"]):
        anchor_count = 2 if query_id == "q021" else (4 if index < 5 else 2)
        results.append(
            {
                "query_id": query_id,
                "source_ids_equal": True,
                "non_anchor_evidence_bytes_equal": True,
                "service_additivity_verified": True,
                "anchor_count": anchor_count,
                "valid_anchor_count": anchor_count,
                "all_citations_valid": True,
                "prompt_render_count": anchor_count,
                "readable_claim_count": anchor_count,
                "prompt_claim_block_readable": True,
                "raw_claim_text_preserved": True,
                "model_contract": spec["model_contract"],
            }
        )
    return results


def test_owner_window_pins_existing_v2_spec_without_modifying_it():
    spec, questions = harness._load_v2_contract(V2_SPEC)

    assert spec["schema_version"] == "claim_anchor_join_micro_ab.v2"
    assert [question["id"] for question in questions] == spec["query_ids"]
    assert harness._sha256_file(V2_SPEC) == harness.V2_SPEC_SHA256


def test_runtime_contract_requires_temporal_on_in_both_arms():
    off_runtime = harness._runtime_snapshot(_runtime(claims=False))
    on_runtime = harness._runtime_snapshot(_runtime(claims=True))

    harness._require_runtime(off_runtime, claim_anchors_enabled=False)
    harness._require_runtime(on_runtime, claim_anchors_enabled=True)

    with pytest.raises(RuntimeError, match="runtime mismatch"):
        harness._require_runtime(
            harness._runtime_snapshot(_runtime(claims=False, temporal=False)),
            claim_anchors_enabled=False,
        )


def test_runtime_transition_allows_only_the_claim_flag():
    off_runtime = harness._runtime_snapshot(_runtime(claims=False))
    on_runtime = harness._runtime_snapshot(_runtime(claims=True))

    harness._require_claim_only_transition(off_runtime, on_runtime)

    on_runtime["TWO_LANE_ANCHORING_ENABLED"] = True
    with pytest.raises(RuntimeError, match="outside the claim flag"):
        harness._require_claim_only_transition(off_runtime, on_runtime)


def test_owner_window_requires_exact_outer_lock_environment(monkeypatch):
    values = {
        "POLYMATH_EVAL_LOCK_OWNER": "claims-window",
        "POLYMATH_EVAL_WINDOW_NONCE": "claims-window-nonce-0001",
        "POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC": "2026-07-18T00:00:00+00:00",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match="outer host-lock attestation"):
        harness._require_outer_lock_environment(
            lock_owner=values["POLYMATH_EVAL_LOCK_OWNER"],
            window_nonce=values["POLYMATH_EVAL_WINDOW_NONCE"],
            window_not_before_utc=values["POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC"],
        )

    monkeypatch.setenv("POLYMATH_EVAL_OUTER_LOCK_ATTESTED", "1")
    harness._require_outer_lock_environment(
        lock_owner=values["POLYMATH_EVAL_LOCK_OWNER"],
        window_nonce=values["POLYMATH_EVAL_WINDOW_NONCE"],
        window_not_before_utc=values["POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC"],
    )

    monkeypatch.setenv("POLYMATH_EVAL_WINDOW_NONCE", "wrong")
    with pytest.raises(RuntimeError, match="environment drifted"):
        harness._require_outer_lock_environment(
            lock_owner=values["POLYMATH_EVAL_LOCK_OWNER"],
            window_nonce=values["POLYMATH_EVAL_WINDOW_NONCE"],
            window_not_before_utc=values["POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC"],
        )


def test_attested_fresh_off_artifact_round_trips_with_explicit_sha(tmp_path):
    spec, _ = harness._load_v2_contract(V2_SPEC)
    runtime = harness._runtime_snapshot(_runtime(claims=False))
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    window = _window(now)
    off = harness._attest_off_payload(
        off=_off_payload(spec),
        spec=spec,
        runtime=runtime,
        window=window,
        now=now,
    )
    path = tmp_path / "fresh-off.json"
    path.write_text(json.dumps(off, sort_keys=True), encoding="utf-8")
    digest = harness._sha256_file(path)

    validated = harness._validate_fresh_off_artifact(
        off_path=path,
        expected_sha256=digest,
        spec=spec,
        expected_window=window,
        max_age_seconds=1800,
        now=now,
    )

    assert validated["arm"] == "off"
    assert (
        validated["owner_window_attestation"]["capture_runtime"][
            "TEMPORAL_QUERY_ROUTING_ENABLED"
        ]
        is True
    )


def test_fresh_off_rejects_wrong_explicit_sha(tmp_path):
    spec, _ = harness._load_v2_contract(V2_SPEC)
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    window = _window(now)
    off = harness._attest_off_payload(
        off=_off_payload(spec),
        spec=spec,
        runtime=harness._runtime_snapshot(_runtime(claims=False)),
        window=window,
        now=now,
    )
    path = tmp_path / "fresh-off.json"
    path.write_text(json.dumps(off, sort_keys=True), encoding="utf-8")

    with pytest.raises(RuntimeError, match="SHA drifted"):
        harness._validate_fresh_off_artifact(
            off_path=path,
            expected_sha256="0" * 64,
            spec=spec,
            expected_window=window,
            max_age_seconds=1800,
            now=now,
        )


def test_owner_window_explicitly_rejects_stale_v1_packet(monkeypatch, tmp_path):
    spec, _ = harness._load_v2_contract(V2_SPEC)
    path = tmp_path / "stale-off.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        harness,
        "_sha256_file",
        lambda _path: harness.SEALED_V1_OFF_SHA256,
    )

    with pytest.raises(RuntimeError, match="stale pinned v1"):
        harness._validate_fresh_off_artifact(
            off_path=path,
            expected_sha256=harness.SEALED_V1_OFF_SHA256,
            spec=spec,
            expected_window=_window(),
            max_age_seconds=1800,
        )


def test_fresh_off_rejects_wrong_window_nonce(tmp_path):
    spec, _ = harness._load_v2_contract(V2_SPEC)
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    window = _window(now)
    off = harness._attest_off_payload(
        off=_off_payload(spec),
        spec=spec,
        runtime=harness._runtime_snapshot(_runtime(claims=False)),
        window=window,
        now=now,
    )
    path = tmp_path / "fresh-off.json"
    path.write_text(json.dumps(off, sort_keys=True), encoding="utf-8")
    wrong_window = dict(window)
    wrong_window["window_nonce"] = "claims-window-nonce-9999"

    with pytest.raises(RuntimeError, match="outer lock identity drifted"):
        harness._validate_fresh_off_artifact(
            off_path=path,
            expected_sha256=harness._sha256_file(path),
            spec=spec,
            expected_window=wrong_window,
            max_age_seconds=1800,
            now=now,
        )


def test_fresh_off_rejects_expired_attestation(tmp_path):
    spec, _ = harness._load_v2_contract(V2_SPEC)
    captured = datetime(2026, 7, 18, tzinfo=timezone.utc)
    window = _window(captured)
    off = harness._attest_off_payload(
        off=_off_payload(spec),
        spec=spec,
        runtime=harness._runtime_snapshot(_runtime(claims=False)),
        window=window,
        now=captured,
    )
    path = tmp_path / "fresh-off.json"
    path.write_text(json.dumps(off, sort_keys=True), encoding="utf-8")

    with pytest.raises(RuntimeError, match="exceeded"):
        harness._validate_fresh_off_artifact(
            off_path=path,
            expected_sha256=harness._sha256_file(path),
            spec=spec,
            expected_window=window,
            max_age_seconds=60,
            now=captured + timedelta(seconds=61),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda off: off.__setitem__("runtime_flag_enabled", True),
            "off_runtime_claim_flag_not_false",
        ),
        (
            lambda off: off["results"][0].__setitem__("anchor_count", 1),
            "off_anchor_exposure",
        ),
        (
            lambda off: off["results"][0].__setitem__(
                "selected_evidence_sha256_without_anchors", "0" * 64
            ),
            "selected_evidence_hash_drift",
        ),
        (
            lambda off: off["results"][0].__setitem__("model_used", "wrong/model"),
            "model_contract",
        ),
    ],
)
def test_off_payload_rejects_adjacent_contract_drift(mutation, message):
    spec, _ = harness._load_v2_contract(V2_SPEC)
    off = _off_payload(spec)
    mutation(off)

    with pytest.raises(RuntimeError, match=message):
        harness._validate_off_payload_base(off=off, spec=spec)


def test_replay_failure_gate_accepts_all_strict_v2_invariants():
    spec, _ = harness._load_v2_contract(V2_SPEC)
    off = _off_payload(spec)
    before = off["corpus_fingerprint_after"]
    results = _green_results(spec)

    failures = harness._replay_failures(
        spec=spec,
        off=off,
        before=before,
        after=deepcopy(before),
        results=results,
    )

    assert sum(row["anchor_count"] for row in results) >= 18
    assert failures == []


@pytest.mark.parametrize(
    ("field", "value", "failure"),
    [
        ("source_ids_equal", False, "q021:source_identity"),
        (
            "non_anchor_evidence_bytes_equal",
            False,
            "q021:non_anchor_evidence",
        ),
        ("service_additivity_verified", False, "q021:service_additivity"),
        ("all_citations_valid", False, "q021:citation_invalid"),
        ("prompt_render_count", 1, "q021:not_all_anchors_rendered"),
        ("readable_claim_count", 1, "q021:not_all_claims_readable"),
        (
            "prompt_claim_block_readable",
            False,
            "q021:prompt_claim_block_unreadable",
        ),
        ("raw_claim_text_preserved", False, "q021:raw_claim_mutated"),
        ("model_contract", "wrong/model", "q021:model_contract"),
    ],
)
def test_replay_gate_rejects_each_per_query_invariant(field, value, failure):
    spec, _ = harness._load_v2_contract(V2_SPEC)
    off = _off_payload(spec)
    results = _green_results(spec)
    results[0][field] = value

    failures = harness._replay_failures(
        spec=spec,
        off=off,
        before=off["corpus_fingerprint_after"],
        after=deepcopy(off["corpus_fingerprint_after"]),
        results=results,
    )

    assert failure in failures


def test_replay_gate_binds_to_fresh_off_fingerprint_and_raw_store():
    spec, _ = harness._load_v2_contract(V2_SPEC)
    off = _off_payload(spec)
    before = deepcopy(off["corpus_fingerprint_after"])
    before["combined_sha256"] = "c" * 64

    failures = harness._replay_failures(
        spec=spec,
        off=off,
        before=before,
        after=deepcopy(before),
        results=_green_results(spec),
    )

    assert "fresh_off_corpus_fingerprint_drifted_before_replay" in failures


def test_replay_gate_rejects_raw_claim_store_byte_drift():
    spec, _ = harness._load_v2_contract(V2_SPEC)
    off = _off_payload(spec)
    before = deepcopy(off["corpus_fingerprint_after"])
    after = deepcopy(before)
    after["collections"]["semantic_digest_claim_compilations"]["sha256"] = "c" * 64

    failures = harness._replay_failures(
        spec=spec,
        off=off,
        before=before,
        after=after,
        results=_green_results(spec),
    )

    assert "raw_claim_store_changed" in failures
    assert "corpus_fingerprint_changed_during_replay" in failures


def test_render_readability_rejects_machine_grammar_but_accepts_prose():
    assert harness._render_is_readable("Dry testing validates a product.")
    assert not harness._render_is_readable("you POSITIVE POSSIBLE UNTYPED[use] dry")
    assert harness._prompt_claim_block_is_readable(
        "<atomic_claim_anchors>\n"
        '- From "Source": Dry testing validates a product.\n'
        "</atomic_claim_anchors>"
    )
    assert not harness._prompt_claim_block_is_readable(
        "<atomic_claim_anchors>\n"
        '- From "Source": you POSITIVE POSSIBLE UNTYPED[use] dry\n'
        "</atomic_claim_anchors>"
    )


def test_owner_capture_request_forces_temp_zero_and_strict_telemetry(monkeypatch):
    captured = {}
    payload = (
        'data: {"type":"trace_event","trace_event":'
        '{"title":"Answerability gate","metadata":'
        '{"raw_answerable":true,"corpus_scope_guard":'
        '{"eligible":true,"coverage":1.0}}}}\n\n'
        'data: {"type":"trace_event","trace_event":'
        '{"title":"Chat model route","metadata":'
        '{"model":"anthropic/minimax-m2.7"}}}\n\n'
        'data: {"type":"trace_event","trace_event":'
        '{"title":"Assistant final answer","metadata":{"model_skipped":false}}}\n\n'
        'data: {"type":"sources","sources":'
        '[{"corpus_id":"c","doc_id":"d","chunk_id":"x"}]}\n\n'
        'data: {"type":"done","model_used":"anthropic/minimax-m2.7"}\n\n'
    ).encode("utf-8")

    class Response(io.BytesIO):
        status = 200

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response(payload)

    monkeypatch.setattr(harness.urllib.request, "urlopen", fake_urlopen)

    result = harness._run_owner_sse(
        base="http://example.test",
        token="not-recorded",
        corpus_id="c",
        tier="qdrant_mongo_graph",
        question="Question?",
        timeout=12.0,
    )

    assert captured["payload"]["overrides"] == {"temperature": 0}
    assert captured["timeout"] == 12.0
    assert result["request_temperature"] == 0
    assert result["trace_contract"]["ok"] is True
    assert result["answerability"]["ok"] is True
    assert result["model_used"] == "anthropic/minimax-m2.7"


def test_owner_capture_missing_final_trace_is_red(monkeypatch):
    payload = (
        'data: {"type":"trace_event","trace_event":'
        '{"title":"Answerability gate","metadata":'
        '{"raw_answerable":true,"corpus_scope_guard":'
        '{"eligible":true,"coverage":1.0}}}}\n\n'
        'data: {"type":"trace_event","trace_event":'
        '{"title":"Chat model route","metadata":'
        '{"model":"anthropic/minimax-m2.7"}}}\n\n'
        'data: {"type":"done","model_used":"anthropic/minimax-m2.7"}\n\n'
    ).encode("utf-8")

    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(
        harness.urllib.request,
        "urlopen",
        lambda request, timeout: Response(payload),
    )

    result = harness._run_owner_sse(
        base="http://example.test",
        token="not-recorded",
        corpus_id="c",
        tier="qdrant_mongo_graph",
        question="Question?",
    )

    assert result["model_skipped"] is None
    assert result["trace_contract"]["ok"] is False
