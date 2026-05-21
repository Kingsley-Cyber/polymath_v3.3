# `hybrid_search.py`

Foundry Function — vector ANN + lexical search over Chunks, fused with reciprocal-rank-fusion.

> **DoD-laptop note:** paste the code block into `functions/hybrid_search.py` on the Foundry side.

## Code

```python
"""
hybrid_search.py — Foundry Function

PURPOSE
-------
Run vector ANN over Chunks via Foundry Vector Search Service AND a
lexical pass over Chunk.text, then fuse the two ranked lists with
reciprocal-rank-fusion (RRF). Filters by corpus_ids.

INPUTS
------
query: str
corpus_ids: list[str]
k: int   (top-K to return, default 40)

OUTPUT
------
list of Chunk objects (length <= k), in fused-score order.

NOTES
-----
- Vector retrieval uses query_embed(query); if expansion was performed
  upstream, the agent calls hybrid_search once per expanded query and
  fuses externally.
- Lexical uses Foundry's text index over Chunk.text (BM25-equivalent).
- RRF k constant = 60 (literature default).
"""

from functions.api import function
from ontology.objects import Chunk

from polymath_lib.embedding import embed_query
from polymath_lib.vector_search import ann_search
from polymath_lib.lexical import lexical_search


RRF_K = 60


@function()
def hybrid_search(query: str, corpus_ids: list[str], k: int = 40) -> list[Chunk]:
    q = (query or "").strip()
    if not q or not corpus_ids:
        return []

    vec = embed_query(q)

    ann_hits = ann_search(
        index_name="polymath_chunks",
        vector=vec,
        filter={"corpus_id": {"$in": corpus_ids}},
        k=k,
    )
    lex_hits = lexical_search(
        text_index="polymath_chunks_text",
        query=q,
        filter={"corpus_id": {"$in": corpus_ids}},
        k=k,
    )

    scores: dict[str, float] = {}
    for rank, hit in enumerate(ann_hits, start=1):
        scores[hit["chunk_id"]] = scores.get(hit["chunk_id"], 0.0) + 1.0 / (RRF_K + rank)
    for rank, hit in enumerate(lex_hits, start=1):
        scores[hit["chunk_id"]] = scores.get(hit["chunk_id"], 0.0) + 1.0 / (RRF_K + rank)

    ranked_ids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)[:k]
    chunks = Chunk.objects().filter(chunk_id__in=ranked_ids).all()

    by_id = {c.chunk_id: c for c in chunks}
    return [by_id[cid] for cid in ranked_ids if cid in by_id]
```
