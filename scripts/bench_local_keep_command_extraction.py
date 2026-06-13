#!/usr/bin/env python3
"""Benchmark a KEEP_* command-language local extraction helper.

The model does not emit JSON or JSONL. It selects exact Python-provided
candidate IDs with commands such as:

  KEEP_ENTITY E001 Software app
  KEEP_REL E001 uses E002 EV001 uses

Python maps IDs to exact text, validates the existing ExtractionResponse
contract, and converts accepted output to the existing compact JSONL shape.
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


KEEP_ENTITY_RE = re.compile(
    r"^\s*KEEP_ENTITY\s+(E\d{3})\s+([A-Za-z_ ]+)\s+([A-Za-z0-9_.:-]{1,100})\s*$",
    re.I,
)
KEEP_ENTITY_COMPACT_RE = re.compile(
    r"^\s*KEEP_ENTITY\s+(E\d{3})\s+([A-Za-z_ ]+)\s*$",
    re.I,
)
KEEP_REL_RE = re.compile(
    r"^\s*KEEP_REL\s+(E\d{3})\s+([a-z_]+)\s+(E\d{3})\s+(EV\d{3})\s+([A-Za-z0-9_.:-]{1,80})\s*$",
    re.I,
)
KEEP_REL_COMPACT_RE = re.compile(
    r"^\s*KEEP_REL\s+(E\d{3})\s+([a-z_]+)\s+(E\d{3})\s+(EV\d{3})(?:\s+([A-Za-z0-9_.:-]{1,80}))?\s*$",
    re.I,
)
NONE_RE = re.compile(r"^\s*NONE\s*$", re.I)
EXTRA_JUNK_RE = re.compile(
    r"(?:copyright|all rights reserved|library of congress|cataloging-in-publication|"
    r"registered trademarks?|xlink|xmlns|ebook|isbn|title page|published in|"
    r"calibre|mbp_pagebreak|mailto:|https?://)",
    re.I,
)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]*")

STOP_WORDS = {
    "a",
    "about",
    "above",
    "after",
    "again",
    "all",
    "also",
    "although",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "because",
    "before",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "each",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "his",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "more",
    "not",
    "of",
    "on",
    "or",
    "our",
    "she",
    "such",
    "that",
    "the",
    "their",
    "there",
    "these",
    "they",
    "this",
    "to",
    "was",
    "were",
    "which",
    "while",
    "with",
    "would",
    "you",
}


def _system_prompt(
    *,
    enable_facts: bool,
    body_selection_pressure: bool,
    allow_compact_lines: bool,
) -> str:
    facts_line = (
        "Facts are enabled, but emit KEEP_FACT only when explicitly instructed."
        if enable_facts
        else "Facts are disabled. Never emit KEEP_FACT."
    )
    selection_line = (
        "These are curated body chunks. If meaningful entity candidates exist, keep the obvious named entities and core concepts. Use NONE only when the candidates are junk or the chunk has no useful content."
        if body_selection_pressure
        else "If evidence is weak, output NONE.\nIf unsure, output NONE."
    )
    compact_line = (
        "Compact forms are allowed: KEEP_ENTITY E001 Organization and KEEP_REL E001 uses E002 EV001."
        if allow_compact_lines
        else "Always include object_kind on KEEP_ENTITY and cue on KEEP_REL."
    )
    return f"""You are Polymath Ghost-B Helper.
You output only command lines. No JSON. No JSONL. No Markdown. No prose. No reasoning.

Use only candidate IDs provided by Python.
Use only exact enum labels.
{selection_line}
{facts_line}
{compact_line}

Allowed commands:
KEEP_ENTITY <entity_id> <entity_type> <object_kind>
KEEP_REL <subject_entity_id> <predicate> <object_entity_id> <evidence_id> <cue>
NONE

Allowed entity_type:
{ENTITY_TYPES}

Allowed predicate:
{PREDICATES}

Rules:
- KEEP_ENTITY must use an E### id from ENTITY CANDIDATES.
- In KEEP_ENTITY, the token after the entity id must be an entity_type enum, not the candidate text.
- KEEP_REL subject and object must both be kept entities.
- KEEP_REL evidence must use an EV### id from EVIDENCE CANDIDATES.
- In KEEP_REL, use ids only. Do not copy entity text or evidence text.
- Do not emit old predicates like runs_on, trained_on, or classifies.
- Prefer 3-8 strong entities and 1-5 strong relations.
- Do not select formatting artifacts, citations, reference-list entries, or vague fragments.

