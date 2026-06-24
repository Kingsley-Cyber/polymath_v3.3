from __future__ import annotations

import pytest

from services import ghost_a
from services.ghost_a import SummaryTask, summarize_parents


class _BlankSummaryResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": ""}}]}


class _BlankSummaryClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, *args, **kwargs) -> _BlankSummaryResponse:
        return _BlankSummaryResponse()


@pytest.mark.asyncio
async def test_blank_model_content_uses_extractive_summary_fallback(monkeypatch) -> None:
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _BlankSummaryClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_BACKOFF_SECONDS", 0)

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id="parent-1",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text=(
                    "Shopify lets sellers create an online store. "
                    "The tutorial explains account setup, pricing, products, "
                    "themes, and publishing an ecommerce website."
                ),
            )
        ],
        max_summary_tokens=80,
        pool=[
            {
                "model": "unit/blank-model",
                "base_url": None,
                "api_key": None,
                "max_concurrent": 1,
                "extra_params": {},
            }
        ],
        global_max_concurrent=1,
    )

    assert len(results) == 1
    assert results[0].parent_id == "parent-1"
    assert "Shopify lets sellers create an online store" in results[0].summary
    assert results[0].domain == "other"
    assert "shopify" in (results[0].topics or [])
