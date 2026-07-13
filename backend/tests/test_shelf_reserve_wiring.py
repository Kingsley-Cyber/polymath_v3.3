"""P1.5 shelf_reserve wiring tests (dark behind SHELF_RESERVE_ENABLED).

Shared invariants under test, per the seat-pass contract:

- off-by-default: no ``shelf_reserve_context`` = byte-identical selection AND
  meta versus today's selector (zero behavior change while the flag is dark);
- a bridge seat replaces ONLY the weakest unprotected seat, and ONLY when the
  pooled candidate passes the calibrated corpus-reservation bound (P0.3);
- a counterbalance seat exists ONLY on a versioned policy trigger;
- every skipped role records a reason (skip beats weak fill);
- un-pooled documents are NEVER added and scores are NEVER modified;
- ``reading_path`` is role-ordered over seated + already-present docs;
- diagnostics carry the exact documented shape.
"""

import os
from dataclasses import dataclass

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from services.facets.final_selector import (
    FacetCandidate,
    ShelfReserveContext,
    select_facet_final,
)
from services.retriever.reservation_policy import corpus_reservation_bound


@dataclass
class DummyChunk:
    chunk_id: str
    doc_id: str
    corpus_id: str = "c1"
    text: str = ""


def _candidate(
    chunk_id: str,
    *,
    score: float,
    lanes: set[str] | None = None,
    doc_id: str | None = None,
    corpus_id: str = "c1",
    junk: bool = False,
    order: int = 0,
) -> FacetCandidate:
    item = DummyChunk(
        chunk_id=chunk_id,
        doc_id=doc_id or f"doc-{chunk_id}",
        corpus_id=corpus_id,
    )
    return FacetCandidate(
        item=item,
        score=score,
        lanes=lanes or set(),
        key=f"chunk:{chunk_id}",
        doc_id=item.doc_id,
        corpus_id=item.corpus_id,
        junk=junk,
        order=order,
    )


def _entries(values: tuple[str, ...] | list[str]) -> list[dict]:
    return [
        {"value": value, "value_key": value, "source_ids": [f"src-{value}"]}
        for value in values
    ]


def _card(
    corpus_id: str,
    doc_id: str,
    *,
    subjects: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = (),
    mechanisms: tuple[str, ...] = (),
    principles: tuple[str, ...] = (),
) -> dict:
    return {
        "schema_version": "librarian_card.v0",
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "central_subjects": _entries(subjects),
        "candidate_latent_subjects": [],
        "capabilities_developed": _entries(capabilities),
        "mechanisms_taught": _entries(mechanisms),
        "transferable_principles": _entries(principles),
        "evidence_spans": {
            "source_parent_ids": [],
            "source_chunk_ids": [],
            "section_ids": [],
        },
    }


def _context(
    *,
    query_concepts: list[str],
    cards: list[dict],
    enabled: bool = True,
) -> ShelfReserveContext:
    return ShelfReserveContext(
        query_concepts=query_concepts,
        cards_by_doc={
            (str(card["corpus_id"]), str(card["doc_id"])): card for card in cards
        },
        enabled=enabled,
    )


# The direct-shelf pool: three same-corpus docs about the query concept.
def _direct_pool() -> list[FacetCandidate]:
    return [
        _candidate("a1", score=0.90, doc_id="doc-a1", corpus_id="alpha", order=1),
        _candidate("a2", score=0.85, doc_id="doc-a2", corpus_id="alpha", order=2),
        _candidate("a3", score=0.80, doc_id="doc-a3", corpus_id="alpha", order=3),
    ]


def _direct_cards() -> list[dict]:
    return [
        _card("alpha", "doc-a1", subjects=("compounding_growth",)),
        _card("alpha", "doc-a2", subjects=("compounding_growth",)),
        _card("alpha", "doc-a3", subjects=("compounding_growth",)),
    ]


def _bridge_card(corpus_id: str = "alpha", doc_id: str = "doc-bridge") -> dict:
    # Bridge v0 eligibility: shared transferable principle WITH source ids,
    # different central subjects (zero subject overlap with the query).
    return _card(
        corpus_id,
        doc_id,
        subjects=("evolutionary_biology",),
        principles=("compounding_growth",),
    )


# ── off-by-default -------------------------------------------------------


