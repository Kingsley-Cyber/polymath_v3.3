"""Optional constrained planner for ambiguous corpus vocabulary.

The deterministic QueryPlanV2 remains authoritative. This module may add
non-required translation/step-back probes, but only when every introduced
domain term cites a scoped lexicon entry. Provider calls are disabled unless an
operator configures all three gates: enabled flag, model, and durable budget.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ReturnDocument

from config import get_settings
from services.llm import llm_service
from services.retriever.query_plan import QueryLane, QueryPlanV2
from services.retriever.query_semantics import lexical_terms
from services.retriever.vocabulary import select_strong_vocabulary_matches

PLANNER_VERSION = "grounded_query_planner.v5"
CACHE_COLLECTION = "grounded_query_plans"
USAGE_COLLECTION = "grounded_query_planner_usage"

_SYSTEM_PROMPT = """You are a constrained query planner for a private corpus.
Use only the supplied corpus vocabulary. Do not answer the user's question.
Output only valid JSON with this exact shape:
{
  "intent": "short description",
  "required_obligations": ["complete question"],
  "exploratory_obligations": [
    {"question": "complete question", "lexicon_entry_ids": ["id"]}
  ],
  "step_back_probes": [
    {"question": "broader complete question", "lexicon_entry_ids": ["id"]}
  ],
  "introduced_terms": [
    {"term": "corpus term", "lexicon_entry_id": "id"}
  ],
  "applicability_conditions": ["condition grounded in the supplied glosses"],
  "unresolved_terms": ["term"],
  "dependencies": [{"before": "obligation", "after": "obligation"}],
  "confidence": 0.0
}
Rules:
- Preserve the user's intent and all required obligations.
- Preserve inclusions, exclusions, named phrases, and requested answer shape.
- Every obligation must be a complete standalone question with no unresolved
  pronouns or dependence on another lane's hidden context.
- Use separate obligations only for distinct, non-overlapping evidence needs.
- Record a dependency when a question genuinely requires an earlier result;
  do not pretend dependent work can execute in parallel.
- Every introduced domain term must cite one supplied lexicon_entry_id.
- Exploratory concepts are optional and must not become required claims.
- Use ordinary language freely, but do not invent domain vocabulary.
- Keep intent under 60 characters and every question under 120 characters.
- Return at most 3 required obligations, 1 exploratory obligation, 1 step-back probe,
  4 introduced terms, 3 applicability conditions, 3 unresolved terms, and 2 dependencies.
