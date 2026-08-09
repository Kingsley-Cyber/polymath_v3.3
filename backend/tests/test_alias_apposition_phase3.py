"""Phase-3 typed apposition classifier + alias_candidates wiring.

Length is never an alias decision. Descriptive/role/location must not enter
identity-alias paths; explicit known-as may; ambiguous proper names are review.
"""

from __future__ import annotations

from services.extraction.appos_enrichment import (
    classify_apposition_phrase,
    extract_apposition_observations,
    spacy_appos_enrichment,
)
from services.ingestion.alias_candidates import collect_alias_candidates
from services.ingestion.enrich import extract_aliases


def test_classifier_never_uses_length_as_alias_rule():
    long_desc = "a " + ("very " * 20) + "large multinational technology company"
    assert len(long_desc) > 60
    cls, rule = classify_apposition_phrase(long_desc)
    assert cls == "descriptive_common_noun"
    assert "LENGTH" not in rule


def test_microsoft_descriptive_apposition_not_alias():
    text = "Microsoft, a leading technology company, announced Azure."
    entities = [{"canonical_name": "Microsoft", "surface_form": "Microsoft"}]
    obs = extract_apposition_observations(text, entities)
    assert any(o.class_name == "descriptive_common_noun" for o in obs)
    assert not any(o.class_name == "explicit_name_alias" for o in obs)
    aliases, defs = spacy_appos_enrichment(text, entities)
    assert aliases == {}
    assert "microsoft" in defs

    batch = collect_alias_candidates(
        text,
        entities,
        document_id="doc:ms",
        chunk_id="chunk:ms",
        include_curated=False,
        include_relex_surfaces=False,
    )
    desc = [c for c in batch.candidates if c.candidate_type == "descriptive_apposition"]
    assert len(desc) == 1
    assert "leading technology company" in desc[0].candidate_surface
    assert not any(c.candidate_type == "proper_name_apposition" for c in batch.candidates)


def test_qdrant_descriptive_not_alias_and_no_verb_false_positive():
    text = "Qdrant, an open-source vector database, stores embeddings."
    entities = [{"canonical_name": "Qdrant", "surface_form": "Qdrant"}]
    obs = extract_apposition_observations(text, entities)
    phrases = {o.appos_text.lower() for o in obs}
    assert any("vector database" in p for p in phrases)
    assert not any("stores embeddings" in p for p in phrases)
    aliases, _ = spacy_appos_enrichment(text, entities)
    assert aliases == {}


def test_known_as_proper_name_is_explicit_alias_candidate():
    text = "Abel Tesfaye, also known as the Weeknd, performed."
    entities = [{"canonical_name": "Abel Tesfaye", "surface_form": "Abel Tesfaye"}]
    obs = extract_apposition_observations(text, entities)
    hits = [o for o in obs if o.class_name == "explicit_name_alias"]
    assert len(hits) == 1
    assert "weeknd" in hits[0].appos_text.lower()
    assert hits[0].entity_start < hits[0].entity_end
    assert hits[0].appos_start < hits[0].appos_end

    aliases, _ = spacy_appos_enrichment(text, entities)
    assert any("weeknd" in a.lower() for a in aliases.get("abel tesfaye", []))

    batch = collect_alias_candidates(
        text,
        entities,
        document_id="doc:wk",
        chunk_id="chunk:wk",
        include_curated=False,
        include_relex_surfaces=False,
    )
    proper = [
        c
        for c in batch.candidates
        if c.candidate_type == "proper_name_apposition"
        and "weeknd" in c.candidate_surface.lower()
    ]
    assert len(proper) == 1
    assert proper[0].rule_id.startswith("APPOS_KNOWN_AS")
    assert proper[0].confidence >= 0.9


