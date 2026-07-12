from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from models.schemas import RetrievalTier
from services.retriever.query_plan import build_query_plan_v2
from services.retriever.vocabulary import (
    CorpusVocabularyResolver,
    _definition_reference_terms,
    _grounded_association_targets,
    _match_overlap,
    grounded_document_route_hints,
    grounded_translation_lane_targets,
    grounded_vocabulary_lanes,
    hierarchy_bound_vocabulary_matches,
    query_exact_terms,
)


def _facs_match(score: float = 0.5):
    return {
        "lexicon_id": "lex-facs",
        "term": "Facial Action Coding System",
        "canonical_key": "facial action coding system",
        "aliases": ["FACS"],
        "aliases_normalized": ["facs"],
        "abbreviations": ["FACS"],
        "retrieval_gloss": (
            "Facial Action Coding System. Source definition: a system for "
            "coding facial expressions and actor facial movement with Action Units."
        ),
        "definitions": [{"text": "A system for coding facial actions."}],
        "support_count": 24,
        "source_document_ids": ["doc-facs"],
        "source_document_support": [{"doc_id": "doc-facs", "support_count": 24}],
        "source_chunk_ids": ["chunk-facs"],
        "source_parent_ids": ["parent-facs"],
        "application_contexts": [
            {"predicate": "used_for", "target": "facial performance"}
        ],
        "components": [{"target": "Action Unit 12"}],
        "entity_ids": ["entity:facial-action-coding-system"],
        "match_type": "gloss_vector",
        "score": score,
        "dense_rank": 1,
        "_vector": [0.1, 0.2],
    }


def test_translation_lane_targets_originating_required_probe_only():
    resolution = {
        "matches": [
            {
                "lexicon_id": "lex-sticky",
                "matched_lane_ids": ["probe_sticky_message", "original"],
            },
            {
                "lexicon_id": "lex-background",
                "matched_lane_ids": ["probe_unrequired_background"],
            },
        ]
    }
    expansion = {
        "lane_lexicon_ids": {
            "translation_sticky": ["lex-sticky"],
            "planner_translation_0_sticky": ["lex-sticky"],
            "stepback_sticky": ["lex-sticky"],
            "translation_background": ["lex-background"],
        }
    }

    assert grounded_translation_lane_targets(
        resolution,
        expansion,
        required_lane_ids=["sticky_message"],
    ) == {
        "translation_sticky": ["sticky_message"],
        "planner_translation_0_sticky": ["sticky_message"],
    }


def test_exact_term_generation_preserves_short_uppercase_acronyms():
    terms = query_exact_terms("How does FACS describe facial movement?")
    assert "facs" in terms
    assert "facial movement" in terms


def test_exact_term_generation_preserves_ordered_stopword_phrase():
    terms = query_exact_terms(
        "sticky message sticky idea made to stick success principles"
    )
    assert "made to stick" in terms
    assert "sticky message" in terms


def test_exact_term_generation_recovers_possessive_concept_unigram():
    terms = query_exact_terms("actor's")

    assert "actor" in terms


def test_overlap_bridge_recognizes_regular_morphology_without_identity_merge():
    overlap, _ratio = _match_overlap("face movement", _facs_match())

    assert overlap == 2


