"""Exact source identity audit for corpus ingestion.

Near-duplicate detection finds similar text. This audit is stricter: it reports
documents that share the same deterministic source key or byte content hash, and
documents that predate source identity metadata.
"""

from __future__ import annotations

from typing import Any

from services.storage.record_status import with_active_records

DOC_PROJECTION = {
    "_id": 0,
    "doc_id": 1,
    "filename": 1,
    "title": 1,
    "ingest_stage": 1,
    "source_key": 1,
    "source_kind": 1,
    "source_identity.source_key": 1,
    "source_identity.source_kind": 1,
    "source_identity.content_sha256": 1,
    "source_identity.identity_version": 1,
    "content_sha256": 1,
    "source_file_hash": 1,
    "write_state.mongo_written": 1,
    "write_state.qdrant_written": 1,
    "write_state.neo4j_written": 1,
    "write_state.verified": 1,
}


DOC_AGG_CARD = {
    "doc_id": "$doc_id",
    "filename": "$filename",
    "title": "$title",
    "ingest_stage": "$ingest_stage",
    "source_key": "$source_key",
    "source_kind": "$source_kind",
    "source_identity": "$source_identity",
    "content_sha256": "$content_sha256",
    "source_file_hash": "$source_file_hash",
    "write_state": "$write_state",
}

SOURCE_KEY_FIELDS = ("source_key", "source_identity.source_key")
CONTENT_HASH_FIELDS = (
    "source_identity.content_sha256",
    "content_sha256",
    "source_file_hash",
)

_CANONICAL_STAGE_RANK = {
    "fully_enriched": 90,
    "complete": 90,
    "queryable_with_pending_summary": 80,
    "queryable_with_pending_graph": 75,
    "queryable_with_pending_summary_and_graph": 70,
    "queryable": 70,
    "graph_promoted": 65,
    "promoted": 65,
    "indexed": 55,
    "extracted": 45,
    "chunked": 35,
    "parsed": 25,
    "failed": -10,
    "skipped_duplicate": -20,
}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _has_any_identity_field_query(fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        "$or": [
            {field: {"$exists": True, "$nin": [None, ""]}}
            for field in fields
        ]
    }


def _missing_all_identity_fields_query(fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        "$and": [
            {
                "$or": [
                    {field: {"$exists": False}},
                    {field: None},
                    {field: ""},
                ]
            }
            for field in fields
        ]
    }


def _first_non_empty_expr(fields: tuple[str, ...]) -> Any:
    expr: Any = None
    for field in reversed(fields):
        expr = {
            "$cond": [
                {
                    "$and": [
                        {"$ne": [f"${field}", None]},
                        {"$ne": [f"${field}", ""]},
                    ]
                },
                f"${field}",
                expr,
            ]
        }
    return expr


def _doc_card(row: dict[str, Any]) -> dict[str, Any]:
    identity = row.get("source_identity") or {}
    write_state = row.get("write_state") or {}
    return {
        "doc_id": row.get("doc_id"),
        "filename": row.get("filename"),
        "title": row.get("title"),
        "ingest_stage": row.get("ingest_stage"),
        "source_key": row.get("source_key") or identity.get("source_key"),
        "source_kind": row.get("source_kind") or identity.get("source_kind"),
        "content_sha256": (
            identity.get("content_sha256")
            or row.get("content_sha256")
            or row.get("source_file_hash")
        ),
        "identity_version": identity.get("identity_version"),
        "write_state": {
            "mongo_written": bool(write_state.get("mongo_written")),
            "qdrant_written": bool(write_state.get("qdrant_written")),
            "neo4j_written": bool(write_state.get("neo4j_written")),
            "verified": bool(write_state.get("verified")),
        },
    }


def _canonical_sort_key(card: dict[str, Any]) -> tuple[Any, ...]:
    write_state = card.get("write_state") or {}
    stage = str(card.get("ingest_stage") or "")
    # Prefer the document that already owns the most complete artifacts. The
    # final tie-breaker is lexical so exact duplicate groups are stable.
    return (
        -int(bool(write_state.get("mongo_written"))),
        -int(bool(write_state.get("qdrant_written"))),
        -int(bool(write_state.get("neo4j_written"))),
        -int(bool(write_state.get("verified"))),
        -_CANONICAL_STAGE_RANK.get(stage, 0),
        str(card.get("doc_id") or ""),
    )


