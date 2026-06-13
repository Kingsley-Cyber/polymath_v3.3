#!/usr/bin/env python3
"""Benchmark a fused MLX extraction model against the Polymath contract.

This is for models fine-tuned to emit the full object:

    {"entities": [...], "relations": [...], "facts": [...]}

The model is still not trusted. Python parses, Pydantic-validates, applies
exact evidence gates, converts accepted items to compact Ghost B JSONL, and
scores entity/relation quality against the local gold fixture.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from mlx_lm import generate, load
from mlx_lm.sample_utils import make_sampler

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[0]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from autoresearch_polymath_local_extraction import (  # noqa: E402
    bad_surface,
    canonical,
    load_gold,
    load_samples,
    score_results_against_gold,
)
from services.ghost_b_schemas import ExtractionResponse  # noqa: E402


SYSTEM_PROMPT = """Extract entities, relations, and facts from TEXT. Return exactly one JSON object: {"entities":[...],"relations":[...],"facts":[...]} and nothing else (no markdown/prose).
entity_type one of: Person|Organization|Location|Event|Concept|Method|Product|Software|Document|Standard|Rule|Law|Artifact|TimeReference|other.
predicate one of: part_of|member_of|located_in|works_for|created_by|owns|affiliated_with|synonym_of|instance_of|example_of|uses|references|implements|depends_on|produces|stores|detects|supports|defines|represents|maps_to|preceded_by|causes|overlaps|during|derived_from|contradicts|excepts|overrides|related_to.
fact_type one of: property|status|timestamp|quantity|threshold|category|tag|rule_condition|rule_action.
canonical_name lowercase, punctuation stripped; confidence in [0,1]; evidence_phrase an exact substring of TEXT; relation subject/object reference an entity canonical_name; use the sentinel ('other'/'related_to') only when nothing else fits; never invent a type or predicate."""

CANON_RE = re.compile(r"^[a-z0-9][a-z0-9 ]{0,199}$")


def apply_chat_template(tokenizer: Any, system: str, user: str) -> str:
    if hasattr(tokenizer, "apply_chat_template"):
        return tokenizer.apply_chat_template(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tokenize=False,
            add_generation_prompt=True,
        )
    return f"<|system|>\n{system}\n<|user|>\n{user}\n<|assistant|>\n"


def extract_first_json_object(raw: str) -> tuple[dict[str, Any] | None, str]:
    text = str(raw or "").strip()
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj, ""
    return None, "no_json_object"


def _substring_ok(needle: str, haystack: str) -> bool:
    return bool(needle) and needle in haystack


def _entity_aliases(raw_name: str, clean_name: str, surface_form: str) -> set[str]:
    aliases = {
        raw_name,
        clean_name,
        canonical(raw_name),
        canonical(clean_name),
        canonical(surface_form),
    }
    return {item for item in aliases if item}


def gate_model_object(
    obj: dict[str, Any] | None,
    text: str,
    *,
    include_facts: bool,
) -> tuple[bool, dict[str, Any], dict[str, int], list[str]]:
    counters = {
        "raw_entities": 0,
        "raw_relations": 0,
        "raw_facts": 0,
        "accepted_entities": 0,
        "accepted_relations": 0,
        "accepted_facts": 0,
        "dropped_entities": 0,
        "dropped_relations": 0,
        "dropped_facts": 0,
    }
    errors: list[str] = []
    clean: dict[str, Any] = {"entities": [], "relations": [], "facts": []}
    if obj is None:
        return False, clean, counters, ["parse:no_json_object"]

    try:
        parsed = ExtractionResponse.model_validate(obj)
    except Exception as exc:  # noqa: BLE001
        return False, clean, counters, [f"pydantic:{type(exc).__name__}:{str(exc).splitlines()[0][:200]}"]

    counters["raw_entities"] = len(parsed.entities)
    counters["raw_relations"] = len(parsed.relations)
    counters["raw_facts"] = len(parsed.facts)

    entity_alias_to_clean: dict[str, str] = {}
    seen_entities: set[str] = set()
    for idx, entity in enumerate(parsed.entities):
        name = canonical(entity.canonical_name)
        if not name or not CANON_RE.match(name):
            errors.append(f"entity[{idx}].bad_canonical")
            counters["dropped_entities"] += 1
            continue
        if name in seen_entities:
            continue
        if not _substring_ok(entity.surface_form, text):
            errors.append(f"entity[{idx}].surface_not_substring")
            counters["dropped_entities"] += 1
            continue
        if bad_surface(entity.surface_form):
            errors.append(f"entity[{idx}].bad_surface")
            counters["dropped_entities"] += 1
            continue
        seen_entities.add(name)
        for alias in _entity_aliases(entity.canonical_name, name, entity.surface_form):
            entity_alias_to_clean[alias] = name
        clean["entities"].append(
            {
                "canonical_name": name,
                "surface_form": entity.surface_form,
                "entity_type": entity.entity_type,
                "confidence": entity.confidence,
                "query_aliases": entity.query_aliases[:5],
                "definitional_phrase": entity.definitional_phrase,
                "object_kind": entity.object_kind,
            }
        )
        counters["accepted_entities"] += 1

    seen_relations: set[tuple[str, str, str, str]] = set()
    for idx, rel in enumerate(parsed.relations):
        subject = entity_alias_to_clean.get(rel.subject) or entity_alias_to_clean.get(canonical(rel.subject))
        if not subject:
            errors.append(f"relation[{idx}].subject_not_entity")
            counters["dropped_relations"] += 1
            continue
        if not _substring_ok(rel.evidence_phrase, text):
            errors.append(f"relation[{idx}].evidence_not_substring")
            counters["dropped_relations"] += 1
            continue
        obj_value = canonical(rel.object) if rel.object_kind == "literal" else ""
        if rel.object_kind == "entity":
            obj_value = entity_alias_to_clean.get(rel.object) or entity_alias_to_clean.get(canonical(rel.object)) or ""
            if not obj_value:
                errors.append(f"relation[{idx}].object_not_entity")
                counters["dropped_relations"] += 1
                continue
        elif not obj_value:
            errors.append(f"relation[{idx}].empty_literal_object")
            counters["dropped_relations"] += 1
            continue
        key = (subject, rel.predicate, obj_value, rel.evidence_phrase)
        if key in seen_relations:
            continue
        seen_relations.add(key)
        clean["relations"].append(
            {
                "subject": subject,
                "predicate": rel.predicate,
                "object": obj_value,
                "object_kind": rel.object_kind,
                "confidence": rel.confidence,
                "evidence_phrase": rel.evidence_phrase,
                "relation_cue": rel.relation_cue,
            }
        )
        counters["accepted_relations"] += 1

    if include_facts:
        for idx, fact in enumerate(parsed.facts):
            subject = entity_alias_to_clean.get(fact.subject) or entity_alias_to_clean.get(canonical(fact.subject))
            if not subject:
                errors.append(f"fact[{idx}].subject_not_entity")
                counters["dropped_facts"] += 1
                continue
            if not _substring_ok(fact.evidence_phrase, text):
                errors.append(f"fact[{idx}].evidence_not_substring")
                counters["dropped_facts"] += 1
                continue
            clean["facts"].append(
                {
                    "subject": subject,
                    "fact_type": fact.fact_type,
                    "property_name": fact.property_name,
                    "value": fact.value,
                    "unit": fact.unit,
                    "condition": fact.condition,
                    "confidence": fact.confidence,
                    "evidence_phrase": fact.evidence_phrase,
                }
            )
            counters["accepted_facts"] += 1
    else:
        counters["dropped_facts"] += len(parsed.facts)

    return True, clean, counters, errors


def object_to_jsonl(obj: dict[str, Any], *, include_facts: bool) -> str:
    parsed = ExtractionResponse.model_validate(obj)
    lines: list[dict[str, Any]] = []
    for entity in parsed.entities:
        item: dict[str, Any] = {
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
    for rel in parsed.relations:
        lines.append(
            {
                "t": "r",
                "sub": rel.subject,
                "pred": rel.predicate,
                "obj": rel.object,
                "ok": rel.object_kind,
                "cf": rel.confidence,
                "ev": rel.evidence_phrase,
                "cue": rel.relation_cue,
            }
        )
    if include_facts:
        for fact in parsed.facts:
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


def run(args: argparse.Namespace) -> dict[str, Any]:
    load_started = time.perf_counter()
    model, tokenizer = load(str(args.model.expanduser()))
    load_s = time.perf_counter() - load_started
    sampler = make_sampler(temp=args.temperature)
    samples = load_samples(args.samples, args.limit)
    gold = load_gold(args.gold)
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    latencies: list[float] = []

    for sample in samples:
        sample_id = str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"))
        text = str(sample["text"])
        user = f"TEXT:\n{text}"
        prompt = apply_chat_template(tokenizer, SYSTEM_PROMPT, user)
        sample_started = time.perf_counter()
        raw = generate(
            model,
            tokenizer,
            prompt=prompt,
            max_tokens=args.max_tokens,
            sampler=sampler,
            verbose=False,
        )
        latency = time.perf_counter() - sample_started
        obj, parse_error = extract_first_json_object(raw)
        pydantic_ok, clean, gate_counts, gate_errors = gate_model_object(
            obj,
            text,
            include_facts=args.include_facts,
        )
        errors = ([f"parse:{parse_error}"] if parse_error else []) + gate_errors
        try:
            jsonl = object_to_jsonl(clean, include_facts=args.include_facts)
        except Exception as exc:  # noqa: BLE001
            jsonl = '{"t":"x"}'
            errors.append(f"jsonl:{type(exc).__name__}:{str(exc)[:160]}")
        prompt_tokens = len(tokenizer.encode(prompt)) if hasattr(tokenizer, "encode") else 0
        completion_tokens = len(tokenizer.encode(raw)) if hasattr(tokenizer, "encode") else 0
        latencies.append(latency)
        result = {
            "id": sample_id,
            "filename": sample.get("filename"),
            "prompt_variant": "mlx_fused_full_json",
            "candidate_mode": "direct_model_json_python_gate",
            "entity_candidate_count": 0,
            "evidence_candidate_count": 0,
            "relation_option_count": gate_counts["accepted_relations"],
            "entity_call": {
                "raw": raw,
                "latency_s": latency,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            "relation_call": {
                "raw": "same_full_json_call",
                "latency_s": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
            "entity_stats": {
                "raw_lines": gate_counts["raw_entities"],
                "valid_lines": gate_counts["accepted_entities"],
                "invalid_lines": gate_counts["dropped_entities"],
                "none_lines": 0,
            },
            "relation_stats": {
                "raw_lines": gate_counts["raw_relations"],
                "valid_lines": gate_counts["accepted_relations"],
                "invalid_lines": gate_counts["dropped_relations"],
                "none_lines": 0,
            },
            "clean_object": clean,
            "jsonl": jsonl,
            "accepted": {
                "entities": gate_counts["accepted_entities"],
                "relations": gate_counts["accepted_relations"],
                "facts": gate_counts["accepted_facts"],
            },
            "schema_ok": pydantic_ok,
            "errors": errors,
            "latency_s": latency,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "completion_tok_s": completion_tokens / latency if latency and completion_tokens else None,
            "truncated": completion_tokens >= args.max_tokens,
            "reasoning_tokens_seen": "<think>" in raw.lower(),
            "diagnostics": {
                "gate_counts": gate_counts,
                "raw_prefix": raw[:500],
                "raw_suffix": raw[-500:],
            },
        }
        results.append(result)
        print(
            f"{sample_id} rawE/R/F={gate_counts['raw_entities']}/{gate_counts['raw_relations']}/{gate_counts['raw_facts']} "
            f"acceptedE/R/F={gate_counts['accepted_entities']}/{gate_counts['accepted_relations']}/{gate_counts['accepted_facts']} "
            f"lat={latency:.2f}s errs={len(errors)}",
            flush=True,
        )

    wall_s = time.perf_counter() - started
    total_prompt = sum(int(item["prompt_tokens"] or 0) for item in results)
    total_completion = sum(int(item["completion_tokens"] or 0) for item in results)
    total_accepted_entities = sum(int(item["accepted"]["entities"]) for item in results)
    total_accepted_relations = sum(int(item["accepted"]["relations"]) for item in results)
    total_accepted_facts = sum(int(item["accepted"]["facts"]) for item in results)
    summary = {
        "model": str(args.model.expanduser()),
        "samples": len(results),
        "wall_s": wall_s,
        "model_load_s": load_s,
        "chunks_per_hour_wall": len(results) / wall_s * 3600 if wall_s else 0,
        "latency_p50_s": statistics.median(latencies) if latencies else None,
        "latency_p95_s": sorted(latencies)[int(len(latencies) * 0.95) - 1] if latencies else None,
        "schema_pass": sum(1 for item in results if item["schema_ok"]),
        "accepted_entities": total_accepted_entities,
        "accepted_relations": total_accepted_relations,
        "accepted_facts": total_accepted_facts,
        "accepted_entities_per_hour": total_accepted_entities / wall_s * 3600 if wall_s else 0,
        "accepted_relations_per_hour": total_accepted_relations / wall_s * 3600 if wall_s else 0,
        "prompt_tokens_total": total_prompt,
        "completion_tokens_total": total_completion,
        "completion_tok_s_median": statistics.median(
            [item["completion_tok_s"] for item in results if item["completion_tok_s"]]
        ) if any(item["completion_tok_s"] for item in results) else None,
        "truncation_count": sum(1 for item in results if item["truncated"]),
        "reasoning_responses": sum(1 for item in results if item["reasoning_tokens_seen"]),
        "error_count": sum(len(item["errors"]) for item in results),
        "include_facts": args.include_facts,
        "gold_score": score_results_against_gold(results, gold),
    }
    summary["gate_failures"] = []
    if summary["schema_pass"] != summary["samples"]:
        summary["gate_failures"].append("schema_not_100")
    if summary["truncation_count"]:
        summary["gate_failures"].append("truncations")
    if summary["accepted_relations"] == 0:
        summary["gate_failures"].append("accepted_relations_zero")
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "schema": "mlx_fused_extraction_contract_v1",
        "model": str(args.model.expanduser()),
        "samples_path": str(args.samples),
        "gold_path": str(args.gold),
        "payload": {"summary": summary, "results": results},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=Path("/Users/king/lfm2-ft/lfm2-1.2b-extract-ft"))
    parser.add_argument("--samples", type=Path, default=Path("scripts/local_extraction_fixtures/13_building_ai_mobile_apps_10_chunks.jsonl"))
    parser.add_argument("--gold", type=Path, default=Path("scripts/local_extraction_fixtures/13_building_ai_mobile_apps_gold_v1.json"))
    parser.add_argument("--out", type=Path, default=Path("/tmp/polymath_mlx_fused_extraction_model.json"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--include-facts", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run(args)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = report["payload"]["summary"]
    gold = summary["gold_score"]
    print("\nMLX FUSED EXTRACTION MODEL")
    print(f"model: {report['model']}")
    print(f"load_s: {summary['model_load_s']:.2f}")
    print(f"chunks/hr: {summary['chunks_per_hour_wall']:.1f}")
    print(f"schema: {summary['schema_pass']}/{summary['samples']}")
    print(f"accepted E/R/F: {summary['accepted_entities']}/{summary['accepted_relations']}/{summary['accepted_facts']}")
    print(
        "gold E/R/graph F1: "
        f"{gold['entity_f1']*100:.1f}% / "
        f"{gold['relation_f1']*100:.1f}% / "
        f"{gold['graph_f1']*100:.1f}%"
    )
    print(
        "gold relation TP/FP/FN: "
        f"{gold['relation_tp']}/{gold['relation_fp']}/{gold['relation_fn']}"
    )
    print(f"completion_tok_s_median: {summary['completion_tok_s_median']}")
    print(f"gate failures: {summary['gate_failures']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