@pytest.mark.asyncio
async def test_plain_language_face_query_discovers_grounded_corpus_term(monkeypatch):
    async def fake_search(*args, **kwargs):
        if kwargs.get("with_vectors"):
            return [_facs_match()]
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )

    class FakeQdrant:
        async def scroll(self, **_kwargs):
            return (
                [
                    SimpleNamespace(
                        payload={
                            "corpus_id": "c1",
                            "doc_id": "doc-facs",
                            "title": "Actor performance",
                            "summary": "Facial performance and action units.",
                            "concepts": ["facial performance"],
                            "section_ids": ["section-1"],
                        }
                    )
                ],
                None,
            )

    resolver = CorpusVocabularyResolver()
    resolution = await resolver.resolve(
        query="How should an actor's face move in the opening ad?",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1, 0.2],
        qdrant_client=FakeQdrant(),
    )

    assert [row["canonical_key"] for row in resolution["matches"]] == [
        "facial action coding system"
    ]
    assert resolution["matches"][0]["applicability"] == "source_term_overlap"
    assert resolution["matches"][0]["required"] is False
    assert resolution["matches"][0]["dense_rank_by_lane"] == {"original": 1}
    assert resolution["document_profiles"][0]["doc_id"] == "doc-facs"
    assert resolution["raptor_ancestors"][0]["ancestor_level"] == "document_root"
    lanes, diagnostics = grounded_vocabulary_lanes(
        build_query_plan_v2("How should an actor's face move in the opening ad?"),
        resolution,
    )
    assert any(lane.lane_id.startswith("translation_") for lane in lanes)
    assert all(lane.required is False for lane in lanes)
    assert diagnostics["introduced_lexicon_ids"] == ["lex-facs"]
    route_hints = grounded_document_route_hints(resolution, diagnostics)
    translation_lane = next(
        lane.lane_id for lane in lanes if lane.lane_id.startswith("translation_")
    )
    assert route_hints[translation_lane][0]["doc_id"] == "doc-facs"
    assert route_hints[translation_lane][0]["route_source"] == (
        "corpus_lexicon_provenance"
    )


@pytest.mark.asyncio
async def test_source_relation_promotes_canonical_association_without_query_guessing(
    monkeypatch,
):
    facial_expression = {
        "lexicon_id": "lex-expression",
        "term": "facial expressions",
        "canonical_key": "facial expressions",
        "retrieval_gloss": "Visible movements of an actor's face.",
        "support_count": 8,
        "source_document_ids": ["doc-performance"],
        "factual_relations": [
            {
                "predicate": "related_to",
                "direction": "incoming",
                "target_lexicon_key": "facial action coding system",
                "target": "Facial Action Coding System",
                "confidence": 0.9,
                "chunk_id": "chunk-expression-facs",
                "evidence_phrase": "FACS-style facial expressions",
            }
        ],
        "match_type": "exact_alias",
        "score": 1.0,
        "_vector": [0.1, 0.2],
    }

    async def fake_search(*args, **kwargs):
        if kwargs.get("query_vec") is not None:
            return [facial_expression]
        if "facial action coding system" in (kwargs.get("exact_terms") or []):
            return [_facs_match(score=1.0)]
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    resolution = await CorpusVocabularyResolver().resolve(
        query="How should an actor's facial expressions move?",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1, 0.2],
        qdrant_client=object(),
    )

    facs = next(
        row
        for row in resolution["matches"]
        if row["canonical_key"] == "facial action coding system"
    )
    assert facs["applicability"] == "corpus_association"
    assert facs["required"] is False
    assert facs["association_evidence"]["seed_lexicon_id"] == "lex-expression"
    assert facs["association_evidence"]["chunk_id"] == "chunk-expression-facs"
    assert resolution["association_expansion_count"] == 1
    lanes, diagnostics = grounded_vocabulary_lanes(
        build_query_plan_v2("How should an actor's facial expressions move?"),
        resolution,
    )
    assert [lane.phrase for lane in lanes] == ["Facial Action Coding System"]
    assert diagnostics["introduced_lexicon_ids"] == ["lex-facs"]


