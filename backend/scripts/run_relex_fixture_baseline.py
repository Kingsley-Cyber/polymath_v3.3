#!/usr/bin/env python3
"""Run the frozen Relex production extractor over one committed fixture.

This is the one allowed offline Relex benchmark runner. It calls the same
``services.ingestion.relex_local.extract_entities`` function used by the
ingestion worker, but it writes only an isolated JSON report. It never writes
MongoDB, Qdrant, or Neo4j.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
PACK_ROOT = REPO_ROOT / "GRAPHIFY_GLINER2_CPU_AGENT"
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("RELEX_LOCAL_URL", "http://127.0.0.1:8086")

from services.ghost_b import ExtractionTask  # noqa: E402
from services.ingestion import relex_local  # noqa: E402


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _normalized_surface(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _bounded_windows(text: str, *, max_chars: int = 560) -> list[dict[str, Any]]:
    """Split text deterministically while preserving exact document slices."""
    windows: list[dict[str, Any]] = []
    start = 0
    size = len(text)
    while start < size:
        end = min(size, start + max_chars)
        if end < size:
            floor = min(end, start + max_chars // 2)
            candidates = [
                text.rfind("\n\n", floor, end),
                text.rfind("\n", floor, end),
                text.rfind(". ", floor, end),
                text.rfind(" ", floor, end),
            ]
            cut = max(candidates)
            if cut > start:
                end = cut + (2 if text[cut : cut + 2] in {"\n\n", ". "} else 1)
        if end <= start:
            end = min(size, start + max_chars)
        window_text = text[start:end]
        if window_text.strip():
            windows.append({"start": start, "end": end, "text": window_text})
        start = end
    return windows


def _gold_path(fixture: Path) -> Path | None:
    gold_stem = fixture.stem.removesuffix("_fixture") + "_gold.json"
    candidate = fixture.with_name(gold_stem)
    return candidate if candidate.is_file() else None


def _quality_metrics(results: list[dict[str, Any]], gold: dict[str, Any]) -> dict[str, Any]:
    entity_rows = gold.get("entities") or gold.get("sampled_entities") or []
    relation_rows = (
        gold.get("relations")
        or gold.get("sample_relations")
        or gold.get("sampled_relations")
        or []
    )
    predicted_entities = {
        (_normalized_surface(entity.get("surface_form") or entity.get("canonical_name") or ""),
         str(entity.get("entity_type") or "").casefold())
        for row in results
        for entity in row.get("entities", [])
        if _normalized_surface(entity.get("surface_form") or entity.get("canonical_name") or "")
    }
    gold_entities = {
        (
            _normalized_surface(row.get("surface") or row.get("text") or ""),
            str(row.get("type") or row.get("label") or "").casefold(),
        )
        for row in entity_rows
        if row.get("status", "accept") == "accept"
        and _normalized_surface(row.get("surface") or row.get("text") or "")
    }
    entity_tp = len(predicted_entities & gold_entities)
    entity_precision = entity_tp / len(predicted_entities) if predicted_entities else 0.0
    entity_recall = entity_tp / len(gold_entities) if gold_entities else 0.0

    predicted_relations = {
        (
            _normalized_surface(relation.get("subject") or ""),
            str(relation.get("predicate") or "").casefold(),
            _normalized_surface(relation.get("object") or ""),
        )
        for row in results
        for relation in row.get("relations", [])
    }
    gold_relations = {
        (
            _normalized_surface(row["subject"]),
            str(row["predicate"]).casefold(),
            _normalized_surface(row["object"]),
        )
        for row in relation_rows
        if row.get("lane", "accept") == "accept"
        and row.get("decision", "FACT") == "FACT"
    }
    relation_tp = len(predicted_relations & gold_relations)
    relation_precision = relation_tp / len(predicted_relations) if predicted_relations else 0.0
    relation_recall = relation_tp / len(gold_relations) if gold_relations else 0.0
    relation_f1 = (
        2 * relation_precision * relation_recall / (relation_precision + relation_recall)
        if relation_precision + relation_recall
        else 0.0
    )
    return {
        "entity": {
            "surface_type_precision": entity_precision,
            "surface_type_recall": entity_recall,
            "surface_type_true_positives": entity_tp,
            "predicted_surface_types": len(predicted_entities),
            "gold_surface_types": len(gold_entities),
            "exact_span_supported": False,
        },
        "relation": {
            "directed_triple_precision": relation_precision,
            "directed_triple_recall": relation_recall,
            "directed_triple_f1": relation_f1,
            "directed_triple_true_positives": relation_tp,
            "predicted_directed_triples": len(predicted_relations),
            "gold_accepted_directed_triples": len(gold_relations),
        },
    }


async def _run(args: argparse.Namespace) -> int:
    fixture = Path(args.input).expanduser().resolve()
    report_path = Path(args.report_json).expanduser().resolve()
    source = fixture.read_bytes()
    text = source.decode("utf-8")
    windows = _bounded_windows(text, max_chars=args.max_chars)
    document_id = fixture.stem
    tasks = []
    for index, window in enumerate(windows):
        identity = _sha256_json(
            [document_id, window["start"], window["end"], window["text"]]
        )[:20]
        tasks.append(
            ExtractionTask(
                chunk_id=f"{document_id}:{index:04d}:{identity}",
                doc_id=document_id,
                corpus_id=args.isolated_namespace,
                text=window["text"],
                metadata={
                    "document_start": window["start"],
                    "document_end": window["end"],
                },
            )
        )

    started = time.perf_counter()
    report = await relex_local.extract_entities(tasks, return_report=True)
    wall_seconds = time.perf_counter() - started
    rows = [asdict(row) for row in report.results]
    failures = [asdict(row) for row in report.failures]
    stable_rows = sorted(rows, key=lambda row: row["chunk_id"])
    identity_rows = []
    for row in stable_rows:
        stable_row = dict(row)
        stable_row.pop("corpus_id", None)
        identity_rows.append(stable_row)

    nodes = sorted(
        {
            (
                _normalized_surface(entity.get("canonical_name") or entity.get("surface_form") or ""),
                str(entity.get("entity_type") or "").casefold(),
            )
            for row in rows
            for entity in row.get("entities", [])
        }
    )
    edges = sorted(
        {
            (
                _normalized_surface(relation.get("subject") or ""),
                str(relation.get("predicate") or "").casefold(),
                _normalized_surface(relation.get("object") or ""),
            )
            for row in rows
            for relation in row.get("relations", [])
        }
    )
    evidence_alignment = all(
        not relation.get("evidence_phrase")
        or relation["evidence_phrase"] in row.get("text", "")
        for row in rows
        for relation in row.get("relations", [])
    )

    gold = {}
    gold_path = _gold_path(fixture)
    if gold_path is not None:
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
    quality = _quality_metrics(rows, gold) if (
        gold.get("entities") or gold.get("sampled_entities")
    ) else {
        "entity": {},
        "relation": {},
    }

    health = rows[0].get("local_extraction", {}) if rows else {}
    output_identity = {
        "fixture_sha256": _sha256_bytes(source),
        "window_bounds": [[w["start"], w["end"]] for w in windows],
        "results": identity_rows,
        "projection": {"nodes": nodes, "edges": edges},
        "failures": failures,
    }
    result = {
        "status": "passed" if not failures and len(rows) == len(tasks) else "failed",
        "fixture": str(fixture),
        "provider": "relex_local_frozen_baseline",
        "run_id": args.isolated_namespace,
        "metrics": {
            **quality,
            "throughput": {
                "source_bytes": len(source),
                "windows_per_second": len(tasks) / wall_seconds if wall_seconds else 0.0,
                "source_bytes_per_second": len(source) / wall_seconds if wall_seconds else 0.0,
            },
        },
        "checks": {
            "all_windows_returned": len(rows) == len(tasks),
            "zero_failures": not failures,
            "exact_evidence_substrings": evidence_alignment,
            "model_hash_verified": bool(health.get("model_hash_verified")),
            "isolated_no_database_writes": True,
        },
        "counts": {
            "windows": len(tasks),
            "results": len(rows),
            "failures": len(failures),
            "entities": sum(len(row.get("entities", [])) for row in rows),
            "relations": sum(len(row.get("relations", [])) for row in rows),
            "projection_nodes": len(nodes),
            "projection_edges": len(edges),
        },
        "identity_digest": _sha256_json(output_identity),
        "projection_digest": _sha256_json({"nodes": nodes, "edges": edges}),
        "release_pins": {
            "extractor_engine": relex_local.EXTRACTOR_ENGINE,
            "extractor_release": relex_local.EXTRACTOR_RELEASE,
            "schema_version": relex_local.SCHEMA_VERSION,
            "model_id": health.get("model_id"),
            "model_hash": health.get("model_hash"),
            "ontology_hash": relex_local.ontology_hash(),
            "acceptance_policy_hash": relex_local.acceptance_policy_hash(),
            "fixture_sha256": _sha256_bytes(source),
        },
        "stage_timings": {
            "semantic_extraction_seconds": wall_seconds,
            "total_seconds": wall_seconds,
        },
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "device": (report.metrics or {}).get("device"),
            "sidecar_url": relex_local.sidecar_url(),
        },
        "gate_decision_counts": (report.metrics or {}).get("gate_decision_counts", {}),
        "artifacts": [str(report_path.with_name(f"{report_path.stem}_records.json"))],
        "errors": failures,
        "warnings": [
            "Final Relex entity records do not preserve exact offsets; baseline entity scoring uses unique surface and type pairs."
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    records_path = report_path.with_name(f"{report_path.stem}_records.json")
    records_path.write_text(json.dumps({
        "schema_version": "polymath.relex_fixture_records.v1",
        "fixture": str(fixture),
        "fixture_sha256": _sha256_bytes(source),
        "results": stable_rows,
        "failures": failures,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(report_path),
        "status": result["status"],
        "counts": result["counts"],
        "identity_digest": result["identity_digest"],
        "wall_seconds": wall_seconds,
    }, indent=2))
    return 0 if result["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--isolated-namespace", required=True)
    parser.add_argument("--max-chars", type=int, default=560)
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
