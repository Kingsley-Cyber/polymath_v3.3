#!/usr/bin/env python3
"""Linear local Ghost B extraction pipeline.

This is a speed-first research harness:

1. Python creates exact entity and evidence candidates.
2. A cheap entity model selects useful entity IDs.
3. Python constructs normalized entity objects.
4. Python creates Polymath-valid relation options.
5. A stronger schema model selects valid relation option IDs.
6. Python validates ExtractionResponse, evidence, and compact JSONL.

The models propose; Python owns truth and schema.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import time
from pathlib import Path
from typing import Any

from autoresearch_polymath_local_extraction import (
    DEFAULT_PORT,
    MODEL_REGISTRY as BASE_MODEL_REGISTRY,
    Candidate,
    MlxServer,
    build_object,
    call_chat,
    entity_candidates,
    evidence_candidates,
    format_xml_candidates,
    format_relation_options,
    infer_entity_type,
    load_gold,
    load_samples,
    object_to_jsonl,
    parse_entity_json,
    parse_relation_json,
    relation_options,
    score_results_against_gold,
    summarize_model,
    validate_object,
    with_no_think,
)


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[0]

MODEL_REGISTRY: dict[str, dict[str, Any]] = dict(BASE_MODEL_REGISTRY)
MODEL_REGISTRY.update(
    {
        "qwen3_06b": {
            "label": "Qwen3-0.6B-MLX-4bit",
            "model": "Qwen/Qwen3-0.6B-MLX-4bit",
            "path": (
                "/Users/king/.cache/huggingface/hub/"
                "models--Qwen--Qwen3-0.6B-MLX-4bit/"
                "snapshots/173234aa840d113125e9f2271100ddbaf16c9620"
            ),
            "no_think": True,
        }
    }
)


def linear_entity_system_prompt() -> str:
    return """You are the fast entity-selection worker in a Polymath extraction line.
Return only one JSON object. No markdown. No prose. No reasoning.

Task:
Select useful entity candidate IDs from the provided XML.

Output shape:
{"entities":[selected_candidate_ids]}

Rules:
- Use only E### IDs listed in entity_candidates.
- Select named entities, technical terms, methods, software, artifacts, documents, standards, and core concepts.
- These are dense textbook chunks. Usually select 8 to 18 IDs when useful candidates exist.
- Drop sentence fragments, vague phrases, duplicate variants, markup, citations, and publisher junk.
- Return {"entities":[]} only if every candidate is junk.
"""


def linear_schema_system_prompt() -> str:
    return """You are the schema-selection worker in a Polymath extraction line.
Return only one JSON object. No markdown. No prose. No reasoning.

Task:
Select relation option IDs whose subject, predicate, object, and evidence form useful textbook graph edges.

Output shape:
{"relations":[selected_relation_ids]}

Rules:
- Use only R### IDs listed in relation_options.
- Every option already has an allowed Polymath predicate and exact evidence.
- Select only options directly supported by evidence.
- Prefer useful semantic relations, not loose co-occurrence.
- Select at most 8 relation IDs.
- Return {"relations":[]} when every option is weak.
"""


def select_entities_with_model(
    *,
    args: argparse.Namespace,
    model: dict[str, Any],
    sample: dict[str, Any],
    candidates: list[Candidate],
) -> tuple[list[tuple[str, str]], dict[str, Any], dict[str, int], list[str]]:
    text = str(sample["text"])
    user = f"""<task>select_entity_ids</task>
