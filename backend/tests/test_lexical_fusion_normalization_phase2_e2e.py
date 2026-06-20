"""Phase 2 — fusion normalization. Asserting e2e (exit 1 on fail).

The Qdrant sparse lexical lane emitted raw BM25 scores (~100-140) while dense
cosine is ~0.9 and graph is [0,1], so the lexical lane categorically dominated
merge_pools' max()+sort and the pre-rerank cut, starving good dense/graph
candidates before the cross-encoder. The Mongo $text path already normalizes;
the sparse path now does too (divide-by-max).

Proves, against LIVE retrieval:
  1. the sparse lexical lane is now bounded to [0,1] (the scale fix), and the
     sparse path is actually exercised (not the Mongo fallback);
  2. NO regression — all three tiers still return results, and Phase 1's
     candidate-collapse fix still holds (graph max_doc_share_final <= 1/3).

Non-destructive. Run:
  docker cp services/retriever/lexical.py polymath_v33-backend-1:/app/services/retriever/lexical.py
  docker cp /tmp/assert_phase2.py polymath_v33-backend-1:/app/_assert_phase2.py
  docker exec -w /app polymath_v33-backend-1 python _assert_phase2.py
"""
import asyncio
import os
import sys

from motor.motor_asyncio import AsyncIOMotorClient

CID = os.environ.get("E2E_CORPUS", "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
QUERY = os.environ.get("E2E_QUERY", "what is nlp and how does it assist in model fine tuning")

_fail: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not cond:
        _fail.append(name)


async def main() -> None:
    from services.ingestion_service import ingestion_service
    from services.conversation import conversation_service
    from services.retriever.lexical import lexical_retriever
    from services.retriever import retriever_orchestrator
    from models.schemas import RetrievalTier

    db = AsyncIOMotorClient(os.environ["MONGODB_URI"]).get_default_database()
    await ingestion_service.connect(db)
    try:
        await conversation_service.connect()
    except Exception as exc:
        print(f"[info] conversation_service.connect: {exc}", flush=True)

    # 1. lexical lane now bounded to [0,1]
    lex = await lexical_retriever.search(QUERY, [CID], top_k=12)
    scores = [float(c.score or 0.0) for c in lex]
    provs = {
        (c.provenance[0].get("retriever") if c.provenance else "?")
        for c in lex
    }
    check("1_lexical_nonempty", len(scores) > 0, f"{len(scores)} lexical hits")
    check("1a_sparse_path_exercised", "qdrant_sparse" in provs,
          f"provenances={provs}")
    check("1b_lexical_bounded_0_1",
          bool(scores) and max(scores) <= 1.0001 and min(scores) >= 0.0,
          f"min={min(scores):.4f} max={max(scores):.4f} (was raw BM25 ~103-140)")

    # 2. no regression across tiers + Phase 1 still holds
    async def _ret(t):
        return await retriever_orchestrator.retrieve(
            QUERY, [CID], t, collections=None, final_top_k=8, rerank_enabled=True,
        )

    v = await _ret(RetrievalTier.qdrant_only)
    h = await _ret(RetrievalTier.qdrant_mongo)
    g = await _ret(RetrievalTier.qdrant_mongo_graph)
    check("2a_vector_returns", len(v.chunks or []) > 0, f"{len(v.chunks or [])} chunks")
    check("2b_hybrid_returns", len(h.chunks or []) > 0, f"{len(h.chunks or [])} chunks")
    check("2c_graph_returns", len(g.chunks or []) > 0, f"{len(g.chunks or [])} chunks")

    gshare = float((g.diagnostics or {}).get("max_doc_share_final") or 0.0)
    hshare = float((h.diagnostics or {}).get("max_doc_share_final") or 0.0)
    check("2d_phase1_intact_graph", gshare <= 0.34,
          f"graph max_doc_share_final={gshare} (Phase 1 ceiling 1/3)")
    check("2e_phase1_intact_hybrid", hshare <= 0.34,
          f"hybrid max_doc_share_final={hshare}")

    # final scores are reranker-probability-bounded everywhere (sanity)
    gmax = max((float(c.score or 0.0) for c in g.chunks or []), default=0.0)
    check("2f_final_scores_bounded", gmax <= 1.0001,
          f"graph final max score={gmax:.4f}")

    print(f"\n{'=== ALL PASS ===' if not _fail else '=== FAILURES: ' + ', '.join(_fail) + ' ==='}", flush=True)
    sys.exit(1 if _fail else 0)


asyncio.run(main())
