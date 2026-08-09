"""O4 — vector-omission conservation.

Every active chunk is either an eligible child vector or carries an explicit
BY_DESIGN omission receipt. The stamp writes the receipts; the verifier
refuses an unexplained gap between Mongo chunks and child vectors.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.ingestion.verify import stamp_by_design_vector_omissions


def _matches(row: dict, query: dict) -> bool:
    for key, cond in query.items():
        if key == "$and":
            if not all(_matches(row, sub) for sub in cond):
                return False
        elif key == "$or":
            if not any(_matches(row, sub) for sub in cond):
                return False
        elif isinstance(cond, dict) and any(op.startswith("$") for op in cond):
            value = _get_path(row, key)
            for op, operand in cond.items():
                if op == "$in" and value not in operand:
                    return False
                if op == "$nin" and value in operand:
                    return False
                if op == "$exists" and (value is not None) is not bool(operand):
                    return False
        else:
            if _get_path(row, key) != cond:
                return False
    return True


def _get_path(row: dict, path: str):
    node = row
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class _Chunks:
    def __init__(self, rows):
        self.rows = rows

    async def count_documents(self, query):
        return sum(_matches(row, query) for row in self.rows)

    async def update_many(self, query, update):
        modified = 0
        for row in self.rows:
            if _matches(row, query):
                for key, value in (update.get("$set") or {}).items():
                    row[key] = value
                modified += 1
        return SimpleNamespace(modified_count=modified)


class _Db(dict):
    pass


def _db(rows):
    db = _Db()
    db["chunks"] = _Chunks(rows)
    return db


def _rows():
    return [
        {"chunk_id": "c1", "doc_id": "doc", "corpus_id": "corpus",
         "chunk_kind": "body", "status": "active"},
        {"chunk_id": "c2", "doc_id": "doc", "corpus_id": "corpus",
         "chunk_kind": "appendix", "status": "active"},
        {"chunk_id": "c3", "doc_id": "doc", "corpus_id": "corpus",
         "chunk_kind": "front_matter"},  # legacy row without status
        {"chunk_id": "c4", "doc_id": "doc", "corpus_id": "corpus",
         "chunk_kind": "appendix", "status": "deleted"},  # tombstone
        {"chunk_id": "c5", "doc_id": "other-doc", "corpus_id": "corpus",
         "chunk_kind": "appendix", "status": "active"},  # other doc
    ]


@pytest.mark.asyncio
async def test_stamp_targets_only_active_noisy_chunks_of_the_doc() -> None:
    rows = _rows()
    stamped = await stamp_by_design_vector_omissions(
        _db(rows), doc_id="doc", corpus_id="corpus",
    )
    assert stamped == 2
    receipts = {row["chunk_id"]: row.get("vector_omitted_by_design") for row in rows}
    assert receipts["c1"] is None  # retrievable body chunk untouched
    assert receipts["c2"] == {
        "by_design": True, "reason": "noisy_kind_not_retrievable",
    }
    assert receipts["c3"] == {
        "by_design": True, "reason": "noisy_kind_not_retrievable",
    }
    assert receipts["c4"] is None  # tombstone untouched
    assert receipts["c5"] is None  # other document untouched


@pytest.mark.asyncio
async def test_stamp_is_idempotent() -> None:
    rows = _rows()
    db = _db(rows)
    first = await stamp_by_design_vector_omissions(db, doc_id="doc", corpus_id="corpus")
    second = await stamp_by_design_vector_omissions(db, doc_id="doc", corpus_id="corpus")
    assert (first, second) == (2, 0)


@pytest.mark.asyncio
async def test_conservation_identity_over_stamped_rows() -> None:
    # The verifier's 3b identity computed over the same fake rows:
    # eligible (non-noisy active) + stamped == active chunks.
    from services.ingestion.section_classifier import NOISY_KINDS
    from services.storage.record_status import with_active_records

    rows = _rows()
    db = _db(rows)
    active = {"doc_id": "doc", "corpus_id": "corpus"}
    total = await db["chunks"].count_documents(with_active_records(dict(active)))
    eligible = await db["chunks"].count_documents(with_active_records({
        **active,
        "$or": [
            {"chunk_kind": {"$exists": False}},
            {"chunk_kind": {"$nin": sorted(NOISY_KINDS)}},
        ],
    }))
    stamped = await db["chunks"].count_documents(with_active_records({
        **active, "vector_omitted_by_design.by_design": True,
    }))
    assert eligible + stamped != total  # unexplained before stamping

    await stamp_by_design_vector_omissions(db, doc_id="doc", corpus_id="corpus")
    stamped = await db["chunks"].count_documents(with_active_records({
        **active, "vector_omitted_by_design.by_design": True,
    }))
    assert eligible + stamped == total  # receipts close the gap