def _duplicate_group_action(
    docs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a deterministic non-destructive action for an exact duplicate group."""

    cards = [_doc_card(doc) for doc in docs]
    content_hashes = sorted(
        {
            str(card.get("content_sha256") or "")
            for card in cards
            if str(card.get("content_sha256") or "").strip()
        }
    )
    if not cards:
        return {
            "canonical_doc_id": None,
            "canonical_doc": None,
            "duplicate_doc_ids": [],
            "content_hash_count": 0,
            "source_key_collision": False,
            "recommended_action": "inspect_empty_duplicate_group",
        }
    canonical = sorted(cards, key=_canonical_sort_key)[0]
    canonical_doc_id = canonical.get("doc_id")
    duplicate_doc_ids = [
        str(card.get("doc_id"))
        for card in cards
        if card.get("doc_id") and card.get("doc_id") != canonical_doc_id
    ]
    source_key_collision = len(content_hashes) > 1
    return {
        "canonical_doc_id": canonical_doc_id,
        "canonical_doc": canonical,
        "duplicate_doc_ids": duplicate_doc_ids,
        "content_hash_count": len(content_hashes),
        "source_key_collision": source_key_collision,
        "recommended_action": (
            "repair_source_identity_collision"
            if source_key_collision
            else "reuse_canonical_artifacts_for_exact_duplicate"
            if duplicate_doc_ids
            else "no_duplicate_action_needed"
        ),
    }


def summarize_identity_audit(
    *,
    corpus_id: str,
    doc_total: int,
    source_keyed_documents: int,
    content_hash_documents: int,
    duplicate_source_key_groups: list[dict[str, Any]] | None = None,
    duplicate_content_hash_groups: list[dict[str, Any]] | None = None,
    missing_source_identity: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    duplicate_source_key_groups = duplicate_source_key_groups or []
    duplicate_content_hash_groups = duplicate_content_hash_groups or []
    missing_source_identity = missing_source_identity or []
    duplicate_source_key_docs = sum(_int(row.get("doc_count")) for row in duplicate_source_key_groups)
    duplicate_content_hash_docs = sum(_int(row.get("doc_count")) for row in duplicate_content_hash_groups)
    source_key_collision_groups = [
        row for row in duplicate_source_key_groups if row.get("source_key_collision")
    ]
    source_key_collision_docs = sum(_int(row.get("doc_count")) for row in source_key_collision_groups)
    missing_count = max(_int(doc_total) - _int(source_keyed_documents), 0)
    if duplicate_source_key_groups or duplicate_content_hash_groups:
        status = "needs_review"
    elif missing_count:
        status = "incomplete_identity"
    else:
        status = "clear"
    return {
        "corpus_id": corpus_id,
        "status": status,
        "doc_total": _int(doc_total),
        "source_keyed_documents": _int(source_keyed_documents),
        "content_hash_documents": _int(content_hash_documents),
        "missing_source_identity_count": missing_count,
        "duplicate_source_key_group_count": len(duplicate_source_key_groups),
        "duplicate_source_key_doc_count": duplicate_source_key_docs,
        "source_key_collision_group_count": len(source_key_collision_groups),
        "source_key_collision_doc_count": source_key_collision_docs,
        "duplicate_content_hash_group_count": len(duplicate_content_hash_groups),
        "duplicate_content_hash_doc_count": duplicate_content_hash_docs,
        "duplicate_source_key_groups": duplicate_source_key_groups,
        "duplicate_content_hash_groups": duplicate_content_hash_groups,
        "missing_source_identity": missing_source_identity,
    }


def _duplicate_group_pipeline(
    *,
    corpus_id: str,
    key_fields: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    return [
        {
            "$match": with_active_records(
                {
                    "corpus_id": corpus_id,
                    **_has_any_identity_field_query(key_fields),
                }
            )
        },
        {"$addFields": {"_identity_group_key": _first_non_empty_expr(key_fields)}},
        {
            "$group": {
                "_id": "$_identity_group_key",
                "doc_count": {"$sum": 1},
                "docs": {"$push": DOC_AGG_CARD},
            }
        },
        {"$match": {"_id": {"$exists": True, "$nin": [None, ""]}}},
        {"$match": {"doc_count": {"$gt": 1}}},
        {"$sort": {"doc_count": -1, "_id": 1}},
        {"$limit": limit},
    ]


async def audit_corpus_idempotency(
    db: Any,
    *,
    corpus_id: str,
    group_limit: int = 25,
    missing_limit: int = 25,
) -> dict[str, Any]:
    """Return exact source/content identity gaps and duplicate groups."""

    group_limit = max(1, min(int(group_limit or 25), 200))
    missing_limit = max(1, min(int(missing_limit or 25), 200))
    doc_match = with_active_records({"corpus_id": corpus_id})
    source_key_query = with_active_records(
        {"corpus_id": corpus_id, **_has_any_identity_field_query(SOURCE_KEY_FIELDS)}
    )
    content_hash_query = with_active_records(
        {"corpus_id": corpus_id, **_has_any_identity_field_query(CONTENT_HASH_FIELDS)}
    )
    missing_query = with_active_records(
        {
            "corpus_id": corpus_id,
            **_missing_all_identity_fields_query(SOURCE_KEY_FIELDS),
        }
    )
    source_key_rows = await db["documents"].aggregate(
        _duplicate_group_pipeline(
            corpus_id=corpus_id,
            key_fields=SOURCE_KEY_FIELDS,
            limit=group_limit,
        )
    ).to_list(length=group_limit)
    content_hash_rows = await db["documents"].aggregate(
        _duplicate_group_pipeline(
            corpus_id=corpus_id,
            key_fields=CONTENT_HASH_FIELDS,
            limit=group_limit,
        )
    ).to_list(length=group_limit)
    duplicate_source_key_groups = [
        {
            "source_key": row.get("_id"),
            "doc_count": _int(row.get("doc_count")),
            "docs": [_doc_card(doc) for doc in row.get("docs") or []],
            **_duplicate_group_action(row.get("docs") or []),
        }
        for row in source_key_rows
    ]
    duplicate_content_hash_groups = [
        {
            "content_sha256": row.get("_id"),
            "doc_count": _int(row.get("doc_count")),
            "docs": [_doc_card(doc) for doc in row.get("docs") or []],
            **_duplicate_group_action(row.get("docs") or []),
        }
        for row in content_hash_rows
    ]
    missing_rows = await db["documents"].find(
        missing_query,
        DOC_PROJECTION,
    ).limit(missing_limit).to_list(length=missing_limit)
    return summarize_identity_audit(
        corpus_id=corpus_id,
        doc_total=await db["documents"].count_documents(doc_match),
        source_keyed_documents=await db["documents"].count_documents(source_key_query),
        content_hash_documents=await db["documents"].count_documents(content_hash_query),
        duplicate_source_key_groups=duplicate_source_key_groups,
        duplicate_content_hash_groups=duplicate_content_hash_groups,
        missing_source_identity=[_doc_card(row) for row in missing_rows],
    )
