from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

import services.retriever.librarian_planner as planner_module
from config import Settings
from models.librarian_query_plan import (
    LibrarianShortlistItemV1,
    QueryPlanV1,
    normalize_planner_query,
    replay_query_plan_v1,
)
from services.retriever.four_lane_router import FourLaneDocumentRouter
from services.retriever.librarian_planner import (
    LibrarianPlanner,
    QueryPlanReplayCache,
    build_query_plan_v1,
    build_tier0_shortlist,
    corpus_doc_set_version_from_rows,
)
from services.retriever.tier0_router import DocumentRoute


def _version(label: str = "state") -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _shortlist() -> tuple[LibrarianShortlistItemV1, ...]:
    return (
        LibrarianShortlistItemV1(
            corpus_id="corpus",
            doc_id="story",
            title="Alpha Method",
            summary="Narrative directing, story structure, and dramatic beats.",
            score=0.92,
        ),
        LibrarianShortlistItemV1(
            corpus_id="corpus",
            doc_id="camera",
            title="Beta System",
            summary="Camera optics, focal length, and lens characteristics.",
            score=0.84,
        ),
    )


def _plan(query: str, shortlist=None) -> QueryPlanV1:
    return build_query_plan_v1(
        query,
        corpus_id="corpus",
        corpus_doc_version=_version(),
        shortlist=_shortlist() if shortlist is None else shortlist,
        requested_tier="qdrant_mongo_graph",
    )


def test_librarian_flags_ship_default_off():
    settings = Settings()

    assert settings.LIBRARIAN_PLANNER_ENABLED is False
    assert settings.LIBRARIAN_PLANNER_SHADOW is False


def test_schema_hash_and_durable_replay_are_byte_repeatable():
    first = _plan("  What IS narrative-directing? ")
    second = _plan("what is narrative directing")
    replayed = replay_query_plan_v1(first.canonical_bytes())

    assert first.normalized_query == normalize_planner_query(
        "What IS narrative-directing?"
    )
    assert first.plan_hash == second.plan_hash
    assert first.seat_assignment_bytes() == second.seat_assignment_bytes()
    assert replayed == first
    assert replayed.canonical_bytes() == first.canonical_bytes()


@pytest.mark.parametrize(
    ("first_query", "second_query", "expected_shape"),
    [
        (
            "  Compare Narrative-Directing with Camera Optics!!! ",
            "compare narrative directing with camera optics",
            "comparison",
        ),
        (
            " LIST camera changes published in 2004!!! ",
            "list camera changes published in 2004",
            "temporal",
        ),
        (
            " Summarize ALPHA METHOD alongside Beta System. ",
            "summarize alpha method alongside beta system",
            "entity_bridge",
        ),
    ],
)
def test_normalized_variants_have_identical_shape_seats_and_canonical_subqueries(
    first_query,
    second_query,
    expected_shape,
):
    first = _plan(first_query)
    second = _plan(second_query)

    assert first.normalized_query == second.normalized_query
    assert first.shape == second.shape == expected_shape
    assert first.plan_hash == second.plan_hash
    assert first.seat_assignment_bytes() == second.seat_assignment_bytes()
    assert first.subqueries == second.subqueries


def test_replay_rejects_hash_drift():
    payload = _plan("What is narrative directing?").model_dump(mode="json")
    payload["plan_hash"] = _version("tampered")

    with pytest.raises(ValidationError, match="plan_hash"):
        replay_query_plan_v1(payload)


@pytest.mark.parametrize(
    ("query", "shape", "roles"),
    [
        (
            "Compare narrative directing with camera optics in 2004.",
            "comparison",
            ("side_a", "side_b"),
        ),
        (
            "List the camera changes published in 2004.",
            "temporal",
            ("main", "time_slice"),
        ),
        (
            "List the stages of narrative development.",
            "enumerative_trace",
            ("main",),
        ),
        (
            "Summarize Alpha Method alongside Beta System.",
            "entity_bridge",
            ("side_a", "side_b", "hop"),
        ),
        (
            "What is narrative directing?",
            "simple",
            ("main",),
        ),
    ],
)
def test_rule_registry_is_ordered_and_deterministic(query, shape, roles):
    first = _plan(query)
    second = _plan(query)

    assert first.shape == shape
    assert tuple(item.role for item in first.subqueries) == roles
    assert first.plan_hash == second.plan_hash
    assert first.seat_assignment_bytes() == second.seat_assignment_bytes()
    assert sum(item.seat_quota for item in first.subqueries) == 8


