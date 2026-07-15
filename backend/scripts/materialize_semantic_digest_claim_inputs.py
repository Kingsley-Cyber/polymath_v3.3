#!/usr/bin/env python3
"""Materialize and audit B2 atomic-claim inputs for mark digest packets.

``scope`` and ``packet-census`` are read-only. ``export`` writes only a raw
temporary JSONL under /tmp. ``import`` performs full-file canonical-image
validation before additive, immutable, canonical_write=false Mongo upserts.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

from config import get_settings
from models.hash_taxonomy import canonical_json_v1, namespace_hash
from models.semantic_digest_claim_input import (
    COMPILATION_COLLECTION,
    CompiledChildCandidateExportV1,
    ClaimCompilationMaterializationRowV1,
    parse_materialized_row_document,
)
from services.ingestion.semantic_digest_claim_inputs import (
    PARSER_VERSION,
    SPACY_LIBRARY_VERSION,
    SPACY_MODEL,
    SPACY_MODEL_VERSION,
    PacketNotReadyError,
    build_bounded_atomic_parent_packet,
    compile_child_candidate,
    document_source_version_id,
    materialize_candidate_row,
    validate_materialized_row_against_source,
)
from services.ingestion.semantic_parent_eligibility import (
    classify_parent_text_v2,
    parent_eligibility_recipe_hash,
)
from services.ingestion.paid_cost_reservation import worst_case_authority_usd
from services.settings import settings_service
from scripts.semantic_gateway_ugo_canary import (
    _canonical_store_census,
    _canonical_store_census_receipt,
)

SCHEMA_VERSION = "polymath.semantic_digest_claim_input_materialization.v1"
DEFAULT_CORPUS_NAME = "markbuildsbrands_transcripts"
PRICE_CARD_PATH = (
    Path(__file__).resolve().parents[1]
    / "registries"
    / "semantic_gateway_provider_prices.v1.json"
)
ROUTE_CARD_PATH = (
    Path(__file__).resolve().parents[1]
    / "registries"
    / "semantic_gateway_route_parameters.v1.json"
)
ROUTE_ID = "longcat-api__longcat-2.0"
HISTORICAL_JOB_COLLECTION = "semantic_digest_jobs"


class MaterializationError(RuntimeError):
    """A source, export, import, or packet census invariant failed."""


@dataclass(frozen=True)
class Scope:
    corpus_id: str
    parents: list[dict[str, Any]]
    child_ids: list[str]
    children: dict[str, dict[str, Any]]
    documents: dict[str, dict[str, Any]]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_tmp_path(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(Path("/tmp").resolve()):
        raise MaterializationError("raw claim compilation files must stay under /tmp")
    return resolved


def _persist_before_census(path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Persist a crash-surviving, count-only BEFORE receipt before mutation."""

    output_path = _require_tmp_path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".partial")
    temp_path.write_text(canonical_json_v1(receipt) + "\n", encoding="utf-8")
    temp_path.replace(output_path)
    return {
        "file_bytes": output_path.stat().st_size,
        "file_sha256": _file_sha256(output_path),
        "location": "/tmp only; not committed",
    }


async def _database() -> tuple[AsyncIOMotorClient, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI, tz_aware=True)
    try:
        db = client.get_default_database()
    except Exception:
        db = client[settings.MONGODB_DATABASE]
    settings_service.attach(db)
    return client, db


