from __future__ import annotations

import asyncio
import json

import pytest

import services.retriever.librarian_planner as planner_module
import services.retriever.librarian_decomposer as decomposer_module
from models.librarian_query_plan import (
    LibrarianShortlistItemV1,
    librarian_execution_lane_id,
    plan_cache_key_for,
)
from services.chat_cost_meter import chat_cost_scope
from services.chat_orchestrator import _build_retrieval_answerability_gate
from services.llm import LLMService
from services.retriever.librarian_decomposer import (
    LibrarianDecomposer,
    LibrarianRefiner,
    LibrarianSeatedDocument,
    detect_librarian_refinement_gaps,
)
from services.retriever.librarian_planner import (
    LibrarianPlanner,
    QueryPlanReplayCache,
    apply_librarian_execution_plan,
    build_query_plan_v1,
    llm_escalation_eligible,
    planning_requires_shortlist,
)
from services.retriever.query_plan import build_query_plan_v2


QUERY = "What is narrative directing and why is it useful?"
VERSION = "sha256:" + "a" * 64


def _shortlist() -> tuple[LibrarianShortlistItemV1, ...]:
    return (
        LibrarianShortlistItemV1(
            corpus_id="corpus",
            doc_id="story",
            title="Story Craft",
            summary="Narrative directing and dramatic structure.",
            score=0.92,
        ),
        LibrarianShortlistItemV1(
            corpus_id="corpus",
            doc_id="camera",
            title="Camera Craft",
            summary="Camera movement and cinematography.",
            score=0.84,
        ),
    )


def _base_plan():
    plan = build_query_plan_v1(
        QUERY,
        corpus_id="corpus",
        corpus_doc_version=VERSION,
        shortlist=_shortlist(),
        requested_tier="qdrant_mongo_graph",
        allow_llm_escalation=True,
    )
    assert plan.planner == "rule:simple"
    assert plan.shortlist == _shortlist()
    return plan


def _relationship_plan():
    plan = build_query_plan_v1(
        "Compare Story Craft with Camera Craft.",
        corpus_id="corpus",
        corpus_doc_version=VERSION,
        shortlist=_shortlist(),
        requested_tier="qdrant_mongo_graph",
    )
    assert plan.shape == "comparison"
    assert [item.role for item in plan.subqueries] == ["side_a", "side_b"]
    return plan


def _seated_documents():
    return (
        LibrarianSeatedDocument(
            corpus_id="corpus",
            doc_id="story",
            title="Story Craft",
            summary="Narrative directing and dramatic structure.",
            score=0.92,
            lane_ids=(librarian_execution_lane_id(0, "side_a"),),
        ),
        LibrarianSeatedDocument(
            corpus_id="corpus",
            doc_id="camera",
            title="Camera Craft",
            summary="Camera movement and cinematography.",
            score=0.84,
            lane_ids=(librarian_execution_lane_id(1, "side_b"),),
        ),
    )


