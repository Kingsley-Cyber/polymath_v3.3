"""Bounded document-level summary repair.

Parent summaries are the retrieval breadth lane; document summaries are the
compact source card used for routing/orientation. This module makes the latter
repairable without re-ingesting documents.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from models.schemas import IngestionConfig
from services.ingestion.section_classifier import parent_summary_required_clause
from services.ingestion.summary_tree import (
    build_and_store_tree,
    sync_document_profile_from_existing_tree,
)
from services.ingestion.summary_tree_llm import summary_tree_llm_from_pool
from services.storage.record_status import with_active_records

SUMMARY_TEXT_CLAUSE: dict[str, Any] = {"summary": {"$exists": True, "$nin": [None, ""]}}
MISSING_DOCUMENT_SUMMARY_CLAUSE: dict[str, Any] = {
    "$or": [
        {"doc_profile.summary": {"$exists": False}},
        {"doc_profile.summary": None},
        {"doc_profile.summary": ""},
    ]
}


async def _summary_tree_pool_for_corpus(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None,
) -> tuple[Any | None, dict[str, Any], IngestionConfig | None]:
    from services.settings import settings_service

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "default_ingestion_config": 1, "user_id": 1},
    )
    if not corpus:
        return None, {"source": "missing_corpus", "models": [], "lanes": 0}, None

    cfg = IngestionConfig(**(corpus.get("default_ingestion_config") or {}))
    effective_user_id = user_id or str(corpus.get("user_id") or "")
    runtime_summary = (
        await settings_service.get_runtime_ingestion_settings(effective_user_id)
    ).summary
    corpus_summary_models = list(cfg.summary_models or [])
    from services.ingestion.summary_provider_pool import (
        resolve_summary_provider_pool,
    )

    pool, pool_resolution = await resolve_summary_provider_pool(
        configured_refs=corpus_summary_models,
        runtime_refs=(
            runtime_summary.summary_models if runtime_summary.enabled else []
        ),
        user_id=effective_user_id,
        db=db,
    )
    source = "resolved_flash_primary"
    global_cap = runtime_summary.max_concurrent if runtime_summary.enabled else None
    provider_capacity = sum(
        max(1, int(entry.get("max_concurrent") or 1)) for entry in pool
    )
    effective_max_concurrent = (
        min(
            64,
            provider_capacity,
            max(1, int(global_cap or provider_capacity)),
        )
        if pool
        else 0
    )

    contract = {
        "source": source,
        "models": [str(entry.get("model") or "") for entry in pool],
        "lanes": len(pool),
        "provider_capacity": provider_capacity,
        "max_concurrent": effective_max_concurrent,
        "resolution": pool_resolution,
    }
    return (
        summary_tree_llm_from_pool(
            pool,
            cfg.max_summary_tokens,
            global_max_concurrent=effective_max_concurrent,
        ),
        contract,
        cfg,
    )


async def backfill_document_summaries(
    db: Any,
    *,
    corpus_id: str,
    qdrant_client: Any | None = None,
    user_id: str | None = None,
    limit: int = 25,
    doc_ids: list[str] | None = None,
    parent_heal_limit: int = 2000,
) -> dict[str, Any]:
    """Build missing document-level summary profiles from parent summaries."""

    limit = max(0, int(limit or 0))
    parent_heal_limit = max(0, int(parent_heal_limit or 0))
    started = datetime.utcnow()
    llm_fn, contract, cfg = await _summary_tree_pool_for_corpus(
        db,
        corpus_id=corpus_id,
        user_id=user_id,
    )
    if cfg is None:
        return {
            "status": "not_found",
            "corpus_id": corpus_id,
            "error": "corpus not found",
            "started_at": started,
            "completed_at": datetime.utcnow(),
        }

    if limit == 0:
        return {
            "status": "complete",
            "corpus_id": corpus_id,
            "limit": limit,
            "attempted": 0,
            "built": 0,
            "skipped": 0,
            "failed": 0,
            "summary_contract": contract,
            "started_at": started,
            "completed_at": datetime.utcnow(),
        }

    # When a durable summary job passes explicit doc_ids, run those docs even
    # if doc_profile.summary already exists. That repairs the opposite drift:
    # profile present but the summary_tree document node is missing.
    base_doc_filter: dict[str, Any] = {"corpus_id": corpus_id}
    if doc_ids is None:
        base_doc_filter.update(MISSING_DOCUMENT_SUMMARY_CLAUSE)
    doc_filter: dict[str, Any] = with_active_records(base_doc_filter)
    if doc_ids is not None:
        doc_filter["doc_id"] = {
            "$in": sorted({str(doc_id) for doc_id in doc_ids if str(doc_id).strip()})
        }

    rows = (
        await db["documents"]
        .find(
            doc_filter,
            {"_id": 0, "doc_id": 1, "filename": 1, "title": 1},
        )
        .limit(limit)
        .to_list(length=limit)
    )

    parent_clause = parent_summary_required_clause()
    max_concurrent = min(
        max(1, len(rows)),
        max(1, int(contract.get("max_concurrent") or 1)),
    )
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _process_row(row: dict[str, Any]) -> dict[str, Any] | None:
        doc_id = str(row.get("doc_id") or "")
        if not doc_id:
            return None
        async with semaphore:
            try:
                synced = await sync_document_profile_from_existing_tree(
                    db=db,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                )
            except Exception as exc:  # noqa: BLE001 - generation remains fallback
                synced = {
                    "status": "sync_failed",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            if synced.get("status") in {"synced", "already_synced"}:
                return {
                    "doc_id": doc_id,
                    "status": "built",
                    "result": {
                        "document": 1,
                        "profile_source": "existing_summary_tree",
                        **synced,
                    },
                }
            parent_query = {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "$and": [parent_clause],
            }
            required_parent_count, summarized_parent_count = await asyncio.gather(
                db["parent_chunks"].count_documents(parent_query),
                db["parent_chunks"].count_documents(
                    {
                        "corpus_id": corpus_id,
                        "doc_id": doc_id,
                        "$and": [parent_clause, SUMMARY_TEXT_CLAUSE],
                    }
                ),
            )
            missing_parent_count = max(
                required_parent_count - summarized_parent_count, 0
            )
            if required_parent_count == 0 or summarized_parent_count == 0:
                return {
                    "doc_id": doc_id,
                    "status": "skipped_no_parent_summaries",
                    "required_parent_count": required_parent_count,
                    "summarized_parent_count": summarized_parent_count,
                }
            if missing_parent_count and (
                llm_fn is None or missing_parent_count > parent_heal_limit
            ):
                return {
                    "doc_id": doc_id,
                    "status": "skipped_parent_summaries_incomplete",
                    "required_parent_count": required_parent_count,
                    "summarized_parent_count": summarized_parent_count,
                    "missing_parent_count": missing_parent_count,
                    "can_heal": llm_fn is not None,
                }
            try:
                tree_result = await build_and_store_tree(
                    db=db,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    llm_fn=llm_fn,
                    use_llm=llm_fn is not None,
                    heal_missing=llm_fn is not None,
                    heal_limit=parent_heal_limit,
                    max_concurrent=max_concurrent,
                    qdrant_client=qdrant_client,
                    embedding_config=cfg,
                )
                if qdrant_client is not None and not tree_result.get("skipped"):
                    try:
                        from services.ingestion.corpus_lexicon import (
                            refresh_and_index_document_lexicon,
                        )

                        tree_result["lexicon_context_refresh"] = (
                            await refresh_and_index_document_lexicon(
                                db,
                                qdrant_client,
                                corpus_id=corpus_id,
                                doc_id=doc_id,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - tree remains durable
                        tree_result["lexicon_context_refresh"] = {
                            "status": "degraded",
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        }
            except Exception as exc:  # noqa: BLE001 - bounded repair reports per doc
                return {
                    "doc_id": doc_id,
                    "status": "failed",
                    "error": str(exc)[:500],
                }
            return {
                "doc_id": doc_id,
                "status": "skipped" if tree_result.get("skipped") else "built",
                "result": tree_result,
            }

    processed = await asyncio.gather(*(_process_row(row) for row in rows))
    results = [result for result in processed if result is not None]
    attempted = len(results)
    built = sum(1 for result in results if result.get("status") == "built")
    failed = sum(1 for result in results if result.get("status") == "failed")
    skipped = attempted - built - failed

    tier0_projection: dict[str, Any] = {
        "status": "not_needed",
        "requested": 0,
        "embedded": 0,
    }
    built_doc_ids = [
        str(result.get("doc_id") or "")
        for result in results
        if result.get("status") == "built" and result.get("doc_id")
    ]
    if built_doc_ids and qdrant_client is not None:
        try:
            from services.ingestion.tier0 import embed_doc_profiles

            tier0_projection = {
                "status": "complete",
                **await embed_doc_profiles(
                    db,
                    qdrant_client,
                    corpus_id=corpus_id,
                    doc_ids=built_doc_ids,
                    dim=int(getattr(cfg, "embedding_dimension", 1024)),
                ),
            }
        except Exception as exc:  # noqa: BLE001 - durable state records retry need
            tier0_projection = {
                "status": "failed",
                "requested": len(built_doc_ids),
                "embedded": 0,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
    elif built_doc_ids:
        tier0_projection = {
            "status": "deferred_no_qdrant_client",
            "requested": len(built_doc_ids),
            "embedded": 0,
        }

    status = (
        "complete"
        if failed == 0 and tier0_projection.get("status") != "failed"
        else "partial"
    )
    return {
        "status": status,
        "corpus_id": corpus_id,
        "limit": limit,
        "attempted": attempted,
        "built": built,
        "skipped": skipped,
        "failed": failed,
        "max_concurrent": max_concurrent,
        "summary_contract": contract,
        "tier0_projection": tier0_projection,
        "results": results,
        "started_at": started,
        "completed_at": datetime.utcnow(),
    }
