"""Corpus-scoped LLM hook for document summary trees."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from config import get_settings

LlmFn = Callable[[str], Awaitable[str]]


def summary_tree_llm_from_pool(
    pool: list[dict[str, Any]],
    max_tokens: int,
    *,
    global_max_concurrent: int | None = None,
) -> LlmFn | None:
    """Build a summary-tree LLM hook from the corpus Summary pool."""

    if not pool:
        return None

    settings = get_settings()
    lane_idx = 0
    lane_limits = [max(1, int(entry.get("max_concurrent") or 1)) for entry in pool]
    lane_semaphores = [asyncio.Semaphore(limit) for limit in lane_limits]
    lane_slots = [
        index
        for index, limit in enumerate(lane_limits)
        for _ in range(limit)
    ]
    provider_capacity = max(1, sum(lane_limits))
    effective_global_limit = min(
        provider_capacity,
        max(1, int(global_max_concurrent or provider_capacity)),
    )
    global_semaphore = asyncio.Semaphore(effective_global_limit)

    async def _call(prompt: str) -> str:
        nonlocal lane_idx
        import httpx

        from services.ingestion.extraction_contract import provider_payload_extras

        selected_lane = lane_slots[lane_idx % len(lane_slots)]
        lane_idx += 1
        entry = pool[selected_lane]
        payload: dict[str, Any] = {
            "model": entry["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max(128, min(1024, int(max_tokens or 300))),
        }
        if entry.get("base_url"):
            payload["api_base"] = entry["base_url"]
        if entry.get("api_key"):
            payload["api_key"] = entry["api_key"]
        payload.update(provider_payload_extras(entry.get("extra_params")))
        model_name = str(entry.get("model") or "").lower()
        if "v4-flash" in model_name or "v4-pro" in model_name or "deepseek-v4" in model_name:
            payload.setdefault("thinking", {"type": "disabled"})
        headers = {
            "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
            "Content-Type": "application/json",
        }
        async with global_semaphore, lane_semaphores[selected_lane]:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{settings.LITELLM_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                return str(resp.json()["choices"][0]["message"]["content"] or "").strip()

    return _call
