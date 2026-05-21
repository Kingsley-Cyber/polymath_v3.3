# `compose_answer.py`

Foundry Function — compose the final assistant text with inline citations and write the Message + Citation objects via Action.

> **DoD-laptop note:** paste the code block into `functions/compose_answer.py` on the Foundry side.

## Code

```python
"""
compose_answer.py — Foundry Function

PURPOSE
-------
Compose the final assistant text with inline numbered citations from the
top reranked chunks. Writes the Message and Citation Ontology objects
through the CreateMessageWithCitations action — no raw upserts.

INPUTS
------
query: str
chunks: list[Chunk]            (already reranked; order is citation order)
conversation_id: str

OUTPUT
------
message_id: str  (empty string if the LLM produced zero citation markers)

NOTES
-----
- The LLM is given ONLY the reranked chunks and the query. It must use
  bracketed citation markers [1], [2], etc. that correspond to chunks
  in the input order.
- After the LLM returns, the function parses citation markers, builds
  Citation objects with span_start/span_end into the cited chunk text,
  and calls the action.
- If the LLM produces no citations, the function rejects the answer and
  returns an empty message_id. The agent decides whether to retry or
  ask a clarifying question.
"""

from functions.api import function

from polymath_lib.llm import aip_chat
from polymath_lib.citations import parse_citations
from polymath_lib.actions import create_message_with_citations


COMPOSE_PROMPT = (
    "Answer the user's question using ONLY the numbered context chunks below. "
    "Use inline citation markers like [1], [2] referencing the chunk numbers. "
    "If the chunks do not contain the answer, say so explicitly.\n\n"
    "Question: {query}\n\n"
    "Context chunks:\n{context}\n\n"
    "Answer:"
)


@function()
def compose_answer(query: str, chunks: list, conversation_id: str) -> str:
    if not chunks:
        return ""

    context = "\n\n".join(f"[{i+1}] {c.text}" for i, c in enumerate(chunks))
    answer_text = aip_chat(
        prompt=COMPOSE_PROMPT.format(query=query, context=context),
        temperature=0.0,
        max_tokens=900,
    )

    citation_spans = parse_citations(answer_text, chunks)
    if not citation_spans:
        return ""

    message_id = create_message_with_citations(
        conversation_id=conversation_id,
        content=answer_text,
        citations=citation_spans,
    )
    return message_id
```
