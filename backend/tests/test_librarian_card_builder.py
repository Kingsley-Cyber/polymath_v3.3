"""Phase 1 / P2.1 — deterministic librarian_card.v0 builder tests.

Fakes mirror the _FakeDb/_FakeCollection idiom in tests/test_storage_lifecycle.py,
extended minimally with async find cursors (the _Cursor idiom already used in
tests/test_corpus_lexicon.py).
"""

from __future__ import annotations

import pytest

from services.librarian.card_builder import (
    BUILDER_VERSION,
    CARD_SCHEMA_VERSION,
    build_corpus_cards,
    build_librarian_card,
    slim_card_payload,
)


# ── fakes ───────────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    async def to_list(self, length=None):
        return list(self.rows if length is None else self.rows[:length])


def _matches(row, query):
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(row, sub) for sub in expected):
                return False
        elif key == "$or":
            if not any(_matches(row, sub) for sub in expected):
                return False
        elif isinstance(expected, dict) and (
            "$exists" in expected or "$in" in expected
        ):
            if "$exists" in expected and (key in row) != bool(expected["$exists"]):
                return False
            if "$in" in expected and row.get(key) not in expected["$in"]:
                return False
        else:
            value = row.get(key)
            if isinstance(value, list):
                if expected != value and expected not in value:
                    return False
            elif value != expected:
                return False
    return True


class _UpdateResult:
    matched_count = 1
    modified_count = 1


class _FakeCollection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.update_one_calls = []

    def find(self, query, projection=None):
        return _FakeCursor([row for row in self.rows if _matches(row, query)])

    async def find_one(self, query, projection=None):
        for row in self.rows:
            if _matches(row, query):
                return row
        return None

    async def update_one(self, query, update, upsert=False):
        self.update_one_calls.append((query, update, upsert))
        return _UpdateResult()


class _FakeDb:
    def __init__(self, collections=None):
        self.collections = collections or {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _FakeCollection())


# ── fixture data ────────────────────────────────────────────────────────────

CORPUS = "c1"
DOC = "d1"


def _document(doc_id=DOC, *, profile=None):
    return {
        "doc_id": doc_id,
        "corpus_id": CORPUS,
        "status": "active",
        "doc_profile": (
            profile
            if profile is not None
            else {
                "summary_id": f"docsum_{doc_id}",
                "concepts": ["Cultural Priming"],
                "section_ids": [f"section_{doc_id}_0000"],
            }
        ),
    }


def _lexicon_entries():
    return [
        {
            "corpus_id": CORPUS,
            "lexicon_id": "lexA",
            "canonical_name": "Cultural Priming",
            "canonical_key": "cultural priming",
            "member_keys": ["cultural priming"],
            "support_count": 5,
            "mean_confidence": 0.9,
            "source_document_ids": [DOC, "d2"],
            "source_document_support": [
                {"doc_id": DOC, "support_count": 3},
                {"doc_id": "d2", "support_count": 2},
            ],
            "retrieval_eligible": True,
        },
        {
            "corpus_id": CORPUS,
            "lexicon_id": "lexB",
            "canonical_name": "Self Construal",
            "canonical_key": "self construal",
            "member_keys": ["self construal"],
            "support_count": 2,
            "mean_confidence": 0.8,
            "source_document_ids": [DOC],
            "source_document_support": [{"doc_id": DOC, "support_count": 2}],
            "retrieval_eligible": True,
        },
        {
            "corpus_id": CORPUS,
            "lexicon_id": "lexJunk",
            "canonical_name": "P 214",
            "canonical_key": "p 214",
            "member_keys": ["p 214"],
            "support_count": 9,
            "source_document_ids": [DOC],
            "source_document_support": [{"doc_id": DOC, "support_count": 9}],
            "retrieval_eligible": False,
        },
    ]


