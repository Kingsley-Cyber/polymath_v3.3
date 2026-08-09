"""Phase-5 parent aggregation + document identity clustering."""

from __future__ import annotations

from models.alias_identity import AliasCandidateV1, AliasDecisionV1, ParentAliasBundleV1
from services.ingestion.alias_document_clustering import (
    cluster_document_entities,
    cluster_from_candidates_and_decisions,
)
from services.ingestion.alias_gate import run_alias_gate
from services.ingestion.alias_parent_aggregation import (
    ChildAliasEvidence,
    ParentSourceRegion,
    aggregate_parent_alias_evidence,
)


def _cand(**overrides) -> AliasCandidateV1:
    kwargs = dict(
        candidate_type="acronym_long_form",
        source_method="schwartz_hearst_acronym",
        rule_id="SCHWARTZ_HEARST_V1",
        rule_release="alias_candidate_rules.v1",
        canonical_surface="Retrieval-Augmented Generation",
        candidate_surface="RAG",
        document_id="doc:p5",
        chunk_id="child:1",
        sentence_id="doc:p5:sent:0",
        canonical_start=0,
        canonical_end=30,
        candidate_start=32,
        candidate_end=35,
        evidence_text="Retrieval-Augmented Generation (RAG)",
        scope="document",
        confidence=0.98,
    )
    kwargs.update(overrides)
    return AliasCandidateV1.create(**kwargs)


def _evidence(cand: AliasCandidateV1, decision: AliasDecisionV1, **meta) -> ChildAliasEvidence:
    return ChildAliasEvidence(
        candidate=cand,
        decision=decision,
        parent_id=meta.get("parent_id", "parent:17"),
        child_id=meta.get("child_id", cand.chunk_id),
        child_text=meta.get("child_text", ""),
        child_start_in_parent=meta.get("child_start_in_parent"),
        child_end_in_parent=meta.get("child_end_in_parent"),
    )


def test_explicit_definition_plus_later_usage_supports_siblings():
    defining = _cand(chunk_id="child:17:1")
    # Usage-only surface variant on sibling (retrieval-only after gate).
    usage_cand = _cand(
        candidate_type="extraction_surface_variant",
        source_method="extraction_surface_variant",
        rule_id="RELEX_SURFACE_VARIANT_V1",
        canonical_surface="RAG",
        candidate_surface="RAG",
        chunk_id="child:17:2",
        sentence_id="doc:p5:sent:1",
        canonical_start=0,
        canonical_end=3,
        candidate_start=0,
        candidate_end=3,
        evidence_text="RAG retrieves external documents before answering.",
        confidence=0.5,
    )
    gate = run_alias_gate([defining, usage_cand])
    by_id = {d.alias_candidate_id: d for d in gate.decisions}
    rows = [
        _evidence(
            defining,
            by_id[defining.alias_candidate_id],
            child_id="child:17:1",
            child_text="Retrieval-Augmented Generation (RAG) combines retrieval and generation.",
        ),
        _evidence(
            usage_cand,
            by_id[usage_cand.alias_candidate_id],
            child_id="child:17:2",
            child_text="RAG retrieves external documents before answering.",
        ),
    ]
    batch = cluster_document_entities(rows)
    identity_bundles = [
        b
        for b in batch.parent_bundles
        if b.decision == "ACCEPT_IDENTITY" and b.candidate_surface == "RAG"
    ]
    assert len(identity_bundles) == 1
    bundle = identity_bundles[0]
    assert "child:17:1" in bundle.defining_child_ids
    assert "child:17:1" in bundle.supporting_child_ids
    assert "child:17:2" in bundle.supporting_child_ids
    assert bundle.identity_merge_allowed is True

    ents = [e for e in batch.entities if "Retrieval-Augmented" in e.canonical_name]
    assert len(ents) == 1
    ent = ents[0]
    assert defining.alias_candidate_id in ent.accepted_alias_ids
    assert "child:17:1" in ent.defining_child_ids
    assert "child:17:2" in ent.supporting_child_ids


