#!/usr/bin/env python3
"""Execute the exact senior-authorized sentence-hybrid v3 canary."""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from scripts.semantic_gateway_mark_atomic_b4 import (
    _checkpoint,
    _execute_serial,
    _selection_hash,
    _selection_rows,
    _typed_route_cards,
)
from scripts.semantic_gateway_mark_paid_pass import (
    JOB_COLLECTION,
    ROUTE_ID,
    PaidPassError,
    PlannedPacket,
    _cumulative_cost,
    _materialize_jobs,
    _persist_phase_selection,
)
from scripts.semantic_gateway_mark_sentence_hybrid_preflight import (
    CUMULATIVE_OWNER_UMBRELLA_USD,
    DEFAULT_CORPUS_NAME,
    SentenceHybridPrepared,
    _prepare,
)
from scripts.semantic_gateway_ugo_canary import _canonical_store_census
from services.ingestion.job_leases import corpus_lane_lease
from services.settings import settings_service

PHASE = "sentence_hybrid_v3_canary"
LANE = "semantic_digest_sentence_hybrid_v3_canary"
TARGET_COUNT = 10
MIN_ACCEPTED_COUNT = 9
MAX_READ_TIMEOUTS = 2
CREDENTIAL_PROVIDER = "longcat"
GO_AUTHORIZATION_REFERENCE = "COORDINATION.md:2026-07-15T03:39:13Z:v3-CANARY-GO"
AUTHORIZED_PACKET_SET_HASH = (
    "sha256:89ace7ede4eab1d00f7f8d062b92d756cc5f7243fe4d0c3d0c7e0fec131b2d43"
)
AUTHORIZED_PACKET_SCHEMA_HASH = (
    "sha256:5c600d3047807541a09be38d01933b6e048f5a3f730de1b5e2cf6c48991f2e40"
)
AUTHORIZED_SELECTION_SET_HASH = (
    "sha256:6aed7b1a967c1ad8889a0f058091e7f47691053d25185ff03cac797b3875f595"
)
AUTHORIZED_COST_USD = Decimal("0.78260930")


