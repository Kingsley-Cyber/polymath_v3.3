#!/usr/bin/env python3
"""Benchmark an MLX small-language-model relation proposer.

The model is not trusted as a graph writer. It sees one sentence and two
Python-owned entity spans, then emits exactly:

    1 <predicate> 2
    2 <predicate> 1
    none

Python builds the ExtractionResponse object, validates evidence, and converts
to compact Ghost B JSONL.
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
sys.path.insert(0, str(SCRIPT_DIR))

from autoresearch_polymath_local_extraction import (  # noqa: E402
    Candidate,
    RelationOption,
    build_object,
    candidate_score,
    canonical,
    gold_entity_labels,
    infer_entity_type,
    load_gold,
    load_samples,
    norm_key,
    object_to_jsonl,
    raw_sentence_spans,
    score_results_against_gold,
    summarize_model,
    validate_object,
)
from bench_python_deterministic_relation_compiler import (  # noqa: E402
    bad_relation_endpoint_surface,
    build_current_candidates,
    candidate_positions,
)


MODEL_REGISTRY: dict[str, str] = {
    "qwen25_15b": "mlx-community/Qwen2.5-1.5B-Instruct-4bit",
    "qwen3_17b": "Qwen/Qwen3-1.7B-MLX-4bit",
    "llama32_1b": "mlx-community/Llama-3.2-1B-Instruct-4bit",
}

PREDICATES = [
    "uses",
    "supports",
    "produces",
    "depends_on",
    "causes",
    "stores",
    "detects",
    "references",
    "example_of",
    "located_in",
    "part_of",
    "synonym_of",
    "represents",
    "maps_to",
    "related_to",
    "implements",
]

SYSTEM_PROMPT = """You are Polymath Relation Proposer.
You do not write JSON or graph objects.
You only classify the relation between two marked entities in one sentence.

Output exactly one line:
1 <predicate> 2
2 <predicate> 1
none

Allowed predicates:
uses, supports, produces, depends_on, causes, stores, detects, references, example_of, located_in, part_of, synonym_of, represents, maps_to, related_to, implements

Rules:
- Use "none" if the sentence does not explicitly support a relation.
- Use only the allowed predicate labels.
- Do not explain.
"""

FEW_SHOT = """Examples:
Sentence: RuntimeKit provides an inference runtime and GPU delegate.
Entity 1: RuntimeKit
Entity 2: inference runtime
Answer: 1 supports 2

Sentence: Compression converts model weights to 8-bit integers.
Entity 1: Compression
Entity 2: 8-bit integers
Answer: 1 maps_to 2

Sentence: Alpha chips are neural processing units.
Entity 1: Alpha chips
Entity 2: neural processing units
Answer: 1 example_of 2

Sentence: ModelHub hosts optimized mobile models.
Entity 1: ModelHub
Entity 2: optimized mobile models
Answer: 1 stores 2

