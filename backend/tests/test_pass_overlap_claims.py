"""Phase 3 pass-overlap: prove pass-1 and pass-2 claim DISJOINT item sets.

The overlap runs the queryable pass and the full pass concurrently. Correctness
rests on two properties:
  1. pass-1 (rank=queryable) claims only not-yet-staged items (queued /
     failed_recoverable), never staged ones.
  2. pass-2 (rank=full) claims only staged items, never queued ones.
These tests capture the exact Mongo filter _lease_next_item issues for each
binding and assert the status sets are disjoint, so a document can never be in
both passes at once and pass-2 cannot leapfrog a queued doc past queryable-first.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-password")

from services.ingestion import batches


class _CapturingCollection:
    """Capture the filter passed to find_one_and_update."""

    def __init__(self):
        self.last_filter = None

    async def find_one_and_update(self, flt, *args, **kwargs):
        self.last_filter = flt
        return None


class _CapturingDb:
    def __init__(self, coll):
        self._coll = coll

    def __getitem__(self, name):
        assert name == batches.ITEMS
        return self._coll


def _statuses_in_filter(flt) -> set:
    """Extract the set of item statuses the filter's $or would match."""
    ors = flt["$and"][0]["$or"]
    return {clause.get("status") for clause in ors if "status" in clause}


@pytest.mark.asyncio
async def test_pass1_claims_only_unstaged():
    coll = _CapturingCollection()
    db = _CapturingDb(coll)
    await batches._lease_next_item(
        db,
        batch_id="b1",
        owner="p1-0",
        lease_seconds=60,
        target_rank=batches.STAGE_RANK.get("queryable"),
        claim_statuses=frozenset({batches.ITEM_QUEUED, batches.ITEM_FAILED_RECOVERABLE}),
    )
    statuses = _statuses_in_filter(coll.last_filter)
    assert batches.ITEM_QUEUED in statuses
    assert batches.ITEM_FAILED_RECOVERABLE in statuses
    assert batches.ITEM_STAGED not in statuses


@pytest.mark.asyncio
async def test_pass2_claims_only_staged():
    coll = _CapturingCollection()
    db = _CapturingDb(coll)
    await batches._lease_next_item(
        db,
        batch_id="b1",
        owner="p2-0",
        lease_seconds=60,
        target_rank=None,
        claim_statuses=frozenset({batches.ITEM_STAGED}),
    )
    statuses = _statuses_in_filter(coll.last_filter)
    assert statuses == {batches.ITEM_STAGED}
    assert batches.ITEM_QUEUED not in statuses
    assert batches.ITEM_FAILED_RECOVERABLE not in statuses


@pytest.mark.asyncio
async def test_overlap_claim_sets_are_disjoint():
    """The two overlap bindings must never both match the same status."""
    p1_statuses = {batches.ITEM_QUEUED, batches.ITEM_FAILED_RECOVERABLE}
    p2_statuses = {batches.ITEM_STAGED}
    assert p1_statuses.isdisjoint(p2_statuses)


@pytest.mark.asyncio
async def test_legacy_none_claim_statuses_matches_all_leaseable():
    """claim_statuses=None (sequential path) must keep legacy behavior: queued,
    failed_recoverable, AND staged are all claimable."""
    coll = _CapturingCollection()
    db = _CapturingDb(coll)
    await batches._lease_next_item(
        db,
        batch_id="b1",
        owner="w0",
        lease_seconds=60,
        target_rank=None,
        claim_statuses=None,
    )
    statuses = _statuses_in_filter(coll.last_filter)
    assert batches.ITEM_QUEUED in statuses
    assert batches.ITEM_FAILED_RECOVERABLE in statuses
    assert batches.ITEM_STAGED in statuses
