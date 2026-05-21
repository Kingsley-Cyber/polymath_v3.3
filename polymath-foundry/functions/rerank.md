# `rerank.py`

Foundry Function — cross-encoder rerank of candidate chunks against the query.

> **DoD-laptop note:** paste the code block into `functions/rerank.py` on the Foundry side.

## Code

```python
"""
rerank.py — Foundry Function

PURPOSE
-------
Cross-encoder rerank of candidate chunks against the query. Parity with
v3.3 which used cross-encoder/ms-marco-MiniLM-L6-v2.

INPUTS
------
query: str
chunks: list[Chunk]
top_n: int  (default 12)

OUTPUT
------
list[Chunk] sorted by rerank score descending, length <= top_n. Each
returned Chunk has a rerank_score attached.

NOTES
-----
- The cross-encoder is served by AIP as a callable model.
- Truncate Chunk.text to 2048 chars before scoring (encoder context window).
- Deterministic.
"""

from functions.api import function
from ontology.objects import Chunk

from polymath_lib.rerank import cross_encoder_score


@function()
def rerank(query: str, chunks: list[Chunk], top_n: int = 12) -> list[Chunk]:
    if not chunks:
        return []
    q = (query or "").strip()
    pairs = [(q, c.text[:2048]) for c in chunks]
    scores = cross_encoder_score(pairs)
    scored = list(zip(chunks, scores))
    scored.sort(key=lambda x: x[1], reverse=True)
    out: list[Chunk] = []
    for c, s in scored[:top_n]:
        c.rerank_score = float(s)
        out.append(c)
    return out
```
