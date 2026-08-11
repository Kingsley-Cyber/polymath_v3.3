"""Owner ops act: point graphify_cpu extraction at the Mac-local MPS sidecar.

The RTX GLiNER replicas were retired 2026-08-11 (one-engine ruling) but the
routing document still fans the ~75k-job enrichment tail across their four
dead ports, stalling it. This clears sidecar_url/sidecar_pool/expected_release
so the client falls back to the env chain (RELEX_SIDECAR_URL -> the Mac MPS
host sidecar), and preserves every governance block (auto_gpu_engine,
qualified_releases, wake, worker_stack, route_history).

Run:  docker exec polymath_v33-backend-1 python /app/scripts/failover_sidecar_to_local.py
Workers pick the change up within the 30s route-cache window; no restarts.
"""
import asyncio
import sys
from datetime import datetime

sys.path.insert(0, "/app")


async def main() -> None:
    from services.conversation import conversation_service

    await conversation_service.connect()
    db = conversation_service._db
    result = await db["extraction_engine_routing"].update_one(
        {"_id": "primary"},
        {
            "$unset": {"sidecar_url": "", "sidecar_pool": "", "expected_release": ""},
            "$set": {
                "mode": "env_chain_local_failover",
                "updated_by": "owner_ops_script",
                "note": "RTX GLiNER retired; env chain -> Mac MPS sidecar until a GPU engine qualifies",
                "updated_at": datetime.utcnow(),
            },
            "$push": {"route_history": {"$each": [{
                "at": datetime.utcnow(), "by": "owner_ops_script",
                "mode": "env_chain_local_failover",
                "note": "cleared dead RTX pool", "sidecar_url": None,
            }], "$slice": -50}},
        },
    )
    doc = await db["extraction_engine_routing"].find_one({"_id": "primary"})
    print("modified:", result.modified_count)
    print("sidecar_url now:", doc.get("sidecar_url"), "| pool:", doc.get("sidecar_pool"))
    print("preserved auto_gpu_engine.enabled:", (doc.get("auto_gpu_engine") or {}).get("enabled"),
          "| qualified_releases:", doc.get("qualified_releases"))


if __name__ == "__main__":
    asyncio.run(main())