- Emit minified one-line JSON without indentation or commentary.
"""


def _clip(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _json_object(value: str) -> dict[str, Any] | None:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        parsed = json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


def lexicon_signature(resolution: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for row in sorted(
        resolution.get("matches") or [],
        key=lambda item: str(item.get("lexicon_id") or ""),
    ):
        digest.update(str(row.get("lexicon_id") or "").encode("utf-8"))
        digest.update(str(row.get("schema_version") or "").encode("utf-8"))
        digest.update(str(row.get("retrieval_gloss") or "").encode("utf-8"))
    return digest.hexdigest()


def planner_cache_key(
    query: str,
    corpus_ids: list[str],
    resolution: dict[str, Any],
    model: str,
) -> str:
    payload = json.dumps(
        {
            "version": PLANNER_VERSION,
            "query": " ".join(query.lower().split()),
            "corpus_ids": sorted(set(corpus_ids)),
            "lexicon_signature": lexicon_signature(resolution),
            "model": model,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def should_run_grounded_planner(
    plan: QueryPlanV2,
    resolution: dict[str, Any],
) -> bool:
    if not resolution.get("matches"):
        return False
    required_count = len([probe for probe in plan.probes if probe.required])
    exploratory_match = any(
        row.get("applicability") == "exploratory_semantic"
        for row in resolution.get("matches") or []
    )
    return bool(
        plan.complexity != "simple"
        or required_count > 1
        or exploratory_match
        or resolution.get("rejected_expansions")
    )


def _validate_plan(
    raw: dict[str, Any],
    resolution: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    matches = {
        str(row.get("lexicon_id") or ""): row
        for row in resolution.get("matches") or []
        if row.get("lexicon_id")
    }
    rejected: list[dict[str, str]] = []

    def cited_items(key: str, cap: int) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for item in raw.get(key) or []:
            if not isinstance(item, dict):
                continue
            question = _clip(item.get("question"), 280)
            ids = [
                str(value)
                for value in (item.get("lexicon_entry_ids") or [])
                if str(value) in matches
            ]
            if not question or not ids:
                rejected.append(
                    {
                        "field": key,
                        "reason": "missing_question_or_scoped_lexicon_citation",
                    }
                )
                continue
            output.append(
                {
                    "question": question,
                    "lexicon_entry_ids": list(dict.fromkeys(ids))[:4],
                }
            )
            if len(output) >= cap:
                break
        return output

    introduced: list[dict[str, str]] = []
    for item in raw.get("introduced_terms") or []:
        if not isinstance(item, dict):
            continue
        lexicon_id = str(item.get("lexicon_entry_id") or "")
        term = _clip(item.get("term"), 120)
        match = matches.get(lexicon_id)
        allowed_surfaces = {
            " ".join(str(value).lower().split())
            for value in [
                (match or {}).get("term") or (match or {}).get("canonical_name"),
                *((match or {}).get("aliases") or []),
            ]
            if value
        }
        if (
            not match
            or not term
            or " ".join(term.lower().split()) not in allowed_surfaces
        ):
            rejected.append(
                {
                    "field": "introduced_terms",
                    "reason": "unsupported_domain_term",
                }
            )
            continue
        introduced.append({"term": term, "lexicon_entry_id": lexicon_id})

    required = [
        _clip(value, 280)
        for value in (raw.get("required_obligations") or [])
        if _clip(value, 280)
    ][:3]
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    validated = {
        "version": PLANNER_VERSION,
        "intent": _clip(raw.get("intent"), 240),
        "required_obligations": required,
        "required_obligations_authority": "advisory_deterministic_plan_remains_authoritative",
        "exploratory_obligations": cited_items("exploratory_obligations", 1),
        "step_back_probes": cited_items("step_back_probes", 1),
        "introduced_terms": introduced[:4],
        "applicability_conditions": [
            _clip(value, 240)
            for value in (raw.get("applicability_conditions") or [])
            if _clip(value, 240)
        ][:3],
        "unresolved_terms": [
            _clip(value, 120)
            for value in (raw.get("unresolved_terms") or [])
            if _clip(value, 120)
        ][:3],
        "dependencies": [
            {
                "before": _clip(item.get("before"), 160),
                "after": _clip(item.get("after"), 160),
            }
            for item in (raw.get("dependencies") or [])
            if isinstance(item, dict)
            and _clip(item.get("before"), 160)
            and _clip(item.get("after"), 160)
        ][:2],
        "confidence": confidence,
        "rejected_expansions": rejected,
    }
    if not any(
        [
            validated["required_obligations"],
            validated["exploratory_obligations"],
            validated["step_back_probes"],
        ]
    ):
        return None, rejected
    return validated, rejected


async def _lease_call_budget(db: Any, model: str, maximum: int) -> int | None:
    if maximum <= 0:
        return None
    now = datetime.now(timezone.utc)
    await db[USAGE_COLLECTION].update_one(
        {"_id": "global"},
        {
            "$setOnInsert": {"calls": 0, "created_at": now},
            "$set": {"updated_at": now},
        },
        upsert=True,
    )
    row = await db[USAGE_COLLECTION].find_one_and_update(
        {"_id": "global", "calls": {"$lt": maximum}},
        {"$inc": {"calls": 1}, "$set": {"updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not row:
        return None
    route_id = "route:" + hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]
    await db[USAGE_COLLECTION].update_one(
        {"_id": route_id},
        {
            "$inc": {"calls": 1},
            "$set": {"model": model, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return int(row.get("calls") or 0)


async def run_grounded_planner(
    db: Any,
    *,
    plan: QueryPlanV2,
    resolution: dict[str, Any],
    corpus_ids: list[str],
    route: dict[str, Any] | None = None,
    enabled_override: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    route = route or {}
    model = str(
        route.get("model")
        or getattr(settings, "GROUNDED_QUERY_PLANNER_MODEL", "")
        or ""
    )
    enabled = (
        bool(getattr(settings, "GROUNDED_QUERY_PLANNER_ENABLED", False))
        if enabled_override is None
        else bool(enabled_override)
    )
    maximum = int(getattr(settings, "GROUNDED_QUERY_PLANNER_MAX_CALLS_TOTAL", 0) or 0)
    base = {
        "version": PLANNER_VERSION,
        "status": "skipped",
        "model": model or None,
        "provider_calls": 0,
        "cache_hit": False,
        "lanes": [],
    }
    if not enabled or not model or maximum <= 0:
        base["reason"] = "planner_not_fully_configured"
        return base
    if db is None or (not force and not should_run_grounded_planner(plan, resolution)):
        base["reason"] = "deterministic_plan_sufficient"
        return base

    cache_key = planner_cache_key(plan.standalone_query, corpus_ids, resolution, model)
    now = datetime.now(timezone.utc)
    cached = await db[CACHE_COLLECTION].find_one(
        {"_id": cache_key, "expires_at": {"$gt": now}},
        {"_id": 0, "result": 1},
    )
    if cached and isinstance(cached.get("result"), dict):
        return {
            **base,
            **cached["result"],
            "status": "cache_hit",
            "cache_hit": True,
        }
    total_calls = await _lease_call_budget(db, model, maximum)
    if total_calls is None:
        base["reason"] = "durable_call_budget_exhausted"
        return base

    packet = {
        "query": plan.standalone_query,
        "deterministic_required_obligations": [
            probe.question for probe in plan.probes if probe.required
        ],
        "deterministic_dependencies": [
            {"probe_id": probe.probe_id, "depends_on": list(probe.depends_on)}
            for probe in plan.probes
            if probe.depends_on
        ],
        "constraints": [
            {
                "constraint_id": constraint.constraint_id,
                "operator": constraint.operator,
                "kind": constraint.kind,
                "text": constraint.text,
                "terms": list(constraint.terms),
            }
            for constraint in plan.constraints
        ],
        "corpora": sorted(set(corpus_ids)),
        "lexicon": [
            {
                "lexicon_entry_id": row.get("lexicon_id"),
                "canonical_name": row.get("term") or row.get("canonical_name"),
                "aliases": list(row.get("aliases") or [])[:6],
                "gloss": _clip(row.get("retrieval_gloss"), 420),
                "applications": list(row.get("application_contexts") or [])[:3],
                "components": list(row.get("components") or [])[:3],
                "cooccurrence_neighbors": list(row.get("cooccurrence_neighbors") or [])[
                    :3
                ],
                "source_document_ids": list(row.get("source_document_ids") or [])[:4],
            }
            # Strength-first dossier (P1.2/P1.5): strong matches are admitted
            # regardless of list position, remaining capacity fills by rank.
            for row in select_strong_vocabulary_matches(
                resolution.get("matches") or [], cap=8, fill_by_rank=True
            )
        ],
        "document_profiles": [
            {
                "corpus_id": row.get("corpus_id"),
                "doc_id": row.get("doc_id"),
                "title": _clip(row.get("title"), 240),
                "summary": _clip(row.get("summary"), 450),
                "concepts": list(row.get("concepts") or [])[:10],
            }
            for row in (resolution.get("document_profiles") or [])[:4]
            if isinstance(row, dict)
        ],
        "raptor_ancestors": [
            {
                "corpus_id": row.get("corpus_id"),
                "doc_id": row.get("doc_id"),
                "node_id": row.get("node_id"),
                "node_type": row.get("node_type"),
                "summary": _clip(row.get("summary"), 400),
            }
            for row in (resolution.get("raptor_ancestors") or [])[:6]
            if isinstance(row, dict)
        ],
        "graph_neighbors": [
            {
                "corpus_id": corpus_id,
                "source_entity_id": row.get("source_entity_id"),
                "target_entity_id": row.get("target_entity_id"),
                "target": row.get("target"),
                "predicate": row.get("predicate"),
                "confidence": row.get("confidence"),
            }
            for corpus_id, corpus_data in (resolution.get("per_corpus") or {}).items()
            if isinstance(corpus_data, dict)
            for row in (corpus_data.get("graph_neighbors") or [])[:4]
            if isinstance(row, dict)
        ][:6],
    }
    try:
        raw_text = await llm_service.complete_sync(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(packet, ensure_ascii=True)},
            ],
            model=model,
            temperature=0.0,
            max_tokens=650,
            timeout=float(
                getattr(settings, "GROUNDED_QUERY_PLANNER_TIMEOUT_SECONDS", 8.0)
            ),
            api_base=route.get("api_base"),
            api_key=route.get("api_key"),
            extra_params={
                **dict(route.get("extra_params") or {}),
                "disable_thinking": True,
            },
        )
        parsed = _json_object(raw_text)
        validated, rejected = _validate_plan(parsed or {}, resolution)
        if validated is None:
            stripped = str(raw_text or "").strip()
            return {
                **base,
                "status": "fallback",
                "reason": "malformed_or_unsupported_output",
                "provider_calls": 1,
                "durable_calls_total": total_calls,
                "rejected_expansions": rejected,
                "response_diagnostics": {
                    "characters": len(stripped),
                    "parsed_json_object": parsed is not None,
                    "starts_with_object": stripped.startswith("{"),
                    "ends_with_object": stripped.endswith("}"),
                },
            }
        result = {
            **validated,
            "provider_calls": 1,
            "durable_calls_total": total_calls,
            "cache_hit": False,
        }
        ttl_hours = int(getattr(settings, "GROUNDED_QUERY_PLANNER_CACHE_TTL_HOURS", 24))
        await db[CACHE_COLLECTION].replace_one(
            {"_id": cache_key},
            {
                "_id": cache_key,
                "query_hash": hashlib.sha256(
                    plan.standalone_query.encode("utf-8")
                ).hexdigest(),
                "corpus_ids": sorted(set(corpus_ids)),
                "lexicon_signature": lexicon_signature(resolution),
                "model": model,
                "result": result,
                "created_at": now,
                "expires_at": now + timedelta(hours=ttl_hours),
            },
            upsert=True,
        )
        return {**base, **result, "status": "validated"}
    except Exception as exc:
        return {
            **base,
            "status": "fallback",
            "reason": type(exc).__name__,
            "provider_calls": 1,
            "durable_calls_total": total_calls,
        }


def grounded_planner_lanes(
    result: dict[str, Any],
    resolution: dict[str, Any],
) -> tuple[list[QueryLane], dict[str, list[str]]]:
    matches = {
        str(row.get("lexicon_id") or ""): row
        for row in resolution.get("matches") or []
        if row.get("lexicon_id")
    }
    lanes: list[QueryLane] = []
    lane_lexicon_ids: dict[str, list[str]] = {}

    # The model may decompose the user's wording, but these lanes remain
    # advisory. QueryPlanV2's deterministic obligations and protected original
    # lane stay authoritative for coverage and answerability.
    for index, question_value in enumerate(
        (result.get("required_obligations") or [])[:3]
    ):
        question = _clip(question_value, 280)
        if not question:
            continue
        lane_id = f"planner_decomposition_{index}"
        lanes.append(
            QueryLane(
                lane_id=lane_id,
                role="core",
                query=question,
                dense_text=question,
                lexical_terms=tuple(lexical_terms(question)),
                required=False,
            )
        )
    for role, field, cap in (
        ("translation", "exploratory_obligations", 1),
        ("stepback", "step_back_probes", 1),
    ):
        for index, item in enumerate((result.get(field) or [])[:cap]):
            if not isinstance(item, dict):
                continue
            ids = [
                str(value)
                for value in (item.get("lexicon_entry_ids") or [])
                if str(value) in matches
            ]
            question = _clip(item.get("question"), 280)
            if not ids or not question:
                continue
            support = list(
                dict.fromkeys(
                    str(
                        matches[lexicon_id].get("term")
                        or matches[lexicon_id].get("canonical_name")
                        or ""
                    )
                    for lexicon_id in ids
                )
            )
            lane_id = f"planner_{role}_{index}_{ids[0][:8]}"
            lanes.append(
                QueryLane(
                    lane_id=lane_id,
                    role="core",
                    query=question,
                    dense_text=question,
                    lexical_terms=tuple(lexical_terms(question)),
                    required=False,
                    phrase=support[0] if support else None,
                    support_phrases=tuple(value for value in support if value),
                )
            )
            lane_lexicon_ids[lane_id] = list(dict.fromkeys(ids))[:4]
    selected = lanes[:6]
    return selected, {
        lane.lane_id: lane_lexicon_ids[lane.lane_id]
        for lane in selected
        if lane.lane_id in lane_lexicon_ids
    }


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def filter_aligned_planner_lanes(
    lanes: list[QueryLane],
    vectors: list[list[float] | None],
    lane_lexicon_ids: dict[str, list[str]],
    *,
    original_vector: list[float] | None,
    minimum_alignment: float,
    step_back_minimum_alignment: float,
    protected_lane_ids: set[str] | None = None,
) -> tuple[
    list[QueryLane],
    list[list[float] | None],
    dict[str, list[str]],
    dict[str, Any],
]:
    """Reject generated probes that drift from the protected user query.

    Lexicon citations prove that an expert term exists in the selected corpus;
    they do not prove that the term answers this query. Cosine alignment is the
    second, independent gate. Step-back probes receive a slightly lower floor
    because abstraction intentionally broadens wording, while decomposition and
    translation probes must remain close to the original intent.
    """

    protected_lane_ids = set(protected_lane_ids or ())
    accepted_lanes: list[QueryLane] = []
    accepted_vectors: list[list[float] | None] = []
    accepted_lexicon_ids: dict[str, list[str]] = {}
    rows: list[dict[str, Any]] = []
    for lane, vector in zip(lanes, vectors):
        threshold = (
            step_back_minimum_alignment
            if lane.lane_id.startswith("planner_stepback_")
            else minimum_alignment
        )
        score = (
            _cosine_similarity(original_vector, vector)
            if original_vector is not None and vector is not None
            else 0.0
        )
        protected = lane.lane_id in protected_lane_ids
        accepted = bool(
            vector is not None
            and (protected or (original_vector is not None and score >= threshold))
        )
        rows.append(
            {
                "lane_id": lane.lane_id,
                "role": (
                    "step_back"
                    if lane.lane_id.startswith("planner_stepback_")
                    else "decomposition"
                    if lane.lane_id.startswith("planner_decomposition_")
                    else "translation"
                ),
                "alignment": round(score, 4),
                "threshold": round(threshold, 4),
                "protected_policy_probe": protected,
                "accepted": accepted,
                "reason": (
                    "protected_policy_probe"
                    if protected and accepted
                    else "aligned"
                    if accepted
                    else "missing_vector"
                    if vector is None
                    else "below_semantic_alignment_floor"
                ),
            }
        )
        if not accepted:
            continue
        accepted_lanes.append(lane)
        accepted_vectors.append(vector)
        if lane.lane_id in lane_lexicon_ids:
            accepted_lexicon_ids[lane.lane_id] = lane_lexicon_ids[lane.lane_id]

    return (
        accepted_lanes,
        accepted_vectors,
        accepted_lexicon_ids,
        {
            "original_lane_protected": True,
            "minimum_alignment": minimum_alignment,
            "step_back_minimum_alignment": step_back_minimum_alignment,
            "accepted": len(accepted_lanes),
            "rejected": len(lanes) - len(accepted_lanes),
            "lanes": rows,
        },
    )
