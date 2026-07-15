import pytest

from services.graph.entity_dedup import apply as dedup_apply
from services.graph import junk_cleanup


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    async def single(self):
        return self.row


class FakeSession:
    def __init__(self):
        self.calls = []

    async def run(self, query, **params):
        self.calls.append((query, params))
        return FakeResult()


@pytest.mark.asyncio
async def test_entity_dedup_undo_batches_every_snapshot_edge_family():
    session = FakeSession()
    rows = [{"ordinal": idx} for idx in range(205)]
    snapshot = {
        "dup_id": "entity:dup",
        "survivor_id": "entity:survivor",
        "merge_run": "run-1",
        "dprops": {"entity_id": "entity:dup"},
        "mentions": rows,
        "rel_out": rows,
        "rel_in": rows,
        "self_loops": rows,
        "facts": rows,
    }

    await dedup_apply._undo_one(session, snapshot)

    unwind_batches = [
        params["rows"]
        for query, params in session.calls
        if "UNWIND $rows AS row" in query
    ]
    assert [len(batch) for batch in unwind_batches] == [100, 100, 5] * 5
    assert all(
        len(batch) <= dedup_apply.ENTITY_DEDUP_WRITE_BATCH_SIZE
        for batch in unwind_batches
    )
    assert all("LIMIT $batch_size" in query for query in dedup_apply._REPOINT)
    assert all("LIMIT $batch_size" in query for query in dedup_apply._CLEANUP_MOVES)


def test_junk_cleanup_orphan_fact_delete_is_transaction_bounded():
    assert "LIMIT $batch_size" in junk_cleanup._DELETE_ORPHAN_FACTS
    assert "collect(f)" not in junk_cleanup._DELETE_ORPHAN_FACTS
