#!/usr/bin/env python3
"""Execute the senior-authorized, noncanonical atomic-claims B4 canary."""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import hmac
import json
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from models.hash_taxonomy import namespace_hash
from scripts.materialize_semantic_digest_claim_inputs import _route_prices
from scripts.semantic_gateway_mark_atomic_preflight import (
    DEFAULT_CORPUS_NAME,
    AtomicB4Prepared,
    _prepare,
)
from scripts.semantic_gateway_mark_paid_pass import (
    FAILURE_STATUSES,
    JOB_COLLECTION,
    PURCHASED_TERMINAL_STATUSES,
    ROUTE_ID,
    SUCCESS_STATUS,
    UNPRICED_EXPOSURE_BASIS,
    UNPRICED_EXPOSURE_BOUND_USD,
    PaidPassError,
    PlannedPacket,
    _cost_accounting,
    _credential_fingerprint,
    _load_certified_acceptance,
    _mark_cached_success,
    _materialize_jobs,
    _persist_phase_selection,
    _run_claimed_job,
)
from scripts.semantic_gateway_ugo_canary import (
    DEFAULT_PROVIDER_PRICE_CARDS,
    DEFAULT_ROUTE_PARAMETER_CARDS,
    ProviderPriceCard,
    RouteParameterCard,
    _canonical_store_census,
    _canonical_store_census_receipt,
    _load_provider_price_card,
    _load_route_parameter_card,
)
from services.ingestion.job_leases import claim_runnable_jobs, corpus_lane_lease
from services.ingestion.paid_cost_reservation import (
    cost_reservation_allows_claim,
    worst_case_next_call_cost_usd,
)
from services.semantic_gateway import SemanticGatewayRoute
from services.settings import settings_service


PHASE = "b4_atomic"
LANE = "semantic_digest_atomic_b4"
TARGET_COUNT = 10
MIN_ACCEPTED_COUNT = 9
MAX_READ_TIMEOUTS = 2
CREDENTIAL_PROVIDER = "longcat"
GO_AUTHORIZATION_REFERENCE = "COORDINATION.md:2026-07-15T01:31:30Z:B4-GO"


def _assert_exact(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise PaidPassError(f"{label} drifted from the senior-authorized value")


def _assert_go_contract(
    prepared: AtomicB4Prepared,
    *,
    authorization_reference: str,
    expected_packet_set_hash: str,
    expected_selection_set_hash: str,
    expected_prompt_hash: str,
    expected_repair_prompt_hash: str,
    expected_schema_hash: str,
    max_authorized_cost_usd: Decimal,
) -> None:
    receipt = prepared.receipt
    _assert_exact(
        "authorization reference",
        authorization_reference,
        GO_AUTHORIZATION_REFERENCE,
    )
    _assert_exact(
        "packet set hash",
        receipt["packet_contract"]["packet_set_hash"],
        expected_packet_set_hash,
    )
    _assert_exact(
        "selection set hash",
        receipt["selection"]["selection_set_hash"],
        expected_selection_set_hash,
    )
    _assert_exact(
        "prompt hash",
        receipt["provider_contract"]["prompt_hash"],
        expected_prompt_hash,
    )
    _assert_exact(
        "repair prompt hash",
        receipt["provider_contract"]["repair_prompt_hash"],
        expected_repair_prompt_hash,
    )
    _assert_exact(
        "schema hash",
        receipt["provider_contract"]["schema_hash"],
        expected_schema_hash,
    )
    _assert_exact("target count", len(prepared.selected), TARGET_COUNT)
    _assert_exact(
        "selected document count",
        receipt["selection"]["unique_document_count"],
        TARGET_COUNT,
    )
    if len({item.row.planned.job_id for item in prepared.selected}) != TARGET_COUNT:
        raise PaidPassError("B4 selection contains duplicate durable job identities")
    selected_ceiling = Decimal(
        str(receipt["cost_authority"]["selected_b4_ceiling_usd"])
    )
    _assert_exact("authorized cost ceiling", selected_ceiling, max_authorized_cost_usd)
    provider = receipt["provider_contract"]
    expected_provider = {
        "route_id": ROUTE_ID,
        "model_id": "openai/LongCat-2.0",
        "capability_tier": "tier3",
        "max_tokens": 8192,
        "temperature": 0,
        "thinking": "disabled",
    }
    for key, value in expected_provider.items():
        _assert_exact(f"provider contract {key}", provider[key], value)


def _selection_hash(planned: Sequence[PlannedPacket]) -> str:
    return namespace_hash("input-set", frozenset(row.job_id for row in planned))


def _terminal_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("status") in PURCHASED_TERMINAL_STATUSES]


def _read_timeout_count(rows: Sequence[dict[str, Any]]) -> int:
    return sum(row.get("transport_error_class") == "ReadTimeout" for row in rows)


