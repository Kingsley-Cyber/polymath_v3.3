from argparse import Namespace
from decimal import Decimal
from types import SimpleNamespace

import pytest
import scripts.semantic_gateway_mark_paid_pass as paid_pass_module

from db.queue_integrity import DURABLE_JOB_COLLECTIONS
from models.hash_taxonomy import namespace_hash
from scripts.semantic_gateway_ugo_canary import (
    ProviderPriceCard,
    _canonical_store_census_snapshot,
    _packet_from_parent,
)
from scripts.semantic_gateway_mark_paid_pass import (
    CANARIED_MAX_PACKET_BYTES,
    FAILURE_STATUSES,
    PHASE1B_CANONICAL_FIELD,
    PHASE1B_GREEN_FIELD,
    PHASE1B_LIMIT,
    PHASE1C_LIMIT,
    PHASE1C_MIN_ACCEPTANCE,
    PHASE1C_SELECTION,
    PHASE1C_READ_TIMEOUT_PAUSE_COUNT,
    PHASE1_LIMIT,
    PHASE1_PURCHASED_COUNT,
    PRE_PHASE1C_PURCHASED_COUNT,
    TAIL_RETRY_LIMIT,
    UNPRICED_EXPOSURE_BASIS,
    UNPRICED_EXPOSURE_BOUND_USD,
    BOUNDED_SUCCESS_EXPOSURE_BASIS,
    PlannedPacket,
    PaidPassError,
    _deterministic_fresh_selection,
    _cost_accounting,
    _bounded_success_exposure_fields,
    _job_id,
    _phase1_tail_failure_query,
    _phase1c_timeout_tail_query,
    _read_timeout_count,
    _reevaluation_context,
    _resolve_persisted_selection,
    _validated_release_canonical,
    _valid_cached_row,
    _tail_retry_job_id,
    paid_pass_ceiling_usd,
    paid_phase_checkpoint,
    phase1_checkpoint,
    phase2_auto_stop_reason,
)
from services.semantic_gateway import (
    SemanticGatewayConfig,
    semantic_digest_cache_key,
    semantic_digest_input_hash,
    semantic_digest_prompt_hash,
    semantic_digest_repair_prompt_hash,
    semantic_digest_schema_hash,
)


def _rows(*, accepted=PHASE1_LIMIT, dead_letters=0, cost=0.04):
    rows = [
        {
            "ordinal": index,
            "status": "succeeded",
            "actual_cost_usd": cost,
            "cost_complete": True,
            "packet_bytes": 20_000,
        }
        for index in range(accepted)
    ]
    rows.extend(
        {
            "ordinal": accepted + index,
            "status": "dead_letter",
            "actual_cost_usd": cost,
            "cost_complete": True,
            "packet_bytes": 22_000,
        }
        for index in range(dead_letters)
    )
    return rows


def _canonical_census(**updates):
    counts = {
        "mongo_count": 0,
        "qdrant_counts": {
            "corpus_5a20bc21_naive": 10,
            "polymath_doc_summaries": 20,
            "hermes_memories": 608,
        },
        "neo4j_nodes": 30,
        "neo4j_relationships": 40,
    }
    counts.update(updates)
    return _canonical_store_census_snapshot(**counts)


def _planned_packets(count: int) -> list[PlannedPacket]:
    rows: list[PlannedPacket] = []
    for index in range(count):
        item = _packet_from_parent(
            corpus_id="corpus:mark",
            corpus_name="mark",
            parent={
                "parent_id": f"parent:{index:03d}",
                "doc_id": "doc:one",
                "text": f"Supported statement {index}.",
                "source_hash": f"source:{index}",
                "validation_status": "valid",
                "child_ids": [f"child:{index}"],
            },
            extraction_rows=[
                {
                    "chunk_id": f"child:{index}",
                    "status": "ok",
                    "schema_version": "polymath.extract.v1",
                    "entities": [
                        {
                            "canonical_name": f"Concept {index}",
                            "entity_type": "CONCEPT",
                        }
                    ],
                }
            ],
            max_entities=40,
        )
        rows.append(
            PlannedPacket(
                item=item,
                ordinal=index,
                job_id=f"job:{index:03d}",
                cache_key=f"cache:{index:03d}",
                input_hash=f"input:{index:03d}",
                packet_bytes=1000 + index,
            )
        )
    return rows