class _Completion:
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    async def complete_sync(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.response


async def _configured_route(_user_id, kind):
    assert kind == "synthesis"
    return {
        "model": "test/utility",
        "api_base": "https://utility.invalid",
        "api_key": "test-secret-never-traced",
        "extra_params": {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "enable_thinking": True,
            "think": True,
            "top_p": 0.8,
        },
    }


def _success_response() -> str:
    return json.dumps(
        {
            "shape": "complex",
            "subqueries": [
                {
                    "role": "facet",
                    "text": "Why is narrative directing useful?",
                    "target_doc_ids": ["story"],
                },
                {
                    "role": "main",
                    "text": "What is narrative directing?",
                    "target_doc_ids": ["story"],
                },
            ],
        }
    )


def test_llm_eligibility_is_independent_and_opt_in_for_shortlist_work():
    assert llm_escalation_eligible(QUERY) is True
    assert planning_requires_shortlist(QUERY) is False
    assert planning_requires_shortlist(QUERY, allow_llm_escalation=True) is True
    assert llm_escalation_eligible("What is Purple Ocean strategy?") is False


@pytest.mark.asyncio
async def test_success_uses_configured_synthesis_route_and_server_budgets():
    completion = _Completion(_success_response())
    decomposer = LibrarianDecomposer(
        resolver=_configured_route,
        completion_service=completion,
    )

    result = await decomposer.decompose(base_plan=_base_plan(), user_id="user")

    assert result.status == "built"
    assert result.provider_attempts == 1
    assert result.silent_fallback_count == 0
    assert result.plan.planner == "llm:v1"
    assert result.plan.shape == "complex"
    assert [item.role for item in result.plan.subqueries] == ["main", "facet"]
    assert [item.seat_quota for item in result.plan.subqueries] == [5, 3]
    assert sum(item.rerank_cap for item in result.plan.subqueries) == 32
    assert result.plan.refusal_signals.planner_llm_unavailable is False

    call = completion.calls[0]
    assert call["model"] == "test/utility"
    assert call["temperature"] == 0
    assert call["max_tokens"] == 600
    assert call["timeout"] == 12.0
    assert call["response_format"] == {"type": "json_object"}
    assert call["extra_params"] == {
        "top_p": 0.8,
        "disable_thinking": True,
    }
    assert call["api_key"] == "test-secret-never-traced"
    request = json.loads(call["messages"][1]["content"])
    assert set(request) == {"question", "shortlist"}
    assert set(request["shortlist"][0]) == {"doc_id", "title", "summary"}
    assert "test-secret-never-traced" not in json.dumps(result.diagnostics())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_key", "expected_value"),
    [
        ("openai/tencent/Hy3", "enable_thinking", False),
        ("deepseek/deepseek-v4-flash", "thinking", {"type": "disabled"}),
    ],
)
async def test_thinking_is_disabled_on_the_real_llm_wire(
    monkeypatch,
    model,
    expected_key,
    expected_value,
):
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": _success_response()}}]}

    class _Client:
        async def post(self, _url, json, _headers=None, **_kwargs):
            captured.update(json)
            return _Response()

    service = LLMService()

    async def fake_get_client():
        return _Client()

    async def fake_resolve_api_key(_model):
        return None

    async def route(_user_id, _kind):
        return {
            "model": model,
            "extra_params": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
                "enable_thinking": True,
                "think": True,
            },
        }

    monkeypatch.setattr(service, "_get_client", fake_get_client)
    monkeypatch.setattr(service, "_resolve_api_key", fake_resolve_api_key)
    result = await LibrarianDecomposer(
        resolver=route,
        completion_service=service,
    ).decompose(base_plan=_base_plan(), user_id="user")

    assert result.plan.planner == "llm:v1"
    assert captured[expected_key] == expected_value
    assert captured.get("enable_thinking") is not True
    assert captured.get("think") is not True
    assert captured.get("reasoning_effort") != "high"