@pytest.mark.asyncio
async def test_generic_exact_seed_cannot_expand_unrelated_association(monkeypatch):
    actor = {
        "lexicon_id": "lex-actor",
        "term": "actor",
        "canonical_key": "actor",
        "retrieval_gloss": "A performer in a film.",
        "support_count": 20,
        "source_document_ids": ["doc-actor"],
        "factual_relations": [
            {
                "predicate": "related_to",
                "target_lexicon_key": "hypnosis scene",
                "target": "hypnosis scene",
                "confidence": 0.98,
                "chunk_id": "chunk-actor-scene",
            }
        ],
        "match_type": "exact_alias",
        "score": 1.0,
        "_vector": [0.1],
    }
    hypnosis = {
        "lexicon_id": "lex-hypnosis",
        "term": "hypnosis scene",
        "canonical_key": "hypnosis scene",
        "retrieval_gloss": "A spoon and teacup hypnosis sequence in Get Out.",
        "support_count": 1,
        "retrieval_eligible": True,
        "match_type": "exact_alias",
        "score": 1.0,
    }

    async def fake_search(*args, **kwargs):
        if kwargs.get("query_vec") is not None:
            return [actor]
        if "hypnosis scene" in (kwargs.get("exact_terms") or []):
            return [hypnosis]
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )

    resolution = await CorpusVocabularyResolver().resolve(
        query="actor",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1],
        qdrant_client=object(),
    )

    assert [row["lexicon_id"] for row in resolution["matches"]] == ["lex-actor"]
    assert any(
        row["reason"] == "association_not_query_grounded"
        and row["lexicon_id"] == "lex-hypnosis"
        for row in resolution["rejected_expansions"]
    )


def test_generic_association_fanout_cannot_crowd_out_specific_bridge():
    generic_face = {
        "lexicon_id": "lex-face",
        "term": "face",
        "canonical_key": "face",
        "applicability": "direct",
        "score": 1.0,
        "overlap_count": 2,
        "components": [
            {
                "target_lexicon_key": f"face component {index}",
                "confidence": 0.98,
            }
            for index in range(12)
        ],
    }
    specific_visibility_code = {
        "lexicon_id": "lex-visibility-73",
        "term": "visibility code 73",
        "canonical_key": "visibility code 73",
        "applicability": "source_term_overlap",
        "score": 0.77,
        "overlap_count": 2,
        "component_of": [
            {
                "target_lexicon_key": "facial action coding system",
                "confidence": 0.9,
            }
        ],
    }

    targets = _grounded_association_targets(
        [generic_face, specific_visibility_code], limit=3
    )

    assert targets[0]["target_key"] == "facial action coding system"
    assert targets[0]["fanout_damping"] == 1.0
    generic_targets = [
        row for row in targets if row["seed_lexicon_id"] == "lex-face"
    ]
    assert generic_targets
    assert all(row["fanout_damping"] < 0.3 for row in generic_targets)


@pytest.mark.asyncio
async def test_definition_reference_promotes_named_canonical_concept(monkeypatch):
    lower_face_units = {
        "lexicon_id": "lex-lower-face",
        "term": "lower face action units",
        "canonical_key": "lower face action units",
        "retrieval_gloss": "Lower face movements used to direct an actor's face.",
        "definitions": [
            {
                "text": (
                    "Units describing lower face movement in the Facial Action "
                    "Coding System"
                ),
                "chunk_id": "chunk-definition",
                "parent_id": "parent-definition",
            }
        ],
        "support_count": 12,
        "source_document_ids": ["doc-facs"],
        "match_type": "gloss_vector",
        "score": 0.72,
        "_vector": [0.1, 0.2],
    }

    async def fake_search(*args, **kwargs):
        if kwargs.get("query_vec") is not None:
            return [lower_face_units]
        if "facial action coding system" in (kwargs.get("exact_terms") or []):
            return [_facs_match(score=1.0)]
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )

    resolution = await CorpusVocabularyResolver().resolve(
        query="How should an actor's face move?",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1, 0.2],
        qdrant_client=object(),
    )

    facs = next(row for row in resolution["matches"] if row["lexicon_id"] == "lex-facs")
    assert facs["applicability"] == "corpus_definition_reference"
    assert facs["definition_reference_evidence"]["seed_lexicon_id"] == (
        "lex-lower-face"
    )
    assert resolution["definition_reference_expansion_count"] == 1


