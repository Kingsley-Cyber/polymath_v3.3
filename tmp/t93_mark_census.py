import asyncio
import json
import math
import sys

from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, "/tmp/t93_repo")

from config import get_settings
from models.hash_taxonomy import canonical_json_v1
from scripts.semantic_gateway_ugo_canary import CanaryError, _packet_from_parent
from services.semantic_gateway import (
    semantic_digest_cache_key,
    semantic_digest_input_hash,
    semantic_digest_prompt_hash,
    semantic_digest_schema_hash,
)


CORPUS_ID = "5a20bc21-95df-42c2-80c8-f927b4e83904"
MODEL_ID = "openai/LongCat-2.0"
RUNTIME_VERSION = "longcat-api.openai-compatible.2026-07-14.cp9-recanary-mt8192-v1"


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1]


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
        corpus = await db["corpora"].find_one(
            {"corpus_id": CORPUS_ID, "status": {"$ne": "deleted"}},
            {"_id": 0, "corpus_id": 1, "name": 1},
        )
        assert corpus is not None
        document_count = await db["documents"].count_documents(
            {"corpus_id": CORPUS_ID}
        )
        parents = (
            await db["parent_chunks"]
            .find(
                {
                    "corpus_id": CORPUS_ID,
                    "validation_status": "valid",
                    "text": {"$exists": True, "$nin": [None, ""]},
                    "child_ids.0": {"$exists": True},
                },
                {
                    "_id": 0,
                    "parent_id": 1,
                    "doc_id": 1,
                    "text": 1,
                    "source_hash": 1,
                    "child_ids": 1,
                    "validation_status": 1,
                },
            )
            .sort("parent_id", 1)
            .to_list(length=None)
        )
        child_ids = sorted(
            {
                str(child_id)
                for parent in parents
                for child_id in (parent.get("child_ids") or [])
                if child_id
            }
        )
        extractions_by_child: dict[str, list[dict]] = {}
        for offset in range(0, len(child_ids), 1000):
            rows = (
                await db["ghost_b_extractions"]
                .find(
                    {
                        "corpus_id": CORPUS_ID,
                        "chunk_id": {"$in": child_ids[offset : offset + 1000]},
                        "status": "ok",
                        "schema_version": "polymath.extract.v1",
                    },
                    {
                        "_id": 0,
                        "chunk_id": 1,
                        "status": 1,
                        "schema_version": 1,
                        "entities": 1,
                    },
                )
                .sort("chunk_id", 1)
                .to_list(length=None)
            )
            for row in rows:
                extractions_by_child.setdefault(str(row["chunk_id"]), []).append(row)

        packets = []
        failures = []
        for parent in parents:
            extraction_rows = [
                row
                for child_id in (parent.get("child_ids") or [])
                for row in extractions_by_child.get(str(child_id), [])
            ]
            try:
                packets.append(
                    _packet_from_parent(
                        corpus_id=CORPUS_ID,
                        corpus_name=str(corpus.get("name") or ""),
                        parent=parent,
                        extraction_rows=extraction_rows,
                        max_entities=40,
                    )
                )
            except CanaryError as exc:
                failures.append(
                    {
                        "parent_id": str(parent.get("parent_id") or ""),
                        "error_type": type(exc).__name__,
                    }
                )

        packet_sizes = [
            len(canonical_json_v1(packet.packet).encode("utf-8"))
            for packet in packets
        ]
        schema_hash = semantic_digest_schema_hash()
        prompt_hash = semantic_digest_prompt_hash()
        cache_keys = [
            semantic_digest_cache_key(
                input_hash=semantic_digest_input_hash(packet.packet),
                model_id=MODEL_ID,
                schema_hash=schema_hash,
                prompt_hash=prompt_hash,
                runtime_version=RUNTIME_VERSION,
            )
            for packet in packets
        ]
        cached_keys = set()
        for offset in range(0, len(cache_keys), 1000):
            rows = (
                await db["semantic_digest_cache"]
                .find(
                    {
                        "_id": {"$in": cache_keys[offset : offset + 1000]},
                        "status": "accepted_cache",
                        "canonical_write": False,
                    },
                    {"_id": 1},
                )
                .to_list(length=None)
            )
            cached_keys.update(str(row["_id"]) for row in rows)

        count = len(packets)
        ceiling = count * 0.04 * 1.25
        result = {
            "schema_version": "polymath.t9_3_mark_parent_census.v1",
            "provider_calls": 0,
            "active_ingest_batches": active_batches,
            "corpus": {
                "corpus_id": CORPUS_ID,
                "name": corpus.get("name"),
                "document_count": document_count,
            },
            "eligibility": {
                "valid_text_child_parents": len(parents),
                "packet_buildable_parents": count,
                "unbuildable_parents": len(failures),
                "unique_child_ids": len(child_ids),
                "accepted_extraction_child_ids": len(extractions_by_child),
                "already_cached_parents": len(cached_keys),
                "provider_calls_required": count - len(cached_keys),
            },
            "packet_bytes": {
                "minimum": min(packet_sizes),
                "p50": percentile(packet_sizes, 0.50),
                "p95": percentile(packet_sizes, 0.95),
                "maximum": max(packet_sizes),
            },
            "budget": {
                "formula": "N * 0.04 * 1.25",
                "n": count,
                "estimated_cost_per_packet_usd": 0.04,
                "contingency_multiplier": 1.25,
                "ceiling_usd": round(ceiling, 8),
                "owner_visibility_threshold_usd": 200.0,
                "go_arithmetic_passed": ceiling <= 200.0,
            },
            "failure_parent_ids": [row["parent_id"] for row in failures],
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        assert active_batches == 0
        assert parents
        assert len(failures) == 0
        assert len(parents) == count
    finally:
        mongo.close()


asyncio.run(main())
