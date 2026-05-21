# `expand_query.py`

Foundry Function — HyDE-style query expansion for short or vague queries.

> **DoD-laptop note:** paste the code block into `functions/expand_query.py` on the Foundry side.

## Code

```python
"""
expand_query.py — Foundry Function

PURPOSE
-------
HyDE-style query expansion. Given a short or vague user query, ask an
AIP-hosted LLM to generate one or two pseudo-answers, and return those
plus the original query as a list. Downstream tools embed each variant
and fuse the results (this function does NOT embed).

INPUTS
------
query: str

OUTPUT
------
list[str] — 2 or 3 strings, original first

NOTES
-----
- Use only when query is short (< 8 tokens) or vague — the agent decides.
- LLM is the AIP default chat model, not a custom one.
- Determinism: temperature=0.0; seed pinned where possible.
"""

from functions.api import function

from polymath_lib.llm import aip_chat


HYDE_PROMPT = (
    "Write a concise paragraph that would directly answer the following "
    "question. Do not invent facts; speculate only on what a relevant "
    "source would say. Question: {query}"
)


@function()
def expand_query(query: str) -> list[str]:
    q = (query or "").strip()
    if not q:
        return []
    pseudo = aip_chat(
        prompt=HYDE_PROMPT.format(query=q),
        temperature=0.0,
        max_tokens=180,
    )
    pseudo2 = aip_chat(
        prompt=HYDE_PROMPT.format(query=q) + " Provide a different angle.",
        temperature=0.0,
        max_tokens=180,
    )
    return [q, pseudo, pseudo2]
```
