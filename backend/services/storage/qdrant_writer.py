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

import asyncio
import hashlib
import logging
import re

from config import get_settings
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    Modifier,
    PayloadSchemaType,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _upsert_batch_size() -> int:
    try:
        return max(1, int(settings.QDRANT_UPSERT_BATCH_SIZE))
    except Exception:
        return 256


async def _upsert_points_batched(
    client: AsyncQdrantClient,
    *,
    collection_name: str,
    points: list[PointStruct],
    point_label: str,
) -> None:
    if not points:
        return
    batch_size = _upsert_batch_size()
    for start in range(0, len(points), batch_size):
        batch = points[start : start + batch_size]
        try:
            await client.upsert(collection_name=collection_name, points=batch)
        except Exception:
            logger.exception(
                "Qdrant upsert failed collection=%s label=%s batch=%d-%d "
                "batch_size=%d total=%d",
                collection_name,
                point_label,
                start,
                start + len(batch) - 1,
                len(batch),
                len(points),
            )
            raise


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


_ALIAS_PREFIX = "corpus_"  # keeps aliases namespaced away from user-typed strings
_SLUG_MAX_LEN = 40


def _slugify_name(name: str) -> str:
    """Make a Qdrant-alias-safe slug from a corpus name. Lowercase, alnum+underscore
    only, collapsed runs, length-capped. Empty / non-ASCII-only names collapse to
    'unnamed' so we always have a usable slug (uniqueness handled by `[:cid8]`
    suffix in `_alias_for_corpus`).
    """
    if not name:
        return "unnamed"
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if not s:
        return "unnamed"
    return s[:_SLUG_MAX_LEN]


