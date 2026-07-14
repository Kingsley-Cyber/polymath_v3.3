#!/usr/bin/env python3
"""Compare spaCy, GLiNER, GLiREL-oracle, and GLiNER->GLiREL locally.

The gold fixture is small and adversarial: it measures conditional scope,
negation, modality, attribution, exceptions, temporal cues, and analogy
limitations in addition to the legacy entity/relation surface.  The optional
``--scale-repetitions`` repeats the inputs only for a warm throughput stress;
quality is always scored once on the unique fixture.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
LOCAL = ROOT / "local_ghost_b"
for path in (str(BACKEND), str(LOCAL), str(LOCAL / "tools")):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from evals.semantic_extraction_scoring import (  # noqa: E402
    score_claim_candidates,
    score_extraction_lane,
)
from services.ingestion.semantic_observations import (  # noqa: E402
    build_spacy_observation_bundle,
    compile_claim_candidates,
    validate_evidence_round_trip,
)
from chunk_with_gliner import dedupe_entities  # noqa: E402
from glirel_infer import GliRELClassifier, pick_device  # noqa: E402
from pipeline_config import GHOST_B_ENTITY_TYPES  # noqa: E402


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return round(ordered[index], 4)


def _load_fixture(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != "polymath.semantic_extraction_gold.v1":
        raise ValueError("unsupported semantic extraction fixture")
    return value


def _expanded_samples(samples: list[dict[str, Any]], repetitions: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for repeat in range(max(1, repetitions)):
        for sample in samples:
            rows.append(
                {
                    **sample,
                    "id": f"{sample['id']}@{repeat}",
                    "source_id": sample["id"],
                }
            )
    return rows


def _load_gliner(model_id: str):
    from gliner import GLiNER

    return GLiNER.from_pretrained(model_id, local_files_only=True)


def _gliner_results(
    model: Any,
    samples: list[dict[str, Any]],
    *,
    threshold: float,
    batch_size: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    texts = [str(item["text"]) for item in samples]
    started = time.perf_counter()
    raw_batches = model.batch_predict_entities(
        texts,
        list(GHOST_B_ENTITY_TYPES),
        threshold=threshold,
        batch_size=max(1, batch_size),
    )
    wall = time.perf_counter() - started
    results: list[dict[str, Any]] = []
    per_sample = wall / max(1, len(samples))
    for sample, rows in zip(samples, raw_batches, strict=False):
        entities = dedupe_entities(rows or [])
        results.append(
            {
                "id": sample["id"],
                "text": sample["text"],
                "entities": entities,
                "relations": [],
                "latency_s": per_sample,
            }
        )
    return results, {
        "wall_seconds": round(wall, 4),
        "chunks_per_second": round(len(samples) / wall, 4) if wall else None,
        "latency_p50_s": round(per_sample, 4),
        "latency_p95_s": round(per_sample, 4),
    }


def _gold_entities(sample: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "canonical_name": item["canonical_name"],
            "surface_form": item["surface_form"],
            "entity_type": item["entity_type"],
            "query_aliases": [],
        }
        for item in (sample.get("gold") or {}).get("entities") or []
    ]


def _relations_from_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "subject": item["sub"],
            "predicate": item["pred"],
            "object": item["obj"],
            "object_kind": "entity",
            "confidence": item["score"],
            "evidence_phrase": item["ev"],
        }
        for item in edges
    ]


def _run_glirel(
    classifier: GliRELClassifier,
    samples: list[dict[str, Any]],
    entities_by_id: dict[str, list[dict[str, Any]]],
    *,
    unit_batch: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    chunks = [
        {
            "chunk_id": str(sample["id"]),
            "doc_id": "semantic-extraction-gold-v1",
            "text": str(sample["text"]),
            "entities": entities_by_id.get(str(sample["id"])) or [],
        }
        for sample in samples
    ]
    started = time.perf_counter()
    edge_lists = classifier.extract_chunks(chunks, unit_batch=max(1, unit_batch))
    wall = time.perf_counter() - started
    results = [
        {
            "id": sample["id"],
            "text": sample["text"],
            "entities": chunk["entities"],
            "relations": _relations_from_edges(edges),
        }
        for sample, chunk, edges in zip(samples, chunks, edge_lists, strict=False)
    ]
    return results, {
        "wall_seconds": round(wall, 4),
        "chunks_per_second": round(len(samples) / wall, 4) if wall else None,
    }


def _run_spacy(samples: list[dict[str, Any]], model_id: str) -> dict[str, Any]:
    import spacy

    nlp = spacy.load(model_id)
    candidates_by_sample: dict[str, list[dict[str, Any]]] = {}
    qualifiers_by_sample: dict[str, list[dict[str, Any]]] = {}
    latencies: list[float] = []
    evidence_errors: list[str] = []
    bundle_counts: dict[str, dict[str, int]] = {}
    for sample in samples:
        started = time.perf_counter()
        bundle = build_spacy_observation_bundle(
            text=str(sample["text"]),
            nlp=nlp,
            source_version_id="source-version:semantic-extraction-gold-v1",
            hierarchy_node_id=f"child:{sample['id']}",
            parser_id=model_id,
            parser_version=str(spacy.__version__),
        )
        candidates = compile_claim_candidates(bundle)
        latencies.append(time.perf_counter() - started)
        candidates_by_sample[str(sample["id"])] = [
            item.model_dump() for item in candidates
        ]
        qualifiers_by_sample[str(sample["id"])] = [
            item.model_dump() for item in bundle.qualifiers
        ]
        evidence_errors.extend(
            f"{sample['id']}:{error}"
            for error in validate_evidence_round_trip(bundle, str(sample["text"]))
        )
        bundle_counts[str(sample["id"])] = {
            "spans": len(bundle.spans),
            "predicates": len(bundle.predicates),
            "qualifiers": len(bundle.qualifiers),
            "claim_candidates": len(candidates),
        }
    score = score_claim_candidates(
        samples, candidates_by_sample, qualifiers_by_sample
    )
    return {
        "parser": model_id,
        "spacy_version": str(spacy.__version__),
        "pipeline": list(nlp.pipe_names),
        "wall_seconds": round(sum(latencies), 4),
        "chunks_per_second": round(len(samples) / sum(latencies), 4)
        if sum(latencies)
        else None,
        "latency_p50_s": _percentile(latencies, 0.50),
        "latency_p95_s": _percentile(latencies, 0.95),
        "evidence_round_trip_errors": evidence_errors,
        "score": score,
        "bundle_counts": bundle_counts,
        "candidates_by_sample": candidates_by_sample,
        "qualifiers_by_sample": qualifiers_by_sample,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=BACKEND / "evals" / "semantic_extraction_gold_v1.json",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--spacy-model", default="en_core_web_sm")
    parser.add_argument("--gliner-model", default="urchade/gliner_medium-v2.1")
    parser.add_argument("--gliner-threshold", type=float, default=0.40)
    parser.add_argument("--gliner-batch-size", type=int, default=8)
    parser.add_argument(
        "--glirel-checkpoint",
        default=str(ROOT / "models" / "glirel_ghost_b_v1" / "best"),
    )
    parser.add_argument("--glirel-threshold", type=float, default=0.50)
    parser.add_argument("--glirel-unit-batch", type=int, default=64)
    parser.add_argument("--scale-repetitions", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fixture = _load_fixture(args.fixture)
    unique_samples = list(fixture["samples"])
    stress_samples = _expanded_samples(unique_samples, args.scale_repetitions)

    spacy_report = _run_spacy(unique_samples, args.spacy_model)

    gliner_started = time.perf_counter()
    gliner = _load_gliner(args.gliner_model)
    gliner_load_s = time.perf_counter() - gliner_started
    gliner_stress, gliner_perf = _gliner_results(
        gliner,
        stress_samples,
        threshold=args.gliner_threshold,
        batch_size=args.gliner_batch_size,
    )
    gliner_unique = [
        {**item, "id": str(item["id"]).split("@", 1)[0]}
        for item in gliner_stress
        if str(item["id"]).endswith("@0")
    ]

    labels = json.loads(
        (Path(args.glirel_checkpoint) / "labels.json").read_text(encoding="utf-8")
    )
    glirel_started = time.perf_counter()
    glirel = GliRELClassifier(
        args.glirel_checkpoint,
        labels,
        pick_device(),
        threshold=args.glirel_threshold,
    )
    glirel_load_s = time.perf_counter() - glirel_started

    oracle_entities = {
        str(sample["id"]): _gold_entities(sample) for sample in stress_samples
    }
    oracle_results_stress, oracle_perf = _run_glirel(
        glirel,
        stress_samples,
        oracle_entities,
        unit_batch=args.glirel_unit_batch,
    )
    oracle_unique = [
        {**item, "id": str(item["id"]).split("@", 1)[0]}
        for item in oracle_results_stress
        if str(item["id"]).endswith("@0")
    ]

    predicted_entities = {
        str(item["id"]): item.get("entities") or [] for item in gliner_stress
    }
    pipeline_results_stress, pipeline_perf = _run_glirel(
        glirel,
        stress_samples,
        predicted_entities,
        unit_batch=args.glirel_unit_batch,
    )
    pipeline_unique = [
        {**item, "id": str(item["id"]).split("@", 1)[0]}
        for item in pipeline_results_stress
        if str(item["id"]).endswith("@0")
    ]

    report = {
        "schema_version": "polymath.semantic_extraction_local_benchmark.v1",
        "fixture_schema": fixture["schema_version"],
        "unique_samples": len(unique_samples),
        "stress_samples": len(stress_samples),
        "scale_repetitions": args.scale_repetitions,
        "device": pick_device(),
        "spacy": spacy_report,
        "gliner_only": {
            "model": args.gliner_model,
            "threshold": args.gliner_threshold,
            "model_load_seconds": round(gliner_load_s, 4),
            "performance": gliner_perf,
            "score": score_extraction_lane(unique_samples, gliner_unique),
        },
        "glirel_oracle_entities": {
            "model": args.glirel_checkpoint,
            "threshold": args.glirel_threshold,
            "model_load_seconds": round(glirel_load_s, 4),
            "performance": oracle_perf,
            "score": score_extraction_lane(unique_samples, oracle_unique),
        },
        "gliner_then_glirel": {
            "models": [args.gliner_model, args.glirel_checkpoint],
            "performance": pipeline_perf,
            "score": score_extraction_lane(unique_samples, pipeline_unique),
        },
        "production_interpretation": {
            "quality_fixture_is_human_curated": True,
            "stress_input_is_repeated": args.scale_repetitions > 1,
            "stress_run_proves_capacity_only": args.scale_repetitions > 1,
            "promotion_requires_diverse_heldout_chunks": True,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "out": str(args.out),
                "unique_samples": len(unique_samples),
                "stress_samples": len(stress_samples),
                "spacy_claim_field_accuracy": spacy_report["score"][
                    "claim_field_accuracy_overall"
                ],
                "gliner_entity_f1": report["gliner_only"]["score"]["entities"]["f1"],
                "glirel_oracle_relation_f1": report["glirel_oracle_entities"]["score"]["relations"]["f1"],
                "pipeline_relation_f1": report["gliner_then_glirel"]["score"]["relations"]["f1"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
