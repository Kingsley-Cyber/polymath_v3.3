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
