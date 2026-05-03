from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services import ghost_a


@pytest.mark.asyncio
async def test_ghost_a_uses_lfm2_rag_parent_context_prompt():
    captured: dict = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "Dense summary."}}]}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["payload"] = json
            captured["headers"] = headers
            return _Response()

    settings = MagicMock(
        SUMMARY_MAX_TOKENS=96,
        SUMMARY_MAX_CONCURRENT=1,
        DEFAULT_COMPLETION_MODEL="LFM2-1.2B-RAG",
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
    )
    task = ghost_a.SummaryTask(
        parent_id="p1",
        doc_id="d1",
        corpus_id="c1",
        text="# Heading\n\nPolymath uses parent chunks for retrieval.",
        source_tier="tier_a",
    )

    with patch.object(ghost_a, "get_settings", return_value=settings), patch.object(
        ghost_a.httpx, "AsyncClient", _Client
    ):
        results = await ghost_a.summarize_parents(
            [task],
            pool=[
                {
                    "model": "LFM2-1.2B-RAG",
                    "base_url": None,
                    "api_key": None,
                    "max_concurrent": 1,
                    "extra_params": {},
                }
            ],
        )

    assert results[0].summary == "Dense summary."
    payload = captured["payload"]
    assert payload["model"] == "LFM2-1.2B-RAG"
    assert payload["temperature"] == 0
    assert payload["max_tokens"] == 96

    system_prompt = payload["messages"][0]["content"]
    user_prompt = payload["messages"][1]["content"]
    assert "book, article, and markdown parent chunks" in system_prompt
    assert "Do not frame the content as a meeting or transcript" in system_prompt
    assert (
        "Use the following context to produce a dense factual parent-chunk summary"
        in user_prompt
    )
    assert "book, article, or markdown document parent chunk" in user_prompt
    assert "not a meeting transcript" in user_prompt
    assert "CONTEXT:" in user_prompt


# ── Token-budget guard ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ghost_a_skips_oversized_parent_without_calling_provider():
    """A parent whose prompt would exceed the model's context window must be
    skipped client-side. The provider must NOT be called — sending a request
    that's guaranteed to 400 wastes a slot, trips the soft-fatal lane-disable
    counter, and burns retry budget.

    This is the bug that killed a 160-parent book ingest in production: 34
    oversized parents each returned 400 from vLLM (12114+175 = 12289 over
    the 12288 limit), Ghost A swallowed the errors, and the worker
    hard-failed the doc."""
    call_count = {"n": 0}

    class _UnusedClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            call_count["n"] += 1
            raise AssertionError(
                "Provider must not be called for over-budget parents"
            )

    settings = MagicMock(
        SUMMARY_MAX_TOKENS=175,
        SUMMARY_MAX_CONCURRENT=1,
        DEFAULT_COMPLETION_MODEL="lfm2-summary",
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
    )
    # Build a parent that's too big for a tight context window. Using ~50k
    # chars (~12.5k tokens via the char/4 fallback estimator) against a
    # context_length of 4096 — definitely infeasible.
    huge_text = "Apple Inc. hired Steve Jobs in 1976. " * 1500
    task = ghost_a.SummaryTask(
        parent_id="p-huge",
        doc_id="d1",
        corpus_id="c1",
        text=huge_text,
        source_tier="tier_a",
    )

    with patch.object(ghost_a, "get_settings", return_value=settings), patch.object(
        ghost_a.httpx, "AsyncClient", _UnusedClient
    ):
        results = await ghost_a.summarize_parents(
            [task],
            pool=[
                {
                    "model": "lfm2-summary",
                    "base_url": None,
                    "api_key": None,
                    "max_concurrent": 1,
                    "extra_params": {},
                    "context_length": 4096,
                }
            ],
        )

    # Phase 21 contract: oversized parents return a skip-marker SummaryResult
    # (not None / empty list) so the worker can persist summary_status="skipped"
    # on parent_chunks[] and avoid wasted retries on resume.
    assert call_count["n"] == 0, "Provider must not have been called"
    assert len(results) == 1
    assert results[0].status == "skipped"
    assert results[0].skip_reason == "token_budget_infeasible"
    assert results[0].summary == ""
    assert results[0].parent_id == "p-huge"


@pytest.mark.asyncio
async def test_ghost_a_uses_pool_context_length_over_registry():
    """When the pool entry advertises a context_length, the budget helper must
    prefer it over the static utils.tokens registry. Local fine-tunes like
    lfm2-summary aren't in the registry — without the override they'd default
    to 4096 and starve their own legitimate 12288-token context."""
    captured_payload: dict = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "Summary."}}]}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured_payload.update(json)
            return _Response()

    settings = MagicMock(
        SUMMARY_MAX_TOKENS=512,
        SUMMARY_MAX_CONCURRENT=1,
        DEFAULT_COMPLETION_MODEL="lfm2-summary",
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
    )
    # ~6k tokens via char/4 — fits in 12288 but NOT in the 4096 registry default.
    medium_text = "Apple Inc. hired Steve Jobs in 1976. " * 700
    task = ghost_a.SummaryTask(
        parent_id="p-medium",
        doc_id="d1",
        corpus_id="c1",
        text=medium_text,
        source_tier="tier_a",
    )

    with patch.object(ghost_a, "get_settings", return_value=settings), patch.object(
        ghost_a.httpx, "AsyncClient", _Client
    ):
        results = await ghost_a.summarize_parents(
            [task],
            pool=[
                {
                    "model": "lfm2-summary",
                    "base_url": None,
                    "api_key": None,
                    "max_concurrent": 1,
                    "extra_params": {},
                    "context_length": 12288,
                }
            ],
        )

    assert len(results) == 1, "Parent fitting in context_length must be summarized"
    assert results[0].summary == "Summary."
    # max_tokens must be capped to the available budget (context − prompt − margin),
    # never exceeding the requested SUMMARY_MAX_TOKENS.
    assert captured_payload["max_tokens"] <= 512
    assert captured_payload["max_tokens"] > 0
