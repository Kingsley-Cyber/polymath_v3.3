from __future__ import annotations

import asyncio
import json

import pytest

import services.retriever.librarian_planner as planner_module
from models.librarian_query_plan import (
    LibrarianShortlistItemV1,
    plan_cache_key_for,
)
from services.chat_orchestrator import _build_retrieval_answerability_gate
from services.llm import LLMService
from services.retriever.librarian_decomposer import (
    LibrarianDecomposer,
)
from services.retriever.librarian_planner import (
    LibrarianPlanner,
    QueryPlanReplayCache,
    build_query_plan_v1,
    llm_escalation_eligible,
    planning_requires_shortlist,
)


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


class _Completion:
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    async def complete_sync(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.response


async def _utility_route(_user_id, kind):
    assert kind == "utility"
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
async def test_success_uses_exact_bounded_utility_contract_and_server_budgets():
    completion = _Completion(_success_response())
    decomposer = LibrarianDecomposer(
        resolver=_utility_route,
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
    assert call["timeout"] == 2.0
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
async def test_model_subquery_order_cannot_change_compiled_plan_bytes():
    forward = json.loads(_success_response())
    reverse = {
        **forward,
        "subqueries": list(reversed(forward["subqueries"])),
    }
    first = await LibrarianDecomposer(
        resolver=_utility_route,
        completion_service=_Completion(json.dumps(forward)),
    ).decompose(base_plan=_base_plan(), user_id="user")
    second = await LibrarianDecomposer(
        resolver=_utility_route,
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
        resolver=_utility_route,
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
async def test_missing_utility_route_fails_open_without_provider_attempt():
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

    assert resolver_calls == 1
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
        resolver=_utility_route,
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
            resolver=_utility_route,
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
    assert missing_calls == 2
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
            resolver=_utility_route,
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