async def _load_scope(
    db: Any,
    *,
    corpus_name: str,
    expected_parent_count: int,
    expected_child_count: int | None,
) -> Scope:
    corpora = (
        await db["corpora"]
        .find(
            {"name": corpus_name, "status": {"$ne": "deleted"}},
            {"_id": 0, "corpus_id": 1},
        )
        .to_list(length=3)
    )
    if len(corpora) != 1:
        raise MaterializationError(
            f"expected one active corpus named {corpus_name!r}; found {len(corpora)}"
        )
    corpus_id = str(corpora[0].get("corpus_id") or "")
    structural = (
        await db["parent_chunks"]
        .find(
            {
                "corpus_id": corpus_id,
                "validation_status": "valid",
                "text": {"$exists": True, "$nin": [None, ""]},
                "child_ids.0": {"$exists": True},
            },
            {
                "_id": 0,
                "parent_id": 1,
                "doc_id": 1,
                "text": 1,
                "child_ids": 1,
                "validation_status": 1,
            },
        )
        .sort("parent_id", 1)
        .to_list(length=None)
    )
    parents = [
        row for row in structural if classify_parent_text_v2(row["text"]).eligible
    ]
    if len(parents) != expected_parent_count:
        raise MaterializationError(
            f"eligible parent census drifted: expected {expected_parent_count}, "
            f"found {len(parents)}"
        )
    child_ids = sorted(
        {
            str(child_id)
            for parent in parents
            for child_id in parent.get("child_ids") or []
            if child_id
        }
    )
    if expected_child_count is not None and len(child_ids) != expected_child_count:
        raise MaterializationError(
            f"child census drifted: expected {expected_child_count}, "
            f"found {len(child_ids)}"
        )
    child_rows = (
        await db["chunks"]
        .find(
            {"corpus_id": corpus_id, "chunk_id": {"$in": child_ids}},
            {"_id": 0, "chunk_id": 1, "doc_id": 1, "text": 1, "status": 1},
        )
        .sort("chunk_id", 1)
        .to_list(length=None)
    )
    children = {str(row.get("chunk_id") or ""): row for row in child_rows}
    if set(children) != set(child_ids):
        raise MaterializationError("eligible parent child IDs do not close in chunks")
    document_ids = sorted({str(row.get("doc_id") or "") for row in child_rows})
    document_rows = (
        await db["documents"]
        .find(
            {"corpus_id": corpus_id, "doc_id": {"$in": document_ids}},
            {"_id": 0, "doc_id": 1, "source_identity": 1},
        )
        .sort("doc_id", 1)
        .to_list(length=None)
    )
    documents = {str(row.get("doc_id") or ""): row for row in document_rows}
    if set(documents) != set(document_ids):
        raise MaterializationError("eligible child document IDs do not close")
    for child in child_rows:
        document_id = str(child.get("doc_id") or "")
        if not isinstance(child.get("text"), str) or not child["text"].strip():
            raise MaterializationError("eligible child has empty text")
        document_source_version_id(documents[document_id])
    return Scope(
        corpus_id=corpus_id,
        parents=parents,
        child_ids=child_ids,
        children=children,
        documents=documents,
    )


async def _collection_disclosure(db: Any, corpus_id: str) -> dict[str, int]:
    collection = db[COMPILATION_COLLECTION]
    total = await collection.count_documents({"corpus_id": corpus_id})
    noncanonical = await collection.count_documents(
        {"corpus_id": corpus_id, "canonical_write": False}
    )
    canonical_or_missing = await collection.count_documents(
        {
            "corpus_id": corpus_id,
            "$or": [
                {"canonical_write": {"$ne": False}},
                {"canonical_write": {"$exists": False}},
            ],
        }
    )
    return {
        "row_count": total,
        "canonical_write_false_count": noncanonical,
        "canonical_or_missing_flag_count": canonical_or_missing,
    }


def _receipt_base(
    command: str,
    scope: Scope,
    *,
    corpus_name: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "corpus": {
            "name": corpus_name,
            "corpus_id": scope.corpus_id,
            "eligible_parent_count": len(scope.parents),
            "unique_child_count": len(scope.child_ids),
            "document_count": len(scope.documents),
        },
        "eligibility_recipe_hash": parent_eligibility_recipe_hash(),
        "provider_calls": 0,
        "canonical_writes": 0,
    }


async def _scope_receipt(args: argparse.Namespace) -> dict[str, Any]:
    client, db = await _database()
    try:
        scope = await _load_scope(
            db,
            corpus_name=args.corpus_name,
            expected_parent_count=args.expected_parent_count,
            expected_child_count=args.expected_child_count,
        )
        receipt = _receipt_base("scope", scope, corpus_name=args.corpus_name)
        receipt["disclosed_noncanonical_stores"] = {
            COMPILATION_COLLECTION: await _collection_disclosure(db, scope.corpus_id)
        }
        receipt["writes"] = 0
        return receipt
    finally:
        client.close()