def _lexicon_sources():
    return [
        {
            "corpus_id": CORPUS,
            "doc_id": DOC,
            "canonical_key": "cultural priming",
            "canonical_keys": ["cultural priming"],
            "canonical_names": [{"value": "Cultural Priming", "count": 3}],
            "definitions": [
                {
                    "text": "Cultural priming is activating cultural frames before judgment.",
                    "chunk_id": "ch1",
                    "parent_id": "p1",
                    "confidence": 0.9,
                    "method": "extraction_definitional_phrase",
                }
            ],
            "application_contexts": [
                {
                    "predicate": "used_for",
                    "target": "advertising research",
                    "chunk_id": "ch1",
                    "parent_id": "p1",
                    "confidence": 0.8,
                }
            ],
            "contextual_usages": [
                {
                    "text": "Priming shifts consumer judgments across cultures",
                    "method": "parent_main_mechanism",
                    "chunk_id": "ch2",
                    "parent_id": "p2",
                    "confidence": 0.7,
                },
                {
                    # non-functional method — must NOT become a capability
                    "text": "Chapter three discusses culture",
                    "method": "structural_context",
                    "chunk_id": "ch2",
                    "parent_id": "p2",
                    "confidence": 0.9,
                },
            ],
            "source_chunk_ids": ["ch1", "ch2"],
            "source_parent_ids": ["p1", "p2"],
            "support_count": 3,
        },
        {
            "corpus_id": CORPUS,
            "doc_id": DOC,
            "canonical_key": "self construal",
            "canonical_keys": ["self construal"],
            "canonical_names": [{"value": "Self Construal", "count": 2}],
            "definitions": [],
            "application_contexts": [],
            "contextual_usages": [],
            "source_chunk_ids": ["ch2"],
            "source_parent_ids": ["p2"],
            "support_count": 2,
        },
    ]


def _ghost_rows(extra_entities=()):
    entities = [
        {"canonical_name": "Hofstede Model", "confidence": 0.95},
        {"canonical_name": "Cultural Priming", "confidence": 0.9},
        *extra_entities,
    ]
    return [
        {
            "corpus_id": CORPUS,
            "doc_id": DOC,
            "chunk_id": "ch1",
            "status": "ok",
            "entities": entities,
        }
    ]


def _parent_rows():
    return [
        {
            "corpus_id": CORPUS,
            "doc_id": DOC,
            "parent_id": "p1",
            "mechanisms": ["priming"],
            "main_mechanism": "Culture primes judgment before evaluation.",
            "semantic_chunk_type": "claim",
            "quality_score": 1.0,
            "validation_status": "valid",
            "latent_concepts": [
                {
                    "concept": "Acculturation Dynamics",
                    "evidence_basis": "direct",
                    "confidence": 0.6,
                },
                {"concept": "Speculative Thing", "evidence_basis": "inferred"},
            ],
        },
        {
            "corpus_id": CORPUS,
            "doc_id": DOC,
            "parent_id": "p2",
            "mechanisms": ["priming", "framing"],
            "main_mechanism": None,
            "semantic_chunk_type": "warning",
            "quality_score": 0.9,
            "validation_status": "valid",
        },
        {
            # failed validation — its mechanisms must never seed the card
            "corpus_id": CORPUS,
            "doc_id": DOC,
            "parent_id": "p3",
            "mechanisms": ["should_not_appear"],
            "semantic_chunk_type": "procedure",
            "validation_status": "failed",
        },
    ]


def _db(**overrides):
    collections = {
        "documents": _FakeCollection([_document()]),
        "corpus_lexicon": _FakeCollection(_lexicon_entries()),
        "corpus_lexicon_sources": _FakeCollection(_lexicon_sources()),
        "ghost_b_extractions": _FakeCollection(_ghost_rows()),
        "parent_chunks": _FakeCollection(_parent_rows()),
        "librarian_cards": _FakeCollection(),
    }
    collections.update(overrides)
    return _FakeDb(collections)


def _values(card, field):
    return [entry["value"] for entry in card[field]]


# ── tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejects_values_without_source_ids():
    """A profile concept with no summary_id/section_ids has no source ids —
    it must be rejected at build time and counted, never written value-less."""
    db = _db(
        documents=_FakeCollection(
            [
                _document(
                    profile={
                        "summary_id": None,
                        "concepts": ["Orphan Concept"],
                        "section_ids": [],
                    }
                )
            ]
        ),
        corpus_lexicon=_FakeCollection(),
        corpus_lexicon_sources=_FakeCollection(),
        ghost_b_extractions=_FakeCollection(),
    )
    card = await build_librarian_card(db, corpus_id=CORPUS, doc_id=DOC)
    assert card is not None  # mechanisms still seed the card
    assert "Orphan Concept" not in _values(card, "central_subjects")
    assert card["rejected_value_count"] >= 1