def test_mark_ceiling_matches_named_go_arithmetic():
    assert paid_pass_ceiling_usd(989) == 49.45
    with pytest.raises(PaidPassError, match="positive"):
        paid_pass_ceiling_usd(0)


def test_zero_provider_reevaluation_requires_phase1c_and_exact_references():
    args = Namespace(
        phase="phase1c",
        reevaluation_prior_receipt_sha256="sha256:" + "a" * 64,
        reevaluation_authorization="COORDINATION.md#2026-07-14T22:19:43Z",
    )
    assert _reevaluation_context(args) == {
        "mode": "zero_provider_postflight",
        "prior_receipt_sha256": "sha256:" + "a" * 64,
        "authorization_reference": "COORDINATION.md#2026-07-14T22:19:43Z",
    }

    args.phase = "phase2"
    with pytest.raises(PaidPassError, match="phase1c"):
        _reevaluation_context(args)
    args.phase = "phase1c"
    args.reevaluation_prior_receipt_sha256 = "sha256:wrong"
    with pytest.raises(PaidPassError, match="malformed"):
        _reevaluation_context(args)


def test_semantic_digest_queue_is_registered_for_unique_durable_identity():
    assert "semantic_digest_jobs" in DURABLE_JOB_COLLECTIONS


def test_job_identity_is_deterministic_and_cache_sensitive():
    first = _job_id(corpus_id="corpus", parent_id="parent", cache_key="cache:a")
    replay = _job_id(corpus_id="corpus", parent_id="parent", cache_key="cache:a")
    changed = _job_id(corpus_id="corpus", parent_id="parent", cache_key="cache:b")

    assert first == replay
    assert first.startswith("sha256:")
    assert first != changed


def test_phase1c_selection_is_deterministic_and_never_repurchases():
    planned = _planned_packets(8)
    excluded = {"parent:000", "parent:002"}
    certified = {"parent:001", "parent:004"}

    first = _deterministic_fresh_selection(
        planned,
        excluded_parent_ids=excluded,
        certified_parent_ids=certified,
        limit=3,
    )
    replay = _deterministic_fresh_selection(
        planned,
        excluded_parent_ids=set(reversed(sorted(excluded))),
        certified_parent_ids=set(reversed(sorted(certified))),
        limit=3,
    )

    assert [row.item.parent_id for row in first] == [
        "parent:003",
        "parent:005",
        "parent:006",
    ]
    assert [row.job_id for row in replay] == [row.job_id for row in first]
    assert not ({row.item.parent_id for row in first} & (excluded | certified))
    with pytest.raises(PaidPassError, match="expected 9 packets"):
        _deterministic_fresh_selection(
            planned,
            excluded_parent_ids=excluded,
            certified_parent_ids=certified,
            limit=9,
        )


def test_persisted_selection_resume_is_exact_and_order_stable():
    planned = _planned_packets(5)
    resumed = _resolve_persisted_selection(
        planned,
        [{"job_id": "job:003"}, {"job_id": "job:001"}],
        selection_name=PHASE1C_SELECTION,
        expected_count=2,
    )

    assert [row.ordinal for row in resumed] == [1, 3]
    with pytest.raises(PaidPassError, match="contains duplicates"):
        _resolve_persisted_selection(
            planned,
            [{"job_id": "job:001"}, {"job_id": "job:001"}],
            selection_name=PHASE1C_SELECTION,
            expected_count=2,
        )
    with pytest.raises(PaidPassError, match="no longer maps"):
        _resolve_persisted_selection(
            planned,
            [{"job_id": "job:missing"}],
            selection_name=PHASE1C_SELECTION,
            expected_count=1,
        )
    with pytest.raises(PaidPassError, match="expected 3"):
        _resolve_persisted_selection(
            planned,
            [{"job_id": "job:001"}, {"job_id": "job:003"}],
            selection_name=PHASE1C_SELECTION,
            expected_count=3,
        )


