"""
Qdrant writer — per-corpus collection setup and idempotent vector upserts.

Phase 7.5 — every corpus owns its own family of 4 collections:
  corpus_{cid8}_naive    — all tiers: child + summary vectors
  corpus_{cid8}_hrag     — Tier A/B/B+ heading-aware vectors
  corpus_{cid8}_graph    — use_neo4j path, aligned with Neo4j Chunk nodes
  corpus_{cid8}_schemas  — Phase 14.2 schema-term family (entity_type / relation)

`{cid8}` is `corpus_id[:8]` — the first 8 hex chars of the UUID4. ~1-in-4-billion
collision odds per pair; first-write code asserts the existing collection's
payload.corpus_id matches and raises if not.

Point IDs are still deterministic MD5-derived UUIDs so re-ingest upserts the
same point IDs (no duplicates, even across re-runs of the migration script).

Legacy `_col()` is kept as a SOURCE-SIDE-ONLY helper for the one-shot migration
script (`migrations/001_per_corpus_qdrant.py`). All hot-path writers and readers
use `_col_for_corpus()`.
"""

import hashlib
import logging

from config import get_settings
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _uuid_from_str(s: str) -> str:
    digest = hashlib.md5(s.encode()).hexdigest()
    return (
        f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    )


def _child_point_id(chunk_id: str) -> str:
    return _uuid_from_str(chunk_id)


def _summary_point_id(corpus_id: str, parent_id: str) -> str:
    return _uuid_from_str(f"{corpus_id}:{parent_id}:summary")


def _schema_point_id(corpus_id: str, kind: str, term: str) -> str:
    """Phase 14.2 — deterministic point ID for a schema-term embedding.
    Same (corpus_id, kind, term) tuple always yields the same UUID, so re-embedding
    overwrites cleanly and never duplicates.
    """
    return _uuid_from_str(f"schema:{corpus_id}:{kind}:{term}")


_VALID_KINDS = ("naive", "hrag", "graph", "schemas")


def _col(key: str) -> str | None:
    """LEGACY — global collection name lookup. Used only by the migration
    script (`migrations/001_per_corpus_qdrant.py`) to read from the old
    polymath_* collections. Hot paths use `_col_for_corpus()` instead.
    """
    mapping = {
        "naive": settings.QDRANT_NAIVE,
        "hrag": settings.QDRANT_HRAG,
        "graph": settings.QDRANT_GRAPH,
        "schemas": settings.QDRANT_SCHEMAS,
    }
    return mapping.get(key)