async def _ledger_census(args: argparse.Namespace) -> dict[str, Any]:
    client, db = await _database()
    try:
        scope = await _load_scope(
            db,
            corpus_name=args.corpus_name,
            expected_parent_count=args.expected_parent_count,
            expected_child_count=args.expected_child_count,
        )
        job_rows = (
            await db[HISTORICAL_JOB_COLLECTION]
            .find(
                {"corpus_id": scope.corpus_id},
                {"_id": 0, "parent_id": 1, "status": 1},
            )
            .to_list(length=None)
        )
        status_counts = Counter(str(row.get("status") or "") for row in job_rows)
        expected_status_counts = {
            "cancelled_checkpoint_failed": args.expected_cancelled_count,
            "dead_letter": args.expected_dead_letter_count,
            "succeeded": args.expected_accepted_count,
            "superseded": args.expected_superseded_count,
        }
        if dict(sorted(status_counts.items())) != expected_status_counts:
            raise MaterializationError(
                "historical semantic-digest job status ledger drifted"
            )

        accepted_rows = [row for row in job_rows if row.get("status") == "succeeded"]
        dead_letter_rows = [
            row for row in job_rows if row.get("status") == "dead_letter"
        ]
        purchased_rows = [*accepted_rows, *dead_letter_rows]
        purchased_parent_ids = [
            str(row.get("parent_id") or "") for row in purchased_rows
        ]
        if any(not parent_id for parent_id in purchased_parent_ids) or len(
            purchased_parent_ids
        ) != len(set(purchased_parent_ids)):
            raise MaterializationError(
                "historical purchased parent identities are missing or duplicated"
            )
        eligible_parent_ids = {
            str(parent.get("parent_id") or "") for parent in scope.parents
        }
        accepted_parent_ids = {str(row.get("parent_id") or "") for row in accepted_rows}
        dead_letter_parent_ids = {
            str(row.get("parent_id") or "") for row in dead_letter_rows
        }
        accepted_eligible = accepted_parent_ids & eligible_parent_ids
        dead_letter_eligible = dead_letter_parent_ids & eligible_parent_ids
        if len(accepted_eligible) != args.expected_accepted_eligible_count:
            raise MaterializationError("historical accepted/eligible overlap drifted")
        if len(dead_letter_eligible) != args.expected_dead_letter_eligible_count:
            raise MaterializationError("historical DLQ/eligible overlap drifted")

        purchased_terminal_eligible = accepted_eligible | dead_letter_eligible
        fresh_before_b4 = len(eligible_parent_ids - purchased_terminal_eligible)
        if fresh_before_b4 < args.b4_count:
            raise MaterializationError("fresh atomic pool cannot supply B4")
        receipt = _receipt_base("ledger-census", scope, corpus_name=args.corpus_name)
        receipt.update(
            {
                "historical_job_rows_by_status": dict(sorted(status_counts.items())),
                "historical_purchases": {
                    "accepted_total": len(accepted_parent_ids),
                    "accepted_eligible": len(accepted_eligible),
                    "accepted_outside_current_eligibility": len(
                        accepted_parent_ids - eligible_parent_ids
                    ),
                    "dead_letter_total": len(dead_letter_parent_ids),
                    "dead_letter_eligible": len(dead_letter_eligible),
                    "dead_letter_outside_current_eligibility": len(
                        dead_letter_parent_ids - eligible_parent_ids
                    ),
                    "accepted_artifacts_remain_valid": True,
                },
                "fresh_selection_accounting": {
                    "eligible_parent_count": len(eligible_parent_ids),
                    "purchased_terminal_eligible_count": len(
                        purchased_terminal_eligible
                    ),
                    "fresh_atomic_pool_before_b4": fresh_before_b4,
                    "b4_count": args.b4_count,
                    "fresh_phase2_pool_if_b4_claims_all": (
                        fresh_before_b4 - args.b4_count
                    ),
                },
                "disclosed_noncanonical_stores": {
                    COMPILATION_COLLECTION: await _collection_disclosure(
                        db, scope.corpus_id
                    )
                },
                "writes": 0,
            }
        )
        return receipt
    finally:
        client.close()