def test_phase_release_requires_all_markers_and_one_canonical_checkpoint():
    canonical = {"mongo": 0, "qdrant": 12, "neo4j": 34}
    rows = [
        {
            PHASE1B_GREEN_FIELD: True,
            PHASE1B_CANONICAL_FIELD: canonical,
        }
        for _ in range(PHASE1B_LIMIT)
    ]
    assert (
        _validated_release_canonical(
            rows,
            phase_name="phase1b",
            expected_count=PHASE1B_LIMIT,
            green_field=PHASE1B_GREEN_FIELD,
            canonical_field=PHASE1B_CANONICAL_FIELD,
        )
        == canonical
    )

    rows[0][PHASE1B_GREEN_FIELD] = False
    with pytest.raises(PaidPassError, match="not green"):
        _validated_release_canonical(
            rows,
            phase_name="phase1b",
            expected_count=PHASE1B_LIMIT,
            green_field=PHASE1B_GREEN_FIELD,
            canonical_field=PHASE1B_CANONICAL_FIELD,
        )
    rows[0][PHASE1B_GREEN_FIELD] = True
    rows[-1][PHASE1B_CANONICAL_FIELD] = {"mongo": 1}
    with pytest.raises(PaidPassError, match="drifted"):
        _validated_release_canonical(
            rows,
            phase_name="phase1b",
            expected_count=PHASE1B_LIMIT,
            green_field=PHASE1B_GREEN_FIELD,
            canonical_field=PHASE1B_CANONICAL_FIELD,
        )


def test_senior_phase_ledger_counts_and_phase1c_bar_are_frozen():
    assert PHASE1_PURCHASED_COUNT == 12
    assert PHASE1B_LIMIT == 10
    assert PRE_PHASE1C_PURCHASED_COUNT == 22
    assert PHASE1C_LIMIT == 50
    assert PHASE1C_MIN_ACCEPTANCE == 0.95
    assert TAIL_RETRY_LIMIT == 5
    assert PHASE1C_READ_TIMEOUT_PAUSE_COUNT == 3


def test_tail_retry_uses_frozen_phase1_ledger_without_unavailable_prompt_fields():
    query = _phase1_tail_failure_query("corpus:mark")

    assert query["corpus_id"] == "corpus:mark"
    assert query["phase"] == "phase1"
    assert query["status"] == {"$in": sorted(FAILURE_STATUSES)}
    assert query["attempt_count"] == {"$gt": 0}
    assert "prompt_version" not in query
    assert "repair_prompt_version" not in query

    timeout_query = _phase1c_timeout_tail_query("corpus:mark")
    assert timeout_query["phase_selection"] == PHASE1C_SELECTION
    assert timeout_query["transport_error_class"] == "ReadTimeout"


def test_tail_retry_job_identity_is_distinct_and_authorization_scoped():
    normal = _job_id(
        corpus_id="corpus:mark",
        parent_id="parent:timeout",
        cache_key="cache:v6",
    )
    tail = _tail_retry_job_id(
        corpus_id="corpus:mark",
        parent_id="parent:timeout",
        cache_key="cache:v6",
    )
    replay = _tail_retry_job_id(
        corpus_id="corpus:mark",
        parent_id="parent:timeout",
        cache_key="cache:v6",
    )

    assert tail == replay
    assert tail != normal


def test_read_timeout_recurrence_pauses_at_three_total():
    rows = [
        {"transport_error_class": "ReadTimeout"},
        {"transport_error_class": "ReadTimeout"},
        {"transport_error_class": "ReadTimeout"},
        {"transport_error_class": "ConnectError"},
    ]

    assert _read_timeout_count(rows) == 3


