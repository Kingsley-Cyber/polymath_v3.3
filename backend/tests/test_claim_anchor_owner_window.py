"""Contract tests for the fresh-baseline claims owner-window harness."""

from __future__ import annotations

import json
from copy import deepcopy
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
                "done_received": True,
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


def test_owner_window_requires_exact_existing_eval_lock(monkeypatch, tmp_path):
    lock_path = tmp_path / "polymath-eval.lock"
    monkeypatch.setattr(harness, "EVAL_LOCK_PATH", lock_path)

    with pytest.raises(RuntimeError, match="requires the eval lock"):
        harness._require_eval_lock("claims-window")

    lock_path.write_text("other-window\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="owner mismatch"):
        harness._require_eval_lock("claims-window")

    lock_path.write_text("claims-window\n", encoding="utf-8")
    harness._require_eval_lock("claims-window")


def test_attested_fresh_off_artifact_round_trips_with_explicit_sha(tmp_path):
    spec, _ = harness._load_v2_contract(V2_SPEC)
    runtime = harness._runtime_snapshot(_runtime(claims=False))
    off = harness._attest_off_payload(
        off=_off_payload(spec),
        spec=spec,
        runtime=runtime,
    )
    path = tmp_path / "fresh-off.json"
    path.write_text(json.dumps(off, sort_keys=True), encoding="utf-8")
    digest = harness._sha256_file(path)

    validated = harness._validate_fresh_off_artifact(
        off_path=path,
        expected_sha256=digest,
        spec=spec,
    )

    assert validated["arm"] == "off"
    assert (
        validated["owner_window_attestation"]["capture_runtime"][
            "temporal_query_routing_enabled"
        ]
        is True
    )


def test_fresh_off_rejects_wrong_explicit_sha(tmp_path):
    spec, _ = harness._load_v2_contract(V2_SPEC)
    off = harness._attest_off_payload(
        off=_off_payload(spec),
        spec=spec,
        runtime=harness._runtime_snapshot(_runtime(claims=False)),
    )
    path = tmp_path / "fresh-off.json"
    path.write_text(json.dumps(off, sort_keys=True), encoding="utf-8")

    with pytest.raises(RuntimeError, match="SHA drifted"):
        harness._validate_fresh_off_artifact(
            off_path=path,
            expected_sha256="0" * 64,
            spec=spec,
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