def test_definition_reference_terms_preserve_source_cited_acronym():
    terms, evidence = _definition_reference_terms(
        [
            {
                "lexicon_id": "lex-motion",
                "term": "Kinetic Motion Direction Prompting",
                "score": 0.91,
                "contextual_usages": [
                    {
                        "text": (
                            "Kinetic prompting complements FACS for facial "
                            "performance and gaze control."
                        ),
                        "chunk_id": "chunk-motion",
                        "parent_id": "parent-motion",
                    }
                ],
            }
        ]
    )

    assert "facs" in terms
    assert evidence["facs"]["seed_lexicon_id"] == "lex-motion"
    assert evidence["facs"]["source_field"] == "contextual_usage"


def test_translation_lane_cap_is_fair_across_selected_corpora():
    matches = []
    for corpus_id in ("c1", "c2"):
        for index in range(4):
            matches.append(
                {
                    "corpus_id": corpus_id,
                    "lexicon_id": f"{corpus_id}-lex-{index}",
                    "term": f"Grounded Concept {corpus_id} {index}",
                    "score": 0.9 - index * 0.01,
                    "applicability": "hierarchy_bound",
                }
            )

    _lanes, diagnostics = grounded_vocabulary_lanes(
        build_query_plan_v2("How should I design the campaign?"),
        {"matches": matches},
        max_translation_lanes=4,
        max_translation_lanes_per_corpus=2,
    )

    introduced = diagnostics["introduced_lexicon_ids"]
    assert len([value for value in introduced if value.startswith("c1-")]) == 2
    assert len([value for value in introduced if value.startswith("c2-")]) == 2


def test_translation_cap_ignores_query_duplicate_terms_before_selection():
    plan = build_query_plan_v2("How should an actor's face move in the opening ad?")
    matches = [
        {
            "corpus_id": "c1",
            "lexicon_id": f"lex-{term}",
            "term": term,
            "score": 1.0,
            "applicability": "direct",
        }
        for term in ("actor", "face", "opening")
    ]
    matches.append(
        {**_facs_match(), "corpus_id": "c1", "applicability": "hierarchy_bound"}
    )

    lanes, diagnostics = grounded_vocabulary_lanes(
        plan,
        {"matches": matches},
        max_translation_lanes=3,
        max_translation_lanes_per_corpus=3,
    )

    assert diagnostics["introduced_lexicon_ids"] == ["lex-facs"]
    assert "Facial Action Coding System" in lanes[0].dense_text


def test_simple_translation_does_not_add_step_back_or_execute_weak_overlap():
    plan = build_query_plan_v2("How should an actor's face move in the opening ad?")
    matches = [
        {
            "corpus_id": "c1",
            "lexicon_id": "lex-head-movement",
            "term": "head movements",
            "score": 0.91,
            "applicability": "source_term_overlap",
        },
        {
            **_facs_match(),
            "corpus_id": "c1",
            "applicability": "hierarchy_bound",
            "application_contexts": [{"target": "actor performance"}],
        },
    ]

    lanes, diagnostics = grounded_vocabulary_lanes(plan, {"matches": matches})

    assert [lane.lane_id for lane in lanes] == ["translation_lex-facs"]
    assert diagnostics["step_back_lane_ids"] == []
    assert diagnostics["skipped_non_executable_lexicon_ids"] == ["lex-head-movement"]