@pytest.mark.asyncio
async def test_refiner_value_error_still_closes_registered_route_accounting(
    monkeypatch,
):
    plan = _relationship_plan()
    side_a = librarian_execution_lane_id(0, "side_a")
    side_b = librarian_execution_lane_id(1, "side_b")
    gaps = detect_librarian_refinement_gaps(
        plan=plan,
        reservation_receipt={
            "lane_candidates": {
                side_a: {"score_eligible_candidates": 4},
                side_b: {"score_eligible_candidates": 0},
            }
        },
        seated_doc_ids_by_lane={side_a: {"story"}, side_b: set()},
    )
    # A transport-successful but unchanged refinement raises the same strict
    # local ValueError class observed in final acceptance.
    provider_content = json.dumps(
        {
            "subqueries": [
                {
                    "subquery_index": 1,
                    "role": "side_b",
                    "text": plan.subqueries[1].text,
                    "target_doc_ids": list(plan.subqueries[1].target_doc_ids),
                }
            ]
        }
    )
    captured = {}

    class _Response:
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": provider_content}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            }

    class _Client:
        async def post(self, _url, json, **_kwargs):
            captured.update(json)
            return _Response()

    service = LLMService()

    async def fake_get_client():
        return _Client()

    async def explicit_key_must_win(_model):
        raise AssertionError("configured credential must be forwarded")

    resolver_kinds = []

    async def configured_synthesis_route(_user_id, kind):
        resolver_kinds.append(kind)
        if kind != "synthesis":
            raise AssertionError("Utility fallback must not replace synthesis")
        return {
            "entry_id": "deepseek-api__deepseek-v4-flash",
            "model": "deepseek/deepseek-v4-flash",
            "api_base": "https://api.deepseek.com",
            "api_key": "dispatch-only-test-key",
            "extra_params": {"thinking": {"type": "enabled"}},
        }

    monkeypatch.setattr(service, "_get_client", fake_get_client)
    monkeypatch.setattr(service, "_resolve_api_key", explicit_key_must_win)

    with chat_cost_scope() as ledger:
        result = await LibrarianRefiner(
            resolver=configured_synthesis_route,
            completion_service=service,
        ).refine(
            base_plan=plan,
            original_query="Compare Story Craft with Camera Craft.",
            gaps=gaps,
            seated_documents=_seated_documents(),
            user_id="user",
        )
        receipt = ledger.snapshot()

    assert resolver_kinds == ["synthesis"]
    assert captured["model"] == "deepseek/deepseek-v4-flash"
    assert captured["api_base"] == "https://api.deepseek.com"
    assert captured["api_key"] == "dispatch-only-test-key"
    assert captured["thinking"] == {"type": "disabled"}
    assert result.status == "fallback"
    assert result.reason == "planner_refinement_unavailable:ValueError"
    assert result.provider_attempts == 1
    assert receipt["accounting_state"] == "CLOSED"
    assert receipt["unmetered_synthesis_call_count"] == 0
    assert receipt["computed_cost_usd"] == "0.0000196"
    assert receipt["calls"][0]["model"] == "deepseek/deepseek-v4-flash"
    assert receipt["calls"][0]["price"]["route_id"] == (
        "deepseek-api__deepseek-v4-flash"
    )
    assert receipt["calls"][0]["failure_reason"] is None
    assert "dispatch-only-test-key" not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_utility_route_is_only_a_legacy_fallback():
    kinds = []

    async def legacy_route(_user_id, kind):
        kinds.append(kind)
        if kind == "synthesis":
            return None
        assert kind == "utility"
        return {
            "model": "test/legacy-utility",
            "api_base": "https://utility.invalid",
            "api_key": "test-secret-never-traced",
            "extra_params": {},
        }

    completion = _Completion(_success_response())
    result = await LibrarianDecomposer(
        resolver=legacy_route,
        completion_service=completion,
    ).decompose(base_plan=_base_plan(), user_id="user")

    assert result.status == "built"
    assert kinds == ["synthesis", "utility"]
    assert completion.calls[0]["model"] == "test/legacy-utility"


@pytest.mark.asyncio
async def test_model_subquery_order_cannot_change_compiled_plan_bytes():
    forward = json.loads(_success_response())
    reverse = {
        **forward,
        "subqueries": list(reversed(forward["subqueries"])),
    }
    first = await LibrarianDecomposer(
        resolver=_configured_route,
        completion_service=_Completion(json.dumps(forward)),
    ).decompose(base_plan=_base_plan(), user_id="user")
    second = await LibrarianDecomposer(
        resolver=_configured_route,
        completion_service=_Completion(json.dumps(reverse)),
    ).decompose(base_plan=_base_plan(), user_id="user")

    assert first.plan.canonical_bytes() == second.plan.canonical_bytes()
    assert first.plan.seat_assignment_bytes() == second.plan.seat_assignment_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "```json\n" + _success_response() + "\n```",
        _success_response() + "\nextra prose",
        json.dumps(
            {
                "shape": "complex",
                "subqueries": [
                    {
                        "role": "main",
                        "text": "Find evidence.",
                        "target_doc_ids": ["not-shortlisted"],
                    }
                ],
            }
        ),
        json.dumps(
            {
                "shape": "complex",
                "answer": "Narrative directing is useful.",
                "subqueries": [
                    {
                        "role": "main",
                        "text": "Find evidence.",
                        "target_doc_ids": [],
                    }
                ],
            }
        ),
    ],
)
async def test_invalid_or_prose_output_fails_open_without_cacheable_plan(response):
    completion = _Completion(response)
    decomposer = LibrarianDecomposer(
        resolver=_configured_route,
        completion_service=completion,
    )

    result = await decomposer.decompose(base_plan=_base_plan(), user_id="user")

    assert result.plan.planner == "fallback:simple"
    assert result.plan.shape == "simple"
    assert result.plan.subqueries[0].text == QUERY
    assert result.plan.refusal_signals.planner_llm_unavailable is True
    assert result.provider_attempts == 1
    assert result.silent_fallback_count == 1