def test_role_apposition_not_identity_alias():
    text = "Satya Nadella, CEO of Microsoft, spoke today."
    entities = [{"canonical_name": "Satya Nadella", "surface_form": "Satya Nadella"}]
    obs = extract_apposition_observations(text, entities)
    assert any(o.class_name == "role" for o in obs)
    aliases, _ = spacy_appos_enrichment(text, entities)
    assert aliases == {}

    batch = collect_alias_candidates(
        text,
        entities,
        document_id="doc:role",
        chunk_id="chunk:role",
        include_curated=False,
        include_relex_surfaces=False,
    )
    assert any(c.candidate_type == "role_apposition" for c in batch.candidates)
    assert not any(
        c.candidate_type == "proper_name_apposition" for c in batch.candidates
    )


def test_location_apposition_not_identity_alias():
    text = "Paris, capital of France, is beautiful."
    entities = [{"canonical_name": "Paris", "surface_form": "Paris"}]
    obs = extract_apposition_observations(text, entities)
    assert any(o.class_name == "location" for o in obs)
    aliases, _ = spacy_appos_enrichment(text, entities)
    assert aliases == {}

    batch = collect_alias_candidates(
        text,
        entities,
        document_id="doc:loc",
        chunk_id="chunk:loc",
        include_curated=False,
        include_relex_surfaces=False,
    )
    assert any(c.candidate_type == "location_apposition" for c in batch.candidates)


def test_ambiguous_proper_name_apposition_is_review_path():
    text = "Facebook, Meta Platforms, reported earnings."
    entities = [{"canonical_name": "Facebook", "surface_form": "Facebook"}]
    obs = extract_apposition_observations(text, entities)
    amb = [o for o in obs if o.class_name == "ambiguous_name_like"]
    assert len(amb) == 1
    assert "meta" in amb[0].appos_text.lower()
    # Legacy adapter: ambiguous does NOT enter aliases dict (gate/review only).
    aliases, _ = spacy_appos_enrichment(text, entities)
    assert aliases == {}

    batch = collect_alias_candidates(
        text,
        entities,
        document_id="doc:amb",
        chunk_id="chunk:amb",
        include_curated=False,
        include_relex_surfaces=False,
    )
    proper = [c for c in batch.candidates if c.candidate_type == "proper_name_apposition"]
    assert len(proper) == 1
    assert proper[0].confidence < 0.7
    assert "REVIEW" in proper[0].rule_id or proper[0].source_method.endswith("ambiguous")


def test_legacy_query_aliases_unchanged_when_appositions_enabled():
    text = "Retrieval-Augmented Generation (RAG) is useful."
    entities = [
        {
            "canonical_name": "Retrieval-Augmented Generation",
            "surface_form": "Retrieval-Augmented Generation",
        }
    ]
    batch = collect_alias_candidates(
        text, entities, document_id="doc:leg", chunk_id="chunk:leg"
    )
    assert batch.legacy_query_aliases == extract_aliases(text, entities)


def test_shared_doc_reuse_deterministic():
    from services.extraction.appos_enrichment import get_shared_nlp

    text = "Microsoft, a leading technology company, announced Azure."
    entities = [{"canonical_name": "Microsoft", "surface_form": "Microsoft"}]
    nlp = get_shared_nlp()
    doc = nlp(text)
    a = collect_alias_candidates(
        text,
        entities,
        document_id="doc:share",
        chunk_id="chunk:share",
        include_curated=False,
        include_relex_surfaces=False,
        spacy_doc=doc,
    )
    b = collect_alias_candidates(
        text,
        entities,
        document_id="doc:share",
        chunk_id="chunk:share",
        include_curated=False,
        include_relex_surfaces=False,
        spacy_doc=doc,
    )
    assert [c.alias_candidate_id for c in a.candidates] == [
        c.alias_candidate_id for c in b.candidates
    ]


def test_include_appositions_false_skips_producer():
    text = "Microsoft, a leading technology company, announced Azure."
    entities = [{"canonical_name": "Microsoft", "surface_form": "Microsoft"}]
    batch = collect_alias_candidates(
        text,
        entities,
        document_id="doc:off",
        chunk_id="chunk:off",
        include_curated=False,
        include_relex_surfaces=False,
        include_appositions=False,
    )
    assert not any("apposition" in c.candidate_type for c in batch.candidates)
