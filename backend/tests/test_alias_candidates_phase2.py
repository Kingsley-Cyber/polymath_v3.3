"""Phase-2 producer wrappers → AliasCandidateV1 (legacy query_aliases preserved)."""

from __future__ import annotations

from services.ingestion.alias_candidates import collect_alias_candidates
from services.ingestion.enrich import extract_aliases, schwartz_hearst_matches


def test_schwartz_hearst_producer_emits_acronym_candidate_with_offsets():
    text = "Retrieval-Augmented Generation (RAG) combines retrieval with generation."
    entities = [
        {
            "canonical_name": "Retrieval-Augmented Generation",
            "surface_form": "Retrieval-Augmented Generation",
            "entity_type": "concept",
        }
    ]
    batch = collect_alias_candidates(
        text, entities, document_id="doc:a1", chunk_id="chunk:a1"
    )
    sh = [
        c
        for c in batch.candidates
        if c.candidate_type == "acronym_long_form" and c.candidate_surface == "RAG"
    ]
    assert len(sh) == 1
    cand = sh[0]
    assert cand.rule_id == "SCHWARTZ_HEARST_V1"
    assert cand.source_method == "schwartz_hearst_acronym"
    assert cand.evidence_text
    assert "RAG" in text[cand.candidate_start : cand.candidate_end]
    assert cand.scope == "document"
    assert cand.extractor_release
    assert cand.alias_candidate_id.startswith("aliascand:")


def test_legacy_query_aliases_preserved_bit_compatible():
    text = "Retrieval-Augmented Generation (RAG) is useful."
    entities = [
        {
            "canonical_name": "Retrieval-Augmented Generation",
            "surface_form": "Retrieval-Augmented Generation",
        }
    ]
    batch = collect_alias_candidates(
        text, entities, document_id="doc:a1", chunk_id="chunk:a1"
    )
    assert batch.legacy_query_aliases == extract_aliases(text, entities)
    assert "RAG" in (
        batch.legacy_query_aliases.get("Retrieval-Augmented Generation") or []
    )


def test_explicit_alias_pattern_producer():
    text = "International Business Machines, also known as IBM, developed the system."
    entities = [
        {
            "canonical_name": "International Business Machines",
            "surface_form": "International Business Machines",
        }
    ]
    batch = collect_alias_candidates(
        text, entities, document_id="doc:a2", chunk_id="chunk:a2"
    )
    hits = [
        c
        for c in batch.candidates
        if c.candidate_type == "explicit_alias_pattern" and c.candidate_surface == "IBM"
    ]
    assert len(hits) == 1
    assert hits[0].rule_id == "EXPLICIT_ALIAS_PATTERN_V1"
    assert hits[0].confidence == 1.0


def test_explicit_abbreviation_producer():
    text = "Benesh Movement Notation, abbreviated BMN, records movement."
    entities = [
        {
            "canonical_name": "Benesh Movement Notation",
            "surface_form": "Benesh Movement Notation",
        }
    ]
    batch = collect_alias_candidates(
        text, entities, document_id="doc:a3", chunk_id="chunk:a3"
    )
    hits = [c for c in batch.candidates if c.candidate_type == "explicit_abbreviation"]
    assert len(hits) == 1
    assert hits[0].candidate_surface == "BMN"
    assert hits[0].rule_id == "EXPLICIT_ABBREVIATION_V1"


def test_former_name_producer():
    text = "Beta Systems, formerly known as Alpha Systems, shipped the product."
    entities = [{"canonical_name": "Beta Systems", "surface_form": "Beta Systems"}]
    batch = collect_alias_candidates(
        text, entities, document_id="doc:a4", chunk_id="chunk:a4"
    )
    hits = [c for c in batch.candidates if c.candidate_type == "former_name"]
    assert len(hits) == 1
    assert hits[0].candidate_surface == "Alpha Systems"
    assert hits[0].canonical_surface == "Beta Systems"


def test_relex_surface_variant_defaults_to_typed_candidate_or_incomplete():
    text = "Qdrant stores vectors. The qdrant vector db is fast."
    entities = [
        {
            "canonical_name": "qdrant",
            "surface_form": "Qdrant",
            "query_aliases": ["qdrant vector db"],
            "entity_type": "org",
            "confidence": 0.8,
        }
    ]
    batch = collect_alias_candidates(
        text, entities, document_id="doc:a9", chunk_id="chunk:a9"
    )
    variants = [
        c
        for c in batch.candidates
        if c.candidate_type == "extraction_surface_variant"
        and c.candidate_surface.lower() == "qdrant vector db"
    ]
    incomplete = [
        i
        for i in batch.incomplete
        if i.candidate_type == "extraction_surface_variant"
        and "qdrant vector db" in i.candidate_surface.lower()
    ]
    assert variants or incomplete
    if variants:
        assert variants[0].rule_id == "RELEX_SURFACE_VARIANT_V1"
        assert variants[0].source_method == "extraction_surface_variant"