def test_simple_plan_preserves_exact_query_and_has_no_behavior_payload():
    query = "  What is Purple Ocean strategy?\n"
    plan = _plan(query)

    assert plan.planner == "rule:simple"
    assert len(plan.subqueries) == 1
    assert plan.subqueries[0].text == query
    assert plan.shortlist == ()
    assert not hasattr(plan, "selected_chunks")
    assert not hasattr(plan, "ranking_scores")


def test_relationship_side_targets_use_shortlist_lexical_affinity():
    plan = _plan("Compare narrative directing with camera optics.")

    assert [item.target_doc_ids for item in plan.subqueries] == [
        ("story",),
        ("camera",),
    ]


def test_named_source_and_empty_shortlist_signals_are_explicit():
    plan = _plan(
        "According to The Missing Manual, what is the launch method?",
        shortlist=(),
    )

    assert plan.refusal_signals.shortlist_empty is True
    assert plan.refusal_signals.named_source_missing is True


def test_named_source_requires_strong_title_match_not_generic_overlap():
    irrelevant = (
        LibrarianShortlistItemV1(
            corpus_id="corpus",
            doc_id="other",
            title="The Story Handbook",
            summary="The launch method is discussed in this summary.",
            score=0.91,
        ),
    )
    matching = (
        LibrarianShortlistItemV1(
            corpus_id="corpus",
            doc_id="manual",
            title="The Missing Manual",
            summary="A different summary.",
            score=0.91,
        ),
    )
    query = "According to The Missing Manual, what is the launch method?"
    substring_only = (
        LibrarianShortlistItemV1(
            corpus_id="corpus",
            doc_id="earth",
            title="Earth Methods",
            summary="Art appears only as a substring of the title.",
            score=0.91,
        ),
    )

    missing = _plan(query, shortlist=irrelevant)
    present = _plan(query, shortlist=matching)
    substring_missing = _plan(
        "According to Art, what is the method?",
        shortlist=substring_only,
    )

    assert missing.refusal_signals.named_source_missing is True
    assert present.refusal_signals.named_source_missing is False
    assert substring_missing.refusal_signals.named_source_missing is True


def test_named_source_exact_short_title_precedes_length_threshold():
    ai_title = (
        LibrarianShortlistItemV1(
            corpus_id="corpus",
            doc_id="ai",
            title="AI",
            summary="Artificial intelligence.",
            score=0.91,
        ),
    )
    unrelated = (
        LibrarianShortlistItemV1(
            corpus_id="corpus",
            doc_id="ml",
            title="ML",
            summary="Machine learning.",
            score=0.91,
        ),
    )
    generic = (
        LibrarianShortlistItemV1(
            corpus_id="corpus",
            doc_id="the",
            title="The",
            summary="Generic token fixture.",
            score=0.91,
        ),
    )

    present = _plan("According to AI, what is the method?", shortlist=ai_title)
    missing = _plan("According to AI, what is the method?", shortlist=unrelated)
    generic_missing = _plan(
        "According to The, what is the method?",
        shortlist=generic,
    )

    assert present.refusal_signals.named_source_missing is False
    assert missing.refusal_signals.named_source_missing is True
    assert generic_missing.refusal_signals.named_source_missing is True


