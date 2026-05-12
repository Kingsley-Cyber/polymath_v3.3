"""
Query-refinement helper — Pt 7.

Takes a user's draft question and returns three structured suggestion lists:
  • Alternative phrasings (same intent, different wording)
  • Opposing / contrarian framings (challenges the assumption)
  • Related questions (adjacent angles to explore)

The user picks one from the chips in the Graph Query tab → it's sent to
the chat for RAG retrieval. This is the HyDE-style assistant: instead of
the user re-writing their question themselves, the LLM offers options that
typically retrieve a wider, more diverse set of chunks.

Idempotency contract
====================

Same `(question, corpus_ids, model)` tuple → same result, every time. We
hash the tuple into an `idempotency_key` and cache the LLM response in
MongoDB collection `query_refinements`. Cache hits return immediately
without hitting the LLM. TTL is 24h — the underlying corpus drift on the
scale of weeks, and the refinement output is stable per-question.

This prevents:
  • Duplicate LLM calls when the user double-clicks "Refine"
  • Repeat cost on browser refresh / network retry / parallel sessions
  • Inconsistent suggestion lists between two clicks on the same question

The LLM is called with `temperature=0` so even on a cache miss, the
response is deterministic per (question, model) pair — re-running with
the same inputs yields the same suggestions byte-for-byte.

Cache schema
============
  {
    "_id":              ObjectId (auto),
    "idempotency_key":  str (sha256 of normalized inputs),
    "question":         str (raw),
    "corpus_ids":       list[str] (sorted),
    "model":            str,
    "result":           {alternative_phrasings, opposing_framings, related_questions},
    "created_at":       datetime (UTC),
    "expires_at":       datetime (UTC; created_at + TTL),
  }

Mongo TTL index on `expires_at` evicts stale rows automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from config import get_settings
from services.llm import llm_service

_settings = get_settings()

logger = logging.getLogger(__name__)

CACHE_COLLECTION = "query_refinements"
CACHE_TTL_HOURS = 24

_SYSTEM_PROMPT = """You are a research-assistant query coach. The user is about to ask a question of a knowledge base built from books and papers. Your job is to expand a single draft question into a small structured set of variants the user can pick from. The goal is RAG retrieval diversity — different phrasings retrieve different chunks; opposing angles surface contradictions; related questions reveal adjacent context.

Output ONLY valid JSON matching this exact shape (no preamble, no markdown):

{
  "alternative_phrasings": [
    "..."   // 3-4 reformulations that mean the same thing in different words
  ],
  "opposing_framings": [
    "..."   // 2-3 contrarian / inverted framings of the same topic
  ],
  "related_questions": [
    "..."   // 2-3 adjacent questions that explore neighboring context
  ]
}

Rules:
- Each suggestion is a complete question, phrased naturally, ending in a question mark.
- Stay grounded in the user's topic — don't drift to unrelated subjects.
- "alternative_phrasings" must preserve the user's INTENT (different wording, same goal).
- "opposing_framings" must INVERT or CHALLENGE the user's assumption (e.g. "Why X works?" → "When does X fail?").
- "related_questions" must explore ADJACENT topics the user might benefit from but didn't ask.
- Do not duplicate suggestions across the three lists.
- Total: 7-10 suggestions across the three lists. Quality over quantity."""


class RefinementResult(dict):
    """Convenience wrapper — Pydantic-free dict with known shape."""


def _normalize_question(q: str) -> str:
    return " ".join(q.strip().split())


def compute_idempotency_key(
    question: str, corpus_ids: list[str], model: str | None
) -> str:
    """Deterministic SHA-256 of (normalized question, sorted corpus_ids, model).

    Same inputs → same key, forever. This is what gates duplicate LLM calls.
    """
    canonical = json.dumps(
        {
            "q": _normalize_question(question).lower(),
            "c": sorted(set(corpus_ids or [])),
            "m": str(model or ""),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def ensure_cache_index(db: AsyncIOMotorDatabase) -> None:
    """Create the idempotency_key + TTL indexes if missing.

    Idempotent — safe to call on every backend startup.
    """
    coll = db[CACHE_COLLECTION]
    try:
        await coll.create_index("idempotency_key", unique=True)
        await coll.create_index("expires_at", expireAfterSeconds=0)
        logger.debug("query_refinement cache indexes ensured")
    except Exception as exc:
        logger.warning("Failed to ensure query_refinement indexes: %s", exc)


async def get_cached_refinement(
    db: AsyncIOMotorDatabase, idempotency_key: str
) -> dict[str, Any] | None:
    """Read-through cache lookup. Returns None on miss (or stale row)."""
    row = await db[CACHE_COLLECTION].find_one(
        {"idempotency_key": idempotency_key},
        {"result": 1, "expires_at": 1, "_id": 0},
    )
    if not row:
        return None
    exp = row.get("expires_at")
    if exp and exp < datetime.now(timezone.utc):
        # TTL index will collect it; treat as miss for this request.
        return None
    return row.get("result")


async def _store_cached_refinement(
    db: AsyncIOMotorDatabase,
    idempotency_key: str,
    question: str,
    corpus_ids: list[str],
    model: str | None,
    result: dict[str, Any],
) -> None:
    """Idempotent upsert into the cache. `replace_one(upsert=True)` keyed by
    idempotency_key prevents duplicate rows even under concurrent writers."""
    now = datetime.now(timezone.utc)
    doc = {
        "idempotency_key": idempotency_key,
        "question": question,
        "corpus_ids": sorted(set(corpus_ids or [])),
        "model": str(model or ""),
        "result": result,
        "created_at": now,
        "expires_at": now + timedelta(hours=CACHE_TTL_HOURS),
    }
    try:
        await db[CACHE_COLLECTION].replace_one(
            {"idempotency_key": idempotency_key}, doc, upsert=True
        )
    except Exception as exc:
        logger.warning("query_refinement cache write failed: %s", exc)


def _coerce_suggestions(raw: dict[str, Any]) -> dict[str, list[str]]:
    """Pull the three lists out of the LLM response. Be lenient with formatting
    so a slightly-off response from a smaller model doesn't trigger a retry."""
    out: dict[str, list[str]] = {
        "alternative_phrasings": [],
        "opposing_framings": [],
        "related_questions": [],
    }
    if not isinstance(raw, dict):
        return out
    for key in out.keys():
        val = raw.get(key)
        if isinstance(val, list):
            out[key] = [str(s).strip() for s in val if str(s).strip()]
    return out


