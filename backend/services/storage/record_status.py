"""Storage status helpers shared by retrieval and deletion code.

Legacy corpora predate an explicit ``status`` field on documents, parents, and
chunks. For reads, missing status is active. New deletes mark rows deleted, so
the same helper keeps current data queryable while excluding lifecycle tombstones.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

ACTIVE_STATUS = "active"
DELETING_STATUS = "deleting"
DELETED_STATUS = "deleted"


def active_record_clause(field: str = "status") -> dict[str, Any]:
    return {"$or": [{field: {"$exists": False}}, {field: ACTIVE_STATUS}]}


def with_active_records(query: dict[str, Any] | None, field: str = "status") -> dict[str, Any]:
    base = deepcopy(query or {})
    clause = active_record_clause(field)
    if not base:
        return clause
    return {"$and": [base, clause]}


def mark_active(row: dict[str, Any]) -> dict[str, Any]:
    row["status"] = ACTIVE_STATUS
    return row