@pytest.mark.asyncio
async def test_missing_planner_routes_fail_open_without_provider_attempt():
    resolver_calls = 0

    async def missing_route(_user_id, _kind):
        nonlocal resolver_calls
        resolver_calls += 1
        return None

    completion = _Completion(_success_response())
    decomposer = LibrarianDecomposer(
        resolver=missing_route,
        completion_service=completion,
    )

    result = await decomposer.decompose(base_plan=_base_plan(), user_id="user")

    assert resolver_calls == 2
    assert completion.calls == []
    assert result.plan.planner == "fallback:simple"
    assert result.plan.refusal_signals.planner_llm_unavailable is True
    assert result.provider_attempts == 0
    assert result.silent_fallback_count == 1


@pytest.mark.asyncio
async def test_provider_cancellation_returns_counted_validated_fallback():
    started = asyncio.Event()

    class _CancelledCompletion:
        async def complete_sync(self, *_args, **_kwargs):
            started.set()
            await asyncio.sleep(60)

    decomposer = LibrarianDecomposer(
        resolver=_configured_route,
        completion_service=_CancelledCompletion(),
    )
    task = asyncio.create_task(
        decomposer.decompose(base_plan=_base_plan(), user_id="user")
    )
    await started.wait()
    task.cancel()
    result = await task

    assert result.plan.planner == "fallback:simple"
    assert result.plan.refusal_signals.planner_llm_unavailable is True
    assert result.reason == "provider_cancelled"
    assert result.provider_attempts == 1
    assert result.silent_fallback_count == 1


@pytest.mark.asyncio
async def test_success_is_cached_but_failure_is_retried(monkeypatch):
    async def fake_version(_db, _corpus_ids):
        return VERSION

    shortlist_calls = 0

    async def fake_shortlist(*_args, **_kwargs):
        nonlocal shortlist_calls
        shortlist_calls += 1
        return _shortlist(), {"status": "fake"}

    monkeypatch.setattr(planner_module, "corpus_doc_set_version", fake_version)
    monkeypatch.setattr(planner_module, "build_tier0_shortlist", fake_shortlist)

    completion = _Completion(_success_response())
    success_planner = LibrarianPlanner(
        cache=QueryPlanReplayCache(),
        decomposer=LibrarianDecomposer(
            resolver=_configured_route,
            completion_service=completion,
        ),
    )
    first = await success_planner.build(
        QUERY,
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo_graph",
        db=object(),
        embedding_config=None,
        user_id="user",
        llm_decomposer_enabled=True,
    )
    replay = await success_planner.build(
        QUERY,
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo_graph",
        db=object(),
        embedding_config=None,
        user_id="user",
        llm_decomposer_enabled=True,
    )

    assert len(completion.calls) == 1
    assert shortlist_calls == 1
    assert first.plan.planner == replay.plan.planner == "llm:v1"
    assert first.plan.cache.hit is False
    assert replay.plan.cache.hit is True
    assert replay.diagnostics["provider_attempts"] == 0

    missing_calls = 0

    async def missing_route(_user_id, _kind):
        nonlocal missing_calls
        missing_calls += 1
        return None

    failed_planner = LibrarianPlanner(
        cache=QueryPlanReplayCache(),
        decomposer=LibrarianDecomposer(
            resolver=missing_route,
            completion_service=_Completion(_success_response()),
        ),
    )
    for _ in range(2):
        failed = await failed_planner.build(
            QUERY,
            corpus_ids=["corpus"],
            requested_tier="qdrant_mongo_graph",
            db=object(),
            embedding_config=None,
            user_id="user",
            llm_decomposer_enabled=True,
        )
        assert failed.plan.planner == "fallback:simple"
        assert failed.diagnostics["silent_fallback_count"] == 1
    assert missing_calls == 4
    assert shortlist_calls == 3


