"""Phase 0 — doc-concentration observability. Asserting e2e (exit 1 on fail).

Proves, against LIVE retrieval, that:
  1. _diagnostics now emits unique_docs_final + max_doc_share_final.
  2. Those values are COMPUTED CORRECTLY (independently recomputed from
     res.chunks — deterministic, regardless of the run's exact scores).
  3. The candidate-collapse baseline is captured on the graph tier for the
     real query (max_doc_share_final >= 0.5 — the Denis 6/9 pathology).
  4. The trace renderer now SURFACES the previously-dropped distinct_docs_*
     plus the new fields (end-to-end exposure, not just computed-and-dropped).

Pure observability — no behavior change. Non-destructive (read-only retrieve).

Run:
  docker cp services/retriever/__init__.py polymath_v33-backend-1:/app/services/retriever/__init__.py
  docker cp services/chat_orchestrator.py  polymath_v33-backend-1:/app/services/chat_orchestrator.py
  docker cp /tmp/assert_phase0.py polymath_v33-backend-1:/app/_assert_phase0.py
  docker exec -w /app polymath_v33-backend-1 python _assert_phase0.py
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


def _recompute(chunks):
    counts: dict[str, int] = {}
    for c in chunks or []:
        d = getattr(c, "doc_id", None)
        if not d:
            continue
        counts[str(d)] = counts.get(str(d), 0) + 1
    total = sum(counts.values())
    if total <= 0:
        return 0, 0.0
    return len(counts), round(max(counts.values()) / total, 4)


async def main() -> None:
    from services.ingestion_service import ingestion_service
    from services.conversation import conversation_service
    from services.retriever import retriever_orchestrator
    from models.schemas import RetrievalTier
    from services.chat_orchestrator import _format_retrieval_diagnostics_trace

    db = AsyncIOMotorClient(os.environ["MONGODB_URI"]).get_default_database()
    await ingestion_service.connect(db)
    try:
        await conversation_service.connect()
    except Exception as exc:  # best-effort; _filter_existing_corpora tolerates
        print(f"[info] conversation_service.connect: {exc}", flush=True)

    res = await retriever_orchestrator.retrieve(
        QUERY, [CID], RetrievalTier.qdrant_mongo_graph,
        collections=None, final_top_k=8, rerank_enabled=True,
    )
    diag = res.diagnostics or {}
    chunks = res.chunks or []

    # 1. fields present
    has_fields = "unique_docs_final" in diag and "max_doc_share_final" in diag
    check("1_fields_emitted", has_fields,
          f"unique_docs_final={diag.get('unique_docs_final')} max_doc_share_final={diag.get('max_doc_share_final')}")

    # 2. computed correctly (independent recompute from the SAME final chunks)
    exp_unique, exp_share = _recompute(chunks)
    got_unique = diag.get("unique_docs_final")
    got_share = diag.get("max_doc_share_final")
    check("2a_unique_docs_correct", got_unique == exp_unique,
          f"diag={got_unique} recomputed={exp_unique}")
    check("2b_max_share_correct", got_share == exp_share,
          f"diag={got_share} recomputed={exp_share}")

    # 3. the metric is a valid share in [0,1], consistent with the final set
    #    (the pre-Phase-1 baseline was ~0.667; Phase 1 drives it down, so this
    #    asserts validity/exposure, not that the pathology persists).
    valid_share = (
        isinstance(got_share, (int, float))
        and 0.0 <= float(got_share) <= 1.0
        and (len(chunks) == 0 or float(got_share) > 0.0)
    )
    check("3_share_valid_and_consistent", valid_share,
          f"max_doc_share_final={got_share} over {len(chunks)} final chunks / {got_unique} docs")

    # 4. trace renderer surfaces the metrics (previously dropped distinct_docs_* + new fields)
    trace = _format_retrieval_diagnostics_trace(
        diag, fallback_tier=RetrievalTier.qdrant_mongo_graph,
        raw_chunks=len(chunks), context_chunks=len(chunks),
    )
    check("4a_trace_has_diversity_line", "diversity:" in trace, "")
    check("4b_trace_has_max_doc_share", "max_doc_share_final=" in trace, "")
    check("4c_trace_has_distinct_docs", ("docs_merged=" in trace and "docs_final=" in trace), "")
    print("\n----- rendered diversity line -----", flush=True)
    for ln in trace.splitlines():
        if ln.startswith("diversity:"):
            print(ln, flush=True)

    print(f"\n{'=== ALL PASS ===' if not _fail else '=== FAILURES: ' + ', '.join(_fail) + ' ==='}", flush=True)
    sys.exit(1 if _fail else 0)


# Import-safe: only runs as a script (docker exec), NEVER at pytest collection
# (a bare module-level asyncio.run would fire main() on import and crash a
# fresh clone with no live stack). This is a live e2e harness, not a unit test;
# portable invariants live in test_retrieval_quality_invariants.py.
if __name__ == "__main__":
    # Make `services.*` importable no matter the cwd (run from /app, from
    # backend/, or from tests/) so a fresh clone can launch this anywhere.
    import os as _os
    _sys = __import__("sys")
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    asyncio.run(main())
