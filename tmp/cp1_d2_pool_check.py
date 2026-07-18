import asyncio
import json

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.summary_provider_pool import resolve_summary_provider_pool


CORPUS_ID = "62193743-4175-40da-b861-ba1e1e567b9a"


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    corpus = await db.corpora.find_one(
        {"corpus_id": CORPUS_ID},
        {"_id": 0, "user_id": 1, "default_ingestion_config.summary_models": 1},
    )
    assert corpus, "official corpus missing"
    refs = (
        (corpus.get("default_ingestion_config") or {}).get("summary_models")
        or []
    )
    pool, report = await resolve_summary_provider_pool(
        configured_refs=refs,
        runtime_refs=[],
        user_id=str(corpus.get("user_id") or ""),
        db=db,
    )
    safe = {
        "models": [str(row.get("model") or "") for row in pool],
        "all_keys_available": all(bool(row.get("api_key")) for row in pool),
        "report": report,
    }
    print(json.dumps(safe, sort_keys=True))
    assert report["flash_primary"] is True
    assert report["flash_key_available"] is True
    assert report["demoted_provider_count"] == 3
    assert len(pool) == 1
    client.close()


asyncio.run(main())
