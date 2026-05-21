# `query_embed.py`

Foundry Function — embed a user query into the same 1024-dim space as the Chunks.

> **DoD-laptop note:** paste the code block into `functions/query_embed.py` on the Foundry side.

## Code

```python
"""
query_embed.py — Foundry Function

PURPOSE
-------
Embed a user query string into the same 1024-dim vector space as the
Chunks. Called by hybrid_search at the start of each retrieval turn.

INPUTS
------
query: str

OUTPUT
------
list[float] of length 1024

NOTES
-----
- Embedding model is the same one used by embed_chunks.py — pinned in
  polymath_lib.embedding so ingest and query stay in lockstep.
- Pure function: no Ontology read, no Ontology write. Safe to cache.
"""

from functions.api import function

from polymath_lib.embedding import embed_query, EMBEDDING_DIM


@function()
def query_embed(query: str) -> list[float]:
    if not query or not query.strip():
        return [0.0] * EMBEDDING_DIM
    return embed_query(query.strip())
```
