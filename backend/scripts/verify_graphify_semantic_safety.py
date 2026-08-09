#!/usr/bin/env python3
"""Verify strict frozen semantics without modifying the frozen scorer or policy."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from models.graphify_contracts import PipelineStage  # noqa: E402
from scripts.run_graphify_fixture_e2e import _Stores, _stage_payloads  # noqa: E402

MATCH_CLASSES = {
    "EXACT", "DECLARED_ALIAS", "NORMALIZED_VARIANT",
    "DECLARED_SUBSUMPTION", "NO_MATCH",
}
FAILURE_CLASSES = {
    "MISSING_PAIR", "ARGUMENT_ALIGNMENT", "ENTITY_TYPE", "PREDICATE_MAPPING",
    "DIRECTION", "POLARITY", "MODALITY", "ATTRIBUTION", "CONDITIONAL_SCOPE",
    "GATE_POLICY",
}
SEMANTIC_ZERO_CLASSES = {
    "ARGUMENT_ALIGNMENT", "DIRECTION", "POLARITY", "MODALITY",
    "ATTRIBUTION", "CONDITIONAL_SCOPE", "PREDICATE_MAPPING",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _invalid(value: str) -> bool:
    return re.search(r"[A-Za-z0-9]", value.strip()) is None


async def _verify(args: argparse.Namespace) -> int:
    freeze_dir = Path(args.freeze_dir).resolve()
    run_dir = Path(args.run_dir).resolve()
    freeze = _load(freeze_dir / "STRICT_FREEZE_V2.json")
    score = _load(run_dir / "score_v4.json")
    receipt = _load(run_dir / "extraction_receipt.json")

    file_checks: dict[str, bool] = {}
    for name in ("fixture", "answer_key"):
        spec = freeze[name]
        path = freeze_dir / spec["path"]
        file_checks[f"{name}_sha256"] = _sha256(path) == spec["sha256"]
    file_checks["fixture_bytes"] = (
        (freeze_dir / freeze["fixture"]["path"]).stat().st_size
        == freeze["fixture"]["bytes"]
    )
    policy = freeze["matching_policy"]
    file_checks["matching_policy_documentation_sha256"] = (
        _sha256(freeze_dir / policy["documentation_path"])
        == policy["documentation_sha256"]
    )
    file_checks["matching_policy_sha256"] = (
        _sha256(freeze_dir / policy["policy_path"]) == policy["policy_sha256"]
    )
    scorer_path = REPO_ROOT / freeze["scorer"]["path"]
    file_checks["scorer_sha256"] = _sha256(scorer_path) == freeze["scorer"]["sha256"]

    stores = _Stores(freeze["namespace"]["name"])
    try:
        payloads = await _stage_payloads(stores, freeze["namespace"]["document_id"])
        relation_payload = payloads[PipelineStage.RELATION_COMPILATION_COMPLETE.value]
        completion_payload = payloads[PipelineStage.MENTION_COMPLETION_COMPLETE.value]
        reducer_payload = payloads[PipelineStage.ENTITY_REDUCTION_COMPLETE.value]
        argument_payload = payloads[PipelineStage.OPENIE_ARGUMENT_ADAPTATION_COMPLETE.value]
        openie_payload = payloads[PipelineStage.OPENIE_ASSERTION_ASSEMBLY_COMPLETE.value]

        mention_by_id = {
            row["mention_id"]: row
            for row in [
                *completion_payload["mentions"], *relation_payload["endpoint_mentions"],
            ]
        }
        entity_by_id = {
            row["entity_id"]: row
            for row in [*reducer_payload["entities"], *relation_payload["endpoint_entities"]]
        }
        wildcard_terminal_counts: Counter[str] = Counter()
        for row in relation_payload["mapped_relations"]:
            subject_mention = mention_by_id[row["subject_mention_id"]]
            object_mention = mention_by_id[row["object_mention_id"]]
            values = (
                subject_mention["surface"], object_mention["surface"],
                entity_by_id[subject_mention["entity_id"]]["canonical_name"],
                entity_by_id[object_mention["entity_id"]]["canonical_name"],
            )
            if any(_invalid(str(value)) for value in values):
                wildcard_terminal_counts[str(row["terminal_state"])] += 1

        argument_by_id = {
            row["argument_id"]: row for row in argument_payload["arguments"]
        }
        wildcard_openie_lane_counts: Counter[str] = Counter()
        for row in openie_payload["assertions"]:
            values = (
                argument_by_id[row["subject_argument_id"]]["surface"],
                argument_by_id[row["object_argument_id"]]["surface"],
            )
            if any(_invalid(str(value)) for value in values):
                wildcard_openie_lane_counts[str(row["lane"])] += 1

        query = """
            MATCH (e:Entity)
            WHERE EXISTS {
                MATCH (c:Chunk {corpus_id: $corpus_id})-[:MENTIONS]->(e)
            } OR EXISTS {
                MATCH (e)-[r:RELATES_TO]-()
                WHERE $corpus_id IN coalesce(r.corpus_ids, [])
            }
            RETURN coalesce(e.canonical_name, e.entity_id, '') AS name
        """
        async with stores.neo4j.session() as session:
            result = await session.run(query, corpus_id=stores.corpus_id)
            neo4j_entity_names = [str(row["name"]) async for row in result]
    finally:
        await stores.close()

    namespace = freeze["namespace"]
    taxonomy = score["failure_taxonomy_counts"]
    checkpoint_recall = score["recall_checkpoints"]["recall"]
    gates = {
        **file_checks,
        "frozen_namespace": score["namespace"] == namespace["name"],
        "frozen_mongo_database": receipt["mongo_database"] == namespace["mongo_database"],
        "frozen_corpus_id": receipt["corpus_id"] == namespace["corpus_id"],
        "frozen_document_id": receipt["document_id"] == namespace["document_id"],
        "production_writes_unauthorized": namespace["production_writes_authorized"] is False,
        "scorer_version": score["scorer_version"] == freeze["scorer"]["version"],
        "matching_policy_version": score["matching_policy_version"] == policy["version"],
        "matching_policy_digest_reported": score["matching_policy_sha256"] == policy["policy_sha256"],
        "all_five_match_classes_reported": set(score["match_class_counts"]) == MATCH_CLASSES,
        "all_ten_failure_classes_reported": set(taxonomy) == FAILURE_CLASSES,
        "prohibited_matchers_zero": not score["prohibited_matchers_used"],
        "precision_at_least_0_90": score["metrics"]["precision"] >= 0.90,
        "canonical_recall_at_least_0_90": score["metrics"]["recall"] >= 0.90,
        "pair_checkpoint_at_least_0_90": checkpoint_recall["endpoint_pair_generated"] >= 0.90,
        "surface_checkpoint_at_least_0_90": checkpoint_recall["correct_surface_proposition_generated"] >= 0.90,
        "canonical_checkpoint_at_least_0_90": checkpoint_recall["correct_canonical_assertion_promoted"] >= 0.90,
        "semantic_error_classes_zero": all(taxonomy[name] == 0 for name in SEMANTIC_ZERO_CLASSES),
        "qualified_positive_leakage_zero": score["counts"]["qualified_leaked_to_positive"] == 0,
        "projection_idempotent": receipt["gates"]["projection_idempotent"] is True,
        "syntax_wildcard_accepted_zero": wildcard_terminal_counts["accepted"] == 0,
        "syntax_wildcard_qualified_zero": wildcard_terminal_counts["qualified"] == 0,
        "syntax_wildcard_review_zero": wildcard_terminal_counts["review"] == 0,
        "openie_wildcard_fact_zero": wildcard_openie_lane_counts["FACT"] == 0,
        "openie_wildcard_qualified_zero": wildcard_openie_lane_counts["QUALIFIED_CLAIM"] == 0,
        "openie_wildcard_review_zero": wildcard_openie_lane_counts["REVIEW"] == 0,
        "neo4j_entities_present": len(neo4j_entity_names) > 0,
        "neo4j_wildcard_entities_zero": not any(_invalid(name) for name in neo4j_entity_names),
    }
    report = {
        "status": "passed" if all(gates.values()) else "failed",
        "freeze_id": freeze["freeze_id"],
        "run_dir": str(run_dir),
        "gates": gates,
        "metrics": score["metrics"],
        "counts": score["counts"],
        "match_class_counts": score["match_class_counts"],
        "failure_taxonomy_counts": taxonomy,
        "recall_checkpoints": score["recall_checkpoints"],
        "wildcard_terminal_counts": dict(sorted(wildcard_terminal_counts.items())),
        "wildcard_openie_lane_counts": dict(sorted(wildcard_openie_lane_counts.items())),
        "neo4j_entity_count": len(neo4j_entity_names),
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-dir", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", required=True)
    return asyncio.run(_verify(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
