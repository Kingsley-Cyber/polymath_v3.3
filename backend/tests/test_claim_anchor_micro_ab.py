"""Tests for the read-only claim-anchor micro A/B receipt harness."""

from __future__ import annotations

from copy import deepcopy

from scripts.run_claim_anchor_micro_ab import (
    DEFAULT_SPEC,
    _load_contract,
    _source_fingerprint,
    _validate_anchor,
)


class _Cursor:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def limit(self, count: int) -> "_Cursor":
        self.rows = self.rows[:count]
        return self

    def __iter__(self):
        return iter(self.rows)


class _Collection:
    def __init__(self, *, one: dict | None = None, many: list[dict] | None = None):
        self.one = one
        self.many = many or []

    def find_one(self, _query: dict):
        return deepcopy(self.one)

    def find(self, _query: dict):
        return _Cursor(deepcopy(self.many))


class _DB:
    def __init__(self):
        self.semantic_digest_claim_compilations = _Collection(
            one={
                "corpus_id": "corpus:test",
                "document_id": "doc:test",
                "child_id": "child:test",
                "source_version_id": "version:test",
                "evidence_refs": [
                    {
                        "evidence_ref_id": "sentence:test",
                        "quote": "Feedback changes the operating baseline.",
                        "start": 0,
                        "end": 40,
                    }
                ],
                "envelope": {
                    "artifact_revision_id": "revision:test",
                    "body": {
                        "claims": [
                            {
                                "claim_id": "claim:test",
                                "canonical_proposition": (
                                    "feedback changes operating baseline"
                                ),
                                "evidence_sentence_ids": ["sentence:test"],
                            }
                        ]
                    },
                },
            }
        )
        self.chunks = _Collection(
            one={"text": "Feedback changes the operating baseline."}
        )
        self.documents = _Collection(one={"doc_id": "doc:test"})
        self.parent_chunks = _Collection(
            many=[
                {
                    "corpus_id": "corpus:test",
                    "doc_id": "doc:test",
                    "parent_id": "parent:test",
                    "child_ids": ["child:test"],
                    "source_child_ids": ["child:test"],
                }
            ]
        )


def _source() -> dict:
    return {
        "corpus_id": "corpus:test",
        "doc_id": "doc:test",
        "chunk_id": "parent:test_summary",
        "parent_id": "parent:test",
        "text": "Selected parent summary.",
        "score": 0.91,
        "metadata": {
            "atomic_claim_anchors": [{"claim_id": "claim:test"}],
            "retrieval_lane": "summary",
        },
    }


def _anchor() -> dict:
    return {
        "claim_id": "claim:test",
        "claim_text": "feedback changes operating baseline",
        "evidence_ref_id": "sentence:test",
        "exact_sentence": "Feedback changes the operating baseline.",
        "child_id": "child:test",
        "selected_chunk_id": "parent:test_summary",
        "mapped_parent_id": "parent:test",
        "source_version_id": "version:test",
        "compilation_revision_id": "revision:test",
        "start": 0,
        "end": 40,
    }


def test_micro_ab_contract_is_six_frozen_mark_queries():
    spec, questions = _load_contract(DEFAULT_SPEC)

    assert spec["query_ids"] == ["q021", "q022", "q023", "q024", "q025", "q029"]
    assert [row["id"] for row in questions] == spec["query_ids"]
    assert {row["corpora"][0] for row in questions} == {"markbuildsbrands_transcripts"}


def test_source_fingerprint_ignores_only_attached_claim_anchors():
    source = _source()
    changed_anchor = deepcopy(source)
    changed_anchor["metadata"]["atomic_claim_anchors"] = [{"claim_id": "other"}]
    changed_score = deepcopy(source)
    changed_score["score"] = 0.89

    assert _source_fingerprint([source]) == _source_fingerprint([changed_anchor])
    assert _source_fingerprint([source]) != _source_fingerprint([changed_score])


def test_micro_ab_validator_accepts_exact_sentence_to_parent_mapping():
    checks = _validate_anchor(_DB(), source=_source(), anchor=_anchor())

    assert checks == {
        "selected_source_ownership": True,
        "exact_span": True,
        "claim_identity": True,
        "provenance_closure": True,
        "valid": True,
    }


def test_micro_ab_validator_rejects_foreign_mapped_child():
    anchor = _anchor()
    anchor["child_id"] = "child:foreign"

    checks = _validate_anchor(_DB(), source=_source(), anchor=anchor)

    assert checks["selected_source_ownership"] is False
    assert checks["valid"] is False