Sentence: Flutter and Kotlin are listed in the same heading.
Entity 1: Flutter
Entity 2: Kotlin
Answer: none
"""


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


def clean_model_output(raw: str) -> tuple[str | None, str | None]:
    text = str(raw or "").strip().lower()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = first_line.strip(" .`'\"")
    if first_line == "none":
        return None, None
    match = re.fullmatch(r"([12])\s+([a-z_]+)\s+([12])", first_line)
    if not match:
        # Salvage common near-miss forms like "1 uses 2." or "relation: uses".
        loose = re.search(r"\b([12])\s+([a-z_]+)\s+([12])\b", first_line)
        if not loose:
            pred = next((item for item in PREDICATES if re.search(rf"\b{re.escape(item)}\b", first_line)), None)
            return ("1", pred) if pred else (None, None)
        match = loose
    left, predicate, right = match.group(1), match.group(2), match.group(3)
    if predicate not in PREDICATES or left == right:
        return None, None
    return left, predicate


def endpoint_ok(candidate: Candidate) -> bool:
    key = norm_key(candidate.text)
    if not key:
        return False
    if bad_relation_endpoint_surface(candidate.text):
        # Keep a few gold-relevant simple endpoints that the strict spaCy path
        # rejected too aggressively.
        if key not in {"users", "cloud", "memory", "device"}:
            return False
    if len(key.split()) > 6:
        return False
    return True


def span_rank(candidate: Candidate, sentence: str) -> float:
    key = norm_key(candidate.text)
    tokens = key.split()
    score = candidate_score(candidate.text, sentence)
    score += min(8.0, len(candidate.text) / 8.0)
    score += 5.0 if len(tokens) >= 2 else 0.0
    if len(tokens) == 1 and key in {"api", "ai", "app", "model", "data"}:
        score -= 7.0
    return score


def resolve_sentence_candidates(
    sentence: str,
    candidates: list[Candidate],
    *,
    max_entities: int,
) -> list[Candidate]:
    present = [
        (candidate, start, end)
        for candidate, start, end in candidate_positions(sentence, candidates)
        if endpoint_ok(candidate) and candidate_score(candidate.text, sentence) >= 1
    ]
    present.sort(key=lambda item: (-span_rank(item[0], sentence), item[1], -(item[2] - item[1])))
    selected: list[tuple[Candidate, int, int]] = []
    for candidate, start, end in present:
        if any(max(start, s) < min(end, e) for _, s, e in selected):
            continue
        selected.append((candidate, start, end))
        if len(selected) >= max_entities:
            break
    selected.sort(key=lambda item: item[1])
    return [item[0] for item in selected]


def relation_pair_allowed(a: Candidate, b: Candidate) -> bool:
    ka = norm_key(a.text)
    kb = norm_key(b.text)
    if not ka or not kb or ka == kb:
        return False
    if ka in kb or kb in ka:
        return False
    return True


def build_pair_prompt(sentence: str, a: Candidate, b: Candidate, *, few_shot: bool) -> str:
    prefix = FEW_SHOT + "\n" if few_shot else ""
    return f"""{prefix}Now classify this pair.

