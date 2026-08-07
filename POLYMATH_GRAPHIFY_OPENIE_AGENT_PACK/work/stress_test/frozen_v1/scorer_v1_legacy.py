#!/usr/bin/env python3
"""Run one Markdown stress fixture through Graphify and score its Neo4j projection."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from run_graphify_fixture_e2e import (
    _Stores,
    _graph_snapshot,
    _ingest_fixture,
    _project_document,
    _reset_namespace,
    _stage_payloads,
)
from models.graphify_contracts import PipelineStage, stable_digest


def _norm(value: str) -> str:
    words = re.findall(r"[\w]+", value.casefold())
    while words and words[0] in {"a", "an", "the"}:
        words.pop(0)
    return " ".join(words)


def _name_matches(left: str, right: str) -> bool:
    left_norm, right_norm = _norm(left), _norm(right)
    if left_norm == right_norm:
        return True
    if not left_norm or not right_norm:
        return False
    left_words, right_words = left_norm.split(), right_norm.split()
    shorter, longer = (
        (left_words, right_words) if len(left_words) <= len(right_words)
        else (right_words, left_words)
    )
    return len(shorter) >= 2 and (
        longer[:len(shorter)] == shorter or longer[-len(shorter):] == shorter
    )


def _triple_matches(gold: dict[str, Any], predicted: dict[str, Any]) -> bool:
    return (
        str(gold["predicate"]) == str(predicted["predicate"])
        and _name_matches(str(gold["subject"]), str(predicted["subject"]))
        and _name_matches(str(gold["object"]), str(predicted["object"]))
    )


async def _promoted_triples(stores: _Stores) -> list[dict[str, Any]]:
    query = """
        MATCH (s:Entity)-[r:RELATES_TO]->(o:Entity)
        WHERE $corpus_id IN coalesce(r.corpus_ids, [])
        RETURN coalesce(s.canonical_name, s.name, s.entity_id) AS subject,
               coalesce(r.predicate, 'related_to') AS predicate,
               coalesce(o.canonical_name, o.name, o.entity_id) AS object,
               s.entity_id AS subject_entity_id,
               o.entity_id AS object_entity_id,
               size([x IN coalesce(r.evidence_chunk_keys, [])
                     WHERE x STARTS WITH $prefix]) AS evidence_count
        ORDER BY subject, predicate, object
    """
    async with stores.neo4j.session() as session:
        result = await session.run(
            query,
            corpus_id=stores.corpus_id,
            prefix=f"{stores.corpus_id}::",
        )
        return [dict(row) async for row in result]


def _qualified_rows(payloads: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    entity_by_id = {
        row["entity_id"]: row
        for row in payloads[PipelineStage.ENTITY_REDUCTION_COMPLETE.value]["entities"]
    }
    argument_by_id = {
        row["argument_id"]: row
        for row in payloads[PipelineStage.OPENIE_ARGUMENT_ADAPTATION_COMPLETE.value]["arguments"]
    }

    def argument_name(argument_id: str) -> str:
        argument = argument_by_id[argument_id]
        entity = entity_by_id.get(argument.get("entity_id")) or {}
        return str(
            entity.get("canonical_name")
            or argument.get("normalized_value")
            or argument.get("surface")
            or ""
        )

    output = []
    for row in payloads[PipelineStage.OPENIE_ASSERTION_ASSEMBLY_COMPLETE.value]["assertions"]:
        if row["lane"] != "QUALIFIED_CLAIM":
            continue
        output.append({
            "subject": argument_name(row["subject_argument_id"]),
            "predicate": row.get("canonical_predicate") or row.get("surface_relation"),
            "object": argument_name(row["object_argument_id"]),
            "lane": row["lane"],
            "qualifiers": row.get("qualifiers") or {},
            "evidence_start": row.get("evidence_start"),
            "evidence_end": row.get("evidence_end"),
        })
    return output


def _score(
    *,
    gold: dict[str, Any],
    promoted: list[dict[str, Any]],
    qualified: list[dict[str, Any]],
) -> dict[str, Any]:
    gold_rows = list(gold["positive_canonical_triples"])
    used_predictions: set[int] = set()
    comparisons: list[dict[str, Any]] = []
    for gold_row in gold_rows:
        match_index = next(
            (
                index for index, predicted in enumerate(promoted)
                if index not in used_predictions and _triple_matches(gold_row, predicted)
            ),
            None,
        )
        if match_index is not None:
            used_predictions.add(match_index)
        comparisons.append({
            "id": gold_row["id"],
            "status": "matched" if match_index is not None else "missing",
            "gold": gold_row,
            "prediction": promoted[match_index] if match_index is not None else None,
        })

    qualified_gold = list(gold.get("qualified_statements", []))
    leaked = [
        row for row in qualified_gold
        if any(_triple_matches(row, predicted) for predicted in promoted)
    ]
    retained = [
        row for row in qualified_gold
        if any(_triple_matches(row, candidate) for candidate in qualified)
    ]
    matched = sum(row["status"] == "matched" for row in comparisons)
    precision = matched / len(promoted) if promoted else 0.0
    recall = matched / len(gold_rows) if gold_rows else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    healthy_min, healthy_max = gold["grading"]["acceptable_positive_canonical_range"]
    return {
        "status": "passed" if (
            healthy_min <= len(promoted) <= healthy_max
            and recall >= 0.80
            and not leaked
        ) else "failed",
        "counts": {
            "gold_positive": len(gold_rows),
            "neo4j_promoted_positive": len(promoted),
            "matched_positive": matched,
            "missing_positive": len(gold_rows) - matched,
            "unmatched_promoted": len(promoted) - len(used_predictions),
            "qualified_gold": len(qualified_gold),
            "qualified_retained": len(retained),
            "qualified_leaked_to_positive": len(leaked),
        },
        "metrics": {"precision": precision, "recall": recall, "f1": f1},
        "gates": {
            "positive_count_58_to_72": healthy_min <= len(promoted) <= healthy_max,
            "positive_recall_at_least_0_80": recall >= 0.80,
            "qualified_positive_leakage_zero": not leaked,
        },
        "matched_ids": [row["id"] for row in comparisons if row["status"] == "matched"],
        "missing_ids": [row["id"] for row in comparisons if row["status"] == "missing"],
        "qualified_leaks": leaked,
        "qualified_retained_ids": [row["id"] for row in retained],
        "comparisons": comparisons,
    }


async def _run(args: argparse.Namespace) -> int:
    source = Path(args.input).resolve()
    answer_key_path = Path(args.answer_key).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    gold = json.loads(answer_key_path.read_text(encoding="utf-8"))
    stores = _Stores(args.namespace)
    started = time.perf_counter()
    try:
        await _reset_namespace(stores)
        ingest = await _ingest_fixture(stores, source)
        await _project_document(stores, str(ingest["doc_id"]))
        initial = await _graph_snapshot(stores)
        promoted = await _promoted_triples(stores)
        payloads = await _stage_payloads(stores, str(ingest["doc_id"]))
        qualified = _qualified_rows(payloads)
        score = _score(gold=gold, promoted=promoted, qualified=qualified)

        await stores.db["graph_projection_jobs"].delete_many({"corpus_id": stores.corpus_id})
        await _project_document(stores, str(ingest["doc_id"]))
        repeated = await _graph_snapshot(stores)
        idempotent = initial["digest"] == repeated["digest"] and initial["counts"] == repeated["counts"]
        elapsed = time.perf_counter() - started

        (output_dir / "neo4j_promoted_triples.json").write_text(
            json.dumps(promoted, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output_dir / "qualified_assertions.json").write_text(
            json.dumps(qualified, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        comparison_rows = "\n".join(
            json.dumps(row, sort_keys=True) for row in score.pop("comparisons")
        )
        (output_dir / "answer_key_comparison.jsonl").write_text(
            comparison_rows + ("\n" if comparison_rows else ""), encoding="utf-8"
        )
        score["status"] = "passed" if score["status"] == "passed" and idempotent else "failed"
        score["gates"]["projection_idempotent"] = idempotent
        score["source"] = str(source)
        score["answer_key"] = str(answer_key_path)
        score["namespace"] = args.namespace
        score["mongo_database"] = stores.db_name
        score["corpus_id"] = stores.corpus_id
        score["document_id"] = ingest["doc_id"]
        score["elapsed_seconds"] = elapsed
        score["graph"] = {
            "counts": initial["counts"],
            "digest": initial["digest"],
            "repeat_digest": repeated["digest"],
        }
        score["artifact_digest"] = stable_digest({
            "score": score,
            "promoted": promoted,
            "qualified": qualified,
        })
        (output_dir / "score.json").write_text(
            json.dumps(score, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps({
            "output_dir": str(output_dir),
            "status": score["status"],
            "counts": score["counts"],
            "metrics": score["metrics"],
            "gates": score["gates"],
            "elapsed_seconds": elapsed,
        }, indent=2, sort_keys=True))
        return 0 if score["status"] == "passed" else 1
    finally:
        await stores.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--answer-key", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--output-dir", required=True)
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