@pytest.mark.asyncio
async def test_every_entry_carries_full_provenance():
    db = _db()
    card = await build_librarian_card(db, corpus_id=CORPUS, doc_id=DOC)
    assert card is not None
    assert card["schema_version"] == CARD_SCHEMA_VERSION
    assert card["builder_version"] == BUILDER_VERSION
    assert card["built_at"] is not None
    populated = 0
    for field in (
        "central_subjects",
        "mechanisms_taught",
        "candidate_latent_subjects",
        "capabilities_developed",
        "problems_addressed",
        "transferable_principles",
        "risks_or_likely_misuse",
        "counterbalancing_concepts",
    ):
        for entry in card[field]:
            populated += 1
            assert entry["value"], (field, entry)
            assert entry["method"], (field, entry)
            assert entry["source_ids"], (field, entry)
            assert isinstance(entry["confidence"], float), (field, entry)
    assert populated > 0
    # spans aggregate doc-tied lexicon provenance + tree section ids
    assert card["evidence_spans"]["source_parent_ids"] == ["p1", "p2"]
    assert card["evidence_spans"]["source_chunk_ids"] == ["ch1", "ch2"]
    assert card["evidence_spans"]["section_ids"] == [f"section_{DOC}_0000"]


@pytest.mark.asyncio
async def test_field_seed_contracts():
    db = _db()
    card = await build_librarian_card(db, corpus_id=CORPUS, doc_id=DOC)

    subjects = _values(card, "central_subjects")
    assert "Cultural Priming" in subjects  # lexicon + profile + ghost merge
    assert "Self Construal" in subjects
    assert "Hofstede Model" in subjects  # ghost-only
    assert "P 214" not in subjects  # retrieval_eligible=False junk gate
    merged = next(
        entry
        for entry in card["central_subjects"]
        if entry["value"] == "Cultural Priming"
    )
    assert merged["method"] == (
        "doc_profile_concept+ghost_b_entity+lexicon_canonical_term"
    )

    mechanisms = _values(card, "mechanisms_taught")
    assert "priming" in mechanisms
    assert "framing" in mechanisms
    assert "Culture primes judgment before evaluation." in mechanisms
    assert "should_not_appear" not in mechanisms  # failed-validation parent
    priming = next(
        entry for entry in card["mechanisms_taught"] if entry["value"] == "priming"
    )
    assert priming["source_ids"] == ["p1", "p2"]
    assert priming["support"] == 2

    capabilities = _values(card, "capabilities_developed")
    assert "used for advertising research" in capabilities
    assert "Priming shifts consumer judgments across cultures" in capabilities
    assert "Chapter three discusses culture" not in capabilities

    problems = card["problems_addressed"]
    assert len(problems) == 1  # ONLY definitional evidence
    assert problems[0]["method"] == (
        "lexicon_definition:extraction_definitional_phrase"
    )
    assert problems[0]["subject"] == "Cultural Priming"

    principles = card["transferable_principles"]
    assert _values(card, "transferable_principles") == ["Cultural Priming"]
    assert principles[0]["corpus_support_count"] == 5
    assert principles[0]["distinct_document_count"] == 2

    assert card["counterbalancing_concepts"] == []


@pytest.mark.asyncio
async def test_latent_concepts_only_in_candidate_field():
    db = _db()
    card = await build_librarian_card(db, corpus_id=CORPUS, doc_id=DOC)
    latent = _values(card, "candidate_latent_subjects")
    assert latent == ["Acculturation Dynamics"]  # direct only
    assert card["candidate_latent_subjects"][0]["method"] == (
        "parent_latent_concept_direct"
    )
    assert card["candidate_latent_subjects"][0]["source_ids"] == ["p1"]
    # LLM-derived values never leak into the deterministic fields
    for field in (
        "central_subjects",
        "mechanisms_taught",
        "capabilities_developed",
        "problems_addressed",
        "transferable_principles",
    ):
        assert "Acculturation Dynamics" not in _values(card, field)
        assert "Speculative Thing" not in _values(card, field)
    assert "Speculative Thing" not in latent


