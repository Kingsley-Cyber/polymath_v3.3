#!/usr/bin/env python3
"""Run the certified, noncanonical T9.3 semantic-digest paid pass.

The pass is deliberately separate from activation. It materializes one
deterministic durable job per eligible parent, claims jobs under the repository
lease contract, skips a structurally and semantically valid certified cache
row, and never retries a terminal or ambiguous provider outcome. Accepted
digests remain in ``semantic_digest_cache`` and failures remain in the gateway
dead-letter lane; neither is projected to canonical stores here.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import ValidationError
from pymongo import UpdateOne


HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from config import get_settings  # noqa: E402
from db.queue_integrity import (  # noqa: E402
    bulk_upsert_durable_jobs,
    ensure_unique_job_id_index,
)
from models.hash_taxonomy import canonical_json_v1, namespace_hash  # noqa: E402
from models.semantic_digest import SemanticDigestV1  # noqa: E402
from models.semantic_validator import semantic_validate  # noqa: E402
from scripts.semantic_gateway_ugo_canary import (  # noqa: E402
    DEFAULT_PROVIDER_PRICE_CARDS,
    DEFAULT_ROUTE_PARAMETER_CARDS,
    CanaryPacket,
    ProviderPriceCard,
    RouteParameterCard,
    _apply_provider_price_fallback,
    _canonical_store_census,
    _canonical_store_census_comparison,
    _canonical_store_census_receipt,
    _failure_receipt,
    _gateway_config,
    _load_provider_price_card,
    _load_route_parameter_card,
    _packet_from_parent,
    _result_receipt,
)
from services.ingestion.job_leases import (  # noqa: E402
    claim_runnable_jobs,
    corpus_lane_lease,
    normalize_failure_class,
)
from services.semantic_gateway import (  # noqa: E402
    LiteLLMProxyTransport,
    MongoSemanticGatewayStore,
    PROMPT_VERSION,
    REPAIR_PROMPT_VERSION,
    SemanticGateway,
    SemanticGatewayConfig,
    SemanticGatewayProvenance,
    SemanticGatewayRoute,
    StructuredGenerationError,
    semantic_digest_cache_key,
    semantic_digest_input_hash,
    semantic_digest_prompt_hash,
    semantic_digest_repair_prompt_hash,
    semantic_digest_schema_hash,
)
from services.settings import settings_service  # noqa: E402


JOB_COLLECTION = "semantic_digest_jobs"
LANE = "semantic_digest_paid_pass"
SUCCESS_STATUS = "succeeded"
FAILURE_STATUSES = {
    "dead_letter",
    "dead_letter_unknown_outcome",
    "blocked_missing_cached_artifact",
    "blocked_unrecognized_status",
}
PURCHASED_TERMINAL_STATUSES = {SUCCESS_STATUS, *FAILURE_STATUSES}
NONPURCHASE_TERMINAL_STATUSES = {
    "cancelled_checkpoint_failed",
    "blocked_prior_terminal_failure",
    "superseded_prompt_contract_unclaimed",
}
TERMINAL_STATUSES = {
    *PURCHASED_TERMINAL_STATUSES,
    *NONPURCHASE_TERMINAL_STATUSES,
}
ROUTE_ID = "longcat-api__longcat-2.0"
DEFAULT_CORPUS_NAME = "markbuildsbrands_transcripts"
DEFAULT_CREDENTIAL_PROVIDER = "longcat"
ESTIMATED_COST_PER_PACKET_USD = 0.04
COST_SAFETY_MULTIPLIER = 1.25
PHASE1_LIMIT = 50
PHASE1_PURCHASED_COUNT = 12
PHASE1B_LIMIT = 10
PHASE1B_MIN_ACCEPTANCE = 0.90
PHASE1B_SELECTION = "mark-phase1b.parent-digest.v6.v1"
PHASE1C_LIMIT = 50
PHASE1C_MIN_ACCEPTANCE = 0.95
PHASE1C_SELECTION = "mark-phase1c.parent-digest.v6.v1"
PHASE2_SELECTION = "mark-phase2.parent-digest.v6.v1"
TAIL_RETRY_LIMIT = 5
TAIL_RETRY_SELECTION = "mark-tail-retry.parent-digest.v6.v1"
PHASE1B_GREEN_FIELD = "phase1b_checkpoint_green"
PHASE1B_CANONICAL_FIELD = "phase1b_checkpoint_canonical"
PHASE1C_GREEN_FIELD = "phase1c_checkpoint_green"
PHASE1C_CANONICAL_FIELD = "phase1c_checkpoint_canonical"
PHASE2_GREEN_FIELD = "phase2_completion_green"
PHASE2_CANONICAL_FIELD = "phase2_completion_canonical"
PRE_PHASE1C_PURCHASED_COUNT = PHASE1_PURCHASED_COUNT + PHASE1B_LIMIT
PHASE1_MIN_ACCEPTANCE = 0.95
PHASE1_MAX_COST_MULTIPLIER = 1.5
PHASE2_ROLLING_WINDOW = 50
PHASE2_MIN_ROLLING_ACCEPTANCE = 0.90
PHASE2_MAX_CONSECUTIVE_DLQ = 5
CANARIED_MAX_PACKET_BYTES = 21_515
UNPRICED_EXPOSURE_BOUND_USD = 0.06
UNPRICED_EXPOSURE_BASIS = "bounded_transport_exposure.v1"
PHASE1C_READ_TIMEOUT_PAUSE_COUNT = 3


class PaidPassError(RuntimeError):
    """A fail-closed operator-visible paid-pass contract error."""


@dataclass(frozen=True)
class PlannedPacket:
    item: CanaryPacket
    ordinal: int
    job_id: str
    cache_key: str
    input_hash: str
    packet_bytes: int


PHASE_SELECTIONS = {
    "phase1b": PHASE1B_SELECTION,
    "phase1c": PHASE1C_SELECTION,
    "phase2": PHASE2_SELECTION,
    "tail-retry": TAIL_RETRY_SELECTION,
}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _deterministic_fresh_selection(
    planned: Sequence[PlannedPacket],
    *,
    excluded_parent_ids: set[str],
    certified_parent_ids: set[str],
    limit: int | None,
) -> list[PlannedPacket]:
    selected = [
        row
        for row in planned
        if row.item.parent_id not in excluded_parent_ids
        and row.item.parent_id not in certified_parent_ids
    ]
    if limit is not None:
        selected = selected[:limit]
        if len(selected) != limit:
            raise PaidPassError(
                f"fresh selection expected {limit} packets, found {len(selected)}"
            )
    if not selected:
        raise PaidPassError("fresh selection is empty")
    return selected


def _resolve_persisted_selection(
    planned: Sequence[PlannedPacket],
    persisted_rows: Sequence[dict[str, Any]],
    *,
    selection_name: str,
    expected_count: int | None,
) -> list[PlannedPacket]:
    by_job_id = {row.job_id: row for row in planned}
    persisted_ids = [str(row.get("job_id") or "") for row in persisted_rows]
    if not persisted_ids or any(not job_id for job_id in persisted_ids):
        raise PaidPassError(
            f"persisted {selection_name} selection is empty or malformed"
        )
    if len(set(persisted_ids)) != len(persisted_ids):
        raise PaidPassError(f"persisted {selection_name} selection contains duplicates")
    missing = [job_id for job_id in persisted_ids if job_id not in by_job_id]
    if missing:
        raise PaidPassError(
            f"persisted {selection_name} selection no longer maps to planned packets"
        )
    selected = sorted(
        (by_job_id[job_id] for job_id in persisted_ids),
        key=lambda row: row.ordinal,
    )
    if expected_count is not None and len(selected) != expected_count:
        raise PaidPassError(
            f"persisted {selection_name} selection expected {expected_count}, "
            f"found {len(selected)}"
        )
    return selected


def _validated_release_canonical(
    rows: Sequence[dict[str, Any]],
    *,
    phase_name: str,
    expected_count: int | None,
    green_field: str,
    canonical_field: str,
) -> dict[str, Any]:
    if expected_count is not None and len(rows) != expected_count:
        raise PaidPassError(
            f"{phase_name} release expected {expected_count} rows, found {len(rows)}"
        )
    if not rows or not all(row.get(green_field) is True for row in rows):
        raise PaidPassError(f"{phase_name} release marker is not green")
    canonicals = [row.get(canonical_field) for row in rows]
    if any(canonical is None for canonical in canonicals):
        raise PaidPassError(f"{phase_name} release canonical checkpoint is missing")
    first = canonical_json_v1(canonicals[0])
    if any(canonical_json_v1(canonical) != first for canonical in canonicals[1:]):
        raise PaidPassError(f"{phase_name} release canonical checkpoint drifted")
    return dict(canonicals[0])


def paid_pass_ceiling_usd(packet_count: int) -> float:
    if packet_count < 1:
        raise PaidPassError("packet count must be positive")
    return round(
        packet_count * ESTIMATED_COST_PER_PACKET_USD * COST_SAFETY_MULTIPLIER,
        8,
    )


def _reevaluation_context(args: argparse.Namespace) -> dict[str, str] | None:
    prior_receipt = getattr(args, "reevaluation_prior_receipt_sha256", None)
    authorization = getattr(args, "reevaluation_authorization", None)
    if prior_receipt is None and authorization is None:
        return None
    if not prior_receipt or not authorization:
        raise PaidPassError(
            "zero-provider re-evaluation requires both prior receipt and "
            "authorization references"
        )
    if args.phase != "phase1c":
        raise PaidPassError("zero-provider re-evaluation is authorized for phase1c")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(prior_receipt)):
        raise PaidPassError("re-evaluation prior receipt SHA-256 is malformed")
    return {
        "mode": "zero_provider_postflight",
        "prior_receipt_sha256": str(prior_receipt),
        "authorization_reference": str(authorization),
    }


def _cost_accounting(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    known_actual = 0.0
    bounded_exposure = 0.0
    unpriced_exposure_count = 0
    actual_cost_complete = True
    budget_accounting_complete = True
    for row in rows:
        value = row.get("actual_cost_usd")
        if (
            row.get("cost_complete") is True
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            known_actual += float(value)
            continue
        actual_cost_complete = False
        bound = row.get("unpriced_exposure_upper_bound_usd")
        if (
            row.get("cost_accounting_basis") == UNPRICED_EXPOSURE_BASIS
            and isinstance(bound, (int, float))
            and not isinstance(bound, bool)
            and 0 < float(bound) <= UNPRICED_EXPOSURE_BOUND_USD
        ):
            bounded_exposure += float(bound)
            unpriced_exposure_count += 1
            continue
        budget_accounting_complete = False
    state = (
        "incomplete"
        if not budget_accounting_complete
        else "complete_with_bounded_exposure"
        if unpriced_exposure_count
        else "complete"
    )
    return {
        "known_actual_cost_usd": known_actual,
        "unpriced_exposure_count": unpriced_exposure_count,
        "bounded_exposure_usd": bounded_exposure,
        "ceiling_basis_usd": known_actual + bounded_exposure,
        "actual_cost_complete": actual_cost_complete,
        "budget_accounting_complete": budget_accounting_complete,
        "cost_accounting_state": state,
    }


def paid_phase_checkpoint(
    rows: Sequence[dict[str, Any]],
    *,
    target_count: int,
    minimum_acceptance: float,
    canonical_before: dict[str, Any],
    canonical_after: dict[str, Any],
) -> dict[str, Any]:
    attempted_rows = [
        row for row in rows if row.get("status") in PURCHASED_TERMINAL_STATUSES
    ]
    accepted = sum(row.get("status") == SUCCESS_STATUS for row in attempted_rows)
    terminal = len(attempted_rows)
    dead_letters = sum(row.get("status") in FAILURE_STATUSES for row in attempted_rows)
    cost_ledger = _cost_accounting(attempted_rows)
    actual_cost = float(cost_ledger["known_actual_cost_usd"])
    denominator = len(attempted_rows)
    acceptance = accepted / denominator if denominator else 0.0
    cost_per_packet = actual_cost / denominator if denominator else None
    ceiling_basis_per_packet = (
        float(cost_ledger["ceiling_basis_usd"]) / denominator if denominator else None
    )
    census_comparison = _canonical_store_census_comparison(
        canonical_before,
        canonical_after,
    )
    canonical_unchanged = bool(census_comparison["protected_exactly_unchanged"])
    size_bands: dict[str, dict[str, Any]] = {}
    for name, predicate in (
        (
            "at_or_below_canaried_max",
            lambda row: int(row.get("packet_bytes") or 0) <= CANARIED_MAX_PACKET_BYTES,
        ),
        (
            "above_canaried_max",
            lambda row: int(row.get("packet_bytes") or 0) > CANARIED_MAX_PACKET_BYTES,
        ),
    ):
        band = [row for row in attempted_rows if predicate(row)]
        band_accepted = sum(row.get("status") == SUCCESS_STATUS for row in band)
        size_bands[name] = {
            "threshold_bytes": CANARIED_MAX_PACKET_BYTES,
            "packet_count": len(band),
            "accepted_count": band_accepted,
            "dead_letter_count": sum(
                row.get("status") in FAILURE_STATUSES for row in band
            ),
            "acceptance": band_accepted / len(band) if band else None,
            "max_packet_bytes": max(
                (int(row.get("packet_bytes") or 0) for row in band),
                default=None,
            ),
        }
    complete = (
        len(rows) == target_count
        and denominator == target_count
        and terminal == target_count
    )
    acceptance_green = acceptance >= minimum_acceptance
    cost_green = (
        cost_ledger["budget_accounting_complete"] is True
        and ceiling_basis_per_packet is not None
        and ceiling_basis_per_packet
        <= ESTIMATED_COST_PER_PACKET_USD * PHASE1_MAX_COST_MULTIPLIER
    )
    return {
        "target_count": target_count,
        "row_count": len(rows),
        "attempted_count": denominator,
        "terminal_count": terminal,
        "accepted_count": accepted,
        "dead_letter_count": dead_letters,
        "acceptance": acceptance,
        "minimum_acceptance": minimum_acceptance,
        "acceptance_green": acceptance_green,
        "actual_cost_usd": actual_cost,
        "cost_complete": cost_ledger["actual_cost_complete"],
        "budget_accounting_complete": cost_ledger["budget_accounting_complete"],
        "cost_accounting_state": cost_ledger["cost_accounting_state"],
        "unpriced_exposure_count": cost_ledger["unpriced_exposure_count"],
        "bounded_exposure_usd": cost_ledger["bounded_exposure_usd"],
        "cost_ceiling_basis_usd": cost_ledger["ceiling_basis_usd"],
        "cost_per_packet_usd": cost_per_packet,
        "cost_ceiling_basis_per_packet_usd": ceiling_basis_per_packet,
        "max_cost_per_packet_usd": (
            ESTIMATED_COST_PER_PACKET_USD * PHASE1_MAX_COST_MULTIPLIER
        ),
        "cost_green": cost_green,
        "canonical_drift_zero": canonical_unchanged,
        "canonical_census_scope_version": census_comparison["scope_version"],
        "canonical_census_scope_recipe_hash": census_comparison["scope_recipe_hash"],
        "canonical_census_scope_valid": census_comparison["scope_valid"],
        "ambient_qdrant_collection_deltas": census_comparison[
            "ambient_qdrant_collection_deltas"
        ],
        "acceptance_by_packet_size_band": size_bands,
        "complete": complete,
        "all_green": bool(
            complete and acceptance_green and cost_green and canonical_unchanged
        ),
    }


def phase1_checkpoint(
    rows: Sequence[dict[str, Any]],
    *,
    canonical_before: dict[str, Any],
    canonical_after: dict[str, Any],
) -> dict[str, Any]:
    return paid_phase_checkpoint(
        rows,
        target_count=PHASE1_LIMIT,
        minimum_acceptance=PHASE1_MIN_ACCEPTANCE,
        canonical_before=canonical_before,
        canonical_after=canonical_after,
    )


def phase2_auto_stop_reason(
    rows: Sequence[dict[str, Any]],
    *,
    cumulative_cost_usd: float,
    cost_ceiling_usd: float,
) -> str | None:
    if cumulative_cost_usd >= cost_ceiling_usd:
        return "cumulative_cost_ceiling_reached"
    terminal_rows = [
        row for row in rows if row.get("status") in PURCHASED_TERMINAL_STATUSES
    ]
    if _cost_accounting(terminal_rows)["budget_accounting_complete"] is not True:
        return "cost_telemetry_incomplete"
    if len(terminal_rows) >= PHASE2_ROLLING_WINDOW:
        window = terminal_rows[-PHASE2_ROLLING_WINDOW:]
        accepted = sum(row.get("status") == SUCCESS_STATUS for row in window)
        if accepted / PHASE2_ROLLING_WINDOW < PHASE2_MIN_ROLLING_ACCEPTANCE:
            return "rolling_acceptance_below_90_percent"
    consecutive_dlq = 0
    for row in reversed(terminal_rows):
        if row.get("status") not in FAILURE_STATUSES:
            break
        consecutive_dlq += 1
    if consecutive_dlq >= PHASE2_MAX_CONSECUTIVE_DLQ:
        return "five_consecutive_terminal_dlqs"
    return None


def _credential_fingerprint(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _job_id(*, corpus_id: str, parent_id: str, cache_key: str) -> str:
    return namespace_hash(
        "work",
        {
            "artifact_type": "semantic_digest_job",
            "corpus_id": corpus_id,
            "parent_id": parent_id,
            "cache_key": cache_key,
        },
    )


def _tail_retry_job_id(*, corpus_id: str, parent_id: str, cache_key: str) -> str:
    return namespace_hash(
        "work",
        {
            "artifact_type": "semantic_digest_tail_retry_job",
            "authorization": TAIL_RETRY_SELECTION,
            "corpus_id": corpus_id,
            "parent_id": parent_id,
            "cache_key": cache_key,
        },
    )


def _as_tail_retry_packet(row: PlannedPacket, *, corpus_id: str) -> PlannedPacket:
    return replace(
        row,
        job_id=_tail_retry_job_id(
            corpus_id=corpus_id,
            parent_id=row.item.parent_id,
            cache_key=row.cache_key,
        ),
    )


def _cache_identity(
    item: CanaryPacket,
    config: SemanticGatewayConfig,
) -> tuple[str, str]:
    input_hash = semantic_digest_input_hash(item.packet)
    cache_key = semantic_digest_cache_key(
        input_hash=input_hash,
        model_id=config.model_id,
        schema_hash=semantic_digest_schema_hash(),
        prompt_hash=semantic_digest_prompt_hash(
            config.prompt_version,
            config.repair_prompt_version,
        ),
        runtime_version=config.runtime_version,
    )
    return input_hash, cache_key


def _valid_cached_row(
    row: dict[str, Any] | None,
    *,
    item: CanaryPacket,
    config: SemanticGatewayConfig,
    cache_key: str | None = None,
) -> bool:
    if not row or row.get("status") != "accepted_cache":
        return False
    if row.get("canonical_write") is not False:
        return False
    try:
        digest = SemanticDigestV1.model_validate(row.get("digest"))
        provenance = SemanticGatewayProvenance.model_validate(row.get("provenance"))
    except (TypeError, ValueError, ValidationError):
        return False
    expected_input_hash = semantic_digest_input_hash(item.packet)
    try:
        expected_repair_hash = semantic_digest_repair_prompt_hash(
            provenance.repair_prompt_version
        )
        expected_prompt_hash = semantic_digest_prompt_hash(
            provenance.prompt_version,
            provenance.repair_prompt_version,
        )
        expected_cache_key = semantic_digest_cache_key(
            input_hash=expected_input_hash,
            model_id=provenance.model_id,
            schema_hash=semantic_digest_schema_hash(),
            prompt_hash=expected_prompt_hash,
            runtime_version=provenance.runtime_version,
        )
    except (TypeError, ValueError):
        return False
    return bool(
        (cache_key is None or provenance.cache_key == cache_key)
        and provenance.cache_key == expected_cache_key
        and (row.get("_id") is None or row.get("_id") == provenance.cache_key)
        and provenance.input_hash == expected_input_hash
        and provenance.model_id == config.model_id
        and provenance.runtime_version == config.runtime_version
        and provenance.schema_hash == semantic_digest_schema_hash()
        and provenance.prompt_hash == expected_prompt_hash
        and provenance.repair_prompt_hash == expected_repair_hash
        and provenance.capability_tier == "tier3"
        and ROUTE_ID in provenance.capability_detection
        and not semantic_validate(digest, item.context)
    )


async def _load_valid_cache(
    db: Any,
    *,
    planned: PlannedPacket,
    config: SemanticGatewayConfig,
) -> bool:
    row = await db["semantic_digest_cache"].find_one({"_id": planned.cache_key})
    return _valid_cached_row(
        row,
        item=planned.item,
        config=config,
        cache_key=planned.cache_key,
    )


async def _load_certified_acceptance(
    db: Any,
    *,
    planned: PlannedPacket,
    config: SemanticGatewayConfig,
) -> dict[str, str] | None:
    rows = (
        await db["semantic_digest_cache"]
        .find(
            {
                "status": "accepted_cache",
                "canonical_write": False,
                "digest.parent_id": planned.item.parent_id,
                "provenance.input_hash": planned.input_hash,
                "provenance.model_id": config.model_id,
                "provenance.runtime_version": config.runtime_version,
                "provenance.schema_hash": semantic_digest_schema_hash(),
                "provenance.capability_tier": "tier3",
            }
        )
        .limit(20)
        .to_list(length=20)
    )
    for row in rows:
        if not _valid_cached_row(
            row,
            item=planned.item,
            config=config,
            cache_key=None,
        ):
            continue
        provenance = SemanticGatewayProvenance.model_validate(row["provenance"])
        return {
            "cache_key": provenance.cache_key,
            "prompt_version": provenance.prompt_version,
            "repair_prompt_version": provenance.repair_prompt_version,
        }
    return None


async def _discover_all_packets(
    db: Any,
    *,
    corpus_name: str,
    max_entities: int,
) -> tuple[str, int, list[CanaryPacket]]:
    corpora = (
        await db["corpora"]
        .find(
            {"name": corpus_name, "status": {"$ne": "deleted"}},
            {"_id": 0, "corpus_id": 1, "name": 1},
        )
        .to_list(length=3)
    )
    if len(corpora) != 1:
        raise PaidPassError(
            f"expected exactly one active corpus named {corpus_name!r}; "
            f"found {len(corpora)}"
        )
    corpus_id = str(corpora[0].get("corpus_id") or "")
    document_count = await db["documents"].count_documents({"corpus_id": corpus_id})
    parents = (
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
                "source_hash": 1,
                "child_ids": 1,
                "validation_status": 1,
            },
        )
        .sort("parent_id", 1)
        .to_list(length=None)
    )
    child_ids = sorted(
        {
            str(child_id)
            for parent in parents
            for child_id in (parent.get("child_ids") or [])
            if child_id
        }
    )
    extraction_rows = (
        await db["ghost_b_extractions"]
        .find(
            {
                "corpus_id": corpus_id,
                "chunk_id": {"$in": child_ids},
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
    for parent in parents:
        rows = [
            by_child[str(child_id)]
            for child_id in parent.get("child_ids") or []
            if str(child_id) in by_child
        ]
        packets.append(
            _packet_from_parent(
                corpus_id=corpus_id,
                corpus_name=corpus_name,
                parent=parent,
                extraction_rows=rows,
                max_entities=max_entities,
            )
        )
    return corpus_id, int(document_count), packets


def _plan_packets(
    *,
    corpus_id: str,
    packets: Sequence[CanaryPacket],
    config: SemanticGatewayConfig,
) -> list[PlannedPacket]:
    planned: list[PlannedPacket] = []
    for ordinal, item in enumerate(packets):
        input_hash, cache_key = _cache_identity(item, config)
        planned.append(
            PlannedPacket(
                item=item,
                ordinal=ordinal,
                job_id=_job_id(
                    corpus_id=corpus_id,
                    parent_id=item.parent_id,
                    cache_key=cache_key,
                ),
                cache_key=cache_key,
                input_hash=input_hash,
                packet_bytes=len(canonical_json_v1(item.packet).encode("utf-8")),
            )
        )
    return planned


async def _materialize_jobs(
    db: Any,
    *,
    corpus_id: str,
    planned: Sequence[PlannedPacket],
    config: SemanticGatewayConfig,
    parameter_card: RouteParameterCard,
) -> dict[str, int]:
    await ensure_unique_job_id_index(db[JOB_COLLECTION], collection_name=JOB_COLLECTION)
    job_ids = [row.job_id for row in planned]
    existing_rows = (
        await db[JOB_COLLECTION]
        .find({"job_id": {"$in": job_ids}}, {"_id": 0})
        .to_list(length=len(job_ids))
    )
    existing = {str(row.get("job_id") or ""): row for row in existing_rows}
    now = datetime.utcnow()
    ops: list[UpdateOne] = []
    counts: dict[str, int] = {}
    for row in planned:
        prior = existing.get(row.job_id)
        prior_status = str((prior or {}).get("status") or "")
        accepted_cache = await _load_certified_acceptance(
            db, planned=row, config=config
        )
        if accepted_cache:
            status = SUCCESS_STATUS
            status_fields: dict[str, Any] = {
                "cache_hit": True,
                "accepted_cache_key": accepted_cache["cache_key"],
                "accepted_prompt_version": accepted_cache["prompt_version"],
                "accepted_repair_prompt_version": accepted_cache[
                    "repair_prompt_version"
                ],
                "actual_cost_usd": float((prior or {}).get("actual_cost_usd") or 0.0),
                "cost_complete": True,
                "lease_until": None,
                "completed_at": (prior or {}).get("completed_at") or now,
            }
        elif prior_status == SUCCESS_STATUS:
            status = "blocked_missing_cached_artifact"
            status_fields = {
                "cost_complete": bool((prior or {}).get("cost_complete")),
                "actual_cost_usd": float((prior or {}).get("actual_cost_usd") or 0.0),
                "lease_until": None,
                "failure_class": "missing_certified_cache_after_success",
            }
        elif prior_status == "running":
            lease_until = (prior or {}).get("lease_until")
            if isinstance(lease_until, datetime) and lease_until > now:
                status = "running"
                status_fields = {}
            else:
                status = "dead_letter_unknown_outcome"
                status_fields = {
                    "cost_complete": False,
                    "actual_cost_usd": float(
                        (prior or {}).get("actual_cost_usd") or 0.0
                    ),
                    "lease_until": None,
                    "failure_class": "expired_lease_unknown_provider_outcome",
                    "dead_lettered_at": now,
                }
        elif prior_status in TERMINAL_STATUSES:
            status = prior_status
            status_fields = {}
        elif prior_status in {"", "queued"}:
            status = "queued"
            status_fields = {"lease_until": None}
        else:
            status = "blocked_unrecognized_status"
            status_fields = {
                "cost_complete": False,
                "actual_cost_usd": float((prior or {}).get("actual_cost_usd") or 0.0),
                "lease_until": None,
                "failure_class": "unrecognized_durable_job_status",
            }
        counts[status] = counts.get(status, 0) + 1
        ops.append(
            UpdateOne(
                {"job_id": row.job_id},
                {
                    "$set": {
                        "job_id": row.job_id,
                        "corpus_id": corpus_id,
                        "doc_id": row.item.doc_id,
                        "parent_id": row.item.parent_id,
                        "ordinal": row.ordinal,
                        "status": status,
                        "cache_key": row.cache_key,
                        "input_hash": row.input_hash,
                        "packet_bytes": row.packet_bytes,
                        "model_id": config.model_id,
                        "runtime_version": config.runtime_version,
                        "schema_hash": semantic_digest_schema_hash(),
                        "prompt_version": config.prompt_version,
                        "prompt_hash": semantic_digest_prompt_hash(
                            config.prompt_version,
                            config.repair_prompt_version,
                        ),
                        "repair_prompt_version": config.repair_prompt_version,
                        "repair_prompt_hash": semantic_digest_repair_prompt_hash(
                            config.repair_prompt_version
                        ),
                        "capability_tier": "tier3",
                        "route_id": ROUTE_ID,
                        "parameter_version": parameter_card.parameter_version,
                        "canonical_write": False,
                        "last_planned_at": now,
                        "updated_at": now,
                        **status_fields,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                        "attempt_count": 0,
                    },
                    **(
                        {"$unset": {"runner": "", "started_at": ""}}
                        if status in TERMINAL_STATUSES
                        else {}
                    ),
                },
                upsert=True,
            )
        )
    await bulk_upsert_durable_jobs(db[JOB_COLLECTION], ops)
    return counts


async def _mark_cached_success(
    db: Any,
    *,
    planned: PlannedPacket,
    phase: str,
    accepted_cache: dict[str, str],
) -> None:
    now = datetime.utcnow()
    await db[JOB_COLLECTION].update_one(
        {"job_id": planned.job_id, "status": "queued"},
        {
            "$set": {
                "status": SUCCESS_STATUS,
                "phase": phase,
                "cache_hit": True,
                "accepted_cache_key": accepted_cache["cache_key"],
                "accepted_prompt_version": accepted_cache["prompt_version"],
                "accepted_repair_prompt_version": accepted_cache[
                    "repair_prompt_version"
                ],
                "provider_calls": 0,
                "actual_cost_usd": 0.0,
                "cost_complete": True,
                "completed_at": now,
                "updated_at": now,
                "lease_until": None,
            },
            "$unset": {"runner": "", "started_at": ""},
        },
    )


async def _persist_terminal_job(
    db: Any,
    *,
    claimed: dict[str, Any],
    status: str,
    fields: dict[str, Any],
) -> None:
    now = datetime.utcnow()
    result = await db[JOB_COLLECTION].update_one(
        {
            "job_id": claimed["job_id"],
            "status": "running",
            "runner": claimed.get("runner"),
        },
        {
            "$set": {
                "status": status,
                "canonical_write": False,
                "completed_at": now,
                "updated_at": now,
                "lease_until": None,
                **fields,
            },
            "$unset": {"runner": "", "started_at": ""},
        },
    )
    if int(getattr(result, "modified_count", 0) or 0) != 1:
        raise PaidPassError("lost durable job ownership before terminal write")


async def _certified_parent_ids(
    db: Any,
    *,
    planned: Sequence[PlannedPacket],
    config: SemanticGatewayConfig,
) -> set[str]:
    by_parent_id = {row.item.parent_id: row for row in planned}
    rows = (
        await db["semantic_digest_cache"]
        .find(
            {
                "status": "accepted_cache",
                "canonical_write": False,
                "digest.parent_id": {"$in": sorted(by_parent_id)},
                "provenance.model_id": config.model_id,
                "provenance.runtime_version": config.runtime_version,
                "provenance.schema_hash": semantic_digest_schema_hash(),
                "provenance.capability_tier": "tier3",
            }
        )
        .to_list(length=None)
    )
    accepted: set[str] = set()
    for row in rows:
        parent_id = str(((row.get("digest") or {}).get("parent_id")) or "")
        planned_row = by_parent_id.get(parent_id)
        if planned_row and _valid_cached_row(
            row,
            item=planned_row.item,
            config=config,
            cache_key=None,
        ):
            accepted.add(parent_id)
    return accepted


async def _select_fresh_phase_packets(
    db: Any,
    *,
    corpus_id: str,
    planned: Sequence[PlannedPacket],
    config: SemanticGatewayConfig,
    selection_name: str,
    limit: int | None,
    expected_excluded_count: int | None = None,
) -> list[PlannedPacket]:
    prior_selection = (
        await db[JOB_COLLECTION]
        .find(
            {
                "corpus_id": corpus_id,
                "phase_selection": selection_name,
                "prompt_version": config.prompt_version,
                "repair_prompt_version": config.repair_prompt_version,
            },
            {"_id": 0, "job_id": 1},
        )
        .sort("ordinal", 1)
        .to_list(length=None)
    )
    if prior_selection:
        return _resolve_persisted_selection(
            planned,
            prior_selection,
            selection_name=selection_name,
            expected_count=limit,
        )

    running = await db[JOB_COLLECTION].count_documents(
        {"corpus_id": corpus_id, "status": "running"}
    )
    if running:
        raise PaidPassError(
            f"cannot select {selection_name} while semantic jobs are running"
        )
    attempted_parent_ids = set(
        await db[JOB_COLLECTION].distinct(
            "parent_id",
            {
                "corpus_id": corpus_id,
                "$or": [
                    {"attempt_count": {"$gt": 0}},
                    {"status": {"$in": sorted(PURCHASED_TERMINAL_STATUSES)}},
                ],
            },
        )
    )
    certified_parent_ids = await _certified_parent_ids(
        db,
        planned=planned,
        config=config,
    )
    excluded_count = len(attempted_parent_ids | certified_parent_ids)
    if (
        expected_excluded_count is not None
        and excluded_count != expected_excluded_count
    ):
        raise PaidPassError(
            f"{selection_name} exclusion ledger drifted: expected "
            f"{expected_excluded_count}, found {excluded_count}"
        )
    return _deterministic_fresh_selection(
        planned,
        excluded_parent_ids=attempted_parent_ids,
        certified_parent_ids=certified_parent_ids,
        limit=limit,
    )


async def _persist_phase_selection(
    db: Any,
    *,
    selected: Sequence[PlannedPacket],
    config: SemanticGatewayConfig,
    phase: str,
    selection_name: str,
) -> None:
    result = await db[JOB_COLLECTION].update_many(
        {
            "job_id": {"$in": [row.job_id for row in selected]},
            "prompt_version": config.prompt_version,
            "repair_prompt_version": config.repair_prompt_version,
        },
        {
            "$set": {
                "phase_selection": selection_name,
                "phase": phase,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    if int(getattr(result, "matched_count", 0) or 0) != len(selected):
        raise PaidPassError(
            f"{phase} durable selection did not match {len(selected)} jobs"
        )


async def _persist_phase_release(
    db: Any,
    *,
    selected: Sequence[PlannedPacket],
    selection_name: str,
    green_field: str,
    canonical_field: str,
    canonical: dict[str, Any],
) -> None:
    now = datetime.utcnow()
    result = await db[JOB_COLLECTION].update_many(
        {
            "job_id": {"$in": [row.job_id for row in selected]},
            "phase_selection": selection_name,
        },
        {
            "$set": {
                green_field: True,
                canonical_field: canonical,
                f"{green_field}_at": now,
                "updated_at": now,
            }
        },
    )
    if int(getattr(result, "matched_count", 0) or 0) != len(selected):
        raise PaidPassError(
            f"{selection_name} release did not match {len(selected)} jobs"
        )


async def _require_phase_release(
    db: Any,
    *,
    corpus_id: str,
    selection_name: str,
    expected_count: int | None,
    phase_name: str,
    green_field: str,
    canonical_field: str,
) -> dict[str, Any]:
    rows = (
        await db[JOB_COLLECTION]
        .find(
            {
                "corpus_id": corpus_id,
                "route_id": ROUTE_ID,
                "phase_selection": selection_name,
                "prompt_version": PROMPT_VERSION,
                "repair_prompt_version": REPAIR_PROMPT_VERSION,
            },
            {"_id": 0, green_field: 1, canonical_field: 1},
        )
        .sort("ordinal", 1)
        .to_list(length=None)
    )
    return _validated_release_canonical(
        rows,
        phase_name=phase_name,
        expected_count=expected_count,
        green_field=green_field,
        canonical_field=canonical_field,
    )


async def _corpus_certified_acceptance(
    db: Any,
    *,
    planned: Sequence[PlannedPacket],
    config: SemanticGatewayConfig,
) -> dict[str, Any]:
    accepted_parent_ids = await _certified_parent_ids(
        db,
        planned=planned,
        config=config,
    )
    required = math.ceil(len(planned) * PHASE1_MIN_ACCEPTANCE)
    return {
        "eligible_count": len(planned),
        "certified_accepted_count": len(accepted_parent_ids),
        "minimum_accepted_count": required,
        "acceptance": len(accepted_parent_ids) / len(planned) if planned else 0.0,
        "green": len(accepted_parent_ids) >= required,
    }


def _phase1_tail_failure_query(corpus_id: str) -> dict[str, Any]:
    return {
        "corpus_id": corpus_id,
        "route_id": ROUTE_ID,
        "phase": "phase1",
        "status": {"$in": sorted(FAILURE_STATUSES)},
        "attempt_count": {"$gt": 0},
    }


def _phase1c_timeout_tail_query(corpus_id: str) -> dict[str, Any]:
    return {
        "corpus_id": corpus_id,
        "route_id": ROUTE_ID,
        "phase_selection": PHASE1C_SELECTION,
        "status": {"$in": sorted(FAILURE_STATUSES)},
        "transport_error_class": "ReadTimeout",
        "attempt_count": {"$gt": 0},
    }


async def _select_tail_retry_packets(
    db: Any,
    *,
    corpus_id: str,
    planned: Sequence[PlannedPacket],
    config: SemanticGatewayConfig,
) -> list[PlannedPacket]:
    tail_planned = [_as_tail_retry_packet(row, corpus_id=corpus_id) for row in planned]
    prior_selection = (
        await db[JOB_COLLECTION]
        .find(
            {
                "corpus_id": corpus_id,
                "phase_selection": TAIL_RETRY_SELECTION,
                "prompt_version": config.prompt_version,
                "repair_prompt_version": config.repair_prompt_version,
            },
            {"_id": 0, "job_id": 1},
        )
        .sort("ordinal", 1)
        .to_list(length=None)
    )
    if prior_selection:
        return _resolve_persisted_selection(
            tail_planned,
            prior_selection,
            selection_name=TAIL_RETRY_SELECTION,
            expected_count=TAIL_RETRY_LIMIT,
        )

    running = await db[JOB_COLLECTION].count_documents(
        {"corpus_id": corpus_id, "status": "running"}
    )
    if running:
        raise PaidPassError("cannot select tail retry while semantic jobs are running")
    old_failures = (
        await db[JOB_COLLECTION]
        .find(
            _phase1_tail_failure_query(corpus_id),
            {"_id": 0, "parent_id": 1},
        )
        .sort("ordinal", 1)
        .to_list(length=TAIL_RETRY_LIMIT + 1)
    )
    # The frozen Phase-1 jobs predate per-job prompt-version fields. Their
    # phase/run/status/attempt ledger plus the exact-four uniqueness check is
    # the durable identity; the Phase-1 receipt supplies the v5/v2 contract.
    timeout_failures = (
        await db[JOB_COLLECTION]
        .find(
            _phase1c_timeout_tail_query(corpus_id),
            {"_id": 0, "parent_id": 1},
        )
        .sort("ordinal", 1)
        .to_list(length=PHASE1C_READ_TIMEOUT_PAUSE_COUNT)
    )
    parent_ids = [
        str(row.get("parent_id") or "") for row in [*old_failures, *timeout_failures]
    ]
    if (
        len(parent_ids) != TAIL_RETRY_LIMIT
        or any(not parent_id for parent_id in parent_ids)
        or len(set(parent_ids)) != TAIL_RETRY_LIMIT
    ):
        raise PaidPassError(f"tail retry expected {TAIL_RETRY_LIMIT} unique ruled DLQs")
    by_parent_id = {row.item.parent_id: row for row in tail_planned}
    try:
        selected = [by_parent_id[parent_id] for parent_id in parent_ids]
    except KeyError as exc:
        raise PaidPassError(
            "tail retry DLQ no longer maps to a planned packet"
        ) from exc
    certified_parent_ids = await _certified_parent_ids(
        db,
        planned=selected,
        config=config,
    )
    if certified_parent_ids:
        raise PaidPassError("tail retry contains an already certified acceptance")
    prior_current_attempt = await db[JOB_COLLECTION].count_documents(
        {
            "job_id": {"$in": [row.job_id for row in selected]},
            "$or": [
                {"attempt_count": {"$gt": 0}},
                {"status": {"$in": sorted(PURCHASED_TERMINAL_STATUSES)}},
            ],
        }
    )
    if prior_current_attempt:
        raise PaidPassError("tail retry v6 job was already attempted or purchased")
    return sorted(selected, key=lambda row: row.ordinal)


async def _run_claimed_job(
    db: Any,
    *,
    claimed: dict[str, Any],
    planned: PlannedPacket,
    config: SemanticGatewayConfig,
    route: SemanticGatewayRoute,
    provider_price_card: ProviderPriceCard,
) -> dict[str, Any]:
    transport = LiteLLMProxyTransport()
    try:
        result = await SemanticGateway(
            transport=transport,
            store=MongoSemanticGatewayStore(db),
        ).generate(
            packet=planned.item.packet,
            context=planned.item.context,
            config=config,
            route=route,
        )
        telemetry = _apply_provider_price_fallback(
            transport.call_telemetry, provider_price_card
        )
        receipt = _result_receipt(
            planned.item,
            result,
            fault_injected=False,
            provider_telemetry=telemetry,
        )
        if result.cache_hit:
            receipt.update(
                {
                    "provider_calls": 0,
                    "actual_cost_usd": 0.0,
                    "cost_complete": True,
                    "call_costs_usd": [],
                    "call_cost_sources": [],
                }
            )
        await _persist_terminal_job(
            db,
            claimed=claimed,
            status=SUCCESS_STATUS,
            fields={
                "cache_hit": bool(result.cache_hit),
                "output_hash": result.provenance.output_hash,
                "gateway_attempts": result.provenance.attempts,
                "repair_attempted": result.provenance.repair_attempted,
                "provider_calls": receipt["provider_calls"],
                "usage": receipt["usage"],
                "actual_cost_usd": receipt["actual_cost_usd"],
                "cost_complete": receipt["cost_complete"],
                "provenance_complete": receipt["provenance_complete"],
                "semantic_replay_green": not receipt["semantic_validation_errors"],
            },
        )
        return {
            "ordinal": planned.ordinal,
            "status": SUCCESS_STATUS,
            "provider_calls": receipt["provider_calls"],
            "actual_cost_usd": receipt["actual_cost_usd"],
            "cost_complete": receipt["cost_complete"],
            "gateway_attempts": result.provenance.attempts,
            "cache_hit": bool(result.cache_hit),
        }
    except StructuredGenerationError as exc:
        telemetry = _apply_provider_price_fallback(
            transport.call_telemetry, provider_price_card
        )
        receipt = _failure_receipt(
            planned.item,
            exc,
            fault_injected=False,
            provider_telemetry=telemetry,
            requested_tier="tier3",
        )
        first_error = (receipt.get("validation_errors") or ["structured_generation"])[0]
        failure_class = normalize_failure_class(first_error)
        transport_error_class = (
            type(exc.__cause__).__name__
            if first_error.startswith("transport.attempt[")
            and exc.__cause__ is not None
            else None
        )
        bounded_transport = bool(
            receipt["cost_complete"] is not True
            and first_error.startswith("transport.attempt[")
            and transport_error_class
        )
        await _persist_terminal_job(
            db,
            claimed=claimed,
            status="dead_letter",
            fields={
                "dead_letter_id": exc.dead_letter_id,
                "gateway_attempts": exc.attempts,
                "repair_attempted": exc.attempts == 2,
                "provider_calls": receipt["provider_calls"],
                "usage": receipt["usage"],
                "actual_cost_usd": receipt["actual_cost_usd"],
                "cost_complete": receipt["cost_complete"],
                "failure_class": failure_class,
                **(
                    {
                        "transport_error_class": transport_error_class,
                        "unpriced_exposure_upper_bound_usd": (
                            UNPRICED_EXPOSURE_BOUND_USD
                        ),
                        "cost_accounting_basis": UNPRICED_EXPOSURE_BASIS,
                    }
                    if bounded_transport
                    else {}
                ),
                "validation_error_count": len(receipt.get("validation_errors") or []),
                "dead_lettered_at": datetime.utcnow(),
            },
        )
        return {
            "ordinal": planned.ordinal,
            "status": "dead_letter",
            "provider_calls": receipt["provider_calls"],
            "actual_cost_usd": receipt["actual_cost_usd"],
            "cost_complete": receipt["cost_complete"],
            "unpriced_exposure_upper_bound_usd": (
                UNPRICED_EXPOSURE_BOUND_USD if bounded_transport else None
            ),
            "cost_accounting_basis": (
                UNPRICED_EXPOSURE_BASIS if bounded_transport else None
            ),
            "transport_error_class": transport_error_class,
            "gateway_attempts": exc.attempts,
            "cache_hit": False,
        }
    except Exception as exc:
        telemetry = _apply_provider_price_fallback(
            transport.call_telemetry, provider_price_card
        )
        costs = [row.get("actual_cost_usd") for row in telemetry]
        cost_complete = bool(costs) and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in costs
        )
        actual_cost = sum(float(value) for value in costs) if cost_complete else None
        await _persist_terminal_job(
            db,
            claimed=claimed,
            status="dead_letter_unknown_outcome",
            fields={
                "provider_calls": len(telemetry),
                "actual_cost_usd": actual_cost,
                "cost_complete": cost_complete,
                "failure_class": normalize_failure_class(type(exc).__name__),
                "dead_lettered_at": datetime.utcnow(),
            },
        )
        return {
            "ordinal": planned.ordinal,
            "status": "dead_letter_unknown_outcome",
            "provider_calls": len(telemetry),
            "actual_cost_usd": actual_cost,
            "cost_complete": cost_complete,
            "gateway_attempts": None,
            "cache_hit": False,
        }


async def _phase_rows(
    db: Any,
    *,
    corpus_id: str,
    phase: str,
) -> list[dict[str, Any]]:
    selection_name = PHASE_SELECTIONS.get(phase)
    if selection_name:
        query: dict[str, Any] = {
            "corpus_id": corpus_id,
            "route_id": ROUTE_ID,
            "phase_selection": selection_name,
            "prompt_version": PROMPT_VERSION,
            "repair_prompt_version": REPAIR_PROMPT_VERSION,
        }
    elif phase == "phase1":
        query = {
            "corpus_id": corpus_id,
            "route_id": ROUTE_ID,
            "ordinal": {"$lt": PHASE1_LIMIT},
            "prompt_version": "parent-digest.v5",
            "repair_prompt_version": "parent-digest-repair.v2",
        }
    else:
        raise PaidPassError(f"unrecognized paid-pass phase {phase!r}")
    return (
        await db[JOB_COLLECTION]
        .find(
            query,
            {"_id": 0},
        )
        .sort("ordinal", 1)
        .to_list(length=None)
    )


def _read_timeout_count(rows: Sequence[dict[str, Any]]) -> int:
    return sum(row.get("transport_error_class") == "ReadTimeout" for row in rows)


async def _book_transport_exposure_bounds(
    db: Any,
    *,
    corpus_id: str,
    phase: str,
) -> int:
    rows = await _phase_rows(db, corpus_id=corpus_id, phase=phase)
    booked = 0
    for row in rows:
        if (
            row.get("status") not in FAILURE_STATUSES
            or row.get("cost_complete") is True
            or not str(row.get("failure_class") or "").startswith("transport_")
        ):
            continue
        bound = row.get("unpriced_exposure_upper_bound_usd")
        basis = row.get("cost_accounting_basis")
        transport_error_class = row.get("transport_error_class")
        if transport_error_class is None and row.get("dead_letter_id"):
            dead_letter = await db["semantic_digest_dead_letters"].find_one(
                {"_id": row["dead_letter_id"]},
                {"_id": 0, "validation_errors": 1},
            )
            error = str(((dead_letter or {}).get("validation_errors") or [""])[0])
            if error.startswith("transport.attempt[") and ":" in error:
                transport_error_class = error.rsplit(":", 1)[-1].strip() or None
        if bound is not None or basis is not None:
            if (
                bound != UNPRICED_EXPOSURE_BOUND_USD
                or basis != UNPRICED_EXPOSURE_BASIS
                or not transport_error_class
            ):
                raise PaidPassError("transport exposure booking drifted")
            continue
        if not transport_error_class:
            raise PaidPassError("transport exposure lacks a bounded error class")
        result = await db[JOB_COLLECTION].update_one(
            {
                "job_id": row["job_id"],
                "status": row["status"],
                "cost_complete": False,
            },
            {
                "$set": {
                    "transport_error_class": transport_error_class,
                    "unpriced_exposure_upper_bound_usd": (UNPRICED_EXPOSURE_BOUND_USD),
                    "cost_accounting_basis": UNPRICED_EXPOSURE_BASIS,
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        if int(getattr(result, "modified_count", 0) or 0) != 1:
            raise PaidPassError("transport exposure booking lost durable identity")
        booked += 1
    return booked


async def _cumulative_cost(db: Any, *, corpus_id: str) -> dict[str, Any]:
    rows = (
        await db[JOB_COLLECTION]
        .find(
            {
                "corpus_id": corpus_id,
                "route_id": ROUTE_ID,
                "status": {"$in": list(PURCHASED_TERMINAL_STATUSES)},
            },
            {
                "_id": 0,
                "actual_cost_usd": 1,
                "cost_complete": 1,
                "unpriced_exposure_upper_bound_usd": 1,
                "cost_accounting_basis": 1,
            },
        )
        .to_list(length=None)
    )
    return _cost_accounting(rows)


async def _execute_phase(
    db: Any,
    *,
    phase: str,
    corpus_id: str,
    planned: Sequence[PlannedPacket],
    config: SemanticGatewayConfig,
    provider_price_card: ProviderPriceCard,
    credential_provider: str,
    initial_api_key: str,
    concurrency: int,
    cost_ceiling_usd: float,
) -> tuple[list[dict[str, Any]], str | None]:
    if phase == "phase1":
        phase_planned = [row for row in planned if row.ordinal < PHASE1_LIMIT]
    else:
        phase_planned = list(planned)
    by_job_id = {row.job_id: row for row in phase_planned}
    expected_by_phase = {
        "phase1": PHASE1_LIMIT,
        "phase1b": PHASE1B_LIMIT,
        "phase1c": PHASE1C_LIMIT,
        "tail-retry": TAIL_RETRY_LIMIT,
    }
    expected = expected_by_phase.get(phase, len(phase_planned))
    if len(phase_planned) != expected:
        raise PaidPassError("phase packet partition is not exact")
    runner = f"semantic-digest-paid-pass:{phase}:{uuid4().hex}"
    initial_fingerprint = _credential_fingerprint(initial_api_key)
    safe_receipts: list[dict[str, Any]] = []

    while True:
        rows = await _phase_rows(db, corpus_id=corpus_id, phase=phase)
        if len(rows) != expected:
            raise PaidPassError(
                f"durable {phase} row count drifted: expected {expected}, found {len(rows)}"
            )
        if (
            phase == "phase1c"
            and _read_timeout_count(rows) >= PHASE1C_READ_TIMEOUT_PAUSE_COUNT
        ):
            return safe_receipts, "read_timeout_recurrence_pause"
        blocked = [
            row
            for row in rows
            if row.get("status")
            in {"blocked_missing_cached_artifact", "blocked_unrecognized_status"}
        ]
        if blocked:
            return safe_receipts, str(blocked[0].get("status"))
        queued = [row for row in rows if row.get("status") == "queued"]
        running = [row for row in rows if row.get("status") == "running"]
        if not queued:
            if running:
                return safe_receipts, "preexisting_live_running_job"
            return safe_receipts, None

        cumulative_cost = await _cumulative_cost(db, corpus_id=corpus_id)
        if cumulative_cost["budget_accounting_complete"] is not True:
            return safe_receipts, "cost_telemetry_incomplete"
        if cumulative_cost["ceiling_basis_usd"] >= cost_ceiling_usd:
            return safe_receipts, "cumulative_cost_ceiling_reached"

        current_key = await settings_service.get_plaintext_key_any_user(
            credential_provider
        )
        if not current_key:
            return safe_receipts, "encrypted_provider_credential_unavailable"
        if not hmac.compare_digest(
            _credential_fingerprint(current_key), initial_fingerprint
        ):
            return safe_receipts, "provider_key_rotation_detected"

        candidates = queued[:concurrency]
        uncached: list[dict[str, Any]] = []
        for job in candidates:
            packet = by_job_id[str(job.get("job_id") or "")]
            accepted_cache = await _load_certified_acceptance(
                db, planned=packet, config=config
            )
            if accepted_cache:
                await _mark_cached_success(
                    db,
                    planned=packet,
                    phase=phase,
                    accepted_cache=accepted_cache,
                )
                safe_receipts.append(
                    {
                        "ordinal": packet.ordinal,
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

        claimed = await claim_runnable_jobs(
            db,
            collection_name=JOB_COLLECTION,
            jobs=uncached,
            runnable_statuses={"queued"},
            runner=runner,
            increment_attempt=True,
            max_attempts=1,
            set_fields={"phase": phase, "phase_run_id": runner},
        )
        if not claimed:
            continue
        route = SemanticGatewayRoute(
            api_base=provider_price_card.api_base,
            api_key=current_key,
        )
        results = await asyncio.gather(
            *[
                _run_claimed_job(
                    db,
                    claimed=job,
                    planned=by_job_id[str(job.get("job_id") or "")],
                    config=config,
                    route=route,
                    provider_price_card=provider_price_card,
                )
                for job in claimed
            ]
        )
        safe_receipts.extend(sorted(results, key=lambda row: int(row["ordinal"])))
        cumulative_cost = await _cumulative_cost(db, corpus_id=corpus_id)
        if cumulative_cost["budget_accounting_complete"] is not True:
            return safe_receipts, "cost_telemetry_incomplete"
        if cumulative_cost["ceiling_basis_usd"] >= cost_ceiling_usd:
            return safe_receipts, "cumulative_cost_ceiling_reached"
        if phase in {"phase1b", "phase1c", "phase2"}:
            current_rows = await _phase_rows(db, corpus_id=corpus_id, phase=phase)
            if (
                phase == "phase1c"
                and _read_timeout_count(current_rows)
                >= PHASE1C_READ_TIMEOUT_PAUSE_COUNT
            ):
                return safe_receipts, "read_timeout_recurrence_pause"
            stop = phase2_auto_stop_reason(
                current_rows,
                cumulative_cost_usd=float(cumulative_cost["ceiling_basis_usd"]),
                cost_ceiling_usd=cost_ceiling_usd,
            )
            if stop:
                return safe_receipts, stop


def _build_config(parameter_card: RouteParameterCard) -> SemanticGatewayConfig:
    args = argparse.Namespace(
        runtime_version=parameter_card.runtime_version,
        model=parameter_card.model_id,
        api_base=parameter_card.api_base,
        tokenizer_id=parameter_card.tokenizer_id,
        max_tokens=parameter_card.max_tokens,
        timeout_seconds=parameter_card.timeout_seconds,
    )
    return _gateway_config(args, requested_tier="tier3")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    allowed_phases = {"phase1", "phase1b", "phase1c", "phase2", "tail-retry"}
    if args.phase not in allowed_phases:
        raise PaidPassError(
            "phase must be phase1, phase1b, phase1c, phase2, or tail-retry"
        )
    if not 1 <= args.concurrency <= 3:
        raise PaidPassError("certified paid pass requires concurrency between 1 and 3")
    reevaluation = _reevaluation_context(args)
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:
            db = client[settings.MONGODB_DATABASE]
        active_batches = await db["ingest_batches"].count_documents(
            {"status": {"$in": ["queued", "running"]}}
        )
        if active_batches:
            raise PaidPassError(
                f"refusing paid pass while {active_batches} ingest batch(es) are active"
            )
        parameter_card = _load_route_parameter_card(
            args.route_parameter_cards,
            route_id=ROUTE_ID,
            model_id="openai/LongCat-2.0",
            api_base="https://api.longcat.chat/openai/v1",
        )
        provider_price_card = _load_provider_price_card(
            args.provider_price_cards,
            route_id=ROUTE_ID,
            model_id=parameter_card.model_id,
            api_base=parameter_card.api_base,
        )
        config = _build_config(parameter_card)
        if (
            parameter_card.capability_tier != "tier3"
            or parameter_card.max_tokens != 8192
            or parameter_card.temperature != 0
            or parameter_card.thinking != "disabled"
        ):
            raise PaidPassError("certified route parameter card drifted")
        corpus_id, document_count, packets = await _discover_all_packets(
            db,
            corpus_name=args.corpus_name,
            max_entities=args.max_entities,
        )
        if len(packets) != args.expected_packet_count:
            raise PaidPassError(
                f"eligible packet census drifted: expected {args.expected_packet_count}, "
                f"found {len(packets)}"
            )
        ceiling = paid_pass_ceiling_usd(len(packets))
        if ceiling > args.max_authorized_cost_usd:
            raise PaidPassError(
                f"computed ceiling {ceiling:.8f} exceeds authorized "
                f"{args.max_authorized_cost_usd:.8f}"
            )
        planned = _plan_packets(corpus_id=corpus_id, packets=packets, config=config)
        packet_bytes = [row.packet_bytes for row in planned]
        canonical_before = await _canonical_store_census(db=db, settings=settings)

        settings_service.attach(db)
        api_key = await settings_service.get_plaintext_key_any_user(
            args.credential_provider
        )
        if not api_key:
            raise PaidPassError(
                f"encrypted {args.credential_provider} credential is not configured"
            )
        owner = f"semantic-digest-paid-pass:{args.phase}:{uuid4().hex}"
        async with corpus_lane_lease(
            db,
            corpus_id=corpus_id,
            lane=LANE,
            owner=owner,
            lease_seconds=30 * 60,
        ) as lease:
            if not lease:
                raise PaidPassError("semantic digest paid-pass lane lease is busy")
            phase_planned = list(planned[:PHASE1_LIMIT])
            selection_name: str | None = None
            prerequisite_release: dict[str, Any] | None = None
            corpus_acceptance_before_tail: dict[str, Any] | None = None
            booked_exposure_count = 0
            if args.phase == "phase1b":
                selection_name = PHASE1B_SELECTION
                phase_planned = await _select_fresh_phase_packets(
                    db,
                    corpus_id=corpus_id,
                    planned=planned,
                    config=config,
                    selection_name=selection_name,
                    limit=PHASE1B_LIMIT,
                    expected_excluded_count=PHASE1_PURCHASED_COUNT,
                )
            elif args.phase == "phase1c":
                prerequisite_release = await _require_phase_release(
                    db,
                    corpus_id=corpus_id,
                    selection_name=PHASE1B_SELECTION,
                    expected_count=PHASE1B_LIMIT,
                    phase_name="phase1b",
                    green_field=PHASE1B_GREEN_FIELD,
                    canonical_field=PHASE1B_CANONICAL_FIELD,
                )
                selection_name = PHASE1C_SELECTION
                phase_planned = await _select_fresh_phase_packets(
                    db,
                    corpus_id=corpus_id,
                    planned=planned,
                    config=config,
                    selection_name=selection_name,
                    limit=PHASE1C_LIMIT,
                    expected_excluded_count=PRE_PHASE1C_PURCHASED_COUNT,
                )
            elif args.phase == "phase2":
                prerequisite_release = await _require_phase_release(
                    db,
                    corpus_id=corpus_id,
                    selection_name=PHASE1C_SELECTION,
                    expected_count=PHASE1C_LIMIT,
                    phase_name="phase1c",
                    green_field=PHASE1C_GREEN_FIELD,
                    canonical_field=PHASE1C_CANONICAL_FIELD,
                )
                selection_name = PHASE2_SELECTION
                phase_planned = await _select_fresh_phase_packets(
                    db,
                    corpus_id=corpus_id,
                    planned=planned,
                    config=config,
                    selection_name=selection_name,
                    limit=None,
                    expected_excluded_count=(
                        PRE_PHASE1C_PURCHASED_COUNT + PHASE1C_LIMIT
                    ),
                )
                expected_remainder = (
                    len(planned) - PRE_PHASE1C_PURCHASED_COUNT - PHASE1C_LIMIT
                )
                if len(phase_planned) != expected_remainder:
                    raise PaidPassError(
                        "phase2 remainder drifted: expected "
                        f"{expected_remainder}, found {len(phase_planned)}"
                    )
            elif args.phase == "tail-retry":
                prerequisite_release = await _require_phase_release(
                    db,
                    corpus_id=corpus_id,
                    selection_name=PHASE2_SELECTION,
                    expected_count=(
                        len(planned) - PRE_PHASE1C_PURCHASED_COUNT - PHASE1C_LIMIT
                    ),
                    phase_name="phase2",
                    green_field=PHASE2_GREEN_FIELD,
                    canonical_field=PHASE2_CANONICAL_FIELD,
                )
                corpus_acceptance_before_tail = await _corpus_certified_acceptance(
                    db,
                    planned=planned,
                    config=config,
                )
                if corpus_acceptance_before_tail["green"] is not True:
                    raise PaidPassError(
                        "tail retry is sealed until corpus-wide certified "
                        "acceptance is at least 95 percent"
                    )
                selection_name = TAIL_RETRY_SELECTION
                phase_planned = await _select_tail_retry_packets(
                    db,
                    corpus_id=corpus_id,
                    planned=planned,
                    config=config,
                )
            planned_counts = await _materialize_jobs(
                db,
                corpus_id=corpus_id,
                planned=phase_planned,
                config=config,
                parameter_card=parameter_card,
            )
            if reevaluation is not None:
                reevaluation_rows = await _phase_rows(
                    db,
                    corpus_id=corpus_id,
                    phase=args.phase,
                )
                if len(reevaluation_rows) != len(phase_planned):
                    raise PaidPassError("zero-provider re-evaluation row count drifted")
                nonterminal = [
                    row
                    for row in reevaluation_rows
                    if row.get("status") not in PURCHASED_TERMINAL_STATUSES
                ]
                if nonterminal:
                    raise PaidPassError(
                        "zero-provider re-evaluation found claimable or "
                        "non-purchased rows"
                    )
            if selection_name:
                await _persist_phase_selection(
                    db,
                    selected=phase_planned,
                    config=config,
                    phase=args.phase,
                    selection_name=selection_name,
                )
            booked_exposure_count = await _book_transport_exposure_bounds(
                db,
                corpus_id=corpus_id,
                phase=args.phase,
            )
            call_receipts, stop_reason = await _execute_phase(
                db,
                phase=args.phase,
                corpus_id=corpus_id,
                planned=phase_planned,
                config=config,
                provider_price_card=provider_price_card,
                credential_provider=args.credential_provider,
                initial_api_key=api_key,
                concurrency=args.concurrency,
                cost_ceiling_usd=ceiling,
            )
            if reevaluation is not None and call_receipts:
                raise PaidPassError(
                    "zero-provider re-evaluation unexpectedly produced call receipts"
                )
            canonical_after = await _canonical_store_census(db=db, settings=settings)
            canonical_census_receipt = _canonical_store_census_receipt(
                canonical_before,
                canonical_after,
            )
            rows = await _phase_rows(db, corpus_id=corpus_id, phase=args.phase)
            checkpoint: dict[str, Any] | None = None
            if args.phase == "phase1":
                checkpoint = phase1_checkpoint(
                    rows,
                    canonical_before=canonical_before,
                    canonical_after=canonical_after,
                )
                if stop_reason:
                    checkpoint["all_green"] = False
                    checkpoint["stop_reason"] = stop_reason
                if checkpoint["all_green"]:
                    checkpoint_time = datetime.utcnow()
                    await db[JOB_COLLECTION].update_many(
                        {
                            "corpus_id": corpus_id,
                            "route_id": ROUTE_ID,
                            "ordinal": {"$lt": PHASE1_LIMIT},
                        },
                        {
                            "$set": {
                                "phase1_checkpoint_green": True,
                                "phase1_checkpoint_at": checkpoint_time,
                                "checkpoint_canonical": canonical_after,
                                "updated_at": checkpoint_time,
                            }
                        },
                    )
            elif args.phase in {"phase1b", "phase1c"}:
                target_count = (
                    PHASE1B_LIMIT if args.phase == "phase1b" else PHASE1C_LIMIT
                )
                minimum_acceptance = (
                    PHASE1B_MIN_ACCEPTANCE
                    if args.phase == "phase1b"
                    else PHASE1C_MIN_ACCEPTANCE
                )
                checkpoint = paid_phase_checkpoint(
                    rows,
                    target_count=target_count,
                    minimum_acceptance=minimum_acceptance,
                    canonical_before=canonical_before,
                    canonical_after=canonical_after,
                )
                if stop_reason:
                    checkpoint["all_green"] = False
                    checkpoint["stop_reason"] = stop_reason
                if checkpoint["all_green"]:
                    if args.phase == "phase1b":
                        green_field = PHASE1B_GREEN_FIELD
                        canonical_field = PHASE1B_CANONICAL_FIELD
                        release_selection = PHASE1B_SELECTION
                    else:
                        green_field = PHASE1C_GREEN_FIELD
                        canonical_field = PHASE1C_CANONICAL_FIELD
                        release_selection = PHASE1C_SELECTION
                    await _persist_phase_release(
                        db,
                        selected=phase_planned,
                        selection_name=release_selection,
                        green_field=green_field,
                        canonical_field=canonical_field,
                        canonical=canonical_after,
                    )
            cumulative_cost = await _cumulative_cost(db, corpus_id=corpus_id)
            completed = sum(
                row.get("status") in PURCHASED_TERMINAL_STATUSES for row in rows
            )
            accepted = sum(row.get("status") == SUCCESS_STATUS for row in rows)
            phase_complete = completed == len(rows) and not stop_reason
            all_green = (
                checkpoint["all_green"]
                if checkpoint is not None
                else bool(
                    phase_complete
                    and cumulative_cost["budget_accounting_complete"] is True
                    and cumulative_cost["ceiling_basis_usd"] < ceiling
                    and canonical_before == canonical_after
                )
            )
            if args.phase == "phase2" and all_green:
                await _persist_phase_release(
                    db,
                    selected=phase_planned,
                    selection_name=PHASE2_SELECTION,
                    green_field=PHASE2_GREEN_FIELD,
                    canonical_field=PHASE2_CANONICAL_FIELD,
                    canonical=canonical_after,
                )
            report = {
                "schema_version": "polymath.semantic_digest_paid_pass.v1",
                "generated_at": _utc_now(),
                "phase": args.phase,
                "corpus": {
                    "name": args.corpus_name,
                    "corpus_id": corpus_id,
                    "document_count": document_count,
                    "eligible_packet_count": len(planned),
                    "packet_bytes": {
                        "min": min(packet_bytes),
                        "max": max(packet_bytes),
                    },
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
                        config.prompt_version,
                        config.repair_prompt_version,
                    ),
                    "repair_prompt_version": config.repair_prompt_version,
                    "repair_prompt_hash": semantic_digest_repair_prompt_hash(
                        config.repair_prompt_version
                    ),
                    "max_tokens": parameter_card.max_tokens,
                    "temperature": parameter_card.temperature,
                    "thinking": parameter_card.thinking,
                    "credential_source": (
                        "encrypted settings.api_keys." + args.credential_provider
                    ),
                },
                "durable_queue": {
                    "collection": JOB_COLLECTION,
                    "phase_selection": selection_name,
                    "planned_counts": planned_counts,
                    "phase_target": len(rows),
                    "terminal_count": completed,
                    "accepted_count": accepted,
                    "booked_exposure_count": booked_exposure_count,
                    "dead_letter_count": sum(
                        row.get("status") in FAILURE_STATUSES for row in rows
                    ),
                    "call_receipts": call_receipts,
                },
                "cost_accounting": {
                    "estimated_cost_per_packet_usd": ESTIMATED_COST_PER_PACKET_USD,
                    "safety_multiplier": COST_SAFETY_MULTIPLIER,
                    "authorized_ceiling_usd": ceiling,
                    "cumulative_actual_cost_usd": cumulative_cost[
                        "known_actual_cost_usd"
                    ],
                    "cost_complete": cumulative_cost["actual_cost_complete"],
                    "budget_accounting_complete": cumulative_cost[
                        "budget_accounting_complete"
                    ],
                    "cost_accounting_state": cumulative_cost["cost_accounting_state"],
                    "unpriced_exposure_count": cumulative_cost[
                        "unpriced_exposure_count"
                    ],
                    "bounded_exposure_usd": cumulative_cost["bounded_exposure_usd"],
                    "ceiling_basis_usd": cumulative_cost["ceiling_basis_usd"],
                    "within_ceiling": (cumulative_cost["ceiling_basis_usd"] < ceiling),
                },
                "canonical_store_census": canonical_census_receipt,
                "re_evaluation": (
                    {
                        **reevaluation,
                        "provider_call_count": len(call_receipts),
                    }
                    if reevaluation is not None
                    else None
                ),
                "checkpoint": checkpoint,
                "prerequisite_release": {
                    "phase": (
                        "phase1b"
                        if args.phase == "phase1c"
                        else "phase1c"
                        if args.phase == "phase2"
                        else "phase2"
                        if args.phase == "tail-retry"
                        else None
                    ),
                    "present": prerequisite_release is not None,
                },
                "corpus_acceptance_before_tail": corpus_acceptance_before_tail,
                "stop_reason": stop_reason,
                "security": {
                    "credentials_from_encrypted_settings": True,
                    "plaintext_credentials_in_receipt": False,
                    "packet_text_in_receipt": False,
                    "raw_provider_output_in_receipt": False,
                    "canonical_write": False,
                },
                "acceptance": {
                    "phase_complete": phase_complete,
                    "all_green": all_green,
                },
            }
            return report
    finally:
        client.close()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("phase1", "phase1b", "phase1c", "phase2", "tail-retry"),
        required=True,
    )
    parser.add_argument("--corpus-name", default=DEFAULT_CORPUS_NAME)
    parser.add_argument("--expected-packet-count", type=int, required=True)
    parser.add_argument("--max-authorized-cost-usd", type=float, required=True)
    parser.add_argument("--max-entities", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--credential-provider", default=DEFAULT_CREDENTIAL_PROVIDER)
    parser.add_argument(
        "--provider-price-cards", type=Path, default=DEFAULT_PROVIDER_PRICE_CARDS
    )
    parser.add_argument(
        "--route-parameter-cards", type=Path, default=DEFAULT_ROUTE_PARAMETER_CARDS
    )
    parser.add_argument("--reevaluation-prior-receipt-sha256")
    parser.add_argument("--reevaluation-authorization")
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        safe_failure = {
            "schema_version": "polymath.semantic_digest_paid_pass.failure.v1",
            "generated_at": _utc_now(),
            "phase": args.phase,
            "error_class": type(exc).__name__,
            "all_green": False,
        }
        _write_report(args.out, safe_failure)
        print(json.dumps(safe_failure, sort_keys=True))
        return 1
    _write_report(args.out, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["acceptance"]["all_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
