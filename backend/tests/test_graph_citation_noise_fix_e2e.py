"""Graph-tier citation-noise fix — asserting e2e (exit 1 on fail).

Levers (services/retriever/__init__.py _retrieve_graph_seed_facts + mode_a.py expand):
  A — query-relevant + junk-filtered fact-seeding: seeds facts only for entities
      that hit a non-generic query concept and aren't junk; on a citation-heavy
      corpus this kills the frequency-driven "association for computational
      linguistics" (ACL) conference facts that flooded <key_facts>.
  B — mode_a NOISY_KINDS filter: drops bibliography/index/toc co-mention chunks
      (Neo4j has no chunk_kind, so via a batched Mongo lookup).

Oracles on the live query (graph tier = the polluted one):
  O1 — no NOISY_KINDS chunk in the graph final set (Lever B invariant).
  O2 — fact seeds query-relevant: ZERO ACL facts, and if any facts survive >=1
       is on-topic (NLP / data augmentation).  ← load-bearing RED-before assert.
  O3 — content not lost: the data-augmentation answer chunk is still present.
  O4 — hybrid tier unchanged (never calls mode_a/fact-seed): answer present, no facts.

Run (from /app, with the live stack):
  docker cp tests/test_graph_citation_noise_fix_e2e.py polymath_v33-backend-1:/app/_t.py
  docker exec -w /app polymath_v33-backend-1 python _t.py ; echo exit=$?
"""
import asyncio
import os
import sys

from motor.motor_asyncio import AsyncIOMotorClient

CID = os.environ.get("E2E_CORPUS", "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
QUERY = os.environ.get("E2E_QUERY", "what is nlp and how does it associate with data augmentation")

_fail: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not cond:
        _fail.append(name)


async def main() -> None:
    from services.ingestion_service import ingestion_service
    from services.conversation import conversation_service
    from services.retriever import retriever_orchestrator
    from services.ingestion.section_classifier import NOISY_KINDS
    from models.schemas import RetrievalTier

    NOISY = set(NOISY_KINDS)
    db = AsyncIOMotorClient(os.environ["MONGODB_URI"]).get_default_database()
    await ingestion_service.connect(db)
    try:
        await conversation_service.connect()
    except Exception as exc:
        print(f"[info] conversation_service.connect: {exc}", flush=True)

    async def _ret(tier):
        return await retriever_orchestrator.retrieve(
            QUERY, [CID], tier, collections=None, final_top_k=8, rerank_enabled=True,
        )

    # ── GRAPH tier (the patched, polluted path) ──
    g = await _ret(RetrievalTier.qdrant_mongo_graph)
    gchunks = g.chunks or []
    gfacts = list(getattr(g, "facts", []) or [])

    # O1 — no NOISY_KINDS chunk in the graph final set (look kind up in Mongo)
    ids = [c.chunk_id for c in gchunks if c.chunk_id]
    kinds = {
        d["chunk_id"]: d.get("chunk_kind")
        async for d in db["chunks"].find(
            {"chunk_id": {"$in": ids}}, {"_id": 0, "chunk_id": 1, "chunk_kind": 1}
        )
    }
    noisy_finals = [(c, kinds.get(c)) for c in ids if kinds.get(c) in NOISY]
    check("O1_no_noisy_kinds_in_graph_final", not noisy_finals, f"noisy survivors: {noisy_finals}")

    # O2 — fact seeds query-relevant: zero ACL, and any survivors are on-topic
    subs = [str(getattr(f, "subject", "") or "").lower() for f in gfacts]
    acl = [s for s in subs if "association for computational" in s]
    check("O2_no_acl_conference_facts", not acl, f"ACL facts leaked: {acl[:4]} (of {len(subs)})")
    on_topic = any(("nlp" in s) or ("natural language" in s) or ("augmentation" in s) for s in subs)
    check("O2b_facts_empty_or_ontopic", (not gfacts) or on_topic,
          f"facts present but none on-topic: {subs[:6]}")

    # O3 — content not lost: data-augmentation content still present in the graph
    # set (any of the 56 body chunks discussing it — not one exact heading).
    check("O3_data_aug_content_present",
          any("data augmentation" in (c.text or "").lower() for c in gchunks),
          "data-augmentation content lost from graph final set")

    # ── HYBRID tier (no-regression control) ──
    h = await _ret(RetrievalTier.qdrant_mongo)
    check("O4_hybrid_content_present",
          any("data augmentation" in (c.text or "").lower() for c in (h.chunks or [])),
          "hybrid data-augmentation content regressed")
    check("O4b_hybrid_factfree",
          not (getattr(h, "facts", []) or []),
          "hybrid unexpectedly has facts")

    print(f"\n  graph_chunks={len(gchunks)} graph_facts={len(gfacts)} acl={len(acl)} "
          f"fact_subjects={sorted(set(subs))[:6]}", flush=True)
    print(f"\n{'=== ALL PASS ===' if not _fail else '=== FAILURES: ' + ', '.join(_fail) + ' ==='}", flush=True)
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    import os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    asyncio.run(main())
