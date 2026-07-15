from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest

from services.ingestion.summary_cost_control import (
    CALLS_COLLECTION,
    RUNS_COLLECTION,
    SummaryCostAuthorityRequired,
    SummaryCostCeilingExceeded,
    SummaryCostController,
    SummaryCostPriceCardError,
    list_price_nanos,
    load_summary_price_card,
    message_input_token_upper_bound,
)


class _UpdateResult:
    def __init__(self, matched_count: int) -> None:
        self.matched_count = matched_count


def _matches(row: dict, query: dict) -> bool:
    for key, expected in query.items():
        if key == "$expr":
            continue
        actual = row.get(key)
        if isinstance(expected, dict) and "$ne" in expected:
            value = expected["$ne"]
            if isinstance(actual, list) and value in actual:
                return False
            if not isinstance(actual, list) and actual == value:
                return False
        elif isinstance(actual, list):
            if expected not in actual:
                return False
        elif actual != expected:
            return False
    return True


def _apply(row: dict, update: dict) -> None:
    for key, value in (update.get("$setOnInsert") or {}).items():
        row.setdefault(key, deepcopy(value))
    for key, value in (update.get("$inc") or {}).items():
        row[key] = int(row.get(key) or 0) + int(value)
    for key, value in (update.get("$set") or {}).items():
        row[key] = deepcopy(value)
    for key, value in (update.get("$addToSet") or {}).items():
        bucket = row.setdefault(key, [])
        if value not in bucket:
            bucket.append(deepcopy(value))


class _Runs:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    async def update_one(self, query, update, upsert=False):
        async with self.lock:
            run_id = str(query.get("_id") or query.get("run_id") or "")
            row = self.rows.get(run_id)
            if row is None and upsert:
                row = {"_id": run_id}
                self.rows[run_id] = row
            if row is None or not _matches(row, query):
                return _UpdateResult(0)
            _apply(row, update)
            return _UpdateResult(1)

    async def find_one(self, query, projection=None):
        async with self.lock:
            row = next(
                (value for value in self.rows.values() if _matches(value, query)),
                None,
            )
            if row is None:
                return None
            out = deepcopy(row)
            if projection and projection.get("settled_reservation_ids") == 0:
                out.pop("settled_reservation_ids", None)
            return out

    async def find_one_and_update(self, query, update, **_kwargs):
        async with self.lock:
            run_id = str(query.get("_id") or query.get("run_id") or "")
            row = self.rows.get(run_id)
            if row is None or not _matches(row, query):
                return None
            addends = query["$expr"]["$lte"][0]["$add"]
            next_reservation = int(addends[-1])
            basis = int(row.get("actual_nanos") or 0) + int(
                row.get("reserved_nanos") or 0
            )
            if basis + next_reservation > int(row["authorized_nanos"]):
                return None
            _apply(row, update)
            return deepcopy(row)


class _Calls:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def insert_one(self, doc):
        self.rows[str(doc["reservation_id"])] = deepcopy(doc)
        return _UpdateResult(1)

    async def update_one(self, query, update, **_kwargs):
        row = self.rows.get(str(query.get("_id") or query.get("reservation_id") or ""))
        if row is None or not _matches(row, query):
            return _UpdateResult(0)
        _apply(row, update)
        return _UpdateResult(1)


class _DB:
    def __init__(self) -> None:
        self.collections = {
            RUNS_COLLECTION: _Runs(),
            CALLS_COLLECTION: _Calls(),
        }

    def __getitem__(self, name):
        return self.collections[name]


def test_deepseek_price_card_matches_root_and_v1_bases() -> None:
    root = load_summary_price_card(
        provider="deepseek",
        model="deepseek/deepseek-v4-flash",
        api_base="https://api.deepseek.com",
    )
    v1 = load_summary_price_card(
        provider="deepseek",
        model="deepseek/deepseek-v4-flash",
        api_base="https://api.deepseek.com/v1/",
    )
    assert root == v1
    assert root.uncached_input_usd == Decimal("0.14")
    assert root.output_usd == Decimal("0.28")
    assert list_price_nanos(root, input_tokens=10, output_tokens=20) == 7000


