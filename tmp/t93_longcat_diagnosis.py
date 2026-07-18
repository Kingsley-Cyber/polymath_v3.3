import asyncio
import hashlib
import json
import statistics
import sys

from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, "/tmp/t93_repo")

from config import get_settings
from models.hash_taxonomy import canonical_json_v1
from scripts.semantic_gateway_ugo_canary import _discover_packets


RUNTIME_VERSION = "longcat-api.openai-compatible.2026-07-14.cp9-preflight-v1"


async def main() -> None:
    receipt = json.load(open("/tmp/t93_longcat_ugo_canary.json", encoding="utf-8"))
    rows = receipt["packet_canary"]["receipts"]
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = mongo.get_default_database()
        except Exception:
            db = mongo[settings.MONGODB_DATABASE]
        _, _, packets = await _discover_packets(
            db,
            corpus_name="UGO_CORPUS",
            count=10,
            max_entities=40,
        )
        packet_by_parent = {packet.parent_id: packet for packet in packets}
        table = []
        for index, row in enumerate(rows):
            packet = packet_by_parent[row["parent_id"]]
            packet_bytes = len(canonical_json_v1(packet.packet).encode("utf-8"))
            output_lengths = None
            output_hashes = None
            if row["status"] == "dead_letter":
                dlq = await db["semantic_digest_dead_letters"].find_one(
                    {
                        "_id": row["dead_letter_id"],
                        "runtime_version": RUNTIME_VERSION,
                    },
                    {"_id": 0, "raw_outputs": 1, "raw_output_hashes": 1},
                )
                assert dlq is not None
                raw_outputs = dlq.get("raw_outputs") or []
                output_lengths = [len(value.encode("utf-8")) for value in raw_outputs]
                output_hashes = list(dlq.get("raw_output_hashes") or [])
            table.append(
                {
                    "index": index,
                    "status": row["status"],
                    "parent_id": row["parent_id"],
                    "packet_bytes": packet_bytes,
                    "attempts": row["attempts"],
                    "completion_tokens": row["usage"]["completion_tokens"],
                    "completion_cap_total": 4096 * row["attempts"],
                    "at_completion_cap": (
                        row["usage"]["completion_tokens"] == 4096 * row["attempts"]
                    ),
                    "stored_argument_output_bytes": output_lengths,
                    "stored_argument_output_hashes": output_hashes,
                    "finish_reason_recorded": False,
                }
            )
        failed = [row for row in table if row["status"] == "dead_letter"]
        accepted = [row for row in table if row["status"] == "accepted"]
        summary = {
            "schema_version": "polymath.t9_3_longcat_readonly_diagnosis.v1",
            "provider_calls": 0,
            "packet_count": len(table),
            "failed_count": len(failed),
            "accepted_count": len(accepted),
            "failed_at_completion_cap": sum(row["at_completion_cap"] for row in failed),
            "accepted_at_completion_cap": sum(
                row["at_completion_cap"] for row in accepted
            ),
            "failed_packet_bytes": {
                "min": min(row["packet_bytes"] for row in failed),
                "max": max(row["packet_bytes"] for row in failed),
                "mean": round(statistics.mean(row["packet_bytes"] for row in failed), 2),
            },
            "accepted_packet_bytes": {
                "min": min(row["packet_bytes"] for row in accepted),
                "max": max(row["packet_bytes"] for row in accepted),
                "mean": round(
                    statistics.mean(row["packet_bytes"] for row in accepted), 2
                ),
            },
            "all_failed_stored_argument_outputs_empty": all(
                row["stored_argument_output_bytes"] == [0, 0] for row in failed
            ),
            "finish_reason_recorded": False,
            "diagnosis": "completion_cap_parameter_primary",
            "table": table,
        }
        encoded = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        print(encoded, end="")
        print(
            "DIAGNOSIS_SHA256=" + hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            file=sys.stderr,
        )
        assert len(table) == 10
        assert len(failed) == 5
        assert summary["failed_at_completion_cap"] == 5
        assert summary["accepted_at_completion_cap"] == 0
        assert summary["all_failed_stored_argument_outputs_empty"] is True
    finally:
        mongo.close()


asyncio.run(main())