def test_document_set_version_is_order_independent_and_content_sensitive():
    rows = [
        {
            "corpus_id": "c",
            "doc_id": "b",
            "source_identity": {"content_sha256": "b" * 64},
        },
        {
            "corpus_id": "c",
            "doc_id": "a",
            "source_identity": {"content_sha256": "a" * 64},
        },
    ]

    first = corpus_doc_set_version_from_rows(rows, corpus_ids=["c"])
    reordered = corpus_doc_set_version_from_rows(reversed(rows), corpus_ids=["c"])
    changed_rows = [dict(rows[0]), dict(rows[1])]
    changed_rows[1] = {
        **changed_rows[1],
        "source_identity": {"content_sha256": "c" * 64},
    }
    changed = corpus_doc_set_version_from_rows(changed_rows, corpus_ids=["c"])

    assert first == reordered
    assert changed != first


def test_document_set_version_changes_for_same_source_key_new_revision():
    first_rows = [
        {
            "corpus_id": "c",
            "doc_id": "a",
            "source_identity": {"source_key": "stable-source"},
            "source_version_id": "version-1",
            "revision": "revision-1",
        }
    ]
    second_rows = [
        {
            **first_rows[0],
            "source_version_id": "version-2",
            "revision": "revision-2",
        }
    ]

    first = corpus_doc_set_version_from_rows(first_rows, corpus_ids=["c"])
    second = corpus_doc_set_version_from_rows(second_rows, corpus_ids=["c"])

    assert first != second


def test_legacy_document_version_includes_updated_at_with_stable_source_key():
    day_one = [
        {
            "corpus_id": "c",
            "doc_id": "legacy",
            "source_identity": {"source_key": "stable-source"},
            "updated_at": "2026-07-17T00:00:00Z",
        }
    ]
    day_two = [
        {
            **day_one[0],
            "updated_at": "2026-07-18T00:00:00Z",
        }
    ]

    first = corpus_doc_set_version_from_rows(day_one, corpus_ids=["c"])
    second = corpus_doc_set_version_from_rows(day_two, corpus_ids=["c"])

    assert first != second


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows)


class _Documents:
    def __init__(self, rows):
        self.rows = rows
        self.find_calls = []

    def find(self, *args, **kwargs):
        self.find_calls.append((args, kwargs))
        return _Cursor(self.rows)


class _Db:
    def __init__(self, rows):
        self.documents = _Documents(rows)

    def __getitem__(self, name):
        assert name == "documents"
        return self.documents


class _SemanticRouter:
    def __init__(self):
        self.calls = []

    async def route_lanes(self, lane_vectors, corpus_ids, **kwargs):
        self.calls.append(
            {
                "lane_vectors": lane_vectors,
                "corpus_ids": corpus_ids,
                **kwargs,
            }
        )
        return (
            {
                "librarian_shortlist": [
                    DocumentRoute(
                        lane_id="librarian_shortlist",
                        corpus_id="c",
                        doc_id="semantic",
                        score=0.91,
                        title="Semantic Camera",
                        summary="Camera optics.",
                    )
                ]
            },
            {"collection": "polymath_doc_summaries"},
        )


@pytest.mark.asyncio
async def test_shortlist_reuses_four_lane_lexical_and_semantic_mechanisms():
    router = FourLaneDocumentRouter()
    semantic = _SemanticRouter()
    db = _Db(
        [
            {
                "corpus_id": "c",
                "doc_id": "lexical",
                "title": "Narrative Directing",
                "doc_profile": {"summary": "Dramatic beats and story structure."},
            },
            {
                "corpus_id": "c",
                "doc_id": "semantic",
                "title": "Semantic Camera",
                "doc_profile": {"summary": "Camera optics."},
            },
        ]
    )

    routes, diagnostics = await router.route_summary_shortlist(
        query="dramatic narrative directing",
        vector=[0.1, 0.2],
        corpus_ids=["c"],
        db=db,
        semantic_router=semantic,
        max_documents=8,
    )

    assert {route.doc_id for route in routes} == {"lexical", "semantic"}
    assert diagnostics["lanes"] == ["lexical", "semantic"]
    assert diagnostics["parent_summary_vectors"] == 0
    assert semantic.calls[0]["max_per_lane"] == 8
    assert semantic.calls[0]["lane_vectors"] == {"librarian_shortlist": [0.1, 0.2]}
    projection = db.documents.find_calls[0][0][1]
    assert projection == {
        "_id": 0,
        "corpus_id": 1,
        "doc_id": 1,
        "original_filename": 1,
        "filename": 1,
        "title": 1,
        "doc_profile.title": 1,
        "doc_profile.summary": 1,
    }