def _alias_for_corpus(corpus_id: str, name: str, kind: str) -> str:
    """Human-readable alias → physical collection. Format:
        corpus_{slug(name)}_{cid8}_{kind}
    The cid8 suffix keeps aliases unique even when two corpora share a name.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"Invalid Qdrant kind {kind!r}")
    slug = _slugify_name(name)
    return f"{_ALIAS_PREFIX}{slug}_{corpus_id[:8]}_{kind}"


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
    # Code lane (Phase 1) — chunk_kind was stored on payload but never
    # indexed, so default retrieval's `must_not in noisy_kinds` was a full
    # scan. Indexed here so filter-by-kind (including the new "code" kind)
    # is O(log n). language enables fast per-language scopes for /python,
    # /rust, /luau, etc.
    "chunk_kind",
    "language",
)
_SCHEMA_PAYLOAD_INDEXES: tuple[str, ...] = ("corpus_id", "kind", "term")


async def _collection_exists_safe(
    client: AsyncQdrantClient,
    collection_name: str,
) -> bool:
    try:
        return bool(await client.collection_exists(collection_name))
    except Exception as exc:
        logger.debug("Qdrant collection_exists failed for %s: %s", collection_name, exc)
        return False


async def _create_collection_with_retry(
    client: AsyncQdrantClient,
    *,
    collection_name: str,
    vectors_config,
    sparse_vectors_config=None,
    attempts: int = 3,
) -> None:
    """Create a collection with timeout tolerance.

    Qdrant can complete the create server-side after the HTTP client times out.
    On failure we immediately re-check existence and treat that as success.
    """
    if await _collection_exists_safe(client, collection_name):
        logger.debug("Qdrant collection exists: %s", collection_name)
        return

    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            kwargs = {
                "collection_name": collection_name,
                "vectors_config": vectors_config,
            }
            if sparse_vectors_config is not None:
                kwargs["sparse_vectors_config"] = sparse_vectors_config
            await client.create_collection(**kwargs)
            return
        except Exception as exc:
            last_exc = exc
            if await _collection_exists_safe(client, collection_name):
                logger.warning(
                    "Qdrant create_collection failed/timed out but %s now exists",
                    collection_name,
                )
                return
            if attempt >= attempts:
                break
            logger.warning(
                "Qdrant create_collection failed for %s (attempt %d/%d): %s",
                collection_name,
                attempt,
                attempts,
                exc,
            )
            await asyncio.sleep(0.75 * attempt)

    assert last_exc is not None
    raise last_exc


async def _create_payload_index_with_retry(
    client: AsyncQdrantClient,
    *,
    collection_name: str,
    field_name: str,
    attempts: int = 3,
) -> None:
    for attempt in range(1, attempts + 1):
        try:
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
            )
            return
        except Exception as exc:
            message = str(exc).lower()
            if "already exists" in message:
                logger.debug(
                    "Qdrant payload index already exists for %s.%s",
                    collection_name,
                    field_name,
                )
                return
            if attempt >= attempts:
                raise
            logger.warning(
                "Qdrant create_payload_index failed for %s.%s (attempt %d/%d): %s",
                collection_name,
                field_name,
                attempt,
                attempts,
                exc,
            )
            await asyncio.sleep(0.5 * attempt)


async def _list_aliases_for_collection(
    client: AsyncQdrantClient, collection_name: str
) -> list[str]:
    """Return the alias names pointing at a given physical collection."""
    try:
        resp = await client.get_collection_aliases(collection_name=collection_name)
        return [a.alias_name for a in resp.aliases]
    except Exception as exc:
        logger.debug("Alias lookup failed for %s: %s", collection_name, exc)
        return []


async def rename_corpus_aliases(
    client: AsyncQdrantClient, corpus_id: str, new_name: str
) -> None:
    """Re-point human-readable aliases to match the new corpus name. Called by
    `IngestionService.update_corpus` on rename. Never raises — aliases are a UX
    affordance, not load-bearing.
    """
    ops: list[CreateAliasOperation | DeleteAliasOperation] = []
    for kind in _VALID_KINDS:
        physical = _col_for_corpus(corpus_id, kind)
        new_alias = _alias_for_corpus(corpus_id, new_name, kind)
        existing_aliases = await _list_aliases_for_collection(client, physical)
        for a in existing_aliases:
            if a != new_alias:
                ops.append(DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=a)))
        if new_alias not in existing_aliases:
            ops.append(CreateAliasOperation(create_alias=CreateAlias(
                collection_name=physical, alias_name=new_alias,
            )))
    if not ops:
        return
    try:
        await client.update_collection_aliases(change_aliases_operations=ops)
        logger.info("Updated Qdrant aliases for corpus %s → %r", corpus_id, new_name)
    except Exception as exc:
        logger.warning("Alias update failed for corpus %s: %s", corpus_id, exc)


async def ensure_collections_for_corpus(
    client: AsyncQdrantClient,
    corpus_id: str,
    dim: int = 1024,
    *,
    corpus_name: str | None = None,
) -> None:
    """Create the 4 per-corpus collections (naive/hrag/graph/schemas) if they
    do not exist. Idempotent.

    Called by `IngestionService.create_corpus` and by the migration script.
    When `corpus_name` is provided, also creates human-readable Qdrant aliases
    (`corpus_{slug}_{cid8}_{kind}` → physical collection) for dashboard use.
    """
    chunk_kinds = ("naive", "hrag", "graph")
    for kind in chunk_kinds:
        name = _col_for_corpus(corpus_id, kind)
        if await client.collection_exists(name):
            logger.debug("Qdrant collection exists: %s", name)
            continue

        # Hybrid layout: named "dense" for the Qwen3 embedding + named
        # "sparse" with server-side IDF for BM25. New corpora always get
        # both; existing corpora (created before this change) keep their
        # legacy unnamed-dense layout and the lexical retriever falls
        # back to Mongo $text for those — see retriever/lexical.py.
        await _create_collection_with_retry(
            client,
            collection_name=name,
            vectors_config={"dense": VectorParams(size=dim, distance=Distance.COSINE)},
            sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
        )
        for field_name in _CHUNK_PAYLOAD_INDEXES:
            await _create_payload_index_with_retry(
                client,
                collection_name=name,
                field_name=field_name,
            )
        logger.info("Created Qdrant collection: %s (corpus %s) [hybrid]", name, corpus_id)

    schemas_name = _col_for_corpus(corpus_id, "schemas")
    if not await client.collection_exists(schemas_name):
        await _create_collection_with_retry(
            client,
            collection_name=schemas_name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        for field_name in _SCHEMA_PAYLOAD_INDEXES:
            await _create_payload_index_with_retry(
                client,
                collection_name=schemas_name,
                field_name=field_name,
            )
        logger.info("Created Qdrant collection: %s (corpus %s)", schemas_name, corpus_id)

    if corpus_name:
        await rename_corpus_aliases(client, corpus_id, corpus_name)


async def drop_collections_for_corpus(
    client: AsyncQdrantClient, corpus_id: str
) -> int:
    """Drop the 4 per-corpus collections. O(1) per-collection — replaces the
    old filter-delete cascade. Idempotent: missing collections are silently
    skipped. Returns the count of collections actually dropped. Qdrant aliases
    bound to a dropped collection are removed automatically by the server.
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