def _col_for_corpus(corpus_id: str, kind: str) -> str:
    """Resolve the per-corpus collection name. Single source of truth.

    Args:
        corpus_id: full UUID string of the corpus.
        kind: one of {naive, hrag, graph, schemas}.

    Returns:
        f"{prefix}{corpus_id[:8]}_{kind}" — e.g. "corpus_a1b2c3d4_naive".
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"Invalid Qdrant kind {kind!r} (expected one of {_VALID_KINDS})")
    if not corpus_id:
        raise ValueError("corpus_id is required for per-corpus collection naming")
    return f"{settings.QDRANT_COLLECTION_PREFIX}{corpus_id[:8]}_{kind}"


async def _assert_collection_owner(
    client: AsyncQdrantClient,
    collection_name: str,
    corpus_id: str,
) -> None:
    """Defense against the 8-char prefix collision: if the collection already
    exists, scroll one point and verify its `corpus_id` payload matches the
    expected owner. Raises RuntimeError on mismatch so the caller halts before
    polluting another corpus's vectors.

    No-ops when the collection has no points yet (fresh creation).
    """
    try:
        records, _ = await client.scroll(
            collection_name=collection_name,
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
    except Exception:
        return  # Collection doesn't exist yet or transient error — let upsert handle it
    if not records:
        return
    existing_owner = (records[0].payload or {}).get("corpus_id")
    if existing_owner and existing_owner != corpus_id:
        msg = (
            f"Qdrant collection {collection_name!r} already owned by corpus "
            f"{existing_owner!r}, refusing write for corpus {corpus_id!r} "
            f"(8-char prefix collision)"
        )
        logger.warning(msg)
        raise RuntimeError(msg)


# Payload indexes per collection family. Schemas collection has its own shape
# (corpus_id, kind, term) — distinct from the chunk family.
_CHUNK_PAYLOAD_INDEXES: tuple[str, ...] = (
    "corpus_id",
    "doc_id",
    "chunk_id",
    "parent_id",
    "chunk_type",
    "source_tier",
    "user_id",
)
_SCHEMA_PAYLOAD_INDEXES: tuple[str, ...] = ("corpus_id", "kind", "term")


async def ensure_collections_for_corpus(
    client: AsyncQdrantClient, corpus_id: str, dim: int = 1024
) -> None:
    """Create the 4 per-corpus collections (naive/hrag/graph/schemas) if they
    do not exist. Idempotent.

    Called by `IngestionService.create_corpus` and by the migration script.
    """
    chunk_kinds = ("naive", "hrag", "graph")
    for kind in chunk_kinds:
        name = _col_for_corpus(corpus_id, kind)
        if await client.collection_exists(name):
            logger.debug("Qdrant collection exists: %s", name)
            continue

        await client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        for field_name in _CHUNK_PAYLOAD_INDEXES:
            await client.create_payload_index(
                collection_name=name,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        logger.info("Created Qdrant collection: %s (corpus %s)", name, corpus_id)

    schemas_name = _col_for_corpus(corpus_id, "schemas")
    if not await client.collection_exists(schemas_name):
        await client.create_collection(
            collection_name=schemas_name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        for field_name in _SCHEMA_PAYLOAD_INDEXES:
            await client.create_payload_index(
                collection_name=schemas_name,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        logger.info("Created Qdrant collection: %s (corpus %s)", schemas_name, corpus_id)


async def drop_collections_for_corpus(
    client: AsyncQdrantClient, corpus_id: str
) -> int:
    """Drop the 4 per-corpus collections. O(1) per-collection — replaces the
    old filter-delete cascade. Idempotent: missing collections are silently
    skipped. Returns the count of collections actually dropped.
    """
    dropped = 0
    for kind in _VALID_KINDS:
        name = _col_for_corpus(corpus_id, kind)
        try:
            if await client.collection_exists(name):
                await client.delete_collection(collection_name=name)
                logger.info("Dropped Qdrant collection: %s (corpus %s)", name, corpus_id)
                dropped += 1
        except Exception as exc:
            logger.warning(
                "Failed to drop Qdrant collection %s for corpus %s: %s",
                name,
                corpus_id,
                exc,
            )
    return dropped


async def upsert_children(
    client: AsyncQdrantClient,
    corpus_id: str,
    chunks: list[dict],
    vectors: list[list[float]],
    target_kinds: list[str],
) -> None:
    """
    Upsert child vectors into the per-corpus collections for `corpus_id`.

    Args:
        client: AsyncQdrantClient.
        corpus_id: owning corpus — resolves to per-corpus collection names.
        chunks: dicts with chunk_id, parent_id, doc_id, corpus_id,
                source_tier, heading_path, text, user_id.
        vectors: 1024-d embeddings, same order as chunks.
        target_kinds: subset of ["naive", "hrag", "graph"] — kind selectors,
            NOT collection names. Each is resolved via `_col_for_corpus`.
    """
    if not chunks or not vectors:
        return
    assert len(chunks) == len(vectors), "chunks and vectors length mismatch"

    points = [
        PointStruct(
            id=_child_point_id(c["chunk_id"]),
            vector=v,
            payload={
                "corpus_id": c["corpus_id"],
                "doc_id": c["doc_id"],
                "chunk_id": c["chunk_id"],
                "parent_id": c["parent_id"],
                "chunk_type": "child",
                "source_tier": c["source_tier"],
                "heading_path": c.get("heading_path"),
                "chunk_text": c["text"][:512],
                "user_id": c.get("user_id", ""),
            },
        )
        for c, v in zip(chunks, vectors)
    ]

    for kind in target_kinds:
        name = _col_for_corpus(corpus_id, kind)
        await _assert_collection_owner(client, name, corpus_id)
        await client.upsert(collection_name=name, points=points)
        logger.debug("Upserted %d child points → %s", len(points), name)


async def upsert_summaries(
    client: AsyncQdrantClient,
    corpus_id: str,
    summary_payloads: list[dict],
    vectors: list[list[float]],
    target_kinds: list[str],
) -> None:
    """
    Upsert summary vectors into per-corpus collections. Never written to graph.

    Args:
        client: AsyncQdrantClient.
        corpus_id: owning corpus — resolves to per-corpus collection names.
        summary_payloads: dicts with parent_id, corpus_id, doc_id,
                          source_tier, summary, heading_path, user_id.
        vectors: 1024-d embeddings.
        target_kinds: subset of ["naive", "hrag"] — "graph" is silently skipped.
    """
    if not summary_payloads or not vectors:
        return
    assert len(summary_payloads) == len(vectors)

    points = [
        PointStruct(
            id=_summary_point_id(p["corpus_id"], p["parent_id"]),
            vector=v,
            payload={
                "corpus_id": p["corpus_id"],
                "doc_id": p["doc_id"],
                "chunk_id": f"{p['parent_id']}_summary",
                "parent_id": p["parent_id"],
                "chunk_type": "summary",
                "source_tier": p["source_tier"],
                "heading_path": p.get("heading_path"),
                "chunk_text": p["summary"][:512],
                "user_id": p.get("user_id", ""),
            },
        )
        for p, v in zip(summary_payloads, vectors)
    ]

    for kind in target_kinds:
        if kind == "graph":
            continue  # summaries never seed the graph collection
        name = _col_for_corpus(corpus_id, kind)
        await _assert_collection_owner(client, name, corpus_id)
        await client.upsert(collection_name=name, points=points)
        logger.debug("Upserted %d summary points → %s", len(points), name)


# `delete_points_by_corpus` was removed in Phase 7.5 — per-corpus collections
# are dropped atomically via `drop_collections_for_corpus()`.  The legacy
# filter-delete is no longer needed because no collection holds points from
# more than one corpus.


# ── Phase 14.2 — Schema-Term Embedding (Ontology-Lite) ────────────────────


async def upsert_schema_terms(
    client: AsyncQdrantClient,
    corpus_id: str,
    terms: list[str],
    kind: str,  # "entity_type" or "relation"
    vectors: list[list[float]],
) -> int:
    """
    Upsert per-corpus schema-term embeddings into the polymath_schemas collection.

    Args:
        client: AsyncQdrantClient.
        corpus_id: corpus that owns these terms.
        terms: schema vocabulary strings (e.g. ["Unit", "Equipment"]).
        kind: "entity_type" or "relation" — discriminator stored in payload.
        vectors: pre-embedded term vectors, same order as `terms`.

    Returns:
        Number of points upserted (== len(terms) on success).

    Caller is responsible for embedding via embedder.embed_batch() so this module
    stays free of embedding dependencies.
    """
    if not terms or not vectors:
        return 0
    assert len(terms) == len(vectors), "terms and vectors length mismatch"

    name = _col_for_corpus(corpus_id, "schemas")
    await _assert_collection_owner(client, name, corpus_id)

    points = [
        PointStruct(
            id=_schema_point_id(corpus_id, kind, term),
            vector=v,
            payload={
                "corpus_id": corpus_id,
                "kind": kind,
                "term": term,
            },
        )
        for term, v in zip(terms, vectors)
    ]
    await client.upsert(collection_name=name, points=points)
    logger.debug(
        "Upserted %d schema terms (kind=%s) for corpus %s", len(points), kind, corpus_id
    )
    return len(points)


async def delete_schema_terms(
    client: AsyncQdrantClient,
    corpus_id: str,
    kind: str | None = None,
) -> bool:
    """
    Delete schema-term embeddings for a corpus.

    Args:
        client: AsyncQdrantClient.
        corpus_id: corpus to clear.
        kind: when provided, only delete points of this kind. When None, delete all
              schema-term points for the corpus (entity_type AND relation).

    Returns:
        True if the delete operation completed (Qdrant returned an operation_id).
    """
    name = _col_for_corpus(corpus_id, "schemas")
    if not await client.collection_exists(name):
        return False

    # Per-corpus collection already isolates by corpus_id; the payload filter
    # is kept as belt+suspenders so a partial-kind delete still works.
    must_filters = [
        FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
    ]
    if kind is not None:
        must_filters.append(
            FieldCondition(key="kind", match=MatchValue(value=kind)),
        )

    result = await client.delete(
        collection_name=name,
        points_selector=Filter(must=must_filters),
    )
    deleted = getattr(result, "operation_id", None) is not None
    logger.info(
        "Qdrant: deleted schema terms for corpus %s (kind=%s, success=%s)",
        corpus_id,
        kind or "ALL",
        deleted,
    )
    return deleted


async def retrieve_schema_for_chunk(
    client: AsyncQdrantClient,
    corpus_id: str,
    kind: str,
    query_vec: list[float],
    top_k: int = 10,
) -> list[str]:
    """
    Phase 14.2 — retrieve top-K most similar schema terms for a chunk's embedding.

    Used by ghost_b when the user's vocabulary exceeds SCHEMA_INLINE_LIMIT and
    the full vocab can't fit in the prompt context. Returns terms only (the LLM
    receives them as a constrained allowlist).

    Args:
        client: AsyncQdrantClient.
        corpus_id: scope to this corpus's schema only.
        kind: "entity_type" or "relation".
        query_vec: chunk vector to compare against.
        top_k: how many terms to return.

    Returns:
        List of term strings, ranked by cosine similarity (descending). Empty list
        when the schemas collection is missing or has no terms for this corpus.
    """
    name = _col_for_corpus(corpus_id, "schemas")
    if not await client.collection_exists(name):
        return []

    # Per-corpus collection already filters by corpus; payload filter on
    # corpus_id is kept as defense-in-depth, kind filter is the real selector.
    hits = await client.search(
        collection_name=name,
        query_vector=query_vec,
        query_filter=Filter(
            must=[
                FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
                FieldCondition(key="kind", match=MatchValue(value=kind)),
            ]
        ),
        limit=top_k,
        with_payload=True,
    )
    return [h.payload["term"] for h in hits if h.payload and "term" in h.payload]