def _checkpoint(
    rows: Sequence[dict[str, Any]],
    *,
    canonical_before: dict[str, Any],
    canonical_after: dict[str, Any],
    authorized_ceiling: Decimal,
    stop_reason: str | None,
) -> dict[str, Any]:
    terminal = _terminal_rows(rows)
    accepted = sum(row.get("status") == SUCCESS_STATUS for row in terminal)
    dead_lettered = sum(row.get("status") in FAILURE_STATUSES for row in terminal)
    cost = _cost_accounting(terminal)
    canonical = _canonical_store_census_receipt(canonical_before, canonical_after)
    ceiling_green = Decimal(str(cost["ceiling_basis_usd"])) <= authorized_ceiling
    execution_complete = len(rows) == TARGET_COUNT and len(terminal) == TARGET_COUNT
    acceptance_green = accepted >= MIN_ACCEPTED_COUNT
    execution_green = bool(
        execution_complete
        and acceptance_green
        and cost["budget_accounting_complete"] is True
        and ceiling_green
        and canonical["protected_exactly_unchanged"] is True
        and stop_reason is None
    )
    return {
        "target_count": TARGET_COUNT,
        "row_count": len(rows),
        "terminal_count": len(terminal),
        "accepted_count": accepted,
        "dead_letter_count": dead_lettered,
        "minimum_accepted_count": MIN_ACCEPTED_COUNT,
        "acceptance_green": acceptance_green,
        "execution_complete": execution_complete,
        "known_actual_cost_usd": cost["known_actual_cost_usd"],
        "bounded_exposure_usd": cost["bounded_exposure_usd"],
        "unpriced_exposure_count": cost["unpriced_exposure_count"],
        "cost_accounting_state": cost["cost_accounting_state"],
        "budget_accounting_complete": cost["budget_accounting_complete"],
        "ceiling_basis_usd": cost["ceiling_basis_usd"],
        "authorized_ceiling_usd": float(authorized_ceiling),
        "ceiling_green": ceiling_green,
        "read_timeout_count": _read_timeout_count(rows),
        "read_timeout_pause_threshold": MAX_READ_TIMEOUTS,
        "canonical_store_census": canonical,
        "stop_reason": stop_reason,
        "summary_faithfulness_review_pending": accepted > 0,
        "execution_green": execution_green,
    }


async def _selection_rows(
    db: Any,
    *,
    job_ids: Sequence[str],
    selection_name: str,
) -> list[dict[str, Any]]:
    rows = (
        await db[JOB_COLLECTION]
        .find(
            {
                "job_id": {"$in": list(job_ids)},
                "phase_selection": selection_name,
                "phase": PHASE,
            },
            {"_id": 0},
        )
        .sort("ordinal", 1)
        .to_list(length=None)
    )
    if len(rows) != TARGET_COUNT:
        raise PaidPassError(
            f"atomic B4 durable row count drifted: expected {TARGET_COUNT}, "
            f"found {len(rows)}"
        )
    if {str(row.get("job_id") or "") for row in rows} != set(job_ids):
        raise PaidPassError("atomic B4 durable selection identity drifted")
    return rows


def _typed_route_cards() -> tuple[RouteParameterCard, ProviderPriceCard]:
    route = _route_prices()
    parameter = route["parameters"]
    price = route["price"]
    parameter_card = _load_route_parameter_card(
        DEFAULT_ROUTE_PARAMETER_CARDS,
        route_id=ROUTE_ID,
        model_id=parameter["model_id"],
        api_base=parameter["api_base"],
    )
    price_card = _load_provider_price_card(
        DEFAULT_PROVIDER_PRICE_CARDS,
        route_id=ROUTE_ID,
        model_id=price["model_id"],
        api_base=price["api_base"],
    )
    return parameter_card, price_card