Valid examples:
KEEP_ENTITY E001 Software app
KEEP_ENTITY E002 Method capability
KEEP_REL E001 uses E002 EV001 uses

Invalid examples:
KEEP_ENTITY E001 "Messenger" Software
KEEP_REL E001 "uses" "Messenger"
"""


def _with_no_think(text: str, enabled: bool) -> str:
    return text + ("\n/no_think" if enabled else "")


def _alpha_ratio(text: str) -> float:
    alpha = sum(ch.isalpha() for ch in text)
    return alpha / max(1, len(text))


def _clean_phrase(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip(" ,;:()[]{}\"'")
    return text[:120]


def _is_bad_candidate(text: str) -> bool:
    if not text or len(text) < 2:
        return True
    if JUNK_RE.search(text) or EXTRA_JUNK_RE.search(text) or PLACEHOLDER_RE.search(text):
        return True
    if _alpha_ratio(text) < 0.45:
        return True
    if text.lower() in STOP_WORDS:
        return True
    if len(text.split()) > 7:
        return True
    return False


def _candidate_entities(text: str, *, max_candidates: int) -> list[dict[str, str]]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(phrase: str) -> None:
        phrase = _clean_phrase(phrase)
        key = phrase.lower()
        if key in seen or _is_bad_candidate(phrase):
            return
        seen.add(key)
        candidates.append(phrase)

    for item in _candidate_entity_spans(text, max_candidates=max_candidates * 2):
        add(item["quote"])

    # Code/model/artifact-ish exact spans.
    for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9]*(?:[._+-][A-Za-z0-9]+)+\b", text):
        add(match.group(0))

    # Lowercase concept phrases. This is a deterministic rough candidate pass,
    # not a truth layer; Python still validates selected exact surface spans.
    words = [(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(text)]
    for n in (4, 3, 2):
        for i in range(0, max(0, len(words) - n + 1)):
            toks = [w[0] for w in words[i : i + n]]
            low = [t.lower() for t in toks]
            if low[0] in STOP_WORDS or low[-1] in STOP_WORDS:
                continue
            if sum(1 for t in low if t in STOP_WORDS) > 1:
                continue
            phrase = text[words[i][1] : words[i + n - 1][2]]
            if any(ch.isupper() for ch in phrase) and n < 3:
                continue
            if not any(
                marker in phrase.lower()
                for marker in (
                    "database",
                    "model",
                    "language",
                    "schema",
                    "integrity",
                    "method",
                    "system",
                    "project",
                    "technology",
                    "coordinate",
                    "damage",
                    "weapon",
                    "motive",
                    "inference",
                    "privacy",
                    "management",
                    "protocol",
                    "measurement",
                    "gyroscope",
                    "relation",
                    "hypoth",
                )
            ):
                continue
            add(phrase)
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates:
            break

    return [
        {"id": f"E{idx + 1:03d}", "quote": quote}
        for idx, quote in enumerate(candidates[:max_candidates])
    ]


def _candidate_evidence(text: str, *, max_candidates: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in _candidate_quotes(text, max_candidates=max_candidates * 3):
        quote = _clean_phrase(item["quote"])
        if quote.lower() in seen or _is_bad_candidate(quote):
            continue
        if len(quote) < 35 or len(quote) > 360:
            continue
        seen.add(quote.lower())
        out.append({"id": f"EV{len(out) + 1:03d}", "quote": quote})
        if len(out) >= max_candidates:
            break
    return out


def _format_candidates(candidates: list[dict[str, str]], *, key: str) -> str:
    return "\n".join(f'{item["id"]} | {item[key]}' for item in candidates)


def _parse_commands(
    raw: str,
    entity_candidates: list[dict[str, str]],
    evidence_candidates: list[dict[str, str]],
    *,
    allow_compact_lines: bool,
) -> tuple[dict[str, Any], dict[str, int], list[str]]:
    stats = {
        "raw_lines": 0,
        "valid_lines": 0,
        "invalid_lines": 0,
        "keep_entity_lines": 0,
        "keep_rel_lines": 0,
        "none_lines": 0,
        "dropped_entities": 0,
        "dropped_relations": 0,
        "bad_enum": 0,
        "bad_id": 0,
    }
    errors: list[str] = []
    entity_by_id = {item["id"].upper(): item["quote"] for item in entity_candidates}
    evidence_by_id = {item["id"].upper(): item["quote"] for item in evidence_candidates}
    clean = {"entities": [], "relations": [], "facts": []}
    accepted_entities_by_id: dict[str, dict[str, Any]] = {}
    seen_entity_names: set[str] = set()

    raw_relations: list[dict[str, str]] = []
    for line in [ln.strip() for ln in str(raw or "").splitlines() if ln.strip()]:
        stats["raw_lines"] += 1
        if NONE_RE.match(line):
            stats["valid_lines"] += 1
            stats["none_lines"] += 1
            continue

        ent_match = KEEP_ENTITY_RE.match(line)
        compact_entity = False
        if not ent_match and allow_compact_lines:
            ent_match = KEEP_ENTITY_COMPACT_RE.match(line)
            compact_entity = ent_match is not None
        if ent_match:
            entity_id = ent_match.group(1).upper()
            surface = entity_by_id.get(entity_id)
            if not surface:
                stats["invalid_lines"] += 1
                stats["bad_id"] += 1
                errors.append("entity_unknown_id")
                continue
            entity_type, repaired = _repair_entity_type(ent_match.group(2))
            if not entity_type or repaired:
                stats["invalid_lines"] += 1
                stats["bad_enum"] += 1
                errors.append("entity_bad_type")
                continue
            canonical = _canonical(surface)
            if (
                not surface
                or _is_bad_candidate(surface)
                or not _canonical_supported(canonical, surface)
            ):
                stats["valid_lines"] += 1
                stats["dropped_entities"] += 1
                errors.append("entity_bad_surface")
                continue
            if canonical in seen_entity_names:
                stats["valid_lines"] += 1
                continue
            object_kind = (
                ""
                if compact_entity
                else _canonical(ent_match.group(3)).replace(" ", "_")[:100]
            )
            entity = {
                "canonical_name": canonical,
                "surface_form": surface[:300],
                "entity_type": entity_type,
                "confidence": 0.85,
                "query_aliases": [],
                "definitional_phrase": "",
                "object_kind": object_kind,
            }
            clean["entities"].append(entity)
            accepted_entities_by_id[entity_id] = entity
            seen_entity_names.add(canonical)
            stats["valid_lines"] += 1
            stats["keep_entity_lines"] += 1
            continue

        rel_match = KEEP_REL_RE.match(line)
        if not rel_match and allow_compact_lines:
            rel_match = KEEP_REL_COMPACT_RE.match(line)
        if rel_match:
            raw_relations.append(
                {
                    "subject_id": rel_match.group(1).upper(),
                    "predicate": rel_match.group(2),
                    "object_id": rel_match.group(3).upper(),
                    "evidence_id": rel_match.group(4).upper(),
                    "cue": rel_match.group(5) or rel_match.group(2),
                }
            )
            stats["valid_lines"] += 1
            stats["keep_rel_lines"] += 1
            continue

        stats["invalid_lines"] += 1
        errors.append("command_line_invalid")

    seen_relations: set[tuple[str, str, str, str]] = set()
    for rel in raw_relations:
        subject = accepted_entities_by_id.get(rel["subject_id"])
        obj = accepted_entities_by_id.get(rel["object_id"])
        evidence = evidence_by_id.get(rel["evidence_id"])
        if not subject or not obj or not evidence:
            stats["dropped_relations"] += 1
            stats["bad_id"] += 1
            errors.append("relation_missing_endpoint_or_evidence")
            continue
        predicate, repaired = _repair_predicate(rel["predicate"])
        if not predicate or repaired:
            stats["dropped_relations"] += 1
            stats["bad_enum"] += 1
            errors.append("relation_bad_predicate")
            continue
        if subject["canonical_name"] == obj["canonical_name"]:
            stats["dropped_relations"] += 1
            errors.append("relation_self_edge")
            continue
        if _is_bad_candidate(evidence):
            stats["dropped_relations"] += 1
            errors.append("relation_bad_evidence")
            continue
        key = (subject["canonical_name"], predicate, obj["canonical_name"], evidence)
        if key in seen_relations:
            continue
        seen_relations.add(key)
        clean["relations"].append(
            {
                "subject": subject["canonical_name"],
                "predicate": predicate,
                "object": obj["canonical_name"],
                "object_kind": "entity",
                "confidence": 0.82,
                "evidence_phrase": evidence[:500],
                "relation_cue": _canonical(rel["cue"]).replace(" ", "_")[:120],
            }
        )

    return clean, stats, errors


def _run_one(args: argparse.Namespace, sample: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    text = str(sample["text"])
    entity_candidates = _candidate_entities(text, max_candidates=args.max_entity_candidates)
    evidence_candidates = _candidate_evidence(text, max_candidates=args.max_evidence_candidates)
    user = f"""CHUNK:
{text}