<chunk><![CDATA[{text}]]></chunk>
{format_xml_candidates("entity_candidates", candidates)}
Return JSON with one key named entities. Its value must be an array of selected E### candidate IDs.
Do not include IDs not listed in entity_candidates.
Return JSON only.
"""
    call = call_chat(
        port=args.entity_port,
        model_name=model["model"],
        system=linear_entity_system_prompt(),
        user=with_no_think(user, model.get("no_think", False)),
        max_tokens=args.entity_max_tokens,
        timeout=args.timeout,
    )
    choices, stats, errors = parse_entity_json(call["raw"], candidates)
    return choices, call, stats, errors


def select_relations_with_model(
    *,
    args: argparse.Namespace,
    model: dict[str, Any],
    selected_entities: list[Candidate],
    evidence: list[Candidate],
    options: list[Any],
) -> tuple[list[str], dict[str, Any] | None, dict[str, int], list[str]]:
    stats = {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0, "none_lines": 0}
    if not options:
        stats["none_lines"] = 1
        return [], None, stats, []

    ent_by_id = {item.id: item for item in selected_entities}
    ev_by_id = {item.id: item for item in evidence}
    user = f"""<task>select_relation_ids</task>
RELATION OPTIONS:
{format_relation_options(options, ent_by_id, ev_by_id)}