@pytest.mark.asyncio
async def test_risks_strictly_from_warning_chunk_type():
    db = _db()
    card = await build_librarian_card(db, corpus_id=CORPUS, doc_id=DOC)
    assert len(card["risks_or_likely_misuse"]) == 1
    risk = card["risks_or_likely_misuse"][0]
    assert risk["value"] == "warning_chunks_present"  # flag, no prose
    assert risk["source_ids"] == ["p2"]  # only the warning parent
    assert risk["method"] == "parent_semantic_chunk_type_warning"

    # no warning parents → field strictly empty
    rows = [row for row in _parent_rows() if row["semantic_chunk_type"] != "warning"]
    db = _db(parent_chunks=_FakeCollection(rows))
    card = await build_librarian_card(db, corpus_id=CORPUS, doc_id=DOC)
    assert card["risks_or_likely_misuse"] == []


@pytest.mark.asyncio
async def test_returns_none_on_zero_seeds():
    db = _db(
        documents=_FakeCollection([_document(profile={})]),
        corpus_lexicon=_FakeCollection(),
        corpus_lexicon_sources=_FakeCollection(),
        ghost_b_extractions=_FakeCollection(),
        parent_chunks=_FakeCollection(),
    )
    assert await build_librarian_card(db, corpus_id=CORPUS, doc_id=DOC) is None
    # missing document row also degrades to None
    assert await build_librarian_card(db, corpus_id=CORPUS, doc_id="ghost") is None


@pytest.mark.asyncio
async def test_slim_caps_and_deterministic_ordering():
    extra = [
        {"canonical_name": f"Filler Concept {chr(ord('a') + i)}", "confidence": 0.5}
        for i in range(10)
    ]
    db = _db(ghost_b_extractions=_FakeCollection(_ghost_rows(extra)))
    card_a = await build_librarian_card(db, corpus_id=CORPUS, doc_id=DOC)
    card_b = await build_librarian_card(db, corpus_id=CORPUS, doc_id=DOC)

    # deterministic: identical output modulo the build timestamp
    strip = lambda card: {k: v for k, v in card.items() if k != "built_at"}
    assert strip(card_a) == strip(card_b)

    # ordering: support desc, then value_key alpha
    entries = card_a["central_subjects"]
    keys = [(-entry["support"], entry["value_key"]) for entry in entries]
    assert keys == sorted(keys)

    slim = slim_card_payload(card_a)
    assert slim["corpus_id"] == CORPUS and slim["doc_id"] == DOC
    assert len(slim["subjects"]) == 8  # >8 available, capped
    assert len(slim["mechanisms"]) <= 6
    assert len(slim["capabilities"]) <= 6
    assert slim["subjects"] == [entry["value"] for entry in entries[:8]]
    assert all(isinstance(value, str) for value in slim["subjects"])


@pytest.mark.asyncio
async def test_build_corpus_cards_upserts_and_reports_coverage():
    # d3 is referenced by no projection at all (d2 would legitimately seed a
    # card because lexA's source_document_ids ties it to the corpus lexicon)
    zero_seed_doc = _document("d3", profile={})
    db = _db()
    db["documents"].rows.append(zero_seed_doc)

    result = await build_corpus_cards(db, corpus_id=CORPUS)

    assert result["documents_scanned"] == 2
    assert result["cards_built"] == 1
    assert result["cards_skipped_zero_seed"] == 1
    coverage = result["field_coverage"]
    assert coverage["central_subjects"]["documents_with_values"] == 1
    assert coverage["central_subjects"]["total_values"] >= 3
    assert coverage["counterbalancing_concepts"]["documents_with_values"] == 0

    calls = db["librarian_cards"].update_one_calls
    assert len(calls) == 1
    query, update, upsert = calls[0]
    assert query == {"corpus_id": CORPUS, "doc_id": DOC}
    assert upsert is True
    assert update["$set"]["schema_version"] == CARD_SCHEMA_VERSION
    assert "created_at" in update["$setOnInsert"]
