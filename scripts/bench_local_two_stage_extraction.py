#!/usr/bin/env python3
"""Benchmark a caged two-stage local extraction flow.

This stays outside production Ghost B. It tests the architecture where a local
model proposes evidence first, proposes extraction second, and Python enforces
the existing Pydantic contract plus exact evidence substrings before converting
the accepted object to Polymath's existing compact JSONL shape.
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
    FACT_TYPES,
    JUNK_RE,
    PREDICATES,
    PLACEHOLDER_RE,
    _canonical_supported,
    _contract_schema,
    _empty_checks,
    _empty_counts,
    _extract_json,
    _is_router_skip,
    _norm,
    _post_json,
    _validate,
)
from services.ghost_b_schemas import ExtractionResponse  # noqa: E402


ENTITY_TYPE_BY_LOWER = {
    "person": "Person",
    "people": "Person",
    "organization": "Organization",
    "org": "Organization",
    "company": "Organization",
    "location": "Location",
    "place": "Location",
    "event": "Event",
    "concept": "Concept",
    "idea": "Concept",
    "method": "Method",
    "procedure": "Method",
    "product": "Product",
    "software": "Software",
    "document": "Document",
    "book": "Document",
    "standard": "Standard",
    "rule": "Rule",
    "law": "Law",
    "artifact": "Artifact",
    "time_reference": "TimeReference",
    "time reference": "TimeReference",
    "time": "TimeReference",
    "other": "other",
}

PREDICATE_SET = {item.strip() for item in PREDICATES.split(",")}
PREDICATE_REPAIR = {
    "define": "defines",
    "defined_by": "defines",
    "defines": "defines",
    "is_a": "instance_of",
    "is a": "instance_of",
    "kind_of": "instance_of",
    "type_of": "instance_of",
    "example": "example_of",
    "example_of": "example_of",
    "uses": "uses",
    "use": "uses",
    "used_for": "uses",
    "references": "references",
    "reference": "references",
    "mentions": "references",
    "depends": "depends_on",
    "depends_on": "depends_on",
    "requires": "depends_on",
    "causes": "causes",
    "cause": "causes",
    "drives": "causes",
    "drive": "causes",
    "enables": "supports",
    "supports": "supports",
    "creates": "produces",
    "produces": "produces",
    "outputs": "produces",
    "part": "part_of",
    "part_of": "part_of",
    "member": "member_of",
    "member_of": "member_of",
    "created_by": "created_by",
    "authored_by": "created_by",
    "related": "related_to",
    "related_to": "related_to",
}

FACT_TYPE_SET = {item.strip() for item in FACT_TYPES.split(",")}
LEDGER_KINDS = {"entity", "relation", "fact"}


def _clip_confidence(value: Any, default: float = 0.8) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, num))


def _canonical(value: Any) -> str:
    return _norm(str(value or ""))[:200]


def _snake(value: Any) -> str:
    text = _norm(str(value or "")).replace(" ", "_")
    return text[:80]


def _ledger_prompt() -> str:
    return """You are an evidence ledger extractor for Polymath.
Return ONLY valid JSON with exactly these top-level keys: skip, evidence.
Do not output markdown, code fences, prose, reasoning, summaries, or paraphrases.

Your job is not to summarize. Your job is to return exact quotes from the
document text that could support entities, relations, or facts.

If the chunk is mostly cover metadata, SVG, images, Calibre markup, index
entries, links, pagebreaks, bibliography/navigation boilerplate, or formatting
artifacts, return exactly {"skip":true,"evidence":[]}.

Rules:
- quote must be copied exactly from the chunk text.
- quote must be useful evidence, not a file path, html id, image name, citation
  target, page number, or formatting artifact.
- kind must be exactly one of: entity, relation, fact.
- return at most 10 evidence items.

JSON shape:
{
  "skip": false,
  "evidence": [
    {"quote": "exact quote from the chunk", "kind": "entity"}
  ]
}
"""


def _ledger_id_prompt() -> str:
    return """You are an evidence selector for Polymath.
Return ONLY valid JSON with exactly these top-level keys: skip, evidence.
Do not output markdown, code fences, prose, reasoning, summaries, or paraphrases.

Python has already extracted exact quote candidates from the chunk. Your job is
only to choose candidate ids that support useful entities, relations, or facts.
Do not copy quotes. Do not invent ids. Do not summarize.

If the candidates are mostly cover metadata, SVG, images, Calibre markup, index
entries, links, pagebreaks, bibliography/navigation boilerplate, or formatting
artifacts, return exactly {"skip":true,"evidence":[]}.

Rules:
- id must be one of the candidate ids.
- kind must be exactly one of: entity, relation, fact.
- return at most 10 evidence items.

JSON shape:
{
  "skip": false,
  "evidence": [
    {"id": "q001", "kind": "entity"}
  ]
}
"""


def _typed_ledger_id_prompt() -> str:
    return f"""You are an evidence selector for Polymath.
Return ONLY valid JSON with exactly these top-level keys: skip, entities, relations.
Do not output markdown, code fences, prose, reasoning, summaries, or paraphrases.

Python has already extracted exact evidence candidates from the chunk.
Entity candidates are exact surface_form spans.
Relation candidates are exact evidence_phrase quote/sentence spans.

Your job:
- choose useful entity candidate ids
- assign each chosen entity one allowed entity_type
- choose relation evidence ids only when the quote can support a relation

Do not copy candidate text. Do not invent ids. Prefer fewer correct selections.
If candidates are mostly cover metadata, SVG, Calibre markup, index entries,
links, bibliography/navigation boilerplate, or formatting artifacts, return
exactly {{"skip":true,"entities":[],"relations":[]}}.

Allowed entity_type values only:
{ENTITY_TYPES}

JSON shape:
{{
  "skip": false,
  "entities": [
    {{
      "id": "e001",
      "entity_type": "Person",
      "confidence": 0.9,
      "object_kind": ""
    }}
  ],
  "relations": [
    {{"id": "r001"}}
  ]
}}
"""


def _extraction_prompt(*, enable_facts: bool) -> str:
    facts_rule = (
        "Facts are enabled, but only emit facts directly supported by ledger quotes."
        if enable_facts
        else 'Facts are disabled for this benchmark. Always return "facts": [].'
    )
    return f"""You are a strict Polymath Ghost B extraction engine.
Return ONLY valid JSON with exactly these top-level keys: entities, relations, facts.
Do not output markdown, code fences, prose, reasoning, XML, YAML, or JSONL.
Return one JSON object only.

Use the VALID EVIDENCE LEDGER as your source of truth.
Every entity surface_form and every relation evidence_phrase must appear inside
one of the ledger quotes and must also be an exact substring of the chunk text.
If the ledger does not support an item, omit it.
{facts_rule}

Allowed entity_type values only:
{ENTITY_TYPES}

Allowed relation predicate values only:
{PREDICATES}

Allowed relation object_kind values only:
entity, literal

Allowed fact_type values only:
{FACT_TYPES}

Prefer fewer correct items over broad coverage.
Use at most 5 entities and 4 relations.

JSON shape:
{{
  "entities": [
    {{
      "canonical_name": "lowercase no punctuation",
      "surface_form": "verbatim phrase from ledger quote",
      "entity_type": "Concept",
      "confidence": 0.9,
      "query_aliases": [],
      "definitional_phrase": "",
      "object_kind": ""
    }}
  ],
  "relations": [
    {{
      "subject": "canonical_name from entities",
      "predicate": "defines",
      "object": "canonical_name or literal",
      "object_kind": "entity",
      "confidence": 0.8,
      "evidence_phrase": "exact ledger quote or exact substring of a ledger quote",
      "relation_cue": ""
    }}
  ],
  "facts": []
}}
"""


def _extraction_id_prompt(*, enable_facts: bool) -> str:
    facts_rule = (
        "Facts are enabled, but only emit facts directly supported by evidence ids."
        if enable_facts
        else 'Facts are disabled for this benchmark. Always return "facts": [].'
    )
    return f"""You are a strict Polymath Ghost B extraction adapter.
