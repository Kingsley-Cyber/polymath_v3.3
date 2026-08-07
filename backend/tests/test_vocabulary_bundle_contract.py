"""Contract tests for models/vocabulary_resolution.py (Slice 1 scaffolding).

The wrapper must be LOSSLESS against the resolver's dict contract: round
trip dict → bundle → dict preserves every key the resolver emits, including
fields the typed model does not name (extra="allow"). Slice 1 does not
change CorpusVocabularyResolver itself — these tests pin the wrapper.
"""

from models.vocabulary_resolution import (
    VOCABULARY_BUNDLE_SCHEMA_VERSION,
    VocabularyResolutionBundle,
)

# Shape mirrors CorpusVocabularyResolver.resolve output
# (services/retriever/vocabulary.py) without importing services.
_RESOLVER_DICT = {
    "version": "corpus_vocabulary.v3",
    "query": "How does FACS score facial movement?",
    "query_lanes": [{"lane_id": "original", "query": "How does FACS score facial movement?", "has_vector": True}],
    "matches": [
        {
            "corpus_id": "c1",
            "lexicon_id": "lex-facs",
            "term": "facial action coding system",
            "canonical_key": "facial action coding system",
            "applicability": "source_term_overlap",
            "score": 0.81,
            "evidence_adjusted_score": 0.84,
            "global_rank": 1,
            "corpus_rank": 1,
            "required": False,
        }
    ],
    "per_corpus": {"c1": {"matches": [{"corpus_rank": 1}], "status": "resolved"}},
    "document_profiles": [{"corpus_id": "c1", "doc_id": "doc-facs", "title": "FACS"}],
    "raptor_ancestors": [{"ancestor_level": "document_root"}],
    "rejected_expansions": [],
    "disabled_lexicon_ids": [],
    "global_search": {
        "mode": "selected_corpus_fanout_global_merge",
        "selected_corpus_ids": ["c1"],
        "represented_corpus_ids": ["c1"],
        "per_corpus_reservation": 6,
        "match_count": 1,
    },
    "store_usage": {"qdrant": True, "mongo": False, "neo4j": False},
    "degraded_stores": [],
    "cache": {"hit": False},
    "duration_s": 0.42,
}


def test_bundle_wraps_resolver_dict_without_mutation():
    snapshot = dict(_RESOLVER_DICT)
    bundle = VocabularyResolutionBundle.from_resolution(_RESOLVER_DICT)
    assert _RESOLVER_DICT == snapshot  # wrapper never mutates its input
    assert bundle.schema_version == VOCABULARY_BUNDLE_SCHEMA_VERSION
    assert bundle.version == "corpus_vocabulary.v3"
    assert bundle.matches[0]["lexicon_id"] == "lex-facs"


def test_round_trip_is_lossless_for_known_and_extra_fields():
    bundle = VocabularyResolutionBundle.from_resolution(_RESOLVER_DICT)
    restored = bundle.to_resolution_dict()
    # Typed fields round-trip; extra fields (store_usage, query_lanes,
    # disabled_lexicon_ids, degraded_stores) must survive via extra="allow".
    for key, value in _RESOLVER_DICT.items():
        if key == "global_search":
            # Typed sub-record: compare by semantic content, not identity.
            assert restored["global_search"]["mode"] == value["mode"]
            assert restored["global_search"]["match_count"] == value["match_count"]
            continue
        assert restored[key] == value, f"round-trip lost field: {key}"


def test_global_search_record_proves_corpus_scoped_resolution():
    bundle = VocabularyResolutionBundle.from_resolution(_RESOLVER_DICT)
    assert bundle.global_search is not None
    assert bundle.global_search.mode == "selected_corpus_fanout_global_merge"
    assert bundle.global_search.selected_corpus_ids == ("c1",)
    assert bundle.global_search.per_corpus_reservation == 6


def test_empty_resolution_wraps_cleanly():
    bundle = VocabularyResolutionBundle.from_resolution({})
    assert bundle.matches == ()
    assert bundle.per_corpus == {}
    assert bundle.global_search is None