@pytest.mark.asyncio
async def test_shortlist_embedding_and_routing_use_canonical_query(monkeypatch):
    calls = {}

    async def fake_embed(texts, _config):
        calls["embed_texts"] = texts
        return [[0.1, 0.2]]

    async def fake_route(**kwargs):
        calls["route_query"] = kwargs["query"]
        return [], {"status": "fake"}

    monkeypatch.setattr(planner_module, "embed_queries", fake_embed)
    monkeypatch.setattr(
        planner_module.four_lane_document_router,
        "route_summary_shortlist",
        fake_route,
    )

    shortlist, diagnostics = await build_tier0_shortlist(
        "  Compare Narrative-Directing with Camera Optics!!! ",
        corpus_ids=["c"],
        db=object(),
        embedding_config={"test": True},
    )

    assert shortlist == ()
    assert diagnostics == {"status": "fake"}
    assert calls == {
        "embed_texts": ["compare narrative directing with camera optics"],
        "route_query": "compare narrative directing with camera optics",
    }


@pytest.mark.asyncio
async def test_plan_cache_hits_and_invalidates_on_document_version(monkeypatch):
    planner = LibrarianPlanner(cache=QueryPlanReplayCache())
    current = {"version": _version("one")}
    shortlist_calls = {"count": 0}

    async def fake_version(_db, _corpus_ids):
        return current["version"]

    async def fake_shortlist(*_args, **_kwargs):
        shortlist_calls["count"] += 1
        return _shortlist(), {"status": "fake"}

    monkeypatch.setattr(planner_module, "corpus_doc_set_version", fake_version)
    monkeypatch.setattr(planner_module, "build_tier0_shortlist", fake_shortlist)

    first = await planner.build(
        "Compare narrative directing with camera optics.",
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )
    cached = await planner.build(
        "Compare narrative directing with camera optics.",
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )
    current["version"] = _version("two")
    invalidated = await planner.build(
        "Compare narrative directing with camera optics.",
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )

    assert shortlist_calls["count"] == 2
    assert first.plan.cache.hit is False
    assert cached.plan.cache.hit is True
    assert first.plan.plan_hash == cached.plan.plan_hash
    assert invalidated.plan.plan_hash != first.plan.plan_hash
    assert invalidated.plan.cache.key != first.plan.cache.key


@pytest.mark.asyncio
async def test_simple_shadow_bypasses_shortlist_embedding_and_cache(monkeypatch):
    planner = LibrarianPlanner(cache=QueryPlanReplayCache())
    version_calls = {"count": 0}

    async def fake_version(_db, _corpus_ids):
        version_calls["count"] += 1
        return _version("simple")

    async def forbidden_shortlist(*_args, **_kwargs):
        raise AssertionError("simple shadow must not enter Tier-0")

    monkeypatch.setattr(planner_module, "corpus_doc_set_version", fake_version)
    monkeypatch.setattr(
        planner_module,
        "build_tier0_shortlist",
        forbidden_shortlist,
    )

    first_query = "  What IS narrative-directing? "
    second_query = "what is narrative directing"
    first = await planner.build(
        first_query,
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )
    second = await planner.build(
        second_query,
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )

    assert version_calls["count"] == 2
    assert first.diagnostics["shortlist_calls"] == 0
    assert first.diagnostics["query_embedding_calls"] == 0
    assert first.diagnostics["provider_calls"] == 0
    assert second.diagnostics == first.diagnostics
    assert first.plan.subqueries[0].text == first_query
    assert second.plan.subqueries[0].text == second_query
    assert first.plan.plan_hash == second.plan.plan_hash
    assert first.plan.seat_assignment_bytes() == second.plan.seat_assignment_bytes()


