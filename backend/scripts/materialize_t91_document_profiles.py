"""Dry-run-first materializer for additive T9.1 document profiles.

Examples (inside the backend container):
  PYTHONPATH=/app python -m scripts.materialize_t91_document_profiles \
    --corpus-name polymath_e2e_full_20260715
  PYTHONPATH=/app python -m scripts.materialize_t91_document_profiles \
    --corpus-id <discovered-id> --expected-documents 15 --apply

No provider route is imported or called.  ``--apply`` writes only the new
``t91_document_profiles`` collection and never updates source collections.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from models.document_semantic_profile import PROFILE_COLLECTION
from services.ingestion.document_semantic_profile import (
    DocumentProfileCompilationError,
    compile_document_profile,
)
from services.settings import settings_service
from services.storage.record_status import with_active_records


async def _database() -> tuple[AsyncIOMotorClient, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI, tz_aware=True)
    try:
        database = client.get_default_database()
    except Exception:
        database = client[settings.MONGODB_DATABASE]
    settings_service.attach(database)
    return client, database


async def _discover_corpus(
    database: Any,
    *,
    corpus_id: str | None,
    corpus_name: str | None,
) -> dict[str, Any]:
    query: dict[str, Any] = with_active_records({})
    if corpus_id:
        query["corpus_id"] = corpus_id
    else:
        query["name"] = corpus_name
    rows = await database["corpora"].find(query, {"_id": 0}).to_list(length=2)
    if len(rows) != 1:
        raise DocumentProfileCompilationError(
            f"corpus discovery must resolve exactly one row; found {len(rows)}"
        )
    return rows[0]


def _document_ordinals(
    parents: list[dict[str, Any]],
    extraction_rows: list[dict[str, Any]],
) -> dict[str, tuple[int, str]]:
    """Derive stable document ordinals without mutating source child rows."""

    ordered: list[tuple[str, str]] = []
    for parent in sorted(parents, key=lambda row: str(row.get("parent_id") or "")):
        parent_id = str(parent.get("parent_id") or "")
        seen: set[str] = set()
        for value in parent.get("child_ids") or []:
            child_id = str(value or "")
            if child_id and child_id not in seen:
                seen.add(child_id)
                ordered.append((child_id, parent_id))
    known = {child_id for child_id, _ in ordered}
    ordered.extend(
        (str(row.get("chunk_id") or ""), "")
        for row in sorted(
            extraction_rows,
            key=lambda row: str(row.get("chunk_id") or ""),
        )
        if str(row.get("chunk_id") or "") not in known
    )
    if len({child_id for child_id, _ in ordered}) != len(ordered):
        raise DocumentProfileCompilationError(
            "parent child ordering contains duplicate child ownership"
        )
    return {
        child_id: (ordinal, parent_id)
        for ordinal, (child_id, parent_id) in enumerate(ordered)
    }


async def materialize(
    database: Any,
    *,
    corpus_id: str | None,
    corpus_name: str | None,
    expected_documents: int | None,
    apply: bool,
) -> dict[str, Any]:
    corpus = await _discover_corpus(
        database,
        corpus_id=corpus_id,
        corpus_name=corpus_name,
    )
    resolved_corpus_id = str(corpus.get("corpus_id") or "")
    documents = (
        await database["documents"]
        .find(
            with_active_records({"corpus_id": resolved_corpus_id}),
            {"_id": 0},
        )
        .sort("doc_id", 1)
        .to_list(length=None)
    )
    if expected_documents is not None and len(documents) != expected_documents:
        raise DocumentProfileCompilationError(
            f"document census drifted: expected {expected_documents}, "
            f"found {len(documents)}"
        )

    profiles = []
    counts: Counter[str] = Counter()
    for document in documents:
        doc_id = str(document.get("doc_id") or "")
        parents = (
            await database["parent_chunks"]
            .find(
                with_active_records(
                    {"corpus_id": resolved_corpus_id, "doc_id": doc_id}
                ),
                {"_id": 0},
            )
            .sort("parent_id", 1)
            .to_list(length=None)
        )
        extraction_rows = (
            await database["ghost_b_extractions"]
            .find(
                {
                    "corpus_id": resolved_corpus_id,
                    "doc_id": doc_id,
                    "status": "ok",
                    "local_extraction": {"$exists": True},
                    "claim_compilation": {"$exists": True},
                },
                {"_id": 0},
            )
            .sort("chunk_id", 1)
            .to_list(length=None)
        )
        ordinals = _document_ordinals(parents, extraction_rows)
        shaped_extractions = []
        for row in extraction_rows:
            child_id = str(row.get("chunk_id") or "")
            ordinal, parent_id = ordinals[child_id]
            shaped_extractions.append(
                {
                    **row,
                    "_document_ordinal": ordinal,
                    "_parent_id": parent_id,
                }
            )
        profile = compile_document_profile(
            document=document,
            parent_rows=parents,
            extraction_rows=shaped_extractions,
        )
        profiles.append(profile)
        counts["documents"] += 1
        counts["parents"] += len(parents)
        counts["extraction_rows"] += len(extraction_rows)
        counts["domains"] += len(profile.domain_ids)
        counts["superframes"] += len(profile.superframe_ids)
        counts["motifs"] += len(profile.motif_ids)
        counts["concept_terms"] += len(profile.concept_terms)

    inserted = 0
    reused = 0
    if apply:
        collection = database[PROFILE_COLLECTION]
        for profile in profiles:
            row = profile.model_dump(mode="python")
            existing = await collection.find_one(
                {"profile_id": profile.profile_id},
                {"_id": 0, "profile_hash": 1},
            )
            if existing:
                if str(existing.get("profile_hash") or "") != profile.profile_hash:
                    raise DocumentProfileCompilationError(
                        "existing profile logical ID has a different revision"
                    )
                reused += 1
                continue
            result = await collection.update_one(
                {"profile_id": profile.profile_id},
                {"$setOnInsert": row},
                upsert=True,
            )
            inserted += int(result.upserted_id is not None)
            reused += int(result.upserted_id is None)

    return {
        "schema_version": "t91_document_profile_materialization_receipt.v1",
        "mode": "apply" if apply else "dry_run",
        "corpus_id": resolved_corpus_id,
        "corpus_name": str(corpus.get("name") or ""),
        "source_counts": dict(sorted(counts.items())),
        "profile_count": len(profiles),
        "profile_hashes": sorted(profile.profile_hash for profile in profiles),
        "inserted_count": inserted,
        "reused_count": reused,
        "target_collection": PROFILE_COLLECTION,
        "source_rows_mutated": 0,
        "llm_call_count": 0,
        "provider_spend_usd": 0.0,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--corpus-id")
    scope.add_argument("--corpus-name")
    parser.add_argument("--expected-documents", type=int)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="insert additive profile rows; default is compile-only dry-run",
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    client, database = await _database()
    try:
        receipt = await materialize(
            database,
            corpus_id=args.corpus_id,
            corpus_name=args.corpus_name,
            expected_documents=args.expected_documents,
            apply=bool(args.apply),
        )
        print(json.dumps(receipt, sort_keys=True))
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(_main())
