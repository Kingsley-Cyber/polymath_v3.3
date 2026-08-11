"""Owner ops act (2026-08-11): flip all queued-work corpora to the GPU engine.

Owner ruling, verbatim intent: "whichever one can be served up to extract the
fastest with throughput is all i care about" — finish the extraction tail now.

Measured basis recorded with the waiver:
  - engines tie on throughput: 30B-A3B ~5.1 valid extractions/s @64
    concurrency vs 4B's 5.18 (both measured on the real card)
  - engines tie on battery band: 35-44/66 vs GLiNER baseline 60/66 — the
    owner explicitly accepts this quality for speed (waiver, not a pass)
  - schema validity 100% on every run (xgrammar)

What this does:
  1. Every ACTIVE corpus with queued extraction jobs: engine -> ghost_b_llm,
     pool -> the vLLM entry (polymath-extract @ 192.168.1.83:8000/v1,
     max_concurrent 64, native :8086 lifecycle).
  2. Routing doc: auto_gpu_engine.qualified=true tagged OWNER WAIVER with the
     measured numbers, so the executor's autoscaler manages the engine
     (up while work exists, VRAM offloaded when the tail drains).

Run:  docker exec polymath_v33-backend-1 python /app/scripts/flip_extraction_to_gpu.py
"""
import asyncio
import sys
from datetime import datetime

sys.path.insert(0, "/app")


async def main() -> None:
    from services.conversation import conversation_service

    await conversation_service.connect()
    db = conversation_service._db
    v1 = await db["corpora"].find_one(
        {"corpus_id": "77edfcea-ea65-4b71-a523-743e72c5ddbc"}
    )
    entry = dict((v1["default_ingestion_config"]["extraction_models"])[0])
    flipped = []
    async for c in db["corpora"].find(
        {"status": {"$ne": "archived"}}, {"corpus_id": 1, "name": 1}
    ):
        cid = c["corpus_id"]
        queued = await db["extraction_jobs"].count_documents(
            {"corpus_id": cid, "status": "queued"}
        )
        if not queued:
            continue
        await db["corpora"].update_one(
            {"corpus_id": cid},
            {"$set": {
                "default_ingestion_config.extraction_engine": "ghost_b_llm",
                "default_ingestion_config.models_linked": False,
                "default_ingestion_config.extraction_models": [entry],
            }},
        )
        flipped.append((str(c.get("name")), queued))
    print("flipped to ghost_b_llm (vLLM 30B-A3B, conc 64):")
    for name, queued in flipped:
        print(f"  {name[:44]:46} queued={queued}")
    await db["extraction_engine_routing"].update_one(
        {"_id": "primary"},
        {
            "$set": {
                "auto_gpu_engine.qualified": True,
                "auto_gpu_engine.qualification_note": (
                    "OWNER WAIVER 2026-08-11: battery 35-44/66 vs baseline "
                    "60/66; ruling = fastest throughput, finish now. Measured "
                    "~5.1 valid extractions/s @64; 100% schema validity."
                ),
            },
            "$push": {"qualification_evidence": {
                "release": "qwen3-30b-a3b-fp8-20260811",
                "evidence": (
                    "owner_waiver: speed ruling; battery v10 fresh 35/66 "
                    "exact, 44/66 pair, 100% validity; throughput 5.1/s@64 "
                    "== 4B's 5.18"
                ),
                "at": datetime.utcnow(),
            }},
        },
    )
    print("waiver recorded; autoscaler ARMED — up while work exists, "
          "VRAM offloaded when the tail drains")


if __name__ == "__main__":
    asyncio.run(main())