def test_uncertified_route_fails_closed() -> None:
    with pytest.raises(SummaryCostPriceCardError, match="no unique certified"):
        load_summary_price_card(
            provider="deepseek",
            model="deepseek/deepseek-v4-pro",
            api_base="https://api.deepseek.com/v1",
        )


def test_message_bound_counts_utf8_bytes_plus_fixed_overhead() -> None:
    ascii_bound = message_input_token_upper_bound(
        [{"role": "user", "content": "plain"}]
    )
    unicode_bound = message_input_token_upper_bound(
        [{"role": "user", "content": "plain 🧪"}]
    )
    assert ascii_bound > 1024
    assert unicode_bound > ascii_bound


@pytest.mark.asyncio
async def test_atomic_reservation_refuses_second_call_before_dispatch() -> None:
    db = _DB()
    controller = await SummaryCostController.open(
        db,
        run_id="run-1",
        corpus_id="corpus-1",
        user_id="user-1",
        authority_usd="0.0006",
    )
    kwargs = {
        "provider": "deepseek",
        "model": "deepseek/deepseek-v4-flash",
        "api_base": "https://api.deepseek.com/v1",
        "messages": [{"role": "user", "content": "bounded prompt"}],
        "max_output_tokens": 1024,
        "item_count": 1,
    }
    outcomes = await asyncio.gather(
        controller.reserve(**kwargs),
        controller.reserve(**kwargs),
        return_exceptions=True,
    )
    reservations = [value for value in outcomes if not isinstance(value, Exception)]
    refusals = [value for value in outcomes if isinstance(value, Exception)]
    assert len(reservations) == 1
    assert len(refusals) == 1
    assert isinstance(refusals[0], SummaryCostCeilingExceeded)
    snapshot = await controller.snapshot()
    assert snapshot["calls_reserved"] == 1
    assert snapshot["calls_refused"] == 1
    assert snapshot["status"] == "ceiling_exhausted"


@pytest.mark.asyncio
async def test_settlement_uses_list_price_and_is_idempotent() -> None:
    db = _DB()
    controller = await SummaryCostController.open(
        db,
        run_id="run-2",
        corpus_id="corpus-1",
        user_id="user-1",
        authority_usd="1",
    )
    reservation = await controller.reserve(
        provider="deepseek",
        model="deepseek/deepseek-v4-flash",
        api_base="https://api.deepseek.com",
        messages=[{"role": "user", "content": "bounded prompt"}],
        max_output_tokens=1024,
        item_count=1,
    )
    fields = await controller.settle(
        reservation,
        usage={"prompt_tokens": 10, "completion_tokens": 20},
    )
    await controller.settle(
        reservation,
        usage={"prompt_tokens": 10, "completion_tokens": 20},
    )
    snapshot = await controller.snapshot()
    assert fields["summary_cost_accounted_nanos"] == 7000
    assert snapshot["calls_completed"] == 1
    assert snapshot["accounted_cost_usd"] == "0.000007000"
    assert snapshot["reported_usage_list_price_usd"] == "0.000007000"
    assert snapshot["conservative_missing_usage_charge_usd"] == "0.000000000"
    assert snapshot["outstanding_reserved_usd"] == "0.000000000"


@pytest.mark.asyncio
async def test_missing_usage_charges_full_reservation() -> None:
    db = _DB()
    controller = await SummaryCostController.open(
        db,
        run_id="run-3",
        corpus_id="corpus-1",
        user_id="user-1",
        authority_usd="1",
    )
    reservation = await controller.reserve(
        provider="deepseek",
        model="deepseek/deepseek-v4-flash",
        api_base="https://api.deepseek.com/v1",
        messages=[{"role": "user", "content": "bounded prompt"}],
        max_output_tokens=1024,
        item_count=1,
    )
    fields = await controller.settle(
        reservation,
        usage=None,
        failure_class="ReadTimeout",
    )
    assert fields["summary_cost_accounted_nanos"] == reservation.reserved_nanos
    assert fields["summary_cost_usage_complete"] is False