def _assert_exact(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise PaidPassError(f"{label} drifted from the senior-authorized value")


def _assert_go_contract(
    prepared: SentenceHybridPrepared,
    *,
    authorization_reference: str,
    expected_packet_set_hash: str,
    expected_packet_schema_hash: str,
    expected_selection_set_hash: str,
    expected_prompt_hash: str,
    expected_repair_prompt_hash: str,
    expected_digest_schema_hash: str,
    max_authorized_cost_usd: Decimal,
) -> None:
    receipt = prepared.receipt
    _assert_exact(
        "authorization reference",
        authorization_reference,
        GO_AUTHORIZATION_REFERENCE,
    )
    _assert_exact(
        "authorized packet set argument",
        expected_packet_set_hash,
        AUTHORIZED_PACKET_SET_HASH,
    )
    _assert_exact(
        "authorized packet schema argument",
        expected_packet_schema_hash,
        AUTHORIZED_PACKET_SCHEMA_HASH,
    )
    _assert_exact(
        "authorized selection argument",
        expected_selection_set_hash,
        AUTHORIZED_SELECTION_SET_HASH,
    )
    _assert_exact(
        "authorized cost argument",
        max_authorized_cost_usd,
        AUTHORIZED_COST_USD,
    )

    packet = receipt["packet_contract"]
    _assert_exact(
        "packet schema version",
        packet["packet_schema_version"],
        ("semantic_parent_packet.sentence_hybrid.v3"),
    )
    _assert_exact(
        "packet set hash", packet["packet_set_hash"], expected_packet_set_hash
    )
    _assert_exact(
        "packet schema hash",
        packet["packet_schema_hash"],
        expected_packet_schema_hash,
    )
    _assert_exact("packet byte cap", packet["packet_max_utf8_bytes"], 26_000)
    _assert_exact("packet ready count", receipt["corpus"]["packet_ready_count"], 793)
    _assert_exact(
        "non-packet-ready count",
        receipt["corpus"]["non_packet_ready_count"],
        2,
    )
    _assert_exact("long packet population", packet["over_20000_packet_count"], 3)
    _assert_exact("oversize packet population", packet["over_26000_packet_count"], 0)
    _assert_exact("dropped sentence count", packet["dropped_sentence_count"], 0)

    mapping = receipt["mapping_disclosure"]
    _assert_exact("source sentence count", mapping["source_sentence_count"], 30_694)
    _assert_exact("mapped sentence count", mapping["mapped_sentence_count"], 24_845)
    _assert_exact(
        "context-only sentence count",
        mapping["context_only_sentence_count"],
        5_849,
    )
    _assert_exact(
        "context-only citations", mapping["context_only_units_uncitable"], True
    )
    _assert_exact(
        "atomic intent assertion", mapping["expansion_is_unique_model_intent"], False
    )

    selection = receipt["selection"]
    _assert_exact(
        "selection set hash",
        selection["selection_set_hash"],
        expected_selection_set_hash,
    )
    _assert_exact("selection count", len(prepared.selected), TARGET_COUNT)
    _assert_exact(
        "selection documents", selection["unique_document_count"], TARGET_COUNT
    )
    _assert_exact(
        "selected long packet count", selection["long_packet_selected_count"], 1
    )
    _assert_exact(
        "fresh packet-ready population",
        receipt["historical_ledger"]["fresh_packet_ready_count"],
        728,
    )

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
    _assert_exact("prompt hash", provider["prompt_hash"], expected_prompt_hash)
    _assert_exact(
        "repair prompt hash",
        provider["repair_prompt_hash"],
        expected_repair_prompt_hash,
    )
    _assert_exact(
        "digest schema hash",
        provider["digest_schema_hash"],
        expected_digest_schema_hash,
    )
    _assert_exact(
        "selected authority",
        Decimal(receipt["cost_authority"]["selected_governing_authority_usd"]),
        max_authorized_cost_usd,
    )
    _assert_exact(
        "selected reservation sum",
        Decimal(receipt["cost_authority"]["selected_claim_reservation_sum_usd"]),
        max_authorized_cost_usd,
    )


async def _global_umbrella_preflight(db: Any, *, corpus_id: str) -> dict[str, Any]:
    cumulative = await _cumulative_cost(db, corpus_id=corpus_id)
    if cumulative["budget_accounting_complete"] is not True:
        raise PaidPassError("cumulative umbrella accounting is incomplete")
    basis = Decimal(str(cumulative["ceiling_basis_usd"]))
    remaining = CUMULATIVE_OWNER_UMBRELLA_USD - basis
    if remaining < AUTHORIZED_COST_USD:
        raise PaidPassError("remaining cumulative umbrella cannot reserve canary")
    return {
        **cumulative,
        "owner_cumulative_umbrella_usd": float(CUMULATIVE_OWNER_UMBRELLA_USD),
        "remaining_before_canary_usd": float(remaining),
        "canary_reservation_usd": float(AUTHORIZED_COST_USD),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    authorized_ceiling = Decimal(str(args.max_authorized_cost_usd))
    prepared = await _prepare(
        argparse.Namespace(
            corpus_name=args.corpus_name,
            expected_parent_count=args.expected_parent_count,
            expected_child_count=args.expected_child_count,
            expected_packet_ready_count=793,
            expected_non_packet_ready_count=2,
            expected_source_sentence_count=30_694,
            expected_mapped_sentence_count=24_845,
            expected_context_only_sentence_count=5_849,
            expected_over_20000_count=3,
            max_entities=args.max_entities,
        )
    )
    _assert_go_contract(
        prepared,
        authorization_reference=args.authorization_reference,
        expected_packet_set_hash=args.expected_packet_set_hash,
        expected_packet_schema_hash=args.expected_packet_schema_hash,
        expected_selection_set_hash=args.expected_selection_set_hash,
        expected_prompt_hash=args.expected_prompt_hash,
        expected_repair_prompt_hash=args.expected_repair_prompt_hash,
        expected_digest_schema_hash=args.expected_digest_schema_hash,
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
                "sentence-hybrid canary requires no active ingest or running semantic job"
            )
        canonical_before = await _canonical_store_census(db=db, settings=settings)
        umbrella_before = await _global_umbrella_preflight(db, corpus_id=corpus_id)
        settings_service.attach(db)
        api_key = await settings_service.get_plaintext_key_any_user(CREDENTIAL_PROVIDER)
        if not api_key:
            raise PaidPassError("encrypted LongCat credential is not configured")

        owner = f"semantic-digest-sentence-hybrid-v3:{uuid4().hex}"
        async with corpus_lane_lease(
            db,
            corpus_id=corpus_id,
            lane=LANE,
            owner=owner,
            lease_seconds=30 * 60,
        ) as lease:
            if not lease:
                raise PaidPassError("sentence-hybrid canary lane lease is busy")
            active_ingests = await db["ingest_batches"].count_documents(
                {"status": {"$in": ["queued", "running"]}}
            )
            running_jobs = await db[JOB_COLLECTION].count_documents(
                {"status": "running"}
            )
            if active_ingests or running_jobs:
                raise PaidPassError(
                    "sentence-hybrid operational state changed after lease"
                )

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
                phase=PHASE,
                runner_prefix="semantic-digest-sentence-hybrid-v3",
                lane_label="sentence-hybrid v3 canary",
            )
            canonical_after = await _canonical_store_census(db=db, settings=settings)
            rows = await _selection_rows(
                db,
                job_ids=[row.job_id for row in selected],
                selection_name=selection_name,
                phase=PHASE,
                lane_label="sentence-hybrid v3 canary",
            )
            checkpoint = _checkpoint(
                rows,
                canonical_before=canonical_before,
                canonical_after=canonical_after,
                authorized_ceiling=authorized_ceiling,
                stop_reason=stop_reason,
                target_count=TARGET_COUNT,
                minimum_accepted_count=MIN_ACCEPTED_COUNT,
                max_read_timeouts=MAX_READ_TIMEOUTS,
            )
            umbrella_after = await _cumulative_cost(db, corpus_id=corpus_id)
            if (
                umbrella_after["budget_accounting_complete"] is not True
                or Decimal(str(umbrella_after["ceiling_basis_usd"]))
                > CUMULATIVE_OWNER_UMBRELLA_USD
            ):
                raise PaidPassError("cumulative owner umbrella breached or incomplete")
            return {
                "schema_version": (
                    "polymath.semantic_digest_sentence_hybrid_canary_execution.v1"
                ),
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
                    "packet_schema_hash": args.expected_packet_schema_hash,
                    "packet_set_hash": args.expected_packet_set_hash,
                    "selection_set_hash": args.expected_selection_set_hash,
                    "selection_name": selection_name,
                    "selected_count": len(selected),
                    "unique_document_count": prepared.receipt["selection"][
                        "unique_document_count"
                    ],
                    "selected_long_packet_count": prepared.receipt["selection"][
                        "long_packet_selected_count"
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
                    "digest_schema_hash": args.expected_digest_schema_hash,
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
                "cumulative_umbrella": {
                    "before": umbrella_before,
                    "after": umbrella_after,
                    "owner_cumulative_umbrella_usd": float(
                        CUMULATIVE_OWNER_UMBRELLA_USD
                    ),
                },
                "summary_faithfulness_review": {
                    **prepared.receipt["selection_recipe"][
                        "summary_faithfulness_review"
                    ],
                    "status": "pending_read_only_postflight",
                },
                "security": {
                    "credential_source": "encrypted settings.api_keys.longcat",
                    "plaintext_credential_in_receipt": False,
                    "packet_text_in_receipt": False,
                    "raw_provider_output_in_receipt": False,
                    "canonical_write": False,
                },
                "execution_gate_green": checkpoint["execution_green"],
                "canary_final_green": False,
                "canary_final_green_reason": "faithfulness_review_pending",
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
    parser.add_argument("--expected-packet-schema-hash", required=True)
    parser.add_argument("--expected-selection-set-hash", required=True)
    parser.add_argument("--expected-prompt-hash", required=True)
    parser.add_argument("--expected-repair-prompt-hash", required=True)
    parser.add_argument("--expected-digest-schema-hash", required=True)
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
            "schema_version": (
                "polymath.semantic_digest_sentence_hybrid_canary_execution.failure.v1"
            ),
            "phase": PHASE,
            "error_class": type(exc).__name__,
            "execution_gate_green": False,
            "canary_final_green": False,
            "plaintext_credential_in_receipt": False,
        }
        _write_report(args.out, safe_failure)
        print(json.dumps(safe_failure, sort_keys=True))
        return 1
    _write_report(args.out, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["execution_gate_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
