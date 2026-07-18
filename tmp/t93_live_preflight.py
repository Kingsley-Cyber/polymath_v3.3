import asyncio
import json
import sys

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, "/tmp/t93_repo")

from config import get_settings
from scripts.semantic_gateway_ugo_canary import _discover_packets
from services.settings import settings_service


async def main() -> None:
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = mongo.get_default_database()
        except Exception:
            db = mongo[settings.MONGODB_DATABASE]
        active_batches = await db["ingest_batches"].count_documents(
            {"status": {"$in": ["queued", "running"]}}
        )
        corpus_id, eligible, packets = await _discover_packets(
            db,
            corpus_name="UGO_CORPUS",
            count=10,
            max_entities=40,
        )
        settings_service.attach(db)
        key_presence = {}
        for provider in ("deepseek", "longcat"):
            key = await settings_service.get_plaintext_key_any_user(provider)
            key_presence[provider] = bool(key)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                settings.LITELLM_URL.rstrip("/") + "/health/liveliness"
            )
        print(
            json.dumps(
                {
                    "active_ingest_batches": active_batches,
                    "ugo_corpus_id": corpus_id,
                    "ugo_eligible_parents": eligible,
                    "ugo_selected_packets": len(packets),
                    "ugo_unique_selected_parents": len(
                        {packet.parent_id for packet in packets}
                    ),
                    "max_parent_text_bytes": max(
                        len(packet.packet["parent_text"].encode("utf-8"))
                        for packet in packets
                    ),
                    "encrypted_key_presence": key_presence,
                    "litellm_liveliness_status": response.status_code,
                },
                sort_keys=True,
            )
        )
        assert active_batches == 0
        assert eligible >= 10
        assert len(packets) == 10
        assert len({packet.parent_id for packet in packets}) == 10
        assert all(key_presence.values())
        assert response.status_code == 200
    finally:
        mongo.close()


asyncio.run(main())