# Per-collection layout cache: (has_named_dense, has_sparse). Populated
# lazily by `_collection_layout`. Lets us support legacy (unnamed dense)
# and new (named dense+sparse) collections in the same upsert path
# without per-call introspection.
_COLLECTION_LAYOUT_CACHE: dict[str, tuple[bool, bool]] = {}


async def _collection_layout(
    client: AsyncQdrantClient, collection_name: str
) -> tuple[bool, bool]:
    """Return (has_named_dense, has_sparse) for a collection. Cached."""
    cached = _COLLECTION_LAYOUT_CACHE.get(collection_name)
    if cached is not None:
        return cached
    info = await client.get_collection(collection_name)
    params = info.config.params
    vec_cfg = getattr(params, "vectors", None)
    has_named_dense = isinstance(vec_cfg, dict) and "dense" in vec_cfg
    sparse_cfg = getattr(params, "sparse_vectors", None) or {}
    has_sparse = bool(sparse_cfg) and "sparse" in sparse_cfg
    layout = (has_named_dense, has_sparse)
    _COLLECTION_LAYOUT_CACHE[collection_name] = layout
    return layout


def _build_vector(
    *,
    dense: list[float],
    sparse: SparseVector | None,
    has_named_dense: bool,
    has_sparse: bool,
):
    """Shape the per-point `vector` field to match the collection layout.

    * Legacy (unnamed dense): return the raw list[float].
    * New (named dense + sparse): return {"dense": [...], "sparse": SV(...)}.
      Drop the sparse entry if its indices are empty (Qdrant rejects
      empty SparseVectors on upsert).
    """
    if not has_named_dense:
        return dense
    out: dict = {"dense": dense}
    if has_sparse and sparse is not None and getattr(sparse, "indices", None):
        out["sparse"] = sparse
    return out


async def upsert_children(
    client: AsyncQdrantClient,
    corpus_id: str,
    chunks: list[dict],
    vectors: list[list[float]],
    target_kinds: list[str],
    sparse_vectors: list[SparseVector] | None = None,
) -> None:
    """
    Upsert child vectors into the per-corpus collections for `corpus_id`.

    Args:
        client: AsyncQdrantClient.
        corpus_id: owning corpus — resolves to per-corpus collection names.
        chunks: dicts with chunk_id, parent_id, doc_id, corpus_id,
                source_tier, heading_path, text, user_id.
        vectors: 1024-d dense embeddings, same order as chunks.
        target_kinds: subset of ["naive", "hrag", "graph"] — kind selectors,
            NOT collection names. Each is resolved via `_col_for_corpus`.
        sparse_vectors: optional BM25 sparse vectors (same order as chunks).
            Written into the "sparse" named slot for collections that have
            it (new corpora). Silently ignored for legacy unnamed-dense
            collections — the lexical retriever falls back to Mongo $text
            for those.
    """
    if not chunks or not vectors:
        return
    assert len(chunks) == len(vectors), "chunks and vectors length mismatch"
    if sparse_vectors is not None:
        assert len(sparse_vectors) == len(chunks), "sparse_vectors length mismatch"
    sv_iter = sparse_vectors or [None] * len(chunks)

    payloads = [
        {
            "corpus_id": c["corpus_id"],
            "doc_id": c["doc_id"],
            "chunk_id": c["chunk_id"],
            "parent_id": c["parent_id"],
            "chunk_type": "child",
            "source_tier": c["source_tier"],
            "heading_path": c.get("heading_path"),
            "chunk_text": c["text"],
            "user_id": c.get("user_id", ""),
            # Semantic role (body / toc / bibliography / … / code). Default
            # retrieval excludes non-body via a `must_not` filter on this
            # field; missing field treated as body for backwards compat.
            "chunk_kind": c.get("chunk_kind", "body"),
            # Code lane (Phase 1) — language tag + AST-derived metadata
            # (symbols_defined / symbols_called / imports / ast_signature /
            # file_path). Empty for prose chunks. Used at retrieval time for
            # /python, /rust, … skill scoping and code-aware reranking.
            "language": c.get("language"),
            "metadata": c.get("metadata") or {},
        }
        for c in chunks
    ]

    for kind in target_kinds:
        name = _col_for_corpus(corpus_id, kind)
        await _assert_collection_owner(client, name, corpus_id)
        has_named, has_sparse = await _collection_layout(client, name)
        points = [
            PointStruct(
                id=_child_point_id(c["chunk_id"]),
                vector=_build_vector(
                    dense=v, sparse=sv,
                    has_named_dense=has_named, has_sparse=has_sparse,
                ),
                payload=payload,
            )
            for c, v, sv, payload in zip(chunks, vectors, sv_iter, payloads)
        ]
        await _upsert_points_batched(
            client,
            collection_name=name,
            points=points,
            point_label="child",
        )
        logger.debug(
            "Upserted %d child points → %s (named=%s sparse=%s)",
            len(points), name, has_named, has_sparse,
        )


