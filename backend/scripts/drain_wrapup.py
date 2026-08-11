"""Drain wrap-up census — run at (or near) extraction-tail zero.

One command answers "is it actually finished, and what's left over":
  - queued / running / terminal counts per corpus
  - poison list: jobs stuck in retry (attempts >= 4) that will never finish
    without an owner decision (quarantine or chunk fix) — REPORTED, never
    auto-flipped
  - promotion + certificate lag (extractions done but graph/certs behind)
  - GPU state (engine health; VRAM should be freed by the autoscaler soon
    after zero)

Run:  docker exec polymath_v33-backend-1 python /app/scripts/drain_wrapup.py
"""
import asyncio
import sys
import urllib.request

sys.path.insert(0, "/app")


async def main() -> None:
    from services.conversation import conversation_service

    await conversation_service.connect()
    db = conversation_service._db
    total = {"queued": 0, "running": 0, "terminal": 0, "failed": 0}
    print("== per-corpus tail ==")
    async for c in db["corpora"].find({"status": {"$ne": "archived"}},
                                      {"corpus_id": 1, "name": 1}):
        cid = c["corpus_id"]
        q = await db["extraction_jobs"].count_documents({"corpus_id": cid, "status": "queued"})
        r = await db["extraction_jobs"].count_documents({"corpus_id": cid, "status": "running"})
        f = await db["extraction_jobs"].count_documents(
            {"corpus_id": cid, "status": {"$in": ["failed", "failed_recoverable", "validation_failed"]}})
        t = await db["extraction_jobs"].count_documents(
            {"corpus_id": cid, "status": {"$in": ["succeeded", "promoted", "skipped"]}})
        total["queued"] += q; total["running"] += r
        total["failed"] += f; total["terminal"] += t
        if q or r or f:
            print(f"  {str(c.get('name'))[:40]:42} queued={q:6} running={r:4} failed={f:4} terminal={t}")
    print(f"TOTAL queued={total['queued']} running={total['running']} "
          f"failed={total['failed']} terminal={total['terminal']}")
    print("DONE: tail is drained" if not (total["queued"] or total["running"])
          else "NOT DONE — tail still has work")

    poison = []
    async for j in db["extraction_jobs"].find(
        {"status": {"$in": ["failed_recoverable", "validation_failed", "failed"]},
         "attempts": {"$gte": 4}},
        {"job_id": 1, "corpus_id": 1, "chunk_id": 1, "attempts": 1, "failure": 1},
    ).limit(50):
        poison.append(j)
    print(f"\n== poison jobs (attempts>=4): {len(poison)} ==")
    for j in poison[:15]:
        print(f"  {j.get('job_id')} attempts={j.get('attempts')} "
              f"failure={str(j.get('failure'))[:70]}")
    if poison:
        print("  -> owner decision: fix chunks or quarantine; these retry forever otherwise")

    promo_pending = await db["graph_promotion_jobs"].count_documents(
        {"status": {"$in": ["queued", "blocked_no_extractions"]}})
    certs = await db["query_ready_certificates"].count_documents({})
    print(f"\npromotion pending/blocked: {promo_pending} | query-ready certificates: {certs}")

    try:
        with urllib.request.urlopen("http://192.168.1.83:8000/health", timeout=4) as resp:
            print("gpu engine:", "UP (autoscaler will offload ~3 idle cycles after zero)"
                  if resp.status == 200 else f"http {resp.status}")
    except Exception:
        print("gpu engine: down/offloaded (VRAM freed)")


if __name__ == "__main__":
    asyncio.run(main())