@pytest.mark.asyncio
async def test_disabling_decomposer_ignores_previously_cached_llm_plan(monkeypatch):
    query = (
        "In the book titled Story Craft, explain how narrative directs "
        "emotion and why that matters?"
    )

    async def fake_version(_db, _corpus_ids):
        return VERSION

    async def fake_shortlist(*_args, **_kwargs):
        return _shortlist(), {"status": "fake"}

    monkeypatch.setattr(planner_module, "corpus_doc_set_version", fake_version)
    monkeypatch.setattr(planner_module, "build_tier0_shortlist", fake_shortlist)
    completion = _Completion(_success_response())
    planner = LibrarianPlanner(
        cache=QueryPlanReplayCache(),
        decomposer=LibrarianDecomposer(
            resolver=_configured_route,
            completion_service=completion,
        ),
    )

    enabled = await planner.build(
        query,
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo_graph",
        db=object(),
        embedding_config=None,
        user_id="user",
        llm_decomposer_enabled=True,
    )
    disabled = await planner.build(
        query,
        corpus_ids=["corpus"],
        requested_tier="qdrant_mongo_graph",
        db=object(),
        embedding_config=None,
        user_id="user",
        llm_decomposer_enabled=False,
    )

    assert enabled.plan.planner == "llm:v1"
    assert disabled.plan.planner == "rule:simple"
    assert disabled.plan.cache.hit is False
    assert disabled.diagnostics["provider_attempts"] == 0
    assert len(completion.calls) == 1


def test_prompt_identity_invalidates_cache_namespace():
    common = {
        "normalized_query": "complex question",
        "corpus_id": "corpus",
        "corpus_doc_version": VERSION,
    }

    first = plan_cache_key_for(
        **common,
        planner_prompt_hash="sha256:" + "1" * 64,
    )
    second = plan_cache_key_for(
        **common,
        planner_prompt_hash="sha256:" + "2" * 64,
    )

    assert first != second


def test_refinement_cache_identity_has_exact_four_inputs(monkeypatch):
    plan = _relationship_plan()
    seated_hash = decomposer_module._seated_identity_hash(_seated_documents())
    baseline = decomposer_module._refinement_cache_key(
        plan=plan,
        seated_document_identity_hash=seated_hash,
    )
    changed_query = build_query_plan_v1(
        "Compare narrative structure with lens emphasis.",
        corpus_id="corpus",
        corpus_doc_version=VERSION,
        shortlist=_shortlist(),
        requested_tier="qdrant_mongo_graph",
    )
    changed_version = build_query_plan_v1(
        "Compare Story Craft with Camera Craft.",
        corpus_id="corpus",
        corpus_doc_version="sha256:" + "b" * 64,
        shortlist=_shortlist(),
        requested_tier="qdrant_mongo_graph",
    )
    changed_seating_hash = decomposer_module._seated_identity_hash(
        tuple(
            item.model_copy(update={"lane_ids": ("different_lane",)})
            for item in _seated_documents()
        )
    )

    assert (
        decomposer_module._refinement_cache_key(
            plan=changed_query,
            seated_document_identity_hash=seated_hash,
        )
        != baseline
    )
    assert (
        decomposer_module._refinement_cache_key(
            plan=changed_version,
            seated_document_identity_hash=seated_hash,
        )
        != baseline
    )
    assert (
        decomposer_module._refinement_cache_key(
            plan=plan,
            seated_document_identity_hash=changed_seating_hash,
        )
        != baseline
    )
    monkeypatch.setattr(
        decomposer_module,
        "LLM_REFINER_PROMPT_HASH",
        "sha256:" + "c" * 64,
    )
    assert (
        decomposer_module._refinement_cache_key(
            plan=plan,
            seated_document_identity_hash=seated_hash,
        )
        != baseline
    )


def test_refinement_gap_detector_is_post_allocation_and_simple_shape_safe():
    plan = _relationship_plan()
    side_a = librarian_execution_lane_id(0, "side_a")
    side_b = librarian_execution_lane_id(1, "side_b")
    gaps = detect_librarian_refinement_gaps(
        plan=plan,
        reservation_receipt={
            "lane_quota_fulfilled": {side_a: 4, side_b: 0},
            "lane_candidates": {
                side_a: {"score_eligible_candidates": 4},
                side_b: {"score_eligible_candidates": 0},
            },
        },
        seated_doc_ids_by_lane={
            side_a: {"story"},
            side_b: set(),
        },
    )

    assert len(gaps) == 1
    assert gaps[0].lane_id == side_b
    assert gaps[0].reasons == (
        "empty_above_admission",
        "required_role_without_seated_document",
        "targeted_shortlist_miss",
    )
    assert (
        detect_librarian_refinement_gaps(
            plan=_base_plan(),
            reservation_receipt={"lane_quota_fulfilled": {}},
            seated_doc_ids_by_lane={},
        )
        == ()
    )