async def _export(args: argparse.Namespace) -> dict[str, Any]:
    output_path = _require_tmp_path(Path(args.output))
    client, db = await _database()
    try:
        scope = await _load_scope(
            db,
            corpus_name=args.corpus_name,
            expected_parent_count=args.expected_parent_count,
            expected_child_count=args.expected_child_count,
        )
        import spacy

        if str(spacy.__version__) != SPACY_LIBRARY_VERSION:
            raise MaterializationError("export spaCy library version is not pinned")
        nlp = spacy.load(SPACY_MODEL)
        if str(nlp.meta.get("version") or "") != SPACY_MODEL_VERSION:
            raise MaterializationError("export spaCy model version is not pinned")

        temp_path = output_path.with_suffix(output_path.suffix + ".partial")
        claim_count = 0
        typed_count = 0
        untyped_count = 0
        link_count = 0
        evidence_count = 0
        compiler_hashes: set[str] = set()
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            for index, child_id in enumerate(scope.child_ids, 1):
                child = scope.children[child_id]
                document = scope.documents[str(child.get("doc_id") or "")]
                candidate = compile_child_candidate(
                    corpus_id=scope.corpus_id,
                    document=document,
                    child=child,
                    nlp=nlp,
                    spacy_library_version=str(spacy.__version__),
                )
                handle.write(
                    canonical_json_v1(candidate.model_dump(mode="python")) + "\n"
                )
                claim_count += len(candidate.compilation.claims)
                typed_count += sum(
                    item.typing_status == "typed"
                    for item in candidate.compilation.claims
                )
                untyped_count += sum(
                    item.typing_status == "untyped"
                    for item in candidate.compilation.claims
                )
                link_count += len(candidate.compilation.links)
                evidence_count += len(candidate.evidence_refs)
                compiler_hashes.add(candidate.compiler_recipe_hash)
                if index % 250 == 0:
                    print(f"progress_children={index}", flush=True)
        if len(compiler_hashes) != 1:
            raise MaterializationError("compiler recipe hash drifted during export")
        temp_path.replace(output_path)
        receipt = _receipt_base("export", scope, corpus_name=args.corpus_name)
        receipt.update(
            {
                "runtime": {
                    "spacy_library_version": str(spacy.__version__),
                    "spacy_model": SPACY_MODEL,
                    "spacy_model_version": str(nlp.meta.get("version") or ""),
                    "parser_version": PARSER_VERSION,
                },
                "export": {
                    "row_count": len(scope.child_ids),
                    "claim_count": claim_count,
                    "typed_claim_count": typed_count,
                    "untyped_claim_count": untyped_count,
                    "claim_link_count": link_count,
                    "evidence_sentence_count": evidence_count,
                    "compiler_recipe_hash": next(iter(compiler_hashes)),
                    "file_sha256": _file_sha256(output_path),
                    "file_bytes": output_path.stat().st_size,
                    "raw_output_location": "/tmp only; not committed",
                },
                "writes": 0,
                "disclosed_noncanonical_stores": {
                    COMPILATION_COLLECTION: await _collection_disclosure(
                        db, scope.corpus_id
                    )
                },
            }
        )
        return receipt
    finally:
        client.close()


