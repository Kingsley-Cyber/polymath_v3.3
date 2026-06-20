"""Phase 1 — candidate-collapse hygiene. Asserting e2e (exit 1 on fail).

Proves, against LIVE retrieval on the real query, that the per-document
ceiling + relative noise floor in select_with_diversity:
  1. break same-document domination — max_doc_share_final drops from the
     ~0.667 (Denis 6/9) baseline to <= 1/3;
  2. kill the lexical-rescued junk tail — every NON-graph final chunk now
     scores at/above the pool-derived floor (no 0.176/0.215 fillers);
  3. keep document spread — unique_docs_final stays healthy;
  4. do NOT regress Vector Base / Hybrid (both still return results; vector
     base is strict top-k, untouched).

Non-destructive (read-only retrieve). Run:
  docker cp services/retriever/ranking_policy.py polymath_v33-backend-1:/app/services/retriever/ranking_policy.py
  docker cp /tmp/assert_phase1.py polymath_v33-backend-1:/app/_assert_phase1.py
  docker exec -w /app polymath_v33-backend-1 python _assert_phase1.py
"""
import asyncio
import os
import sys

from motor.motor_asyncio import AsyncIOMotorClient

CID = os.environ.get("E2E_CORPUS", "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
QUERY = os.environ.get("E2E_QUERY", "what is nlp and how does it assist in model fine tuning")

_BASELINE_SHARE = 0.667   # pre-fix max_doc_share_final (Denis 6/9), measured
_SHARE_CAP = 0.34         # post-fix ceiling (~1/3)

_fail: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not cond:
        _fail.append(name)


async def _retrieve(tier):
    from services.retriever import retriever_orchestrator
    return await retriever_orchestrator.retrieve(
        QUERY, [CID], tier, collections=None, final_top_k=8, rerank_enabled=True,
    )


async def main() -> None:
    from services.ingestion_service import ingestion_service
    from services.conversation import conversation_service
    from services.retriever.ranking_policy import _is_graph_expansion
    from models.schemas import RetrievalTier

    db = AsyncIOMotorClient(os.environ["MONGODB_URI"]).get_default_database()
    await ingestion_service.connect(db)
    try:
        await conversation_service.connect()
    except Exception as exc:
        print(f"[info] conversation_service.connect: {exc}", flush=True)

    # ── GRAPH tier — the tier the user reported.
    g = await _retrieve(RetrievalTier.qdrant_mongo_graph)
    gdiag = g.diagnostics or {}
    gchunks = g.chunks or []
    share = float(gdiag.get("max_doc_share_final") or 0.0)
    uniq = int(gdiag.get("unique_docs_final") or 0)

    # 1. domination broken
    check("1_domination_broken", share <= _SHARE_CAP,
          f"max_doc_share_final={share} (cap {_SHARE_CAP}; baseline ~{_BASELINE_SHARE})")
    check("1b_better_than_baseline", share < _BASELINE_SHARE - 0.05,
          f"share={share} vs baseline ~{_BASELINE_SHARE}")

    # 2. junk tail gone — recompute the pool-derived floor from the live top
    scores = [float(c.score or 0.0) for c in gchunks]
    top = max(scores) if scores else 0.0
    rel_floor = max(0.10, 0.25 * top) if 0.0 <= top <= 1.0 else 0.0
    nongraph_below = [
        (getattr(c, "chunk_id", "?"), float(c.score or 0.0))
        for c in gchunks
        if not _is_graph_expansion(c) and float(c.score or 0.0) < rel_floor - 1e-9
    ]
    check("2_no_subfloor_nongraph_chunks", not nongraph_below,
          f"rel_floor={rel_floor:.4f}; below-floor non-graph: {nongraph_below}")
    check("2b_min_nongraph_reasonable",
          all(float(c.score or 0.0) >= 0.15 for c in gchunks if not _is_graph_expansion(c)),
          f"min non-graph score={min([float(c.score or 0.0) for c in gchunks if not _is_graph_expansion(c)] or [0.0]):.4f}")

    # 3. spread retained
    check("3_doc_spread_retained", uniq >= 4,
          f"unique_docs_final={uniq} over {len(gchunks)} chunks")

    print("\n  graph final set:", flush=True)
    for c in gchunks:
        print(f"    {float(c.score or 0.0):.4f}  {('GRAPH ' if _is_graph_expansion(c) else '      ')}{str(c.doc_id)[:36]}", flush=True)

    # 4. no regression on Vector Base / Hybrid (both still return results)
    v = await _retrieve(RetrievalTier.qdrant_only)
    h = await _retrieve(RetrievalTier.qdrant_mongo)
    check("4a_vector_base_returns", len(v.chunks or []) > 0,
          f"{len(v.chunks or [])} chunks")
    check("4b_hybrid_returns", len(h.chunks or []) > 0,
          f"{len(h.chunks or [])} chunks, max_doc_share={float((h.diagnostics or {}).get('max_doc_share_final') or 0.0)}")

    print(f"\n{'=== ALL PASS ===' if not _fail else '=== FAILURES: ' + ', '.join(_fail) + ' ==='}", flush=True)
    sys.exit(1 if _fail else 0)


# Import-safe: only runs as a script (docker exec), never at pytest collection.
if __name__ == "__main__":
    import os as _os
    _sys = __import__("sys")
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    asyncio.run(main())
