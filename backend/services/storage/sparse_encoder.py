"""
BM25 sparse encoder — pure-Python tokenizer + sparse-vector builder for the
Qdrant hybrid-search migration.

Why this exists:
    The lexical-retrieval path used to live in Mongo (`$text` index + regex
    fallback). That worked but split the hybrid stack across two engines:
    dense scores from Qdrant, lexical scores from Mongo, merged after the
    fact. Qdrant 1.10+ supports first-class sparse vectors, so we move the
    BM25 weights into Qdrant alongside the dense embedding. One engine, one
    filter pass (`chunk_kind`, `corpus_id`, `chunk_type`), one round-trip
    per retrieval — lower latency, simpler merge.

Why pure Python:
    The `fastembed` BM25 model is the canonical option but it pulls in a
    Rust tokenizer + ~30 MB of resources. We're constrained on GPU memory
    (3090 holds docling, 4070 holds the dense embedder), and BM25 itself
    is a stateless tokenize-and-count. ~80 LOC of Python is enough; we
    don't need a Rust crate to count words.

Server-side IDF:
    The collection is created with `SparseVectorParams(modifier=Modifier.IDF)`,
    so Qdrant accumulates document frequencies on its side. Clients only
    need to send raw term frequencies as the sparse vector values. No
    corpus-statistics state on the client.

Token IDs:
    Qdrant sparse vectors index by uint32. We hash each token and clamp to
    the positive 31-bit range. Hash collisions are statistically rare with
    English alphanumeric tokens (~26^k * digits), and a collision merely
    conflates two terms' BM25 contributions — not catastrophic, just slight
    score noise. The same hash is used for ingest and query, so ingest-side
    `cat` and query-side `cat` always map to the same id.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Iterable

# `qdrant_client.models.SparseVector` carries the wire format. We import
# lazily so this module stays importable in environments where qdrant-client
# isn't installed (e.g. analyser-only sub-packages).
try:  # pragma: no cover - import-shape only
    from qdrant_client.models import SparseVector
except Exception:  # pragma: no cover
    SparseVector = None  # type: ignore[assignment]


# Token-pattern: alphanumerics + underscore, length ≥ 2. Matches model
# names ("gpt4", "qwen3"), version strings ("v1.5"), function names, etc.
# Drops single characters and punctuation.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")

# Stopword set — kept tight because BM25's IDF already down-weights frequent
# terms. We only drop true high-frequency function words that have no
# semantic content of their own and would otherwise inflate every vector.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
    "for", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did",
    "this", "that", "these", "those", "it", "its", "itself",
    "if", "then", "than", "so", "such",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "they", "them",
    "his", "her", "their",
    "not", "no", "yes",
})


def _tokenize(text: str | None) -> list[str]:
    """Lowercase + strip diacritics + regex tokenize + drop stopwords."""
    if not text:
        return []
    # NFKD normalize then ASCII-strip diacritics (résumé → resume) so
    # query-side and ingest-side tokens match across encodings.
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return [
        tok.lower()
        for tok in _TOKEN_RE.findall(ascii_only)
        if tok.lower() not in _STOPWORDS
    ]


def _token_id(token: str) -> int:
    """Stable, deterministic uint32-range id for a token.

    Python's built-in hash() is randomized per process via PYTHONHASHSEED,
    so it can't be used. We use a deterministic FNV-1a-ish rolling hash
    so ingest-side and query-side ids always agree, regardless of which
    process computes them.
    """
    # FNV-1a 32-bit
    h = 0x811C9DC5
    for ch in token:
        h ^= ord(ch) & 0xFF
        h = (h * 0x01000193) & 0xFFFFFFFF
    # Clamp to positive 31-bit (Qdrant uses uint32, but let's stay safe)
    return h & 0x7FFFFFFF


def encode_text(text: str | None) -> "SparseVector":
    """Return a SparseVector of {token_id: term_frequency} for `text`.

    Used at ingest time on every chunk and summary. Empty input returns
    an empty SparseVector — Qdrant accepts these and they contribute zero
    to BM25 score.
    """
    if SparseVector is None:
        raise RuntimeError(
            "qdrant_client.models.SparseVector unavailable; install qdrant-client"
        )
    tokens = _tokenize(text)
    if not tokens:
        return SparseVector(indices=[], values=[])
    counts = Counter(tokens)
    seen: set[int] = set()
    indices: list[int] = []
    values: list[float] = []
    for token, tf in counts.items():
        idx = _token_id(token)
        if idx in seen:
            # First-token-wins on hash collision. Rare; merely conflates
            # BM25 contributions, which IDF re-weights anyway.
            continue
        seen.add(idx)
        indices.append(idx)
        values.append(float(tf))
    return SparseVector(indices=indices, values=values)


def encode_query(text: str | None) -> "SparseVector":
    """Encode a query identically to a chunk. BM25 treats the query as a
    very short document where each term occurs once; our `encode_text`
    already gives that shape because Counter handles single occurrences
    correctly. Exposed under a separate name so call-sites are explicit.
    """
    return encode_text(text)


def encode_many(texts: Iterable[str | None]) -> list["SparseVector"]:
    """Vectorize a batch of texts. Same as map(encode_text, texts), wrapped
    so callers (worker.py) can stay symmetric with `embed_batch`.
    """
    return [encode_text(t) for t in texts]
