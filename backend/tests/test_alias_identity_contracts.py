"""Phase-1 contract tests for AliasCandidateV1 / Decision / Entity models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.alias_identity import (
    AliasCandidateV1,
    AliasDecisionV1,
    CorpusEntityV1,
    DocumentEntityV1,
    build_alias_candidate_id,
)


def _rag_candidate(**overrides):
    kwargs = dict(
        candidate_type="acronym_long_form",
        source_method="schwartz_hearst_acronym",
        rule_id="SCHWARTZ_HEARST_V1",
        rule_release="schwartz_hearst.v1",
        canonical_surface="Retrieval-Augmented Generation",
        candidate_surface="RAG",
        document_id="doc:alias_01",
        chunk_id="chunk:alias_01",
        sentence_id="sentence:alias_01:0",
        canonical_start=0,
        canonical_end=31,
        candidate_start=33,
        candidate_end=36,
        evidence_text="Retrieval-Augmented Generation (RAG)",
        scope="document",
        confidence=0.98,
    )
    kwargs.update(overrides)
    return AliasCandidateV1.create(**kwargs)


def test_alias_candidate_ids_are_deterministic():
    a = _rag_candidate()
    b = _rag_candidate()
    assert a.alias_candidate_id == b.alias_candidate_id
    assert a.contract_hash == b.contract_hash
    assert a.alias_candidate_id.startswith("aliascand:")


def test_alias_candidate_id_changes_with_span():
    a = _rag_candidate()
    b = _rag_candidate(candidate_end=37)
    assert a.alias_candidate_id != b.alias_candidate_id


def test_alias_candidate_rejects_missing_positive_span():
    with pytest.raises(ValidationError):
        _rag_candidate(canonical_start=10, canonical_end=10)


def test_alias_candidate_rejects_tampered_id():
    cand = _rag_candidate()
    payload = cand.model_dump()
    payload["alias_candidate_id"] = "aliascand:tampered"
    with pytest.raises(ValidationError):
        AliasCandidateV1(**payload)


def test_decision_hashes_are_deterministic():
    cand = _rag_candidate()
    d1 = AliasDecisionV1.create(
        alias_candidate_id=cand.alias_candidate_id,
        decision="ACCEPT_IDENTITY",
        decision_reason="schwartz_hearst_validated",
        identity_merge_allowed=True,
        retrieval_expansion_allowed=True,
        scope="document",
        ambiguity_status="unambiguous",
    )
    d2 = AliasDecisionV1.create(
        alias_candidate_id=cand.alias_candidate_id,
        decision="ACCEPT_IDENTITY",
        decision_reason="schwartz_hearst_validated",
        identity_merge_allowed=True,
        retrieval_expansion_allowed=True,
        scope="document",
        ambiguity_status="unambiguous",
    )
    assert d1.decision_hash == d2.decision_hash


def test_retrieval_only_forbids_identity_merge():
    cand = _rag_candidate()
    with pytest.raises(ValidationError):
        AliasDecisionV1.create(
            alias_candidate_id=cand.alias_candidate_id,
            decision="ACCEPT_RETRIEVAL_ONLY",
            decision_reason="extraction_surface_variant",
            identity_merge_allowed=True,
            retrieval_expansion_allowed=True,
            scope="document",
            ambiguity_status="not_applicable",
        )


def test_reject_forbids_identity_merge():
    cand = _rag_candidate()
    with pytest.raises(ValidationError):
        AliasDecisionV1.create(
            alias_candidate_id=cand.alias_candidate_id,
            decision="REJECT",
            decision_reason="descriptive_apposition",
            identity_merge_allowed=True,
            retrieval_expansion_allowed=False,
            scope="sentence",
            ambiguity_status="not_applicable",
        )


def test_document_and_corpus_entity_hashes_stable():
    cand = _rag_candidate()
    decision = AliasDecisionV1.create(
        alias_candidate_id=cand.alias_candidate_id,
        decision="ACCEPT_IDENTITY",
        decision_reason="ok",
        identity_merge_allowed=True,
        retrieval_expansion_allowed=True,
        scope="document",
        ambiguity_status="unambiguous",
    )
    doc_a = DocumentEntityV1.create(
        document_id="doc:alias_01",
        canonical_name="Retrieval-Augmented Generation",
        mention_ids=["mention:1", "mention:0"],
        accepted_alias_ids=[decision.alias_candidate_id],
        retrieval_variant_ids=[],
        description_records=[],
    )
    doc_b = DocumentEntityV1.create(
        document_id="doc:alias_01",
        canonical_name="Retrieval-Augmented Generation",
        mention_ids=["mention:0", "mention:1"],
        accepted_alias_ids=[decision.alias_candidate_id],
        retrieval_variant_ids=[],
        description_records=[],
    )
    assert doc_a.document_entity_id == doc_b.document_entity_id
    assert doc_a.cluster_hash == doc_b.cluster_hash

    corp_a = CorpusEntityV1.create(
        corpus_id="corpus:alias_fixture",
        canonical_name="Retrieval-Augmented Generation",
        document_entity_ids=[doc_a.document_entity_id],
        trusted_aliases=["RAG"],
        retrieval_surface_variants=[],
        related_terms=[],
        descriptions=[],
        source_document_ids=[doc_a.document_id],
        canonical_name_rule="validated_acronym_long_form_v1",
    )
    corp_b = CorpusEntityV1.create(
        corpus_id="corpus:alias_fixture",
        canonical_name="Retrieval-Augmented Generation",
        document_entity_ids=[doc_a.document_entity_id],
        trusted_aliases=["RAG"],
        retrieval_surface_variants=[],
        related_terms=[],
        descriptions=[],
        source_document_ids=[doc_a.document_id],
        canonical_name_rule="validated_acronym_long_form_v1",
    )
    assert corp_a.corpus_entity_id == corp_b.corpus_entity_id
    assert "RAG" in corp_a.trusted_aliases


def test_build_alias_candidate_id_helper_matches_create():
    cand = _rag_candidate()
    rebuilt = build_alias_candidate_id(
        candidate_type=cand.candidate_type,
        rule_id=cand.rule_id,
        document_id=cand.document_id,
        chunk_id=cand.chunk_id,
        sentence_id=cand.sentence_id,
        canonical_start=cand.canonical_start,
        canonical_end=cand.canonical_end,
        candidate_start=cand.candidate_start,
        candidate_end=cand.candidate_end,
        canonical_surface=cand.canonical_surface,
        candidate_surface=cand.candidate_surface,
    )
    assert rebuilt == cand.alias_candidate_id