def test_refinement_gap_signals_keep_admission_seating_and_targeting_distinct():
    plan = _relationship_plan()
    side_a = librarian_execution_lane_id(0, "side_a")
    side_b = librarian_execution_lane_id(1, "side_b")

    clean = detect_librarian_refinement_gaps(
        plan=plan,
        reservation_receipt={
            "lane_candidates": {
                side_a: {"score_eligible_candidates": 3},
                side_b: {"score_eligible_candidates": 2},
            }
        },
        seated_doc_ids_by_lane={
            side_a: {"story"},
            side_b: {"camera"},
        },
    )
    unseated = detect_librarian_refinement_gaps(
        plan=plan,
        reservation_receipt={
            "lane_candidates": {
                side_a: {"score_eligible_candidates": 3},
                side_b: {"score_eligible_candidates": 1},
            }
        },
        seated_doc_ids_by_lane={
            side_a: {"story"},
            side_b: set(),
        },
    )
    wrong_target = detect_librarian_refinement_gaps(
        plan=plan,
        reservation_receipt={
            "lane_candidates": {
                side_a: {"score_eligible_candidates": 3},
                side_b: {"score_eligible_candidates": 1},
            }
        },
        seated_doc_ids_by_lane={
            side_a: {"story"},
            side_b: {"story"},
        },
    )

    assert clean == ()
    assert unseated[0].reasons == (
        "required_role_without_seated_document",
        "targeted_shortlist_miss",
    )
    assert wrong_target[0].reasons == ("targeted_shortlist_miss",)


@pytest.mark.asyncio
async def test_refiner_changes_only_gapped_role_and_replays_byte_stably():
    plan = _relationship_plan()
    side_a = librarian_execution_lane_id(0, "side_a")
    side_b = librarian_execution_lane_id(1, "side_b")
    gaps = detect_librarian_refinement_gaps(
        plan=plan,
        reservation_receipt={
            "lane_quota_fulfilled": {side_a: 4, side_b: 0},
            "lane_candidates": {
                side_a: {"score_eligible_candidates": 4},
                side_b: {"score_eligible_candidates": 0},
            },
        },
        seated_doc_ids_by_lane={
            side_a: {"story"},
            side_b: set(),
        },
    )
    completion = _Completion(
        json.dumps(
            {
                "subqueries": [
                    {
                        "subquery_index": 1,
                        "role": "side_b",
                        "text": "How does camera movement shape visual emphasis?",
                        "target_doc_ids": ["camera"],
                    }
                ]
            }
        )
    )
    refiner = LibrarianRefiner(
        resolver=_configured_route,
        completion_service=completion,
    )

    first = await refiner.refine(
        base_plan=plan,
        original_query="Compare Story Craft with Camera Craft.",
        gaps=gaps,
        seated_documents=_seated_documents(),
        user_id="user",
    )
    replay = await refiner.refine(
        base_plan=plan,
        original_query="Compare Story Craft with Camera Craft.",
        gaps=gaps,
        seated_documents=tuple(reversed(_seated_documents())),
        user_id="user",
    )
    reassigned_documents = tuple(
        item.model_copy(
            update={"lane_ids": (librarian_execution_lane_id(0, "side_a"),)}
        )
        if item.doc_id == "camera"
        else item
        for item in _seated_documents()
    )
    reassigned = await refiner.refine(
        base_plan=plan,
        original_query="Compare Story Craft with Camera Craft.",
        gaps=gaps,
        seated_documents=reassigned_documents,
        user_id="user",
    )

    assert first.status == replay.status == "built"
    assert first.cache_hit is False
    assert replay.cache_hit is True
    assert first.provider_attempts == 1
    assert replay.provider_attempts == 0
    assert reassigned.cache_hit is False
    assert len(completion.calls) == 2
    assert first.plan.canonical_bytes() == replay.plan.canonical_bytes()
    assert first.plan.subqueries[0] == plan.subqueries[0]
    assert first.plan.subqueries[1].role == plan.subqueries[1].role
    assert first.plan.subqueries[1].seat_quota == plan.subqueries[1].seat_quota
    assert first.plan.subqueries[1].tier == plan.subqueries[1].tier
    assert first.plan.subqueries[1].rerank_cap == plan.subqueries[1].rerank_cap
    assert first.plan.subqueries[1].text.startswith("How does camera movement")
    assert sum(item.seat_quota for item in first.plan.subqueries) == 8
    call = completion.calls[0]
    assert call["temperature"] == 0
    assert call["max_tokens"] == 600
    assert call["timeout"] == 12.0
    assert call["extra_params"] == {
        "top_p": 0.8,
        "disable_thinking": True,
    }
    request = json.loads(call["messages"][1]["content"])
    assert set(request) == {"gaps", "plan", "question", "seated_documents"}
    assert request["gaps"][0]["lane_id"] == side_b
    assert request["question"] == "Compare Story Craft with Camera Craft."
    assert {item["doc_id"] for item in request["seated_documents"]} == {
        "story",
        "camera",
    }
    assert first.diagnostics()["silent_fallback_count"] == 0
    assert first.diagnostics()["refined_plan"] is not None