@pytest.mark.asyncio
async def test_open_requires_positive_bounded_authority() -> None:
    for value in (None, 0, -1, "10000.000000001"):
        with pytest.raises(SummaryCostAuthorityRequired):
            await SummaryCostController.open(
                _DB(),
                run_id="run-invalid",
                corpus_id="corpus-1",
                user_id="user-1",
                authority_usd=value,
            )


class _ProviderResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"summary":"Durable reservations bound provider work '
                            'before dispatch and preserve an auditable cost record.",'
                            '"central_claim":"Reservations bound provider work.",'
                            '"key_points":[{"point":"The source describes a '
                            'pre-dispatch bound.","supporting_child_ids":["child-1"]}],'
                            '"concept_tags":["cost reservation"]}'
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 20},
        }


class _ProviderClient:
    calls = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, *args, **kwargs):
        self.__class__.calls += 1
        return _ProviderResponse()


class _FakeCostController:
    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.reserved: list[dict] = []
        self.settled: list[dict] = []

    async def reserve(self, **kwargs):
        if self.refuse:
            raise SummaryCostCeilingExceeded("test ceiling")
        self.reserved.append(kwargs)
        return object()

    async def settle(self, reservation, *, usage, failure_class=None):
        self.settled.append(
            {"reservation": reservation, "usage": usage, "failure_class": failure_class}
        )
        return {
            "summary_cost_run_id": "run-ghost-a",
            "summary_cost_reserved_nanos": 100,
            "summary_cost_accounted_nanos": 10,
        }


def _ghost_a_task():
    from services.ghost_a import SummaryTask

    return SummaryTask(
        parent_id="parent-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        source_tier="tier_a",
        text="The source describes a pre-dispatch bound for provider work.",
        source_child_ids=["child-1"],
        child_boundaries="[child-1]\nThe source describes a pre-dispatch bound.",
    )


@pytest.mark.asyncio
async def test_ghost_a_requires_controller_before_http(monkeypatch) -> None:
    from services import ghost_a

    _ProviderClient.calls = 0
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _ProviderClient)
    with pytest.raises(SummaryCostAuthorityRequired, match="durable summary cost"):
        await ghost_a.summarize_parents(
            [_ghost_a_task()],
            pool=[
                {
                    "model": "deepseek/deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "max_concurrent": 1,
                    "extra_params": {},
                }
            ],
            require_cost_control=True,
        )
    assert _ProviderClient.calls == 0


@pytest.mark.asyncio
async def test_ghost_a_reserves_then_settles_usage(monkeypatch) -> None:
    from services import ghost_a

    _ProviderClient.calls = 0
    controller = _FakeCostController()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _ProviderClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 0)
    results = await ghost_a.summarize_parents(
        [_ghost_a_task()],
        pool=[
            {
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "max_concurrent": 1,
                "extra_params": {},
            }
        ],
        cost_controller=controller,
        require_cost_control=True,
    )
    assert len(results) == 1
    assert _ProviderClient.calls == 1
    assert len(controller.reserved) == 1
    assert controller.reserved[0]["max_output_tokens"] >= 1024
    assert controller.settled[0]["usage"] == {
        "prompt_tokens": 30,
        "completion_tokens": 20,
    }


@pytest.mark.asyncio
async def test_ghost_a_ceiling_refusal_prevents_http(monkeypatch) -> None:
    from services import ghost_a

    _ProviderClient.calls = 0
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _ProviderClient)
    with pytest.raises(SummaryCostCeilingExceeded, match="test ceiling"):
        await ghost_a.summarize_parents(
            [_ghost_a_task()],
            pool=[
                {
                    "model": "deepseek/deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "max_concurrent": 1,
                    "extra_params": {},
                }
            ],
            cost_controller=_FakeCostController(refuse=True),
            require_cost_control=True,
        )
    assert _ProviderClient.calls == 0
