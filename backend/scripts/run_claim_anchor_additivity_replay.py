#!/usr/bin/env python3
"""Replay atomic anchors over one sealed OFF final-evidence packet.

This is the treatment half of the claim-anchor micro A/B. Retrieval runs only
in the OFF arm. The treatment consumes those exact final sources after
selection, so the experiment measures the additive annotation seam without
confounding it with independent ANN/reranker run-to-run variation.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_settings
from models.schemas import SourceChunk
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from scripts.run_claim_anchor_micro_ab import (
    DEFAULT_SPEC,
    _atomic_write,
    _fingerprint,
    _load_contract,
    _source_fingerprint,
    _validate_anchor,
)
from services.context_manager import context_manager
from services.retriever.atomic_claim_anchors import (
    attach_atomic_claim_anchors,
    source_additivity_receipt,
)
from services.retriever.claim_anchor_rendering import render_claim_proposition


V2_SPEC = (
    Path(__file__).resolve().parents[1] / "evals" / "claim_anchor_join_micro_ab_v2.json"
)
SEALED_V1_OFF_SHA256 = (
    "fd02ed0abb93f4017c4adbaefaa7ad557a3454d916173f07bfd039cbbf0424e0"
)
V2_SCHEMA = "claim_anchor_join_micro_ab.v2"
V2_COMPATIBILITY_KEYS = (
    "heldout_questions_sha256",
    "tier",
    "corpus_name",
    "model_contract",
    "query_ids",
)


def _validate_off_contract(
    *,
    spec: dict[str, Any],
    off: dict[str, Any],
    off_path: Path,
) -> None:
    if spec.get("schema_version") != V2_SCHEMA:
        if off.get("spec") != spec:
            raise RuntimeError("OFF artifact preregistration does not match")
        return
    actual_sha = hashlib.sha256(off_path.read_bytes()).hexdigest()
    if actual_sha != SEALED_V1_OFF_SHA256:
        raise RuntimeError("v2 re-window OFF artifact SHA drifted")
    off_spec = off.get("spec") or {}
    if any(off_spec.get(key) != spec.get(key) for key in V2_COMPATIBILITY_KEYS):
        raise RuntimeError("v2 re-window OFF selection contract drifted")


def _source_keys(sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "corpus_id": str(source.get("corpus_id") or ""),
            "doc_id": str(source.get("doc_id") or ""),
            "chunk_id": str(source.get("chunk_id") or ""),
            "parent_id": str(source.get("parent_id") or ""),
        }
        for source in sources
    ]


def _anchor_rows(
    sources: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for source in sources:
        for anchor in (source.get("metadata") or {}).get("atomic_claim_anchors") or []:
            if isinstance(anchor, dict):
                rows.append((source, anchor))
    return rows


def _prompt_anchor_count(prompt: str) -> int:
    start = prompt.find("<atomic_claim_anchors>")
    end = prompt.find("</atomic_claim_anchors>", start + 1)
    if start < 0 or end < 0:
        return 0
    return sum(line.startswith('- From "') for line in prompt[start:end].splitlines())


async def _replay(
    *,
    settings: Any,
    sync_db: Any,
    questions: list[dict[str, Any]],
    off_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    motor = AsyncIOMotorClient(settings.MONGODB_URI)
    async_db = motor[settings.MONGODB_DATABASE]
    results: list[dict[str, Any]] = []
    try:
        for question, off_row in zip(questions, off_rows, strict=True):
            started = time.perf_counter()
            off_sources = copy.deepcopy(off_row.get("selected_sources") or [])
            if not off_sources:
                raise RuntimeError(
                    f"{question['id']} OFF artifact has no sealed selected_sources"
                )
            selected = [SourceChunk.model_validate(source) for source in off_sources]
            before = source_additivity_receipt(selected)
            enriched, diagnostics = await attach_atomic_claim_anchors(
                async_db,
                selected,
                query=question["question"],
                per_source=settings.ATOMIC_CLAIM_ANCHORS_PER_SOURCE,
                total=settings.ATOMIC_CLAIM_ANCHORS_TOTAL,
            )
            after = source_additivity_receipt(enriched)
            enriched_payload = [source.model_dump(mode="json") for source in enriched]
            anchors = _anchor_rows(enriched_payload)
            checks = [
                _validate_anchor(sync_db, source=source, anchor=anchor)
                for source, anchor in anchors
            ]
            raw_claims_before_render = [
                str(anchor.get("claim_text") or "") for _, anchor in anchors
            ]
            prompt = context_manager.build_augmented_prompt(
                question["question"],
                enriched,
            )
            after_render_payload = [
                source.model_dump(mode="json") for source in enriched
            ]
            raw_claims_after_render = [
                str(anchor.get("claim_text") or "")
                for _, anchor in _anchor_rows(after_render_payload)
            ]
            cleaned_count = sum(
                int(
                    render_claim_proposition(
                        raw,
                        exact_sentence=str(anchor.get("exact_sentence") or ""),
                    )
                    != raw
                )
                for raw, (_, anchor) in zip(
                    raw_claims_before_render,
                    anchors,
                    strict=True,
                )
            )
            source_keys = _source_keys(enriched_payload)
            valid_count = sum(int(check["valid"]) for check in checks)
            source_ids_equal = source_keys == off_row["source_keys"]
            evidence_bytes_equal = (
                before["non_anchor_evidence_sha256"]
                == after["non_anchor_evidence_sha256"]
                and before["non_anchor_evidence_bytes"]
                == after["non_anchor_evidence_bytes"]
                and _source_fingerprint(enriched_payload)
                == off_row["selected_evidence_sha256_without_anchors"]
            )
            result = {
                "query_id": question["id"],
                "shape": question["shape"],
                "source_keys": source_keys,
                "source_count": len(source_keys),
                "off_source_ids_sha256": before["source_ids_sha256"],
                "on_source_ids_sha256": after["source_ids_sha256"],
                "off_non_anchor_evidence_sha256": before["non_anchor_evidence_sha256"],
                "on_non_anchor_evidence_sha256": after["non_anchor_evidence_sha256"],
                "source_ids_equal": source_ids_equal,
                "non_anchor_evidence_bytes_equal": evidence_bytes_equal,
                "service_additivity_verified": diagnostics.get("additivity_verified"),
                "anchor_count": len(anchors),
                "valid_anchor_count": valid_count,
                "all_citations_valid": (
                    all(check["valid"] for check in checks) if checks else None
                ),
                "prompt_render_count": _prompt_anchor_count(prompt),
                "raw_claim_text_preserved": (
                    raw_claims_before_render == raw_claims_after_render
                ),
                "render_cleaned_claim_count": cleaned_count,
                "diagnostics": diagnostics,
                "model_contract": off_row.get("model_used"),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
            results.append(result)
            print(
                json.dumps(
                    {
                        "query_id": result["query_id"],
                        "sources_equal": source_ids_equal,
                        "evidence_equal": evidence_bytes_equal,
                        "anchors": len(anchors),
                        "valid": valid_count,
                        "rendered": result["prompt_render_count"],
                        "raw_preserved": result["raw_claim_text_preserved"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    finally:
        motor.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, default=V2_SPEC)
    parser.add_argument("--off-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    spec, questions = _load_contract(args.spec)
    settings = get_settings()
    if not settings.ATOMIC_CLAIM_ANCHORS_ENABLED:
        raise RuntimeError("additivity replay requires claim-anchor flag ON")
    off = json.loads(args.off_artifact.read_text(encoding="utf-8"))
    if off.get("arm") != "off" or not off.get("passed"):
        raise RuntimeError("OFF artifact is absent or did not pass")
    _validate_off_contract(spec=spec, off=off, off_path=args.off_artifact)
    off_rows = list(off.get("results") or [])
    if [row.get("query_id") for row in off_rows] != spec["query_ids"]:
        raise RuntimeError("OFF artifact query order drifted")

    mongo = MongoClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    try:
        if spec.get("schema_version") == V2_SCHEMA:
            if not settings.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED:
                raise RuntimeError("v2 re-window requires relationship allocation ON")
            if not settings.ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED:
                raise RuntimeError("v2 re-window requires corpus-scope v2 ON")
            if settings.TEMPORAL_QUERY_ROUTING_ENABLED:
                raise RuntimeError("v2 re-window requires temporal routing OFF")
        before = _fingerprint(db, str(off["corpus_id"]))
        results = asyncio.run(
            _replay(
                settings=settings,
                sync_db=db,
                questions=questions,
                off_rows=off_rows,
            )
        )
        after = _fingerprint(db, str(off["corpus_id"]))
        failures: list[str] = []
        for row in results:
            if not row["source_ids_equal"]:
                failures.append(f"{row['query_id']}:source_identity")
            if not row["non_anchor_evidence_bytes_equal"]:
                failures.append(f"{row['query_id']}:non_anchor_evidence")
            if row["service_additivity_verified"] is not True:
                failures.append(f"{row['query_id']}:service_additivity")
            if row["anchor_count"] and not row["all_citations_valid"]:
                failures.append(f"{row['query_id']}:citation_invalid")
            if not row["raw_claim_text_preserved"]:
                failures.append(f"{row['query_id']}:raw_claim_mutated")
            if (
                spec.get("schema_version") == V2_SCHEMA
                and row["prompt_render_count"] != row["anchor_count"]
            ):
                failures.append(f"{row['query_id']}:not_all_anchors_rendered")
            if row["model_contract"] != spec["model_contract"]:
                failures.append(f"{row['query_id']}:model_contract")

        total_anchors = sum(int(row["anchor_count"]) for row in results)
        total_valid = sum(int(row["valid_anchor_count"]) for row in results)
        q021 = next(row for row in results if row["query_id"] == "q021")
        if spec.get("schema_version") == V2_SCHEMA:
            if total_anchors < int(spec["minimum_structural_anchor_count_when_on"]):
                failures.append("structural_anchor_count_below_minimum")
            if total_valid != total_anchors:
                failures.append("not_all_emitted_anchors_valid")
        else:
            if total_anchors != int(spec["expected_structural_anchor_count_when_on"]):
                failures.append("structural_anchor_count")
            if total_valid != int(
                spec["expected_structurally_valid_anchor_count_when_on"]
            ):
                failures.append("structurally_valid_anchor_count")
        if q021["prompt_render_count"] < int(spec["q021_min_rendered_anchors_when_on"]):
            failures.append("q021:rendered_anchor_floor")
        if before != after:
            failures.append("corpus_fingerprint_changed")

        output = {
            "schema_version": (
                "claim_anchor_additivity_replay.v2"
                if spec.get("schema_version") == V2_SCHEMA
                else "claim_anchor_additivity_replay.v1"
            ),
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "arm": "on_post_final_selection_replay",
            "runtime_flag_enabled": True,
            "spec": spec,
            "corpus_id": off["corpus_id"],
            "off_artifact": str(args.off_artifact),
            "model_contract": spec["model_contract"],
            "corpus_fingerprint_before": before,
            "corpus_fingerprint_after": after,
            "corpus_fingerprint_equal": before == after,
            "total_anchor_count": total_anchors,
            "total_valid_anchor_count": total_valid,
            "structural_citation_precision": (
                total_valid / total_anchors if total_anchors else None
            ),
            "results": results,
            "failures": failures,
            "passed": not failures,
        }
        _atomic_write(args.output, output)
        print(
            json.dumps(
                {
                    "passed": not failures,
                    "failures": failures,
                    "anchors": total_anchors,
                    "valid": total_valid,
                    "corpus_fingerprint_equal": before == after,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0 if not failures else 1
    finally:
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
