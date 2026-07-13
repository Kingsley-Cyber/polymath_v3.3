"""Vocabulary-resolution cache (checklist P1.7).

`CorpusVocabularyResolver.resolve()` is the slowest pre-retrieval stage
(measured 1.8-18.1s against a 360k-row lexicon). Its output is a pure
function of (query text, lane queries, selected corpus set, knobs) and the
underlying lexicon/tree artifacts, so repeated conversational queries can be
served from memory.

Key design:
  - key = sha256 over normalized query, ordered lane queries, sorted corpus
    ids, tier, top_k, disabled ids, exclusions, plus a per-corpus EPOCH
    tuple. Query vectors are excluded: they are deterministic per query text
    under the deployed embedder (an embedder swap is a service restart,
    which empties this in-process cache anyway).
  - epochs: in-process writers (lexicon materialization, promote mirror,
    corpus deletion) bump `bump_corpus_epoch(corpus_id)`, invalidating every
    cached resolution touching that corpus without scanning keys.
  - TTL bounds cross-process staleness: ingestion usually runs in a separate
    worker process whose writes cannot bump this process's epochs, so
    entries also expire after VOCAB_RESOLUTION_CACHE_TTL_SECONDS (default
    300). Lexicon changes are batch events gated by readiness, so a bounded
    staleness window is acceptable; set the TTL to 0 to disable caching.
  - values are deep-copied on put and on get: downstream code mutates the
    resolution dict freely.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import OrderedDict
from threading import Lock
from typing import Any

from config import get_settings

_lock = Lock()
_entries: "OrderedDict[str, tuple[float, dict[str, Any]]]" = OrderedDict()
_corpus_epochs: dict[str, int] = {}
_stats = {"hits": 0, "misses": 0, "invalidations": 0}


def _ttl_seconds() -> float:
    return float(
        getattr(get_settings(), "VOCAB_RESOLUTION_CACHE_TTL_SECONDS", 300.0) or 0.0
    )


def _max_entries() -> int:
    return max(
        16,
        int(getattr(get_settings(), "VOCAB_RESOLUTION_CACHE_MAX_ENTRIES", 512) or 512),
    )


def enabled() -> bool:
    return (
        bool(getattr(get_settings(), "VOCAB_RESOLUTION_CACHE", True))
        and _ttl_seconds() > 0
    )


def bump_corpus_epoch(corpus_id: str) -> None:
    """Invalidate every cached resolution that touches this corpus."""

    cid = str(corpus_id or "")
    if not cid:
        return
    with _lock:
        _corpus_epochs[cid] = _corpus_epochs.get(cid, 0) + 1
        _stats["invalidations"] += 1


def resolution_cache_key(
    *,
    query: str,
    corpus_ids: list[str] | None,
    tier: Any,
    top_k_per_corpus: int,
    lane_queries: list[tuple[str, str]],
    disabled_lexicon_ids: list[str] | None,
    excluded_terms: list[str] | None,
) -> str:
    corpora = sorted(dict.fromkeys(str(c) for c in (corpus_ids or []) if c))
    with _lock:
        epochs = [(cid, _corpus_epochs.get(cid, 0)) for cid in corpora]
    payload = json.dumps(
        {
            "v": 1,
            "q": " ".join(str(query or "").lower().split()),
            "lanes": lane_queries,
            "corpora": epochs,
            "tier": getattr(tier, "value", str(tier)),
            "k": int(top_k_per_corpus),
            "disabled": sorted(str(x) for x in (disabled_lexicon_ids or [])),
            "excluded": sorted(str(x).strip().lower() for x in (excluded_terms or [])),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get(key: str) -> dict[str, Any] | None:
    if not enabled():
        return None
    now = time.monotonic()
    with _lock:
        row = _entries.get(key)
        if row is None:
            _stats["misses"] += 1
            return None
        expires_at, payload = row
        if expires_at < now:
            _entries.pop(key, None)
            _stats["misses"] += 1
            return None
        _entries.move_to_end(key)
        _stats["hits"] += 1
        return copy.deepcopy(payload)


def put(key: str, payload: dict[str, Any]) -> None:
    if not enabled():
        return
    now = time.monotonic()
    with _lock:
        _entries[key] = (now + _ttl_seconds(), copy.deepcopy(payload))
        _entries.move_to_end(key)
        while len(_entries) > _max_entries():
            _entries.popitem(last=False)


def stats() -> dict[str, int]:
    with _lock:
        return {**_stats, "entries": len(_entries)}


def clear() -> None:
    with _lock:
        _entries.clear()
        _corpus_epochs.clear()
        for key in _stats:
            _stats[key] = 0