async def upsert_summaries(
    client: AsyncQdrantClient,
    corpus_id: str,
    summary_payloads: list[dict],
    vectors: list[list[float]],
    target_kinds: list[str],
    sparse_vectors: list[SparseVector] | None = None,
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
        sparse_vectors: optional BM25 sparse vectors over the summary text.
            Same backwards-compat handling as `upsert_children`.
    """
    if not summary_payloads or not vectors:
        return
    assert len(summary_payloads) == len(vectors)
    if sparse_vectors is not None:
        assert len(sparse_vectors) == len(summary_payloads), "sparse length mismatch"
    sv_iter = sparse_vectors or [None] * len(summary_payloads)

    payloads = [
        {
            "corpus_id": p["corpus_id"],
            "doc_id": p["doc_id"],
            "chunk_id": f"{p['parent_id']}_summary",
            "parent_id": p["parent_id"],
            "chunk_type": "summary",
            "source_tier": p["source_tier"],
            "heading_path": p.get("heading_path"),
            "chunk_text": p["summary"],
            "user_id": p.get("user_id", ""),
            "chunk_kind": p.get("chunk_kind", "body"),
            "language": p.get("language"),
            "metadata": p.get("metadata") or {},
        }
        for p in summary_payloads
    ]

    for kind in target_kinds:
        if kind == "graph":
            continue  # summaries never seed the graph collection
        name = _col_for_corpus(corpus_id, kind)
        await _assert_collection_owner(client, name, corpus_id)
        has_named, has_sparse = await _collection_layout(client, name)
        points = [
            PointStruct(
                id=_summary_point_id(p["corpus_id"], p["parent_id"]),
                vector=_build_vector(
                    dense=v, sparse=sv,
                    has_named_dense=has_named, has_sparse=has_sparse,
                ),
                payload=payload,
            )
            for p, v, sv, payload in zip(summary_payloads, vectors, sv_iter, payloads)
        ]
        await _upsert_points_batched(
            client,
            collection_name=name,
            points=points,
            point_label="summary",
        )
        logger.debug(
            "Upserted %d summary points → %s (named=%s sparse=%s)",
            len(points), name, has_named, has_sparse,
        )


# `delete_points_by_corpus` was removed in Phase 7.5 — per-corpus collections
# are dropped atomically via `drop_collections_for_corpus()`.  The legacy
# filter-delete is no longer needed because no collection holds points from
# more than one corpus.


async def delete_points_by_doc(
    client: AsyncQdrantClient,
    corpus_id: str,
    doc_id: str,
) -> dict[str, bool]:
    """Delete all points for a single document across naive / hrag / graph.

    Per-doc delete (Phase 22). Corpus-level drops go through
    `drop_collections_for_corpus`; this helper is for single-document cascade.
    Filters on `doc_id` within each per-corpus collection so summary points
    (which also carry `doc_id` in their payload — see `upsert_summary_points`)
    are removed alongside child chunks.

    Schemas collection is NOT touched — schema terms are corpus-scoped, not
    doc-scoped.
    """
    results: dict[str, bool] = {}
    for kind in ("naive", "hrag", "graph"):
        name = _col_for_corpus(corpus_id, kind)
        try:
            if not await client.collection_exists(name):
                results[kind] = False
                continue
            op = await client.delete(
                collection_name=name,
                points_selector=Filter(
                    must=[
                        FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
                        FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    ]
                ),
            )
            results[kind] = getattr(op, "operation_id", None) is not None
        except Exception as exc:
            logger.warning(
                "Qdrant per-doc delete failed for %s (doc=%s): %s", name, doc_id[:12], exc
            )
            results[kind] = False
    logger.info(
        "Qdrant: deleted points for doc %s in corpus %s → %s",
        doc_id[:12],
        corpus_id[:8],
        results,
    )
    return results


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
    await _upsert_points_batched(
        client,
        collection_name=name,
        points=points,
        point_label=f"schema:{kind}",
    )
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