def test_certified_v5_acceptance_remains_valid_under_v6_skip_contract():
    item = _packet_from_parent(
        corpus_id="corpus:mark",
        corpus_name="mark",
        parent={
            "parent_id": "parent:one",
            "doc_id": "doc:one",
            "text": "A supported source statement.",
            "source_hash": "source:one",
            "validation_status": "valid",
            "child_ids": ["child:one"],
        },
        extraction_rows=[
            {
                "chunk_id": "child:one",
                "status": "ok",
                "schema_version": "polymath.extract.v1",
                "entities": [{"canonical_name": "Source", "entity_type": "CONCEPT"}],
            }
        ],
        max_entities=40,
    )
    digest = {
        "schema_version": "semantic_digest.v1",
        "parent_id": "parent:one",
        "summary": "A supported source statement.",
        "central_thesis": "The source supplies one supported statement.",
        "underlying_meanings": [],
        "domain_proposals": [],
        "frame_proposals": [],
        "latent_concepts": [],
        "motif_proposals": [],
        "conditions": [],
        "exceptions": [],
        "unresolved_interpretations": [],
    }
    input_hash = semantic_digest_input_hash(item.packet)
    prompt_hash = semantic_digest_prompt_hash(
        "parent-digest.v5", "parent-digest-repair.v2"
    )
    cache_key = semantic_digest_cache_key(
        input_hash=input_hash,
        model_id="openai/LongCat-2.0",
        schema_hash=semantic_digest_schema_hash(),
        prompt_hash=prompt_hash,
        runtime_version="longcat-runtime",
    )
    row = {
        "_id": cache_key,
        "status": "accepted_cache",
        "canonical_write": False,
        "digest": digest,
        "provenance": {
            "model_id": "openai/LongCat-2.0",
            "runtime": "provider",
            "runtime_version": "longcat-runtime",
            "tokenizer_id": "provider-managed",
            "chat_template_hash": namespace_hash("recipe", {"chat": "v1"}),
            "schema_version": "semantic_digest.v1",
            "schema_hash": semantic_digest_schema_hash(),
            "prompt_version": "parent-digest.v5",
            "prompt_hash": prompt_hash,
            "repair_prompt_version": "parent-digest-repair.v2",
            "repair_prompt_hash": semantic_digest_repair_prompt_hash(
                "parent-digest-repair.v2"
            ),
            "temperature": 0,
            "input_hash": input_hash,
            "output_hash": namespace_hash("body", digest),
            "capability_tier": "tier3",
            "capability_detection": (
                "explicit-tier3-forced-tool:runtime-capability-registry:"
                "longcat-api__longcat-2.0:provider_rejected"
            ),
            "attempts": 1,
            "repair_attempted": False,
            "cache_key": cache_key,
        },
    }
    config = SemanticGatewayConfig(
        model_id="openai/LongCat-2.0",
        runtime="provider",
        runtime_version="longcat-runtime",
        tokenizer_id="provider-managed",
        chat_template_hash=namespace_hash("recipe", {"chat": "v2"}),
        requested_tier="tier3",
    )

    assert (
        _valid_cached_row(
            row,
            item=item,
            config=config,
            cache_key=None,
        )
        is True
    )
    row["serving_eligible"] = False
    assert _valid_cached_row(row, item=item, config=config, cache_key=None) is False


def test_phase1_checkpoint_accepts_exactly_95_percent_with_cost_and_no_drift():
    canonical = _canonical_census()
    result = phase1_checkpoint(
        _rows(accepted=48, dead_letters=2, cost=0.04),
        canonical_before=canonical,
        canonical_after=canonical,
    )

    assert result["accepted_count"] == 48
    assert result["acceptance"] == 0.96
    assert result["cost_per_packet_usd"] == pytest.approx(0.04)
    assert result["acceptance_by_packet_size_band"]["above_canaried_max"] == {
        "threshold_bytes": CANARIED_MAX_PACKET_BYTES,
        "packet_count": 2,
        "accepted_count": 0,
        "dead_letter_count": 2,
        "acceptance": 0.0,
        "max_packet_bytes": 22_000,
    }
    assert result["all_green"] is True


def test_checkpoint_reports_cotenant_delta_without_failing_polymath_scope():
    before = _canonical_census()
    after = _canonical_census(
        qdrant_counts={
            "corpus_5a20bc21_naive": 10,
            "polymath_doc_summaries": 20,
            "hermes_memories": 609,
        }
    )

    result = phase1_checkpoint(
        _rows(accepted=48, dead_letters=2, cost=0.04),
        canonical_before=before,
        canonical_after=after,
    )

    assert result["canonical_census_scope_valid"] is True
    assert result["canonical_drift_zero"] is True
    assert result["ambient_qdrant_collection_deltas"] == {
        "hermes_memories": {"before": 608, "after": 609, "delta": 1}
    }
    assert result["all_green"] is True