@pytest.mark.asyncio
async def test_hierarchy_binding_resolves_source_proven_expert_card(monkeypatch):
    async def fake_search(*args, **kwargs):
        assert kwargs["allowed_lexicon_ids"] == ["lex-facs", "lex-au12"]
        return [
            _facs_match(score=0.45),
            {
                "lexicon_id": "lex-au12",
                "term": "Action Unit 12",
                "retrieval_gloss": "Lip corner puller movement.",
                "support_count": 2,
                "score": 0.5,
            },
        ]

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    matches, diagnostics = await hierarchy_bound_vocabulary_matches(
        qdrant_client=object(),
        summary_tree_routes={
            "primary": [
                SimpleNamespace(
                    corpus_id="c1",
                    doc_id="doc-facs",
                    lexicon_ids=("lex-facs", "lex-au12"),
                )
            ]
        },
        lane_vectors={"primary": [0.1, 0.2]},
        lane_queries={"primary": "How should an actor's face move in the opening ad?"},
    )

    assert [row["lexicon_id"] for row in matches] == ["lex-facs"]
    assert matches[0]["applicability"] == "hierarchy_bound"
    assert matches[0]["hierarchy_document_ids"] == ["doc-facs"]
    assert diagnostics["status"] == "resolved"


@pytest.mark.asyncio
async def test_hierarchy_binding_ranks_evidence_before_surface_overlap(monkeypatch):
    async def fake_search(*args, **kwargs):
        return [
            {
                "lexicon_id": "lex-au19",
                "term": "face movement AU19",
                "retrieval_gloss": "A narrow lower-face action.",
                "support_count": 2,
                "score": 0.6,
            },
            {
                **_facs_match(score=0.45),
                "support_count": 927,
            },
        ]

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    matches, _diagnostics = await hierarchy_bound_vocabulary_matches(
        qdrant_client=object(),
        summary_tree_routes={
            "primary": [
                SimpleNamespace(
                    corpus_id="c1",
                    doc_id="doc-facs",
                    lexicon_ids=("lex-au19", "lex-facs"),
                )
            ]
        },
        lane_vectors={"primary": [0.1, 0.2]},
        lane_queries={"primary": "How should an actor's face move?"},
    )

    assert [row["lexicon_id"] for row in matches] == ["lex-facs"]


@pytest.mark.asyncio
async def test_hierarchy_binding_prefers_canonical_document_title_identity(monkeypatch):
    async def fake_search(*args, **kwargs):
        return [
            {
                "lexicon_id": "lex-au19",
                "term": "Action Unit 19",
                "retrieval_gloss": "A strongly matching lower-face movement.",
                "support_count": 200,
                "score": 0.82,
            },
            _facs_match(score=0.45),
        ]

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    matches, _diagnostics = await hierarchy_bound_vocabulary_matches(
        qdrant_client=object(),
        summary_tree_routes={
            "primary": [
                SimpleNamespace(
                    corpus_id="c1",
                    doc_id="doc-facs",
                    document_title="Facial Action Coding System manual",
                    lexicon_ids=("lex-au19", "lex-facs"),
                )
            ]
        },
        lane_vectors={"primary": [0.1, 0.2]},
        lane_queries={"primary": "How should an actor's face move?"},
    )

    assert [row["lexicon_id"] for row in matches] == ["lex-facs"]
    assert matches[0]["title_identity_match"] is True


@pytest.mark.asyncio
async def test_hierarchy_binding_recovers_canonical_title_outside_ann_top_k(
    monkeypatch,
):
    async def fake_search(*args, **kwargs):
        return []

    async def fake_retrieve(*args, **kwargs):
        payload = _facs_match(score=0.0)
        payload.pop("score", None)
        return {"lex-facs": {"payload": payload, "vector": None}}

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    monkeypatch.setattr(
        "services.storage.qdrant_writer.retrieve_lexicon_entries", fake_retrieve
    )

    matches, _diagnostics = await hierarchy_bound_vocabulary_matches(
        qdrant_client=object(),
        summary_tree_routes={
            "performance": [
                SimpleNamespace(
                    corpus_id="c1",
                    doc_id="doc-facs",
                    document_title="Facial Action Coding System manual",
                    lexicon_ids=("lex-facs",),
                )
            ]
        },
        lane_vectors={"performance": [0.1, 0.2]},
        lane_queries={"performance": "direct actor performance"},
    )

    assert [row["lexicon_id"] for row in matches] == ["lex-facs"]
    assert matches[0]["title_identity_match"] is True
    assert matches[0]["selection_reason"] == "preindexed_hierarchy_binding"