def test_cooccurrence_does_not_create_identity():
    mw = _cand(
        candidate_type="semantic_related_term",
        source_method="semantic_neighbor",
        rule_id="SEMANTIC_RELATED_V1",
        canonical_surface="movement writing",
        candidate_surface="choreography",
        chunk_id="child:a",
        sentence_id="doc:p5:sent:a",
        canonical_start=0,
        canonical_end=16,
        candidate_start=25,
        candidate_end=37,
        evidence_text="Movement writing records choreography.",
        confidence=0.4,
    )
    bmn = _cand(
        candidate_type="semantic_related_term",
        source_method="semantic_neighbor",
        rule_id="SEMANTIC_RELATED_V1",
        canonical_surface="Benesh Movement Notation",
        candidate_surface="human movement",
        chunk_id="child:b",
        sentence_id="doc:p5:sent:b",
        canonical_start=0,
        canonical_end=24,
        candidate_start=33,
        candidate_end=47,
        evidence_text="Benesh Movement Notation records human movement.",
        confidence=0.4,
    )
    gate = run_alias_gate([mw, bmn])
    by_id = {d.alias_candidate_id: d for d in gate.decisions}
    rows = [
        _evidence(mw, by_id[mw.alias_candidate_id], child_id="child:a",
                  child_text="Movement writing records choreography."),
        _evidence(bmn, by_id[bmn.alias_candidate_id], child_id="child:b",
                  child_text="Benesh Movement Notation records human movement."),
    ]
    batch = cluster_document_entities(rows)
    assert batch.cooccurrence_only_merges == 0
    assert batch.summaries_used_as_identity_evidence == 0
    # No entity may claim both surfaces as accepted identity aliases.
    for ent in batch.entities:
        assert ent.accepted_alias_ids == [] or (
            mw.alias_candidate_id not in ent.accepted_alias_ids
            or bmn.alias_candidate_id not in ent.accepted_alias_ids
        )
        names = {ent.canonical_name.lower()}
        # Must not merge the two concepts into one canonical.
        assert not (
            "movement writing" in ent.canonical_name.lower()
            and "benesh" in " ".join(ent.description_records).lower()
            and mw.alias_candidate_id in ent.accepted_alias_ids
        )


def test_boundary_split_requires_contiguous_parent_offsets():
    parent_text = (
        "Retrieval-Augmented Generation"
        " (RAG) is commonly used in production systems."
    )
    # Split after "Generation"
    split_at = parent_text.index(" (RAG)")
    region = ParentSourceRegion(
        parent_id="parent:boundary",
        document_id="doc:boundary",
        parent_text=parent_text,
        child_spans=(
            ("child:left", 0, split_at),
            ("child:right", split_at, len(parent_text)),
        ),
    )
    # No child-level identity candidates — reconstruction only.
    agg = aggregate_parent_alias_evidence([], parent_regions=[region])
    assert agg.boundary_reconstructions == 1
    assert len(agg.bundles) == 1
    b = agg.bundles[0]
    assert b.decision == "ACCEPT_IDENTITY"
    assert b.reconstruction_method == "contiguous_parent_offset_boundary_v1"
    assert b.candidate_surface == "RAG"
    assert "Retrieval-Augmented Generation" in b.canonical_surface

    # Non-contiguous children must not reconstruct.
    gap_region = ParentSourceRegion(
        parent_id="parent:gap",
        document_id="doc:boundary",
        parent_text=parent_text,
        child_spans=(
            ("child:left", 0, split_at),
            ("child:right", split_at + 10, len(parent_text)),
        ),
    )
    agg_gap = aggregate_parent_alias_evidence([], parent_regions=[gap_region])
    assert agg_gap.boundary_reconstructions == 0


def test_boundary_not_inferred_from_neighbor_strings_alone():
    """Neighbor child texts without parent offsets/proof do not invent aliases."""

    left = _cand(
        document_id="doc:nobound",
        chunk_id="child:left",
        candidate_type="extraction_surface_variant",
        source_method="extraction_surface_variant",
        rule_id="RELEX_SURFACE_VARIANT_V1",
        canonical_surface="Retrieval-Augmented Generation",
        candidate_surface="Retrieval-Augmented Generation",
        canonical_start=0,
        canonical_end=30,
        candidate_start=0,
        candidate_end=30,
        evidence_text="Retrieval-Augmented Generation",
        confidence=0.5,
    )
    right = _cand(
        document_id="doc:nobound",
        chunk_id="child:right",
        candidate_type="extraction_surface_variant",
        source_method="extraction_surface_variant",
        rule_id="RELEX_SURFACE_VARIANT_V1",
        canonical_surface="RAG",
        candidate_surface="RAG",
        canonical_start=1,
        canonical_end=4,
        candidate_start=1,
        candidate_end=4,
        evidence_text="(RAG) is commonly used",
        confidence=0.5,
    )
    gate = run_alias_gate([left, right])
    by_id = {d.alias_candidate_id: d for d in gate.decisions}
    rows = [
        _evidence(left, by_id[left.alias_candidate_id], parent_id="parent:x",
                  child_id="child:left", child_text="Retrieval-Augmented Generation"),
        _evidence(right, by_id[right.alias_candidate_id], parent_id="parent:x",
                  child_id="child:right", child_text="(RAG) is commonly used"),
    ]
    batch = cluster_document_entities(rows)  # no parent_regions
    assert not any(
        b.decision == "ACCEPT_IDENTITY" and b.candidate_surface == "RAG"
        for b in batch.parent_bundles
    )


