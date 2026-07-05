"""RTX vLLM extraction-lane probe — run INSIDE the backend container.

Calls the REAL services.ghost_b.extract_entities (prompt build, output-mode
selection, JSON parsing, alias normalization, evidence gate, failure
accounting — the exact production pipeline) with a one-lane pool pointed at
the RTX box via the litellm OpenAI-passthrough, on real corpus chunks.

    docker cp scripts/probe_rtx_extraction.py polymath_v33-backend-1:/app/
    docker exec polymath_v33-backend-1 python probe_rtx_extraction.py \
        <base_url> <api_key> [n_chunks] [concurrency]

Receipts: coverage, typed-predicate %, entities/relations/facts per chunk,
wall latency, chunks/min, explicit failure list. Non-zero exit when the lane
is unusable (assert-before-adopt).
"""
import asyncio
import sys
import time

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ghost_b import ExtractionTask, extract_entities

CORPUS = "a42992d0-216c-400b-8447-43a90e38d9a5"  # authentic_library_v2
MODEL = "openai/polymath-extract"


async def main() -> int:
    base_url = sys.argv[1]
    api_key = sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 24
    conc = int(sys.argv[4]) if len(sys.argv) > 4 else 24
    # Skip the first K usable chunks — fresh texts defeat litellm response
    # caching so wall time measures the live lane, not a replay.
    offset = int(sys.argv[5]) if len(sys.argv) > 5 else 0

    settings = get_settings()
    db = AsyncIOMotorClient(settings.MONGODB_URI).get_default_database()

    # Real body chunks, max 2 per doc for diversity, substantial text only.
    # No scan cap: corpus docs hold 1,000+ chunks each, so a capped scan only
    # crosses 2-3 docs before the per-doc limit starves the sample.
    rows: list[dict] = []
    per_doc: dict[str, int] = {}
    async for r in db.chunks.find(
        {"corpus_id": CORPUS, "chunk_kind": "body"},
        {"chunk_id": 1, "doc_id": 1, "text": 1},
    ):
        t = r.get("text") or ""
        if len(t) < 300:
            continue
        if per_doc.get(r["doc_id"], 0) >= 2:
            continue
        per_doc[r["doc_id"]] = per_doc.get(r["doc_id"], 0) + 1
        rows.append(r)
        if len(rows) >= n + offset:
            break
    rows = rows[offset:]
    if len(rows) < n:
        print(f"FATAL: only {len(rows)} usable chunks found (wanted {n})")
        return 1

    tasks = [
        ExtractionTask(
            chunk_id=f"probe-{i}-{r['chunk_id']}",  # unique id → no staging reuse
            doc_id=r["doc_id"],
            corpus_id=CORPUS,
            text=r["text"],
        )
        for i, r in enumerate(rows)
    ]
    pool = [
        {
            "model": MODEL,
            "base_url": base_url,
            "api_key": api_key,
            "max_concurrent": conc,
            "extra_params": {},
        }
    ]

    t0 = time.time()
    report = await extract_entities(tasks, pool=pool, return_report=True)
    wall = time.time() - t0

    results = report.results
    failures = report.failures
    ok, fail = len(results), len(failures)
    ents = sum(len(x.entities) for x in results)
    rels = [rel for x in results for rel in x.relations]
    facts = sum(len(x.facts) for x in results)
    typed = sum(1 for rel in rels if rel.predicate != "related_to")
    covered = sum(1 for x in results if x.entities or x.relations)

    print("=== RTX EXTRACTION LANE PROBE ===")
    print(f"endpoint: {base_url}  model: {MODEL}  concurrency: {conc}")
    print(f"chunks: {n}  ok: {ok}  failed: {fail}  (from {len(per_doc)} docs)")
    print(f"wall: {wall:.1f}s  →  {n / wall * 60:.1f} chunks/min  ({wall / n:.2f}s/chunk amortized)")
    print(f"coverage (≥1 entity|relation): {covered}/{ok} = {covered / max(ok, 1) * 100:.0f}%")
    print(
        f"entities: {ents} ({ents / max(ok, 1):.1f}/chunk)  "
        f"relations: {len(rels)} ({len(rels) / max(ok, 1):.1f}/chunk)  "
        f"facts: {facts} ({facts / max(ok, 1):.2f}/chunk)"
    )
    print(f"typed predicates: {typed}/{len(rels)} = {typed / max(len(rels), 1) * 100:.0f}% (rest related_to)")
    if isinstance(report.metrics, dict):
        slim = {k: v for k, v in report.metrics.items() if not isinstance(v, (list, dict))}
        print(f"metrics: {slim}")
    for f in failures[:6]:
        print("FAIL:", str(getattr(f, "chunk_id", "?"))[:40], str(getattr(f, "error", f))[:160])

    # Baselines (A/B 2026-07-04, same prompt machinery):
    #   deepseek v4-flash: 2.79 s/chunk serial, 69% typed predicates
    #   local GLiNER/GLiREL: 0.28 s/chunk, 57% typed, 0.4 facts/chunk, 62% coverage
    usable = ok >= n * 0.9 and covered >= ok * 0.5
    print("VERDICT:", "USABLE" if usable else "NOT USABLE (failures or coverage floor)")
    return 0 if usable else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