@pytest.mark.asyncio
async def test_hierarchy_binding_reserves_one_concept_per_lane_and_corpus(monkeypatch):
    async def fake_search(*args, **kwargs):
        return [
            {
                "lexicon_id": lexicon_id,
                "term": f"Concept {lexicon_id}",
                "retrieval_gloss": "A source-backed expert technique.",
                "support_count": 100,
                "score": 0.80,
            }
            for lexicon_id in kwargs["allowed_lexicon_ids"]
        ]

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    routes = {
        "performance": [
            SimpleNamespace(
                corpus_id="c1", doc_id="p1", lexicon_ids=("performance-c1",)
            ),
            SimpleNamespace(
                corpus_id="c2", doc_id="p2", lexicon_ids=("performance-c2",)
            ),
        ],
        "timing": [
            SimpleNamespace(
                corpus_id="c1", doc_id="t1", lexicon_ids=("timing-c1",)
            ),
            SimpleNamespace(
                corpus_id="c2", doc_id="t2", lexicon_ids=("timing-c2",)
            ),
        ],
    }

    matches, diagnostics = await hierarchy_bound_vocabulary_matches(
        qdrant_client=object(),
        summary_tree_routes=routes,
        lane_vectors={"performance": [0.1], "timing": [0.2]},
        lane_queries={
            "performance": "direct actor performance",
            "timing": "control motion timing",
        },
        max_matches=4,
    )

    assert [row["lexicon_id"] for row in matches] == [
        "performance-c1",
        "performance-c2",
        "timing-c1",
        "timing-c2",
    ]
    assert diagnostics["selected_groups"] == [
        {"lane_id": "performance", "corpus_id": "c1"},
        {"lane_id": "performance", "corpus_id": "c2"},
        {"lane_id": "timing", "corpus_id": "c1"},
        {"lane_id": "timing", "corpus_id": "c2"},
    ]


@pytest.mark.asyncio
async def test_hierarchy_binding_fairly_samples_concepts_from_later_documents(
    monkeypatch,
):
    target_id = "later-document-concept"

    async def fake_search(*args, **kwargs):
        assert target_id in kwargs["allowed_lexicon_ids"]
        assert len(kwargs["allowed_lexicon_ids"]) == 512
        return [
            {
                "lexicon_id": target_id,
                "term": "Later document concept",
                "retrieval_gloss": "A source-backed expert technique.",
                "support_count": 100,
                "score": 0.80,
            }
        ]

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    routes = {
        "performance": [
            SimpleNamespace(
                corpus_id="c1",
                doc_id=f"doc-{doc_index}",
                lexicon_ids=tuple(
                    [target_id, *[f"d{doc_index}-c{i}" for i in range(95)]]
                    if doc_index == 6
                    else [f"d{doc_index}-c{i}" for i in range(96)]
                ),
            )
            for doc_index in range(7)
        ]
    }

    matches, _diagnostics = await hierarchy_bound_vocabulary_matches(
        qdrant_client=object(),
        summary_tree_routes=routes,
        lane_vectors={"performance": [0.1]},
        lane_queries={"performance": "direct actor performance"},
        max_matches=1,
    )

    assert [row["lexicon_id"] for row in matches] == [target_id]