def test_no_context_is_byte_identical_to_legacy_call():
    def run(**kwargs):
        return select_facet_final(
            _direct_pool(),
            missing_lanes=[],
            max_items=3,
            **kwargs,
        )

    legacy_selected, legacy_meta = run()
    off_selected, off_meta = run(shelf_reserve_context=None)

    assert [item.chunk_id for item in legacy_selected] == [
        item.chunk_id for item in off_selected
    ]
    assert legacy_meta == off_meta
    assert "shelf_reserve" not in off_meta


def test_disabled_context_changes_nothing_but_records_disabled():
    legacy_selected, _ = select_facet_final(
        _direct_pool(), missing_lanes=[], max_items=3
    )
    selected, meta = select_facet_final(
        _direct_pool(),
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"],
            cards=[*_direct_cards(), _bridge_card()],
            enabled=False,
        ),
    )
    assert [item.chunk_id for item in selected] == [
        item.chunk_id for item in legacy_selected
    ]
    assert meta["shelf_reserve"]["enabled"] is False
    assert meta["shelf_reserve"]["skipped"] == {
        "bridge": "disabled",
        "counterbalance": "disabled",
    }


# ── bridge seat ----------------------------------------------------------


def test_bridge_seat_replaces_weakest_unprotected_when_gate_passes():
    pool = [*_direct_pool(), _candidate(
        "b1", score=0.50, doc_id="doc-bridge", corpus_id="alpha", order=4
    )]
    # Calibrated gate: bound = max(0.25, 0.9 * 0.30) = 0.27; 0.50 passes.
    assert corpus_reservation_bound(0.90) == pytest.approx(0.27)
    selected, meta = select_facet_final(
        pool,
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"],
            cards=[*_direct_cards(), _bridge_card()],
        ),
    )
    ids = [item.chunk_id for item in selected]
    # The weakest unprotected seat (a3: same corpus count > 1, no lanes) was
    # replaced; stronger seats survive.
    assert ids == ["a1", "a2", "b1"]
    shelf = meta["shelf_reserve"]
    assert shelf["enabled"] is True
    assert shelf["seated"] == [
        {
            "doc_id": "doc-bridge",
            "role": "bridge",
            "matched_fields": {"transferable_principles": ["compounding_growth"]},
            "evidence_ids": ["src-compounding_growth"],
        }
    ]
    assert "bridge" not in shelf["skipped"]


def test_bridge_seat_never_replaces_protected_lane_reservation():
    pool = [
        _candidate("a1", score=0.90, doc_id="doc-a1", corpus_id="alpha", order=1),
        _candidate("a2", score=0.85, doc_id="doc-a2", corpus_id="alpha", order=2),
        _candidate(
            "lane-seat",
            score=0.30,
            doc_id="doc-a3",
            corpus_id="alpha",
            lanes={"mechanism"},
            order=3,
        ),
        _candidate("b1", score=0.50, doc_id="doc-bridge", corpus_id="alpha", order=4),
    ]
    selected, meta = select_facet_final(
        pool,
        missing_lanes=["mechanism"],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"],
            cards=[*_direct_cards(), _bridge_card()],
        ),
    )
    ids = [item.chunk_id for item in selected]
    # The lane reservation (weakest score, but protected) survives; the
    # weakest UNPROTECTED seat (a2) is replaced instead.
    assert "lane-seat" in ids
    assert "b1" in ids
    assert "a2" not in ids
    assert meta["shelf_reserve"]["seated"][0]["doc_id"] == "doc-bridge"


def test_bridge_seat_skipped_below_reservation_bound():
    pool = [*_direct_pool(), _candidate(
        "b1", score=0.10, doc_id="doc-bridge", corpus_id="alpha", order=4
    )]
    # 0.10 < bound (0.27): gate fails, seat skipped, selection untouched.
    selected, meta = select_facet_final(
        pool,
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"],
            cards=[*_direct_cards(), _bridge_card()],
        ),
    )
    assert [item.chunk_id for item in selected] == ["a1", "a2", "a3"]
    assert meta["shelf_reserve"]["seated"] == []
    assert meta["shelf_reserve"]["skipped"]["bridge"] == "below_reservation_bound"


def test_bridge_role_already_selected_is_not_double_seated():
    pool = [*_direct_pool()[:2], _candidate(
        "b1", score=0.88, doc_id="doc-bridge", corpus_id="alpha", order=3
    )]
    selected, meta = select_facet_final(
        pool,
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"],
            cards=[*_direct_cards()[:2], _bridge_card()],
        ),
    )
    ids = [item.chunk_id for item in selected]
    assert ids == ["a1", "b1", "a2"]  # plain score order; nothing reshuffled
    shelf = meta["shelf_reserve"]
    assert shelf["seated"] == []
    assert shelf["skipped"]["bridge"] == "already_selected:doc-bridge"


