import asyncio
import json
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase

from config import get_settings


async def main():
    state = json.loads(Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json").read_text())
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        db = mongo[settings.MONGODB_DATABASE]
        item = await db["ingest_batch_items"].find_one(
            {
                "batch_id": state["batch_id"],
                "corpus_id": state["corpus_id"],
                "ordinal": 3,
            },
            {"_id": 0, "doc_id": 1, "status": 1, "phase": 1},
        )
        doc_id = str(item["doc_id"])
        async with driver.session() as session:
            row = await (
                await session.run(
                    """
                    CALL { MATCH (d:Document {doc_id:$doc_id, corpus_id:$corpus_id}) RETURN count(d) AS documents }
                    CALL { MATCH (c:Chunk {doc_id:$doc_id, corpus_id:$corpus_id}) RETURN count(c) AS chunks }
                    CALL { MATCH (:Chunk {doc_id:$doc_id, corpus_id:$corpus_id})-[r:MENTIONS]->() RETURN count(r) AS mentions }
                    CALL { MATCH (e:Entity) WHERE coalesce(e.tombstone,false)=false AND NOT EXISTS { MATCH (:Chunk)-[:MENTIONS]->(e) } RETURN count(e) AS orphan_entities }
                    RETURN documents, chunks, mentions, orphan_entities
                    """,
                    doc_id=doc_id,
                    corpus_id=state["corpus_id"],
                )
            ).single()
        print(json.dumps({"status": item["status"], "phase": item["phase"], **dict(row)}, sort_keys=True))
    finally:
        await driver.close()
        mongo.close()


asyncio.run(main())
