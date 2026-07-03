"""§12.6 waterfall graph serving — pure helpers for Mode A payload-first hops.

Monolithic graph intelligence is computed OFFLINE at promote time
(neighbor_chunks[], graph_neighbors[], graph_degree — see
services/ingestion/promote.py). This module holds the pure, infra-free
pieces of the ONLINE escalation ladder:

    cached signals (payload adjacency)  →  shallow live Cypher  →  deep

- slug_candidates():        query text → candidate entity_ids for the
                            indexed Neo4j existence check (A1 linking)
- score_payload_neighbors(): seed neighbor_chunks[] → ranked expansion
                            candidates by adjacency votes (zero Cypher)
- ExpansionCache:           TTL cache for the whole expansion result (G3)

Everything here is deterministic: fixed tokenization, fixed vote scoring,
lexicographic tie-breaks. No Neo4j, no Mongo, no clocks (injected).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Hashable, Iterable, Optional

# Tokenizer for n-gram generation only. The gram -> entity_id mapping is the
# caller-injected canonical fn (neo4j_writer.entity_id_from_name in-app); the
# built-in default is a faithful replica (hyphens, punctuation stripped) so
# the two sides of the vector<->graph join always agree — an underscore slug
# here silently never matches any multi-word graph entity.
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Minimal function-word list: blocks junk unigrams ("the", "how") without
# vetoing domain terms. Multi-grams keep inner stopwords ("theory_of_mind").
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have how in is it its of on or "
    "that the this to was what when where which who why will with does do did "
    "can could should would about into over under between across".split()
)

_MAX_NGRAM = 3


def _default_entity_id(name: str) -> str:
    """Replica of neo4j_writer.entity_id_from_name (minus the alias map):
    lowercase -> strip punctuation -> collapse spaces -> hyphens."""
    text = re.sub(r"[^\w\s]", "", (name or "").lower().strip())
    slug = re.sub(r"\s+", " ", text).strip().replace(" ", "-")
    return f"entity:{slug}" if slug else ""


def slug_candidates(
    query: str,
    cap: int = 24,
    entity_id_fn: Optional[Callable[[str], str]] = None,
) -> list[str]:
    """Query text → ordered unique entity_id candidates ("entity:{slug}").

    Generates 1..3-gram slugs over the query tokens, longest n-grams first
    (a 3-gram match is a stronger link than its unigrams), skipping
    stopword/short unigrams. The caller feeds these to ONE indexed
    `WHERE e.entity_id IN $cands` existence check — candidate order only
    matters for the cap, so it is deterministic: n-gram length desc, then
    query position.
    """
    eid_fn = entity_id_fn or _default_entity_id
    tokens = [t for t in _SLUG_RE.split((query or "").lower()) if t]
    if not tokens:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for n in range(min(_MAX_NGRAM, len(tokens)), 0, -1):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i : i + n]
            if n == 1 and (gram[0] in _STOPWORDS or len(gram[0]) < 3):
                continue
            if any(t in _STOPWORDS for t in (gram[0], gram[-1])) and n > 1:
                # edge stopwords make degenerate grams ("of mind"); inner ok
                continue
            eid = eid_fn(" ".join(gram))
            if eid and eid not in seen:
                seen.add(eid)
                out.append(eid)
                if len(out) >= cap:
                    return out
    return out


def score_payload_neighbors(
    seed_neighbor_map: dict[str, list[str]],
    *,
    exclude: Optional[Iterable[str]] = None,
    cap: int = 32,
) -> list[tuple[str, int, float]]:
    """seed chunk_id → its neighbor_chunks[] payload  ⇒  ranked candidates.

    votes = number of seeds listing the chunk as a neighbor. score is a
    bounded monotone map of votes (0.30 base + 0.20/vote, capped 1.0) so a
    multi-seed-adjacent chunk outranks a single-edge one but payload hops
    never drown the vector pool. Deterministic: (-votes, chunk_id) order.
    Returns [(chunk_id, votes, score)].
    """
    excluded = set(exclude or ()) | set(seed_neighbor_map.keys())
    votes: dict[str, int] = {}
    for nbrs in seed_neighbor_map.values():
        for cid in nbrs or []:
            if cid and cid not in excluded:
                votes[cid] = votes.get(cid, 0) + 1
    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[:cap]
    return [(cid, v, min(1.0, 0.30 + 0.20 * v)) for cid, v in ranked]


def prefer_relation_seeds(
    ordered_ids: list[str],
    has_relations: dict[str, bool],
    limit: int,
) -> list[str]:
    """G1 seed preference — relation-bearing chunks first, each group keeping
    the incoming (score) order. `ordered_ids` MUST already be score-desc."""
    if limit <= 0:
        return []
    yes = [c for c in ordered_ids if has_relations.get(c)]
    no = [c for c in ordered_ids if not has_relations.get(c)]
    return (yes + no)[:limit]


class ExpansionCache:
    """G3 — TTL cache for full Mode A expansion results (~180s).

    Keyed by the caller (corpora + vector seed set + limit + query). Values
    are stored/returned via a caller-supplied copy function so downstream
    score/provenance mutation can never poison an entry. Monotonic clock is
    injectable for tests. Size-capped by evicting the stalest entry.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._store: dict[Hashable, tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(
        corpus_ids: Optional[Iterable[str]],
        seed_ids: Iterable[str],
        limit: int,
        query: Optional[str],
    ) -> Hashable:
        return (
            tuple(sorted(str(c) for c in corpus_ids or ())),
            frozenset(seed_ids),
            int(limit),
            (query or "").strip().lower(),
        )

    def get(self, key: Hashable, copy: Callable[[Any], Any]) -> Any:
        if self.ttl <= 0:
            return None
        hit = self._store.get(key)
        if hit is None:
            self.misses += 1
            return None
        ts, value = hit
        if self._clock() - ts > self.ttl:
            self._store.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return copy(value)

    def put(self, key: Hashable, value: Any, copy: Callable[[Any], Any]) -> None:
        if self.ttl <= 0:
            return
        if len(self._store) >= self.max_entries and key not in self._store:
            oldest = min(self._store.items(), key=lambda kv: kv[1][0])[0]
            self._store.pop(oldest, None)
        self._store[key] = (self._clock(), copy(value))
