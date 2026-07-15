#!/usr/bin/env python3
"""Build the credential-blind, zero-provider sentence-hybrid v3 preflight."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from config import get_settings
from models.hash_taxonomy import canonical_json_v1, namespace_hash
from models.semantic_digest_claim_input import (
    COMPILATION_COLLECTION,
    ClaimCompilationMaterializationRowV1,
    parse_materialized_row_document,
)
from models.semantic_digest_sentence_selection import (
    SentenceHybridCanarySelectionRecipeV1,
    load_sentence_hybrid_canary_selection_recipe,
)
from models.semantic_parent_packet import (
    SENTENCE_HYBRID_PACKET_MAX_UTF8_BYTES,
    semantic_parent_packet_sentence_hybrid_schema_hash,
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
    CanaryPacket,
    PlannedPacket,
    _build_config,
    _cumulative_cost,
    _plan_packets,
)
from scripts.semantic_gateway_ugo_canary import (
    _canonical_store_census,
    _canonical_store_census_receipt,
)
from services.ingestion.paid_cost_reservation import (
    DEFAULT_MAX_PROVIDER_CALLS_PER_CLAIM,
    worst_case_authority_usd,
    worst_case_next_call_cost_usd,
)
from services.ingestion.semantic_digest_claim_inputs import (
    PARSER_VERSION,
    SPACY_LIBRARY_VERSION,
    SPACY_MODEL,
    SPACY_MODEL_VERSION,
    PacketNotReadyError,
    build_sentence_hybrid_parent_packet,
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
    / "semantic_digest_sentence_hybrid_canary_selection.v1.json"
)
CUMULATIVE_OWNER_UMBRELLA_USD = Decimal("49.45")
COST_MARGIN = Decimal("1.10")


@dataclass(frozen=True)
class SentenceHybridPopulationRow:
    planned: PlannedPacket
    packet_hash: str
    source_sentence_count: int
    mapped_sentence_count: int
    context_only_sentence_count: int

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
class SentenceHybridSelectedRow:
    row: SentenceHybridPopulationRow
    band_id: str
    selection_order_hash: str
    long_packet_reserved: bool


@dataclass(frozen=True)
class SentenceHybridPrepared:
    receipt: dict[str, Any]
    selected: tuple[SentenceHybridSelectedRow, ...]
    config: SemanticGatewayConfig


def _band_for_basis_points(
    recipe: SentenceHybridCanarySelectionRecipeV1,
    basis_points: int,
) -> Any:
    matches = [
        band
        for band in recipe.bands
        if band.lower_basis_points_inclusive
        <= basis_points
        < band.upper_basis_points_exclusive
    ]
    if len(matches) != 1:
        raise MaterializationError(
            "sentence-hybrid percentile band assignment did not close"
        )
    return matches[0]


def _selection_order_hash(
    row: SentenceHybridPopulationRow,
    *,
    recipe_hash: str,
) -> str:
    return namespace_hash(
        "work",
        {
            "work_kind": "sentence_hybrid_canary_within_band_order.v1",
            "recipe_hash": recipe_hash,
            "parent_id": row.parent_id,
            "packet_hash": row.packet_hash,
        },
    )


def _stratify_and_select(
    population: Sequence[SentenceHybridPopulationRow],
    *,
    recipe: SentenceHybridCanarySelectionRecipeV1,
    recipe_hash: str,
) -> tuple[list[SentenceHybridSelectedRow], list[dict[str, Any]]]:
    if len(population) < recipe.target_count:
        raise MaterializationError("sentence-hybrid fresh population is too small")
    ordered = sorted(population, key=lambda row: (row.packet_bytes, row.parent_id))
    by_band: dict[str, list[tuple[int, int, SentenceHybridPopulationRow]]] = {
        band.band_id: [] for band in recipe.bands
    }
    band_by_parent: dict[str, str] = {}
    for rank, row in enumerate(ordered):
        basis_points = rank * 10_000 // len(ordered)
        band = _band_for_basis_points(recipe, basis_points)
        by_band[band.band_id].append((rank, basis_points, row))
        band_by_parent[row.parent_id] = band.band_id

    long_candidates = sorted(
        (
            row
            for row in population
            if row.packet_bytes > recipe.long_packet_threshold_bytes_exclusive
        ),
        key=lambda row: (-row.packet_bytes, row.parent_id),
    )
    if len(long_candidates) < recipe.minimum_long_packet_selection_count:
        raise MaterializationError(
            "fresh selection cannot represent the required long-packet stratum"
        )
    reserved_long = long_candidates[0]
    reserved = SentenceHybridSelectedRow(
        row=reserved_long,
        band_id=band_by_parent[reserved_long.parent_id],
        selection_order_hash=_selection_order_hash(
            reserved_long,
            recipe_hash=recipe_hash,
        ),
        long_packet_reserved=True,
    )

    selected: list[SentenceHybridSelectedRow] = [reserved]
    used_documents = {reserved_long.doc_id}
    band_receipts: list[dict[str, Any]] = []
    for band in recipe.bands:
        members = by_band[band.band_id]
        chosen = [item for item in selected if item.band_id == band.band_id]
        ranked = sorted(
            (item[2] for item in members),
            key=lambda row: (
                _selection_order_hash(row, recipe_hash=recipe_hash),
                row.parent_id,
            ),
        )
        for row in ranked:
            if row.parent_id == reserved_long.parent_id or row.doc_id in used_documents:
                continue
            chosen.append(
                SentenceHybridSelectedRow(
                    row=row,
                    band_id=band.band_id,
                    selection_order_hash=_selection_order_hash(
                        row,
                        recipe_hash=recipe_hash,
                    ),
                    long_packet_reserved=False,
                )
            )
            used_documents.add(row.doc_id)
            if len(chosen) == band.selection_count:
                break
        if len(chosen) != band.selection_count:
            raise MaterializationError(
                f"sentence-hybrid band {band.band_id} cannot satisfy selection"
            )
        selected = [item for item in selected if item.band_id != band.band_id]
        selected.extend(chosen)
        band_receipts.append(
            {
                "band_id": band.band_id,
                "population_count": len(members),
                "rank_min": min(item[0] for item in members),
                "rank_max": max(item[0] for item in members),
                "packet_bytes_min": min(item[2].packet_bytes for item in members),
                "packet_bytes_max": max(item[2].packet_bytes for item in members),
                "selection_count": len(chosen),
                "reserved_long_packet_count": sum(
                    item.long_packet_reserved for item in chosen
                ),
            }
        )
    if len(selected) != recipe.target_count:
        raise MaterializationError("sentence-hybrid selection count drifted")
    if len({item.row.parent_id for item in selected}) != recipe.target_count:
        raise MaterializationError("sentence-hybrid selection duplicates parents")
    if len({item.row.doc_id for item in selected}) != recipe.target_count:
        raise MaterializationError("sentence-hybrid selection duplicates documents")
    if (
        sum(
            item.row.packet_bytes > recipe.long_packet_threshold_bytes_exclusive
            for item in selected
        )
        < recipe.minimum_long_packet_selection_count
    ):
        raise MaterializationError("sentence-hybrid selection lost long-packet stratum")
    return selected, band_receipts


def _packet_population_set_hash(
    population: Sequence[SentenceHybridPopulationRow],
) -> str:
    raw_hashes: list[str] = []
    for row in population:
        prefix, separator, raw_hash = row.packet_hash.partition(":")
        if prefix != "sha256" or separator != ":" or len(raw_hash) != 64:
            raise MaterializationError("sentence-hybrid packet hash is not canonical")
        try:
            int(raw_hash, 16)
        except ValueError as exc:
            raise MaterializationError(
                "sentence-hybrid packet hash is not canonical"
            ) from exc
        raw_hashes.append(raw_hash)
    return namespace_hash("input-set", frozenset(raw_hashes))


def _cost_kwargs(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_output_tokens": int(route["parameters"]["max_tokens"]),
        "uncached_input_usd": Decimal(str(route["price"]["uncached_input_usd"])),
        "output_usd": Decimal(str(route["price"]["output_usd"])),
        "price_unit_tokens": int(route["price"]["price_unit_tokens"]),
        "max_provider_calls": DEFAULT_MAX_PROVIDER_CALLS_PER_CLAIM,
        "safety_margin": COST_MARGIN,
    }


def _cost_upper_bound(
    rows: Sequence[SentenceHybridPopulationRow],
    *,
    route: dict[str, Any],
) -> Decimal:
    return worst_case_authority_usd(
        packet_input_token_upper_bounds=[row.packet_bytes for row in rows],
        **_cost_kwargs(route),
    )


def _claim_reservation(
    row: SentenceHybridPopulationRow,
    *,
    route: dict[str, Any],
) -> Decimal:
    return worst_case_next_call_cost_usd(
        packet_input_token_upper_bound=row.packet_bytes,
        **_cost_kwargs(route),
    )


def _reservation_sum(
    rows: Sequence[SentenceHybridPopulationRow],
    *,
    route: dict[str, Any],
) -> Decimal:
    return sum((_claim_reservation(row, route=route) for row in rows), Decimal("0"))


def _affordable_prefix(
    rows: Sequence[SentenceHybridPopulationRow],
    *,
    route: dict[str, Any],
    remaining_umbrella_usd: Decimal,
) -> dict[str, Any]:
    reserved = Decimal("0")
    count = 0
    next_reservation: Decimal | None = None
    for row in rows:
        reservation = _claim_reservation(row, route=route)
        if reserved + reservation > remaining_umbrella_usd:
            next_reservation = reservation
            break
        reserved += reservation
        count += 1
    return {
        "order": "planned_ordinal_then_parent_id",
        "affordable_prefix_count": count,
        "affordable_prefix_reservation_usd": str(reserved),
        "remaining_after_prefix_usd": str(remaining_umbrella_usd - reserved),
        "first_unaffordable_claim_reservation_usd": (
            str(next_reservation) if next_reservation is not None else None
        ),
    }


async def _prepare(args: argparse.Namespace) -> SentenceHybridPrepared:
    recipe = load_sentence_hybrid_canary_selection_recipe(RECIPE_PATH)
    recipe_hash = namespace_hash("registry", recipe.model_dump(mode="python"))
    client, db = await _database()
    try:
        active_ingests = await db["ingest_batches"].count_documents(
            {"status": {"$in": ["queued", "running"]}}
        )
        running_semantic_jobs = await db[JOB_COLLECTION].count_documents(
            {"status": "running"}
        )
        queued_semantic_jobs = await db[JOB_COLLECTION].count_documents(
            {"status": "queued"}
        )
        if active_ingests or running_semantic_jobs:
            raise MaterializationError(
                "sentence-hybrid preflight requires no active ingest or running semantic job"
            )
        settings = get_settings()
        canonical_before = await _canonical_store_census(db=db, settings=settings)
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
                raise MaterializationError(
                    "sentence-hybrid compilation child closure drifted"
                )
            validate_materialized_row_against_source(
                row,
                corpus_id=scope.corpus_id,
                document=scope.documents[str(child.get("doc_id") or "")],
                child=child,
            )
            rows_by_child[row.child_id] = row
        if set(rows_by_child) != set(scope.child_ids):
            raise MaterializationError("sentence-hybrid compilation rows do not close")

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
        packet_meta: dict[str, dict[str, Any]] = {}
        non_packet_ready_ledger: list[dict[str, Any]] = []
        for parent in scope.parents:
            child_ids = [
                str(value).strip()
                for value in parent.get("child_ids") or []
                if str(value).strip()
            ]
            child_ids = list(dict.fromkeys(child_ids))
            try:
                built = build_sentence_hybrid_parent_packet(
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
                ledger_entry = _packet_exclusion_ledger_entry(
                    parent=parent,
                    documents=scope.documents,
                    rows_by_child=rows_by_child,
                    reason=exc.reason,
                )
                ledger_entry.update(exc.details)
                non_packet_ready_ledger.append(ledger_entry)
                continue
            packet = built.packet.provider_payload()
            serialized = canonical_json_v1(packet)
            if len(serialized.encode("utf-8")) != built.packet_utf8_bytes:
                raise MaterializationError("sentence-hybrid packet byte replay drifted")
            packet_hash = (
                "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            )
            counts = built.packet.sentence_counts
            packet_meta[built.parent_id] = {
                "packet_hash": packet_hash,
                "source_sentence_count": counts.mapped + counts.unmapped,
                "mapped_sentence_count": counts.mapped,
                "context_only_sentence_count": counts.unmapped,
                "source_child_order_hash": built.source_child_order_hash,
                "source_compilation_set_hash": built.source_compilation_set_hash,
                "sentence_order_hash": built.sentence_order_hash,
            }
            packet_items.append(
                CanaryPacket(
                    packet=packet,
                    context=built.context,
                    parent_id=built.parent_id,
                    doc_id=built.doc_id,
                    entity_count=built.entity_count,
                    source_child_count=len(built.source_child_ids),
                )
            )
        if len(packet_items) + len(non_packet_ready_ledger) != len(scope.parents):
            raise MaterializationError(
                "sentence-hybrid packet population accounting did not close"
            )

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
            SentenceHybridPopulationRow(
                planned=row,
                packet_hash=packet_meta[row.item.parent_id]["packet_hash"],
                source_sentence_count=packet_meta[row.item.parent_id][
                    "source_sentence_count"
                ],
                mapped_sentence_count=packet_meta[row.item.parent_id][
                    "mapped_sentence_count"
                ],
                context_only_sentence_count=packet_meta[row.item.parent_id][
                    "context_only_sentence_count"
                ],
            )
            for row in planned
        ]
        mapped_sentence_count = sum(row.mapped_sentence_count for row in population)
        context_only_sentence_count = sum(
            row.context_only_sentence_count for row in population
        )
        source_sentence_count = sum(row.source_sentence_count for row in population)
        long_packet_count = sum(row.packet_bytes > 20_000 for row in population)
        expected = {
            "packet_ready_count": args.expected_packet_ready_count,
            "non_packet_ready_count": args.expected_non_packet_ready_count,
            "source_sentence_count": args.expected_source_sentence_count,
            "mapped_sentence_count": args.expected_mapped_sentence_count,
            "context_only_sentence_count": args.expected_context_only_sentence_count,
            "long_packet_count": args.expected_over_20000_count,
        }
        actual = {
            "packet_ready_count": len(population),
            "non_packet_ready_count": len(non_packet_ready_ledger),
            "source_sentence_count": source_sentence_count,
            "mapped_sentence_count": mapped_sentence_count,
            "context_only_sentence_count": context_only_sentence_count,
            "long_packet_count": long_packet_count,
        }
        if actual != expected:
            raise MaterializationError(
                "sentence-hybrid measured population drifted from approved contract: "
                f"expected={canonical_json_v1(expected)};"
                f"actual={canonical_json_v1(actual)};"
                "nonready="
                + canonical_json_v1(
                    [
                        {
                            "reason": row.get("reason"),
                            "packet_utf8_bytes": row.get("packet_utf8_bytes"),
                            "max_packet_utf8_bytes": row.get("max_packet_utf8_bytes"),
                        }
                        for row in non_packet_ready_ledger
                    ]
                )
            )

        historical_rows = await (
            db[HISTORICAL_JOB_COLLECTION]
            .find(
                {"corpus_id": scope.corpus_id},
                {"_id": 0, "parent_id": 1, "status": 1},
            )
            .to_list(length=None)
        )
        status_counts = Counter(str(row.get("status") or "") for row in historical_rows)
        purchased_rows = [
            row
            for row in historical_rows
            if row.get("status") in recipe.historical_purchase_statuses
        ]
        purchased_parent_ids = {
            str(row.get("parent_id") or "") for row in purchased_rows
        }
        if (
            not purchased_parent_ids
            or "" in purchased_parent_ids
            or len(purchased_parent_ids) != len(purchased_rows)
        ):
            raise MaterializationError(
                "sentence-hybrid historical purchase identities drifted"
            )
        fresh_population = [
            row for row in population if row.parent_id not in purchased_parent_ids
        ]
        selected, band_receipts = _stratify_and_select(
            fresh_population,
            recipe=recipe,
            recipe_hash=recipe_hash,
        )
        selected_rows = [item.row for item in selected]

        selected_authority = _cost_upper_bound(selected_rows, route=route)
        selected_reservation_sum = _reservation_sum(selected_rows, route=route)
        governing_selected_authority = max(
            selected_authority,
            selected_reservation_sum,
        )
        fresh_authority = _cost_upper_bound(fresh_population, route=route)
        all_ready_authority = _cost_upper_bound(population, route=route)
        max_any_ten_authority = _cost_upper_bound(
            sorted(population, key=lambda row: row.packet_bytes, reverse=True)[:10],
            route=route,
        )
        cumulative = await _cumulative_cost(db, corpus_id=scope.corpus_id)
        if cumulative["budget_accounting_complete"] is not True:
            raise MaterializationError(
                "sentence-hybrid cumulative budget accounting is incomplete"
            )
        cumulative_basis = Decimal(str(cumulative["ceiling_basis_usd"]))
        remaining_umbrella = CUMULATIVE_OWNER_UMBRELLA_USD - cumulative_basis
        if remaining_umbrella <= 0:
            raise MaterializationError("sentence-hybrid owner umbrella is exhausted")
        if governing_selected_authority > remaining_umbrella:
            raise MaterializationError(
                "sentence-hybrid canary authority exceeds remaining owner umbrella"
            )
        fresh_ordered = sorted(
            fresh_population,
            key=lambda row: (row.planned.ordinal, row.parent_id),
        )
        affordable_prefix = _affordable_prefix(
            fresh_ordered,
            route=route,
            remaining_umbrella_usd=remaining_umbrella,
        )

        canonical_after = await _canonical_store_census(db=db, settings=settings)
        canonical_receipt = _canonical_store_census_receipt(
            canonical_before,
            canonical_after,
        )
        if canonical_receipt["protected_exactly_unchanged"] is not True:
            raise MaterializationError(
                "canonical protected stores changed during read-only preflight"
            )
        packet_sizes = [row.packet_bytes for row in population]
        selected_long_count = sum(row.packet_bytes > 20_000 for row in selected_rows)
        receipt = {
            "schema_version": ("polymath.semantic_digest_sentence_hybrid_preflight.v1"),
            "mode": "credential_blind_zero_provider_read_only_preflight",
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
                "packet_schema_hash": (
                    semantic_parent_packet_sentence_hybrid_schema_hash()
                ),
                "packet_max_utf8_bytes": SENTENCE_HYBRID_PACKET_MAX_UTF8_BYTES,
                "packet_set_hash": _packet_population_set_hash(population),
                "packet_set_hash_recipe": (
                    "namespace_hash(input-set, raw_sha256_hex_digests)"
                ),
                "packet_byte_quantiles": _quantiles(packet_sizes),
                "over_20000_packet_count": long_packet_count,
                "over_26000_packet_count": sum(
                    value > SENTENCE_HYBRID_PACKET_MAX_UTF8_BYTES
                    for value in packet_sizes
                ),
                "all_source_sentences_present": True,
                "dropped_sentence_count": 0,
            },
            "mapping_disclosure": {
                "source_sentence_count": source_sentence_count,
                "mapped_sentence_count": mapped_sentence_count,
                "context_only_sentence_count": context_only_sentence_count,
                "mapped_basis_points": (
                    mapped_sentence_count * 10_000 // source_sentence_count
                ),
                "context_only_units_uncitable": True,
                "mapped_units_have_nonempty_atomic_expansions": True,
                "expansion_is_unique_model_intent": False,
            },
            "non_packet_ready_exclusion_ledger": non_packet_ready_ledger,
            "selection_recipe": {
                **recipe.model_dump(mode="python"),
                "recipe_hash": recipe_hash,
                "frozen_before_selection": True,
            },
            "size_strata": band_receipts,
            "historical_ledger": {
                "rows_by_status": dict(sorted(status_counts.items())),
                "purchased_row_count": len(purchased_rows),
                "purchased_unique_parent_count": len(purchased_parent_ids),
                "fresh_packet_ready_count": len(fresh_population),
            },
            "selection": {
                "selection_name": recipe.selection_name,
                "selection_set_hash": namespace_hash(
                    "input-set",
                    frozenset(item.row.planned.job_id for item in selected),
                ),
                "unique_document_count": len({item.row.doc_id for item in selected}),
                "long_packet_selected_count": selected_long_count,
                "total_packet_bytes": sum(row.packet_bytes for row in selected_rows),
                "rows": [
                    {
                        "band_id": item.band_id,
                        "long_packet_reserved": item.long_packet_reserved,
                        "selection_order_hash": item.selection_order_hash,
                        "ordinal": item.row.planned.ordinal,
                        "parent_id": item.row.parent_id,
                        "document_id": item.row.doc_id,
                        "packet_utf8_bytes": item.row.packet_bytes,
                        "mapped_sentence_count": item.row.mapped_sentence_count,
                        "context_only_sentence_count": (
                            item.row.context_only_sentence_count
                        ),
                        "packet_hash": item.row.packet_hash,
                        "input_hash": item.row.planned.input_hash,
                        "cache_key": item.row.planned.cache_key,
                        "job_id": item.row.planned.job_id,
                        "claim_reservation_usd": str(
                            _claim_reservation(item.row, route=route)
                        ),
                    }
                    for item in selected
                ],
            },
            "provider_contract": {
                "route_id": price_card["route_id"],
                "model_id": parameter_card["model_id"],
                "capability_tier": parameter_card["capability_tier"],
                "parameter_version": parameter_card["parameter_version"],
                "runtime_version": parameter_card["runtime_version"],
                "prompt_version": config.prompt_version,
                "prompt_hash": semantic_digest_prompt_hash(
                    config.prompt_version,
                    config.repair_prompt_version,
                ),
                "repair_prompt_version": config.repair_prompt_version,
                "repair_prompt_hash": semantic_digest_repair_prompt_hash(
                    config.repair_prompt_version
                ),
                "digest_schema_hash": semantic_digest_schema_hash(),
                "max_tokens": int(parameter_card["max_tokens"]),
                "temperature": parameter_card["temperature"],
                "thinking": parameter_card["thinking"],
                "provider_credential_plaintext_reads": 0,
            },
            "cost_authority": {
                "basis": (
                    "packet_utf8_bytes_as_input_token_upper_bound;two_full_attempts;"
                    "route_max_output_each_attempt;10_percent_margin;usd_ceiling_round"
                ),
                "selected_batch_authority_usd": str(selected_authority),
                "selected_claim_reservation_sum_usd": str(selected_reservation_sum),
                "selected_governing_authority_usd": str(governing_selected_authority),
                "max_any_10_authority_usd": str(max_any_ten_authority),
                "fresh_population_authority_usd": str(fresh_authority),
                "all_packet_ready_authority_usd": str(all_ready_authority),
                "owner_cumulative_umbrella_usd": str(CUMULATIVE_OWNER_UMBRELLA_USD),
                "cumulative_ceiling_basis_usd": str(cumulative_basis),
                "remaining_owner_umbrella_usd": str(remaining_umbrella),
                "fresh_worst_case_exceeds_remaining_umbrella": (
                    fresh_authority > remaining_umbrella
                ),
                "selected_authority_fits_remaining_umbrella": (
                    governing_selected_authority <= remaining_umbrella
                ),
                "affordable_prefix": affordable_prefix,
                "expected_spend_used_as_authority": False,
            },
            "operational_preflight": {
                "active_ingest_batches": active_ingests,
                "running_semantic_jobs": running_semantic_jobs,
                "queued_semantic_jobs_observed_not_claimed": queued_semantic_jobs,
                "canonical_store_census": canonical_receipt,
                "provider_calls": 0,
                "database_writes": 0,
                "canonical_writes": 0,
                "projection_writes": 0,
            },
            "disclosed_noncanonical_stores": {
                COMPILATION_COLLECTION: await _collection_disclosure(
                    db,
                    scope.corpus_id,
                )
            },
            "authorization": {
                "provider_execution_authorized": False,
                "paid_runner_present_in_this_change": False,
                "required_next_step": (
                    "senior_preregisters_exact_hashes_and_restates_fresh_GO"
                ),
            },
        }
        return SentenceHybridPrepared(
            receipt=receipt,
            selected=tuple(selected),
            config=config,
        )
    finally:
        client.close()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    return (await _prepare(args)).receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-name", default=DEFAULT_CORPUS_NAME)
    parser.add_argument("--expected-parent-count", type=int, default=795)
    parser.add_argument("--expected-child-count", type=int, default=3493)
    parser.add_argument("--expected-packet-ready-count", type=int, default=793)
    parser.add_argument("--expected-non-packet-ready-count", type=int, default=2)
    parser.add_argument("--expected-source-sentence-count", type=int, default=30694)
    parser.add_argument("--expected-mapped-sentence-count", type=int, default=24845)
    parser.add_argument(
        "--expected-context-only-sentence-count", type=int, default=5849
    )
    parser.add_argument("--expected-over-20000-count", type=int, default=3)
    parser.add_argument("--max-entities", type=int, default=40)
    return parser


def main() -> int:
    try:
        receipt = asyncio.run(_run(_parser().parse_args()))
    except Exception as exc:
        error_code = str(exc) if isinstance(exc, MaterializationError) else None
        print(
            json.dumps(
                {
                    "schema_version": (
                        "polymath.semantic_digest_sentence_hybrid_preflight.failure.v1"
                    ),
                    "error_class": type(exc).__name__,
                    "error_code": error_code,
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