def test_casing_variant_not_in_source_is_incomplete():
    text = "Open AI stores the embeddings."
    entities = [{"canonical_name": "Open AI", "surface_form": "Open AI"}]
    batch = collect_alias_candidates(
        text, entities, document_id="doc:a8", chunk_id="chunk:a8"
    )
    # underscore/hyphen punctuation variants are not attested in source.
    casing_incomplete = [
        i
        for i in batch.incomplete
        if i.candidate_type == "casing_variant"
        and i.incomplete_reason == "casing_variant_not_in_source"
    ]
    assert casing_incomplete
    assert any("open_ai" in i.candidate_surface or "open-ai" in i.candidate_surface for i in casing_incomplete)


def test_missing_offsets_fail_closed_as_incomplete():
    text = "Something unrelated about databases."
    entities = [
        {
            "canonical_name": "Qdrant",
            "surface_form": "Qdrant",
            "query_aliases": ["qdrant vector db"],
        }
    ]
    batch = collect_alias_candidates(
        text, entities, document_id="doc:miss", chunk_id="chunk:miss"
    )
    assert not any(
        c.candidate_type == "extraction_surface_variant" for c in batch.candidates
    )
    assert any(
        i.incomplete_reason == "missing_source_offsets"
        for i in batch.incomplete
        if i.candidate_type == "extraction_surface_variant"
    )


def test_deterministic_replay_identical_candidate_ids():
    text = (
        "Retrieval-Augmented Generation (RAG) combines retrieval. "
        "International Business Machines, also known as IBM, built systems."
    )
    entities = [
        {
            "canonical_name": "Retrieval-Augmented Generation",
            "surface_form": "Retrieval-Augmented Generation",
        },
        {
            "canonical_name": "International Business Machines",
            "surface_form": "International Business Machines",
        },
    ]
    a = collect_alias_candidates(
        text, entities, document_id="doc:replay", chunk_id="chunk:replay"
    )
    b = collect_alias_candidates(
        text, entities, document_id="doc:replay", chunk_id="chunk:replay"
    )
    assert [c.alias_candidate_id for c in a.candidates] == [
        c.alias_candidate_id for c in b.candidates
    ]
    assert [c.contract_hash for c in a.candidates] == [
        c.contract_hash for c in b.candidates
    ]
    assert a.legacy_query_aliases == b.legacy_query_aliases
    assert [
        (i.incomplete_reason, i.candidate_surface, i.rule_id) for i in a.incomplete
    ] == [
        (i.incomplete_reason, i.candidate_surface, i.rule_id) for i in b.incomplete
    ]


def test_schwartz_hearst_matches_expose_spans():
    text = "Facial Action Coding System (FACS) annotates faces."
    matches = schwartz_hearst_matches(text)
    assert matches
    m = matches[0]
    assert text[m["short_start"] : m["short_end"]] == "FACS"
    assert "Facial Action Coding System" in text[m["long_start"] : m["long_end"]]


def test_curated_alias_emits_when_dual_spans_present(monkeypatch):
    # Force a curated mapping for the fixture surfaces.
    from services.extraction.canonical import normalize_entity_name
    from services.ingestion import alias_candidates as ac

    lookup = {"open ai": "openai", "openai": "openai"}

    monkeypatch.setattr(
        ac,
        "resolve_entity_alias",
        lambda normalized: lookup.get(normalized, normalized),
    )
    monkeypatch.setattr(
        ac,
        "canonicalize_entity_name",
        lambda name: lookup.get(normalize_entity_name(name), normalize_entity_name(name)),
    )

    text = "OpenAI and Open AI are discussed in this paragraph about openai tooling."
    entities = [{"canonical_name": "Open AI", "surface_form": "Open AI"}]
    batch = ac.collect_alias_candidates(
        text, entities, document_id="doc:cur", chunk_id="chunk:cur"
    )
    curated = [c for c in batch.candidates if c.candidate_type == "curated_alias"]
    incomplete = [
        i for i in batch.incomplete if i.candidate_type == "curated_alias"
    ]
    # Either complete (dual spans) or incomplete — never silent drop.
    assert curated or incomplete
    if curated:
        assert curated[0].rule_id == "CURATED_EXACT_ALIAS_V1"
        assert curated[0].source_method == "curated_exact_alias"