def test_parent_ambiguity_keeps_conflicting_acronyms_separate():
    ir_info = _cand(
        document_id="doc:amb",
        chunk_id="child:1",
        canonical_surface="Information Retrieval",
        candidate_surface="IR",
        canonical_start=0,
        canonical_end=21,
        candidate_start=23,
        candidate_end=25,
        evidence_text="Information Retrieval (IR)",
    )
    ir_infra = _cand(
        document_id="doc:amb",
        chunk_id="child:2",
        canonical_surface="Infrared",
        candidate_surface="IR",
        canonical_start=0,
        canonical_end=8,
        candidate_start=10,
        candidate_end=12,
        evidence_text="Infrared (IR)",
        sentence_id="doc:amb:sent:1",
    )
    gate = run_alias_gate([ir_info, ir_infra])
    # Gate already REVIEWs document-ambiguous acronyms; clustering must not merge.
    by_id = {d.alias_candidate_id: d for d in gate.decisions}
    assert all(d.decision == "REVIEW" for d in gate.decisions)
    rows = [
        _evidence(ir_info, by_id[ir_info.alias_candidate_id], child_id="child:1"),
        _evidence(ir_infra, by_id[ir_infra.alias_candidate_id], child_id="child:2"),
    ]
    batch = cluster_document_entities(rows)
    assert batch.ambiguous_acronym_merges == 0
    accepted_ents = [e for e in batch.entities if e.accepted_alias_ids]
    assert accepted_ents == []


def test_former_name_preserves_temporal_semantics():
    cand = _cand(
        candidate_type="former_name",
        source_method="former_name_pattern",
        rule_id="FORMER_NAME_V1",
        canonical_surface="Meta Platforms",
        candidate_surface="Facebook",
        chunk_id="child:meta",
        canonical_start=0,
        canonical_end=14,
        candidate_start=34,
        candidate_end=42,
        evidence_text="Meta Platforms, formerly known as Facebook",
    )
    gate = run_alias_gate([cand])
    row = _evidence(cand, gate.decisions[0], child_id="child:meta")
    batch = cluster_document_entities([row])
    ents = [e for e in batch.entities if e.canonical_name == "Meta Platforms"]
    assert len(ents) == 1
    assert cand.alias_candidate_id in ents[0].accepted_alias_ids
    assert any(r.startswith("temporal_former_name:Facebook") for r in ents[0].description_records)


def test_descriptive_and_role_never_identity_merge():
    desc = _cand(
        candidate_type="descriptive_apposition",
        source_method="spacy_appos_descriptive",
        rule_id="APPOS_DESCRIPTIVE_DET_V1",
        canonical_surface="Microsoft",
        candidate_surface="a leading technology company",
        chunk_id="child:ms",
        canonical_start=0,
        canonical_end=9,
        candidate_start=11,
        candidate_end=39,
        evidence_text="Microsoft, a leading technology company",
        scope="sentence",
        confidence=0.9,
    )
    role = _cand(
        candidate_type="role_apposition",
        source_method="spacy_appos_role",
        rule_id="APPOS_ROLE_V1",
        canonical_surface="Satya Nadella",
        candidate_surface="CEO of Microsoft",
        chunk_id="child:sn",
        sentence_id="doc:p5:sent:role",
        canonical_start=0,
        canonical_end=13,
        candidate_start=15,
        candidate_end=31,
        evidence_text="Satya Nadella, CEO of Microsoft",
        scope="sentence",
        confidence=0.9,
    )
    gate = run_alias_gate([desc, role])
    by_id = {d.alias_candidate_id: d for d in gate.decisions}
    batch = cluster_document_entities(
        [
            _evidence(desc, by_id[desc.alias_candidate_id], child_id="child:ms"),
            _evidence(role, by_id[role.alias_candidate_id], child_id="child:sn"),
        ]
    )
    assert batch.description_or_role_merges == 0
    assert batch.retrieval_only_identity_merges == 0
    assert all(not e.accepted_alias_ids for e in batch.entities)


