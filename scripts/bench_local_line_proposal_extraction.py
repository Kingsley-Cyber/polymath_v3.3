#!/usr/bin/env python3
"""Benchmark line-based local Ghost B proposals.

This harness tests the architecture where small/local models do tiny proposal
tasks and Python constructs the final Ghost B object. It does not change
production ingestion. Accepted output still validates against
services.ghost_b_schemas.ExtractionResponse and converts to the existing JSONL
shape.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[0]
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(REPO_ROOT / "backend"))

from bench_local_model_adherence import (  # noqa: E402
    ENTITY_TYPES,
    PREDICATES,
    _empty_checks,
    _empty_counts,
    _is_router_skip,
    _validate,
)
from bench_local_two_stage_extraction import (  # noqa: E402
    JUNK_RE,
    PLACEHOLDER_RE,
    _call_chat,
    _candidate_entity_spans,
    _candidate_quotes,
    _canonical,
    _canonical_supported,
    _clip_confidence,
    _object_to_jsonl,
    _repair_entity_type,
    _repair_predicate,
)
from services.ghost_b_schemas import ExtractionResponse  # noqa: E402


ENTITY_RE = re.compile(
    r"^\s*ENTITY\s+(E\d{3})\s+([A-Za-z_ ]+)(?:\s+(.{0,120}))?\s*$",
    re.I,
)
ENTITY_BARE_RE = re.compile(
    r"^\s*(E\d{3})\s+([A-Za-z_ ]+)(?:\s+(.{0,120}))?\s*$",
    re.I,
)
RELATION_QUOTE_RE = re.compile(r"^\s*(?:RELATION_QUOTE\s+)?(R\d{3})\s*$", re.I)
EDGE_RE = re.compile(
    r"^\s*EDGE\s+(E\d{3})\s+([a-z_]+)\s+(E\d{3}|LITERAL:.+?)\s*$",
    re.I,
)
NONE_RE = re.compile(r"^\s*NONE\s*$", re.I)
EXTRA_JUNK_RE = re.compile(
    r"(?:copyright|all rights reserved|library of congress|cataloging-in-publication|"
    r"registered trademarks?|xlink|xmlns|ebook|isbn|title page|published in)",
    re.I,
)


def _entity_system() -> str:
    return f"""You are a tiny entity classifier for Polymath.
Output only parseable lines. No JSON. No markdown. No prose. No reasoning.

Every output line must match this exact format:
ENTITY E001 Person

Allowed entity types:
{ENTITY_TYPES}

Rules:
- Use only ids shown in the candidate list.
- One entity per line.
- Do not output candidate text.
- Do not output tabs, bullets, explanations, or confidence scores.
- If none are useful, output exactly: NONE
"""


def _relation_quote_system() -> str:
    return """You are a tiny relation-evidence selector for Polymath.
Output only parseable lines. No JSON. No markdown. No prose. No reasoning.

Every output line must match this exact format:
RELATION_QUOTE R001

Rules:
- Use only ids shown in the candidate list.
- One quote per line.
- Do not output candidate text.
- Do not output tabs, bullets, explanations, or confidence scores.
- If none are useful, output exactly: NONE
"""


def _edge_system() -> str:
    return f"""You are a tiny predicate classifier for Polymath.
Output only one parseable line. No JSON. No markdown. No prose. No reasoning.

Allowed predicates:
{PREDICATES}

Output exactly one of:
EDGE E001 created_by E002
EDGE E001 defines LITERAL:short literal text
NONE

