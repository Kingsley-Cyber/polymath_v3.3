"""W2 §10.3 A/B probe — same query through legacy vs waterfall assembly.

Usage (inside the backend container):
    python3 scripts_probe_waterfall.py "question" <corpus_id> [tier]

Runs retrieve() with WATERFALL_ASSEMBLY off (legacy), then twice with it
forced on (in-process settings override — the running app's default is NOT
touched). Prints the legacy source list vs the packet items, and asserts the
determinism receipt: identical packet_hash across the two flagged runs.
"""
import asyncio
import sys

sys.path.insert(0, ".")


async def main(query: str, corpus_id: str, tier: str) -> None:
    from config import get_settings
    from models.schemas import RetrievalTier
    from services.ingestion_service import ingestion_service
    from motor.motor_asyncio import AsyncIOMotorClient
    import os

    s = get_settings()
    # scripts run outside the app lifespan — connect the shared service so
    # hydration + assembly see Mongo exactly like the app does
    db = AsyncIOMotorClient(os.environ["MONGODB_URI"])[s.MONGODB_DATABASE]
    await ingestion_service.connect(db)
    # hydrate + assembly read Mongo via conversation_service._db in-app
    from services.conversation import conversation_service
    conversation_service._db = db
    from services.retriever import retriever_orchestrator

    kw = dict(
        query=query,
        corpus_ids=[corpus_id],
        retrieval_tier=RetrievalTier(tier),
        collections=None,
    )

    s.WATERFALL_ASSEMBLY = False
    legacy = await retriever_orchestrator.retrieve(**kw)
    print(f"== LEGACY: {len(legacy.chunks)} chunks, packet={legacy.packet} ==")
    for c in legacy.chunks[:6]:
        print(f"  {c.score:.3f} {str(c.chunk_id)[-10:]} tier={c.source_tier} len={len(c.text or '')}")

    s.WATERFALL_ASSEMBLY = True
    # positional query -> bypasses the retrieval cache (kwargs-only key),
    # so the flagged runs cannot be served the cached legacy result
    pos = dict(kw)
    q = pos.pop("query")
    run1 = await retriever_orchestrator.retrieve(q, **pos)
    run2 = await retriever_orchestrator.retrieve(q, **pos)
    pk1, pk2 = run1.packet, run2.packet
    assert pk1, "flagged run produced no packet"
    print(f"\n== WATERFALL: {len(pk1['items'])} items, {pk1['used_tokens']}/{pk1['budget_tokens']} tokens ==")
    print(f"hash run1={pk1['packet_hash'][:16]} run2={(pk2 or {}).get('packet_hash','')[:16]}")
    for it in pk1["items"]:
        print(f"  [{it['kind']:7}] {it['tokens']:4}tok lane={it['lane'] or '-':9} "
              f"{str(it['ref_id'])[-10:]} :: {it['text'][:60]!r}")
    print("diag:", pk1["diagnostics"])
    assert pk1["packet_hash"] == (pk2 or {}).get("packet_hash"), "hash NOT deterministic"
    print("\nDETERMINISM OK — identical packet_hash across runs")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    tier = sys.argv[3] if len(sys.argv) > 3 else "qdrant_mongo"
    asyncio.run(main(sys.argv[1], sys.argv[2], tier))