ENTITY CANDIDATES:
{_format_candidates(entity_candidates, key="quote")}

EVIDENCE CANDIDATES:
{_format_candidates(evidence_candidates, key="quote")}

Return command lines only.
"""
    started = time.perf_counter()
    call = _call_chat(
        base_url=args.base_url,
        model=args.model,
        system=_system_prompt(
            enable_facts=False,
            body_selection_pressure=args.body_selection_pressure,
            allow_compact_lines=args.allow_compact_lines,
        ),
        user=_with_no_think(user, args.no_think),
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        response_format="none",
        enable_facts=False,
    )
    model_s = time.perf_counter() - started
    clean, command_stats, parse_errors = _parse_commands(
        call["raw"],
        entity_candidates,
        evidence_candidates,
        allow_compact_lines=args.allow_compact_lines,
    )
    validation_errors, raw_counts, accepted, dropped, checks = _validate(
        clean, text, enable_facts=False
    )
    try:
        ExtractionResponse.model_validate(clean)
        jsonl = _object_to_jsonl(clean)
    except Exception as exc:
        validation_errors.append(f"pydantic_final:{type(exc).__name__}:{str(exc)[:160]}")
        jsonl = "{\"t\":\"x\"}"
    errors = parse_errors + validation_errors
    if call.get("finish_reason") == "length":
        errors.append("model_truncated")

    return {
        "id": sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"),
        **meta,
        "chunk_id": sample.get("chunk_id"),
        "doc_id": sample.get("doc_id"),
        "token_count": sample.get("token_count") or sample.get("tokens"),
        "entity_candidate_count": len(entity_candidates),
        "evidence_candidate_count": len(evidence_candidates),
        "entity_candidates": entity_candidates,
        "evidence_candidates": evidence_candidates,
        "model": {
            key: call.get(key)
            for key in (
                "latency_s",
                "finish_reason",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "completion_tok_s",
            )
        },
        "wall_s": model_s,
        "raw_first_1000": call["raw"][:1000],
        "command_stats": command_stats,
        "clean_object": clean,
        "jsonl": jsonl,
        "raw_counts": raw_counts,
        "accepted": accepted,
        "dropped": dropped,
        "checks": checks,
        "contract_pass": not errors,
        "errors": errors,
    }


def _pct(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * pct))]


def _summarize(args: argparse.Namespace, results: list[dict[str, Any]], wall_s: float) -> dict[str, Any]:
    samples = len(results)
    accepted_totals = {
        key: sum(int((r.get("accepted") or {}).get(key) or 0) for r in results)
        for key in ("entities", "relations", "facts")
    }
    command_totals: dict[str, int] = {}
    for r in results:
        for key, value in (r.get("command_stats") or {}).items():
            command_totals[key] = command_totals.get(key, 0) + int(value or 0)
    evidence_errors = sum(int((r.get("checks") or {}).get("evidence_errors") or 0) for r in results)
    invalid_enum_errors = sum(int((r.get("checks") or {}).get("invalid_enum_errors") or 0) for r in results)
    truncations = sum(1 for r in results for e in (r.get("errors") or []) if "truncated" in e)
    contract_pass = sum(1 for r in results if r.get("contract_pass"))
    model_latencies = [
        float((r.get("model") or {}).get("latency_s"))
        for r in results
        if isinstance((r.get("model") or {}).get("latency_s"), (float, int))
    ]
    tok_s = [
        float((r.get("model") or {}).get("completion_tok_s"))
        for r in results
        if isinstance((r.get("model") or {}).get("completion_tok_s"), (float, int))
    ]
    gate_failures: list[str] = []
    if contract_pass != samples:
        gate_failures.append("contract_pass_not_all_samples")
    if evidence_errors:
        gate_failures.append("evidence_errors_nonzero")
    if invalid_enum_errors:
        gate_failures.append("invalid_enum_errors_nonzero")
    if truncations:
        gate_failures.append("truncations_nonzero")
    if command_totals.get("invalid_lines", 0):
        gate_failures.append("invalid_command_lines_nonzero")
    if accepted_totals["relations"] == 0:
        gate_failures.append("accepted_relations_zero")
    return {
        "model": args.model,
        "samples": samples,
        "facts_enabled": False,
        "wall_s": wall_s,
        "chunks_per_hour_wall": samples / wall_s * 3600 if wall_s else None,
        "contract_pass": contract_pass,
        "contract_pass_rate": contract_pass / samples if samples else 0.0,
        "accepted_totals": accepted_totals,
        "accepted_entities_per_hour": accepted_totals["entities"] / wall_s * 3600 if wall_s else None,
        "accepted_relations_per_hour": accepted_totals["relations"] / wall_s * 3600 if wall_s else None,
        "command_totals": command_totals,
        "command_parse_rate": (
            command_totals.get("valid_lines", 0) / command_totals["raw_lines"]
            if command_totals.get("raw_lines")
            else None
        ),
        "evidence_errors": evidence_errors,
        "invalid_enum_errors": invalid_enum_errors,
        "truncation_count": truncations,
        "latency_p50_s": statistics.median(model_latencies) if model_latencies else None,
        "latency_p95_s": _pct(model_latencies, 0.95),
        "completion_tok_s_median": statistics.median(tok_s) if tok_s else None,
        "eligible_for_ghost_b": not gate_failures,
        "gate_failures": gate_failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--samples", default="/tmp/go_to_child_chunks_300_600.jsonl")
    parser.add_argument("--meta", default="/tmp/go_to_child_chunks_300_600_meta.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-entity-candidates", type=int, default=36)
    parser.add_argument("--max-evidence-candidates", type=int, default=18)
    parser.add_argument("--max-tokens", type=int, default=700)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--no-think", action="store_true")
    parser.add_argument("--body-selection-pressure", action="store_true")
    parser.add_argument("--allow-compact-lines", action="store_true")
    args = parser.parse_args()

    samples = [
        json.loads(line)
        for line in Path(args.samples).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta_by_id: dict[str, Any] = {}
    meta_path = Path(args.meta)
    if meta_path.exists():
        meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(meta_raw, list):
            meta_by_id = {
                str(item.get("id") or item.get("fixture_id") or item.get("chunk_id")): item
                for item in meta_raw
                if isinstance(item, dict)
            }

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(
            pool.map(
                lambda sample: _run_one(
                    args,
                    sample,
                    meta_by_id.get(
                        str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id")),
                        {},
                    ),
                ),
                samples,
            )
        )
    wall_s = time.perf_counter() - started
    summary = _summarize(args, results, wall_s)
    Path(args.out).write_text(
        json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print("SUMMARY", json.dumps(summary, indent=2))
    print(f"{'id':<10} {'tok':>4} {'cand':>7} {'cmd':>7} {'acc':>7} {'pass':>5} errors")
    for r in results:
        stats = r.get("command_stats") or {}
        accepted = r.get("accepted") or {}
        print(
            f"{r['id']:<10} {str(r.get('token_count') or '?'):>4} "
            f"{r.get('entity_candidate_count', 0)}/{r.get('evidence_candidate_count', 0):<3} "
            f"{stats.get('valid_lines', 0)}/{stats.get('invalid_lines', 0):<3} "
            f"{accepted.get('entities', 0)}/{accepted.get('relations', 0):<3} "
            f"{str(r.get('contract_pass')):>5} "
            f"{','.join((r.get('errors') or [])[:5])}"
        )
    print("wrote", args.out)


if __name__ == "__main__":
    main()