Sentence: {sentence}
Entity 1: {a.text}
Entity 2: {b.text}
Answer:"""


def propose_pair(
    *,
    model: Any,
    tokenizer: Any,
    sampler: Any,
    sentence: str,
    a: Candidate,
    b: Candidate,
    few_shot: bool,
    max_tokens: int,
) -> dict[str, Any]:
    user = build_pair_prompt(sentence, a, b, few_shot=few_shot)
    prompt = apply_chat_template(tokenizer, SYSTEM_PROMPT, user)
    started = time.perf_counter()
    raw = generate(
        model,
        tokenizer,
        prompt=prompt,
        max_tokens=max_tokens,
        sampler=sampler,
        verbose=False,
    )
    latency = time.perf_counter() - started
    direction, predicate = clean_model_output(raw)
    prompt_tokens = len(tokenizer.encode(prompt)) if hasattr(tokenizer, "encode") else None
    completion_tokens = len(tokenizer.encode(raw)) if hasattr(tokenizer, "encode") else None
    return {
        "raw": raw,
        "direction": direction,
        "predicate": predicate,
        "latency_s": latency,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def compile_sample(
    *,
    sample: dict[str, Any],
    gold_entry: dict[str, Any],
    model: Any,
    tokenizer: Any,
    sampler: Any,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any]]:
    text = str(sample["text"])
    labels = gold_entity_labels(gold_entry) if args.oracle_entities else []
    candidates = build_current_candidates(
        text,
        max_candidates=args.max_entity_candidates,
        include_labels=labels,
    )
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    entity_choices_by_id: dict[str, tuple[str, str]] = {}
    relation_options_by_id: dict[str, RelationOption] = {}
    evidence_by_id: dict[str, Candidate] = {}
    relation_choices: list[str] = []
    seen_relations: set[tuple[str, str, str, str]] = set()
    pair_calls: list[dict[str, Any]] = []
    total_pairs = 0
    skipped_pairs = 0

    def ensure_entity(candidate: Candidate) -> None:
        entity_choices_by_id[candidate.id] = (candidate.id, infer_entity_type(candidate.text))

    def add_relation(subject: Candidate, predicate: str, obj: Candidate, sentence: str, raw: str) -> None:
        if len(relation_choices) >= args.max_relation_options:
            return
        if not relation_pair_allowed(subject, obj):
            return
        evidence = next((item for item in evidence_by_id.values() if item.text == sentence), None)
        if not evidence:
            evidence = Candidate(f"EV{len(evidence_by_id) + 1:03d}", sentence[:500])
            evidence_by_id[evidence.id] = evidence
        key = (canonical(subject.text), predicate, canonical(obj.text), evidence.id)
        if key in seen_relations:
            return
        seen_relations.add(key)
        ensure_entity(subject)
        ensure_entity(obj)
        relation_id = f"R{len(relation_options_by_id) + 1:03d}"
        relation_options_by_id[relation_id] = RelationOption(
            id=relation_id,
            subject_id=subject.id,
            predicate=predicate,
            object_id=obj.id,
            evidence_id=evidence.id,
            cue=f"slm_pair:{raw.strip()[:60]}",
        )
        relation_choices.append(relation_id)

    for sentence in raw_sentence_spans(text):
        sentence_entities = resolve_sentence_candidates(
            sentence,
            candidates,
            max_entities=args.max_sentence_entities,
        )
        if len(sentence_entities) < 2:
            continue
        for idx, a in enumerate(sentence_entities):
            for b in sentence_entities[idx + 1 :]:
                if total_pairs >= args.max_pairs_per_chunk:
                    break
                if not relation_pair_allowed(a, b):
                    skipped_pairs += 1
                    continue
                total_pairs += 1
                call = propose_pair(
                    model=model,
                    tokenizer=tokenizer,
                    sampler=sampler,
                    sentence=sentence,
                    a=a,
                    b=b,
                    few_shot=args.few_shot,
                    max_tokens=args.max_tokens,
                )
                call["a"] = a.text
                call["b"] = b.text
                pair_calls.append(call)
                predicate = call["predicate"]
                direction = call["direction"]
                if not predicate or not direction:
                    continue
                subject, obj = (a, b) if direction == "1" else (b, a)
                add_relation(subject, predicate, obj, sentence, str(call["raw"]))
            if total_pairs >= args.max_pairs_per_chunk:
                break

    used_ids = set(entity_choices_by_id)
    standalone: list[tuple[float, Candidate]] = []
    for candidate in candidates:
        if candidate.id in used_ids:
            continue
        standalone.append((candidate_score(candidate.text, text), candidate))
    standalone.sort(key=lambda item: (-item[0], len(item[1].text), item[1].text))
    for _, candidate in standalone[: args.keep_standalone_entities]:
        ensure_entity(candidate)

    clean = build_object(
        list(entity_choices_by_id.values()),
        relation_choices,
        candidate_by_id,
        relation_options_by_id,
        evidence_by_id,
    )
    diagnostics = {
        "candidate_count": len(candidates),
        "pair_calls": len(pair_calls),
        "total_pairs_seen": total_pairs,
        "skipped_pairs": skipped_pairs,
        "compiled_entities": len(entity_choices_by_id),
        "compiled_relations": len(relation_choices),
        "raw_positive_calls": sum(1 for call in pair_calls if call.get("predicate")),
        "avg_pair_latency_s": statistics.mean([call["latency_s"] for call in pair_calls]) if pair_calls else 0,
        "completion_tokens": sum(int(call.get("completion_tokens") or 0) for call in pair_calls),
        "prompt_tokens": sum(int(call.get("prompt_tokens") or 0) for call in pair_calls),
        "pair_call_examples": pair_calls[: args.keep_pair_examples],
    }
    return clean, diagnostics


def run(args: argparse.Namespace) -> dict[str, Any]:
    model_id = MODEL_REGISTRY.get(args.model, args.model)
    load_started = time.perf_counter()
    load_kwargs: dict[str, Any] = {}
    if args.adapter_path:
        load_kwargs["adapter_path"] = str(args.adapter_path.expanduser())
    model, tokenizer = load(model_id, **load_kwargs)
    load_s = time.perf_counter() - load_started
    sampler = make_sampler(temp=args.temperature)

    samples = load_samples(args.samples, args.limit)
    gold = load_gold(args.gold)
    started = time.perf_counter()
    latencies: list[float] = []
    results: list[dict[str, Any]] = []

    for sample in samples:
        sample_started = time.perf_counter()
        sample_id = str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"))
        clean, diagnostics = compile_sample(
            sample=sample,
            gold_entry=gold.get(sample_id) or {},
            model=model,
            tokenizer=tokenizer,
            sampler=sampler,
            args=args,
        )
        text = str(sample["text"])
        schema_ok, accepted, errors = validate_object(clean, text)
        try:
            jsonl = object_to_jsonl(clean)
        except Exception as exc:
            jsonl = '{"t":"x"}'
            errors.append(f"jsonl:{type(exc).__name__}:{str(exc)[:120]}")
        latency = time.perf_counter() - sample_started
        latencies.append(latency)
        results.append(
            {
                "id": sample_id,
                "filename": sample.get("filename"),
                "prompt_variant": f"mlx_slm_pair_{args.model}",
                "candidate_mode": "mlx_slm_pair",
                "entity_candidate_count": diagnostics["candidate_count"],
                "evidence_candidate_count": 0,
                "relation_option_count": diagnostics["compiled_relations"],
                "entity_call": {"raw": "python", "latency_s": 0, "prompt_tokens": 0, "completion_tokens": 0},
                "relation_call": {
                    "raw": "mlx_slm_pair",
                    "latency_s": latency,
                    "prompt_tokens": diagnostics["prompt_tokens"],
                    "completion_tokens": diagnostics["completion_tokens"],
                },
                "entity_stats": {"raw_lines": 0, "valid_lines": accepted["entities"], "invalid_lines": 0, "none_lines": 0},
                "relation_stats": {"raw_lines": diagnostics["pair_calls"], "valid_lines": accepted["relations"], "invalid_lines": 0, "none_lines": 0},
                "clean_object": clean,
                "jsonl": jsonl,
                "accepted": accepted,
                "schema_ok": schema_ok,
                "errors": errors,
                "latency_s": latency,
                "prompt_tokens": diagnostics["prompt_tokens"],
                "completion_tokens": diagnostics["completion_tokens"],
                "completion_tok_s": diagnostics["completion_tokens"] / latency if latency and diagnostics["completion_tokens"] else None,
                "truncated": False,
                "reasoning_tokens_seen": False,
                "diagnostics": diagnostics,
            }
        )
        print(
            f"{sample_id} pairs={diagnostics['pair_calls']} E/R={accepted['entities']}/{accepted['relations']} "
            f"lat={latency:.2f}s errs={len(errors)}",
            flush=True,
        )

    wall_s = time.perf_counter() - started
    summary = summarize_model(
        {"model": model_id, "label": f"MLX SLM pair {args.model}"},
        results,
        wall_s,
        prompt_variant=f"mlx_slm_pair_{args.model}",
    )
    summary["gold_score"] = score_results_against_gold(results, gold)
    summary["latency_p50_s"] = statistics.median(latencies) if latencies else None
    summary["model_load_s"] = load_s
    summary["adapter_path"] = str(args.adapter_path.expanduser()) if args.adapter_path else None
    summary["oracle_entities"] = args.oracle_entities
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "schema": "mlx_slm_relation_proposer_v1",
        "model": model_id,
        "samples_path": str(args.samples),
        "gold_path": str(args.gold),
        "payload": {"summary": summary, "results": results},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, default=Path("scripts/local_extraction_fixtures/13_building_ai_mobile_apps_10_chunks.jsonl"))
    parser.add_argument("--gold", type=Path, default=Path("scripts/local_extraction_fixtures/13_building_ai_mobile_apps_gold_v1.json"))
    parser.add_argument("--out", type=Path, default=Path("/tmp/polymath_mlx_slm_relation_proposer.json"))
    parser.add_argument("--model", default="qwen25_15b")
    parser.add_argument("--adapter-path", type=Path)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-entity-candidates", type=int, default=260)
    parser.add_argument("--max-sentence-entities", type=int, default=8)
    parser.add_argument("--max-pairs-per-chunk", type=int, default=48)
    parser.add_argument("--max-relation-options", type=int, default=96)
    parser.add_argument("--keep-standalone-entities", type=int, default=40)
    parser.add_argument("--keep-pair-examples", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=8)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--few-shot", action="store_true")
    parser.add_argument("--oracle-entities", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run(args)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = report["payload"]["summary"]
    gold_score = summary["gold_score"]
    print("\nMLX SLM RELATION PROPOSER")
    print(f"model: {report['model']}")
    print(f"load_s: {summary['model_load_s']:.2f}")
    print(f"chunks/hr: {summary['chunks_per_hour_wall']:.1f}")
    print(f"schema: {summary['schema_pass']}/{summary['samples']}")
    print(f"accepted E/R: {summary['accepted_entities']}/{summary['accepted_relations']}")
    print(
        "gold E/R/graph F1: "
        f"{gold_score['entity_f1']*100:.1f}% / "
        f"{gold_score['relation_f1']*100:.1f}% / "
        f"{gold_score['graph_f1']*100:.1f}%"
    )
    print(
        "gold relation TP/FP/FN: "
        f"{gold_score['relation_tp']}/{gold_score['relation_fp']}/{gold_score['relation_fn']}"
    )
    print(f"gate failures: {summary['gate_failures']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
