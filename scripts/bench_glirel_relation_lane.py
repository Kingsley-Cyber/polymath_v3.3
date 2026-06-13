#!/usr/bin/env python3
"""Benchmark GLiREL as a fast Polymath relation-scoring lane.

This deliberately keeps GLiREL outside the production Ghost B path. Python still:

1. Builds exact entity and evidence candidates.
2. Builds Polymath-valid relation options with exact evidence.
3. Lets GLiREL score only entity-pair/predicate plausibility.
4. Accepts only relation options that map back to Python-owned evidence.
5. Validates with ExtractionResponse and scores against the gold fixture.

The model can help choose; it cannot invent graph writes.
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

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[0]
sys.path.insert(0, str(SCRIPT_DIR))

from autoresearch_polymath_local_extraction import (  # noqa: E402
    Candidate,
    PREDICATES,
    build_object,
    entity_candidates,
    evidence_candidates,
    infer_entity_type,
    load_gold,
    load_samples,
    object_to_jsonl,
    relation_options,
    score_results_against_gold,
    summarize_model,
    validate_object,
)
from bench_local_draft_schema_pipeline import standalone_importance_score  # noqa: E402


RAW_LABEL_MAP = {predicate: predicate for predicate in PREDICATES}
NATURAL_LABEL_MAP = {
    "part of": "part_of",
    "member of": "member_of",
    "located in": "located_in",
    "works for": "works_for",
    "created by": "created_by",
    "owns": "owns",
    "affiliated with": "affiliated_with",
    "synonym of": "synonym_of",
    "is an instance of": "instance_of",
    "is an example of": "example_of",
    "uses": "uses",
    "references": "references",
    "implements": "implements",
    "depends on": "depends_on",
    "produces": "produces",
    "stores": "stores",
    "detects": "detects",
    "supports": "supports",
    "defines": "defines",
    "represents": "represents",
    "maps to": "maps_to",
    "precedes": "preceded_by",
    "causes": "causes",
    "overlaps": "overlaps",
    "during": "during",
    "derived from": "derived_from",
    "contradicts": "contradicts",
    "excepts": "excepts",
    "overrides": "overrides",
    "related to": "related_to",
}


def load_glirel(model_id: str, *, device: str):
    from glirel import GLiREL

    # GLiREL 1.2.1 currently has a PyTorchModelHubMixin wrapper mismatch with
    # newer huggingface_hub. Calling the implementation directly passes the
    # required keyword-only args and keeps this benchmark reproducible.
    model = GLiREL._from_pretrained(
        model_id=model_id,
        revision=None,
        cache_dir=None,
        force_download=False,
        proxies=None,
        resume_download=False,
        local_files_only=False,
        token=None,
        map_location=device,
        strict=False,
    )
    model.eval()
    return model


def token_spans(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    tokens: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"\w+(?:[-_]\w+)*|\S", text):
        tokens.append(match.group())
        spans.append((match.start(), match.end()))
    return tokens, spans


def char_to_token_span(
    spans: list[tuple[int, int]],
    start: int,
    end: int,
) -> tuple[int, int] | None:
    token_indexes = [idx for idx, (tok_start, tok_end) in enumerate(spans) if tok_start >= start and tok_end <= end]
    if not token_indexes:
        return None
    return token_indexes[0], token_indexes[-1]


def ner_from_entities(
    text: str,
    tokens: list[str],
    spans: list[tuple[int, int]],
    entities: list[Candidate],
) -> tuple[list[list[Any]], dict[tuple[int, int], str]]:
    del tokens
    ner: list[list[Any]] = []
    span_to_entity_id: dict[tuple[int, int], str] = {}
    used: set[tuple[int, int]] = set()
    for entity in entities:
        start = text.find(entity.text)
        if start < 0:
            continue
        token_span = char_to_token_span(spans, start, start + len(entity.text))
        if token_span is None or token_span in used:
            continue
        used.add(token_span)
        ner.append([token_span[0], token_span[1], infer_entity_type(entity.text), entity.text])
        # GLiREL returns end-exclusive positions.
        span_to_entity_id[(token_span[0], token_span[1] + 1)] = entity.id
    return ner, span_to_entity_id


def prepare_sample(args: argparse.Namespace, sample: dict[str, Any]) -> dict[str, Any]:
    text = str(sample["text"])
    ent_candidates = entity_candidates(text, args.max_entity_candidates)
    entity_choices = [(item.id, infer_entity_type(item.text)) for item in ent_candidates[: args.python_entity_keep]]
    ent_by_id = {item.id: item for item in ent_candidates}
    selected_entities = [ent_by_id[entity_id] for entity_id, _ in entity_choices if entity_id in ent_by_id]
    evidence = evidence_candidates(text, args.max_evidence_candidates)
    rel_options = relation_options(selected_entities, evidence, max_items=args.max_relation_options)
    tokens, spans = token_spans(text)
    endpoint_ids = {item.subject_id for item in rel_options} | {item.object_id for item in rel_options}
    endpoint_entities = [item for item in selected_entities if item.id in endpoint_ids]
    ner, span_to_entity_id = ner_from_entities(text, tokens, spans, endpoint_entities)
    return {
        "sample": sample,
        "text": text,
        "entity_candidates": ent_candidates,
        "entity_choices": entity_choices,
        "selected_entities": selected_entities,
        "evidence": evidence,
        "relation_options": rel_options,
        "tokens": tokens,
        "ner": ner,
        "span_to_entity_id": span_to_entity_id,
    }


def score_predictions(
    predictions: list[dict[str, Any]],
    span_to_entity_id: dict[tuple[int, int], str],
    label_map: dict[str, str],
) -> dict[tuple[str, str, str], float]:
    scores: dict[tuple[str, str, str], float] = {}
    for pred in predictions:
        subject_id = span_to_entity_id.get(tuple(pred.get("head_pos") or []))
        object_id = span_to_entity_id.get(tuple(pred.get("tail_pos") or []))
        predicate = label_map.get(str(pred.get("label") or ""))
        if not subject_id or not object_id or not predicate:
            continue
        key = (subject_id, predicate, object_id)
        score = float(pred.get("score") or 0.0)
        scores[key] = max(scores.get(key, 0.0), score)
    return scores


def build_results_for_threshold(
    *,
    prepared: list[dict[str, Any]],
    prediction_scores: dict[str, dict[tuple[str, str, str], float]],
    threshold: float,
    auto_direct: bool,
    prune_entities: bool,
    keep_standalone_entities: int,
    standalone_importance_threshold: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in prepared:
        sample = item["sample"]
        sample_id = str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"))
        rel_options = item["relation_options"]
        rel_by_id = {option.id: option for option in rel_options}
        ev_by_id = {ev.id: ev for ev in item["evidence"]}
        ent_by_id = {ent.id: ent for ent in item["entity_candidates"]}
        scores = prediction_scores.get(sample_id) or {}
        choices: list[str] = []
        for option in rel_options:
            direct = str(option.cue).startswith("direct_")
            scored = scores.get((option.subject_id, option.predicate, option.object_id), 0.0) >= threshold
            if (auto_direct and direct) or scored:
                choices.append(option.id)

        entity_choices = list(item["entity_choices"])
        if prune_entities:
            used_entity_ids: set[str] = set()
            for relation_id in choices:
                option = rel_by_id[relation_id]
                used_entity_ids.add(option.subject_id)
                used_entity_ids.add(option.object_id)
            endpoint_choices = [
                (entity_id, entity_type)
                for entity_id, entity_type in entity_choices
                if entity_id in used_entity_ids
            ]
            if keep_standalone_entities > 0:
                candidate_by_id = {candidate.id: candidate for candidate in item["entity_candidates"]}
                scored_standalone: list[tuple[float, int, tuple[str, str]]] = []
                for idx, (entity_id, entity_type) in enumerate(entity_choices):
                    if entity_id in used_entity_ids:
                        continue
                    candidate = candidate_by_id.get(entity_id)
                    if not candidate:
                        continue
                    importance = standalone_importance_score(candidate, item["text"])
                    if importance < standalone_importance_threshold:
                        continue
                    scored_standalone.append((importance, -idx, (entity_id, entity_type)))
                scored_standalone.sort(reverse=True)
                standalone_choices = [
                    choice
                    for _, _, choice in scored_standalone[:keep_standalone_entities]
                ]
                entity_choices = endpoint_choices + standalone_choices
            else:
                entity_choices = endpoint_choices

        clean = build_object(entity_choices, choices, ent_by_id, rel_by_id, ev_by_id)
        schema_ok, accepted, errors = validate_object(clean, item["text"])
        try:
            jsonl = object_to_jsonl(clean)
        except Exception as exc:
            jsonl = '{"t":"x"}'
            errors.append(f"jsonl:{type(exc).__name__}:{str(exc)[:120]}")
        results.append(
            {
                "id": sample_id,
                "filename": sample.get("filename"),
                "prompt_variant": "glirel_relation_lane",
                "token_count": sample.get("token_count") or sample.get("tokens"),
                "entity_candidate_count": len(item["entity_candidates"]),
                "evidence_candidate_count": len(item["evidence"]),
                "relation_option_count": len(rel_options),
                "candidate_mode": "glirel_scores_python_options",
                "oracle_stats": {"enabled": False},
                "entity_candidates": [entity.__dict__ for entity in item["entity_candidates"]],
                "evidence_candidates": [ev.__dict__ for ev in item["evidence"]],
                "relation_options": [option.__dict__ for option in rel_options],
                "entity_call": {"raw": "python_entity_stage", "latency_s": 0, "prompt_tokens": 0, "completion_tokens": 0},
                "relation_call": {
                    "raw": "glirel_relation_scores",
                    "latency_s": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "finish_reason": "stop",
                },
                "entity_raw": "python_entity_stage",
                "relation_raw": "glirel_relation_scores",
                "entity_stats": {
                    "raw_lines": 0,
                    "valid_lines": len(entity_choices),
                    "invalid_lines": 0,
                    "none_lines": int(not entity_choices),
                },
                "relation_stats": {
                    "raw_lines": 0,
                    "valid_lines": len(choices),
                    "invalid_lines": 0,
                    "none_lines": int(not choices),
                },
                "clean_object": clean,
                "jsonl": jsonl,
                "accepted": accepted,
                "schema_ok": schema_ok,
                "errors": errors,
                "latency_s": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "completion_tok_s": None,
                "truncated": False,
                "reasoning_tokens_seen": False,
            }
        )
    return results


def run_predictions(
    *,
    model: Any,
    prepared: list[dict[str, Any]],
    labels: list[str],
    mode: str,
    threshold: float,
    top_k: int,
) -> tuple[dict[str, list[dict[str, Any]]], float, list[float]]:
    started = time.perf_counter()
    latencies: list[float] = []
    outputs: dict[str, list[dict[str, Any]]] = {}
    eligible: list[tuple[str, dict[str, Any]]] = []
    for item in prepared:
        sample_id = str(item["sample"].get("id") or item["sample"].get("fixture_id") or item["sample"].get("chunk_id"))
        if len(item["ner"]) < 2:
            outputs[sample_id] = []
            latencies.append(0.0)
            continue
        eligible.append((sample_id, item))

    if mode == "batch":
        if not eligible:
            return outputs, time.perf_counter() - started, latencies
        sample_ids = [sample_id for sample_id, _ in eligible]
        eligible_items = [item for _, item in eligible]
        call_start = time.perf_counter()
        batch_outputs = model.batch_predict_relations(
            [item["tokens"] for item in eligible_items],
            labels,
            threshold=threshold,
            ner=[item["ner"] for item in eligible_items],
            top_k=top_k,
        )
        elapsed = time.perf_counter() - call_start
        per_sample = elapsed / max(1, len(eligible_items))
        for sample_id, sample_output in zip(sample_ids, batch_outputs, strict=False):
            outputs[sample_id] = sample_output
            latencies.append(per_sample)
    else:
        for sample_id, item in eligible:
            call_start = time.perf_counter()
            outputs[sample_id] = model.predict_relations(
                item["tokens"],
                labels,
                threshold=threshold,
                ner=item["ner"],
                top_k=top_k,
            )
            latencies.append(time.perf_counter() - call_start)
    return outputs, time.perf_counter() - started, latencies


def summarize_lane(
    *,
    label: str,
    model_id: str,
    results: list[dict[str, Any]],
    wall_s: float,
    gold: dict[str, Any],
    inference_latencies: list[float],
) -> dict[str, Any]:
    summary = summarize_model(
        {"model": model_id, "label": label},
        results,
        wall_s,
        prompt_variant="glirel_relation_lane",
    )
    summary["gold_score"] = score_results_against_gold(results, gold) if gold else {}
    summary["inference_latency_p50_s"] = statistics.median(inference_latencies) if inference_latencies else None
    summary["inference_latency_p95_s"] = (
        sorted(inference_latencies)[min(len(inference_latencies) - 1, int(len(inference_latencies) * 0.95))]
        if inference_latencies
        else None
    )
    return summary


def parse_thresholds(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, default=Path("/tmp/polymath_xml_json_test_chunks.jsonl"))
    parser.add_argument("--gold", type=Path, default=Path("/tmp/polymath_xml_json_gold_exact_v2.json"))
    parser.add_argument("--out", type=Path, default=Path("/tmp/polymath_glirel_relation_lane_report.json"))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--model-id", default="jackboyla/glirel-large-v0")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--label-profile", choices=["raw", "natural"], default="natural")
    parser.add_argument("--prediction-mode", choices=["sequential", "batch"], default="batch")
    parser.add_argument("--thresholds", default="0.05,0.10,0.15,0.20,0.25,0.30,0.35,0.40,0.45")
    parser.add_argument("--model-threshold", type=float, default=0.0)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--max-entity-candidates", type=int, default=160)
    parser.add_argument("--python-entity-keep", type=int, default=120)
    parser.add_argument("--max-evidence-candidates", type=int, default=32)
    parser.add_argument("--max-relation-options", type=int, default=96)
    parser.add_argument("--auto-direct", action="store_true")
    parser.add_argument("--prune-entities-to-relation-endpoints", action="store_true")
    parser.add_argument("--keep-standalone-entities", type=int, default=0)
    parser.add_argument("--standalone-importance-threshold", type=float, default=0.70)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    samples = load_samples(args.samples, args.limit)
    gold = load_gold(args.gold)
    label_map = NATURAL_LABEL_MAP if args.label_profile == "natural" else RAW_LABEL_MAP
    labels = list(label_map)

    print(f"Preparing {len(samples)} samples", flush=True)
    prepared = [prepare_sample(args, sample) for sample in samples]
    print(f"Loading {args.model_id} on {args.device}", flush=True)
    model = load_glirel(args.model_id, device=args.device)
    print(f"Predicting relations: profile={args.label_profile} mode={args.prediction_mode}", flush=True)
    raw_outputs, inference_wall_s, latencies = run_predictions(
        model=model,
        prepared=prepared,
        labels=labels,
        mode=args.prediction_mode,
        threshold=args.model_threshold,
        top_k=args.top_k,
    )

    prediction_scores: dict[str, dict[tuple[str, str, str], float]] = {}
    for item in prepared:
        sample_id = str(item["sample"].get("id") or item["sample"].get("fixture_id") or item["sample"].get("chunk_id"))
        prediction_scores[sample_id] = score_predictions(
            raw_outputs.get(sample_id) or [],
            item["span_to_entity_id"],
            label_map,
        )

    reports: list[dict[str, Any]] = []
    for threshold in parse_thresholds(args.thresholds):
        started = time.perf_counter()
        results = build_results_for_threshold(
            prepared=prepared,
            prediction_scores=prediction_scores,
            threshold=threshold,
            auto_direct=args.auto_direct,
            prune_entities=args.prune_entities_to_relation_endpoints,
            keep_standalone_entities=args.keep_standalone_entities,
            standalone_importance_threshold=args.standalone_importance_threshold,
        )
        wall_s = inference_wall_s + (time.perf_counter() - started)
        summary = summarize_lane(
            label=f"GLiREL {args.label_profile} threshold={threshold}",
            model_id=args.model_id,
            results=results,
            wall_s=wall_s,
            gold=gold,
            inference_latencies=latencies,
        )
        summary["threshold"] = threshold
        summary["auto_direct"] = args.auto_direct
        summary["prediction_mode"] = args.prediction_mode
        reports.append({"summary": summary, "results": results})

    best = max(
        reports,
        key=lambda report: (
            (report["summary"].get("gold_score") or {}).get("relation_f1", 0),
            (report["summary"].get("gold_score") or {}).get("entity_f1", 0),
            report["summary"].get("accepted_relations") or 0,
        ),
    )
    output = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "schema": "services.ghost_b_schemas.ExtractionResponse",
        "pipeline": "glirel_scores_python_relation_options",
        "samples_path": str(args.samples),
        "gold_path": str(args.gold),
        "label_profile": args.label_profile,
        "prediction_mode": args.prediction_mode,
        "model_threshold": args.model_threshold,
        "top_k": args.top_k,
        "sample_count": len(samples),
        "best_summary": best["summary"],
        "reports": reports,
    }
    args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = best["summary"]
    gold_score = summary.get("gold_score") or {}
    print("\nGLiREL RELATION LANE SUMMARY")
    print(f"best threshold: {summary['threshold']}")
    print(f"mode/profile: {args.prediction_mode}/{args.label_profile} auto_direct={args.auto_direct}")
    print(f"chunks/hr: {summary['chunks_per_hour_wall']:.1f}")
    print(f"inference p50/p95: {summary['inference_latency_p50_s']:.3f}s / {summary['inference_latency_p95_s']:.3f}s")
    print(f"schema: {summary['schema_pass']}/{summary['samples']}")
    print(f"accepted E/R: {summary['accepted_entities']}/{summary['accepted_relations']}")
    print(
        "gold E/R/graph F1: "
        f"{gold_score.get('entity_f1', 0)*100:.1f}% / "
        f"{gold_score.get('relation_f1', 0)*100:.1f}% / "
        f"{gold_score.get('graph_f1', 0)*100:.1f}%"
    )
    print(
        "gold relation TP/FP/FN: "
        f"{gold_score.get('relation_tp', 0)}/"
        f"{gold_score.get('relation_fp', 0)}/"
        f"{gold_score.get('relation_fn', 0)}"
    )
    print(f"gate failures: {summary['gate_failures']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