async def refine_query(
    *,
    db: AsyncIOMotorDatabase,
    question: str,
    corpus_ids: list[str],
    model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    extra_params: dict[str, Any] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return {idempotency_key, cached: bool, result: {...three lists...}}.

    Cache flow:
      1. Compute idempotency_key from inputs.
      2. If `force_refresh=False`: try Mongo cache hit. On hit → return cached.
      3. On miss: call LLM (temperature=0) with structured JSON prompt.
      4. Parse, coerce, validate.
      5. Write to Mongo (upsert keyed by idempotency_key — concurrent safe).
      6. Return result.
    """
    normalized = _normalize_question(question)
    if not normalized:
        return {
            "idempotency_key": "",
            "cached": False,
            "result": {
                "alternative_phrasings": [],
                "opposing_framings": [],
                "related_questions": [],
            },
            "error": "empty question",
        }

    key = compute_idempotency_key(normalized, corpus_ids, model)

    if not force_refresh:
        cached = await get_cached_refinement(db, key)
        if cached:
            return {"idempotency_key": key, "cached": True, "result": cached}

    # Cache miss → LLM call. temperature=0 so the response is deterministic
    # for the (question, model) pair — even before the cache lands, two
    # parallel callers will get the same suggestions.
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": normalized},
    ]
    # Do NOT pass api_key here — llm_service._get_headers() already attaches
    # the LiteLLM master key as Bearer auth. Body-level api_key is forwarded
    # to the downstream provider (OpenRouter etc.) and master keys aren't
    # valid there, which is what was producing the 401.
    try:
        raw_text = await llm_service.complete_sync(
            messages=messages,
            model=model,
            temperature=0.0,
            max_tokens=900,
            api_base=api_base,
            api_key=api_key,  # None unless caller is overriding downstream
            extra_params=extra_params,
            timeout=45.0,
        )
    except Exception as exc:
        logger.warning("query_refinement LLM call failed: %s", exc)
        return {
            "idempotency_key": key,
            "cached": False,
            "result": _coerce_suggestions({}),
            "error": f"llm_unavailable: {exc}",
        }

    # Parse JSON. Some models wrap with markdown — strip if present.
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        # ```json ... ``` fences
        cleaned = cleaned.split("```", 2)[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.rsplit("```", 1)[0].strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning(
            "query_refinement LLM returned non-JSON (model=%s): %s",
            model,
            exc,
        )
        return {
            "idempotency_key": key,
            "cached": False,
            "result": _coerce_suggestions({}),
            "error": "llm_returned_non_json",
        }

    result = _coerce_suggestions(parsed)
    await _store_cached_refinement(db, key, normalized, corpus_ids, model, result)
    return {"idempotency_key": key, "cached": False, "result": result}
