"""Multi-corpus input normalization.

Single source of truth for collapsing the legacy single `corpus_id: str`
inputs and the new `corpus_ids: list[str]` inputs into a canonical
`list[str]` at every service entry point.

PR 1 of the multi-corpus rollout (see Phased Rollout Plan + MULTICORPUS_BRIEF.md).
No runtime feature flag — backward compat is provided purely by input
normalization. The `DISABLE_MULTI_CORPUS` env var is a deploy-time emergency
kill switch that rejects any input with more than one corpus.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Iterable, Sequence


__all__ = [
    "normalize_corpus_ids",
    "is_multi_corpus_disabled",
    "MultiCorpusDisabledError",
    "compute_multi_corpus_signature",
]


class MultiCorpusDisabledError(ValueError):
    """Raised when DISABLE_MULTI_CORPUS=true and the caller passed >1 corpus.

    Routers should catch this and return 400. The message is safe to surface
    to clients — it names the flag so operators can identify the cause.
    """


def is_multi_corpus_disabled() -> bool:
    """Read the deploy-time emergency kill switch.

    Returns True only if `DISABLE_MULTI_CORPUS` env var is set to a truthy
    string (`"true"`, `"1"`, `"yes"`, case-insensitive). Anything else,
    including unset, returns False.
    """
    raw = os.getenv("DISABLE_MULTI_CORPUS", "").strip().lower()
    return raw in ("true", "1", "yes", "on")


def normalize_corpus_ids(
    corpus_id: str | None = None,
    corpus_ids: Sequence[str] | None = None,
) -> list[str]:
    """Collapse legacy single + new plural inputs into a canonical list.

    Resolution rules (matches the Pydantic model_validator pattern in
    backend/models/schemas.py):
      1. If `corpus_ids` is non-empty, it wins. `corpus_id` is ignored.
      2. Else if `corpus_id` is a non-empty string, wrap into [corpus_id].
      3. Else return [].

    Order is preserved as given. Duplicates are NOT removed here — callers
    that need deduplication should do it explicitly so order semantics
    stay obvious at the call site.

    Raises MultiCorpusDisabledError if DISABLE_MULTI_CORPUS=true and the
    resolved list has more than one element.
    """
    if corpus_ids:
        resolved = [str(c) for c in corpus_ids if c]
    elif corpus_id:
        resolved = [str(corpus_id)]
    else:
        resolved = []

    if len(resolved) > 1 and is_multi_corpus_disabled():
        raise MultiCorpusDisabledError(
            "DISABLE_MULTI_CORPUS=true rejects requests with more than one "
            f"corpus_id. Received {len(resolved)}."
        )

    return resolved


def compute_multi_corpus_signature(
    per_corpus_signatures: dict[str, str] | Iterable[tuple[str, str]],
) -> str:
    """Stable sha256 over (corpus_id, signature) pairs sorted by corpus_id.

    Deterministic regardless of input order so the same selection produces
    the same cache key across requests. Use for any cache that needs to
    invalidate when ANY of the selected corpora changes (e.g. multi-corpus
    overview merges, query result caches, schema-lens refresh audit keys).

    Per-corpus signatures themselves are computed by
    services.graph.analytics.compute_corpus_change_signature(db, corpus_id).
    """
    if isinstance(per_corpus_signatures, dict):
        items = list(per_corpus_signatures.items())
    else:
        items = list(per_corpus_signatures)

    parts = [f"{cid}:{sig}" for cid, sig in sorted(items, key=lambda x: x[0])]
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def utcnow_isoformat() -> str:
    """Helper for audit timestamps. Centralized so PR 2's audit log writes
    use the same ISO-8601 formatting as everything else in the codebase."""
    return datetime.utcnow().isoformat()


# ─── HTTP-layer helper (PR 2) ────────────────────────────────────────────────
#
# Lives here (not in routers/graph.py) so unit tests can exercise it
# without importing the routers package — which transitively pulls in
# auth → jose, a Docker-only host dependency that isn't installed on
# every developer machine.

# Default cap mirrors RetrievalSettings.max_corpora_per_query default.
_DEFAULT_MAX_CORPORA_PER_REQUEST = 32


def validate_corpus_ids_or_400(
    body: dict,
    *,
    max_corpora: int = _DEFAULT_MAX_CORPORA_PER_REQUEST,
) -> list[str]:
    """Pull and validate corpus_ids from a request body.

    Raises fastapi.HTTPException(400) on:
      • non-list shape
      • empty list (after legacy single-id wrapping)
      • DISABLE_MULTI_CORPUS env var rejecting multi-corpus inputs
      • count > max_corpora

    Returns the canonical list[str] for the route handler to use.
    """
    # Local import so this module stays usable in environments without
    # FastAPI installed (e.g. lightweight scripts that only need
    # normalize_corpus_ids).
    from fastapi import HTTPException

    raw = body.get("corpus_ids")
    legacy = body.get("corpus_id")
    if raw is not None and not isinstance(raw, list):
        raise HTTPException(
            status_code=400, detail="corpus_ids must be a list of strings"
        )
    try:
        ids = normalize_corpus_ids(corpus_id=legacy, corpus_ids=raw)
    except MultiCorpusDisabledError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not ids:
        raise HTTPException(
            status_code=400, detail="corpus_ids must be a non-empty list"
        )

    if len(ids) > max_corpora:
        raise HTTPException(
            status_code=400,
            detail=f"max {max_corpora} corpora per request (received {len(ids)})",
        )
    return ids
