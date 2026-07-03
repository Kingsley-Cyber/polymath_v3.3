"""W1 Tier-0 routing PROBE — search the universal doc_summaries collection.

Usage (inside the backend container):
    python3 scripts_probe_tier0.py "your question" [corpus_id ...]

Embeds the query, searches polymath_doc_summaries (across ALL corpora unless
corpus_ids given — §11.0 universal routing), prints the routing cards. This
is the GATED groundwork receipt for TIER0_ROUTING: it proves the collection
answers "which documents should this query even look at?" before any
query-path wiring happens.
"""
import asyncio
import sys

sys.path.insert(0, ".")

from config import get_settings  # noqa: E402


async def main(query: str, corpus_ids: list[str]) -> None:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client import models as qm

    from services.embedder import embed_batch
    from services.ingestion.tier0 import SHARED_DOCSUM

    s = get_settings()
    client = AsyncQdrantClient(url=s.QDRANT_URL, timeout=30)
    total = (await client.count(collection_name=SHARED_DOCSUM, exact=True)).count
    print(f"{SHARED_DOCSUM}: {total} routing cards")

    dim_info = await client.get_collection(SHARED_DOCSUM)
    vecs = dim_info.config.params.vectors
    dim = vecs["dense"].size if isinstance(vecs, dict) else vecs.size
    vector = (await embed_batch([query], expected_dim=dim))[0]

    flt = None
    if corpus_ids:
        flt = qm.Filter(must=[
            qm.FieldCondition(key="corpus_id", match=qm.MatchAny(any=corpus_ids))
        ])
    hits = await client.query_points(
        collection_name=SHARED_DOCSUM,
        query=vector,
        using="dense",
        query_filter=flt,
        limit=8,
        with_payload=True,
    )
    print(f"\nquery: {query!r}  scope: {corpus_ids or 'ALL corpora'}")
    for h in hits.points:
        p = h.payload or {}
        print(f"  {h.score:.3f}  [{str(p.get('corpus_id'))[:8]}] {p.get('title') or p.get('doc_id')}")
        print(f"         concepts={list(p.get('concepts') or [])[:5]}")
    if not hits.points:
        print("  (no cards matched)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