@pytest.mark.asyncio
async def test_cross_corpus_candidate_lookups_run_concurrently(monkeypatch):
    active = 0
    maximum = 0

    async def fake_search(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    await CorpusVocabularyResolver().resolve(
        query="How should I design the campaign?",
        corpus_ids=["c1", "c2"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1, 0.2],
        qdrant_client=object(),
    )

    assert maximum == 2


@pytest.mark.asyncio
async def test_cross_corpus_vocabulary_hits_are_globally_ranked_with_corpus_ids(
    monkeypatch,
):
    async def fake_search(_client, corpus_id, **_kwargs):
        score = 0.91 if corpus_id == "c2" else 0.82
        return [
                {
                    "lexicon_id": f"lex-{corpus_id}",
                    "term": "purple ocean strategy",
                    "canonical_key": "purple ocean strategy",
                    "aliases_normalized": ["purple ocean strategy"],
                    "retrieval_gloss": (
                        "Purple Ocean Strategy coordinates differentiation and demand."
                    ),
                "support_count": 4,
                "match_type": "exact_alias",
                "score": score,
                "dense_rank": 1,
                "_vector": [0.1, 0.2],
            }
        ]

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )

    class FakeQdrant:
        async def scroll(self, **_kwargs):
            return [], None

    resolution = await CorpusVocabularyResolver().resolve(
        query="How should I use Purple Ocean Strategy?",
        corpus_ids=["c1", "c2"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1, 0.2],
        qdrant_client=FakeQdrant(),
        top_k_per_corpus=1,
    )

    assert [(row["corpus_id"], row["global_rank"]) for row in resolution["matches"]] == [
        ("c2", 1),
        ("c1", 2),
    ]
    assert set(resolution["per_corpus"]) == {"c1", "c2"}
    assert resolution["per_corpus"]["c1"]["matches"][0]["corpus_rank"] == 1
    assert resolution["per_corpus"]["c2"]["matches"][0]["corpus_rank"] == 1
    assert resolution["global_search"] == {
        "mode": "selected_corpus_fanout_global_merge",
        "selected_corpus_ids": ["c1", "c2"],
        "represented_corpus_ids": ["c2", "c1"],
        "per_corpus_reservation": 1,
        "match_count": 2,
    }


@pytest.mark.asyncio
async def test_deterministic_subquery_lane_recovers_named_concept(monkeypatch):
    sticky = {
        "lexicon_id": "lex-sticky",
        "term": "made to stick",
        "canonical_key": "made to stick",
        "aliases": ["sticky idea"],
        "retrieval_gloss": "Principles that make a message memorable and useful.",
        "support_count": 8,
        "match_type": "gloss_vector",
        "score": 0.81,
        "_vector": [0.2],
    }

    async def fake_search(*args, **kwargs):
        if kwargs.get("query_vec") == [0.2]:
            return [sticky]
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    resolution = await CorpusVocabularyResolver().resolve(
        query="Combine Purple Ocean with a memorable ecommerce ad",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1],
        query_lanes=[
            {
                "lane_id": "original",
                "query": "Combine Purple Ocean with a memorable ecommerce ad",
                "query_vector": [0.1],
            },
            {
                "lane_id": "sticky_message",
                "query": "sticky message principles",
                "query_vector": [0.2],
            },
        ],
        qdrant_client=object(),
    )

    assert resolution["query_lanes"][0]["lane_id"] == "original"
    assert resolution["matches"][0]["canonical_key"] == "made to stick"
    assert resolution["matches"][0]["matched_lane_ids"] == ["sticky_message"]


@pytest.mark.asyncio
async def test_bare_ugc_may_be_exploratory_but_never_required(monkeypatch):
    async def fake_search(*args, **kwargs):
        if kwargs.get("with_vectors"):
            return [_facs_match(score=0.74)]
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    resolution = await CorpusVocabularyResolver().resolve(
        query="UGC ad prompt",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1, 0.2],
        qdrant_client=object(),
    )

    assert resolution["matches"][0]["applicability"] == "exploratory_semantic"
    assert resolution["matches"][0]["required"] is False


