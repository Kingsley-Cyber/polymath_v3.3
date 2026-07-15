from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts.semantic_gateway_mark_paid_pass import PaidPassError, PlannedPacket
from scripts.semantic_gateway_mark_prose_phase2 import (
    AUTHORIZATION_REFERENCE,
    ESCALATED_CONCURRENCY,
    INITIAL_CONCURRENCY,
    REMAINING_UMBRELLA_USD,
    REBUY_ORDINALS,
    _assert_go_contract,
    _phase2_selection,
    _persist_rebuy_supersessions,
    phase2_prose_concurrency,
    phase2_prose_stop_reason,
    receipt_accounting_closes,
)


def _planned(count: int = 600) -> list[PlannedPacket]:
    return [
        PlannedPacket(
            item=SimpleNamespace(
                parent_id=f"parent:{index:03d}",
                doc_id=f"doc:{index:03d}",
                packet={"parent_id": f"parent:{index:03d}"},
            ),
            ordinal=index,
            job_id=f"old-job:{index:03d}",
            cache_key=f"cache:{index:03d}",
            input_hash=f"input:{index:03d}",
            packet_bytes=1000 + index,
        )
        for index in range(count)
    ]


def _row(index: int, status: str = "succeeded", **updates):
    row = {
        "ordinal": index,
        "status": status,
        "completed_at": datetime(2026, 7, 15) + timedelta(seconds=index),
        "actual_cost_usd": 0.02,
        "cost_complete": True,
    }
    row.update(updates)
    return row


def test_selection_excludes_ledger_union_but_rebuys_exact_two():
    planned = _planned()
    selected = _phase2_selection(
        planned,
        corpus_id="corpus:mark",
        attempted_parent_ids={"parent:000", "parent:060", "parent:569"},
        certified_parent_ids={"parent:001"},
        explicitly_excluded_parent_ids={"parent:002", "parent:003"},
        rebuy_parent_ids={"parent:060", "parent:569"},
    )

    parent_ids = {row.item.parent_id for row in selected}
    assert {"parent:060", "parent:569"} <= parent_ids
    assert not {"parent:000", "parent:001", "parent:002", "parent:003"} & parent_ids
    assert len(selected) == 596
    assert len({row.job_id for row in selected}) == len(selected)
    assert all(row.job_id.startswith("sha256:") for row in selected)


def test_selection_rejects_out_of_scope_or_already_certified_rebuy():
    with pytest.raises(PaidPassError, match="outside B1 eligibility"):
        _phase2_selection(
            _planned(100),
            corpus_id="corpus:mark",
            attempted_parent_ids=set(),
            certified_parent_ids=set(),
            explicitly_excluded_parent_ids=set(),
            rebuy_parent_ids={"parent:060", "parent:569"},
        )
    with pytest.raises(PaidPassError, match="already has a certified prose"):
        _phase2_selection(
            _planned(),
            corpus_id="corpus:mark",
            attempted_parent_ids=set(),
            certified_parent_ids={"parent:060"},
            explicitly_excluded_parent_ids=set(),
            rebuy_parent_ids={"parent:060", "parent:569"},
        )


def test_rebuy_selection_uses_durable_parent_ids_not_ordinal_coincidence():
    planned = _planned()
    selected = _phase2_selection(
        planned,
        corpus_id="corpus:mark",
        attempted_parent_ids={"parent:060", "parent:570"},
        certified_parent_ids=set(),
        explicitly_excluded_parent_ids=set(),
        rebuy_parent_ids={"parent:060", "parent:570"},
    )

    assert {"parent:060", "parent:570"} <= {row.item.parent_id for row in selected}


def test_population_accounting_is_set_exact_not_count_only():
    eligible = {f"parent:{index}" for index in range(8)}
    attempted = {"parent:0", "parent:1"}
    certified = {"parent:2"}
    structured = {"parent:3", "parent:4"}
    rebuys = {"parent:1", "parent:4"}
    expected = {"parent:1", "parent:4", "parent:5", "parent:6", "parent:7"}

    assert receipt_accounting_closes(
        eligible_ids=eligible,
        selected_ids=expected,
        attempted_ids=attempted,
        certified_ids=certified,
        explicitly_excluded_ids=structured,
        rebuy_ids=rebuys,
    )
    assert not receipt_accounting_closes(
        eligible_ids=eligible,
        selected_ids=expected - {"parent:7"} | {"parent:0"},
        attempted_ids=attempted,
        certified_ids=certified,
        explicitly_excluded_ids=structured,
        rebuy_ids=rebuys,
    )


def test_stop_reason_enforces_completion_order_rolling_window():
    rows = [_row(index) for index in range(44)]
    rows.extend(_row(44 + index, "dead_letter") for index in range(6))
    assert phase2_prose_stop_reason(rows) == "rolling_acceptance_below_90_percent"

    # Storage/ordinal order is irrelevant; completed_at remains authoritative.
    assert (
        phase2_prose_stop_reason(list(reversed(rows)))
        == "rolling_acceptance_below_90_percent"
    )


def test_stop_reason_enforces_five_dlq_streak_and_two_readtimeouts():
    five_dlq = [_row(index) for index in range(4)] + [
        _row(4 + index, "dead_letter") for index in range(5)
    ]
    assert phase2_prose_stop_reason(five_dlq) == "five_consecutive_terminal_dlqs"

    timeouts = [
        _row(0, "dead_letter", transport_error_class="ReadTimeout"),
        _row(1),
        _row(2, "dead_letter", transport_error_class="ReadTimeout"),
    ]
    assert phase2_prose_stop_reason(timeouts) == "read_timeout_recurrence_pause"