def test_checkpoint_keeps_actual_incomplete_but_accepts_bounded_exposure():
    canonical = _canonical_census()
    rows = _rows(accepted=48, dead_letters=2, cost=0.04)
    rows[-1].update(
        {
            "actual_cost_usd": None,
            "cost_complete": False,
            "failure_class": "transport_attempt_1",
            "transport_error_class": "ReadTimeout",
            "unpriced_exposure_upper_bound_usd": UNPRICED_EXPOSURE_BOUND_USD,
            "cost_accounting_basis": UNPRICED_EXPOSURE_BASIS,
        }
    )

    result = paid_phase_checkpoint(
        rows,
        target_count=50,
        minimum_acceptance=0.95,
        canonical_before=canonical,
        canonical_after=canonical,
    )

    assert result["acceptance"] == 0.96
    assert result["cost_complete"] is False
    assert result["budget_accounting_complete"] is True
    assert result["cost_accounting_state"] == "complete_with_bounded_exposure"
    assert result["unpriced_exposure_count"] == 1
    assert result["bounded_exposure_usd"] == 0.06
    assert result["cost_ceiling_basis_usd"] == pytest.approx(2.02)
    assert result["cost_ceiling_basis_per_packet_usd"] == pytest.approx(0.0404)
    assert result["all_green"] is True


def test_unbounded_missing_cost_remains_incomplete_and_fails_closed():
    rows = _rows(accepted=2, dead_letters=1)
    rows[-1].update({"actual_cost_usd": None, "cost_complete": False})

    ledger = _cost_accounting(rows)

    assert ledger["actual_cost_complete"] is False
    assert ledger["budget_accounting_complete"] is False
    assert ledger["cost_accounting_state"] == "incomplete"


def test_bounded_success_exposure_closes_budget_without_guessing_actual_cost():
    card = ProviderPriceCard(
        schema_version="provider-price-card.v1",
        route_id="longcat",
        model_id="openai/LongCat-2.0",
        api_base="https://api.longcat.chat/openai/v1",
        price_unit_tokens=1_000_000,
        uncached_input_usd=0.7,
        output_usd=2.8,
        source_checked_at="2026-07-15T00:00:00Z",
        source_url="https://example.invalid/card",
    )
    fields = _bounded_success_exposure_fields(
        planned=_planned_packets(1)[0],
        provider_calls=2,
        provider_price_card=card,
        max_output_tokens=8192,
    )

    assert fields["cost_accounting_basis"] == BOUNDED_SUCCESS_EXPOSURE_BASIS
    assert fields["provider_calls"] == 2
    assert fields["actual_cost_usd"] is None
    assert fields["cost_complete"] is False
    assert fields["cost_exposure_call_count"] == 2
    assert (
        fields["cost_reservation_upper_bound_usd"]
        == fields["unpriced_exposure_upper_bound_usd"]
    )
    assert Decimal(str(fields["unpriced_exposure_upper_bound_usd"])) > 0
    ledger = _cost_accounting([fields])
    assert ledger["budget_accounting_complete"] is True
    assert ledger["actual_cost_complete"] is False
    assert ledger["cost_accounting_state"] == "complete_with_bounded_exposure"

    mismatched = dict(fields, cost_exposure_call_count=1)
    assert _cost_accounting([mismatched])["budget_accounting_complete"] is False


@pytest.mark.asyncio
async def test_success_without_transport_telemetry_persists_attempt_bound(monkeypatch):
    card = ProviderPriceCard(
        schema_version="provider-price-card.v1",
        route_id="longcat",
        model_id="openai/LongCat-2.0",
        api_base="https://api.longcat.chat/openai/v1",
        price_unit_tokens=1_000_000,
        uncached_input_usd=0.7,
        output_usd=2.8,
        source_checked_at="2026-07-15T00:00:00Z",
        source_url="https://example.invalid/card",
    )
    planned = _planned_packets(1)[0]
    captured: dict = {}

    class FakeTransport:
        call_telemetry: list[dict] = []

    class FakeGateway:
        def __init__(self, **_kwargs):
            pass

        async def generate(self, **_kwargs):
            return SimpleNamespace(
                cache_hit=False,
                provenance=SimpleNamespace(
                    attempts=2,
                    output_hash="sha256:output",
                    repair_attempted=True,
                ),
            )

    async def fake_persist(_db, *, claimed, status, fields):
        captured.update({"claimed": claimed, "status": status, "fields": fields})

    monkeypatch.setattr(paid_pass_module, "LiteLLMProxyTransport", FakeTransport)
    monkeypatch.setattr(paid_pass_module, "SemanticGateway", FakeGateway)
    monkeypatch.setattr(paid_pass_module, "_persist_terminal_job", fake_persist)
    monkeypatch.setattr(
        paid_pass_module,
        "_result_receipt",
        lambda *_args, **_kwargs: {
            "provider_calls": 0,
            "usage": {},
            "actual_cost_usd": None,
            "cost_complete": False,
            "provenance_complete": True,
            "semantic_validation_errors": [],
        },
    )

    result = await paid_pass_module._run_claimed_job(
        object(),
        claimed={"job_id": planned.job_id, "runner": "test"},
        planned=planned,
        config=SimpleNamespace(max_tokens=8192),
        route=SimpleNamespace(),
        provider_price_card=card,
    )

    assert result["provider_calls"] == 2
    assert result["actual_cost_usd"] is None
    assert result["cost_complete"] is False
    assert captured["status"] == "succeeded"
    assert captured["fields"]["cost_accounting_basis"] == (
        BOUNDED_SUCCESS_EXPOSURE_BASIS
    )
    assert captured["fields"]["cost_exposure_call_count"] == 2
    assert (
        captured["fields"]["cost_reservation_upper_bound_usd"]
        == captured["fields"]["unpriced_exposure_upper_bound_usd"]
    )