async def _execute_serial(
    db: Any,
    *,
    corpus_id: str,
    selected: Sequence[PlannedPacket],
    selection_name: str,
    config: Any,
    price_card: ProviderPriceCard,
    initial_api_key: str,
    authorized_ceiling: Decimal,
) -> tuple[list[dict[str, Any]], str | None]:
    job_ids = [row.job_id for row in selected]
    by_job_id = {row.job_id: row for row in selected}
    runner = f"semantic-digest-atomic-b4:{uuid4().hex}"
    initial_fingerprint = _credential_fingerprint(initial_api_key)
    safe_call_receipts: list[dict[str, Any]] = []
    while True:
        rows = await _selection_rows(
            db,
            job_ids=job_ids,
            selection_name=selection_name,
        )
        if _read_timeout_count(rows) >= MAX_READ_TIMEOUTS:
            return safe_call_receipts, "read_timeout_recurrence_pause"
        blocked = [
            row
            for row in rows
            if row.get("status")
            in {"blocked_missing_cached_artifact", "blocked_unrecognized_status"}
        ]
        if blocked:
            return safe_call_receipts, str(blocked[0].get("status"))
        running = [row for row in rows if row.get("status") == "running"]
        queued = [row for row in rows if row.get("status") == "queued"]
        if not queued:
            if running:
                return safe_call_receipts, "preexisting_live_running_job"
            return safe_call_receipts, None

        cost = _cost_accounting(_terminal_rows(rows))
        if cost["budget_accounting_complete"] is not True:
            return safe_call_receipts, "cost_telemetry_incomplete"
        if Decimal(str(cost["ceiling_basis_usd"])) >= authorized_ceiling:
            return safe_call_receipts, "authorized_cost_ceiling_reached"

        job = queued[0]
        planned = by_job_id[str(job.get("job_id") or "")]
        max_call_cost = worst_case_next_call_cost_usd(
            packet_input_token_upper_bound=planned.packet_bytes,
            max_output_tokens=config.max_tokens,
            uncached_input_usd=price_card.uncached_input_usd,
            output_usd=price_card.output_usd,
            price_unit_tokens=price_card.price_unit_tokens,
        )
        if not cost_reservation_allows_claim(
            current_ceiling_basis_usd=cost["ceiling_basis_usd"],
            max_call_cost_usd=max_call_cost,
            authorized_ceiling_usd=authorized_ceiling,
        ):
            return safe_call_receipts, "insufficient_reserved_cost_for_next_call"

        current_key = await settings_service.get_plaintext_key_any_user(
            CREDENTIAL_PROVIDER
        )
        if not current_key:
            return safe_call_receipts, "encrypted_provider_credential_unavailable"
        if not hmac.compare_digest(
            _credential_fingerprint(current_key), initial_fingerprint
        ):
            return safe_call_receipts, "provider_key_rotation_detected"

        accepted_cache = await _load_certified_acceptance(
            db,
            planned=planned,
            config=config,
        )
        if accepted_cache:
            await _mark_cached_success(
                db,
                planned=planned,
                phase=PHASE,
                accepted_cache=accepted_cache,
            )
            safe_call_receipts.append(
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
            continue

        claimed = await claim_runnable_jobs(
            db,
            collection_name=JOB_COLLECTION,
            jobs=[job],
            runnable_statuses={"queued"},
            runner=runner,
            increment_attempt=True,
            max_attempts=1,
            set_fields={"phase": PHASE, "phase_run_id": runner},
        )
        if not claimed:
            continue
        route = SemanticGatewayRoute(api_base=price_card.api_base, api_key=current_key)
        result = await _run_claimed_job(
            db,
            claimed=claimed[0],
            planned=planned,
            config=config,
            route=route,
            provider_price_card=price_card,
        )
        safe_call_receipts.append(result)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    authorized_ceiling = Decimal(str(args.max_authorized_cost_usd))
    prepared = await _prepare(
        argparse.Namespace(
            corpus_name=args.corpus_name,
            expected_parent_count=args.expected_parent_count,
            expected_child_count=args.expected_child_count,
            max_entities=args.max_entities,
        )
    )
    _assert_go_contract(
        prepared,
        authorization_reference=args.authorization_reference,
        expected_packet_set_hash=args.expected_packet_set_hash,
        expected_selection_set_hash=args.expected_selection_set_hash,
        expected_prompt_hash=args.expected_prompt_hash,
        expected_repair_prompt_hash=args.expected_repair_prompt_hash,
        expected_schema_hash=args.expected_schema_hash,
        max_authorized_cost_usd=authorized_ceiling,
    )
    selected = [item.row.planned for item in prepared.selected]
    _assert_exact(
        "recomputed selection set hash",
        _selection_hash(selected),
        args.expected_selection_set_hash,
    )
    selection_name = str(prepared.receipt["selection"]["selection_name"])
    corpus_id = str(prepared.receipt["corpus"]["corpus_id"])
    parameter_card, price_card = _typed_route_cards()
    _assert_exact("route model", parameter_card.model_id, prepared.config.model_id)
    _assert_exact(
        "route runtime",
        parameter_card.runtime_version,
        prepared.config.runtime_version,
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
                "atomic B4 requires zero active ingest batches and semantic jobs"
            )
        canonical_before = await _canonical_store_census(db=db, settings=settings)
        settings_service.attach(db)
        api_key = await settings_service.get_plaintext_key_any_user(CREDENTIAL_PROVIDER)
        if not api_key:
            raise PaidPassError("encrypted LongCat credential is not configured")

        owner = f"semantic-digest-atomic-b4:{uuid4().hex}"
        async with corpus_lane_lease(
            db,
            corpus_id=corpus_id,
            lane=LANE,
            owner=owner,
            lease_seconds=30 * 60,
        ) as lease:
            if not lease:
                raise PaidPassError("atomic B4 lane lease is busy")
            active_ingests = await db["ingest_batches"].count_documents(
                {"status": {"$in": ["queued", "running"]}}
            )
            running_jobs = await db[JOB_COLLECTION].count_documents(
                {"status": "running"}
            )
            if active_ingests or running_jobs:
                raise PaidPassError("atomic B4 operational state changed after lease")

            planned_counts = await _materialize_jobs(
                db,
                corpus_id=corpus_id,
                planned=selected,
                config=prepared.config,
                parameter_card=parameter_card,
            )
            await _persist_phase_selection(
                db,
                selected=selected,
                config=prepared.config,
                phase=PHASE,
                selection_name=selection_name,
            )
            call_receipts, stop_reason = await _execute_serial(
                db,
                corpus_id=corpus_id,
                selected=selected,
                selection_name=selection_name,
                config=prepared.config,
                price_card=price_card,
                initial_api_key=api_key,
                authorized_ceiling=authorized_ceiling,
            )
            canonical_after = await _canonical_store_census(db=db, settings=settings)
            rows = await _selection_rows(
                db,
                job_ids=[row.job_id for row in selected],
                selection_name=selection_name,
            )
            checkpoint = _checkpoint(
                rows,
                canonical_before=canonical_before,
                canonical_after=canonical_after,
                authorized_ceiling=authorized_ceiling,
                stop_reason=stop_reason,
            )
            return {
                "schema_version": "polymath.semantic_digest_atomic_b4_execution.v1",
                "phase": PHASE,
                "authorization_reference": args.authorization_reference,
                "corpus": {
                    "name": args.corpus_name,
                    "corpus_id": corpus_id,
                },
                "packet_contract": {
                    "schema_version": prepared.receipt["packet_contract"][
                        "packet_schema_version"
                    ],
                    "packet_set_hash": args.expected_packet_set_hash,
                    "selection_set_hash": args.expected_selection_set_hash,
                    "selection_name": selection_name,
                    "selected_count": len(selected),
                    "unique_document_count": prepared.receipt["selection"][
                        "unique_document_count"
                    ],
                },
                "provider_contract": {
                    "route_id": ROUTE_ID,
                    "model_id": parameter_card.model_id,
                    "api_base_origin": urlsplit(parameter_card.api_base).netloc,
                    "capability_tier": parameter_card.capability_tier,
                    "parameter_version": parameter_card.parameter_version,
                    "runtime_version": parameter_card.runtime_version,
                    "prompt_hash": args.expected_prompt_hash,
                    "repair_prompt_hash": args.expected_repair_prompt_hash,
                    "schema_hash": args.expected_schema_hash,
                    "max_tokens": parameter_card.max_tokens,
                    "temperature": parameter_card.temperature,
                    "thinking": parameter_card.thinking,
                },
                "durable_queue": {
                    "collection": JOB_COLLECTION,
                    "planned_counts": planned_counts,
                    "provider_call_count": sum(
                        int(row.get("provider_calls") or 0) for row in call_receipts
                    ),
                    "call_receipts": call_receipts,
                    "max_attempts_per_job": 1,
                    "execution_order": "serial",
                },
                "checkpoint": checkpoint,
                "summary_faithfulness_review": {
                    **prepared.receipt["summary_faithfulness_review"],
                    "status": "pending_read_only_postflight",
                },
                "security": {
                    "credential_source": "encrypted settings.api_keys.longcat",
                    "plaintext_credential_in_receipt": False,
                    "packet_text_in_receipt": False,
                    "raw_provider_output_in_receipt": False,
                    "canonical_write": False,
                },
                "execution_green": checkpoint["execution_green"],
            }
    finally:
        client.close()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--expected-packet-set-hash", required=True)
    parser.add_argument("--expected-selection-set-hash", required=True)
    parser.add_argument("--expected-prompt-hash", required=True)
    parser.add_argument("--expected-repair-prompt-hash", required=True)
    parser.add_argument("--expected-schema-hash", required=True)
    parser.add_argument("--max-authorized-cost-usd", required=True)
    parser.add_argument("--corpus-name", default=DEFAULT_CORPUS_NAME)
    parser.add_argument("--expected-parent-count", type=int, default=795)
    parser.add_argument("--expected-child-count", type=int, default=3493)
    parser.add_argument("--max-entities", type=int, default=40)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        safe_failure = {
            "schema_version": "polymath.semantic_digest_atomic_b4_execution.failure.v1",
            "phase": PHASE,
            "error_class": type(exc).__name__,
            "execution_green": False,
            "plaintext_credential_in_receipt": False,
        }
        _write_report(args.out, safe_failure)
        print(json.dumps(safe_failure, sort_keys=True))
        return 1
    _write_report(args.out, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["execution_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