Return JSON with one key named relations. Its value must be an array of selected R### relation IDs.
Use only R### IDs listed in RELATION OPTIONS.
Select at most 8 relation IDs.
Return JSON only.
"""
    call = call_chat(
        port=args.schema_port,
        model_name=model["model"],
        system=linear_schema_system_prompt(),
        user=with_no_think(user, model.get("no_think", False)),
        max_tokens=args.relation_max_tokens,
        timeout=args.timeout,
    )
    choices, stats, errors = parse_relation_json(call["raw"], options)
    return choices, call, stats, errors


def python_entity_choices(candidates: list[Candidate], limit: int) -> list[tuple[str, str]]:
    return [(item.id, infer_entity_type(item.text)) for item in candidates[:limit]]


def run_linear_pipeline(args: argparse.Namespace, samples: list[dict[str, Any]]) -> dict[str, Any]:
    entity_model = MODEL_REGISTRY[args.entity_model]
    schema_model = MODEL_REGISTRY[args.schema_model]
    entity_stage: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    started = time.perf_counter()
    if args.entity_stage == "model":
        print(f"entity stage: {entity_model['label']}", flush=True)
        with MlxServer(entity_model, args.entity_port):
            for idx, sample in enumerate(samples, 1):
                sample_id = str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"))
                candidates = entity_candidates(str(sample["text"]), args.max_entity_candidates)
                choices, call, stats, errors = select_entities_with_model(
                    args=args,
                    model=entity_model,
                    sample=sample,
                    candidates=candidates,
                )
                entity_stage[sample_id] = {
                    "candidates": candidates,
                    "choices": choices,
                    "call": call,
                    "stats": stats,
                    "errors": errors,
                }
                print(
                    f"E {idx:02d}/{len(samples)} {sample_id} "
                    f"keep={len(choices)}/{len(candidates)} "
                    f"lat={call['latency_s']:.2f}s errs={len(errors)}",
                    flush=True,
                )
    else:
        print("entity stage: python top candidates", flush=True)
        for sample in samples:
            sample_id = str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"))
            candidates = entity_candidates(str(sample["text"]), args.max_entity_candidates)
            choices = python_entity_choices(candidates, args.python_entity_keep)
            entity_stage[sample_id] = {
                "candidates": candidates,
                "choices": choices,
                "call": {
                    "raw": "python_entity_stage",
                    "reasoning": "",
                    "finish_reason": "stop",
                    "latency_s": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "completion_tok_s": None,
                },
                "stats": {
                    "raw_lines": 0,
                    "valid_lines": len(choices),
                    "invalid_lines": 0,
                    "none_lines": int(not choices),
                },
                "errors": [],
            }

    print(
        "schema stage: "
        + ("direct Python templates" if args.schema_stage == "direct" else schema_model["label"]),
        flush=True,
    )
    schema_context = nullcontext() if args.schema_stage == "direct" else MlxServer(schema_model, args.schema_port)
    with schema_context:
        for idx, sample in enumerate(samples, 1):
            text = str(sample["text"])
            sample_id = str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"))
            stage = entity_stage[sample_id]
            ent_candidates: list[Candidate] = stage["candidates"]
            entity_choices: list[tuple[str, str]] = stage["choices"]
            ent_by_id = {item.id: item for item in ent_candidates}
            selected_entities = [
                ent_by_id[entity_id]
                for entity_id, _ in entity_choices
                if entity_id in ent_by_id
            ]
            evidence = evidence_candidates(text, args.max_evidence_candidates)
            rel_options = relation_options(
                selected_entities,
                evidence,
                max_items=args.max_relation_options,
            )
            direct_choices = [item.id for item in rel_options if str(item.cue).startswith("direct_")]
            model_options = rel_options
            if args.schema_stage == "hybrid":
                direct_ids = set(direct_choices)
                model_options = [item for item in rel_options if item.id not in direct_ids]
            if args.schema_stage == "direct":
                relation_choices = direct_choices
                relation_call = {
                    "raw": "direct_python_schema_stage",
                    "reasoning": "",
                    "finish_reason": "stop",
                    "latency_s": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "completion_tok_s": None,
                }
                relation_stats = {
                    "raw_lines": 0,
                    "valid_lines": len(relation_choices),
                    "invalid_lines": 0,
                    "none_lines": int(not relation_choices),
                }
                relation_errors = []
            else:
                model_choices, relation_call, relation_stats, relation_errors = select_relations_with_model(
                    args=args,
                    model=schema_model,
                    selected_entities=selected_entities,
                    evidence=evidence,
                    options=model_options,
                )
                relation_choices = direct_choices + [
                    relation_id for relation_id in model_choices if relation_id not in set(direct_choices)
                ]
                if args.max_selected_relations:
                    relation_choices = relation_choices[: args.max_selected_relations]
            ev_by_id = {item.id: item for item in evidence}
            rel_by_id = {item.id: item for item in rel_options}
            if args.prune_entities_to_relation_endpoints and relation_choices:
                used_entity_ids: set[str] = set()
                for relation_id in relation_choices:
                    option = rel_by_id.get(relation_id)
                    if not option:
                        continue
                    used_entity_ids.add(option.subject_id)
                    used_entity_ids.add(option.object_id)
                entity_choices = [
                    (entity_id, entity_type)
                    for entity_id, entity_type in entity_choices
                    if entity_id in used_entity_ids
                ]
            clean = build_object(entity_choices, relation_choices, ent_by_id, rel_by_id, ev_by_id)
            schema_ok, accepted, validation_errors = validate_object(clean, text)
            try:
                jsonl = object_to_jsonl(clean)
            except Exception as exc:
                jsonl = '{"t":"x"}'
                validation_errors.append(f"jsonl:{type(exc).__name__}:{str(exc)[:120]}")

            entity_call = stage["call"]
            total_latency = float(entity_call["latency_s"]) + float((relation_call or {}).get("latency_s", 0))
            total_completion = int(entity_call["completion_tokens"]) + int(
                (relation_call or {}).get("completion_tokens") or 0
            )
            total_prompt = int(entity_call["prompt_tokens"]) + int(
                (relation_call or {}).get("prompt_tokens") or 0
            )
            result = {
                "id": sample_id,
                "filename": sample.get("filename"),
                "prompt_variant": "linear_entity_then_schema",
                "token_count": sample.get("token_count") or sample.get("tokens"),
                "entity_candidate_count": len(ent_candidates),
                "evidence_candidate_count": len(evidence),
                "relation_option_count": len(rel_options),
                "candidate_mode": "production_linear",
                "oracle_stats": {"enabled": False},
                "entity_candidates": [item.__dict__ for item in ent_candidates],
                "evidence_candidates": [item.__dict__ for item in evidence],
                "relation_options": [item.__dict__ for item in rel_options],
                "entity_call": entity_call,
                "relation_call": relation_call,
                "entity_raw": str(entity_call.get("raw") or "")[:1500],
                "relation_raw": str((relation_call or {}).get("raw") or "")[:1500],
                "entity_stats": stage["stats"],
                "relation_stats": relation_stats,
                "clean_object": clean,
                "jsonl": jsonl,
                "accepted": accepted,
                "schema_ok": schema_ok,
                "errors": stage["errors"] + relation_errors + validation_errors,
                "latency_s": total_latency,
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "completion_tok_s": total_completion / total_latency if total_latency and total_completion else None,
                "truncated": entity_call["finish_reason"] == "length"
                or bool(relation_call and relation_call["finish_reason"] == "length"),
                "reasoning_tokens_seen": bool(
                    entity_call.get("reasoning") or (relation_call or {}).get("reasoning")
                ),
            }
            results.append(result)
            print(
                f"R {idx:02d}/{len(samples)} {sample_id} "
                f"E/R={accepted['entities']}/{accepted['relations']} "
                f"opts={len(rel_options)} lat={total_latency:.2f}s errs={len(result['errors'])}",
                flush=True,
            )

    wall_s = time.perf_counter() - started
    summary = summarize_model(
        {
            "model": f"{entity_model['model']} -> {schema_model['model']}",
            "label": f"{entity_model['label']} -> {schema_model['label']}",
        },
        results,
        wall_s,
        prompt_variant="linear_entity_then_schema",
    )
    if args.gold_entries:
        summary["gold_score"] = score_results_against_gold(results, args.gold_entries)
    return {"summary": summary, "results": results}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, default=Path("/tmp/polymath_xml_json_test_chunks.jsonl"))
    parser.add_argument("--gold", type=Path, default=Path("/tmp/polymath_xml_json_gold_exact_v2.json"))
    parser.add_argument("--out", type=Path, default=Path("/tmp/polymath_linear_model_pipeline_report.json"))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--entity-stage", choices=["model", "python"], default="model")
    parser.add_argument("--schema-stage", choices=["model", "direct", "hybrid"], default="model")
    parser.add_argument("--entity-model", choices=sorted(MODEL_REGISTRY), default="qwen3_06b")
    parser.add_argument("--schema-model", choices=sorted(MODEL_REGISTRY), default="qwen3_4b_2507")
    parser.add_argument("--entity-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--schema-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--max-entity-candidates", type=int, default=96)
    parser.add_argument("--max-evidence-candidates", type=int, default=24)
    parser.add_argument("--max-relation-options", type=int, default=96)
    parser.add_argument("--python-entity-keep", type=int, default=96)
    parser.add_argument("--entity-max-tokens", type=int, default=180)
    parser.add_argument("--relation-max-tokens", type=int, default=220)
    parser.add_argument("--max-selected-relations", type=int, default=8)
    parser.add_argument("--prune-entities-to-relation-endpoints", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.gold_entries = load_gold(args.gold)
    samples = load_samples(args.samples, args.limit)
    report = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "schema": "services.ghost_b_schemas.ExtractionResponse",
        "pipeline": "linear_entity_then_schema",
        "facts_enabled": False,
        "samples_path": str(args.samples),
        "gold_path": str(args.gold),
        "sample_count": len(samples),
        "entity_stage": args.entity_stage,
        "entity_model": args.entity_model,
        "schema_model": args.schema_model,
        "payload": run_linear_pipeline(args, samples),
    }
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = report["payload"]["summary"]
    gold = summary.get("gold_score") or {}
    print("\nLINEAR PIPELINE SUMMARY")
    print(f"chunks/hr: {summary['chunks_per_hour_wall']:.1f}")
    print(f"tok/s median: {(summary['completion_tok_s_median'] or 0):.1f}")
    print(f"schema: {summary['schema_pass']}/{summary['samples']}")
    print(f"accepted E/R: {summary['accepted_entities']}/{summary['accepted_relations']}")
    if gold:
        print(
            "gold E/R/graph F1: "
            f"{gold['entity_f1']*100:.1f}% / {gold['relation_f1']*100:.1f}% / {gold['graph_f1']*100:.1f}%"
        )
    print(f"gate failures: {summary['gate_failures']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
