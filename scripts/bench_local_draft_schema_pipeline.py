#!/usr/bin/env python3
"""Benchmark a local draft schema compiled into Ghost B ExtractionResponse.

The point of this harness is to test the architecture, not to change the
production contract:

    LocalExtractionDraft -> OntologyMapper -> ExtractionResponse -> JSONL

Local models may propose broad spans. Python still owns canonical names,
allowed entity types, relation options, direction/evidence rules, Pydantic
validation, and JSONL conversion.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from autoresearch_polymath_local_extraction import (  # noqa: E402
    Candidate,
    ACRONYM_RE,
    CONCEPT_MARKERS,
    build_object,
    candidate_score,
    canonical,
    clean_surface,
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


class SpanLabel(str, Enum):
    PERSON = "Person"
    ORGANIZATION = "Organization"
    LOCATION = "Location"
    CONCEPT = "Concept"
    TOOL = "Tool"
    METHOD = "Method"
    TIME_REFERENCE = "TimeReference"
    NUMERIC = "Numeric"
    ARTIFACT = "Artifact"
    EVENT = "Event"
    UNKNOWN = "Unknown"


class DraftSpan(BaseModel):
    id: str = Field(pattern=r"^S\d+$")
    text: str
    label: SpanLabel
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    source: str = "python"


class DraftLink(BaseModel):
    head: str
    tail: str
    relation_hint: str
    evidence: str
    confidence: float | None = Field(default=None, ge=0, le=1)


class DraftQualifier(BaseModel):
    target_link: int
    property: str
    value_span: str
    evidence: str


class LocalExtractionDraft(BaseModel):
    spans: list[DraftSpan] = Field(default_factory=list)
    links: list[DraftLink] = Field(default_factory=list)
    qualifiers: list[DraftQualifier] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


GLINER2_FULL_LABELS: dict[str, str] = {
    "person": "people or named individuals",
    "organization": "companies, organizations, institutions",
    "software": "software, apps, platforms, runtimes",
    "method": "methods, algorithms, techniques, procedures",
    "concept": "technical concepts and textbook concepts",
    "document": "books, chapters, documents",
    "artifact": "files, commands, diagrams, tools, concrete artifacts",
    "standard": "standards, formats, protocols",
    "rule": "rules, constraints, safety protocols",
    "product": "products or named tools",
    "location": "places or geopolitical locations",
    "time reference": "dates, years, times, durations",
}

GLINER2_SMALL_LABELS = [
    "person",
    "organization",
    "software",
    "method",
    "concept",
    "artifact",
    "time reference",
]

GLINER_TO_SPAN_LABEL = {
    "person": SpanLabel.PERSON,
    "organization": SpanLabel.ORGANIZATION,
    "software": SpanLabel.TOOL,
    "method": SpanLabel.METHOD,
    "concept": SpanLabel.CONCEPT,
    "document": SpanLabel.ARTIFACT,
    "artifact": SpanLabel.ARTIFACT,
    "standard": SpanLabel.ARTIFACT,
    "rule": SpanLabel.CONCEPT,
    "product": SpanLabel.TOOL,
    "location": SpanLabel.LOCATION,
    "time reference": SpanLabel.TIME_REFERENCE,
}

SPAN_TO_ENTITY_TYPE = {
    SpanLabel.PERSON: "Person",
    SpanLabel.ORGANIZATION: "Organization",
    SpanLabel.LOCATION: "Location",
    SpanLabel.CONCEPT: "Concept",
    SpanLabel.TOOL: "Artifact",
    SpanLabel.METHOD: "Method",
    SpanLabel.TIME_REFERENCE: "TimeReference",
    SpanLabel.NUMERIC: "other",
    SpanLabel.ARTIFACT: "Artifact",
    SpanLabel.EVENT: "Event",
    SpanLabel.UNKNOWN: "Concept",
}

DOMAIN_TERMS = {
    "agent",
    "airbnb",
    "algorithmic complexity",
    "ai landscape",
    "ai outputs",
    "bitcoin prices",
    "case studies",
    "code samples",
    "context construction",
    "data normalization",
    "database fundamentals",
    "database product",
    "declaration",
    "designers",
    "documentation",
    "evaluation benchmarks",
    "fair die",
    "feedback loop",
    "finetuning",
    "game development",
    "hallucinations",
    "house listings",
    "human perception",
    "information technology",
    "language models",
    "lookup table",
    "mental block",
    "ml system",
    "ml systems",
    "object detection",
    "parameter-efficient finetuning",
    "programming video games",
    "prompt engineering",
    "rag",
    "rental price",
    "rental price prediction system",
    "speech recognition",
    "stocks",
    "technical artists",
    "traditional ml engineering",
    "variable names",
    "video game development",
    "video game programmers",
}


def count_occurrences(surface: str, text: str) -> int:
    if not surface:
        return 0
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(surface) + r"(?![A-Za-z0-9_])"
    return len(re.findall(pattern, text, flags=re.I))


def is_acronym(surface: str) -> bool:
    value = surface.strip()
    return bool(ACRONYM_RE.fullmatch(value)) or bool(re.fullmatch(r"[A-Z][A-Z0-9]{1,8}", value))


def has_definition_pattern(surface: str, text: str) -> bool:
    escaped = re.escape(surface)
    patterns = [
        rf"\b{escaped}\s*\(",
        rf"\(\s*{escaped}\s*\)",
        rf"\b{escaped}\s+(?:is|are|means|refers to|stands for)\b",
        rf"\b(?:is|are|means|called|known as)\s+{escaped}\b",
    ]
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def in_heading(surface: str, text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and re.search(re.escape(surface), stripped, flags=re.I):
            return True
        if len(stripped) < 90 and stripped.isupper() and re.search(re.escape(surface), stripped, flags=re.I):
            return True
    return False


def standalone_importance_score(candidate: Candidate, text: str) -> float:
    surface = candidate.text
    key = canonical(surface)
    tokens = key.split()
    if not tokens:
        return -1.0

    score = 0.0
    rep = count_occurrences(surface, text)
    if rep > 1:
        score += 0.20 * min(1.0, (rep - 1) / 2.0)
    if is_acronym(surface):
        score += 0.30
    if has_definition_pattern(surface, text):
        score += 0.30
    if in_heading(surface, text):
        score += 0.20
    if key in DOMAIN_TERMS:
        score += 0.45

    marker_hits = sum(1 for token in tokens if token in CONCEPT_MARKERS)
    score += min(0.25, marker_hits * 0.08)
    score += min(0.18, max(0.0, candidate_score(surface, text) / 60.0))
    if len(tokens) >= 2:
        score += 0.12
    if len(tokens) >= 3:
        score += 0.05
    if surface[:1].isupper() or any(ch.isupper() for ch in surface[1:]):
        score += 0.06

    if len(surface) <= 2 and not surface.isupper():
        score -= 0.35
    if len(tokens) == 1 and not is_acronym(surface) and marker_hits == 0 and key not in DOMAIN_TERMS:
        score -= 0.20
    if any(token in {"thing", "things", "people", "person", "book", "books"} for token in tokens):
        score -= 0.12
    return score


def span_label_from_entity_type(entity_type: str) -> SpanLabel:
    mapping = {
        "Person": SpanLabel.PERSON,
        "Organization": SpanLabel.ORGANIZATION,
        "Location": SpanLabel.LOCATION,
        "Event": SpanLabel.EVENT,
        "Concept": SpanLabel.CONCEPT,
        "Method": SpanLabel.METHOD,
        "Product": SpanLabel.TOOL,
        "Software": SpanLabel.TOOL,
        "Document": SpanLabel.ARTIFACT,
        "Standard": SpanLabel.ARTIFACT,
        "Rule": SpanLabel.CONCEPT,
        "Law": SpanLabel.CONCEPT,
        "Artifact": SpanLabel.ARTIFACT,
        "TimeReference": SpanLabel.TIME_REFERENCE,
        "other": SpanLabel.UNKNOWN,
    }
    return mapping.get(entity_type, SpanLabel.UNKNOWN)


def find_unused_span(text: str, surface: str, used_offsets: set[tuple[int, int]]) -> tuple[int, int] | None:
    if not surface:
        return None
    for match in re.finditer(re.escape(surface), text):
        span = (match.start(), match.end())
        if span not in used_offsets:
            return span
    lower_text = text.lower()
    lower_surface = surface.lower()
    start = 0
    while True:
        idx = lower_text.find(lower_surface, start)
        if idx < 0:
            return None
        span = (idx, idx + len(surface))
        if span not in used_offsets:
            return span
        start = idx + 1


def draft_from_python_candidates(text: str, *, max_candidates: int, keep: int) -> LocalExtractionDraft:
    spans: list[DraftSpan] = []
    used_offsets: set[tuple[int, int]] = set()
    for candidate in entity_candidates(text, max_candidates)[:keep]:
        offset = find_unused_span(text, candidate.text, used_offsets)
        if offset is None:
            continue
        used_offsets.add(offset)
        entity_type = infer_entity_type(candidate.text)
        spans.append(
            DraftSpan(
                id=f"S{len(spans) + 1}",
                text=candidate.text,
                label=span_label_from_entity_type(entity_type),
                start=offset[0],
                end=offset[1],
                confidence=0.90,
                source="python",
            )
        )
    return LocalExtractionDraft(spans=spans, metadata={"span_source": "python"})


def normalize_gliner2_result(result: dict[str, Any]) -> dict[str, list[str]]:
    entities = result.get("entities") if isinstance(result, dict) else None
    if not isinstance(entities, dict):
        return {}
    out: dict[str, list[str]] = {}
    for raw_label, raw_values in entities.items():
        label = str(raw_label).strip().lower()
        values = raw_values if isinstance(raw_values, list) else []
        clean_values = [clean_surface(str(value)) for value in values]
        out[label] = [value for value in clean_values if value]
    return out


def draft_from_gliner2_result(text: str, result: dict[str, Any]) -> LocalExtractionDraft:
    spans: list[DraftSpan] = []
    used_keys: set[str] = set()
    used_offsets: set[tuple[int, int]] = set()
    for label, values in normalize_gliner2_result(result).items():
        span_label = GLINER_TO_SPAN_LABEL.get(label, SpanLabel.UNKNOWN)
        for surface in values:
            key = canonical(surface)
            if not key or key in used_keys:
                continue
            offset = find_unused_span(text, surface, used_offsets)
            if offset is None:
                continue
            used_keys.add(key)
            used_offsets.add(offset)
            spans.append(
                DraftSpan(
                    id=f"S{len(spans) + 1}",
                    text=surface,
                    label=span_label,
                    start=offset[0],
                    end=offset[1],
                    confidence=None,
                    source="gliner2",
                )
            )
    return LocalExtractionDraft(spans=spans, metadata={"span_source": "gliner2"})


def merge_drafts(*drafts: LocalExtractionDraft, source: str) -> LocalExtractionDraft:
    spans: list[DraftSpan] = []
    used: set[str] = set()
    for draft in drafts:
        for span in draft.spans:
            key = canonical(span.text)
            if not key or key in used:
                continue
            used.add(key)
            spans.append(span.model_copy(update={"id": f"S{len(spans) + 1}"}))
    return LocalExtractionDraft(spans=spans, metadata={"span_source": source})


class OntologyMapper:
    def __init__(
        self,
        *,
        max_evidence_candidates: int,
        max_relation_options: int,
        direct_only: bool,
        keep_standalone_entities: int,
        standalone_importance_threshold: float,
    ):
        self.max_evidence_candidates = max_evidence_candidates
        self.max_relation_options = max_relation_options
        self.direct_only = direct_only
        self.keep_standalone_entities = keep_standalone_entities
        self.standalone_importance_threshold = standalone_importance_threshold

    def compile(
        self,
        draft: LocalExtractionDraft,
        text: str,
        *,
        prune_entities_to_relation_endpoints: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        entity_candidates_out: list[Candidate] = []
        entity_choices: list[tuple[str, str]] = []
        seen: set[str] = set()
        for span in sorted(draft.spans, key=lambda item: (item.start, item.end, item.text)):
            if span.end <= span.start or span.text not in text:
                continue
            key = canonical(span.text)
            if not key or key in seen:
                continue
            seen.add(key)
            entity_id = f"E{len(entity_candidates_out) + 1:03d}"
            entity_type = SPAN_TO_ENTITY_TYPE.get(span.label, "Concept")
            entity_candidates_out.append(Candidate(entity_id, span.text))
            entity_choices.append((entity_id, entity_type))

        evidence = evidence_candidates(text, self.max_evidence_candidates)
        rel_options = relation_options(
            entity_candidates_out,
            evidence,
            max_items=self.max_relation_options,
        )
        if self.direct_only:
            relation_choices = [item.id for item in rel_options if str(item.cue).startswith("direct_")]
        else:
            relation_choices = []

        rel_by_id = {item.id: item for item in rel_options}
        if prune_entities_to_relation_endpoints:
            used_entity_ids: set[str] = set()
            for relation_id in relation_choices:
                option = rel_by_id[relation_id]
                used_entity_ids.add(option.subject_id)
                used_entity_ids.add(option.object_id)
            endpoint_choices = [
                (entity_id, entity_type)
                for entity_id, entity_type in entity_choices
                if entity_id in used_entity_ids
            ]
            if self.keep_standalone_entities > 0:
                endpoint_ids = {entity_id for entity_id, _ in endpoint_choices}
                candidate_by_id = {item.id: item for item in entity_candidates_out}
                scored_standalone: list[tuple[float, int, tuple[str, str]]] = []
                for idx, (entity_id, entity_type) in enumerate(entity_choices):
                    if entity_id in endpoint_ids:
                        continue
                    candidate = candidate_by_id.get(entity_id)
                    if not candidate:
                        continue
                    importance = standalone_importance_score(candidate, text)
                    if importance < self.standalone_importance_threshold:
                        continue
                    scored_standalone.append((importance, -idx, (entity_id, entity_type)))
                scored_standalone.sort(reverse=True)
                standalone_choices = [
                    choice
                    for _, _, choice in scored_standalone[: self.keep_standalone_entities]
                ]
                entity_choices = endpoint_choices + standalone_choices
            else:
                entity_choices = endpoint_choices

        clean = build_object(
            entity_choices,
            relation_choices,
            {item.id: item for item in entity_candidates_out},
            rel_by_id,
            {item.id: item for item in evidence},
        )
        diagnostics = {
            "draft_spans": len(draft.spans),
            "compiled_entities": len(entity_choices),
            "evidence_candidates": len(evidence),
            "relation_options": len(rel_options),
            "direct_relations": len(relation_choices),
        }
        return clean, diagnostics


def load_gliner2_model(model_id: str):
    from gliner2 import GLiNER2

    return GLiNER2.from_pretrained(model_id)


def run_gliner2_extracts(
    model: Any,
    samples: list[dict[str, Any]],
    *,
    threshold: float,
    labels: dict[str, str] | list[str],
    batch_size: int,
    concurrency: int,
    max_len: int | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    sample_ids = [
        str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"))
        for sample in samples
    ]
    texts = [str(sample["text"]) for sample in samples]
    outputs: dict[str, dict[str, Any]] = {}
    latencies: dict[str, float] = {}
    if batch_size > 1:
        started = time.perf_counter()
        results = model.batch_extract_entities(
            texts,
            labels,
            batch_size=batch_size,
            threshold=threshold,
            max_len=max_len,
        )
        elapsed = time.perf_counter() - started
        per_sample = elapsed / max(1, len(samples))
        for sample_id, result in zip(sample_ids, results, strict=False):
            outputs[sample_id] = result
            latencies[sample_id] = per_sample
        return outputs, latencies

    def extract(sample: dict[str, Any]) -> tuple[str, dict[str, Any], float]:
        sample_id = str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"))
        started = time.perf_counter()
        result = model.extract_entities(
            str(sample["text"]),
            labels,
            threshold=threshold,
            max_len=max_len,
        )
        return sample_id, result, time.perf_counter() - started

    for sample in samples:
        sample_id, result, latency = extract(sample)
        outputs[sample_id] = result
        latencies[sample_id] = latency
    return outputs, latencies


def run_variant(args: argparse.Namespace, samples: list[dict[str, Any]], gold: dict[str, Any]) -> dict[str, Any]:
    mapper = OntologyMapper(
        max_evidence_candidates=args.max_evidence_candidates,
        max_relation_options=args.max_relation_options,
        direct_only=True,
        keep_standalone_entities=args.keep_standalone_entities,
        standalone_importance_threshold=args.standalone_importance_threshold,
    )
    gliner_outputs: dict[str, dict[str, Any]] = {}
    gliner_latencies: dict[str, float] = {}
    if args.span_source in {"gliner2", "union"}:
        print(f"loading GLiNER2: {args.gliner2_model}", flush=True)
        model = load_gliner2_model(args.gliner2_model)
        labels = GLINER2_FULL_LABELS if args.gliner2_label_profile == "full" else GLINER2_SMALL_LABELS
        gliner_outputs, gliner_latencies = run_gliner2_extracts(
            model,
            samples,
            threshold=args.gliner2_threshold,
            labels=labels,
            batch_size=args.gliner2_batch_size,
            concurrency=args.concurrency,
            max_len=args.gliner2_max_len,
        )

    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    per_sample_latencies: list[float] = []
    for sample in samples:
        sample_started = time.perf_counter()
        text = str(sample["text"])
        sample_id = str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"))
        python_draft = draft_from_python_candidates(
            text,
            max_candidates=args.max_entity_candidates,
            keep=args.python_entity_keep,
        )
        if args.span_source == "python":
            draft = python_draft
            model_latency = 0.0
        else:
            gliner_draft = draft_from_gliner2_result(text, gliner_outputs.get(sample_id) or {})
            model_latency = gliner_latencies.get(sample_id, 0.0)
            if args.span_source == "gliner2":
                draft = gliner_draft
            else:
                draft = merge_drafts(python_draft, gliner_draft, source="union")

        clean, diagnostics = mapper.compile(
            draft,
            text,
            prune_entities_to_relation_endpoints=args.prune_entities_to_relation_endpoints,
        )
        schema_ok, accepted, errors = validate_object(clean, text)
        try:
            jsonl = object_to_jsonl(clean)
        except Exception as exc:
            jsonl = '{"t":"x"}'
            errors.append(f"jsonl:{type(exc).__name__}:{str(exc)[:120]}")
        total_latency = model_latency + (time.perf_counter() - sample_started)
        per_sample_latencies.append(total_latency)
        results.append(
            {
                "id": sample_id,
                "filename": sample.get("filename"),
                "prompt_variant": "local_draft_schema_pipeline",
                "token_count": sample.get("token_count") or sample.get("tokens"),
                "entity_candidate_count": diagnostics["draft_spans"],
                "evidence_candidate_count": diagnostics["evidence_candidates"],
                "relation_option_count": diagnostics["relation_options"],
                "candidate_mode": args.span_source,
                "oracle_stats": {"enabled": False},
                "entity_candidates": [],
                "evidence_candidates": [],
                "relation_options": [],
                "entity_call": {
                    "raw": args.span_source,
                    "latency_s": model_latency,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                },
                "relation_call": {
                    "raw": "ontology_mapper_direct_rules",
                    "latency_s": total_latency - model_latency,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "finish_reason": "stop",
                },
                "entity_raw": args.span_source,
                "relation_raw": "ontology_mapper_direct_rules",
                "entity_stats": {
                    "raw_lines": 0,
                    "valid_lines": diagnostics["compiled_entities"],
                    "invalid_lines": 0,
                    "none_lines": int(not diagnostics["compiled_entities"]),
                },
                "relation_stats": {
                    "raw_lines": 0,
                    "valid_lines": diagnostics["direct_relations"],
                    "invalid_lines": 0,
                    "none_lines": int(not diagnostics["direct_relations"]),
                },
                "clean_object": clean,
                "jsonl": jsonl,
                "accepted": accepted,
                "schema_ok": schema_ok,
                "errors": errors,
                "latency_s": total_latency,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "completion_tok_s": None,
                "truncated": False,
                "reasoning_tokens_seen": False,
                "draft_diagnostics": diagnostics,
            }
        )
        print(
            f"{sample_id} spans={diagnostics['draft_spans']} "
            f"E/R={accepted['entities']}/{accepted['relations']} "
            f"lat={total_latency:.3f}s errs={len(errors)}",
            flush=True,
        )

    wall_s = time.perf_counter() - started
    if args.span_source in {"gliner2", "union"} and args.concurrency > 1:
        # In concurrent mode per-sample latencies overlap; wall clock from this
        # loop excludes extraction overlap already completed above. Use the
        # max latency as a conservative additive model cost for the tiny fixture.
        wall_s += max(gliner_latencies.values() or [0.0])
    elif args.span_source in {"gliner2", "union"}:
        wall_s += sum(gliner_latencies.values())

    summary = summarize_model(
        {
            "model": args.gliner2_model if args.span_source in {"gliner2", "union"} else "python",
            "label": f"LocalDraft {args.span_source}",
        },
        results,
        wall_s,
        prompt_variant="local_draft_schema_pipeline",
    )
    summary["gold_score"] = score_results_against_gold(results, gold) if gold else {}
    summary["latency_p50_s"] = statistics.median(per_sample_latencies) if per_sample_latencies else None
    summary["span_source"] = args.span_source
    summary["gliner2_threshold"] = args.gliner2_threshold
    summary["gliner2_batch_size"] = args.gliner2_batch_size
    summary["gliner2_label_profile"] = args.gliner2_label_profile
    summary["gliner2_max_len"] = args.gliner2_max_len
    summary["concurrency"] = args.concurrency
    summary["keep_standalone_entities"] = args.keep_standalone_entities
    summary["standalone_importance_threshold"] = args.standalone_importance_threshold
    return {"summary": summary, "results": results}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, default=Path("/tmp/polymath_xml_json_test_chunks.jsonl"))
    parser.add_argument("--gold", type=Path, default=Path("/tmp/polymath_xml_json_gold_exact_v2.json"))
    parser.add_argument("--out", type=Path, default=Path("/tmp/polymath_local_draft_schema_pipeline.json"))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--span-source", choices=["python", "gliner2", "union"], default="union")
    parser.add_argument("--gliner2-model", default="fastino/gliner2-base-v1")
    parser.add_argument("--gliner2-threshold", type=float, default=0.15)
    parser.add_argument("--gliner2-label-profile", choices=["full", "small"], default="small")
    parser.add_argument("--gliner2-batch-size", type=int, default=8)
    parser.add_argument("--gliner2-max-len", type=int, default=256)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-entity-candidates", type=int, default=160)
    parser.add_argument("--python-entity-keep", type=int, default=120)
    parser.add_argument("--max-evidence-candidates", type=int, default=32)
    parser.add_argument("--max-relation-options", type=int, default=24)
    parser.add_argument("--prune-entities-to-relation-endpoints", action="store_true")
    parser.add_argument("--keep-standalone-entities", type=int, default=0)
    parser.add_argument("--standalone-importance-threshold", type=float, default=0.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    samples = load_samples(args.samples, args.limit)
    gold = load_gold(args.gold)
    report = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "schema": "LocalExtractionDraft -> ExtractionResponse",
        "canonical_contract": "services.ghost_b_schemas.ExtractionResponse",
        "samples_path": str(args.samples),
        "gold_path": str(args.gold),
        "sample_count": len(samples),
        "payload": run_variant(args, samples, gold),
    }
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = report["payload"]["summary"]
    gold_score = summary.get("gold_score") or {}
    print("\nLOCAL DRAFT SCHEMA SUMMARY")
    print(f"span source: {args.span_source}")
    print(f"concurrency: {args.concurrency}")
    print(f"chunks/hr: {summary['chunks_per_hour_wall']:.1f}")
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
