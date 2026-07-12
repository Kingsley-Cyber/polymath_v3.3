"""Reconcile and optionally backfill shared Tier-0 document routing cards."""

from __future__ import annotations

import argparse
import asyncio
import json

from services.conversation import conversation_service
from services.ingestion.tier0 import (
    embed_doc_profiles,
    reconcile_doc_profile_projection_state,
)
from services.ingestion_service import ingestion_service


async def _run(args: argparse.Namespace) -> dict:
    await conversation_service.connect()
    await ingestion_service.connect(conversation_service._db)
    try:
        output = []
        for corpus_id in args.corpus_id:
            before = await reconcile_doc_profile_projection_state(
                conversation_service._db,
                ingestion_service.qdrant_client,
                corpus_id=corpus_id,
            )
            indexed = 0
            failures = []
            if args.index_missing:
                corpus = await conversation_service._db["corpora"].find_one(
                    {"corpus_id": corpus_id},
                    {"_id": 0, "default_ingestion_config.embedding_dimension": 1},
                )
                dimension = int(
                    ((corpus or {}).get("default_ingestion_config") or {}).get(
                        "embedding_dimension", 1024
                    )
                )
                missing = list(before.get("missing_doc_ids") or [])[: args.limit]
                for offset in range(0, len(missing), args.batch_size):
                    batch = missing[offset : offset + args.batch_size]
                    try:
                        result = await embed_doc_profiles(
                            conversation_service._db,
                            ingestion_service.qdrant_client,
                            corpus_id=corpus_id,
                            doc_ids=batch,
                            dim=dimension,
                        )
                        indexed += int(result.get("embedded") or 0)
                    except Exception as exc:  # noqa: BLE001 - continue bounded repair
                        failures.append(
                            {
                                "offset": offset,
                                "count": len(batch),
                                "error": f"{type(exc).__name__}: {exc}"[:500],
                            }
                        )
            after = await reconcile_doc_profile_projection_state(
                conversation_service._db,
                ingestion_service.qdrant_client,
                corpus_id=corpus_id,
            )
            output.append(
                {
                    "corpus_id": corpus_id,
                    "before": {key: value for key, value in before.items() if key != "missing_doc_ids"},
                    "indexed": indexed,
                    "failures": failures,
                    "after": {key: value for key, value in after.items() if key != "missing_doc_ids"},
                }
            )
        return {"status": "complete", "corpora": output}
    finally:
        await ingestion_service.disconnect()
        await conversation_service.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-id", action="append", required=True)
    parser.add_argument("--index-missing", action="store_true")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()
    args.limit = max(1, min(int(args.limit), 50000))
    args.batch_size = max(1, min(int(args.batch_size), 256))
    print(json.dumps(asyncio.run(_run(args)), indent=2, default=str))


if __name__ == "__main__":
    main()