def test_determinism_order_independent_cluster_ids_and_hashes():
    a = _cand(chunk_id="child:1")
    b = _cand(
        candidate_type="former_name",
        source_method="former_name_pattern",
        rule_id="FORMER_NAME_V1",
        canonical_surface="Meta Platforms",
        candidate_surface="Facebook",
        chunk_id="child:2",
        sentence_id="doc:p5:sent:1",
        canonical_start=0,
        canonical_end=14,
        candidate_start=34,
        candidate_end=42,
        evidence_text="Meta Platforms, formerly known as Facebook",
    )
    gate = run_alias_gate([a, b])
    by_id = {d.alias_candidate_id: d for d in gate.decisions}
    rows = [
        _evidence(a, by_id[a.alias_candidate_id], child_id="child:1",
                  child_text="Retrieval-Augmented Generation (RAG)"),
        _evidence(b, by_id[b.alias_candidate_id], child_id="child:2",
                  child_text="Meta Platforms, formerly known as Facebook"),
    ]
    forward = cluster_document_entities(rows)
    reverse = cluster_document_entities(list(reversed(rows)))
    f_ids = [e.document_entity_id for e in forward.entities]
    r_ids = [e.document_entity_id for e in reverse.entities]
    assert f_ids == r_ids
    assert [e.cluster_hash for e in forward.entities] == [
        e.cluster_hash for e in reverse.entities
    ]
    again = cluster_document_entities(rows)
    assert [e.cluster_hash for e in again.entities] == [
        e.cluster_hash for e in forward.entities
    ]


def test_parent_bundle_contract_hash_stable():
    bundle = ParentAliasBundleV1.create(
        parent_id="parent:17",
        document_id="doc:p5",
        canonical_surface="Retrieval-Augmented Generation",
        candidate_surface="RAG",
        candidate_type="acronym_long_form",
        decision="ACCEPT_IDENTITY",
        scope="parent",
        defining_child_ids=["child:17:1"],
        supporting_child_ids=["child:17:1", "child:17:2"],
        evidence_candidate_ids=["aliascand:abc"],
        ambiguity_status="unambiguous_within_parent",
        identity_merge_allowed=True,
    )
    again = ParentAliasBundleV1.create(
        parent_id="parent:17",
        document_id="doc:p5",
        canonical_surface="Retrieval-Augmented Generation",
        candidate_surface="RAG",
        candidate_type="acronym_long_form",
        decision="ACCEPT_IDENTITY",
        scope="parent",
        defining_child_ids=["child:17:1"],
        supporting_child_ids=["child:17:2", "child:17:1"],
        evidence_candidate_ids=["aliascand:abc"],
        ambiguity_status="unambiguous_within_parent",
        identity_merge_allowed=True,
    )
    assert bundle.parent_alias_bundle_id == again.parent_alias_bundle_id
    assert bundle.bundle_hash == again.bundle_hash


def test_convenience_cluster_from_candidates_and_decisions():
    cand = _cand()
    gate = run_alias_gate([cand])
    batch = cluster_from_candidates_and_decisions(
        [cand],
        gate.decisions,
        parent_id_by_chunk={cand.chunk_id: "parent:17"},
        child_text_by_chunk={cand.chunk_id: cand.evidence_text},
    )
    assert batch.entities
    assert batch.entities[0].defining_child_ids == [cand.chunk_id]


def test_acceptance_gates_on_safe_rag_batch():
    defining = _cand(chunk_id="child:1")
    gate = run_alias_gate([defining])
    rows = [
        _evidence(
            defining,
            gate.decisions[0],
            child_id="child:1",
            child_text=defining.evidence_text,
        )
    ]
    batch = cluster_document_entities(rows)
    assert batch.ambiguous_acronym_merges == 0
    assert batch.description_or_role_merges == 0
    assert batch.retrieval_only_identity_merges == 0
    assert batch.cooccurrence_only_merges == 0
    assert batch.summaries_used_as_identity_evidence == 0
