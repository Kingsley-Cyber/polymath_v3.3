"""Corpus-scoped LLM hook for document summary trees."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from config import get_settings
from services.extraction_provider_cards import (
    provider_payload_defaults,
    resolve_extraction_provider_card,
)
from services.structured_provider_lane import prepare_structured_provider_attempt

LlmFn = Callable[[str], Awaitable[str]]

_SUMMARY_TREE_SCHEMA_HINT = (
    "Return one JSON object that parse_semantic_summary can compile: summary, "
    "domain, semantic_chunk_type, key_terms, mechanisms, central_claim, "
    "key_points, main_mechanism, concept_tags, entity_hints, retrieval_uses, "
    "abstraction_level, latent_concepts, temporal_class, and time_expressions."
)


def summary_tree_llm_from_pool(
    pool: list[dict[str, Any]],
    max_tokens: int,
    *,
    global_max_concurrent: int | None = None,
    cost_controller: Any | None = None,
    require_cost_control: bool = False,
) -> LlmFn | None:
    """Build a summary-tree LLM hook from the corpus Summary pool."""

    if not pool:
        return None
    if require_cost_control and cost_controller is None:
        from services.ingestion.summary_cost_control import (
            SummaryCostAuthorityRequired,
        )

        raise SummaryCostAuthorityRequired(
            "summary-tree provider dispatch requires a durable summary cost authority"
        )

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

        from services.ingestion.extraction_contract import (
            ingestion_provider_payload_extras,
        )

        selected_lane = lane_slots[lane_idx % len(lane_slots)]
        lane_idx += 1
        entry = pool[selected_lane]
        card = resolve_extraction_provider_card(entry)
        output_mode = (
            card.schema_mode
            if card.schema_mode in {"json_schema", "json_object", "json_object_prompt"}
            else "json_object_prompt"
        )
        user_prompt, response_format, _structured_attempt = prepare_structured_provider_attempt(
            card,
            output_mode=output_mode,
            contract_name="summary_tree_semantic_heal.v1",
            prompt=prompt,
            schema_hint=_SUMMARY_TREE_SCHEMA_HINT,
            schema_name="summary_tree_semantic_heal",
        )
        payload: dict[str, Any] = {
            "model": entry["model"],
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": 0,
            "max_tokens": max(128, min(1024, int(max_tokens or 300))),
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if entry.get("base_url"):
            payload["api_base"] = entry["base_url"]
        if entry.get("api_key"):
            payload["api_key"] = entry["api_key"]
        payload.update(ingestion_provider_payload_extras(entry.get("extra_params")))
        for key, value in provider_payload_defaults(card).items():
            payload.setdefault(key, value)
        headers = {
            "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
            "Content-Type": "application/json",
        }
        reservation = None
        if cost_controller is not None:
            reservation = await cost_controller.reserve(
                provider=card.provider,
                model=entry.get("model"),
                api_base=entry.get("base_url"),
                messages=payload["messages"],
                max_output_tokens=int(payload["max_tokens"]),
                item_count=1,
            )
        async with global_semaphore, lane_semaphores[selected_lane]:
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(
                        f"{settings.LITELLM_URL}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:
                if reservation is not None:
                    await cost_controller.settle(
                        reservation,
                        usage=None,
                        failure_class=type(exc).__name__,
                    )
                raise
            if reservation is not None:
                await cost_controller.settle(
                    reservation,
                    usage=body.get("usage") if isinstance(body, dict) else None,
                )
            return str(body["choices"][0]["message"]["content"] or "").strip()

    return _call