@pytest.mark.asyncio
async def test_source_evidence_prior_prevents_generic_phrase_crowding(monkeypatch):
    generic = {
        "lexicon_id": "lex-generic",
        "term": "the actors",
        "canonical_key": "the actors",
        "retrieval_gloss": "the actors",
        "support_count": 1,
        "match_type": "gloss_vector",
        "score": 0.69,
        "_vector": [0.1, 0.2],
    }

    async def fake_search(*args, **kwargs):
        if kwargs.get("with_vectors"):
            return [generic, _facs_match(score=0.5)]
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    resolution = await CorpusVocabularyResolver().resolve(
        query="How should an actor face move?",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1, 0.2],
        qdrant_client=object(),
        top_k_per_corpus=1,
    )

    assert resolution["matches"][0]["lexicon_id"] == "lex-facs"
    assert resolution["matches"][0]["evidence_adjusted_score"] > 0.69


@pytest.mark.asyncio
async def test_product_only_query_does_not_force_unrelated_face_expansion(monkeypatch):
    async def fake_search(*args, **kwargs):
        if kwargs.get("with_vectors"):
            return [_facs_match(score=0.91)]
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    resolution = await CorpusVocabularyResolver().resolve(
        query="Show only the product on a white background with no person or face",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1, 0.2],
        qdrant_client=object(),
        excluded_terms=["person", "face"],
    )

    assert resolution["matches"] == []
    assert resolution["rejected_expansions"][0]["lexicon_id"] == "lex-facs"
    assert resolution["rejected_expansions"][0]["reason"] == ("negated_query_concept")


@pytest.mark.asyncio
async def test_explicit_positive_term_survives_attribute_exclusion(monkeypatch):
    facs = _facs_match(score=1.0)
    facs["match_type"] = "qdrant_exact_alias"

    async def fake_search(*args, **kwargs):
        if kwargs.get("with_vectors"):
            return [facs]
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    resolution = await CorpusVocabularyResolver().resolve(
        query="Use FACS to imply emotion without showing a face.",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1, 0.2],
        qdrant_client=object(),
        excluded_terms=["face"],
    )

    assert [row["lexicon_id"] for row in resolution["matches"]] == ["lex-facs"]


@pytest.mark.asyncio
async def test_one_off_model_alias_requires_semantic_confirmation(monkeypatch):
    weak_alias = {
        "lexicon_id": "lex-au17",
        "term": "Action Unit 17",
        "canonical_key": "action unit 17",
        "aliases": ["similar appearance"],
        "aliases_normalized": ["similar appearance"],
        "alias_evidence": [
            {
                "alias": "similar appearance",
                "alias_key": "similar appearance",
                "method": "extraction_query_alias",
                "chunk_id": "chunk-1",
            }
        ],
        "retrieval_gloss": "Action Unit 17 raises the chin and lower lip.",
        "support_count": 4,
        "match_type": "exact_alias+gloss_vector",
        "score": 1.0,
        "gloss_score": 0.31,
        "_vector": [0.1, 0.2],
    }

    async def fake_search(*args, **kwargs):
        if kwargs.get("with_vectors"):
            return [weak_alias]
        return []

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    resolution = await CorpusVocabularyResolver().resolve(
        query="similar appearance",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1, 0.2],
        qdrant_client=object(),
    )

    assert resolution["matches"] == []
    assert resolution["rejected_expansions"][0]["reason"] == (
        "below_grounded_expansion_threshold"
    )


@pytest.mark.asyncio
async def test_focused_tier_never_calls_mongo(monkeypatch):
    async def fake_search(*args, **kwargs):
        return []

    async def forbidden_mongo(*args, **kwargs):
        raise AssertionError("Focused vocabulary resolution must remain Qdrant-only")

    monkeypatch.setattr(
        "services.storage.qdrant_writer.search_lexicon_entries", fake_search
    )
    monkeypatch.setattr("services.retriever.vocabulary._mongo_matches", forbidden_mongo)

    resolution = await CorpusVocabularyResolver().resolve(
        query="What is positioning?",
        corpus_ids=["c1"],
        tier=RetrievalTier.qdrant_only,
        query_vector=[0.1],
        qdrant_client=object(),
        db=object(),
    )

    assert resolution["store_usage"] == {
        "qdrant": True,
        "mongo": False,
        "neo4j": False,
    }
