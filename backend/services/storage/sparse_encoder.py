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

Tokenization:
    Ingest and query text are NFKC-normalized before tokenization. Tokens are
    built from Unicode letters/numbers plus identifier connectors, with script
    changes treated as token boundaries. That keeps exact identifiers such as
    "NSN 5340-01-234-5678", "para 3-2.1", "Qwen3-Embedding", and code-mixed
    Latin/CJK text searchable without routing through Mongo.

Token IDs:
    Qdrant sparse vectors index by uint32. We hash each token and clamp to
    the positive 31-bit range. Hash collisions are statistically rare with
    English alphanumeric tokens (~26^k * digits), and a collision merely
    conflates two terms' BM25 contributions — not catastrophic, just slight
    score noise. The same hash is used for ingest and query, so ingest-side
    `cat` and query-side `cat` always map to the same id.
"""
from __future__ import annotations

import unicodedata
from collections import Counter
from typing import Iterable
import re

# `qdrant_client.models.SparseVector` carries the wire format. We import
# lazily so this module stays importable in environments where qdrant-client
# isn't installed (e.g. analyser-only sub-packages).
try:  # pragma: no cover - import-shape only
    from qdrant_client.models import SparseVector
except Exception:  # pragma: no cover
    SparseVector = None  # type: ignore[assignment]


# Identifier connectors kept inside sparse tokens so exact lexical lookup can
# recover section numbers, NSNs, model names, paths, and code-ish identifiers.
_CONNECTORS = frozenset("_-./:#")
_CONNECTOR_SPLIT_RE = re.compile(r"[_\-./:#]+")
_CJK_SCRIPT_BUCKETS = frozenset({"cjk", "hiragana", "katakana", "hangul"})

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


def _script_bucket(ch: str) -> str:
    if not ch:
        return "other"
    category = unicodedata.category(ch)
    if category.startswith("N"):
        return "number"
    name = unicodedata.name(ch, "")
    if not name:
        return "other"
    if name.startswith("CJK"):
        return "cjk"
    first = name.split(" ", 1)[0].lower()
    if first in {
        "latin",
        "greek",
        "cyrillic",
        "arabic",
        "hebrew",
        "devanagari",
        "hiragana",
        "katakana",
        "hangul",
    }:
        return first
    return first


def _is_word_char(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category.startswith(("L", "N"))


def _ascii_fold(token: str) -> str:
    return (
        unicodedata.normalize("NFKD", token)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def _append_token(out: list[str], token: str) -> None:
    token = token.strip("".join(_CONNECTORS)).lower()
    if len(token) < 2 or token in _STOPWORDS:
        return
    seen: set[str] = set()
    variants = [token]
    folded = _ascii_fold(token)
    if folded and folded != token:
        variants.append(folded)
    for part in _CONNECTOR_SPLIT_RE.split(token):
        if part and part != token:
            variants.append(part)
            folded_part = _ascii_fold(part)
            if folded_part and folded_part != part:
                variants.append(folded_part)

    # CJK-style scripts often omit spaces. Keep the full run and add bigrams so
    # shorter lexical queries can still match without a language-specific
    # tokenizer dependency.
    if all(_script_bucket(ch) in _CJK_SCRIPT_BUCKETS for ch in token) and len(token) > 2:
        variants.extend(token[idx : idx + 2] for idx in range(0, len(token) - 1))

    for item in variants:
        item = item.strip("".join(_CONNECTORS)).lower()
        if len(item) < 2 or item in _STOPWORDS or item in seen:
            continue
        seen.add(item)
        out.append(item)


def _tokenize(text: str | None) -> list[str]:
    """NFKC normalize + script-aware tokenize + stopword/variant handling."""
    if not text:
        return []
    normalized = unicodedata.normalize("NFKC", text)
    tokens: list[str] = []
    buf: list[str] = []
    current_script: str | None = None

    def flush() -> None:
        nonlocal buf, current_script
        if buf:
            _append_token(tokens, "".join(buf))
        buf = []
        current_script = None

    for ch in normalized:
        if _is_word_char(ch):
            script = _script_bucket(ch)
            if (
                buf
                and current_script
                and script != current_script
                and script != "number"
                and current_script != "number"
            ):
                flush()
            buf.append(ch)
            if script != "number":
                current_script = script
            elif current_script is None:
                current_script = "number"
            continue
        if ch in _CONNECTORS and buf:
            buf.append(ch)
            continue
        flush()
    flush()
    return tokens


def _token_id(token: str) -> int:
    """Stable, deterministic uint32-range id for a token.

    Python's built-in hash() is randomized per process via PYTHONHASHSEED,
    so it can't be used. We use a deterministic FNV-1a-ish rolling hash
    so ingest-side and query-side ids always agree, regardless of which
    process computes them.
    """
    # FNV-1a 32-bit over UTF-8 bytes. For ASCII tokens this is byte-for-byte
    # identical to the old ord(ch) loop; for non-Latin text it hashes the full
    # code point representation instead of truncating to the low byte.
    h = 0x811C9DC5
    for byte in token.encode("utf-8"):
        h ^= byte
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