def _candidate_lines(
    path: Path,
) -> Iterable[tuple[int, CompiledChildCandidateExportV1]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield line_number, CompiledChildCandidateExportV1.model_validate_json(
                    line
                )
            except Exception as exc:
                raise MaterializationError(
                    f"candidate JSONL line {line_number} is invalid"
                ) from exc


async def _import(args: argparse.Namespace) -> dict[str, Any]:
    input_path = _require_tmp_path(Path(args.input))
    actual_sha = _file_sha256(input_path)
    if actual_sha != args.expected_file_sha256:
        raise MaterializationError("candidate export SHA-256 does not match expected")
    client, db = await _database()
    try:
        scope = await _load_scope(
            db,
            corpus_name=args.corpus_name,
            expected_parent_count=args.expected_parent_count,
            expected_child_count=args.expected_child_count,
        )
        materialization_time = datetime.now(timezone.utc)
        seen: set[str] = set()
        validation_rows = 0
        for _line_number, candidate in _candidate_lines(input_path):
            if candidate.child_id in seen:
                raise MaterializationError(
                    "candidate export contains duplicate child IDs"
                )
            seen.add(candidate.child_id)
            child = scope.children.get(candidate.child_id)
            if child is None:
                raise MaterializationError("candidate export contains an unknown child")
            document = scope.documents[str(child.get("doc_id") or "")]
            validated_row = materialize_candidate_row(
                candidate,
                corpus_id=scope.corpus_id,
                document=document,
                child=child,
                run_id=args.run_id,
                now=materialization_time,
            )
            parse_materialized_row_document(
                validated_row.model_dump(mode="python", by_alias=True)
            )
            validation_rows += 1
        if seen != set(scope.child_ids) or validation_rows != len(scope.child_ids):
            raise MaterializationError("candidate export child set does not close")

        settings = get_settings()
        canonical_before = await _canonical_store_census(db=db, settings=settings)
        collection_before = await _collection_disclosure(db, scope.corpus_id)
        before_census_file = _persist_before_census(
            Path(args.before_census_output),
            {
                "schema_version": SCHEMA_VERSION,
                "command": "import-before-census",
                "captured_at_utc": materialization_time.isoformat(),
                "corpus": {
                    "name": args.corpus_name,
                    "corpus_id": scope.corpus_id,
                    "eligible_parent_count": len(scope.parents),
                    "unique_child_count": len(scope.child_ids),
                    "document_count": len(scope.documents),
                },
                "input_file_sha256": actual_sha,
                "disclosed_noncanonical_stores": {
                    COMPILATION_COLLECTION: collection_before
                },
                "protected_canonical_store_census": canonical_before,
                "provider_calls": 0,
                "canonical_writes": 0,
                "writes_before_receipt_persisted": 0,
            },
        )
        inserted = 0
        reused = 0
        batch: list[UpdateOne] = []
        rows_for_readback: list[str] = []
        for _line_number, candidate in _candidate_lines(input_path):
            child = scope.children[candidate.child_id]
            document = scope.documents[str(child.get("doc_id") or "")]
            row = materialize_candidate_row(
                candidate,
                corpus_id=scope.corpus_id,
                document=document,
                child=child,
                run_id=args.run_id,
                now=materialization_time,
            )
            rows_for_readback.append(row.row_id)
            batch.append(
                UpdateOne(
                    {"_id": row.row_id},
                    {"$setOnInsert": row.model_dump(mode="python", by_alias=True)},
                    upsert=True,
                )
            )
            if len(batch) == 100:
                result = await db[COMPILATION_COLLECTION].bulk_write(
                    batch, ordered=True
                )
                inserted += int(getattr(result, "upserted_count", 0) or 0)
                reused += len(batch) - int(getattr(result, "upserted_count", 0) or 0)
                batch = []
        if batch:
            result = await db[COMPILATION_COLLECTION].bulk_write(batch, ordered=True)
            inserted += int(getattr(result, "upserted_count", 0) or 0)
            reused += len(batch) - int(getattr(result, "upserted_count", 0) or 0)

        readback_children: set[str] = set()
        readback_cursor = (
            db[COMPILATION_COLLECTION]
            .find({"_id": {"$in": rows_for_readback}})
            .sort("child_id", 1)
        )
        async for raw in readback_cursor:
            row = parse_materialized_row_document(raw)
            child = scope.children.get(row.child_id)
            if child is None or row.child_id in readback_children:
                raise MaterializationError("post-insert child identity is invalid")
            document = scope.documents[str(child.get("doc_id") or "")]
            validate_materialized_row_against_source(
                row,
                corpus_id=scope.corpus_id,
                document=document,
                child=child,
            )
            readback_children.add(row.child_id)
        if readback_children != set(scope.child_ids):
            raise MaterializationError("post-insert child set does not close")
        collection_after = await _collection_disclosure(db, scope.corpus_id)
        if collection_after["canonical_or_missing_flag_count"] != 0:
            raise MaterializationError("noncanonical collection contains unsafe flags")
        canonical_after = await _canonical_store_census(db=db, settings=settings)
        canonical_census = _canonical_store_census_receipt(
            canonical_before,
            canonical_after,
        )
        if not canonical_census["protected_exactly_unchanged"]:
            raise MaterializationError("protected canonical-store census drifted")

        receipt = _receipt_base("import", scope, corpus_name=args.corpus_name)
        receipt.update(
            {
                "input": {
                    "file_sha256": actual_sha,
                    "validated_row_count_before_write": validation_rows,
                    "materialization_time_utc": materialization_time.isoformat(),
                },
                "materialization": {
                    "inserted_count": inserted,
                    "reused_count": reused,
                    "readback_valid_count": len(readback_children),
                    "set_on_insert_only": True,
                },
                "disclosed_noncanonical_stores": {
                    COMPILATION_COLLECTION: {
                        "before": collection_before,
                        "after": collection_after,
                    }
                },
                "protected_canonical_store_census": canonical_census,
                "persisted_before_census_receipt": before_census_file,
                "writes": inserted,
            }
        )
        return receipt
    finally:
        client.close()


def _quantiles(values: list[int]) -> dict[str, int]:
    ordered = sorted(values)
    if not ordered:
        return {}
    return {
        str(percentile): ordered[round((len(ordered) - 1) * percentile / 100)]
        for percentile in (0, 25, 50, 75, 90, 95, 99, 100)
    }


def _packet_exclusion_ledger_entry(
    *,
    parent: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    rows_by_child: Mapping[str, ClaimCompilationMaterializationRowV1],
    reason: str,
) -> dict[str, Any]:
    parent_id = str(parent.get("parent_id") or "").strip()
    document_id = str(parent.get("doc_id") or "").strip()
    if not parent_id or document_id not in documents:
        raise MaterializationError("packet exclusion identity does not close")
    source_child_ids = sorted(
        {str(value) for value in parent.get("child_ids") or [] if value}
    )
    missing_claim_child_ids = [
        child_id
        for child_id in source_child_ids
        if child_id not in rows_by_child
        or not rows_by_child[child_id].envelope.body.claims
    ]
    return {
        "reason": reason,
        "parent_id": parent_id,
        "document_id": document_id,
        "document_source_version_id": document_source_version_id(
            documents[document_id]
        ),
        "source_child_ids": source_child_ids,
        "source_child_without_atomic_claim_ids": missing_claim_child_ids,
    }


def _route_prices() -> dict[str, Any]:
    prices = json.loads(PRICE_CARD_PATH.read_text(encoding="utf-8"))
    parameters = json.loads(ROUTE_CARD_PATH.read_text(encoding="utf-8"))
    price_rows = [row for row in prices["routes"] if row["route_id"] == ROUTE_ID]
    parameter_rows = [
        row for row in parameters["routes"] if row["route_id"] == ROUTE_ID
    ]
    if len(price_rows) != 1 or len(parameter_rows) != 1:
        raise MaterializationError("LongCat route cards did not resolve exactly once")
    return {"price": price_rows[0], "parameters": parameter_rows[0]}


async def _packet_census(args: argparse.Namespace) -> dict[str, Any]:
    client, db = await _database()
    try:
        scope = await _load_scope(
            db,
            corpus_name=args.corpus_name,
            expected_parent_count=args.expected_parent_count,
            expected_child_count=args.expected_child_count,
        )
        rows_by_child: dict[str, ClaimCompilationMaterializationRowV1] = {}
        row_cursor = (
            db[COMPILATION_COLLECTION]
            .find(
                {
                    "corpus_id": scope.corpus_id,
                    "child_id": {"$in": scope.child_ids},
                    "canonical_write": False,
                    "status": "candidate",
                    "spacy_library_version": SPACY_LIBRARY_VERSION,
                    "spacy_model": SPACY_MODEL,
                    "spacy_model_version": SPACY_MODEL_VERSION,
                    "parser_version": PARSER_VERSION,
                }
            )
            .sort("child_id", 1)
        )
        async for raw in row_cursor:
            row = parse_materialized_row_document(raw)
            if row.child_id in rows_by_child:
                raise MaterializationError(
                    "multiple current rows resolved for one child"
                )
            child = scope.children.get(row.child_id)
            if child is None:
                continue
            document = scope.documents[str(child.get("doc_id") or "")]
            validate_materialized_row_against_source(
                row,
                corpus_id=scope.corpus_id,
                document=document,
                child=child,
            )
            rows_by_child[row.child_id] = row
        if set(rows_by_child) != set(scope.child_ids):
            raise MaterializationError("current materialized child set does not close")

        extraction_rows = (
            await db["ghost_b_extractions"]
            .find(
                {
                    "corpus_id": scope.corpus_id,
                    "chunk_id": {"$in": scope.child_ids},
                    "status": "ok",
                    "schema_version": "polymath.extract.v1",
                },
                {
                    "_id": 0,
                    "chunk_id": 1,
                    "status": 1,
                    "schema_version": 1,
                    "entities": 1,
                },
            )
            .sort("chunk_id", 1)
            .to_list(length=None)
        )
        extraction_by_child = {
            str(row.get("chunk_id") or ""): row for row in extraction_rows
        }
        reasons: Counter[str] = Counter()
        packet_bytes: list[int] = []
        claim_counts: list[int] = []
        evidence_id_counts: list[int] = []
        link_counts: list[int] = []
        source_claim_counts: list[int] = []
        excluded_claim_counts: list[int] = []
        packet_hashes: set[str] = set()
        packet_schema_hashes: set[str] = set()
        validator_scope_parity_count = 0
        selection_recipe_hashes: set[str] = set()
        exclusion_ledger: list[dict[str, Any]] = []
        priority_retention_exception_ledger: list[dict[str, Any]] = []
        retention = Counter()
        for parent in scope.parents:
            child_ids = sorted(
                {str(value) for value in parent.get("child_ids") or [] if value}
            )
            try:
                built = build_bounded_atomic_parent_packet(
                    corpus_id=scope.corpus_id,
                    corpus_name=args.corpus_name,
                    parent=parent,
                    compilation_rows={
                        child_id: rows_by_child[child_id] for child_id in child_ids
                    },
                    extraction_rows=[
                        extraction_by_child[child_id]
                        for child_id in child_ids
                        if child_id in extraction_by_child
                    ],
                    max_entities=args.max_entities,
                )
            except PacketNotReadyError as exc:
                reasons[exc.reason] += 1
                exclusion_ledger.append(
                    _packet_exclusion_ledger_entry(
                        parent=parent,
                        documents=scope.documents,
                        rows_by_child=rows_by_child,
                        reason=exc.reason,
                    )
                )
                continue
            serialized = canonical_json_v1(built.packet.model_dump(mode="python"))
            packet_claim_ids = {item.claim_id for item in built.packet.claims}
            validator_claim_ids = {item.claim_id for item in built.context.claims}
            if packet_claim_ids != validator_claim_ids:
                raise MaterializationError(
                    "packet claim IDs do not equal semantic-validator scope"
                )
            validator_scope_parity_count += 1
            size = len(serialized.encode("utf-8"))
            packet_bytes.append(size)
            claim_counts.append(built.emitted_claim_count)
            source_claim_counts.append(built.source_claim_count)
            excluded_claim_counts.append(built.excluded_claim_count)
            evidence_id_counts.append(
                len({item.evidence_sentence_id for item in built.packet.claims})
            )
            link_counts.append(built.emitted_link_count)
            packet_hashes.add(hashlib.sha256(serialized.encode("utf-8")).hexdigest())
            packet_schema_hashes.add(
                built.packet.evidence_contract.claim_record_schema_hash
                + ":"
                + built.packet.evidence_contract.claim_compilation_schema_hash
            )
            manifest = built.packet.selection_manifest
            selection_recipe_hashes.add(manifest.recipe_hash)
            excluded_by_id = {
                item.claim_id: item for item in built.excluded_claim_records
            }
            for decision in built.excluded_claim_byte_decisions:
                claim = excluded_by_id[decision.claim_id]
                memberships = []
                if claim.typing_status == "typed":
                    memberships.append("typed")
                if claim.polarity == "negative":
                    memberships.append("negative")
                if not memberships:
                    continue
                priority_retention_exception_ledger.append(
                    {
                        "parent_id": built.parent_id,
                        "document_id": built.doc_id,
                        "document_source_version_id": document_source_version_id(
                            scope.documents[built.doc_id]
                        ),
                        "child_id": claim.child_id,
                        "claim_id": claim.claim_id,
                        "priority_memberships": memberships,
                        "first_attempted_packet_utf8_bytes": (
                            decision.first_attempted_packet_utf8_bytes
                        ),
                        "last_attempted_packet_utf8_bytes": (
                            decision.last_attempted_packet_utf8_bytes
                        ),
                        "rejection_attempt_count": decision.rejection_attempt_count,
                        "max_packet_utf8_bytes": decision.max_packet_utf8_bytes,
                        "exclusion_reason": "candidate_exceeded_packet_utf8_bound",
                    }
                )
            retention.update(
                {
                    "source_claims": manifest.source_claim_count,
                    "emitted_claims": manifest.emitted_claim_count,
                    "excluded_claims": manifest.excluded_claim_count,
                    "source_typed": manifest.typed.source_count,
                    "emitted_typed": manifest.typed.emitted_count,
                    "source_negative": manifest.negative.source_count,
                    "emitted_negative": manifest.negative.emitted_count,
                    "source_nuanced": manifest.nuanced.source_count,
                    "emitted_nuanced": manifest.nuanced.emitted_count,
                    "source_ordinary": manifest.ordinary.source_count,
                    "emitted_ordinary": manifest.ordinary.emitted_count,
                    "cap_applied_parents": int(manifest.cap_applied),
                }
            )
            reasons["packet_ready"] += 1
        if sum(reasons.values()) != len(scope.parents):
            raise MaterializationError(
                "parent packet/exclusion accounting does not close"
            )
        if len(packet_hashes) != reasons["packet_ready"]:
            raise MaterializationError("packet hashes are not unique per parent")
        if len(packet_schema_hashes) != 1:
            raise MaterializationError("packet claim schema identity drifted")
        if len(selection_recipe_hashes) != 1:
            raise MaterializationError("bounded selection recipe identity drifted")

        route = _route_prices()
        price = route["price"]
        parameters = route["parameters"]
        unit = int(price["price_unit_tokens"])
        input_rate = float(price["uncached_input_usd"])
        output_rate = float(price["output_usd"])
        output_cap = int(parameters["max_tokens"])
        largest_ten_packet_bounds = sorted(packet_bytes, reverse=True)[:10]
        b4_authority_ceiling = worst_case_authority_usd(
            packet_input_token_upper_bounds=largest_ten_packet_bounds,
            max_output_tokens=output_cap,
            uncached_input_usd=input_rate,
            output_usd=output_rate,
            price_unit_tokens=unit,
        )
        all_ready_ceiling = worst_case_authority_usd(
            packet_input_token_upper_bounds=packet_bytes,
            max_output_tokens=output_cap,
            uncached_input_usd=input_rate,
            output_usd=output_rate,
            price_unit_tokens=unit,
        )

        receipt = _receipt_base("packet-census", scope, corpus_name=args.corpus_name)
        receipt.update(
            {
                "packet_contract": {
                    "packet_schema_version": (
                        "semantic_parent_packet.atomic_claims.v2"
                    ),
                    "prompt_version": "parent-digest.v6",
                    "prompt_changed": False,
                    "claims_interim": False,
                    "parent_text_in_provider_packet": False,
                    "evidence_quote_bodies_in_provider_packet": False,
                    "citation_authority": (
                        "python_local_materialized_claim_and_exact_quote"
                    ),
                    "selection_recipe_version": ("atomic_claim_packet_selection.v2"),
                    "selection_recipe_hash": next(iter(selection_recipe_hashes)),
                    "max_packet_utf8_bytes": 20_000,
                    "proposal_space_disposition": (
                        "bounded_to_emitted_claims_excluded_claims_remain_local"
                    ),
                },
                "parent_accounting": dict(sorted(reasons.items())),
                "non_packet_ready_exclusion_ledger": exclusion_ledger,
                "priority_retention_exception_ledger": (
                    priority_retention_exception_ledger
                ),
                "packet_metrics": {
                    "packet_byte_quantiles": _quantiles(packet_bytes),
                    "emitted_claim_count_quantiles": _quantiles(claim_counts),
                    "source_claim_count_quantiles": _quantiles(source_claim_counts),
                    "excluded_claim_count_quantiles": _quantiles(excluded_claim_counts),
                    "emitted_evidence_id_count_quantiles": _quantiles(
                        evidence_id_counts
                    ),
                    "emitted_claim_link_count_quantiles": _quantiles(link_counts),
                    "total_packet_bytes": sum(packet_bytes),
                    "unique_packet_hash_count": len(packet_hashes),
                    "packet_set_hash": namespace_hash(
                        "input-set", frozenset(packet_hashes)
                    ),
                    "validator_claim_scope_parity_count": (
                        validator_scope_parity_count
                    ),
                },
                "bounded_proposal_space": dict(sorted(retention.items())),
                "conservative_cost_authority": {
                    "basis": (
                        "one_input_token_per_utf8_byte_plus_route_max_output_tokens; "
                        "published_uncached_input_and_output_rates; 10_percent_margin"
                    ),
                    "route_id": ROUTE_ID,
                    "input_token_upper_bound": "packet_utf8_bytes",
                    "output_token_upper_bound_per_packet": output_cap,
                    "uncached_input_usd_per_million": input_rate,
                    "output_usd_per_million": output_rate,
                    "max_any_10_packet_cost_before_margin_usd": round(max_any_ten, 8),
                    "b4_10_packet_authority_ceiling_usd": round(
                        float(b4_authority_ceiling), 8
                    ),
                    "all_packet_ready_authority_ceiling_usd": round(
                        float(all_ready_ceiling), 8
                    ),
                    "old_fixed_0_04_assumption_used": False,
                },
                "disclosed_noncanonical_stores": {
                    COMPILATION_COLLECTION: await _collection_disclosure(
                        db, scope.corpus_id
                    )
                },
                "writes": 0,
            }
        )
        return receipt
    finally:
        client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("scope", "ledger-census", "export", "import", "packet-census"),
    )
    parser.add_argument("--corpus-name", default=DEFAULT_CORPUS_NAME)
    parser.add_argument("--expected-parent-count", type=int, default=795)
    parser.add_argument("--expected-child-count", type=int)
    parser.add_argument("--output")
    parser.add_argument("--input")
    parser.add_argument("--expected-file-sha256")
    parser.add_argument("--run-id")
    parser.add_argument("--before-census-output")
    parser.add_argument("--max-entities", type=int, default=40)
    parser.add_argument("--expected-accepted-count", type=int, default=66)
    parser.add_argument("--expected-dead-letter-count", type=int, default=6)
    parser.add_argument("--expected-superseded-count", type=int, default=939)
    parser.add_argument("--expected-cancelled-count", type=int, default=38)
    parser.add_argument("--expected-accepted-eligible-count", type=int, default=52)
    parser.add_argument("--expected-dead-letter-eligible-count", type=int, default=4)
    parser.add_argument("--b4-count", type=int, default=10)
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "scope":
        return await _scope_receipt(args)
    if args.command == "ledger-census":
        if args.expected_child_count is None:
            raise MaterializationError("ledger census requires expected child count")
        return await _ledger_census(args)
    if args.command == "export":
        if not args.output or args.expected_child_count is None:
            raise MaterializationError(
                "export requires output and expected child count"
            )
        return await _export(args)
    if args.command == "import":
        if not all(
            (
                args.input,
                args.expected_file_sha256,
                args.run_id,
                args.before_census_output,
                args.expected_child_count is not None,
            )
        ):
            raise MaterializationError(
                "import requires input, expected SHA, run ID, BEFORE-census output, "
                "and expected child count"
            )
        return await _import(args)
    if args.command == "packet-census":
        if args.expected_child_count is None:
            raise MaterializationError("packet census requires expected child count")
        return await _packet_census(args)
    raise MaterializationError("unknown command")


def main() -> int:
    receipt = asyncio.run(_run(_parser().parse_args()))
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
