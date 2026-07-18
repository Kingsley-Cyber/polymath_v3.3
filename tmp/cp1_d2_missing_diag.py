import asyncio
import json

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.section_classifier import parent_summary_required_clause


CORPUS_ID = "62193743-4175-40da-b861-ba1e1e567b9a"
MISSING = {
    "$or": [
        {"summary": {"$exists": False}},
        {"summary": None},
        {"summary": ""},
    ]
}


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    all_missing = await db.parent_chunks.count_documents(
        {"corpus_id": CORPUS_ID, **MISSING}
    )
    required = await db.parent_chunks.count_documents(
        {"corpus_id": CORPUS_ID, "$and": [parent_summary_required_clause()]}
    )
    required_missing = await db.parent_chunks.count_documents(
        {
            "corpus_id": CORPUS_ID,
            "$and": [parent_summary_required_clause(), MISSING],
        }
    )
    batch = await db.ingest_batches.find_one(
        {"batch_id": "fb9271d9-ec89-4614-bd81-991cb07562e0"},
        {
            "_id": 0,
            "status": 1,
            "summary_backfill_status": 1,
            "summary_backfill_result.status": 1,
            "summary_backfill_result.missing_after": 1,
            "updated_at": 1,
        },
    )
    jobs = [
        row
        async for row in db.summary_jobs.aggregate(
            [
                {"$match": {"corpus_id": CORPUS_ID}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
                {"$sort": {"_id": 1}},
            ]
        )
    ]
    print(
        json.dumps(
            {
                "all_missing": all_missing,
                "required": required,
                "required_missing": required_missing,
                "batch": batch,
                "summary_jobs_by_status": jobs,
            },
            default=str,
            sort_keys=True,
        )
    )
    client.close()


asyncio.run(main())