Rules:
- Use only entity ids shown in the entity list.
- Use only an allowed predicate.
- Use LITERAL only when the quote clearly states a value not represented by an entity id.
- Do not output entity names, quote text, explanations, or confidence scores.
- If the quote does not clearly support an edge, output exactly: NONE
"""


def _format_entity_candidates(candidates: list[dict[str, str]]) -> str:
    return "\n".join(f"{item['id'].upper()}: {item['quote']}" for item in candidates)


def _format_relation_candidates(candidates: list[dict[str, str]]) -> str:
    return "\n".join(f"{item['id'].upper()}: {item['quote']}" for item in candidates)


def _format_entities_for_edge(entities: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{item['id'].upper()}\t{item['canonical_name']}\t{item['entity_type']}\t{item['surface_form']}"
        for item in entities
    )


def _line_stats(raw: str) -> dict[str, int]:
    lines = [line.strip() for line in str(raw or "").splitlines() if line.strip()]
    return {"raw_lines": len(lines), "valid_lines": 0, "invalid_lines": 0}


def _parse_entity_lines(
    raw: str,
    candidates: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    stats = _line_stats(raw)
    errors: list[str] = []
    by_id = {item["id"].upper(): item for item in candidates}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for line in [line.strip() for line in str(raw or "").splitlines() if line.strip()]:
        if NONE_RE.match(line):
            stats["valid_lines"] += 1
            continue
        match = ENTITY_RE.match(line) or ENTITY_BARE_RE.match(line)
        if not match:
            stats["invalid_lines"] += 1
            errors.append("entity_line_invalid")
            continue
        entity_id = match.group(1).upper()
        candidate = by_id.get(entity_id)
        if not candidate:
            stats["invalid_lines"] += 1
            errors.append("entity_unknown_id")
            continue
        entity_type, repaired = _repair_entity_type(match.group(2))
        if not entity_type or repaired:
            stats["invalid_lines"] += 1
            errors.append("entity_bad_type")
            continue
        span = candidate["quote"]
        canonical = _canonical(span)
        if (
            not span
            or JUNK_RE.search(span)
            or PLACEHOLDER_RE.search(span)
            or not _canonical_supported(canonical, span)
        ):
            stats["invalid_lines"] += 1
            errors.append("entity_bad_span")
            continue
        if entity_id in seen:
            stats["valid_lines"] += 1
            continue
        seen.add(entity_id)
        stats["valid_lines"] += 1
        object_kind = (match.group(3) or "").strip()[:100]
        out.append(
            {
                "id": entity_id,
                "canonical_name": canonical,
                "surface_form": span[:300],
                "entity_type": entity_type,
                "confidence": 0.75,
                "query_aliases": [],
                "definitional_phrase": "",
                "object_kind": object_kind,
            }
        )
    return out, stats, errors


def _parse_relation_quote_lines(
    raw: str,
    candidates: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, int], list[str]]:
    stats = _line_stats(raw)
    errors: list[str] = []
    by_id = {item["id"].upper(): item for item in candidates}
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for line in [line.strip() for line in str(raw or "").splitlines() if line.strip()]:
        if NONE_RE.match(line):
            stats["valid_lines"] += 1
            continue
        match = RELATION_QUOTE_RE.match(line)
        if not match:
            stats["invalid_lines"] += 1
            errors.append("relation_quote_line_invalid")
            continue
        quote_id = match.group(1).upper()
        candidate = by_id.get(quote_id)
        if not candidate:
            stats["invalid_lines"] += 1
            errors.append("relation_quote_unknown_id")
            continue
        if quote_id in seen:
            stats["valid_lines"] += 1
            continue
        seen.add(quote_id)
        stats["valid_lines"] += 1
        out.append({"id": quote_id, "quote": candidate["quote"]})
    return out, stats, errors


def _parse_edge_lines(
    raw: str,
    entity_ids: set[str],
) -> tuple[list[dict[str, str]], dict[str, int], list[str]]:
    stats = _line_stats(raw)
    errors: list[str] = []
    out: list[dict[str, str]] = []
    for line in [line.strip() for line in str(raw or "").splitlines() if line.strip()]:
        if NONE_RE.match(line):
            stats["valid_lines"] += 1
            continue
        match = EDGE_RE.match(line)
        if not match:
            stats["invalid_lines"] += 1
            errors.append("edge_line_invalid")
            continue
        subj = match.group(1).upper()
        pred_raw = match.group(2)
        obj_raw = match.group(3)
        pred, repaired = _repair_predicate(pred_raw)
        if not pred or repaired:
            stats["invalid_lines"] += 1
            errors.append("edge_bad_predicate")
            continue
        if subj not in entity_ids:
            stats["invalid_lines"] += 1
            errors.append("edge_bad_subject")
            continue
        obj = obj_raw.upper() if obj_raw.upper().startswith("E") else obj_raw
        if obj.startswith("E") and obj not in entity_ids:
            stats["invalid_lines"] += 1
            errors.append("edge_bad_object")
            continue
        stats["valid_lines"] += 1
        out.append({"subject_id": subj, "predicate": pred, "object": obj})
    return out[:1], stats, errors


def _merge_line_stats(items: list[dict[str, int]]) -> dict[str, int]:
    merged = {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0}
    for item in items:
        for key in merged:
            merged[key] += int(item.get(key) or 0)
    return merged


def _line_parse_rate(stats: dict[str, int]) -> float | None:
    raw = int(stats.get("raw_lines") or 0)
    if raw == 0:
        return None
    return float(stats.get("valid_lines") or 0) / raw


def _infer_relation_cue(quote: str, predicate: str) -> str:
    quote_lower = quote.lower()
    cues = {
        "created_by": ["co-founded", "founded", "created", "written by", "authored by"],
        "defines": ["defines", "defined as", "means"],
        "uses": ["uses", "use", "used"],
        "references": ["references", "mentions", "quotes"],
        "depends_on": ["depends on", "requires"],
        "causes": ["causes", "drives", "leads to"],
        "supports": ["supports", "enables"],
        "produces": ["produces", "creates", "generates"],
        "part_of": ["part of", "inside", "within"],
    }
    for cue in cues.get(predicate, []):
        if cue in quote_lower:
            return cue[:120]
    return ""


def _build_clean_object(
    entities: list[dict[str, Any]],
    edge_items: list[dict[str, str]],
    relation_quotes_by_id: dict[str, str],
) -> dict[str, Any]:
    id_to_entity = {item["id"]: item for item in entities}
    id_to_name = {item["id"]: item["canonical_name"] for item in entities}
    clean = {
        "entities": [
            {
                "canonical_name": item["canonical_name"],
                "surface_form": item["surface_form"],
                "entity_type": item["entity_type"],
                "confidence": _clip_confidence(item.get("confidence"), 0.75),
                "query_aliases": item.get("query_aliases") or [],
                "definitional_phrase": item.get("definitional_phrase") or "",
                "object_kind": item.get("object_kind") or "",
            }
            for item in entities
            if item["id"] in id_to_entity
        ],
        "relations": [],
        "facts": [],
    }
    seen_relations: set[tuple[str, str, str, str]] = set()
    for item in edge_items:
        quote = relation_quotes_by_id.get(item["quote_id"], "")
        if not quote:
            continue
        if JUNK_RE.search(quote) or EXTRA_JUNK_RE.search(quote) or PLACEHOLDER_RE.search(quote):
            continue
        subject = id_to_name.get(item["subject_id"])
        if not subject:
            continue
        obj_raw = item["object"]
        if obj_raw.startswith("E"):
            obj_value = id_to_name.get(obj_raw)
            object_kind = "entity"
            if not obj_value:
                continue
        else:
            obj_value = obj_raw.removeprefix("LITERAL:").strip()[:200]
            object_kind = "literal"
            if not obj_value:
                continue
        if subject == obj_value:
            continue
        key = (subject, item["predicate"], obj_value, quote)
        if key in seen_relations:
            continue
        seen_relations.add(key)
        clean["relations"].append(
            {
                "subject": subject,
                "predicate": item["predicate"],
                "object": obj_value,
                "object_kind": object_kind,
                "confidence": 0.75,
                "evidence_phrase": quote[:500],
                "relation_cue": _infer_relation_cue(quote, item["predicate"]),
            }
        )
    return clean


def _call_lines(
    *,
    base_url: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    return _call_chat(
        base_url=base_url,
        model=model,
        system=system,
        user=user,
        max_tokens=max_tokens,
        timeout=timeout,
        response_format="none",
        enable_facts=False,
    )


def _with_no_think(user: str, enabled: bool) -> str:
    return user + ("\n/no_think" if enabled else "")


def _empty_result(sample: dict[str, Any], meta: dict[str, Any], *, router_skip: bool) -> dict[str, Any]:
    clean = {"entities": [], "relations": [], "facts": []}
    return {
        "id": sample["id"],
        **meta,
        "router_should_skip": router_skip,
        "router_skipped": router_skip,
        "entity_candidates": 0,
        "relation_candidates": 0,
        "model_calls": {"helper": 0, "qwen": 0},
        "line_stats": {
            "helper_entity": {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0},
            "helper_quote": {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0},
            "helper_edge": {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0},
            "qwen_entity": {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0},
            "qwen_quote": {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0},
            "qwen_edge": {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0},
        },
        "latencies": {},
        "clean_object": clean,
        "jsonl": "{\"t\":\"x\"}",
        "raw_counts": _empty_counts(),
        "accepted": _empty_counts(),
        "dropped": _empty_counts(),
        "checks": _empty_checks(pydantic_ok=True),
        "contract_pass": True,
        "production_useful": True,
        "errors": [],
    }


def run_one(args: argparse.Namespace, sample: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    text = str(sample["text"])
    router_skip = _is_router_skip(meta)
    if router_skip and not args.no_router_skip:
        return _empty_result(sample, meta, router_skip=True)

    result = _empty_result(sample, meta, router_skip=False)
    result["router_should_skip"] = router_skip
    result["router_skipped"] = False
    errors: list[str] = []
    model_calls = {"helper": 0, "qwen": 0}
    latencies: dict[str, float] = {}
    line_stats = result["line_stats"]

    entity_candidates = _candidate_entity_spans(text, max_candidates=args.max_entity_candidates)
    entity_candidates = [
        item for item in entity_candidates if not EXTRA_JUNK_RE.search(item["quote"])
    ]
    relation_candidates = [
        {"id": f"r{idx + 1:03d}", "quote": item["quote"]}
        for idx, item in enumerate(
            [
                item
                for item in _candidate_quotes(
                    text, max_candidates=args.max_relation_candidates
                )
                if not EXTRA_JUNK_RE.search(item["quote"])
            ]
        )
    ]
    result["entity_candidates"] = len(entity_candidates)
    result["relation_candidates"] = len(relation_candidates)

    if not entity_candidates and not relation_candidates:
        return result

    helper_entities: list[dict[str, Any]] = []
    helper_entity_clean_none = False
    if entity_candidates:
        entity_call = _call_lines(
            base_url=args.helper_base_url,
            model=args.helper_model,
            system=_entity_system(),
            user=_with_no_think(
                "ENTITY CANDIDATES:\n"
                + _format_entity_candidates(entity_candidates)
                + "\n\nReturn only ENTITY lines or NONE.",
                args.helper_no_think,
            ),
            max_tokens=args.entity_max_tokens,
            timeout=args.timeout,
        )
        model_calls["helper"] += 1
        latencies["helper_entity_s"] = entity_call["latency_s"]
        helper_entities, stats, parse_errors = _parse_entity_lines(
            entity_call["raw"], entity_candidates
        )
        line_stats["helper_entity"] = stats
        errors.extend(f"helper_entity:{err}" for err in parse_errors[:5])
        entity_truncated = entity_call.get("finish_reason") == "length"
        if entity_truncated:
            errors.append("helper_entity:truncated")
        helper_entity_clean_none = (
            not helper_entities
            and stats.get("raw_lines", 0) > 0
            and stats.get("valid_lines", 0) > 0
            and stats.get("invalid_lines", 0) == 0
            and not entity_truncated
        )

    entities = helper_entities
    if (
        args.qwen_fallback
        and entity_candidates
        and not entities
        and not (args.trust_helper_none and helper_entity_clean_none)
    ):
        qwen_entity_call = _call_lines(
            base_url=args.qwen_base_url,
            model=args.qwen_model,
            system=_entity_system(),
            user="ENTITY CANDIDATES:\n" + _format_entity_candidates(entity_candidates),
            max_tokens=args.entity_max_tokens,
            timeout=args.timeout,
        )
        model_calls["qwen"] += 1
        latencies["qwen_entity_s"] = qwen_entity_call["latency_s"]
        entities, stats, parse_errors = _parse_entity_lines(
            qwen_entity_call["raw"], entity_candidates
        )
        line_stats["qwen_entity"] = stats
        errors.extend(f"qwen_entity:{err}" for err in parse_errors[:5])
        if qwen_entity_call.get("finish_reason") == "length":
            errors.append("qwen_entity:truncated")

    relation_quotes: list[dict[str, str]] = []
    helper_quote_clean_none = False
    if relation_candidates:
        quote_call = _call_lines(
            base_url=args.helper_base_url,
            model=args.helper_model,
            system=_relation_quote_system(),
            user=_with_no_think(
                "RELATION QUOTE CANDIDATES:\n"
                + _format_relation_candidates(relation_candidates)
                + "\n\nReturn only RELATION_QUOTE lines or NONE.",
                args.helper_no_think,
            ),
            max_tokens=args.quote_max_tokens,
            timeout=args.timeout,
        )
        model_calls["helper"] += 1
        latencies["helper_quote_s"] = quote_call["latency_s"]
        relation_quotes, stats, parse_errors = _parse_relation_quote_lines(
            quote_call["raw"], relation_candidates
        )
        line_stats["helper_quote"] = stats
        errors.extend(f"helper_quote:{err}" for err in parse_errors[:5])
        quote_truncated = quote_call.get("finish_reason") == "length"
        if quote_truncated:
            errors.append("helper_quote:truncated")
        helper_quote_clean_none = (
            not relation_quotes
            and stats.get("raw_lines", 0) > 0
            and stats.get("valid_lines", 0) > 0
            and stats.get("invalid_lines", 0) == 0
            and not quote_truncated
        )

    if (
        args.qwen_fallback
        and relation_candidates
        and not relation_quotes
        and not (args.trust_helper_none and helper_quote_clean_none)
    ):
        qwen_quote_call = _call_lines(
            base_url=args.qwen_base_url,
            model=args.qwen_model,
            system=_relation_quote_system(),
            user="RELATION QUOTE CANDIDATES:\n" + _format_relation_candidates(relation_candidates),
            max_tokens=args.quote_max_tokens,
            timeout=args.timeout,
        )
        model_calls["qwen"] += 1
        latencies["qwen_quote_s"] = qwen_quote_call["latency_s"]
        relation_quotes, stats, parse_errors = _parse_relation_quote_lines(
            qwen_quote_call["raw"], relation_candidates
        )
        line_stats["qwen_quote"] = stats
        errors.extend(f"qwen_quote:{err}" for err in parse_errors[:5])
        if qwen_quote_call.get("finish_reason") == "length":
            errors.append("qwen_quote:truncated")

    edge_items: list[dict[str, str]] = []
    edge_stats: list[dict[str, int]] = []
    qwen_edge_stats: list[dict[str, int]] = []
    entity_ids = {item["id"] for item in entities}
    relation_quotes_by_id = {item["id"]: item["quote"] for item in relation_quotes}
    entity_context = _format_entities_for_edge(entities)
    for quote in relation_quotes[: args.max_edge_quotes]:
        if not entities:
            break
        edge_user = (
            f"QUOTE {quote['id'].upper()}: {quote['quote']}\n\n"
            f"ENTITIES:\n{entity_context}"
            "\n\nReturn only one EDGE line or NONE."
        )
        edge_call = _call_lines(
            base_url=args.helper_base_url,
            model=args.helper_model,
            system=_edge_system(),
            user=_with_no_think(edge_user, args.helper_no_think),
            max_tokens=args.edge_max_tokens,
            timeout=args.timeout,
        )
        model_calls["helper"] += 1
        latencies["helper_edge_total_s"] = latencies.get("helper_edge_total_s", 0.0) + edge_call["latency_s"]
        edges, stats, parse_errors = _parse_edge_lines(edge_call["raw"], entity_ids)
        edge_stats.append(stats)
        errors.extend(f"helper_edge:{err}" for err in parse_errors[:3])
        edge_truncated = edge_call.get("finish_reason") == "length"
        if edge_truncated:
            errors.append("helper_edge:truncated")
        helper_edge_clean_none = (
            not edges
            and stats.get("raw_lines", 0) > 0
            and stats.get("valid_lines", 0) > 0
            and stats.get("invalid_lines", 0) == 0
            and not edge_truncated
        )
        if (
            not edges
            and args.qwen_fallback
            and not (args.trust_helper_none and helper_edge_clean_none)
        ):
            qwen_edge_call = _call_lines(
                base_url=args.qwen_base_url,
                model=args.qwen_model,
                system=_edge_system(),
                user=edge_user,
                max_tokens=args.edge_max_tokens,
                timeout=args.timeout,
            )
            model_calls["qwen"] += 1
            latencies["qwen_edge_total_s"] = latencies.get("qwen_edge_total_s", 0.0) + qwen_edge_call["latency_s"]
            edges, qwen_stats, qwen_parse_errors = _parse_edge_lines(
                qwen_edge_call["raw"], entity_ids
            )
            qwen_edge_stats.append(qwen_stats)
            errors.extend(f"qwen_edge:{err}" for err in qwen_parse_errors[:3])
            if qwen_edge_call.get("finish_reason") == "length":
                errors.append("qwen_edge:truncated")
        for edge in edges:
            edge_items.append({"quote_id": quote["id"], **edge})

    line_stats["helper_edge"] = _merge_line_stats(edge_stats)
    line_stats["qwen_edge"] = _merge_line_stats(qwen_edge_stats)

    clean = _build_clean_object(entities, edge_items, relation_quotes_by_id)
    validation_errors, raw_counts, accepted, dropped, checks = _validate(
        clean,
        text,
        enable_facts=False,
    )
    try:
        ExtractionResponse.model_validate(clean)
        jsonl = _object_to_jsonl(clean)
    except Exception as exc:
        validation_errors.append(f"pydantic_final:{type(exc).__name__}:{str(exc)[:160]}")
        jsonl = "{\"t\":\"x\"}"

    result.update(
        {
            "model_calls": model_calls,
            "latencies": latencies,
            "line_stats": line_stats,
            "clean_object": clean,
            "jsonl": jsonl,
            "raw_counts": raw_counts,
            "accepted": accepted,
            "dropped": dropped,
            "checks": checks,
            "contract_pass": not validation_errors,
            "production_useful": not validation_errors,
            "errors": errors + validation_errors,
        }
    )
    return result


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * pct))]


def summarize(args: argparse.Namespace, results: list[dict[str, Any]], wall_s: float) -> dict[str, Any]:
    samples = len(results)
    router_skipped = sum(1 for item in results if item.get("router_skipped"))
    body_samples = samples - router_skipped
    accepted_totals = {
        key: sum(int((item.get("accepted") or {}).get(key) or 0) for item in results)
        for key in ("entities", "relations", "facts")
    }
    raw_totals = {
        key: sum(int((item.get("raw_counts") or {}).get(key) or 0) for item in results)
        for key in ("entities", "relations", "facts")
    }
    helper_calls = sum(int((item.get("model_calls") or {}).get("helper") or 0) for item in results)
    qwen_calls = sum(int((item.get("model_calls") or {}).get("qwen") or 0) for item in results)
    baseline_qwen_calls = args.baseline_qwen_calls or max(0, body_samples * 2)
    qwen_saved = (
        (baseline_qwen_calls - qwen_calls) / baseline_qwen_calls
        if baseline_qwen_calls
        else None
    )

    merged_stats: dict[str, dict[str, int]] = {}
    for name in (
        "helper_entity",
        "helper_quote",
        "helper_edge",
        "qwen_entity",
        "qwen_quote",
        "qwen_edge",
    ):
        merged_stats[name] = _merge_line_stats(
            [(item.get("line_stats") or {}).get(name) or {} for item in results]
        )

    latencies_by_name: dict[str, list[float]] = {}
    for item in results:
        for key, value in (item.get("latencies") or {}).items():
            if isinstance(value, (float, int)):
                latencies_by_name.setdefault(key, []).append(float(value))

    contract_pass = sum(1 for item in results if item.get("contract_pass"))
    evidence_errors = sum(int((item.get("checks") or {}).get("evidence_errors") or 0) for item in results)
    invalid_enum_errors = sum(
        int((item.get("checks") or {}).get("invalid_enum_errors") or 0)
        for item in results
    )
    truncations = sum(
        1
        for item in results
        for err in (item.get("errors") or [])
        if "truncated" in str(err)
    )
    gate_failures: list[str] = []
    if contract_pass != samples:
        gate_failures.append("contract_pass_not_all_samples")
    if evidence_errors:
        gate_failures.append("evidence_errors_nonzero")
    if invalid_enum_errors:
        gate_failures.append("invalid_enum_errors_nonzero")
    if truncations:
        gate_failures.append("truncations_nonzero")
    if accepted_totals["entities"] + accepted_totals["relations"] == 0:
        gate_failures.append("accepted_output_zero")
    if accepted_totals["relations"] == 0:
        gate_failures.append("accepted_relations_zero")
    if qwen_saved is not None and qwen_saved <= 0:
        gate_failures.append("no_qwen_call_savings")

    return {
        "helper_model": args.helper_model,
        "qwen_model": args.qwen_model,
        "samples": samples,
        "router_skipped": router_skipped,
        "body_samples": body_samples,
        "facts_enabled": False,
        "wall_s": wall_s,
        "chunks_per_hour_wall": samples / wall_s * 3600 if wall_s else None,
        "accepted_entities_per_hour": accepted_totals["entities"] / wall_s * 3600 if wall_s else None,
        "accepted_relations_per_hour": accepted_totals["relations"] / wall_s * 3600 if wall_s else None,
        "contract_pass": contract_pass,
        "contract_pass_rate": contract_pass / samples if samples else 0.0,
        "evidence_errors": evidence_errors,
        "invalid_enum_errors": invalid_enum_errors,
        "truncation_count": truncations,
        "raw_totals": raw_totals,
        "accepted_totals": accepted_totals,
        "helper_calls": helper_calls,
        "qwen_calls": qwen_calls,
        "baseline_qwen_calls": baseline_qwen_calls,
        "qwen_calls_saved_percent": qwen_saved,
        "line_stats": merged_stats,
        "line_parse_rates": {
            name: _line_parse_rate(stats) for name, stats in merged_stats.items()
        },
        "latency_p50_s": {
            name: statistics.median(values) if values else None
            for name, values in latencies_by_name.items()
        },
        "latency_p95_s": {
            name: _percentile(values, 0.95)
            for name, values in latencies_by_name.items()
        },
        "eligible_for_ghost_b": not gate_failures,
        "gate_failures": gate_failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper-base-url", required=True)
    parser.add_argument("--qwen-base-url", required=True)
    parser.add_argument("--helper-model", required=True)
    parser.add_argument("--qwen-model", required=True)
    parser.add_argument("--samples", default="/tmp/zto_chunk_samples_balanced.jsonl")
    parser.add_argument("--meta", default="/tmp/zto_chunk_samples_meta.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-entity-candidates", type=int, default=18)
    parser.add_argument("--max-relation-candidates", type=int, default=18)
    parser.add_argument("--max-edge-quotes", type=int, default=6)
    parser.add_argument("--entity-max-tokens", type=int, default=160)
    parser.add_argument("--quote-max-tokens", type=int, default=120)
    parser.add_argument("--edge-max-tokens", type=int, default=80)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--baseline-qwen-calls", type=int, default=0)
    parser.add_argument("--no-qwen-fallback", dest="qwen_fallback", action="store_false")
    parser.add_argument("--helper-no-think", action="store_true")
    parser.add_argument("--trust-helper-none", action="store_true")
    parser.add_argument("--no-router-skip", action="store_true")
    parser.set_defaults(qwen_fallback=True)
    args = parser.parse_args()

    samples = [
        json.loads(line)
        for line in Path(args.samples).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta_by_id: dict[str, Any] = {}
    if Path(args.meta).exists():
        meta_raw = json.loads(Path(args.meta).read_text(encoding="utf-8"))
        if isinstance(meta_raw, list):
            meta_by_id = {item.get("id"): item for item in meta_raw if isinstance(item, dict)}
        elif isinstance(meta_raw, dict):
            items = meta_raw.get("samples", meta_raw)
            if isinstance(items, list):
                meta_by_id = {item.get("id"): item for item in items if isinstance(item, dict)}
            elif isinstance(items, dict):
                meta_by_id = items

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(
            pool.map(lambda sample: run_one(args, sample, meta_by_id.get(sample["id"], {})), samples)
        )
    wall_s = time.perf_counter() - started
    summary = summarize(args, results, wall_s)
    Path(args.out).write_text(
        json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("SUMMARY", json.dumps(summary, indent=2))
    print(
        f"{'id':<24} {'tok':>5} {'skip':>4} {'ec':>3} {'rc':>3} "
        f"{'h/q':>5} {'acc':>7} {'pass':>5} errors"
    )
    for item in results:
        calls = item.get("model_calls") or {}
        accepted = item.get("accepted") or {}
        print(
            f"{item['id']:<24} {str(item.get('tokens', '?')):>5} "
            f"{str(bool(item.get('router_skipped'))):>4} "
            f"{int(item.get('entity_candidates') or 0):>3} "
            f"{int(item.get('relation_candidates') or 0):>3} "
            f"{int(calls.get('helper') or 0)}/{int(calls.get('qwen') or 0):<3} "
            f"{accepted.get('entities', 0)}/{accepted.get('relations', 0):<3} "
            f"{str(item.get('contract_pass')):>5} "
            f"{','.join((item.get('errors') or [])[:5])}"
        )
    print("wrote", args.out)


if __name__ == "__main__":
    main()