def test_phase1_checkpoint_fails_each_hard_gate_independently():
    canonical = _canonical_census()
    low_acceptance = phase1_checkpoint(
        _rows(accepted=47, dead_letters=3),
        canonical_before=canonical,
        canonical_after=canonical,
    )
    high_cost = phase1_checkpoint(
        _rows(accepted=50, cost=0.061),
        canonical_before=canonical,
        canonical_after=canonical,
    )
    drift = phase1_checkpoint(
        _rows(accepted=50),
        canonical_before=canonical,
        canonical_after=_canonical_census(neo4j_nodes=31),
    )

    assert low_acceptance["acceptance_green"] is False
    assert low_acceptance["all_green"] is False
    assert high_cost["cost_green"] is False
    assert high_cost["all_green"] is False
    assert drift["canonical_drift_zero"] is False
    assert drift["all_green"] is False


def test_phase1b_target_corpus_bar_is_preregistered_at_nine_of_ten():
    canonical = _canonical_census()
    green = paid_phase_checkpoint(
        _rows(accepted=9, dead_letters=1)[:10],
        target_count=10,
        minimum_acceptance=0.90,
        canonical_before=canonical,
        canonical_after=canonical,
    )
    red = paid_phase_checkpoint(
        _rows(accepted=8, dead_letters=2)[:10],
        target_count=10,
        minimum_acceptance=0.90,
        canonical_before=canonical,
        canonical_after=canonical,
    )

    assert green["acceptance"] == 0.9
    assert green["all_green"] is True
    assert red["acceptance"] == 0.8
    assert red["all_green"] is False


def test_phase2_auto_stops_on_rolling_acceptance_consecutive_dlq_and_cost():
    rolling = _rows(accepted=44, dead_letters=6)
    assert (
        phase2_auto_stop_reason(
            rolling, cumulative_cost_usd=1.0, cost_ceiling_usd=49.45
        )
        == "rolling_acceptance_below_90_percent"
    )

    five_dlq = _rows(accepted=4, dead_letters=5)
    assert all(row["status"] in FAILURE_STATUSES for row in five_dlq[-5:])
    assert (
        phase2_auto_stop_reason(
            five_dlq, cumulative_cost_usd=1.0, cost_ceiling_usd=49.45
        )
        == "five_consecutive_terminal_dlqs"
    )

    assert (
        phase2_auto_stop_reason(
            _rows(accepted=3), cumulative_cost_usd=49.45, cost_ceiling_usd=49.45
        )
        == "cumulative_cost_ceiling_reached"
    )


def test_phase2_auto_stop_fails_closed_on_missing_cost_telemetry():
    rows = _rows(accepted=3)
    rows[1]["cost_complete"] = False

    assert (
        phase2_auto_stop_reason(rows, cumulative_cost_usd=0.1, cost_ceiling_usd=49.45)
        == "cost_telemetry_incomplete"
    )


def test_phase2_auto_stop_accepts_separately_bounded_transport_exposure():
    rows = _rows(accepted=3)
    rows[1].update(
        {
            "actual_cost_usd": None,
            "cost_complete": False,
            "unpriced_exposure_upper_bound_usd": UNPRICED_EXPOSURE_BOUND_USD,
            "cost_accounting_basis": UNPRICED_EXPOSURE_BASIS,
        }
    )

    assert (
        phase2_auto_stop_reason(rows, cumulative_cost_usd=0.16, cost_ceiling_usd=49.45)
        is None
    )
