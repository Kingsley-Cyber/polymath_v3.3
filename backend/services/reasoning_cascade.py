"""
Phase 24 — Reasoning cascade. Two-stage RAG augmentation: a reasoning model
digests the retrieved chunks before the chat model writes the user-facing
answer.

When `reasoning_cascade=True` on a ChatRequest:
  retrieve → reasoning_cascade.analyze(chunks, query) → chat_orchestrator
  prepends the analysis as an <analysis> context block, then chat model
  generates the final response.

Trade-off: ~20× cost vs a Balanced query. Use sparingly. Best for multi-hop,
cross-source synthesis, or strategic questions. Useless overhead on simple
factual lookups.

Model resolution order for the reasoning step:
  1. settings.REASONING_MODEL (env)
  2. fallback to settings.DEFAULT_COMPLETION_MODEL
"""

import logging

from config import get_settings
from models.schemas import SourceChunk
from services.llm import llm_service

logger = logging.getLogger(__name__)
settings = get_settings()


_PROMPT = (
    "You are an analyst preparing a briefing for another agent who will write the user-facing answer.\n"
    "\n"
    "Below are retrieved passages and the user's question. Produce a tight, structured analysis:\n"
    "  - Key insights (2-5 bullets)\n"
    "  - Cross-source contradictions or tensions, if any\n"
    "  - Gaps the passages do not cover\n"
    "  - The core answer in one sentence\n"
    "\n"
    "Do NOT write the user-facing answer. Do NOT pad. Output only the briefing."
)


async def analyze(
    query: str,
    sources: list[SourceChunk],
    user_id: str | None = None,
    chat_model: str | None = None,
    chat_api_base: str | None = None,
    chat_api_key: str | None = None,
    chat_extra_params: dict | None = None,
) -> str | None:
    """Run the reasoning model over (query, retrieved chunks) → structured briefing.

    Model resolution (Phase 24, fixed):
      1. settings.models.reasoning.pool_entry_id (per-user — Settings → Models)
      2. settings.REASONING_MODEL (env, if explicitly set)
      3. **Active chat model** for this turn (passed in by chat_orchestrator)
      4. None → skip cascade with clear log

    Why chat-model fallback (3) instead of env default: the user's chat
    selection is what they're explicitly running. Falling back to it means
    the cascade uses a model they trust + that's already proven to work.
    Falling back to a hardcoded `DEFAULT_COMPLETION_MODEL` (often a slow
    local Ollama model) silently destroys query latency — see Phase 24 fix.

    Returns None on any failure (graceful fallback — chat_orchestrator skips
    the <analysis> block and generates as if cascade were off).
    """
    if not sources:
        return None

    # Resolution chain.
    model: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    extra_params: dict | None = None
    resolution_source: str = "none"

    # (1) Per-user pool entry (Settings → Models → Reasoning Cascade)
    if user_id:
        try:
            from services.query_model_resolver import resolve as _resolve_query_model

            qres = await _resolve_query_model(user_id, "reasoning")
            if qres:
                model = qres["model"]
                api_base = qres["api_base"]
                api_key = qres["api_key"]
                extra_params = qres["extra_params"] or None
                resolution_source = "user_pool_entry"
        except Exception as exc:
            logger.warning("Reasoning Phase F resolve failed: %s", exc)

    # (2) Env override (deployer-set REASONING_MODEL)
    if not model:
        env_model = (getattr(settings, "REASONING_MODEL", "") or "").strip()
        if env_model:
            model = env_model
            resolution_source = "env_REASONING_MODEL"

    # (3) Fall back to the chat model (the model the user is already running).
    # This is the key fix: never silently degrade to DEFAULT_COMPLETION_MODEL,
    # which is often a slow local Ollama default the user didn't choose.
    if not model and chat_model:
        model = chat_model
        api_base = chat_api_base
        api_key = chat_api_key
        extra_params = chat_extra_params
        resolution_source = "chat_model_fallback"

    # (4) Nothing usable — skip with clear log.
    if not model:
        logger.warning(
            "Reasoning cascade SKIPPED: no model configured. "
            "Set Settings → Models → Reasoning Cascade, or set REASONING_MODEL env, "
            "or ensure a chat model is selected."
        )
        return None

    logger.info(
        "Reasoning cascade resolved: model=%s source=%s",
        model, resolution_source,
    )

    passages = []
    for s in sources:
        attribution = f'from "{s.doc_name or s.doc_id or "doc"}"'
        if s.heading_path:
            attribution += f" §{' / '.join(s.heading_path)}"
        passages.append(f"{attribution}: {s.text}")
    context_block = "\n\n".join(passages)

    user = f"<context>\n{context_block}\n</context>\n\nQuestion: {query}"

    try:
        # Phase 24 — hard 45s wall on the cascade. Reasoning models can
        # legitimately take 20-30s on a tight briefing; 45s is generous but
        # NOT unbounded. If the resolved model is a slow local Ollama, it
        # times out cleanly and the chat continues without the analysis.
        out = await llm_service.complete_sync(
            messages=[
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": user},
            ],
            model=model,
            temperature=0.2,
            max_tokens=1024,
            api_base=api_base,
            api_key=api_key,
            extra_params=extra_params,
            timeout=45.0,
        )
        out = (out or "").strip()
        if not out:
            logger.warning(
                "Reasoning cascade returned empty output (model=%s api_base=%s) "
                "— possible reasoning-only response with no `content`. "
                "Try a non-reasoning model in Settings → Models → Reasoning.",
                model, api_base or "(litellm default)",
            )
            return None
        logger.info(
            "Reasoning cascade complete: model=%s briefing_len=%d",
            model, len(out),
        )
        return out
    except Exception as exc:
        logger.warning(
            "Reasoning cascade failed (model=%s api_base=%s): %s — skipping analysis. "
            "Configure a working entry in Settings → Models → Reasoning.",
            model, api_base or "(litellm default)", exc,
        )
        return None
