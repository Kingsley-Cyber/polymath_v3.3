"""Owner ops act (2026-08-12): route extraction to the RTX GLiNER-Relex pool.

Measured basis for the switch (same 66-triple battery, same corpus):
  - GLiNER-Relex recall 60/66 vs Qwen3-4B 35-39/66 and Qwen3-30B-A3B 35/66
  - encoder does ONE forward pass per window (~21 windows/s per replica)
    where the LLM autoregressively emits ~6.7 output tokens per token of
    content read — extraction is a labeling task, not a generation task

Writes the verified replica URLs into the routing document's sidecar_pool,
pins the expected release so a wrong build fails closed per call, and flips
the GPU corpora to the graphify_cpu engine (the Relex path).

Run:  docker exec polymath_v33-backend-1 python /app/scripts/route_extraction_to_relex_pool.py
"""
import asyncio
import json
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, "/app")

HOST = "192.168.1.83"
PORTS = [8737, 8738, 8739, 8740, 8742, 8743, 8744]  # 8741 is the CPU embedder
EXPECTED_RELEASE = "relex-large-cuda-sidecar-v1"


def _verify(port: int) -> str | None:
    url = f"http://{HOST}:{port}"
    try:
        with urllib.request.urlopen(url + "/health", timeout=5) as resp:
            body = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        print(f"  {port}: unreachable ({str(exc)[:60]})")
        return None
    release = str(body.get("release") or "")
    device = str(body.get("device") or "")
    if release != EXPECTED_RELEASE:
        print(f"  {port}: REJECTED release={release!r} (want {EXPECTED_RELEASE!r})")
        return None
    print(f"  {port}: ok release={release} device={device}")
    return url


async def main() -> None:
    from services.conversation import conversation_service

    await conversation_service.connect()
    db = conversation_service._db

    print("verifying replicas:")
    pool = [u for u in (_verify(p) for p in PORTS) if u]
    if not pool:
        print("FATAL: no verified replicas — leaving routing untouched")
        return

    await db["extraction_engine_routing"].update_one(
        {"_id": "primary"},
        {
            "$set": {
                "sidecar_url": pool[0],
                "sidecar_pool": pool,
                "expected_release": EXPECTED_RELEASE,
                "mode": "rtx_cuda_relex_pool",
                "updated_by": "owner_ops_script",
                "note": (
                    "GLiNER-Relex pool restored 2026-08-12: 60/66 battery recall "
                    "vs 35-39/66 for the LLM engines, ~21 windows/s per replica"
                ),
                "updated_at": datetime.utcnow(),
                # extraction no longer runs on the GPU LLM, so the autoscaler
                # must not resurrect vLLM for it
                "auto_gpu_engine.enabled": False,
            }
        },
        upsert=True,
    )
    print(f"routing doc: sidecar_pool = {len(pool)} verified replicas")

    flipped = []
    async for c in db["corpora"].find(
        {"status": {"$ne": "archived"}}, {"corpus_id": 1, "name": 1, "default_ingestion_config.extraction_engine": 1}
    ):
        cfg = c.get("default_ingestion_config") or {}
        if cfg.get("extraction_engine") != "ghost_b_llm":
            continue
        queued = await db["extraction_jobs"].count_documents(
            {"corpus_id": c["corpus_id"], "status": {"$in": ["queued", "provider_failed"]}}
        )
        await db["corpora"].update_one(
            {"corpus_id": c["corpus_id"]},
            {"$set": {"default_ingestion_config.extraction_engine": "graphify_cpu"}},
        )
        flipped.append((str(c.get("name")), queued))
    print("flipped to graphify_cpu (Relex pool):")
    for name, queued in flipped:
        print(f"  {name[:44]:46} open={queued}")
    print("workers pick up the route within the 30s cache window — no restart")


if __name__ == "__main__":
    asyncio.run(main())