# ── counterbalance seat --------------------------------------------------


def test_counterbalance_seat_only_on_policy_trigger():
    counter_card = _card("alpha", "doc-ethics", subjects=("marketing_ethics",))
    pool = [*_direct_pool(), _candidate(
        "e1", score=0.55, doc_id="doc-ethics", corpus_id="alpha", order=4
    )]

    # NOT triggered: query concepts do not intersect HIGH_MISUSE_KEYS.
    selected, meta = select_facet_final(
        pool,
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"],
            cards=[*_direct_cards(), counter_card],
        ),
    )
    assert [item.chunk_id for item in selected] == ["a1", "a2", "a3"]
    assert meta["shelf_reserve"]["seated"] == []
    assert meta["shelf_reserve"]["skipped"]["counterbalance"].startswith(
        "policy_not_triggered"
    )

    # Triggered: "persuasion" is a HIGH_MISUSE_KEYS family member.
    persuasion_cards = [
        _card("alpha", "doc-a1", subjects=("persuasion",)),
        _card("alpha", "doc-a2", subjects=("persuasion",)),
        _card("alpha", "doc-a3", subjects=("persuasion",)),
        counter_card,
    ]
    selected, meta = select_facet_final(
        pool,
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["persuasion"],
            cards=persuasion_cards,
        ),
    )
    ids = [item.chunk_id for item in selected]
    assert "e1" in ids
    seated = meta["shelf_reserve"]["seated"]
    assert [row["role"] for row in seated] == ["counterbalance"]
    assert seated[0]["doc_id"] == "doc-ethics"
    assert seated[0]["matched_fields"] == {"central_subjects": ["marketing_ethics"]}


# ── shared constraints ---------------------------------------------------


def test_never_adds_unpooled_documents():
    # Bridge card exists for a document with NO pooled candidate.
    selected, meta = select_facet_final(
        _direct_pool(),
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"],
            cards=[*_direct_cards(), _bridge_card(doc_id="doc-unpooled")],
        ),
    )
    ids = {item.doc_id for item in selected}
    assert "doc-unpooled" not in ids
    assert meta["shelf_reserve"]["seated"] == []
    assert meta["shelf_reserve"]["skipped"]["bridge"] == "not_in_candidate_pool"


def test_scores_are_never_modified():
    pool = [*_direct_pool(), _candidate(
        "b1", score=0.50, doc_id="doc-bridge", corpus_id="alpha", order=4
    )]
    before = {candidate.key: candidate.score for candidate in pool}
    select_facet_final(
        pool,
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"],
            cards=[*_direct_cards(), _bridge_card()],
        ),
    )
    assert {candidate.key: candidate.score for candidate in pool} == before


def test_skip_reasons_recorded_for_missing_inputs():
    _, no_concepts = select_facet_final(
        _direct_pool(),
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(query_concepts=[], cards=_direct_cards()),
    )
    assert no_concepts["shelf_reserve"]["skipped"] == {
        "bridge": "no_query_concepts",
        "counterbalance": "no_query_concepts",
    }

    _, no_cards = select_facet_final(
        _direct_pool(),
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"], cards=[]
        ),
    )
    assert no_cards["shelf_reserve"]["skipped"] == {
        "bridge": "no_cards_for_pooled_documents",
        "counterbalance": "no_cards_for_pooled_documents",
    }


# ── reading path + diagnostics shape --------------------------------------


def test_reading_path_is_role_ordered_over_seated_and_present_docs():
    pool = [*_direct_pool(), _candidate(
        "b1", score=0.50, doc_id="doc-bridge", corpus_id="alpha", order=4
    )]
    _, meta = select_facet_final(
        pool,
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"],
            cards=[*_direct_cards(), _bridge_card()],
        ),
    )
    path = meta["shelf_reserve"]["reading_path"]
    roles = [row["role"] for row in path]
    # direct docs first, then the seated bridge; doc-a3 was replaced so it is
    # not on the path, and no doc appears twice.
    assert roles == ["direct", "direct", "bridge"]
    assert [row["doc_id"] for row in path] == ["doc-a1", "doc-a2", "doc-bridge"]
    assert len({row["doc_id"] for row in path}) == len(path)


