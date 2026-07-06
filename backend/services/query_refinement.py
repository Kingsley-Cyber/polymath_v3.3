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
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from config import get_settings
from services.llm import llm_service
from services.graph.graph_query import extract_query_entities

_settings = get_settings()

logger = logging.getLogger(__name__)

CACHE_COLLECTION = "query_refinements"
CONTEXT_CACHE_COLLECTION = "query_refinement_context"
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

_CONTEXTUAL_SYSTEM_PROMPT = """You are a corpus-aware query strategist. The user is exploring a graph/RAG knowledge base. You receive the user's draft question plus a tiny context packet from their corpus: matched entities, nearby graph links, and source hints.

Output ONLY valid JSON matching this exact shape:

{
  "rag": ["..."],
  "research": ["..."],
  "nuance": ["..."],
  "ideation": ["..."],
  "gap": ["..."]
}

Rules:
- Return exactly one complete question in each list.
- rag: the best local-corpus retrieval question.
- research: a more evidence-seeking or comparison question.
- nuance: a question that surfaces tensions, limits, contradictions, or edge cases.
- ideation: a question that turns the corpus context into a useful design, product, workflow, or project idea.
- gap: a question that probes what the corpus does NOT yet connect — two concepts that seem related but are never linked, or a bridge that looks fragile.
- Use the corpus context terms when helpful, but do not invent source titles or entities not present in the packet.
- Keep each question under 26 words.
- No preamble, no markdown, no exclamation marks."""


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


