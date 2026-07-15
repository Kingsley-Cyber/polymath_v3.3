#!/usr/bin/env python3
"""Build the zero-provider, read-only T9.3 B4 atomic-packet preflight."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from config import get_settings
from models.hash_taxonomy import canonical_json_v1, namespace_hash
from models.semantic_digest_atomic_selection import (
    AtomicB4SelectionRecipeV1,
    AtomicB4SizeBandV1,
    load_atomic_b4_selection_recipe,
)
from models.semantic_digest_claim_input import (
    COMPILATION_COLLECTION,
    ClaimCompilationMaterializationRowV1,
    parse_materialized_row_document,
)
from scripts.materialize_semantic_digest_claim_inputs import (
    HISTORICAL_JOB_COLLECTION,
    MaterializationError,
    _collection_disclosure,
    _database,
    _load_scope,
    _packet_exclusion_ledger_entry,
    _quantiles,
    _route_prices,
)
from scripts.semantic_gateway_mark_paid_pass import (
    JOB_COLLECTION,
    PURCHASED_TERMINAL_STATUSES,
    CanaryPacket,
    PlannedPacket,
    _build_config,
    _plan_packets,
)
from scripts.semantic_gateway_ugo_canary import (
    _canonical_store_census,
    _canonical_store_census_receipt,
)
from services.ingestion.semantic_digest_claim_inputs import (
    PARSER_VERSION,
    SPACY_LIBRARY_VERSION,
    SPACY_MODEL,
    SPACY_MODEL_VERSION,
    PacketNotReadyError,
    build_bounded_atomic_parent_packet,
    document_source_version_id,
    validate_materialized_row_against_source,
)
from services.semantic_gateway import (
    SemanticGatewayConfig,
    semantic_digest_prompt_hash,
    semantic_digest_repair_prompt_hash,
    semantic_digest_schema_hash,
)

DEFAULT_CORPUS_NAME = "markbuildsbrands_transcripts"
RECIPE_PATH = (
    Path(__file__).resolve().parents[1]
    / "registries"
    / "semantic_digest_atomic_b4_selection.v1.json"
)
EXPECTED_HISTORICAL_STATUS_COUNTS = {
    "cancelled_checkpoint_failed": 38,
    "dead_letter": 6,
    "succeeded": 66,
    "superseded": 939,
}
COST_MARGIN = 1.10


@dataclass(frozen=True)
class AtomicPopulationRow:
    planned: PlannedPacket
    packet_hash: str

    @property
    def parent_id(self) -> str:
        return self.planned.item.parent_id

    @property
    def doc_id(self) -> str:
        return self.planned.item.doc_id

    @property
    def packet_bytes(self) -> int:
        return self.planned.packet_bytes


@dataclass(frozen=True)
class AtomicSelectedRow:
    row: AtomicPopulationRow
    band_id: str
    selection_order_hash: str


@dataclass(frozen=True)
class AtomicB4Prepared:
    """Internal immutable handoff from read-only preflight to the B4 runner."""

    receipt: dict[str, Any]
    selected: tuple[AtomicSelectedRow, ...]
    config: SemanticGatewayConfig


def _band_for_basis_points(
    recipe: AtomicB4SelectionRecipeV1,
    basis_points: int,
) -> AtomicB4SizeBandV1:
    matches = [
        band
        for band in recipe.bands
        if band.lower_basis_points_inclusive
        <= basis_points
        < band.upper_basis_points_exclusive
    ]
    if len(matches) != 1:
        raise MaterializationError("B4 percentile band assignment did not close")
    return matches[0]


def _stratify_and_select(
    population: Sequence[AtomicPopulationRow],
    *,
    recipe: AtomicB4SelectionRecipeV1,
    recipe_hash: str,
) -> tuple[list[AtomicSelectedRow], list[dict[str, Any]]]:
    if len(population) < recipe.target_count:
        raise MaterializationError("B4 fresh population is too small")
    ordered = sorted(
        population,
        key=lambda row: (row.packet_bytes, row.parent_id),
    )
    by_band: dict[str, list[tuple[int, int, AtomicPopulationRow]]] = {
        band.band_id: [] for band in recipe.bands
    }
    for rank, row in enumerate(ordered):
        basis_points = rank * 10_000 // len(ordered)
        band = _band_for_basis_points(recipe, basis_points)
        by_band[band.band_id].append((rank, basis_points, row))

    selected: list[AtomicSelectedRow] = []
    used_documents: set[str] = set()
    band_receipts: list[dict[str, Any]] = []
    for band in recipe.bands:
        members = by_band[band.band_id]
        ranked = sorted(
            members,
            key=lambda item: (
                namespace_hash(
                    "work",
                    {
                        "work_kind": "atomic_b4_within_band_order",
                        "recipe_hash": recipe_hash,
                        "parent_id": item[2].parent_id,
                        "packet_hash": item[2].packet_hash,
                    },
                ),
                item[2].parent_id,
            ),
        )
        chosen: list[AtomicSelectedRow] = []
        for _, _, row in ranked:
            if row.doc_id in used_documents:
                continue
            order_hash = namespace_hash(
                "work",
                {
                    "work_kind": "atomic_b4_within_band_order",
                    "recipe_hash": recipe_hash,
                    "parent_id": row.parent_id,
                    "packet_hash": row.packet_hash,
                },
            )
            chosen.append(
                AtomicSelectedRow(
                    row=row,
                    band_id=band.band_id,
                    selection_order_hash=order_hash,
                )
            )
            used_documents.add(row.doc_id)
            if len(chosen) == band.selection_count:
                break
        if len(chosen) != band.selection_count:
            raise MaterializationError(
                f"B4 band {band.band_id} cannot satisfy unique-document selection"
            )
        selected.extend(chosen)
        band_receipts.append(
            {
                "band_id": band.band_id,
                "lower_basis_points_inclusive": (band.lower_basis_points_inclusive),
                "upper_basis_points_exclusive": (band.upper_basis_points_exclusive),
                "population_count": len(members),
                "rank_min": min(item[0] for item in members),
                "rank_max": max(item[0] for item in members),
                "packet_bytes_min": min(item[2].packet_bytes for item in members),
                "packet_bytes_max": max(item[2].packet_bytes for item in members),
                "selection_count": len(chosen),
            }
        )
    if len(selected) != recipe.target_count:
        raise MaterializationError("B4 stratified selection count drifted")
    if len({item.row.parent_id for item in selected}) != recipe.target_count:
        raise MaterializationError("B4 selection contains duplicate parents")
    if len({item.row.doc_id for item in selected}) != recipe.target_count:
        raise MaterializationError("B4 selection contains duplicate documents")
    return selected, band_receipts


def _cost_upper_bound(
    rows: Sequence[AtomicPopulationRow],
    *,
    uncached_input_rate: float,
    output_rate: float,
    price_unit: int,
    max_output_tokens: int,
) -> float:
    before_margin = sum(
        (row.packet_bytes * uncached_input_rate + max_output_tokens * output_rate)
        / price_unit
        for row in rows
    )
    return round(before_margin * COST_MARGIN, 8)


def _packet_population_set_hash(
    population: Sequence[AtomicPopulationRow],
) -> str:
    raw_hashes = []
    for row in population:
        prefix, separator, raw_hash = row.packet_hash.partition(":")
        if prefix != "sha256" or separator != ":" or len(raw_hash) != 64:
            raise MaterializationError("B4 packet hash is not canonical SHA-256")
        try:
            int(raw_hash, 16)
        except ValueError as exc:
            raise MaterializationError(
                "B4 packet hash is not canonical SHA-256"
            ) from exc
        raw_hashes.append(raw_hash)
    return namespace_hash("input-set", frozenset(raw_hashes))


async def _prepare(args: argparse.Namespace) -> AtomicB4Prepared:
    recipe = load_atomic_b4_selection_recipe(RECIPE_PATH)
    recipe_hash = namespace_hash("registry", recipe.model_dump(mode="python"))
    client, db = await _database()
    try:
        active_ingests = await db["ingest_batches"].count_documents(
            {"status": {"$in": ["queued", "running"]}}
        )
        running_semantic_jobs = await db[JOB_COLLECTION].count_documents(
            {"status": "running"}
        )
        if active_ingests or running_semantic_jobs:
            raise MaterializationError(
                "B4 preflight requires zero active ingest batches and semantic jobs"
            )
        scope = await _load_scope(
            db,
            corpus_name=args.corpus_name,
            expected_parent_count=args.expected_parent_count,
            expected_child_count=args.expected_child_count,
        )
        rows_by_child: dict[str, ClaimCompilationMaterializationRowV1] = {}
        cursor = (
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
        async for raw in cursor:
            row = parse_materialized_row_document(raw)
            child = scope.children.get(row.child_id)
            if child is None or row.child_id in rows_by_child:
                raise MaterializationError("B4 compilation child closure drifted")
            validate_materialized_row_against_source(
                row,
                corpus_id=scope.corpus_id,
                document=scope.documents[str(child.get("doc_id") or "")],
                child=child,
            )
            rows_by_child[row.child_id] = row
        if set(rows_by_child) != set(scope.child_ids):
            raise MaterializationError("B4 compilation rows do not close")

        extraction_rows = await (
            db["ghost_b_extractions"]
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

        packet_items: list[CanaryPacket] = []
        packet_hash_by_parent: dict[str, str] = {}
        non_packet_ready_ledger: list[dict[str, Any]] = []
        priority_exception_ledger: list[dict[str, Any]] = []
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
                non_packet_ready_ledger.append(
                    _packet_exclusion_ledger_entry(
                        parent=parent,
                        documents=scope.documents,
                        rows_by_child=rows_by_child,
                        reason=exc.reason,
                    )
                )
                continue
            packet = built.packet.model_dump(mode="python")
            serialized = canonical_json_v1(packet)
            packet_hash_by_parent[built.parent_id] = (
                "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            )
            packet_items.append(
                CanaryPacket(
                    packet=packet,
                    context=built.context,
                    parent_id=built.parent_id,
                    doc_id=built.doc_id,
                    entity_count=built.entity_count,
                    source_child_count=built.source_child_count,
                )
            )
            excluded_by_id = {
                item.claim_id: item for item in built.excluded_claim_records
            }
            for decision in built.excluded_claim_byte_decisions:
                claim = excluded_by_id[decision.claim_id]
                if claim.typing_status != "typed" and claim.polarity != "negative":
                    continue
                priority_exception_ledger.append(
                    {
                        "parent_id": built.parent_id,
                        "document_id": built.doc_id,
                        "document_source_version_id": document_source_version_id(
                            scope.documents[built.doc_id]
                        ),
                        "child_id": claim.child_id,
                        "claim_id": claim.claim_id,
                        "typing_status": claim.typing_status,
                        "polarity": claim.polarity,
                        "first_attempted_packet_utf8_bytes": (
                            decision.first_attempted_packet_utf8_bytes
                        ),
                        "max_packet_utf8_bytes": decision.max_packet_utf8_bytes,
                        "locally_authoritative": True,
                    }
                )
        if len(packet_items) + len(non_packet_ready_ledger) != len(scope.parents):
            raise MaterializationError("B4 packet population accounting did not close")

        route = _route_prices()
        parameter_card = route["parameters"]
        price_card = route["price"]
        config = _build_config(
            type(
                "RouteCard",
                (),
                {
                    "runtime_version": parameter_card["runtime_version"],
                    "model_id": parameter_card["model_id"],
                    "api_base": parameter_card["api_base"],
                    "tokenizer_id": parameter_card["tokenizer_id"],
                    "max_tokens": parameter_card["max_tokens"],
                    "timeout_seconds": parameter_card["timeout_seconds"],
                },
            )()
        )
        planned = _plan_packets(
            corpus_id=scope.corpus_id,
            packets=packet_items,
            config=config,
        )
        population = [
            AtomicPopulationRow(
                planned=row,
                packet_hash=packet_hash_by_parent[row.item.parent_id],
            )
            for row in planned
        ]

        historical_rows = await (
            db[HISTORICAL_JOB_COLLECTION]
            .find(
                {"corpus_id": scope.corpus_id},
                {"_id": 0, "parent_id": 1, "status": 1},
            )
            .to_list(length=None)
        )
        status_counts = Counter(str(row.get("status") or "") for row in historical_rows)
        if dict(sorted(status_counts.items())) != EXPECTED_HISTORICAL_STATUS_COUNTS:
            raise MaterializationError("B4 historical job ledger drifted")
        purchased_rows = [
            row
            for row in historical_rows
            if row.get("status") in recipe.historical_purchase_statuses
        ]
        purchased_parent_ids = {
            str(row.get("parent_id") or "") for row in purchased_rows
        }
        if (
            len(purchased_parent_ids) != len(purchased_rows)
            or "" in purchased_parent_ids
        ):
            raise MaterializationError("B4 historical purchase identities drifted")

        eligible_ids = {str(row["parent_id"]) for row in scope.parents}
        ready_ids = {row.parent_id for row in population}
        nonready_ids = {str(row["parent_id"]) for row in non_packet_ready_ledger}
        fresh_population = [
            row for row in population if row.parent_id not in purchased_parent_ids
        ]
        selected, band_receipts = _stratify_and_select(
            fresh_population,
            recipe=recipe,
            recipe_hash=recipe_hash,
        )
        selected_rows = [item.row for item in selected]
        selected_ids = {item.parent_id for item in selected_rows}
        if selected_ids & purchased_parent_ids:
            raise MaterializationError("B4 selection contains a historical purchase")

        input_rate = float(price_card["uncached_input_usd"])
        output_rate = float(price_card["output_usd"])
        price_unit = int(price_card["price_unit_tokens"])
        max_output_tokens = int(parameter_card["max_tokens"])
        selected_ceiling = _cost_upper_bound(
            selected_rows,
            uncached_input_rate=input_rate,
            output_rate=output_rate,
            price_unit=price_unit,
            max_output_tokens=max_output_tokens,
        )
        remaining_after_b4 = [
            row for row in fresh_population if row.parent_id not in selected_ids
        ]
        all_ready_ceiling = _cost_upper_bound(
            population,
            uncached_input_rate=input_rate,
            output_rate=output_rate,
            price_unit=price_unit,
            max_output_tokens=max_output_tokens,
        )
        remaining_ceiling = _cost_upper_bound(
            remaining_after_b4,
            uncached_input_rate=input_rate,
            output_rate=output_rate,
            price_unit=price_unit,
            max_output_tokens=max_output_tokens,
        )
        max_any_ten_ceiling = _cost_upper_bound(
            sorted(population, key=lambda row: row.packet_bytes, reverse=True)[:10],
            uncached_input_rate=input_rate,
            output_rate=output_rate,
            price_unit=price_unit,
            max_output_tokens=max_output_tokens,
        )

        settings = get_settings()
        canonical_before = await _canonical_store_census(db=db, settings=settings)
        receipt = {
            "schema_version": "polymath.semantic_digest_atomic_b4_preflight.v1",
            "mode": "zero_provider_read_only_preflight",
            "corpus": {
                "name": args.corpus_name,
                "corpus_id": scope.corpus_id,
                "document_count": len(scope.documents),
                "eligible_parent_count": len(scope.parents),
                "packet_ready_count": len(population),
                "non_packet_ready_count": len(non_packet_ready_ledger),
            },
            "packet_contract": {
                "packet_schema_version": recipe.packet_schema_version,
                "packet_set_hash": _packet_population_set_hash(population),
                "packet_set_hash_recipe": (
                    "namespace_hash(input-set, raw_sha256_hex_digests)"
                ),
                "packet_byte_quantiles": _quantiles(
                    [row.packet_bytes for row in population]
                ),
                "prompt_version": config.prompt_version,
                "prompt_changed": False,
                "proposal_space_bounded_to_emitted_claims": True,
                "sparse_proposals_are_automatic_failure": False,
            },
            "selection_recipe": {
                **recipe.model_dump(mode="python"),
                "recipe_hash": recipe_hash,
                "bands_frozen_before_selection": True,
            },
            "historical_ledger": {
                "rows_by_status": dict(sorted(status_counts.items())),
                "accepted_total": status_counts["succeeded"],
                "dead_letter_total": status_counts["dead_letter"],
                "purchased_total": len(purchased_parent_ids),
                "purchased_eligible": len(purchased_parent_ids & eligible_ids),
                "purchased_packet_ready": len(purchased_parent_ids & ready_ids),
                "purchased_non_packet_ready": len(purchased_parent_ids & nonready_ids),
                "accepted_artifacts_remain_valid": True,
            },
            "fresh_population_accounting": {
                "packet_ready_count": len(population),
                "historically_purchased_packet_ready": len(
                    purchased_parent_ids & ready_ids
                ),
                "fresh_before_b4": len(fresh_population),
                "b4_count": len(selected),
                "fresh_after_b4_if_all_claimed": len(remaining_after_b4),
            },
            "non_packet_ready_exclusion_ledger": non_packet_ready_ledger,
            "priority_retention_exception_ledger": priority_exception_ledger,
            "size_strata": band_receipts,
            "selection": {
                "selection_name": recipe.selection_name,
                "selection_set_hash": namespace_hash(
                    "input-set",
                    frozenset(item.row.planned.job_id for item in selected),
                ),
                "unique_document_count": len({item.row.doc_id for item in selected}),
                "total_packet_bytes": sum(item.row.packet_bytes for item in selected),
                "rows": [
                    {
                        "band_id": item.band_id,
                        "selection_order_hash": item.selection_order_hash,
                        "ordinal": item.row.planned.ordinal,
                        "parent_id": item.row.parent_id,
                        "document_id": item.row.doc_id,
                        "packet_utf8_bytes": item.row.packet_bytes,
                        "packet_hash": item.row.packet_hash,
                        "input_hash": item.row.planned.input_hash,
                        "cache_key": item.row.planned.cache_key,
                        "job_id": item.row.planned.job_id,
                    }
                    for item in selected
                ],
            },
            "summary_faithfulness_review": recipe.summary_faithfulness_review.model_dump(
                mode="python"
            ),
            "provider_contract": {
                "route_id": price_card["route_id"],
                "model_id": parameter_card["model_id"],
                "capability_tier": parameter_card["capability_tier"],
                "parameter_version": parameter_card["parameter_version"],
                "runtime_version": parameter_card["runtime_version"],
                "prompt_hash": semantic_digest_prompt_hash(
                    config.prompt_version, config.repair_prompt_version
                ),
                "repair_prompt_version": config.repair_prompt_version,
                "repair_prompt_hash": semantic_digest_repair_prompt_hash(
                    config.repair_prompt_version
                ),
                "schema_hash": semantic_digest_schema_hash(),
                "max_tokens": max_output_tokens,
                "temperature": parameter_card["temperature"],
                "thinking": parameter_card["thinking"],
                "credential_plaintext_read": False,
            },
            "cost_authority": {
                "basis": "packet_utf8_bytes_as_input_token_upper_bound_plus_route_max_output_tokens_with_10_percent_margin",
                "uncached_input_usd_per_million": input_rate,
                "output_usd_per_million": output_rate,
                "selected_b4_ceiling_usd": selected_ceiling,
                "max_any_10_ceiling_usd": max_any_ten_ceiling,
                "fresh_after_b4_ceiling_usd": remaining_ceiling,
                "all_packet_ready_ceiling_usd": all_ready_ceiling,
                "old_fixed_0_04_assumption_used": False,
            },
            "operational_preflight": {
                "active_ingest_batches": active_ingests,
                "running_semantic_jobs": running_semantic_jobs,
                "canonical_before": _canonical_store_census_receipt(
                    canonical_before, canonical_before
                )["before"],
                "provider_calls": 0,
                "database_writes": 0,
                "canonical_writes": 0,
            },
            "disclosed_noncanonical_stores": {
                COMPILATION_COLLECTION: await _collection_disclosure(
                    db, scope.corpus_id
                )
            },
            "authorization": {
                "provider_execution_authorized": False,
                "required_next_step": "senior_restates_GO_arithmetic_before_any_call",
            },
        }
        return AtomicB4Prepared(
            receipt=receipt,
            selected=tuple(selected),
            config=config,
        )
    finally:
        client.close()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Preserve the credential-blind CLI contract while exposing preparation."""

    return (await _prepare(args)).receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-name", default=DEFAULT_CORPUS_NAME)
    parser.add_argument("--expected-parent-count", type=int, default=795)
    parser.add_argument("--expected-child-count", type=int, default=3493)
    parser.add_argument("--max-entities", type=int, default=40)
    return parser


def main() -> int:
    try:
        receipt = asyncio.run(_run(_parser().parse_args()))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": (
                        "polymath.semantic_digest_atomic_b4_preflight.failure.v1"
                    ),
                    "error_class": type(exc).__name__,
                    "provider_calls": 0,
                    "database_writes": 0,
                    "all_green": False,
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