@pytest.mark.asyncio
async def test_bare_conjunction_simple_query_is_zero_tier0_and_raw_exact(monkeypatch):
    planner = LibrarianPlanner(cache=QueryPlanReplayCache())

    async def fake_version(_db, _corpus_ids):
        return _version("simple-conjunction")

    async def forbidden_shortlist(*_args, **_kwargs):
        raise AssertionError("bare 'and' must not trigger entity-bridge grounding")

    monkeypatch.setattr(planner_module, "corpus_doc_set_version", fake_version)
    monkeypatch.setattr(
        planner_module,
        "build_tier0_shortlist",
        forbidden_shortlist,
    )

    raw_query = "  What is narrative directing and why is it useful?  "
    result = await planner.build(
        raw_query,
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )

    assert result.plan.shape == "simple"
    assert result.plan.subqueries[0].text == raw_query
    assert result.plan.cache.hit is False
    assert result.diagnostics["status"] == "simple_bypass"
    assert result.diagnostics["shortlist_calls"] == 0
    assert result.diagnostics["query_embedding_calls"] == 0
    assert result.diagnostics["provider_calls"] == 0


@pytest.mark.asyncio
async def test_grounded_simple_fallback_preserves_raw_and_bypasses_cache(monkeypatch):
    planner = LibrarianPlanner(cache=QueryPlanReplayCache())
    shortlist_calls = {"count": 0}

    async def fake_version(_db, _corpus_ids):
        return _version("grounded-simple")

    async def unrelated_shortlist(*_args, **_kwargs):
        shortlist_calls["count"] += 1
        return _shortlist(), {"status": "fake"}

    monkeypatch.setattr(planner_module, "corpus_doc_set_version", fake_version)
    monkeypatch.setattr(
        planner_module,
        "build_tier0_shortlist",
        unrelated_shortlist,
    )

    first_query = "  Summarize Unknown Craft alongside Missing Practice.  "
    second_query = "summarize unknown craft alongside missing practice"
    first = await planner.build(
        first_query,
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )
    second = await planner.build(
        second_query,
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )

    assert shortlist_calls["count"] == 2
    assert first.plan.shape == second.plan.shape == "simple"
    assert first.plan.subqueries[0].text == first_query
    assert second.plan.subqueries[0].text == second_query
    assert first.plan.cache.hit is second.plan.cache.hit is False
    assert first.diagnostics["status"] == "grounded_simple_bypass"
    assert first.diagnostics["shortlist_calls"] == 1
    assert first.diagnostics["query_embedding_calls"] == 1
    assert first.diagnostics["provider_calls"] == 0


@pytest.mark.asyncio
async def test_cache_first_arrival_does_not_change_normalized_plan(monkeypatch):
    async def fake_version(_db, _corpus_ids):
        return _version("stable")

    async def fake_shortlist(*_args, **_kwargs):
        return _shortlist(), {"status": "fake"}

    monkeypatch.setattr(planner_module, "corpus_doc_set_version", fake_version)
    monkeypatch.setattr(planner_module, "build_tier0_shortlist", fake_shortlist)

    first_planner = LibrarianPlanner(cache=QueryPlanReplayCache())
    second_planner = LibrarianPlanner(cache=QueryPlanReplayCache())
    variants = (
        " Compare Narrative-Directing with Camera Optics!!! ",
        "compare narrative directing with camera optics",
    )
    first_arrival = await first_planner.build(
        variants[0],
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )
    second_arrival = await second_planner.build(
        variants[1],
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )
    first_cached = await first_planner.build(
        variants[1],
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )
    second_cached = await second_planner.build(
        variants[0],
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo",
        db=object(),
        embedding_config=None,
    )

    assert first_arrival.plan.canonical_bytes() == second_arrival.plan.canonical_bytes()
    assert first_cached.plan.subqueries == second_cached.plan.subqueries
    assert (
        first_cached.plan.seat_assignment_bytes()
        == second_cached.plan.seat_assignment_bytes()
    )


def test_l1_l2_planner_has_no_generation_provider_path():
    text = Path(planner_module.__file__).read_text(encoding="utf-8")

    assert "llm_service" not in text
    assert "litellm" not in text
    assert "acompletion" not in text
    assert 'provider_calls": 0' in text