@pytest.mark.asyncio
async def test_unchanged_refinement_is_a_counted_uncached_failure():
    plan = _relationship_plan()
    side_a = librarian_execution_lane_id(0, "side_a")
    side_b = librarian_execution_lane_id(1, "side_b")
    gaps = detect_librarian_refinement_gaps(
        plan=plan,
        reservation_receipt={
            "lane_candidates": {
                side_a: {"score_eligible_candidates": 4},
                side_b: {"score_eligible_candidates": 0},
            }
        },
        seated_doc_ids_by_lane={side_a: {"story"}, side_b: set()},
    )
    completion = _Completion(
        json.dumps(
            {
                "subqueries": [
                    {
                        "subquery_index": 1,
                        "role": "side_b",
                        "text": plan.subqueries[1].text,
                        "target_doc_ids": list(plan.subqueries[1].target_doc_ids),
                    }
                ]
            }
        )
    )
    refiner = LibrarianRefiner(
        resolver=_configured_route,
        completion_service=completion,
    )

    for _ in range(2):
        result = await refiner.refine(
            base_plan=plan,
            original_query="Compare Story Craft with Camera Craft.",
            gaps=gaps,
            seated_documents=_seated_documents(),
            user_id="user",
        )
        assert result.status == "fallback"
        assert result.diagnostics()["planner_refinement_unavailable"] is True
        assert result.silent_fallback_count == 1
    assert len(completion.calls) == 2


@pytest.mark.asyncio
async def test_refiner_fail_open_is_counted_and_not_cached():
    plan = _relationship_plan()
    side_a = librarian_execution_lane_id(0, "side_a")
    side_b = librarian_execution_lane_id(1, "side_b")
    gaps = detect_librarian_refinement_gaps(
        plan=plan,
        reservation_receipt={
            "lane_quota_fulfilled": {side_a: 4, side_b: 0},
            "lane_candidates": {
                side_a: {"score_eligible_candidates": 4},
                side_b: {"score_eligible_candidates": 0},
            },
        },
        seated_doc_ids_by_lane={side_a: {"story"}, side_b: set()},
    )
    completion = _Completion(
        json.dumps(
            {
                "subqueries": [
                    {
                        "subquery_index": 0,
                        "role": "side_a",
                        "text": "Rewrite the wrong role.",
                        "target_doc_ids": ["story"],
                    }
                ]
            }
        )
    )
    refiner = LibrarianRefiner(
        resolver=_configured_route,
        completion_service=completion,
    )

    for _ in range(2):
        result = await refiner.refine(
            base_plan=plan,
            original_query="Compare Story Craft with Camera Craft.",
            gaps=gaps,
            seated_documents=_seated_documents(),
            user_id="user",
        )
        assert result.status == "fallback"
        assert result.plan.canonical_bytes() == plan.canonical_bytes()
        assert result.reason.startswith("planner_refinement_unavailable:")
        assert result.provider_attempts == 1
        assert result.silent_fallback_count == 1
        assert result.diagnostics()["refined_plan"] is None
    assert len(completion.calls) == 2