def test_diagnostics_shape():
    pool = [*_direct_pool(), _candidate(
        "b1", score=0.50, doc_id="doc-bridge", corpus_id="alpha", order=4
    )]
    _, meta = select_facet_final(
        pool,
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=_context(
            query_concepts=["compounding_growth"],
            cards=[*_direct_cards(), _bridge_card()],
        ),
    )
    shelf = meta["shelf_reserve"]
    assert set(shelf) == {
        "enabled",
        "roles_considered",
        "seated",
        "skipped",
        "policy_version",
        "reading_path",
    }
    assert shelf["roles_considered"] == ["bridge", "counterbalance"]
    assert shelf["policy_version"] == "shelf_policy.v0"
    for row in shelf["seated"]:
        assert set(row) == {"doc_id", "role", "matched_fields", "evidence_ids"}
    for row in shelf["reading_path"]:
        assert set(row) == {"doc_id", "corpus_id", "role"}
    assert isinstance(shelf["skipped"], dict)


# ── orchestrator wiring helpers -------------------------------------------


def test_query_concepts_prefer_vocabulary_canonical_keys():
    import services.chat_orchestrator as chat_module

    diagnostics = {
        "vocabulary_resolution": {
            "matches": [
                {"canonical_key": "compounding growth", "canonical_name": "Compounding"},
                {"canonical_key": "natural selection"},
                {"canonical_key": "compounding growth"},  # dedupe
            ]
        }
    }
    concepts = chat_module._shelf_reserve_query_concepts(
        "how does compounding growth mirror natural selection?", diagnostics
    )
    assert concepts == ["compounding growth", "natural selection"]


def test_query_concepts_fall_back_to_concept_groups():
    import services.chat_orchestrator as chat_module

    concepts = chat_module._shelf_reserve_query_concepts(
        "how does compounding growth mirror natural selection?", {}
    )
    assert concepts  # deterministic non-empty fallback
    assert all(isinstance(concept, str) and concept for concept in concepts)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, length=None):
        return list(self._rows)


class _FakeCollection:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[dict, dict]] = []

    def find(self, query, projection=None):
        self.calls.append((query, projection))
        return _FakeCursor(self.rows)


class _FakeDb:
    def __init__(self, rows):
        self.collection = _FakeCollection(rows)

    def __getitem__(self, name):
        assert name == "librarian_cards"
        return self.collection


@pytest.mark.asyncio
async def test_context_builder_issues_one_find_for_pooled_docs():
    import services.chat_orchestrator as chat_module
    from models.schemas import SourceChunk

    sources = [
        SourceChunk(
            chunk_id=f"chunk-{i}",
            parent_id=f"parent-{i}",
            doc_id=doc_id,
            corpus_id="alpha",
            text="t",
            score=0.5,
            source_tier="qdrant_child",
        )
        for i, doc_id in enumerate(["doc-a1", "doc-a1", "doc-bridge"])
    ]
    card = _bridge_card()
    db = _FakeDb([card])
    context = await chat_module._shelf_reserve_context_for_pool(
        sources,
        query_concepts=["compounding_growth"],
        db=db,
    )
    assert len(db.collection.calls) == 1
    query, projection = db.collection.calls[0]
    assert query == {
        "$or": [
            {"corpus_id": "alpha", "doc_id": {"$in": ["doc-a1", "doc-bridge"]}}
        ]
    }
    assert projection == chat_module._SHELF_RESERVE_CARD_PROJECTION
    assert context.enabled is True
    assert context.query_concepts == ["compounding_growth"]
    assert context.cards_by_doc == {("alpha", "doc-bridge"): card}


@pytest.mark.asyncio
async def test_context_builder_degrades_to_empty_cards_on_mongo_failure():
    import services.chat_orchestrator as chat_module
    from models.schemas import SourceChunk

    class _BrokenDb:
        def __getitem__(self, name):
            raise RuntimeError("mongo down")

    sources = [
        SourceChunk(
            chunk_id="chunk-1",
            parent_id="parent-1",
            doc_id="doc-a1",
            corpus_id="alpha",
            text="t",
            score=0.5,
            source_tier="qdrant_child",
        )
    ]
    context = await chat_module._shelf_reserve_context_for_pool(
        sources,
        query_concepts=["compounding_growth"],
        db=_BrokenDb(),
    )
    assert context.cards_by_doc == {}
    # The selector then records the skip reason instead of raising.
    _, meta = select_facet_final(
        _direct_pool(),
        missing_lanes=[],
        max_items=3,
        shelf_reserve_context=context,
    )
    assert meta["shelf_reserve"]["skipped"]["bridge"] == (
        "no_cards_for_pooled_documents"
    )
