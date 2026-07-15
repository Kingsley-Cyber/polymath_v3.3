#!/usr/bin/env python3
"""Run the owner-authorized B1-scoped mark Phase-2 prose purchase.

The script has a credential-blind, read-only preflight and an exact-GO
execution mode.  It deliberately reuses the certified interim-prose packet,
gateway, prompt, repair, route, cache, and durable terminal-write contracts.
Only population selection, durable selection identity, launch sealing,
rolling controls, and append-only supersession bookkeeping are new.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import sys
from typing import Any, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne


HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from config import get_settings  # noqa: E402
from models.hash_taxonomy import canonical_json_v1, namespace_hash  # noqa: E402
from scripts.materialize_semantic_digest_claim_inputs import (  # noqa: E402
    MaterializationError,
    _database,
    _load_scope,
)
from scripts.semantic_gateway_mark_paid_pass import (  # noqa: E402
    FAILURE_STATUSES,
    JOB_COLLECTION,
    PURCHASED_TERMINAL_STATUSES,
    ROUTE_ID,
    SUCCESS_STATUS,
    UNPRICED_EXPOSURE_BASIS,
    UNPRICED_EXPOSURE_BOUND_USD,
    CanaryPacket,
    PaidPassError,
    PlannedPacket,
    _build_config,
    _cost_accounting,
    _credential_fingerprint,
    _cumulative_cost,
    _load_certified_acceptance,
    _materialize_jobs,
    _plan_packets,
    _persist_phase_selection,
    _run_claimed_job,
    _certified_parent_ids,
)
from scripts.semantic_gateway_ugo_canary import (  # noqa: E402
    DEFAULT_PROVIDER_PRICE_CARDS,
    DEFAULT_ROUTE_PARAMETER_CARDS,
    ProviderPriceCard,
    RouteParameterCard,
    _canonical_store_census,
    _canonical_store_census_receipt,
    _load_provider_price_card,
    _load_route_parameter_card,
    _packet_from_parent,
)
from services.ingestion.job_leases import (  # noqa: E402
    claim_runnable_jobs,
    corpus_lane_lease,
)
from services.ingestion.paid_cost_reservation import (  # noqa: E402
    cost_reservation_allows_claim,
    worst_case_authority_usd,
    worst_case_next_call_cost_usd,
)
from services.ingestion.semantic_parent_eligibility import (  # noqa: E402
    parent_eligibility_recipe_hash,
)
from services.semantic_gateway import (  # noqa: E402
    PROMPT_VERSION,
    REPAIR_PROMPT_VERSION,
    SemanticGatewayRoute,
    semantic_digest_prompt_hash,
    semantic_digest_repair_prompt_hash,
    semantic_digest_schema_hash,
)
from services.settings import settings_service  # noqa: E402


DEFAULT_CORPUS_NAME = "markbuildsbrands_transcripts"
DEFAULT_CREDENTIAL_PROVIDER = "longcat"
PHASE = "phase2_prose_b1"
SELECTION_NAME = "mark-phase2.b1-interim-prose.parent-digest.v6.v2"
LANE = "semantic_digest_paid_pass"
AUTHORIZATION_REFERENCE = "COORDINATION.md#2026-07-15T05:24:45Z"
EXPECTED_PARENT_COUNT = 795
EXPECTED_CHILD_COUNT = 3493
REBUY_ORDINALS = (60, 569)
STRUCTURED_EXCLUSION_PHASES = {"b4_atomic", "sentence_hybrid_v3_canary"}
ROLLING_WINDOW = 50
MIN_ROLLING_ACCEPTANCE = Decimal("0.90")
MAX_CONSECUTIVE_DLQ = 5
MAX_READ_TIMEOUTS = 2
INITIAL_CONCURRENCY = 3
ESCALATED_CONCURRENCY = 6
ESCALATE_AFTER_CLEAN = 100
REMAINING_UMBRELLA_USD = Decimal("46.69")
COST_MARGIN = Decimal("1.10")
SUPERSESSION_COLLECTION = "semantic_digest_supersessions"
SUPERSESSION_REASON = "faithfulness_rejected_unsupported_synthesis"


@dataclass(frozen=True)
class ProsePhase2Prepared:
    receipt: dict[str, Any]
    selected: tuple[PlannedPacket, ...]
    config: Any
    parameter_card: RouteParameterCard
    price_card: ProviderPriceCard
    rebuy_sources: dict[int, dict[str, str]]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _selection_job_id(*, corpus_id: str, row: PlannedPacket) -> str:
    return namespace_hash(
        "work",
        {
            "artifact_type": "semantic_digest_prose_phase2_job",
            "selection_name": SELECTION_NAME,
            "corpus_id": corpus_id,
            "parent_id": row.item.parent_id,
            "cache_key": row.cache_key,
        },
    )


def _selection_hash(rows: Sequence[PlannedPacket]) -> str:
    return namespace_hash("input-set", frozenset(row.job_id for row in rows))


def _packet_set_hash(rows: Sequence[PlannedPacket]) -> str:
    hashes = [
        hashlib.sha256(canonical_json_v1(row.item.packet).encode("utf-8")).hexdigest()
        for row in rows
    ]
    return namespace_hash("input-set", frozenset(hashes))


def _phase2_selection(
    planned: Sequence[PlannedPacket],
    *,
    corpus_id: str,
    attempted_parent_ids: set[str],
    certified_parent_ids: set[str],
    explicitly_excluded_parent_ids: set[str],
    rebuy_parent_ids: set[str],
) -> list[PlannedPacket]:
    population_ids = {row.item.parent_id for row in planned}
    if len(population_ids) != len(planned):
        raise PaidPassError("B1 prose population contains duplicate parents")
    if len(rebuy_parent_ids) != len(REBUY_ORDINALS):
        raise PaidPassError("required prose re-buy parent count drifted")
    if not rebuy_parent_ids <= population_ids:
        raise PaidPassError("required prose re-buy parent is outside B1 eligibility")
    if rebuy_parent_ids & certified_parent_ids:
        raise PaidPassError("required prose re-buy already has a certified prose cache")
    excluded = (
        attempted_parent_ids | certified_parent_ids | explicitly_excluded_parent_ids
    ) - rebuy_parent_ids
    selected = [row for row in planned if row.item.parent_id not in excluded]
    if not rebuy_parent_ids <= {row.item.parent_id for row in selected}:
        raise PaidPassError("required prose re-buy did not survive selection")
    return [
        replace(
            row,
            job_id=_selection_job_id(corpus_id=corpus_id, row=row),
        )
        for row in selected
    ]


def _terminal_rows_in_completion_order(
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    terminal = [row for row in rows if row.get("status") in PURCHASED_TERMINAL_STATUSES]
    return sorted(
        terminal,
        key=lambda row: (
            row.get("completed_at") or datetime.min,
            int(row.get("ordinal") or 0),
        ),
    )


def phase2_prose_stop_reason(rows: Sequence[dict[str, Any]]) -> str | None:
    terminal = _terminal_rows_in_completion_order(rows)
    if _cost_accounting(terminal)["budget_accounting_complete"] is not True:
        return "cost_telemetry_incomplete"
    if (
        sum(row.get("transport_error_class") == "ReadTimeout" for row in terminal)
        >= MAX_READ_TIMEOUTS
    ):
        return "read_timeout_recurrence_pause"
    if len(terminal) >= ROLLING_WINDOW:
        window = terminal[-ROLLING_WINDOW:]
        accepted = sum(row.get("status") == SUCCESS_STATUS for row in window)
        if Decimal(accepted) / Decimal(ROLLING_WINDOW) < MIN_ROLLING_ACCEPTANCE:
            return "rolling_acceptance_below_90_percent"
    consecutive = 0
    for row in reversed(terminal):
        if row.get("status") not in FAILURE_STATUSES:
            break
        consecutive += 1
    if consecutive >= MAX_CONSECUTIVE_DLQ:
        return "five_consecutive_terminal_dlqs"
    return None


def phase2_prose_concurrency(rows: Sequence[dict[str, Any]]) -> int:
    terminal = _terminal_rows_in_completion_order(rows)
    first = terminal[:ESCALATE_AFTER_CLEAN]
    if len(first) == ESCALATE_AFTER_CLEAN and all(
        row.get("status") == SUCCESS_STATUS for row in first
    ):
        return ESCALATED_CONCURRENCY
    return INITIAL_CONCURRENCY


def _typed_route_cards() -> tuple[RouteParameterCard, ProviderPriceCard]:
    parameter = _load_route_parameter_card(
        DEFAULT_ROUTE_PARAMETER_CARDS,
        route_id=ROUTE_ID,
        model_id="openai/LongCat-2.0",
        api_base="https://api.longcat.chat/openai/v1",
    )
    price = _load_provider_price_card(
        DEFAULT_PROVIDER_PRICE_CARDS,
        route_id=ROUTE_ID,
        model_id=parameter.model_id,
        api_base=parameter.api_base,
    )
    if (
        parameter.capability_tier != "tier3"
        or parameter.max_tokens != 8192
        or parameter.temperature != 0
        or parameter.thinking != "disabled"
    ):
        raise PaidPassError("certified LongCat route parameter card drifted")
    return parameter, price


async def _build_b1_prose_packets(
    db: Any,
    *,
    corpus_name: str,
    expected_parent_count: int,
    expected_child_count: int,
    max_entities: int,
) -> tuple[str, list[CanaryPacket]]:
    scope = await _load_scope(
        db,
        corpus_name=corpus_name,
        expected_parent_count=expected_parent_count,
        expected_child_count=expected_child_count,
    )
    parent_ids = [str(row.get("parent_id") or "") for row in scope.parents]
    source_rows = await (
        db["parent_chunks"]
        .find(
            {"corpus_id": scope.corpus_id, "parent_id": {"$in": parent_ids}},
            {"_id": 0, "parent_id": 1, "source_hash": 1},
        )
        .to_list(length=None)
    )
    source_by_parent = {
        str(row.get("parent_id") or ""): row.get("source_hash") for row in source_rows
    }
    if set(source_by_parent) != set(parent_ids):
        raise PaidPassError("B1 parent source-hash closure drifted")
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
    by_child = {str(row.get("chunk_id") or ""): row for row in extraction_rows}
    packets: list[CanaryPacket] = []
    for parent in scope.parents:
        enriched_parent = {
            **parent,
            "source_hash": source_by_parent[str(parent.get("parent_id") or "")],
        }
        packets.append(
            _packet_from_parent(
                corpus_id=scope.corpus_id,
                corpus_name=corpus_name,
                parent=enriched_parent,
                extraction_rows=[
                    by_child[str(child_id)]
                    for child_id in parent.get("child_ids") or []
                    if str(child_id) in by_child
                ],
                max_entities=max_entities,
            )
        )
    return scope.corpus_id, packets


async def _rebuy_source_rows(
    db: Any,
    *,
    corpus_id: str,
    planned: Sequence[PlannedPacket],
) -> dict[int, dict[str, str]]:
    by_parent_id = {row.item.parent_id: row for row in planned}
    sources: dict[int, dict[str, str]] = {}
    for ordinal in REBUY_ORDINALS:
        rows = await (
            db[JOB_COLLECTION]
            .find(
                {
                    "corpus_id": corpus_id,
                    "phase": "b4_atomic",
                    "ordinal": ordinal,
                    "status": SUCCESS_STATUS,
                },
                {"_id": 0, "parent_id": 1, "cache_key": 1, "job_id": 1},
            )
            .to_list(length=3)
        )
        if len(rows) != 1 or not rows[0].get("cache_key"):
            raise PaidPassError(f"ord{ordinal} rejected-v2 source row drifted")
        parent_id = str(rows[0].get("parent_id") or "")
        planned_row = by_parent_id.get(parent_id)
        if planned_row is None:
            raise PaidPassError(
                f"ord{ordinal} rejected-v2 parent is outside B1 eligibility"
            )
        cache_key = str(rows[0]["cache_key"])
        cache = await db["semantic_digest_cache"].find_one(
            {
                "_id": cache_key,
                "status": "accepted_cache",
                "canonical_write": False,
                "digest.parent_id": parent_id,
            },
            {"_id": 1},
        )
        if cache is None:
            raise PaidPassError(f"ord{ordinal} rejected-v2 cache history drifted")
        sources[ordinal] = {
            "parent_id": parent_id,
            "replacement_ordinal": str(planned_row.ordinal),
            "source_job_id": str(rows[0].get("job_id") or ""),
            "source_cache_key": cache_key,
        }
    return sources


async def _prepare(args: argparse.Namespace) -> ProsePhase2Prepared:
    client, db = await _database()
    try:
        active_ingests = await db["ingest_batches"].count_documents(
            {"status": {"$in": ["queued", "running"]}}
        )
        running_jobs = await db[JOB_COLLECTION].count_documents({"status": "running"})
        if active_ingests or running_jobs:
            raise PaidPassError(
                "Phase-2 prose preflight requires zero active ingests and semantic jobs"
            )
        corpus_id, packets = await _build_b1_prose_packets(
            db,
            corpus_name=args.corpus_name,
            expected_parent_count=args.expected_parent_count,
            expected_child_count=args.expected_child_count,
            max_entities=args.max_entities,
        )
        parameter_card, price_card = _typed_route_cards()
        config = _build_config(parameter_card)
        if (
            config.prompt_version != PROMPT_VERSION
            or config.repair_prompt_version != REPAIR_PROMPT_VERSION
        ):
            raise PaidPassError("certified prose prompt contract drifted")
        base_planned = _plan_packets(
            corpus_id=corpus_id,
            packets=packets,
            config=config,
        )
        rebuy_sources = await _rebuy_source_rows(
            db, corpus_id=corpus_id, planned=base_planned
        )
        current_selection_rows = await (
            db[JOB_COLLECTION]
            .find(
                {
                    "corpus_id": corpus_id,
                    "phase_selection": SELECTION_NAME,
                    "prompt_version": PROMPT_VERSION,
                    "repair_prompt_version": REPAIR_PROMPT_VERSION,
                },
                {"_id": 0, "job_id": 1},
            )
            .sort("ordinal", 1)
            .to_list(length=None)
        )
        selection_planned = [
            replace(
                row,
                job_id=_selection_job_id(corpus_id=corpus_id, row=row),
            )
            for row in base_planned
        ]
        by_job_id = {row.job_id: row for row in selection_planned}
        ledger_rows = await (
            db[JOB_COLLECTION]
            .find(
                {"corpus_id": corpus_id},
                {
                    "_id": 0,
                    "job_id": 1,
                    "parent_id": 1,
                    "status": 1,
                    "attempt_count": 1,
                    "phase": 1,
                    "phase_selection": 1,
                },
            )
            .to_list(length=None)
        )
        current_job_ids = {
            str(row.get("job_id") or "") for row in current_selection_rows
        }
        historical_rows = [
            row
            for row in ledger_rows
            if str(row.get("job_id") or "") not in current_job_ids
        ]
        attempted_ids = {
            str(row.get("parent_id") or "")
            for row in historical_rows
            if int(row.get("attempt_count") or 0) > 0
            or row.get("status") in PURCHASED_TERMINAL_STATUSES
        }
        explicitly_excluded_ids = {
            str(row.get("parent_id") or "")
            for row in historical_rows
            if row.get("phase") in STRUCTURED_EXCLUSION_PHASES
        }
        certified_ids = await _certified_parent_ids(
            db,
            planned=base_planned,
            config=config,
        )
        rebuy_ids = {row["parent_id"] for row in rebuy_sources.values()}
        if current_selection_rows:
            persisted_ids = [
                str(row.get("job_id") or "") for row in current_selection_rows
            ]
            if any(job_id not in by_job_id for job_id in persisted_ids):
                raise PaidPassError(
                    "persisted Phase-2 prose selection identity drifted"
                )
            selected = [by_job_id[job_id] for job_id in persisted_ids]
            selection_mode = "resume_persisted_exact"
        else:
            selected = _phase2_selection(
                base_planned,
                corpus_id=corpus_id,
                attempted_parent_ids=attempted_ids,
                certified_parent_ids=certified_ids,
                explicitly_excluded_parent_ids=explicitly_excluded_ids,
                rebuy_parent_ids=rebuy_ids,
            )
            selection_mode = "fresh_from_live_ledger"
        if len({row.item.parent_id for row in selected}) != len(selected):
            raise PaidPassError("Phase-2 prose selection contains duplicate parents")
        if not rebuy_ids <= {row.item.parent_id for row in selected}:
            raise PaidPassError("Phase-2 prose selection lost a required re-buy")

        current_cost = await _cumulative_cost(db, corpus_id=corpus_id)
        if current_cost["budget_accounting_complete"] is not True:
            raise PaidPassError("cumulative paid-cost ledger is incomplete")
        prior_basis = Decimal(str(current_cost["ceiling_basis_usd"]))
        selection_authority = worst_case_authority_usd(
            packet_input_token_upper_bounds=[row.packet_bytes for row in selected],
            max_output_tokens=config.max_tokens,
            uncached_input_usd=price_card.uncached_input_usd,
            output_usd=price_card.output_usd,
            price_unit_tokens=price_card.price_unit_tokens,
            safety_margin=COST_MARGIN,
        )
        max_next_call = max(
            worst_case_next_call_cost_usd(
                packet_input_token_upper_bound=row.packet_bytes,
                max_output_tokens=config.max_tokens,
                uncached_input_usd=price_card.uncached_input_usd,
                output_usd=price_card.output_usd,
                price_unit_tokens=price_card.price_unit_tokens,
                safety_margin=COST_MARGIN,
            )
            for row in selected
        )
        absolute_authority = prior_basis + REMAINING_UMBRELLA_USD
        settings = get_settings()
        canonical = await _canonical_store_census(db=db, settings=settings)
        status_counts = Counter(str(row.get("status") or "") for row in historical_rows)
        selected_ids = {row.item.parent_id for row in selected}
        eligible_ids = {row.item.parent_id for row in base_planned}
        receipt = {
            "schema_version": "polymath.semantic_digest_prose_phase2_preflight.v1",
            "mode": "zero_provider_read_only_preflight",
            "authorization_reference": AUTHORIZATION_REFERENCE,
            "corpus": {
                "name": args.corpus_name,
                "corpus_id": corpus_id,
                "eligible_parent_count": len(base_planned),
                "eligible_child_count": args.expected_child_count,
                "eligibility_recipe_hash": parent_eligibility_recipe_hash(),
            },
            "provider_contract": {
                "route_id": ROUTE_ID,
                "model_id": parameter_card.model_id,
                "api_base_origin": urlsplit(parameter_card.api_base).netloc,
                "capability_tier": parameter_card.capability_tier,
                "parameter_version": parameter_card.parameter_version,
                "runtime_version": parameter_card.runtime_version,
                "prompt_version": config.prompt_version,
                "prompt_hash": semantic_digest_prompt_hash(
                    config.prompt_version, config.repair_prompt_version
                ),
                "repair_prompt_version": config.repair_prompt_version,
                "repair_prompt_hash": semantic_digest_repair_prompt_hash(
                    config.repair_prompt_version
                ),
                "schema_hash": semantic_digest_schema_hash(),
                "packet_contract": "interim_prose",
                "max_tokens": parameter_card.max_tokens,
                "temperature": parameter_card.temperature,
                "thinking": parameter_card.thinking,
                "credential_plaintext_read": False,
            },
            "selection": {
                "selection_name": SELECTION_NAME,
                "mode": selection_mode,
                "target_count": len(selected),
                "selection_set_hash": _selection_hash(selected),
                "selected_packet_set_hash": _packet_set_hash(selected),
                "eligible_packet_set_hash": _packet_set_hash(base_planned),
                "packet_bytes_min": min(row.packet_bytes for row in selected),
                "packet_bytes_max": max(row.packet_bytes for row in selected),
                "required_rebuy_ordinals": list(REBUY_ORDINALS),
                "required_rebuy_replacement_ordinals": sorted(
                    int(row["replacement_ordinal"]) for row in rebuy_sources.values()
                ),
                "required_rebuy_count": len(rebuy_ids),
                "fresh_non_rebuy_count": len(selected_ids - rebuy_ids),
            },
            "ledger": {
                "historical_rows_by_status": dict(sorted(status_counts.items())),
                "eligible_attempted_or_purchased_count": len(
                    attempted_ids & eligible_ids
                ),
                "eligible_certified_prose_count": len(certified_ids & eligible_ids),
                "eligible_structured_selection_count": len(
                    explicitly_excluded_ids & eligible_ids
                ),
                "excluded_union_before_rebuy_count": len(
                    (attempted_ids | certified_ids | explicitly_excluded_ids)
                    & eligible_ids
                ),
                "rebuy_exception_count": len(rebuy_ids),
                "accounting_closes": len(selected_ids)
                == len(
                    eligible_ids
                    - (
                        (attempted_ids | certified_ids | explicitly_excluded_ids)
                        - rebuy_ids
                    )
                ),
            },
            "cost_authority": {
                "prior_cumulative_ceiling_basis_usd": str(prior_basis),
                "remaining_umbrella_usd": str(REMAINING_UMBRELLA_USD),
                "absolute_authorized_ceiling_usd": str(absolute_authority),
                "selection_two_attempt_authority_usd": str(selection_authority),
                "max_next_claim_reservation_usd": str(max_next_call),
                "selection_fits_remaining_umbrella": (
                    selection_authority <= REMAINING_UMBRELLA_USD
                ),
                "at_least_one_next_claim_fits": (
                    max_next_call <= REMAINING_UMBRELLA_USD
                ),
                "reservation_boundary_may_stop_with_outstanding": True,
                "reservation_rule": "cumulative_basis_plus_max_claim_lte_absolute_authority",
            },
            "controls": {
                "rolling_window": ROLLING_WINDOW,
                "minimum_rolling_acceptance": str(MIN_ROLLING_ACCEPTANCE),
                "max_consecutive_dlq": MAX_CONSECUTIVE_DLQ,
                "max_read_timeouts_before_pause": MAX_READ_TIMEOUTS,
                "initial_concurrency": INITIAL_CONCURRENCY,
                "escalated_concurrency": ESCALATED_CONCURRENCY,
                "escalate_after_clean": ESCALATE_AFTER_CLEAN,
                "max_attempts_per_job": 1,
                "max_provider_calls_per_claim": 2,
                "canonical_write": False,
            },
            "operational_preflight": {
                "active_ingest_batches": active_ingests,
                "running_semantic_jobs": running_jobs,
                "canonical_before": _canonical_store_census_receipt(
                    canonical, canonical
                )["before"],
                "provider_calls": 0,
                "database_writes": 0,
                "canonical_writes": 0,
            },
            "all_green": bool(
                len(base_planned) == args.expected_parent_count
                and len(rebuy_ids) == len(REBUY_ORDINALS)
                and receipt_accounting_closes(
                    eligible_ids=eligible_ids,
                    selected_ids=selected_ids,
                    attempted_ids=attempted_ids,
                    certified_ids=certified_ids,
                    explicitly_excluded_ids=explicitly_excluded_ids,
                    rebuy_ids=rebuy_ids,
                )
                and max_next_call <= REMAINING_UMBRELLA_USD
            ),
        }
        return ProsePhase2Prepared(
            receipt=receipt,
            selected=tuple(selected),
            config=config,
            parameter_card=parameter_card,
            price_card=price_card,
            rebuy_sources=rebuy_sources,
        )
    finally:
        client.close()


def receipt_accounting_closes(
    *,
    eligible_ids: set[str],
    selected_ids: set[str],
    attempted_ids: set[str],
    certified_ids: set[str],
    explicitly_excluded_ids: set[str],
    rebuy_ids: set[str],
) -> bool:
    expected = eligible_ids - (
        (attempted_ids | certified_ids | explicitly_excluded_ids) - rebuy_ids
    )
    return selected_ids == expected


def _assert_exact(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise PaidPassError(f"{label} mismatch")


def _assert_go_contract(
    prepared: ProsePhase2Prepared,
    *,
    authorization_reference: str,
    expected_selection_count: int,
    expected_selection_set_hash: str,
    expected_prompt_hash: str,
    expected_repair_prompt_hash: str,
    expected_schema_hash: str,
    expected_prior_basis_usd: Decimal,
    remaining_authority_usd: Decimal,
) -> None:
    receipt = prepared.receipt
    _assert_exact(
        "authorization reference",
        authorization_reference,
        AUTHORIZATION_REFERENCE,
    )
    _assert_exact(
        "selection count",
        receipt["selection"]["target_count"],
        expected_selection_count,
    )
    _assert_exact(
        "selection set hash",
        receipt["selection"]["selection_set_hash"],
        expected_selection_set_hash,
    )
    _assert_exact(
        "prompt hash", receipt["provider_contract"]["prompt_hash"], expected_prompt_hash
    )
    _assert_exact(
        "repair prompt hash",
        receipt["provider_contract"]["repair_prompt_hash"],
        expected_repair_prompt_hash,
    )
    _assert_exact(
        "schema hash", receipt["provider_contract"]["schema_hash"], expected_schema_hash
    )
    _assert_exact(
        "prior basis",
        Decimal(receipt["cost_authority"]["prior_cumulative_ceiling_basis_usd"]),
        expected_prior_basis_usd,
    )
    _assert_exact(
        "remaining authority", remaining_authority_usd, REMAINING_UMBRELLA_USD
    )
    if receipt["all_green"] is not True:
        raise PaidPassError("credential-blind preflight is not green")


async def _selection_rows(
    db: Any,
    *,
    selected: Sequence[PlannedPacket],
) -> list[dict[str, Any]]:
    job_ids = [row.job_id for row in selected]
    rows = await (
        db[JOB_COLLECTION]
        .find(
            {
                "job_id": {"$in": job_ids},
                "phase_selection": SELECTION_NAME,
                "phase": PHASE,
                "prompt_version": PROMPT_VERSION,
                "repair_prompt_version": REPAIR_PROMPT_VERSION,
            },
            {"_id": 0},
        )
        .sort("ordinal", 1)
        .to_list(length=None)
    )
    if len(rows) != len(selected):
        raise PaidPassError(
            f"Phase-2 durable row count drifted: expected {len(selected)}, found {len(rows)}"
        )
    if {str(row.get("job_id") or "") for row in rows} != set(job_ids):
        raise PaidPassError("Phase-2 durable selection identity drifted")
    return rows


def _checkpoint_receipt(
    rows: Sequence[dict[str, Any]],
    *,
    cumulative_cost: dict[str, Any],
    absolute_authority: Decimal,
    canonical_receipt: dict[str, Any],
) -> dict[str, Any]:
    terminal = _terminal_rows_in_completion_order(rows)
    accepted = sum(row.get("status") == SUCCESS_STATUS for row in terminal)
    dead_letters = sum(row.get("status") in FAILURE_STATUSES for row in terminal)
    return {
        "schema_version": "polymath.semantic_digest_prose_phase2_checkpoint.v1",
        "generated_at": _utc_now(),
        "selection_name": SELECTION_NAME,
        "terminal_count": len(terminal),
        "accepted_count": accepted,
        "dead_letter_count": dead_letters,
        "acceptance": accepted / len(terminal) if terminal else 0.0,
        "read_timeout_count": sum(
            row.get("transport_error_class") == "ReadTimeout" for row in terminal
        ),
        "current_concurrency": phase2_prose_concurrency(rows),
        "stop_reason": phase2_prose_stop_reason(rows),
        "cumulative_cost": cumulative_cost,
        "absolute_authorized_ceiling_usd": str(absolute_authority),
        "canonical_store_census": canonical_receipt,
        "security": {
            "packet_text_in_receipt": False,
            "raw_provider_output_in_receipt": False,
            "plaintext_credentials_in_receipt": False,
            "canonical_write": False,
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


async def _persist_rebuy_supersessions(
    db: Any,
    *,
    selected: Sequence[PlannedPacket],
    rebuy_sources: dict[int, dict[str, str]],
) -> int:
    by_parent_id = {row.item.parent_id: row for row in selected}
    now = datetime.utcnow()
    ops: list[UpdateOne] = []
    source_updates: list[tuple[dict[str, str], str, str]] = []
    for ordinal, source in sorted(rebuy_sources.items()):
        replacement = by_parent_id.get(source["parent_id"])
        if replacement is None:
            raise PaidPassError(f"ord{ordinal} replacement selection is absent")
        job = await db[JOB_COLLECTION].find_one(
            {"job_id": replacement.job_id, "status": SUCCESS_STATUS},
            {"_id": 0, "cache_key": 1},
        )
        if job is None:
            continue
        replacement_cache_key = str(job.get("cache_key") or "")
        cache = await db["semantic_digest_cache"].find_one(
            {
                "_id": replacement_cache_key,
                "status": "accepted_cache",
                "canonical_write": False,
                "digest.parent_id": source["parent_id"],
            },
            {"_id": 1},
        )
        if cache is None:
            raise PaidPassError(f"ord{ordinal} replacement cache is absent")
        ledger_id = namespace_hash(
            "logical-artifact",
            {
                "artifact_type": "semantic_digest_supersession",
                "authorization_reference": AUTHORIZATION_REFERENCE,
                "source_cache_key": source["source_cache_key"],
                "replacement_cache_key": replacement_cache_key,
            },
        )
        ops.append(
            UpdateOne(
                {"_id": ledger_id},
                {
                    "$setOnInsert": {
                        "_id": ledger_id,
                        "corpus_id": replacement.item.packet["corpus_id"],
                        "parent_id": source["parent_id"],
                        "ordinal": ordinal,
                        "source_job_id": source["source_job_id"],
                        "source_cache_key": source["source_cache_key"],
                        "replacement_job_id": replacement.job_id,
                        "replacement_cache_key": replacement_cache_key,
                        "reason": SUPERSESSION_REASON,
                        "authorization_reference": AUTHORIZATION_REFERENCE,
                        "canonical_write": False,
                        "history_preserved": True,
                        "created_at": now,
                    },
                    "$set": {"updated_at": now},
                },
                upsert=True,
            )
        )
        source_updates.append((source, ledger_id, replacement_cache_key))
    if ops:
        result = await db[SUPERSESSION_COLLECTION].bulk_write(ops, ordered=True)
        for source, ledger_id, replacement_cache_key in source_updates:
            source_result = await db["semantic_digest_cache"].update_one(
                {
                    "_id": source["source_cache_key"],
                    "status": "accepted_cache",
                    "canonical_write": False,
                    "digest.parent_id": source["parent_id"],
                },
                {
                    "$set": {
                        "serving_eligible": False,
                        "faithfulness_status": "rejected",
                        "supersession_ledger_id": ledger_id,
                        "superseded_by_cache_key": replacement_cache_key,
                        "supersession_reason": SUPERSESSION_REASON,
                        "superseded_at": now,
                        "updated_at": now,
                    }
                },
            )
            if int(source_result.matched_count or 0) != 1:
                raise PaidPassError("rejected-v2 cache supersession identity drifted")
        return int(result.upserted_count or 0) + int(result.matched_count or 0)
    return 0


async def _execute(
    db: Any,
    *,
    prepared: ProsePhase2Prepared,
    api_key: str,
    absolute_authority: Decimal,
    canonical_before: dict[str, Any],
    checkpoint_dir: Path,
) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    selected = list(prepared.selected)
    by_job_id = {row.job_id: row for row in selected}
    runner = f"semantic-digest-prose-phase2:{uuid4().hex}"
    fingerprint = _credential_fingerprint(api_key)
    receipts: list[dict[str, Any]] = []
    checkpoint_paths: list[str] = []
    next_checkpoint = ROLLING_WINDOW
    while True:
        rows = await _selection_rows(db, selected=selected)
        terminal = _terminal_rows_in_completion_order(rows)
        while len(terminal) >= next_checkpoint:
            canonical_now = await _canonical_store_census(
                db=db, settings=get_settings()
            )
            cumulative = await _cumulative_cost(
                db, corpus_id=prepared.receipt["corpus"]["corpus_id"]
            )
            checkpoint = _checkpoint_receipt(
                rows,
                cumulative_cost=cumulative,
                absolute_authority=absolute_authority,
                canonical_receipt=_canonical_store_census_receipt(
                    canonical_before, canonical_now
                ),
            )
            path = checkpoint_dir / f"checkpoint_{next_checkpoint:04d}.json"
            _write_json(path, checkpoint)
            checkpoint_paths.append(str(path))
            print(
                json.dumps(
                    {
                        "checkpoint": next_checkpoint,
                        "accepted": checkpoint["accepted_count"],
                        "dead_letters": checkpoint["dead_letter_count"],
                        "stop_reason": checkpoint["stop_reason"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            next_checkpoint += ROLLING_WINDOW
        stop = phase2_prose_stop_reason(rows)
        if stop:
            return receipts, stop, checkpoint_paths
        blocked = [
            row
            for row in rows
            if row.get("status")
            in {"blocked_missing_cached_artifact", "blocked_unrecognized_status"}
        ]
        if blocked:
            return receipts, str(blocked[0].get("status")), checkpoint_paths
        queued = [row for row in rows if row.get("status") == "queued"]
        running = [row for row in rows if row.get("status") == "running"]
        if not queued:
            if running:
                return receipts, "preexisting_live_running_job", checkpoint_paths
            return receipts, None, checkpoint_paths
        cumulative = await _cumulative_cost(
            db, corpus_id=prepared.receipt["corpus"]["corpus_id"]
        )
        if cumulative["budget_accounting_complete"] is not True:
            return receipts, "cost_telemetry_incomplete", checkpoint_paths
        if Decimal(str(cumulative["ceiling_basis_usd"])) >= absolute_authority:
            return receipts, "cumulative_cost_ceiling_reached", checkpoint_paths
        current_key = await settings_service.get_plaintext_key_any_user(
            DEFAULT_CREDENTIAL_PROVIDER
        )
        if not current_key:
            return (
                receipts,
                "encrypted_provider_credential_unavailable",
                checkpoint_paths,
            )
        if not hmac.compare_digest(_credential_fingerprint(current_key), fingerprint):
            return receipts, "provider_key_rotation_detected", checkpoint_paths

        concurrency = phase2_prose_concurrency(rows)
        until_checkpoint = next_checkpoint - len(terminal)
        candidates = queued[: min(concurrency, until_checkpoint)]
        uncached: list[dict[str, Any]] = []
        for job in candidates:
            planned = by_job_id[str(job.get("job_id") or "")]
            accepted_cache = await _load_certified_acceptance(
                db, planned=planned, config=prepared.config
            )
            if accepted_cache:
                now = datetime.utcnow()
                result = await db[JOB_COLLECTION].update_one(
                    {"job_id": planned.job_id, "status": "queued"},
                    {
                        "$set": {
                            "status": SUCCESS_STATUS,
                            "cache_hit": True,
                            "accepted_cache_key": accepted_cache["cache_key"],
                            "provider_calls": 0,
                            "actual_cost_usd": 0.0,
                            "cost_complete": True,
                            "completed_at": now,
                            "updated_at": now,
                            "lease_until": None,
                        }
                    },
                )
                if int(result.modified_count or 0) != 1:
                    raise PaidPassError("lost queued cache-hit ownership")
                receipts.append(
                    {
                        "ordinal": planned.ordinal,
                        "status": SUCCESS_STATUS,
                        "provider_calls": 0,
                        "actual_cost_usd": 0.0,
                        "cost_complete": True,
                        "gateway_attempts": 0,
                        "cache_hit": True,
                    }
                )
            else:
                uncached.append(job)
        if not uncached:
            continue

        reserved_basis = Decimal(str(cumulative["ceiling_basis_usd"]))
        reserved_jobs: list[dict[str, Any]] = []
        for job in uncached:
            planned = by_job_id[str(job.get("job_id") or "")]
            max_call = worst_case_next_call_cost_usd(
                packet_input_token_upper_bound=planned.packet_bytes,
                max_output_tokens=prepared.config.max_tokens,
                uncached_input_usd=prepared.price_card.uncached_input_usd,
                output_usd=prepared.price_card.output_usd,
                price_unit_tokens=prepared.price_card.price_unit_tokens,
                safety_margin=COST_MARGIN,
            )
            if not cost_reservation_allows_claim(
                current_ceiling_basis_usd=reserved_basis,
                max_call_cost_usd=max_call,
                authorized_ceiling_usd=absolute_authority,
            ):
                break
            reserved_jobs.append(job)
            reserved_basis += max_call
        if not reserved_jobs:
            return (
                receipts,
                "insufficient_reserved_cost_for_next_call",
                checkpoint_paths,
            )
        claimed = await claim_runnable_jobs(
            db,
            collection_name=JOB_COLLECTION,
            jobs=reserved_jobs,
            runnable_statuses={"queued"},
            runner=runner,
            increment_attempt=True,
            max_attempts=1,
            set_fields={"phase": PHASE, "phase_run_id": runner},
        )
        if not claimed:
            continue
        route = SemanticGatewayRoute(
            api_base=prepared.price_card.api_base, api_key=current_key
        )
        results = await asyncio.gather(
            *[
                _run_claimed_job(
                    db,
                    claimed=job,
                    planned=by_job_id[str(job.get("job_id") or "")],
                    config=prepared.config,
                    route=route,
                    provider_price_card=prepared.price_card,
                )
                for job in claimed
            ]
        )
        receipts.extend(sorted(results, key=lambda row: int(row["ordinal"])))


async def run(args: argparse.Namespace) -> dict[str, Any]:
    prepared = await _prepare(args)
    if args.mode == "preflight":
        return prepared.receipt
    required = {
        "authorization_reference": args.authorization_reference,
        "expected_selection_count": args.expected_selection_count,
        "expected_selection_set_hash": args.expected_selection_set_hash,
        "expected_prompt_hash": args.expected_prompt_hash,
        "expected_repair_prompt_hash": args.expected_repair_prompt_hash,
        "expected_schema_hash": args.expected_schema_hash,
        "expected_prior_basis_usd": args.expected_prior_basis_usd,
        "remaining_authority_usd": args.remaining_authority_usd,
    }
    if any(value is None for value in required.values()):
        raise PaidPassError("execute mode requires every exact-GO argument")
    _assert_go_contract(
        prepared,
        authorization_reference=args.authorization_reference,
        expected_selection_count=args.expected_selection_count,
        expected_selection_set_hash=args.expected_selection_set_hash,
        expected_prompt_hash=args.expected_prompt_hash,
        expected_repair_prompt_hash=args.expected_repair_prompt_hash,
        expected_schema_hash=args.expected_schema_hash,
        expected_prior_basis_usd=Decimal(args.expected_prior_basis_usd),
        remaining_authority_usd=Decimal(args.remaining_authority_usd),
    )
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:
            db = client[settings.MONGODB_DATABASE]
        active_ingests = await db["ingest_batches"].count_documents(
            {"status": {"$in": ["queued", "running"]}}
        )
        running_jobs = await db[JOB_COLLECTION].count_documents({"status": "running"})
        if active_ingests or running_jobs:
            raise PaidPassError(
                "Phase-2 prose execution requires zero active ingests and semantic jobs"
            )
        canonical_before = await _canonical_store_census(db=db, settings=settings)
        settings_service.attach(db)
        api_key = await settings_service.get_plaintext_key_any_user(
            DEFAULT_CREDENTIAL_PROVIDER
        )
        if not api_key:
            raise PaidPassError("encrypted LongCat credential is not configured")
        prior_basis = Decimal(
            prepared.receipt["cost_authority"]["prior_cumulative_ceiling_basis_usd"]
        )
        absolute_authority = prior_basis + REMAINING_UMBRELLA_USD
        owner = f"semantic-digest-prose-phase2:{uuid4().hex}"
        async with corpus_lane_lease(
            db,
            corpus_id=prepared.receipt["corpus"]["corpus_id"],
            lane=LANE,
            owner=owner,
            lease_seconds=12 * 60 * 60,
        ) as lease:
            if not lease:
                raise PaidPassError("semantic digest paid-pass lane lease is busy")
            active_ingests = await db["ingest_batches"].count_documents(
                {"status": {"$in": ["queued", "running"]}}
            )
            running_jobs = await db[JOB_COLLECTION].count_documents(
                {"status": "running"}
            )
            if active_ingests or running_jobs:
                raise PaidPassError("operational state changed after Phase-2 lease")
            planned_counts = await _materialize_jobs(
                db,
                corpus_id=prepared.receipt["corpus"]["corpus_id"],
                planned=prepared.selected,
                config=prepared.config,
                parameter_card=prepared.parameter_card,
            )
            await _persist_phase_selection(
                db,
                selected=prepared.selected,
                config=prepared.config,
                phase=PHASE,
                selection_name=SELECTION_NAME,
            )
            receipts, stop_reason, checkpoints = await _execute(
                db,
                prepared=prepared,
                api_key=api_key,
                absolute_authority=absolute_authority,
                canonical_before=canonical_before,
                checkpoint_dir=args.checkpoint_dir,
            )
            rows = await _selection_rows(db, selected=prepared.selected)
            supersession_rows = await _persist_rebuy_supersessions(
                db,
                selected=prepared.selected,
                rebuy_sources=prepared.rebuy_sources,
            )
            canonical_after = await _canonical_store_census(db=db, settings=settings)
            canonical_receipt = _canonical_store_census_receipt(
                canonical_before, canonical_after
            )
            cumulative = await _cumulative_cost(
                db, corpus_id=prepared.receipt["corpus"]["corpus_id"]
            )
            terminal = _terminal_rows_in_completion_order(rows)
            accepted = sum(row.get("status") == SUCCESS_STATUS for row in terminal)
            execution_green = bool(
                len(terminal) == len(prepared.selected)
                and stop_reason is None
                and cumulative["budget_accounting_complete"] is True
                and Decimal(str(cumulative["ceiling_basis_usd"])) <= absolute_authority
                and canonical_receipt["protected_exactly_unchanged"] is True
            )
            return {
                "schema_version": "polymath.semantic_digest_prose_phase2_execution.v1",
                "generated_at": _utc_now(),
                "authorization_reference": AUTHORIZATION_REFERENCE,
                "corpus": prepared.receipt["corpus"],
                "provider_contract": prepared.receipt["provider_contract"],
                "selection": prepared.receipt["selection"],
                "durable_queue": {
                    "collection": JOB_COLLECTION,
                    "planned_counts": planned_counts,
                    "terminal_count": len(terminal),
                    "accepted_count": accepted,
                    "dead_letter_count": sum(
                        row.get("status") in FAILURE_STATUSES for row in terminal
                    ),
                    "provider_call_count": sum(
                        int(row.get("provider_calls") or 0) for row in receipts
                    ),
                    "max_attempts_per_job": 1,
                    "checkpoint_paths": checkpoints,
                },
                "cost_accounting": {
                    **cumulative,
                    "remaining_umbrella_usd": str(REMAINING_UMBRELLA_USD),
                    "absolute_authorized_ceiling_usd": str(absolute_authority),
                },
                "supersession": {
                    "collection": SUPERSESSION_COLLECTION,
                    "expected_rebuy_count": len(REBUY_ORDINALS),
                    "rows_present_or_inserted": supersession_rows,
                    "ledger_append_only": True,
                    "source_cache_payload_preserved": True,
                    "source_cache_serving_eligible": False,
                    "canonical_write": False,
                    "history_preserved": True,
                },
                "canonical_store_census": canonical_receipt,
                "stop_reason": stop_reason,
                "security": {
                    "credential_source": "encrypted settings.api_keys.longcat",
                    "plaintext_credential_in_receipt": False,
                    "packet_text_in_receipt": False,
                    "raw_provider_output_in_receipt": False,
                    "canonical_write": False,
                },
                "execution_green": execution_green,
            }
    finally:
        client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "execute"), required=True)
    parser.add_argument("--corpus-name", default=DEFAULT_CORPUS_NAME)
    parser.add_argument(
        "--expected-parent-count", type=int, default=EXPECTED_PARENT_COUNT
    )
    parser.add_argument(
        "--expected-child-count", type=int, default=EXPECTED_CHILD_COUNT
    )
    parser.add_argument("--max-entities", type=int, default=40)
    parser.add_argument("--authorization-reference")
    parser.add_argument("--expected-selection-count", type=int)
    parser.add_argument("--expected-selection-set-hash")
    parser.add_argument("--expected-prompt-hash")
    parser.add_argument("--expected-repair-prompt-hash")
    parser.add_argument("--expected-schema-hash")
    parser.add_argument("--expected-prior-basis-usd")
    parser.add_argument("--remaining-authority-usd")
    parser.add_argument(
        "--checkpoint-dir", type=Path, default=Path("/tmp/t93_prose_phase2")
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        report = {
            "schema_version": "polymath.semantic_digest_prose_phase2.failure.v1",
            "generated_at": _utc_now(),
            "mode": args.mode,
            "error_class": type(exc).__name__,
            "all_green": False,
        }
        _write_json(args.out, report)
        print(json.dumps(report, sort_keys=True))
        return 1
    _write_json(args.out, report)
    print(json.dumps(report, sort_keys=True))
    green = (
        report.get("all_green")
        if args.mode == "preflight"
        else report.get("execution_green")
    )
    return 0 if green is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
