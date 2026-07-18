"""Read-only rebuild safety census; prints counts only, never settings."""

import asyncio

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        checks = {
            "semantic_digest_jobs.running": (
                "semantic_digest_jobs",
                {"status": "running"},
            ),
            "ingest_batches.active": (
                "ingest_batches",
                {"status": {"$in": ["queued", "running"]}},
            ),
            "ingest_batch_items.running": (
                "ingest_batch_items",
                {"status": "running"},
            ),
            "source_parse_jobs.active": (
                "source_parse_jobs",
                {"status": {"$in": ["queued", "running"]}},
            ),
            "extraction_jobs.active": (
                "extraction_jobs",
                {"status": {"$in": ["queued", "running"]}},
            ),
            "graph_promotion_jobs.active": (
                "graph_promotion_jobs",
                {"status": {"$in": ["queued", "running"]}},
            ),
        }
        counts = {
            label: await database[collection].count_documents(query)
            for label, (collection, query) in checks.items()
        }
        print(counts)
        assert all(count == 0 for count in counts.values()), counts
    finally:
        client.close()


asyncio.run(main())
