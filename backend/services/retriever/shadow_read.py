"""q8 (owner directive 2026-08-04) — request-scoped shadow-read state.

Shadow read routes ONE explicit request's retrieval through the candidate
one-point-per-child evidence collection instead of the legacy
naive/hrag/graph family. It is:

* EXPLICIT — enabled only via an opt-in signal (HTTP header
  ``X-Polymath-Q8-Shadow-Read`` carrying the corpus UUIDs);
* SCOPED — only the listed corpora are shadowed, everything else stays on
  the legacy topology;
* DEFAULT OFF — the contextvar default is an empty set, so ordinary traffic
  is byte-identical to pre-q8 behavior.

The contextvars are set at the API entry of a request and copied into every
task spawned while handling it, which makes the override request-scoped
without any global switch (owner hard boundary: global_read_switch
prohibited).
"""

from __future__ import annotations

from contextvars import ContextVar

# Corpus UUIDs whose retrieval reads the candidate evidence collection for
# the current request. Empty = shadow read off (production default).
SHADOW_READ_CORPORA: ContextVar[frozenset[str]] = ContextVar(
    "q8_shadow_read_corpora", default=frozenset()
)

# Route-eligibility filter funnel_b must apply when it reads an evidence
# collection: "eligible_focused" on the focused tier, "eligible_graph_seed"
# on the graph tier. Set by `_resolve_collections`, which is the only seam
# that knows both the tier and the resolved collection names.
FUNNEL_B_ELIGIBILITY: ContextVar[str | None] = ContextVar(
    "q8_funnel_b_eligibility", default=None
)

_EVIDENCE_SUFFIX = "_evidence"


def shadow_read_enabled(corpus_id: str) -> bool:
    return corpus_id in SHADOW_READ_CORPORA.get()


def apply_shadow_read_header(header_value: str | None) -> frozenset[str]:
    """q8 (owner directive 2026-08-04) — explicit opt-in seam.

    Parses the ``X-Polymath-Q8-Shadow-Read`` request header (comma-separated
    corpus UUIDs) and activates shadow read for the current request context.
    Missing/empty header leaves production behavior byte-identical. Returns
    the activated set so callers can log exactly which route ran.

    The contextvar is deliberately NOT reset here: the value is bound to the
    request's copied context and must stay visible to the streamed response
    generator that executes after the handler returns.
    """
    if not header_value or not header_value.strip():
        return frozenset()
    corpora = frozenset(
        token.strip() for token in header_value.split(",") if token.strip()
    )
    SHADOW_READ_CORPORA.set(corpora)
    return corpora


def is_evidence_collection(collection_name: str) -> bool:
    return collection_name.endswith(_EVIDENCE_SUFFIX)