Return ONLY valid JSON with exactly these top-level keys: entities, relations, facts.
Do not output markdown, code fences, prose, reasoning, XML, YAML, or JSONL.
Return one JSON object only.

Use the VALID EVIDENCE LEDGER as your source of truth.
Do not copy quotes. Do not invent ids. Every surface_form_id and evidence_id
must be an id from the ledger. Python will map ids back to exact quotes.
If the ledger does not support an item, omit it.
{facts_rule}

Allowed entity_type values only:
{ENTITY_TYPES}

Allowed relation predicate values only:
{PREDICATES}

Allowed relation object_kind values only:
entity, literal

Allowed fact_type values only:
{FACT_TYPES}

Prefer fewer correct items over broad coverage.
Use at most 5 entities and 4 relations.

JSON shape:
{{
  "entities": [
    {{
      "canonical_name": "lowercase no punctuation",
      "surface_form_id": "q001",
      "entity_type": "Concept",
      "confidence": 0.9,
      "query_aliases": [],
      "definitional_phrase": "",
      "object_kind": ""
    }}
  ],
  "relations": [
    {{
      "subject": "canonical_name from entities",
      "predicate": "defines",
      "object": "canonical_name or literal",
      "object_kind": "entity",
      "confidence": 0.8,
      "evidence_id": "q001",
      "relation_cue": ""
    }}
  ],
  "facts": []
}}
"""


def _response_format(kind: str, *, enable_facts: bool = False) -> dict[str, Any] | None:
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "polymath_extraction",
                "strict": True,
                "schema": _contract_schema(enable_facts=enable_facts),
            },
        }
    return None


def _call_chat(
    *,
    base_url: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
    response_format: str = "none",
    enable_facts: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    fmt = _response_format(response_format, enable_facts=enable_facts)
    if fmt:
        payload["response_format"] = fmt

    started = time.perf_counter()
    body = _post_json(base_url.rstrip("/") + "/chat/completions", payload, timeout)
    latency_s = time.perf_counter() - started
    choice = (body.get("choices") or [{}])[0]
    raw = ((choice.get("message") or {}).get("content") or "").strip()
    usage = body.get("usage") or {}
    return {
        "raw": raw,
        "latency_s": latency_s,
        "finish_reason": choice.get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "completion_tok_s": (
            usage.get("completion_tokens") / latency_s
            if isinstance(usage.get("completion_tokens"), int) and latency_s > 0
            else None
        ),
    }


def _parse_ledger(obj: dict[str, Any] | None, text: str) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(obj, dict):
        return {"skip": False, "evidence": [], "raw_count": 0}, ["ledger_missing_json"]

    skip = bool(obj.get("skip", False))
    raw_items = obj.get("evidence", [])
    if not isinstance(raw_items, list):
        return {"skip": skip, "evidence": [], "raw_count": 0}, ["ledger_evidence_not_list"]

    evidence: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            errors.append(f"ledger[{idx}].not_object")
            continue
        quote = str(item.get("quote") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in LEDGER_KINDS:
            errors.append(f"ledger[{idx}].bad_kind")
            continue
        if not quote:
            errors.append(f"ledger[{idx}].empty_quote")
            continue
        if quote not in text:
            errors.append(f"ledger[{idx}].quote_not_substring")
            continue
        if JUNK_RE.search(quote) or PLACEHOLDER_RE.search(quote):
            errors.append(f"ledger[{idx}].quote_pollution")
            continue
        key = (quote, kind)
        if key in seen:
            continue
        seen.add(key)
        evidence.append({"quote": quote[:1000], "kind": kind})

    if skip and evidence:
        errors.append("ledger_skip_with_evidence")
    return {"skip": skip, "evidence": evidence, "raw_count": len(raw_items)}, errors


def _candidate_quotes(text: str, *, max_candidates: int) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    # Exact substrings only. Keep candidates short enough for cheap prompts but
    # long enough to hold relation evidence.
    for match in re.finditer(r"[^.!?\n]{24,260}(?:[.!?]|$)", text):
        quote = match.group(0).strip()
        if not quote or quote in seen or quote not in text:
            continue
        if JUNK_RE.search(quote) or PLACEHOLDER_RE.search(quote):
            continue
        alpha_ratio = sum(ch.isalpha() for ch in quote) / max(1, len(quote))
        if alpha_ratio < 0.55:
            continue
        seen.add(quote)
        candidates.append({"id": f"q{len(candidates) + 1:03d}", "quote": quote})
        if len(candidates) >= max_candidates:
            break
    return candidates


COMMON_ENTITY_FALSE_POSITIVES = {
    "a",
    "an",
    "and",
    "appendix",
    "chapter",
    "contents",
    "copyright",
    "figure",
    "for",
    "from",
    "if",
    "in",
    "index",
    "introduction",
    "note",
    "of",
    "page",
    "part",
    "section",
    "table",
    "the",
    "to",
}


def _candidate_entity_spans(text: str, *, max_candidates: int) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    # Cheap deterministic span candidates. This is deliberately conservative:
    # local models choose ids, but Python owns evidence and drops junk.
    pattern = re.compile(
        r"\b(?:[A-Z][A-Za-z0-9&'.-]{1,}|[A-Z]{2,})"
        r"(?:\s+(?:[A-Z][A-Za-z0-9&'.-]{1,}|[A-Z]{2,}|of|and|the|for|to|in))*\b"
    )
    for match in pattern.finditer(text):
        span = match.group(0).strip(" ,;:()[]{}\"'")
        if not span or span in seen or span not in text:
            continue
        if len(span) < 2 or len(span) > 80:
            continue
        if span.lower() in COMMON_ENTITY_FALSE_POSITIVES:
            continue
        if JUNK_RE.search(span) or PLACEHOLDER_RE.search(span):
            continue
        alpha_ratio = sum(ch.isalpha() for ch in span) / max(1, len(span))
        if alpha_ratio < 0.55:
            continue
        seen.add(span)
        candidates.append(
            {"id": f"e{len(candidates) + 1:03d}", "quote": span, "kind": "entity"}
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def _candidate_typed_evidence(
    text: str,
    *,
    max_entity_candidates: int,
    max_relation_candidates: int,
) -> list[dict[str, str]]:
    entities = _candidate_entity_spans(text, max_candidates=max_entity_candidates)
    relations = [
        {"id": f"r{idx + 1:03d}", "quote": item["quote"], "kind": "relation"}
        for idx, item in enumerate(
            _candidate_quotes(text, max_candidates=max_relation_candidates)
        )
    ]
    return entities + relations


def _parse_ledger_ids(
    obj: dict[str, Any] | None,
    candidates: list[dict[str, str]],
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(obj, dict):
        return {"skip": False, "evidence": [], "raw_count": 0}, ["ledger_missing_json"]

    skip = bool(obj.get("skip", False))
    raw_items = obj.get("evidence", [])
    if not isinstance(raw_items, list):
        return {"skip": skip, "evidence": [], "raw_count": 0}, ["ledger_evidence_not_list"]

    by_id = {item["id"]: item["quote"] for item in candidates}
    evidence: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            errors.append(f"ledger[{idx}].not_object")
            continue
        quote_id = str(item.get("id") or item.get("quote_id") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in LEDGER_KINDS:
            errors.append(f"ledger[{idx}].bad_kind")
            continue
        quote = by_id.get(quote_id)
        if not quote:
            errors.append(f"ledger[{idx}].unknown_id")
            continue
        key = (quote, kind)
        if key in seen:
            continue
        seen.add(key)
        evidence.append({"quote": quote, "kind": kind, "id": quote_id})

    if skip and evidence:
        errors.append("ledger_skip_with_evidence")
    return {"skip": skip, "evidence": evidence, "raw_count": len(raw_items)}, errors


def _parse_typed_ledger_ids(
    obj: dict[str, Any] | None,
    candidates: list[dict[str, str]],
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(obj, dict):
        return {"skip": False, "evidence": [], "raw_count": 0}, ["ledger_missing_json"]

    skip = bool(obj.get("skip", False))
    raw_entities = obj.get("entities", [])
    raw_relations = obj.get("relations", [])
    if not isinstance(raw_entities, list):
        raw_entities = []
        errors.append("ledger_entities_not_list")
    if not isinstance(raw_relations, list):
        raw_relations = []
        errors.append("ledger_relations_not_list")

    by_id = {item["id"]: item for item in candidates}
    evidence: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for idx, item in enumerate(raw_entities):
        if not isinstance(item, dict):
            errors.append(f"ledger.entities[{idx}].not_object")
            continue
        quote_id = str(item.get("id") or item.get("surface_form_id") or "").strip()
        candidate = by_id.get(quote_id)
        if not candidate:
            errors.append(f"ledger.entities[{idx}].unknown_id")
            continue
        if candidate.get("kind") != "entity":
            errors.append(f"ledger.entities[{idx}].wrong_candidate_kind")
            continue
        entity_type, repaired_type = _repair_entity_type(item.get("entity_type"))
        if not entity_type or repaired_type:
            errors.append(f"ledger.entities[{idx}].bad_entity_type")
            continue
        key = (candidate["quote"], "entity")
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            {
                "quote": candidate["quote"],
                "kind": "entity",
                "id": quote_id,
                "entity_type_hint": entity_type,
                "confidence_hint": str(_clip_confidence(item.get("confidence"), 0.8)),
                "object_kind_hint": str(item.get("object_kind") or "")[:100],
            }
        )

    for idx, item in enumerate(raw_relations):
        quote_id = ""
        if isinstance(item, str):
            quote_id = item.strip()
        elif isinstance(item, dict):
            quote_id = str(item.get("id") or item.get("evidence_id") or "").strip()
        else:
            errors.append(f"ledger.relations[{idx}].bad_item")
            continue
        candidate = by_id.get(quote_id)
        if not candidate:
            errors.append(f"ledger.relations[{idx}].unknown_id")
            continue
        if candidate.get("kind") != "relation":
            errors.append(f"ledger.relations[{idx}].wrong_candidate_kind")
            continue
        key = (candidate["quote"], "relation")
        if key in seen:
            continue
        seen.add(key)
        evidence.append({"quote": candidate["quote"], "kind": "relation", "id": quote_id})

    if skip and evidence:
        errors.append("ledger_skip_with_evidence")
    return {
        "skip": skip,
        "evidence": evidence,
        "raw_count": len(raw_entities) + len(raw_relations),
    }, errors


def _supported_by_ledger(value: str, ledger_quotes: list[str]) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    return any(value in quote or quote in value for quote in ledger_quotes)


def _repair_entity_type(value: Any) -> tuple[str | None, bool]:
    raw = str(value or "").strip()
    if raw in ENTITY_TYPE_BY_LOWER.values():
        return raw, False
    repaired = ENTITY_TYPE_BY_LOWER.get(raw.lower().replace("-", "_"))
    return repaired, repaired is not None


def _repair_predicate(value: Any) -> tuple[str | None, bool]:
    raw = str(value or "").strip()
    if raw in PREDICATE_SET:
        return raw, False
    key = raw.lower().replace("-", "_").replace(" ", "_")
    repaired = PREDICATE_REPAIR.get(key)
    return repaired, repaired is not None


def _sanitize_extraction(
    obj: dict[str, Any] | None,
    text: str,
    ledger: dict[str, Any],
    *,
    enable_facts: bool,
) -> tuple[dict[str, Any], dict[str, int]]:
    stats = {
        "repaired_canonical": 0,
        "repaired_entity_type": 0,
        "repaired_predicate": 0,
        "repaired_object_kind": 0,
        "dropped_entities": 0,
        "dropped_relations": 0,
        "dropped_facts": 0,
        "dropped_bad_evidence": 0,
        "dropped_bad_enum": 0,
    }
    clean = {"entities": [], "relations": [], "facts": []}
    if not isinstance(obj, dict):
        return clean, stats

    ledger_quotes = [str(item["quote"]) for item in ledger.get("evidence") or []]
    entity_by_norm: dict[str, str] = {}
    entity_names: set[str] = set()

    raw_entities = obj.get("entities") if isinstance(obj.get("entities"), list) else []
    for item in raw_entities:
        if not isinstance(item, dict):
            stats["dropped_entities"] += 1
            continue
        surface = str(item.get("surface_form") or item.get("canonical_name") or "").strip()
        if surface not in text or not _supported_by_ledger(surface, ledger_quotes):
            stats["dropped_entities"] += 1
            stats["dropped_bad_evidence"] += 1
            continue
        if JUNK_RE.search(surface) or PLACEHOLDER_RE.search(surface):
            stats["dropped_entities"] += 1
            stats["dropped_bad_evidence"] += 1
            continue
        canonical = _canonical(item.get("canonical_name") or surface)
        if canonical != str(item.get("canonical_name") or "").strip():
            stats["repaired_canonical"] += 1
        entity_type, repaired_type = _repair_entity_type(item.get("entity_type"))
        if not entity_type:
            stats["dropped_entities"] += 1
            stats["dropped_bad_enum"] += 1
            continue
        if repaired_type:
            stats["repaired_entity_type"] += 1
        if canonical in entity_names:
            continue
        entity = {
            "canonical_name": canonical,
            "surface_form": surface[:300],
            "entity_type": entity_type,
            "confidence": _clip_confidence(item.get("confidence"), 0.8),
            "query_aliases": [
                str(alias).strip()
                for alias in (item.get("query_aliases") or [])
                if str(alias).strip()
            ][:5],
            "definitional_phrase": str(item.get("definitional_phrase") or "")[:200],
            "object_kind": str(item.get("object_kind") or "")[:100],
        }
        clean["entities"].append(entity)
        entity_names.add(canonical)
        entity_by_norm[_canonical(surface)] = canonical
        entity_by_norm[_canonical(item.get("canonical_name"))] = canonical

    raw_relations = obj.get("relations") if isinstance(obj.get("relations"), list) else []
    for item in raw_relations:
        if not isinstance(item, dict):
            stats["dropped_relations"] += 1
            continue
        evidence = str(item.get("evidence_phrase") or "").strip()
        if evidence not in text or not _supported_by_ledger(evidence, ledger_quotes):
            stats["dropped_relations"] += 1
            stats["dropped_bad_evidence"] += 1
            continue
        if JUNK_RE.search(evidence) or PLACEHOLDER_RE.search(evidence):
            stats["dropped_relations"] += 1
            stats["dropped_bad_evidence"] += 1
            continue
        subject = entity_by_norm.get(_canonical(item.get("subject"))) or _canonical(item.get("subject"))
        obj_value = str(item.get("object") or "").strip()
        object_canonical = entity_by_norm.get(_canonical(obj_value))
        if subject not in entity_names:
            stats["dropped_relations"] += 1
            continue
        predicate, repaired_predicate = _repair_predicate(item.get("predicate"))
        if not predicate:
            stats["dropped_relations"] += 1
            stats["dropped_bad_enum"] += 1
            continue
        if repaired_predicate:
            stats["repaired_predicate"] += 1
        object_kind = str(item.get("object_kind") or "literal").strip().lower()
        if object_canonical:
            relation_object = object_canonical
            if object_kind != "entity":
                stats["repaired_object_kind"] += 1
            object_kind = "entity"
        else:
            relation_object = obj_value[:200]
            if object_kind not in {"entity", "literal"}:
                stats["repaired_object_kind"] += 1
                object_kind = "literal"
            if object_kind == "entity":
                stats["dropped_relations"] += 1
                continue
        clean["relations"].append(
            {
                "subject": subject,
                "predicate": predicate,
                "object": relation_object,
                "object_kind": object_kind,
                "confidence": _clip_confidence(item.get("confidence"), 0.75),
                "evidence_phrase": evidence[:500],
                "relation_cue": str(item.get("relation_cue") or "")[:120],
            }
        )

    if not enable_facts:
        raw_facts = obj.get("facts") if isinstance(obj.get("facts"), list) else []
        stats["dropped_facts"] += len(raw_facts)
        return clean, stats

    raw_facts = obj.get("facts") if isinstance(obj.get("facts"), list) else []
    for item in raw_facts:
        if not isinstance(item, dict):
            stats["dropped_facts"] += 1
            continue
        evidence = str(item.get("evidence_phrase") or "").strip()
        if evidence not in text or not _supported_by_ledger(evidence, ledger_quotes):
            stats["dropped_facts"] += 1
            stats["dropped_bad_evidence"] += 1
            continue
        subject = entity_by_norm.get(_canonical(item.get("subject"))) or _canonical(item.get("subject"))
        if subject not in entity_names:
            stats["dropped_facts"] += 1
            continue
        fact_type = str(item.get("fact_type") or "").strip()
        if fact_type not in FACT_TYPE_SET:
            stats["dropped_facts"] += 1
            stats["dropped_bad_enum"] += 1
            continue
        clean["facts"].append(
            {
                "subject": subject,
                "fact_type": fact_type,
                "property_name": _snake(item.get("property_name")) or "property",
                "value": str(item.get("value") or "")[:500],
                "unit": str(item.get("unit") or "")[:40],
                "condition": str(item.get("condition") or "")[:300],
                "confidence": _clip_confidence(item.get("confidence"), 0.75),
                "evidence_phrase": evidence[:500],
            }
        )
    return clean, stats


def _sanitize_id_extraction(
    obj: dict[str, Any] | None,
    ledger: dict[str, Any],
    *,
    enable_facts: bool,
) -> tuple[dict[str, Any], dict[str, int], dict[str, list[dict[str, Any]]]]:
    stats = {
        "repaired_canonical": 0,
        "repaired_entity_type": 0,
        "repaired_predicate": 0,
        "repaired_object_kind": 0,
        "dropped_entities": 0,
        "dropped_relations": 0,
        "dropped_facts": 0,
        "dropped_bad_evidence": 0,
        "dropped_bad_enum": 0,
        "dropped_bad_id": 0,
        "dropped_bad_predicate": 0,
        "dropped_bad_endpoint": 0,
        "dropped_bad_canonical": 0,
        "dropped_pydantic": 0,
    }
    audit = _empty_item_audit()
    clean = {"entities": [], "relations": [], "facts": []}
    if not isinstance(obj, dict):
        audit["dropped_pydantic"].append(
            _audit_item("response", 0, "adapter_not_object", obj)
        )
        stats["dropped_pydantic"] += 1
        return clean, stats, audit

    id_to_item = {
        str(item.get("id") or ""): item
        for item in (ledger.get("evidence") or [])
        if item.get("id") and item.get("quote")
    }
    entity_names: set[str] = set()
    entity_by_norm: dict[str, str] = {}

    raw_entities = obj.get("entities") if isinstance(obj.get("entities"), list) else []
    for idx, item in enumerate(raw_entities):
        if not isinstance(item, dict):
            stats["dropped_entities"] += 1
            audit["dropped_pydantic"].append(
                _audit_item("entity", idx, "not_object", item)
            )
            stats["dropped_pydantic"] += 1
            continue
        quote_id = str(item.get("surface_form_id") or item.get("surface_id") or "").strip()
        surface_item = id_to_item.get(quote_id)
        surface = str((surface_item or {}).get("quote") or "")
        if not surface:
            stats["dropped_entities"] += 1
            stats["dropped_bad_id"] += 1
            audit["dropped_bad_id"].append(
                _audit_item("entity", idx, "surface_form_id_not_in_ledger", item, id=quote_id)
            )
            continue
        if str(surface_item.get("kind") or "") != "entity":
            stats["dropped_entities"] += 1
            stats["dropped_bad_id"] += 1
            audit["dropped_bad_id"].append(
                _audit_item(
                    "entity",
                    idx,
                    "surface_form_id_not_entity_evidence",
                    item,
                    id=quote_id,
                    kind=surface_item.get("kind"),
                )
            )
            continue
        if JUNK_RE.search(surface) or PLACEHOLDER_RE.search(surface):
            stats["dropped_entities"] += 1
            stats["dropped_bad_evidence"] += 1
            audit["dropped_bad_evidence"].append(
                _audit_item("entity", idx, "surface_pollution", item, quote=surface[:300])
            )
            continue
        canonical = _canonical(item.get("canonical_name") or surface)
        if canonical != str(item.get("canonical_name") or "").strip():
            stats["repaired_canonical"] += 1
            audit["repaired_canonical"].append(
                _audit_item(
                    "entity",
                    idx,
                    "canonical_normalized",
                    item,
                    before=str(item.get("canonical_name") or ""),
                    after=canonical,
                )
            )
        entity_type, repaired_type = _repair_entity_type(
            item.get("entity_type") or surface_item.get("entity_type_hint")
        )
        if not entity_type:
            stats["dropped_entities"] += 1
            stats["dropped_bad_enum"] += 1
            audit["dropped_bad_enum"].append(
                _audit_item("entity", idx, "bad_entity_type", item)
            )
            continue
        if repaired_type:
            stats["repaired_entity_type"] += 1
        if not _canonical_supported(canonical, surface):
            stats["dropped_entities"] += 1
            stats["dropped_bad_canonical"] += 1
            stats["dropped_bad_evidence"] += 1
            audit["dropped_bad_canonical"].append(
                _audit_item(
                    "entity",
                    idx,
                    "canonical_not_supported_by_surface",
                    item,
                    canonical=canonical,
                    surface=surface[:300],
                )
            )
            audit["dropped_bad_evidence"].append(
                _audit_item(
                    "entity",
                    idx,
                    "canonical_not_supported_by_surface",
                    item,
                    canonical=canonical,
                    surface=surface[:300],
                )
            )
            continue
        if canonical in entity_names:
            continue
        clean_entity = {
            "canonical_name": canonical,
            "surface_form": surface[:300],
            "entity_type": entity_type,
            "confidence": _clip_confidence(item.get("confidence"), 0.8),
            "query_aliases": [
                str(alias).strip()
                for alias in (item.get("query_aliases") or [])
                if str(alias).strip()
            ][:5],
            "definitional_phrase": str(item.get("definitional_phrase") or "")[:200],
            "object_kind": str(item.get("object_kind") or "")[:100],
        }
        clean["entities"].append(clean_entity)
        audit["accepted"].append(
            _audit_item("entity", idx, "accepted", item, accepted=clean_entity)
        )
        entity_names.add(canonical)
        entity_by_norm[_canonical(item.get("canonical_name"))] = canonical
        entity_by_norm[_canonical(surface)] = canonical

    raw_relations = obj.get("relations") if isinstance(obj.get("relations"), list) else []
    for idx, item in enumerate(raw_relations):
        if not isinstance(item, dict):
            stats["dropped_relations"] += 1
            audit["dropped_pydantic"].append(
                _audit_item("relation", idx, "not_object", item)
            )
            stats["dropped_pydantic"] += 1
            continue
        evidence_id = str(item.get("evidence_id") or item.get("quote_id") or "").strip()
        evidence_item = id_to_item.get(evidence_id)
        evidence = str((evidence_item or {}).get("quote") or "")
        if not evidence:
            stats["dropped_relations"] += 1
            stats["dropped_bad_id"] += 1
            audit["dropped_bad_id"].append(
                _audit_item("relation", idx, "evidence_id_not_in_ledger", item, id=evidence_id)
            )
            continue
        if str(evidence_item.get("kind") or "") != "relation":
            stats["dropped_relations"] += 1
            stats["dropped_bad_id"] += 1
            audit["dropped_bad_id"].append(
                _audit_item(
                    "relation",
                    idx,
                    "evidence_id_not_relation_evidence",
                    item,
                    id=evidence_id,
                    kind=evidence_item.get("kind"),
                )
            )
            continue
        if JUNK_RE.search(evidence) or PLACEHOLDER_RE.search(evidence):
            stats["dropped_relations"] += 1
            stats["dropped_bad_evidence"] += 1
            audit["dropped_bad_evidence"].append(
                _audit_item("relation", idx, "evidence_pollution", item, quote=evidence[:300])
            )
            continue
        subject = entity_by_norm.get(_canonical(item.get("subject"))) or _canonical(item.get("subject"))
        if subject not in entity_names:
            stats["dropped_relations"] += 1
            stats["dropped_bad_endpoint"] += 1
            audit["dropped_bad_endpoint"].append(
                _audit_item("relation", idx, "subject_not_accepted_entity", item, subject=subject)
            )
            continue
        predicate, repaired_predicate = _repair_predicate(item.get("predicate"))
        if not predicate:
            stats["dropped_relations"] += 1
            stats["dropped_bad_enum"] += 1
            stats["dropped_bad_predicate"] += 1
            audit["dropped_bad_predicate"].append(
                _audit_item("relation", idx, "bad_predicate", item)
            )
            continue
        if repaired_predicate:
            stats["repaired_predicate"] += 1
        obj_value = str(item.get("object") or "").strip()
        object_canonical = entity_by_norm.get(_canonical(obj_value))
        object_kind = str(item.get("object_kind") or "literal").strip().lower()
        if object_canonical:
            relation_object = object_canonical
            if object_kind != "entity":
                stats["repaired_object_kind"] += 1
            object_kind = "entity"
        else:
            relation_object = obj_value[:200]
            if object_kind not in {"entity", "literal"}:
                stats["repaired_object_kind"] += 1
                object_kind = "literal"
            if object_kind == "entity":
                stats["dropped_relations"] += 1
                stats["dropped_bad_endpoint"] += 1
                audit["dropped_bad_endpoint"].append(
                    _audit_item("relation", idx, "object_entity_not_accepted_entity", item)
                )
                continue
        clean_relation = {
            "subject": subject,
            "predicate": predicate,
            "object": relation_object,
            "object_kind": object_kind,
            "confidence": _clip_confidence(item.get("confidence"), 0.75),
            "evidence_phrase": evidence[:500],
            "relation_cue": str(item.get("relation_cue") or "")[:120],
        }
        clean["relations"].append(clean_relation)
        audit["accepted"].append(
            _audit_item("relation", idx, "accepted", item, accepted=clean_relation)
        )

    if not enable_facts:
        raw_facts = obj.get("facts") if isinstance(obj.get("facts"), list) else []
        stats["dropped_facts"] += len(raw_facts)
        for idx, item in enumerate(raw_facts):
            audit["dropped_pydantic"].append(
                _audit_item("fact", idx, "facts_disabled", item)
            )
            stats["dropped_pydantic"] += 1
        return clean, stats, audit

    raw_facts = obj.get("facts") if isinstance(obj.get("facts"), list) else []
    for idx, item in enumerate(raw_facts):
        if not isinstance(item, dict):
            stats["dropped_facts"] += 1
            audit["dropped_pydantic"].append(
                _audit_item("fact", idx, "not_object", item)
            )
            stats["dropped_pydantic"] += 1
            continue
        evidence_id = str(item.get("evidence_id") or item.get("quote_id") or "").strip()
        evidence_item = id_to_item.get(evidence_id)
        evidence = str((evidence_item or {}).get("quote") or "")
        if not evidence:
            stats["dropped_facts"] += 1
            stats["dropped_bad_id"] += 1
            audit["dropped_bad_id"].append(
                _audit_item("fact", idx, "evidence_id_not_in_ledger", item, id=evidence_id)
            )
            continue
        if str(evidence_item.get("kind") or "") != "fact":
            stats["dropped_facts"] += 1
            stats["dropped_bad_id"] += 1
            audit["dropped_bad_id"].append(
                _audit_item(
                    "fact",
                    idx,
                    "evidence_id_not_fact_evidence",
                    item,
                    id=evidence_id,
                    kind=evidence_item.get("kind"),
                )
            )
            continue
        subject = entity_by_norm.get(_canonical(item.get("subject"))) or _canonical(item.get("subject"))
        if subject not in entity_names:
            stats["dropped_facts"] += 1
            stats["dropped_bad_endpoint"] += 1
            audit["dropped_bad_endpoint"].append(
                _audit_item("fact", idx, "subject_not_accepted_entity", item, subject=subject)
            )
            continue
        fact_type = str(item.get("fact_type") or "").strip()
        if fact_type not in FACT_TYPE_SET:
            stats["dropped_facts"] += 1
            stats["dropped_bad_enum"] += 1
            audit["dropped_bad_enum"].append(
                _audit_item("fact", idx, "bad_fact_type", item)
            )
            continue
        clean_fact = {
            "subject": subject,
            "fact_type": fact_type,
            "property_name": _snake(item.get("property_name")) or "property",
            "value": str(item.get("value") or "")[:500],
            "unit": str(item.get("unit") or "")[:40],
            "condition": str(item.get("condition") or "")[:300],
            "confidence": _clip_confidence(item.get("confidence"), 0.75),
            "evidence_phrase": evidence[:500],
        }
        clean["facts"].append(clean_fact)
        audit["accepted"].append(
            _audit_item("fact", idx, "accepted", item, accepted=clean_fact)
        )
    return clean, stats, audit


def _object_to_jsonl(raw: dict[str, Any]) -> str:
    obj = ExtractionResponse.model_validate(raw)
    lines: list[dict[str, Any]] = []
    for entity in obj.entities:
        item = {
            "t": "e",
            "cn": entity.canonical_name,
            "sf": entity.surface_form or entity.canonical_name,
            "et": entity.entity_type,
            "cf": entity.confidence,
        }
        if entity.object_kind:
            item["ek"] = entity.object_kind
        if entity.query_aliases:
            item["qa"] = entity.query_aliases[:5]
        if entity.definitional_phrase:
            item["def"] = entity.definitional_phrase
        lines.append(item)
    for relation in obj.relations:
        item = {
            "t": "r",
            "sub": relation.subject,
            "pred": relation.predicate,
            "obj": relation.object,
            "ok": relation.object_kind,
            "cf": relation.confidence,
            "ev": relation.evidence_phrase,
        }
        if relation.relation_cue:
            item["cue"] = relation.relation_cue
        lines.append(item)
    for fact in obj.facts:
        item = {
            "t": "f",
            "sub": fact.subject,
            "ft": fact.fact_type,
            "pn": fact.property_name,
            "val": fact.value,
            "cf": fact.confidence,
            "ev": fact.evidence_phrase,
        }
        if fact.unit:
            item["unit"] = fact.unit
        if fact.condition:
            item["cond"] = fact.condition
        lines.append(item)
    lines.append({"t": "x"})
    return "\n".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in lines)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * pct))]


def _stage1_error_breakdown(results: list[dict[str, Any]]) -> dict[str, int]:
    breakdown = {
        "bad_kind_count": 0,
        "skip_with_evidence_count": 0,
        "unknown_candidate_id_count": 0,
        "empty_selection_on_body_count": 0,
        "parse_error_count": 0,
        "other_stage1_error_count": 0,
    }
    for result in results:
        errors = list(result.get("stage1_errors") or [])
        for error in errors:
            if "bad_kind" in error:
                breakdown["bad_kind_count"] += 1
            elif error == "ledger_skip_with_evidence":
                breakdown["skip_with_evidence_count"] += 1
            elif "unknown_id" in error:
                breakdown["unknown_candidate_id_count"] += 1
            elif error.startswith("json_parse_error") or error in {
                "ledger_missing_json",
                "ledger_evidence_not_list",
                "stage1_truncated",
            }:
                breakdown["parse_error_count"] += 1
            else:
                breakdown["other_stage1_error_count"] += 1
        if (
            result.get("model_called_stage1")
            and not result.get("router_skipped")
            and not (result.get("ledger") or {}).get("skip")
            and not (result.get("ledger") or {}).get("evidence")
        ):
            breakdown["empty_selection_on_body_count"] += 1
    return breakdown


def _empty_item_audit() -> dict[str, list[dict[str, Any]]]:
    return {
        "accepted": [],
        "repaired_canonical": [],
        "dropped_bad_canonical": [],
        "dropped_bad_predicate": [],
        "dropped_bad_endpoint": [],
        "dropped_bad_evidence": [],
        "dropped_pydantic": [],
        "dropped_bad_id": [],
        "dropped_bad_enum": [],
    }


def _audit_item(kind: str, index: int, reason: str, item: Any, **extra: Any) -> dict[str, Any]:
    out = {
        "kind": kind,
        "index": index,
        "reason": reason,
        "item": item if isinstance(item, dict) else str(item)[:300],
    }
    out.update({key: value for key, value in extra.items() if value not in (None, "")})
    return out


def run_one(args: argparse.Namespace, sample: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    text = str(sample["text"])
    router_skip = _is_router_skip(meta)
    if router_skip and not args.no_router_skip:
        return {
            "id": sample["id"],
            **meta,
            "router_should_skip": True,
            "router_skipped": True,
            "model_called_stage1": False,
            "model_called_stage2": False,
            "ledger": {"skip": True, "evidence": [], "raw_count": 0},
            "raw_counts": _empty_counts(),
            "accepted": _empty_counts(),
            "dropped": _empty_counts(),
            "checks": _empty_checks(pydantic_ok=True),
            "sanitize_stats": {},
            "item_audit": _empty_item_audit(),
            "jsonl": "{\"t\":\"x\"}",
            "contract_pass": True,
            "production_useful": True,
            "errors": [],
        }

    result: dict[str, Any] = {
        "id": sample["id"],
        **meta,
        "router_should_skip": router_skip,
        "router_skipped": False,
        "ledger_mode": args.ledger_mode,
        "model_called_stage1": False,
        "model_called_stage2": False,
        "errors": [],
    }

    candidates: list[dict[str, str]] = []
    if args.ledger_mode in {"candidate_ids", "typed_candidate_ids"}:
        candidates = (
            _candidate_typed_evidence(
                text,
                max_entity_candidates=args.max_entity_candidates,
                max_relation_candidates=args.max_relation_candidates,
            )
            if args.ledger_mode == "typed_candidate_ids"
            else _candidate_quotes(text, max_candidates=args.max_ledger_candidates)
        )
        result["ledger_candidate_count"] = len(candidates)
        if not candidates:
            clean = {"entities": [], "relations": [], "facts": []}
            validation_errors, raw_counts, accepted, dropped, checks = _validate(
                clean,
                text,
                enable_facts=args.enable_facts,
            )
            result.update(
                {
                    "ledger": {"skip": True, "evidence": [], "raw_count": 0},
                    "raw_counts": raw_counts,
                    "accepted": accepted,
                    "dropped": dropped,
                    "checks": checks,
                    "sanitize_stats": {},
                    "item_audit": _empty_item_audit(),
                    "clean_object": clean,
                    "jsonl": _object_to_jsonl(clean),
                    "contract_pass": not validation_errors,
                    "production_useful": not validation_errors,
                    "errors": validation_errors,
                }
            )
            return result

    if args.ledger_mode == "typed_candidate_ids":
        stage1_system = _typed_ledger_id_prompt()
    elif args.ledger_mode == "candidate_ids":
        stage1_system = _ledger_id_prompt()
    else:
        stage1_system = _ledger_prompt()
    stage1_user = (
        "CANDIDATE EVIDENCE:\n"
        + json.dumps(candidates, ensure_ascii=False)
        + "\n\nChoose evidence ids from the candidate list only."
        if args.ledger_mode in {"candidate_ids", "typed_candidate_ids"}
        else "Document text:\n" + text
    )
    try:
        stage1 = _call_chat(
            base_url=args.evidence_base_url,
            model=args.evidence_model,
            system=stage1_system,
            user=stage1_user,
            max_tokens=args.evidence_max_tokens,
            timeout=args.timeout,
            response_format=args.evidence_response_format,
        )
        result["model_called_stage1"] = True
    except Exception as exc:
        result.update(
            {
                "contract_pass": False,
                "production_useful": False,
                "errors": [f"stage1:{exc}"],
            }
        )
        return result

    ledger_obj, stage1_parse_errors = _extract_json(stage1["raw"])
    if args.ledger_mode == "typed_candidate_ids":
        ledger, ledger_errors = _parse_typed_ledger_ids(ledger_obj, candidates)
    elif args.ledger_mode == "candidate_ids":
        ledger, ledger_errors = _parse_ledger_ids(ledger_obj, candidates)
    else:
        ledger, ledger_errors = _parse_ledger(ledger_obj, text)
    stage1_errors = stage1_parse_errors + ledger_errors
    result.update(
        {
            "stage1": {
                key: stage1.get(key)
                for key in (
                    "latency_s",
                    "finish_reason",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "completion_tok_s",
                )
            },
            "stage1_valid_json": bool(ledger_obj is not None and not stage1_parse_errors),
            "stage1_errors": stage1_errors,
            "stage1_raw_first_400": stage1["raw"][:400],
            "ledger": ledger,
        }
    )
    if stage1["finish_reason"] == "length":
        result["stage1_errors"].append("stage1_truncated")

    if ledger.get("skip") or not ledger.get("evidence"):
        if not ledger.get("skip"):
            result["errors"].append("ledger_empty")
        clean = {"entities": [], "relations": [], "facts": []}
        validation_errors, raw_counts, accepted, dropped, checks = _validate(
            clean,
            text,
            enable_facts=args.enable_facts,
        )
        result.update(
            {
                "raw_counts": raw_counts,
                "accepted": accepted,
                "dropped": dropped,
                "checks": checks,
                "sanitize_stats": {},
                "item_audit": _empty_item_audit(),
                "clean_object": clean,
                "jsonl": _object_to_jsonl(clean),
                "contract_pass": not (validation_errors or result["errors"] or result["stage1_errors"]),
                "production_useful": bool(
                    router_skip
                    and ledger.get("skip")
                    and not (validation_errors or result["errors"] or result["stage1_errors"])
                ),
                "errors": result["errors"] + result["stage1_errors"] + validation_errors,
            }
        )
        return result

    user = (
        "VALID EVIDENCE LEDGER:\n"
        + json.dumps({"evidence": ledger["evidence"]}, ensure_ascii=False)
        + (
            "\n\nFill the adapter schema using only ids from the ledger."
            if args.extract_mode == "id_adapter"
            else "\n\nDocument text:\n" + text
        )
    )
    try:
        stage2 = _call_chat(
            base_url=args.extract_base_url,
            model=args.extract_model,
            system=(
                _extraction_id_prompt(enable_facts=args.enable_facts)
                if args.extract_mode == "id_adapter"
                else _extraction_prompt(enable_facts=args.enable_facts)
            ),
            user=user,
            max_tokens=args.extract_max_tokens,
            timeout=args.timeout,
            response_format=args.extract_response_format,
            enable_facts=args.enable_facts,
        )
        result["model_called_stage2"] = True
    except Exception as exc:
        result.update(
            {
                "contract_pass": False,
                "production_useful": False,
                "errors": result["errors"] + [f"stage2:{exc}"],
            }
        )
        return result

    raw_obj, stage2_parse_errors = _extract_json(stage2["raw"])
    item_audit = _empty_item_audit()
    if args.extract_mode == "id_adapter":
        raw_counts = {
            key: len(raw_obj.get(key) or []) if isinstance(raw_obj, dict) and isinstance(raw_obj.get(key), list) else 0
            for key in ("entities", "relations", "facts")
        }
        raw_validation_errors = []
        raw_checks = {"pydantic_ok": False}
        clean, sanitize_stats, item_audit = _sanitize_id_extraction(
            raw_obj,
            ledger,
            enable_facts=args.enable_facts,
        )
    else:
        raw_validation_errors, raw_counts, _, _, raw_checks = _validate(
            raw_obj,
            text,
            enable_facts=args.enable_facts,
        )
        clean, sanitize_stats = _sanitize_extraction(
            raw_obj,
            text,
            ledger,
            enable_facts=args.enable_facts,
        )
    final_errors, final_counts, accepted, dropped, checks = _validate(
        clean,
        text,
        enable_facts=args.enable_facts,
    )
    if final_errors:
        sanitize_stats["dropped_pydantic"] = sanitize_stats.get("dropped_pydantic", 0) + len(final_errors)
        for idx, error in enumerate(final_errors):
            item_audit["dropped_pydantic"].append(
                _audit_item("final", idx, "final_validation_error", {"error": error})
            )
    try:
        jsonl = _object_to_jsonl(clean)
    except Exception as exc:
        jsonl = ""
        final_errors.append(f"jsonl_convert:{type(exc).__name__}:{exc}")

    errors = result["errors"] + result.get("stage1_errors", []) + stage2_parse_errors + final_errors
    if stage2["finish_reason"] == "length":
        stage2_parse_errors.append("stage2_truncated")
        errors.append("stage2_truncated")
    emitted_total = sum(int(accepted.get(key) or 0) for key in ("entities", "relations", "facts"))
    result.update(
        {
            "stage2": {
                key: stage2.get(key)
                for key in (
                    "latency_s",
                    "finish_reason",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "completion_tok_s",
                )
            },
            "stage2_valid_json": bool(raw_obj is not None and not stage2_parse_errors),
            "stage2_errors": stage2_parse_errors,
            "stage2_raw_pydantic_ok": (
                None
                if args.extract_mode == "id_adapter"
                else bool(raw_checks.get("pydantic_ok") and not raw_validation_errors)
            ),
            "stage2_adapter_mode": args.extract_mode,
            "stage2_raw_validation_errors": raw_validation_errors[:30],
            "stage2_raw_first_400": stage2["raw"][:400],
            "raw_counts": raw_counts,
            "final_counts": final_counts,
            "accepted": accepted,
            "dropped": dropped,
            "checks": checks,
            "sanitize_stats": sanitize_stats,
            "item_audit": item_audit,
            "clean_object": clean,
            "jsonl": jsonl,
            "contract_pass": not errors,
            "production_useful": bool(not errors and emitted_total > 0),
            "errors": errors[:40],
        }
    )
    return result


def summarize(args: argparse.Namespace, results: list[dict[str, Any]], wall_s: float) -> dict[str, Any]:
    stage1_results = [r for r in results if r.get("model_called_stage1")]
    stage2_results = [r for r in results if r.get("model_called_stage2")]
    router_skipped = sum(1 for r in results if r.get("router_skipped"))
    stage1_valid = sum(1 for r in stage1_results if r.get("stage1_valid_json"))
    stage2_valid = sum(1 for r in stage2_results if r.get("stage2_valid_json"))
    raw_pydantic_ok = sum(1 for r in stage2_results if r.get("stage2_raw_pydantic_ok"))
    final_pydantic_ok = sum(
        1 for r in stage2_results if (r.get("checks") or {}).get("pydantic_ok")
    )
    evidence_ok = sum(
        1
        for r in stage2_results
        if (r.get("checks") or {}).get("pydantic_ok")
        and int((r.get("checks") or {}).get("evidence_errors") or 0) == 0
    )
    ledger_raw = sum(int((r.get("ledger") or {}).get("raw_count") or 0) for r in results)
    ledger_valid = sum(len((r.get("ledger") or {}).get("evidence") or []) for r in results)
    truncated = sum(
        1
        for r in results
        if "stage1_truncated" in (r.get("stage1_errors") or [])
        or "stage2_truncated" in (r.get("stage2_errors") or [])
    )
    stage1_error_count = sum(len(r.get("stage1_errors") or []) for r in results)
    stage1_error_breakdown = _stage1_error_breakdown(results)
    stage2_error_count = sum(len(r.get("stage2_errors") or []) for r in results)
    stage1_latencies = [
        float((r.get("stage1") or {}).get("latency_s"))
        for r in stage1_results
        if (r.get("stage1") or {}).get("latency_s") is not None
    ]
    stage2_latencies = [
        float((r.get("stage2") or {}).get("latency_s"))
        for r in stage2_results
        if (r.get("stage2") or {}).get("latency_s") is not None
    ]
    stage1_rates = [
        float((r.get("stage1") or {}).get("completion_tok_s"))
        for r in stage1_results
        if (r.get("stage1") or {}).get("completion_tok_s")
    ]
    stage2_rates = [
        float((r.get("stage2") or {}).get("completion_tok_s"))
        for r in stage2_results
        if (r.get("stage2") or {}).get("completion_tok_s")
    ]
    sanitize_totals: dict[str, int] = {}
    audit_totals: dict[str, int] = {}
    for r in results:
        for key, value in (r.get("sanitize_stats") or {}).items():
            sanitize_totals[key] = sanitize_totals.get(key, 0) + int(value or 0)
        for key, items in (r.get("item_audit") or {}).items():
            audit_totals[key] = audit_totals.get(key, 0) + len(items or [])
    accepted_totals = {
        "entities": sum(int((r.get("accepted") or {}).get("entities") or 0) for r in results),
        "relations": sum(int((r.get("accepted") or {}).get("relations") or 0) for r in results),
        "facts": sum(int((r.get("accepted") or {}).get("facts") or 0) for r in results),
    }
    accepted_output_total = sum(accepted_totals.values())
    contract_pass_count = sum(1 for r in results if r.get("contract_pass"))

    denom1 = len(stage1_results) or 1
    denom2 = len(stage2_results) or 1
    stage1_valid_json_rate = stage1_valid / denom1
    stage2_valid_json_rate = stage2_valid / denom2
    raw_pydantic_pass_rate = raw_pydantic_ok / denom2
    final_pydantic_pass_rate = final_pydantic_ok / denom2
    evidence_pass_rate = evidence_ok / denom2
    ledger_quote_pass_rate = (ledger_valid / ledger_raw) if ledger_raw else 1.0
    gate_failures: list[str] = []
    if stage1_valid_json_rate < 1.0:
        gate_failures.append("stage1_valid_json_rate_below_100")
    if stage1_error_count:
        gate_failures.append("stage1_error_count_nonzero")
    if ledger_quote_pass_rate < 0.95:
        gate_failures.append("ledger_quote_pass_rate_below_95")
    if stage2_results and stage2_valid_json_rate < 1.0:
        gate_failures.append("stage2_valid_json_rate_below_100")
    if stage2_error_count:
        gate_failures.append("stage2_error_count_nonzero")
    if final_pydantic_pass_rate < 0.98 and stage2_results:
        gate_failures.append("final_pydantic_pass_rate_below_98")
    if evidence_pass_rate < 0.95 and stage2_results:
        gate_failures.append("evidence_pass_rate_below_95")
    if truncated:
        gate_failures.append("truncated_output_nonzero")
    if stage2_results and accepted_output_total == 0:
        gate_failures.append("accepted_output_zero")
    if contract_pass_count < len(results):
        gate_failures.append("contract_pass_not_all_samples")

    return {
        "evidence_model": args.evidence_model,
        "extract_model": args.extract_model,
        "evidence_base_url": args.evidence_base_url,
        "extract_base_url": args.extract_base_url,
        "ledger_mode": args.ledger_mode,
        "extract_mode": args.extract_mode,
        "samples": len(results),
        "router_skipped": router_skipped,
        "stage1_model_called": len(stage1_results),
        "stage2_model_called": len(stage2_results),
        "concurrency": args.concurrency,
        "facts_enabled": args.enable_facts,
        "wall_s": wall_s,
        "chunks_per_hour_wall": (len(results) / wall_s * 3600) if wall_s else None,
        "contract_pass": contract_pass_count,
        "contract_pass_rate": contract_pass_count / (len(results) or 1),
        "production_useful": sum(1 for r in results if r.get("production_useful")),
        "stage1_valid_json_rate": stage1_valid_json_rate,
        "ledger_raw_count": ledger_raw,
        "ledger_valid_count": ledger_valid,
        "ledger_quote_pass_rate": ledger_quote_pass_rate,
        "stage2_valid_json_rate": stage2_valid_json_rate,
        "stage2_raw_pydantic_pass_rate": (
            None if args.extract_mode == "id_adapter" else raw_pydantic_pass_rate
        ),
        "final_pydantic_pass_rate": final_pydantic_pass_rate,
        "evidence_pass_rate": evidence_pass_rate,
        "stage1_error_count": stage1_error_count,
        "stage1_error_breakdown": stage1_error_breakdown,
        "stage2_error_count": stage2_error_count,
        "truncated_output_count": truncated,
        "eligible_for_ghost_b": not gate_failures,
        "gate_failures": gate_failures,
        "stage1_latency_p50_s": statistics.median(stage1_latencies) if stage1_latencies else None,
        "stage1_latency_p95_s": _percentile(stage1_latencies, 0.95),
        "stage2_latency_p50_s": statistics.median(stage2_latencies) if stage2_latencies else None,
        "stage2_latency_p95_s": _percentile(stage2_latencies, 0.95),
        "stage1_tok_s_median": statistics.median(stage1_rates) if stage1_rates else None,
        "stage2_tok_s_median": statistics.median(stage2_rates) if stage2_rates else None,
        "accepted_totals": accepted_totals,
        "sanitize_totals": sanitize_totals,
        "item_audit_totals": audit_totals,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8094/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--evidence-base-url")
    parser.add_argument("--extract-base-url")
    parser.add_argument("--evidence-model")
    parser.add_argument("--extract-model")
    parser.add_argument("--samples", default="/tmp/zto_chunk_samples_balanced.jsonl")
    parser.add_argument("--meta", default="/tmp/zto_chunk_samples_meta.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--evidence-max-tokens", type=int, default=600)
    parser.add_argument("--extract-max-tokens", type=int, default=1200)
    parser.add_argument(
        "--ledger-mode",
        choices=["free_quote", "candidate_ids", "typed_candidate_ids"],
        default="candidate_ids",
    )
    parser.add_argument(
        "--extract-mode",
        choices=["direct_object", "id_adapter"],
        default="id_adapter",
    )
    parser.add_argument("--max-ledger-candidates", type=int, default=24)
    parser.add_argument("--max-entity-candidates", type=int, default=18)
    parser.add_argument("--max-relation-candidates", type=int, default=18)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument(
        "--evidence-response-format",
        choices=["none", "json_object"],
        default="none",
    )
    parser.add_argument(
        "--extract-response-format",
        choices=["none", "json_object", "json_schema"],
        default="none",
    )
    parser.add_argument("--enable-facts", action="store_true")
    parser.add_argument("--no-router-skip", action="store_true")
    args = parser.parse_args()
    args.evidence_base_url = args.evidence_base_url or args.base_url
    args.extract_base_url = args.extract_base_url or args.base_url
    args.evidence_model = args.evidence_model or args.model
    args.extract_model = args.extract_model or args.model

    samples = [
        json.loads(line)
        for line in Path(args.samples).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta_by_id = {
        str(item["id"]): item
        for item in json.loads(Path(args.meta).read_text(encoding="utf-8"))
    }

    wall_started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(
            pool.map(lambda sample: run_one(args, sample, meta_by_id.get(sample["id"], {})), samples)
        )
    wall_s = time.perf_counter() - wall_started
    summary = summarize(args, results, wall_s)
    Path(args.out).write_text(
        json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("SUMMARY", json.dumps(summary, indent=2))
    print(
        f"{'id':<24} {'tok':>5} {'skip':>4} {'ev':>5} {'s1':>6} {'s2':>6} "
        f"{'raw':>9} {'acc':>9} {'pass':>5} {'use':>5} errors"
    )
    for r in results:
        ledger_count = len((r.get("ledger") or {}).get("evidence") or [])
        stage1_s = (r.get("stage1") or {}).get("latency_s")
        stage2_s = (r.get("stage2") or {}).get("latency_s")
        raw = r.get("raw_counts") or {}
        accepted = r.get("accepted") or {}
        errors = (r.get("errors") or []) + (r.get("stage1_errors") or []) + (r.get("stage2_errors") or [])
        print(
            f"{r['id']:<24} {str(r.get('tokens', '?')):>5} "
            f"{str(bool(r.get('router_skipped'))):>4} {ledger_count:>5} "
            f"{(f'{stage1_s:.2f}' if stage1_s is not None else '-'):>6} "
            f"{(f'{stage2_s:.2f}' if stage2_s is not None else '-'):>6} "
            f"{raw.get('entities', 0)}/{raw.get('relations', 0)}/{raw.get('facts', 0):<3} "
            f"{accepted.get('entities', 0)}/{accepted.get('relations', 0)}/{accepted.get('facts', 0):<3} "
            f"{str(r.get('contract_pass')):>5} {str(r.get('production_useful')):>5} "
            f"{','.join(errors[:4])}"
        )
    print("wrote", args.out)


if __name__ == "__main__":
    main()