@pytest.mark.asyncio
async def test_clean_shape_refinement_has_zero_added_provider_latency():
    resolver_calls = 0

    async def forbidden_resolver(_user_id, _kind):
        nonlocal resolver_calls
        resolver_calls += 1
        raise AssertionError("clean shape must not resolve a provider")

    result = await LibrarianRefiner(
        resolver=forbidden_resolver,
        completion_service=_Completion("{}"),
    ).refine(
        base_plan=_base_plan(),
        original_query=QUERY,
        gaps=(),
        seated_documents=_seated_documents(),
        user_id="user",
    )

    assert result.status == "not_needed"
    assert result.provider_attempts == 0
    assert result.silent_fallback_count == 0
    assert resolver_calls == 0


@pytest.mark.asyncio
async def test_refined_associative_target_compiles_to_corpus_qualified_route():
    plan = _relationship_plan()
    side_a = librarian_execution_lane_id(0, "side_a")
    side_b = librarian_execution_lane_id(1, "side_b")
    gaps = detect_librarian_refinement_gaps(
        plan=plan,
        reservation_receipt={
            "lane_candidates": {
                side_a: {"score_eligible_candidates": 2},
                side_b: {"score_eligible_candidates": 0},
            }
        },
        seated_doc_ids_by_lane={
            side_a: {"story"},
            side_b: set(),
        },
    )
    associative = LibrarianSeatedDocument(
        corpus_id="corpus",
        doc_id="associative-storytelling",
        title="Associative Storytelling",
        summary="Underlying story craft bridges visual and narrative choices.",
        score=0.77,
        lane_ids=(side_b,),
    )
    result = await LibrarianRefiner(
        resolver=_configured_route,
        completion_service=_Completion(
            json.dumps(
                {
                    "subqueries": [
                        {
                            "subquery_index": 1,
                            "role": "side_b",
                            "text": "Which storytelling craft bridges camera choices?",
                            "target_doc_ids": ["associative-storytelling"],
                        }
                    ]
                }
            )
        ),
    ).refine(
        base_plan=plan,
        original_query="Compare Story Craft with Camera Craft.",
        gaps=gaps,
        seated_documents=(*_seated_documents(), associative),
        user_id="user",
    )

    assert result.status == "built"
    assert all(
        item.doc_id != "associative-storytelling" for item in result.plan.shortlist
    )
    execution, policy = apply_librarian_execution_plan(
        build_query_plan_v2(
            "Compare Story Craft with Camera Craft.",
            corpus_ids=["corpus"],
        ),
        result.plan,
        supplementary_shortlist=(
            LibrarianShortlistItemV1(
                corpus_id=associative.corpus_id,
                doc_id=associative.doc_id,
                title=associative.title,
                summary=associative.summary,
                score=associative.score,
            ),
        ),
    )
    lane = next(item for item in execution.lanes if item.lane_id == side_b)
    assert lane.target_doc_refs == (("corpus", "associative-storytelling"),)
    assert policy.document_route_hints[side_b][0]["doc_id"] == (
        "associative-storytelling"
    )


def test_named_source_signal_is_additive_until_corpus_scope_v3_consumes_it():
    kwargs = {
        "query": "According to Missing Book, what is the method?",
        "diagnostics": {"selection": {"sufficiency": {"answerable": False}}},
        "sources": [],
        "facts": [],
        "corpus_ids": ["corpus"],
        "web_search_enabled": False,
    }
    baseline = _build_retrieval_answerability_gate(**kwargs)
    signaled = _build_retrieval_answerability_gate(
        **kwargs,
        librarian_refusal_signals={
            "named_source_missing": True,
            "shortlist_empty": False,
            "planner_llm_unavailable": False,
        },
    )

    signal_payload = signaled.pop("librarian_refusal_signals")
    baseline.pop("librarian_refusal_signals")
    assert signaled == baseline
    assert signal_payload["named_source_missing"] is True
