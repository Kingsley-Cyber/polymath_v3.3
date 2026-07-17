import hashlib
import json
from pathlib import Path

from services.eval_firewall import heldout_query_hash, is_heldout_query


EVALS = Path(__file__).resolve().parents[1] / "evals"
SPEC = EVALS / "e2e_heldout_negative_v2_20260717.json"
SHA_FILE = EVALS / "e2e_heldout_negative_v2_20260717.sha256"
FROZEN_SHA256 = "3b35c14c165f6be89202b809ea01a1cd6ad0f5c0217e4167b86e4b5dc0b09960"


def _spec() -> dict:
    return json.loads(SPEC.read_text(encoding="utf-8"))


def test_negative_v2_spec_is_byte_frozen_and_gate_only():
    assert hashlib.sha256(SPEC.read_bytes()).hexdigest() == FROZEN_SHA256
    assert SHA_FILE.read_text(encoding="utf-8").split()[0] == FROZEN_SHA256
    spec = _spec()
    assert spec["status"] == "frozen_gate_only"
    assert spec["used_for_tuning"] is False
    assert spec["acceptance"]["refusal_rate"] == 1.0


def test_negative_v2_has_28_unique_refusal_probes():
    queries = _spec()["queries"]
    assert len(queries) == 28
    assert len({row["id"] for row in queries}) == 28
    assert all(row["must_refuse"] is True for row in queries)


def test_negative_v2_queries_are_in_the_contamination_firewall():
    queries = _spec()["queries"]
    hashes = [heldout_query_hash(row["question"]) for row in queries]
    assert len(set(hashes)) == 28
    assert all(is_heldout_query(row["question"]) for row in queries)


def test_negative_v2_records_required_absence_swap_and_cross_corpus_proof():
    firewall = _spec()["contamination_firewall"]
    assert firewall["preregistration_swap"]["removed"].startswith("Walter Murch")
    assert firewall["preregistration_swap"]["replacement"].startswith("Bruce Block")
    assert firewall["mark_to_e2e_intersections"] == {
        "doc_id": 0,
        "content_or_source_sha256": 0,
        "normalized_filename": 0,
    }
    assert len(firewall["verified_absent_targets"]["f3_documents"]) == 4
    assert len(firewall["verified_absent_targets"]["f5_artifacts"]) == 4