def compute_contextual_idempotency_key(
    question: str,
    corpus_ids: list[str],
    model: str | None,
    context_signature: str,
) -> str:
    canonical = json.dumps(
        {
            "q": _normalize_question(question).lower(),
            "c": sorted(set(corpus_ids or [])),
            "m": str(model or ""),
            "context": context_signature,
            "kind": "contextual_questions:v1",
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
    context_coll = db[CONTEXT_CACHE_COLLECTION]
    try:
        await coll.create_index("idempotency_key", unique=True)
        await coll.create_index("expires_at", expireAfterSeconds=0)
        await context_coll.create_index("idempotency_key", unique=True)
        await context_coll.create_index("expires_at", expireAfterSeconds=0)
        logger.debug("query_refinement cache indexes ensured")
    except Exception as exc:
        logger.warning("Failed to ensure query_refinement indexes: %s", exc)


def _as_aware_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
    exp = _as_aware_utc(row.get("expires_at"))
    if exp and exp < datetime.now(timezone.utc):
        # TTL index will collect it; treat as miss for this request.
        return None
    return row.get("result")


async def get_cached_contextual_questions(
    db: AsyncIOMotorDatabase, idempotency_key: str
) -> dict[str, Any] | None:
    row = await db[CONTEXT_CACHE_COLLECTION].find_one(
        {"idempotency_key": idempotency_key},
        {"result": 1, "expires_at": 1, "_id": 0},
    )
    if not row:
        return None
    exp = _as_aware_utc(row.get("expires_at"))
    if exp and exp < datetime.now(timezone.utc):
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


async def _store_cached_contextual_questions(
    db: AsyncIOMotorDatabase,
    idempotency_key: str,
    question: str,
    corpus_ids: list[str],
    model: str | None,
    context_signature: str,
    result: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc)
    doc = {
        "idempotency_key": idempotency_key,
        "question": question,
        "corpus_ids": sorted(set(corpus_ids or [])),
        "model": str(model or ""),
        "context_signature": context_signature,
        "result": result,
        "created_at": now,
        "expires_at": now + timedelta(hours=CACHE_TTL_HOURS),
    }
    try:
        await db[CONTEXT_CACHE_COLLECTION].replace_one(
            {"idempotency_key": idempotency_key}, doc, upsert=True
        )
    except Exception as exc:
        logger.warning("query_refinement contextual cache write failed: %s", exc)


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


def _coerce_contextual_questions(raw: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {
        "rag": [],
        "research": [],
        "nuance": [],
        "ideation": [],
        "gap": [],
    }
    if not isinstance(raw, dict):
        return out
    for key in out.keys():
        val = raw.get(key)
        if isinstance(val, list):
            out[key] = [str(s).strip() for s in val if str(s).strip()][:2]
        elif isinstance(val, str) and val.strip():
            out[key] = [val.strip()]
    return out


_QUESTION_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def _fallback_topic(question: str) -> str:
    words = [
        w
        for w in _QUESTION_WORD_RE.findall(question)
        if w.lower()
        not in {
            "about",
            "between",
            "compare",
            "could",
            "does",
            "from",
            "give",
            "have",
            "into",
            "should",
            "that",
            "their",
            "there",
            "these",
            "this",
            "what",
            "when",
            "where",
            "which",
            "with",
            "would",
        }
    ]
    return " ".join(words[:5]) or _normalize_question(question)[:80] or "this topic"


def _concept_names_from_packet(packet: dict[str, Any], question: str) -> list[str]:
    names: list[str] = []
    for row in packet.get("matched_entities") or []:
        name = str((row or {}).get("name") or "").strip()
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)
        if len(names) >= 4:
            break
    if not names:
        names = [_fallback_topic(question)]
    return names


def _local_contextual_questions(question: str, packet: dict[str, Any]) -> dict[str, list[str]]:
    """Deterministic offline questions for Graph Query #2.

    This is intentionally simple and corpus-shaped: it uses the same concept
    packet as the LLM pass, so offline mode and model-failure mode still give
    the user decent chips without touching the heavy graph synthesis path.
    """
    names = _concept_names_from_packet(packet, question)
    primary = names[0]
    secondary = names[1] if len(names) > 1 else None
    relation_rows = packet.get("nearby_relations") or []
    source_rows = packet.get("source_hints") or []
    source_label = (
        str((source_rows[0] or {}).get("label") or "").strip()
        if source_rows
        else ""
    )

    relation_question = None
    for row in relation_rows:
        seed = str((row or {}).get("seed") or "").strip()
        neighbor = str((row or {}).get("neighbor") or "").strip()
        predicate = str((row or {}).get("predicate") or "relates to").replace("_", " ")
        if seed and neighbor:
            relation_question = (
                f"What does the corpus show about the relationship labelled {predicate} "
                f"between {seed} and {neighbor}?"
            )
            break

    compare_target = secondary or "the closest neighboring concept"
    source_clause = f" in {source_label}" if source_label else " in the corpus"
    normalized_question = _normalize_question(question).rstrip("?")
    rag = [
        f"Which passages most directly answer: {normalized_question}?",
        relation_question
        or f"Which passages best support or complicate {primary}{source_clause}?",
    ]
    research = [
        f"What outside evidence would strengthen or challenge the corpus view of {primary}?",
        f"How does {primary} compare with {compare_target} across reliable external sources?",
    ]
    nuance = [
        f"Where does the corpus create tension between {primary} and {compare_target}?",
        f"What assumptions does the corpus make when it frames {primary} this way?",
    ]
    ideation = [
        f"What could be built by combining {primary} with {compare_target}?",
        f"What workflow, taxonomy, or decision model could organize {primary} for practical use?",
    ]
    gap = [
        f"What connection between {primary} and {compare_target} does the corpus imply but never state?",
        f"Where is the corpus's coverage of {primary} thinnest or held together by a single source?",
    ]
    return {
        "rag": rag[:2],
        "research": research[:2],
        "nuance": nuance[:2],
        "ideation": ideation[:2],
        "gap": gap[:2],
    }


def _merge_contextual_with_local(
    llm_questions: dict[str, list[str]],
    local_questions: dict[str, list[str]],
) -> dict[str, list[str]]:
    merged = _coerce_contextual_questions(llm_questions)
    for key, local_values in local_questions.items():
        values = list(merged.get(key) or [])
        seen = {v.lower() for v in values}
        for value in local_values:
            if value.lower() not in seen:
                values.append(value)
                seen.add(value.lower())
            if len(values) >= 2:
                break
        merged[key] = values[:2]
    return merged


async def _extract_entities_for_question(
    *,
    neo4j_driver: Any,
    question: str,
    corpus_ids: list[str],
) -> list[dict[str, Any]]:
    """Pt 7b: run extract_query_entities across all selected corpora and
    merge by entity_id (sum mention_counts, take max score).

    Pure Cypher per corpus — no LLM, no cache (the corpus state can change
    between calls so results must reflect the live graph). Cheap enough
    that we run it on every /refine call alongside the cached HyDE
    suggestions, so the Graph Query tab gets "what entities are in this
    question's neighborhood?" for free.
    """
    if not neo4j_driver or not question.strip() or not corpus_ids:
        return []
    # Phase 1 hybrid — pull the qdrant client from the running
    # ingestion_service singleton so extract_query_entities can run its
    # vector-scope augmentation (synonym/paraphrase coverage). Local
    # import to avoid pulling the singleton at module load time
    # (refinement runs in worker contexts where the import order matters).
    try:
        from services.ingestion_service import ingestion_service
        qdrant = getattr(ingestion_service, "qdrant_client", None)
    except Exception:
        qdrant = None
    merged: dict[str, dict[str, Any]] = {}
    for cid in corpus_ids:
        try:
            rows = await extract_query_entities(question, cid, neo4j_driver, qdrant=qdrant)
        except Exception as exc:
            logger.warning(
                "extract_query_entities failed for corpus=%s: %s", cid, exc
            )
            continue
        for r in rows:
            eid = str(r.get("entity_id") or "")
            if not eid:
                continue
            cur = merged.get(eid)
            if cur is None:
                merged[eid] = dict(r)
                merged[eid].setdefault("source_corpora", [cid])
            else:
                cur["mention_count"] = (cur.get("mention_count") or 0) + (
                    r.get("mention_count") or 0
                )
                if (r.get("score") or 0) > (cur.get("score") or 0):
                    cur["score"] = r.get("score")
                sc = cur.get("source_corpora") or []
                if cid not in sc:
                    sc.append(cid)
                cur["source_corpora"] = sc
    out = list(merged.values())
    out.sort(key=lambda x: (x.get("score") or 0, x.get("mention_count") or 0), reverse=True)
    return out[:24]


async def _build_context_packet(
    *,
    neo4j_driver: Any,
    corpus_ids: list[str],
    entities: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a tiny deterministic graph context for the slower refine pass.

    This intentionally avoids full retrieval. It is just enough corpus shape
    to help the question-generator name better angles without turning refine
    into a RAG answer call.
    """
    top_entities = [
        {
            "name": str(e.get("display_name") or ""),
            "type": str(e.get("entity_type") or "other"),
            "mentions": int(e.get("mention_count") or 0),
        }
        for e in entities[:10]
        if str(e.get("display_name") or "").strip()
    ]
    packet: dict[str, Any] = {
        "matched_entities": top_entities,
        "nearby_relations": [],
        "source_hints": [],
    }
    if not neo4j_driver or not entities or not corpus_ids:
        return packet

    entity_ids = [str(e.get("entity_id") or "") for e in entities[:10]]
    entity_ids = [e for e in entity_ids if e]
    if not entity_ids:
        return packet

    try:
        async with neo4j_driver.session() as session:
            rel_result = await session.run(
                """
                MATCH (seed:Entity)-[r:RELATES_TO]-(other:Entity)
                WHERE seed.entity_id IN $entity_ids
                  AND any(cid IN $corpus_ids WHERE cid IN coalesce(r.corpus_ids, []))
	                WITH seed, other, r,
	                     coalesce(r.confidence, 0.5) AS confidence,
	                     size(coalesce(r.evidence_chunk_ids, [])) AS evidence_count,
	                     coalesce(r.predicate, 'related_to') AS predicate,
	                     coalesce(r.related_to_query_weight, CASE WHEN coalesce(r.predicate, 'related_to') = 'related_to' THEN 0.5 ELSE 1.0 END) AS query_weight
	                WHERE predicate <> 'related_to' OR evidence_count > 0
	                RETURN
	                    coalesce(seed.display_name, seed.normalized_name, seed.entity_id) AS seed,
	                    coalesce(other.display_name, other.normalized_name, other.entity_id) AS neighbor,
	                    predicate,
	                    coalesce(r.relation_family, '') AS family,
	                    coalesce(r.edge_state, CASE WHEN predicate = 'related_to' THEN 'fallback' ELSE 'typed' END) AS edge_state,
	                    coalesce(r.fallback, predicate = 'related_to') AS fallback,
	                    coalesce(r.fallback_family, '') AS fallback_family,
	                    confidence,
	                    evidence_count
	                ORDER BY confidence * query_weight DESC, evidence_count DESC
	                LIMIT 24
                """,
                entity_ids=entity_ids,
                corpus_ids=corpus_ids,
            )
            packet["nearby_relations"] = [dict(r) async for r in rel_result]

            doc_result = await session.run(
                """
                MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
                WHERE e.entity_id IN $entity_ids
                  AND c.corpus_id IN $corpus_ids
                OPTIONAL MATCH (d:Document {doc_id: c.doc_id, corpus_id: c.corpus_id})
                WITH
                    coalesce(d.filename, d.title, d.source_path, c.doc_id) AS label,
                    count(DISTINCT e) AS matched_entities,
                    count(DISTINCT c) AS chunk_hits
                WHERE label IS NOT NULL AND label <> ''
                RETURN label, matched_entities, chunk_hits
                ORDER BY matched_entities DESC, chunk_hits DESC
                LIMIT 8
                """,
                entity_ids=entity_ids,
                corpus_ids=corpus_ids,
            )
            packet["source_hints"] = [dict(r) async for r in doc_result]
    except Exception as exc:
        logger.warning("query_refinement context packet failed: %s", exc)
    return packet


def _context_signature(packet: dict[str, Any]) -> str:
    compact = json.dumps(packet, sort_keys=True, separators=(",", ":"))[:12000]
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


async def _generate_contextual_questions(
    *,
    db: AsyncIOMotorDatabase,
    question: str,
    corpus_ids: list[str],
    model: str | None,
    api_base: str | None,
    api_key: str | None,
    extra_params: dict[str, Any] | None,
    neo4j_driver: Any,
    entities: list[dict[str, Any]],
    force_refresh: bool,
) -> tuple[dict[str, list[str]], str, bool, str | None, dict[str, Any], str]:
    packet = await _build_context_packet(
        neo4j_driver=neo4j_driver,
        corpus_ids=corpus_ids,
        entities=entities,
    )
    local_questions = _local_contextual_questions(question, packet)
    signature = _context_signature(packet)
    key = compute_contextual_idempotency_key(question, corpus_ids, model, signature)
    if not force_refresh:
        cached = await get_cached_contextual_questions(db, key)
        if cached:
            cached_questions = _merge_contextual_with_local(
                _coerce_contextual_questions(cached),
                local_questions,
            )
            return cached_questions, signature, True, None, packet, "cache"

    user_payload = {
        "question": question,
        "context_packet": packet,
    }
    messages = [
        {"role": "system", "content": _CONTEXTUAL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, indent=2)[:14000],
        },
    ]
    try:
        raw_text = await llm_service.complete_sync(
            messages=messages,
            model=model,
            temperature=0.0,
            max_tokens=650,
            api_base=api_base,
            api_key=api_key,
            extra_params=extra_params,
            timeout=45.0,
        )
    except Exception as exc:
        logger.warning("query_refinement contextual LLM call failed: %s", exc)
        return (
            local_questions,
            signature,
            False,
            f"llm_unavailable: {exc}",
            packet,
            "local_fallback",
        )

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.rsplit("```", 1)[0].strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning(
            "query_refinement contextual LLM returned non-JSON (model=%s): %s",
            model,
            exc,
        )
        return (
            local_questions,
            signature,
            False,
            "llm_returned_non_json",
            packet,
            "local_fallback",
        )

    result = _merge_contextual_with_local(
        _coerce_contextual_questions(parsed),
        local_questions,
    )
    await _store_cached_contextual_questions(
        db,
        key,
        question,
        corpus_ids,
        model,
        signature,
        result,
    )
    return result, signature, False, None, packet, "llm"


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
    neo4j_driver: Any = None,
    include_contextual: bool = False,
) -> dict[str, Any]:
    """Return {idempotency_key, cached, result, entities}.

    Pt 7b: now also calls extract_query_entities (pure Cypher, fast) to
    surface the entities already in the corpus that match the question.
    Entity list is NEVER cached — corpus state changes invalidate it.
    Refinement (LLM call) keeps its 24h Mongo cache.

    Cache flow (refinement only):
      1. Compute idempotency_key from inputs.
      2. If `force_refresh=False`: try Mongo cache hit. On hit → return cached.
      3. On miss: call LLM (temperature=0) with structured JSON prompt.
      4. Parse, coerce, validate.
      5. Write to Mongo (upsert keyed by idempotency_key — concurrent safe).
      6. Return result.

    Entity extraction runs on every call regardless of cache.
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
            "entities": [],
            "error": "empty question",
        }

    key = compute_idempotency_key(normalized, corpus_ids, model)

    # Pt 7b: entity extraction runs on every call (fast Cypher, no cache).
    # Even cache HITS for refinement should re-extract entities so the user
    # sees fresh graph state.
    entities = await _extract_entities_for_question(
        neo4j_driver=neo4j_driver,
        question=normalized,
        corpus_ids=corpus_ids,
    )

    if not force_refresh:
        cached = await get_cached_refinement(db, key)
        if cached:
            response = {
                "idempotency_key": key,
                "cached": True,
                "result": cached,
                "entities": entities,
            }
            if include_contextual:
                (
                    contextual,
                    sig,
                    ctx_cached,
                    ctx_error,
                    concept_packet,
                    ctx_source,
                ) = await _generate_contextual_questions(
                    db=db,
                    question=normalized,
                    corpus_ids=corpus_ids,
                    model=model,
                    api_base=api_base,
                    api_key=api_key,
                    extra_params=extra_params,
                    neo4j_driver=neo4j_driver,
                    entities=entities,
                    force_refresh=force_refresh,
                )
                response["contextual_questions"] = contextual
                response["context_signature"] = sig
                response["contextual_cached"] = ctx_cached
                response["contextual_source"] = ctx_source
                response["concept_packet"] = concept_packet
                if ctx_error:
                    response["contextual_error"] = ctx_error
            return response

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
        response = {
            "idempotency_key": key,
            "cached": False,
            "result": _coerce_suggestions({}),
            "entities": entities,
            "error": f"llm_unavailable: {exc}",
        }
        if include_contextual:
            (
                contextual,
                sig,
                ctx_cached,
                ctx_error,
                concept_packet,
                ctx_source,
            ) = await _generate_contextual_questions(
                db=db,
                question=normalized,
                corpus_ids=corpus_ids,
                model=model,
                api_base=api_base,
                api_key=api_key,
                extra_params=extra_params,
                neo4j_driver=neo4j_driver,
                entities=entities,
                force_refresh=force_refresh,
            )
            response["contextual_questions"] = contextual
            response["context_signature"] = sig
            response["contextual_cached"] = ctx_cached
            response["contextual_source"] = ctx_source
            response["concept_packet"] = concept_packet
            if ctx_error:
                response["contextual_error"] = ctx_error
        return response

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
        response = {
            "idempotency_key": key,
            "cached": False,
            "result": _coerce_suggestions({}),
            "entities": entities,
            "error": "llm_returned_non_json",
        }
        if include_contextual:
            (
                contextual,
                sig,
                ctx_cached,
                ctx_error,
                concept_packet,
                ctx_source,
            ) = await _generate_contextual_questions(
                db=db,
                question=normalized,
                corpus_ids=corpus_ids,
                model=model,
                api_base=api_base,
                api_key=api_key,
                extra_params=extra_params,
                neo4j_driver=neo4j_driver,
                entities=entities,
                force_refresh=force_refresh,
            )
            response["contextual_questions"] = contextual
            response["context_signature"] = sig
            response["contextual_cached"] = ctx_cached
            response["contextual_source"] = ctx_source
            response["concept_packet"] = concept_packet
            if ctx_error:
                response["contextual_error"] = ctx_error
        return response

    result = _coerce_suggestions(parsed)
    await _store_cached_refinement(db, key, normalized, corpus_ids, model, result)
    response = {
        "idempotency_key": key,
        "cached": False,
        "result": result,
        "entities": entities,
    }
    if include_contextual:
        (
            contextual,
            sig,
            ctx_cached,
            ctx_error,
            concept_packet,
            ctx_source,
        ) = await _generate_contextual_questions(
            db=db,
            question=normalized,
            corpus_ids=corpus_ids,
            model=model,
            api_base=api_base,
            api_key=api_key,
            extra_params=extra_params,
            neo4j_driver=neo4j_driver,
            entities=entities,
            force_refresh=force_refresh,
        )
        response["contextual_questions"] = contextual
        response["context_signature"] = sig
        response["contextual_cached"] = ctx_cached
        response["contextual_source"] = ctx_source
        response["concept_packet"] = concept_packet
        if ctx_error:
            response["contextual_error"] = ctx_error
    return response