def test_stop_reason_fails_closed_on_unbounded_cost_but_accepts_bound():
    missing = [_row(0)]
    missing[0].update(actual_cost_usd=None, cost_complete=False)
    assert phase2_prose_stop_reason(missing) == "cost_telemetry_incomplete"

    bounded = [_row(0)]
    bounded[0].update(
        actual_cost_usd=None,
        cost_complete=False,
        unpriced_exposure_upper_bound_usd=0.06,
        cost_accounting_basis="bounded_transport_exposure.v1",
    )
    assert phase2_prose_stop_reason(bounded) is None


def test_concurrency_escalates_only_after_one_hundred_clean_completions():
    ninety_nine = [_row(index) for index in range(99)]
    hundred = [_row(index) for index in range(100)]
    one_failure = [_row(index) for index in range(100)]
    one_failure[80]["status"] = "dead_letter"

    assert phase2_prose_concurrency(ninety_nine) == INITIAL_CONCURRENCY
    assert phase2_prose_concurrency(hundred) == ESCALATED_CONCURRENCY
    assert phase2_prose_concurrency(one_failure) == INITIAL_CONCURRENCY


def _prepared_receipt():
    return SimpleNamespace(
        receipt={
            "all_green": True,
            "selection": {
                "target_count": 700,
                "selection_set_hash": "sha256:" + "a" * 64,
            },
            "provider_contract": {
                "prompt_hash": "sha256:" + "b" * 64,
                "repair_prompt_hash": "sha256:" + "c" * 64,
                "schema_hash": "sha256:" + "d" * 64,
            },
            "cost_authority": {"prior_cumulative_ceiling_basis_usd": "2.75"},
        }
    )


def test_exact_go_contract_accepts_only_sealed_identity_and_arithmetic():
    prepared = _prepared_receipt()
    _assert_go_contract(
        prepared,
        authorization_reference=AUTHORIZATION_REFERENCE,
        expected_selection_count=700,
        expected_selection_set_hash="sha256:" + "a" * 64,
        expected_prompt_hash="sha256:" + "b" * 64,
        expected_repair_prompt_hash="sha256:" + "c" * 64,
        expected_schema_hash="sha256:" + "d" * 64,
        expected_prior_basis_usd=Decimal("2.75"),
        remaining_authority_usd=REMAINING_UMBRELLA_USD,
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("authorization_reference", "wrong", "authorization reference"),
        ("expected_selection_count", 699, "selection count"),
        ("expected_selection_set_hash", "sha256:wrong", "selection set hash"),
        ("expected_prior_basis_usd", Decimal("2.76"), "prior basis"),
        ("remaining_authority_usd", Decimal("46.70"), "remaining authority"),
    ],
)
def test_exact_go_contract_rejects_each_material_drift(field, value, message):
    kwargs = {
        "authorization_reference": AUTHORIZATION_REFERENCE,
        "expected_selection_count": 700,
        "expected_selection_set_hash": "sha256:" + "a" * 64,
        "expected_prompt_hash": "sha256:" + "b" * 64,
        "expected_repair_prompt_hash": "sha256:" + "c" * 64,
        "expected_schema_hash": "sha256:" + "d" * 64,
        "expected_prior_basis_usd": Decimal("2.75"),
        "remaining_authority_usd": REMAINING_UMBRELLA_USD,
    }
    kwargs[field] = value
    with pytest.raises(PaidPassError, match=message):
        _assert_go_contract(_prepared_receipt(), **kwargs)


def test_rebuy_ordinals_are_the_two_owner_ruled_failures():
    assert REBUY_ORDINALS == (60, 569)


class _SupersessionCollection:
    def __init__(self, *, find_row=None):
        self.find_row = find_row
        self.bulk_ops = []
        self.updates = []

    async def find_one(self, query, projection):
        return self.find_row

    async def update_one(self, query, update):
        self.updates.append((query, update))
        return SimpleNamespace(matched_count=1)

    async def bulk_write(self, ops, ordered):
        assert ordered is True
        self.bulk_ops.extend(ops)
        return SimpleNamespace(upserted_count=len(ops), matched_count=0)


@pytest.mark.asyncio
async def test_rebuy_supersession_preserves_payload_and_disables_source_cache():
    replacement = _planned()[60]
    replacement.item.packet["corpus_id"] = "corpus:mark"
    jobs = _SupersessionCollection(find_row={"cache_key": replacement.cache_key})
    cache = _SupersessionCollection(find_row={"_id": replacement.cache_key})
    supersessions = _SupersessionCollection()
    db = {
        "semantic_digest_jobs": jobs,
        "semantic_digest_cache": cache,
        "semantic_digest_supersessions": supersessions,
    }

    count = await _persist_rebuy_supersessions(
        db,
        selected=[replacement],
        rebuy_sources={
            60: {
                "parent_id": replacement.item.parent_id,
                "replacement_ordinal": "60",
                "source_job_id": "job:v2",
                "source_cache_key": "cache:v2",
            }
        },
    )

    assert count == 1
    assert len(supersessions.bulk_ops) == 1
    assert cache.updates[0][0]["_id"] == "cache:v2"
    fields = cache.updates[0][1]["$set"]
    assert fields["serving_eligible"] is False
    assert fields["faithfulness_status"] == "rejected"
    assert fields["superseded_by_cache_key"] == replacement.cache_key
