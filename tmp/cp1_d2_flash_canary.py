import asyncio
import json

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ghost_a import SummaryTask, summarize_parents
from services.ingestion.section_classifier import parent_summary_required_clause
from services.ingestion.summary_backfill import child_context_for_rows
from services.ingestion.summary_provider_pool import resolve_summary_provider_pool


CORPUS_ID = "62193743-4175-40da-b861-ba1e1e567b9a"


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    corpus = await db.corpora.find_one(
        {"corpus_id": CORPUS_ID},
        {"_id": 0, "user_id": 1, "default_ingestion_config": 1},
    )
    assert corpus, "official corpus missing"
    cfg = corpus.get("default_ingestion_config") or {}
    pool, resolution = await resolve_summary_provider_pool(
        configured_refs=cfg.get("summary_models") or [],
        runtime_refs=[],
        user_id=str(corpus.get("user_id") or ""),
        db=db,
    )
    assert resolution["flash_primary"] is True
    assert resolution["flash_key_available"] is True
    rows = await db.parent_chunks.find(
        {
            "corpus_id": CORPUS_ID,
            "$and": [parent_summary_required_clause()],
        },
        {
            "_id": 0,
            "parent_id": 1,
            "doc_id": 1,
            "source_tier": 1,
            "text": 1,
        },
    ).sort([("doc_id", 1), ("parent_id", 1)]).limit(3).to_list(length=3)
    assert len(rows) == 3, f"expected 3 canary parents, found {len(rows)}"
    child_context = await child_context_for_rows(db, CORPUS_ID, rows)
    tasks = [
        SummaryTask(
            parent_id=row["parent_id"],
            doc_id=str(row.get("doc_id") or ""),
            corpus_id=CORPUS_ID,
            source_tier=str(row.get("source_tier") or "parent"),
            text=str(row.get("text") or ""),
            source_child_ids=child_context.get(row["parent_id"], {}).get(
                "source_child_ids", []
            ),
            child_boundaries=child_context.get(row["parent_id"], {}).get(
                "child_boundaries", ""
            ),
        )
        for row in rows
    ]
    pool_status = {"resolution": resolution}
    results = await summarize_parents(
        tasks,
        max_summary_tokens=int(cfg.get("max_summary_tokens") or 175),
        pool=pool,
        global_max_concurrent=1,
        pool_status=pool_status,
    )
    safe = {
        "requested": len(tasks),
        "accepted": len(results),
        "models": sorted({str(row.summary_model or "") for row in results}),
        "validation_statuses": [row.validation_status for row in results],
        "temporal_classes": [row.temporal_class for row in results],
        "dropped_provider_count": int(
            pool_status.get("dropped_provider_count") or 0
        ),
        "active_provider_count": int(pool_status.get("active_provider_count") or 0),
    }
    print(json.dumps(safe, sort_keys=True))
    assert len(results) == 3
    assert all(row.validation_status == "valid" for row in results)
    assert all("deepseek-v4-flash" in str(row.summary_model or "").lower() for row in results)
    assert all(str(row.temporal_class or "") for row in results)
    assert safe["dropped_provider_count"] == 0
    client.close()


asyncio.run(main())
