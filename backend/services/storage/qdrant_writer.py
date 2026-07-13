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
    MatchAny,
    MatchValue,
    Modifier,
    PayloadSchemaType,
    PointIdsList,
    PointStruct,
    QueryRequest,
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


def payload_text_contract(text: str | None) -> dict:
    """Integrity metadata proving Qdrant carries full text, not a preview."""
    value = text or ""
    return {
        "text_len": len(value),
        "text_hash": hashlib.sha1(value.encode("utf-8")).hexdigest(),
        "is_truncated": False,
    }


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
        raise ValueError(
            f"Invalid Qdrant kind {kind!r} (expected one of {_VALID_KINDS})"
        )
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
    "schema_version",
    "summary_type",
    "source_child_ids",
    # M1 (2026-07-02): domain denormalized from Ghost-A parents, indexed so a
    # future query-domain pre-filter (must=domain) is O(log n). Keyword index.
    "domain",
    # Q2/U2 (2026-07-04): the funnel-B soft prefilter should-matches these
    # promoted fields. WITHOUT the index Qdrant full-scans payloads - a
    # filtered query on an unpromoted 561k-chunk corpus measured 21.6s; with
    # the index an empty match returns in ms and the deterministic fallback
    # fires cheaply. Promote also creates them, but readiness must guarantee
    # them on EVERY corpus, promoted or not.
    "concepts",
    "entity_ids",
)
_SCHEMA_PAYLOAD_INDEXES: tuple[str, ...] = (
    "corpus_id",
    "kind",
    "term",
    "doc_id",
    "node_id",
    "node_type",
    "lexicon_id",
    "lexicon_ids",
    "canonical_key",
    "member_keys",
    "aliases_normalized",
    "abbreviations_normalized",
)

# Startup readiness verifies every corpus collection before serving traffic.
# Remember that result so high-fanout query stages do not issue one
# ``collection_exists`` HTTP request per lane, document, and hierarchy level.
_COLLECTION_EXISTENCE_CACHE: set[str] = set()


async def _collection_exists_safe(
    client: AsyncQdrantClient,
    collection_name: str,
) -> bool:
    try:
        return bool(await client.collection_exists(collection_name))
    except Exception as exc:
        logger.debug("Qdrant collection_exists failed for %s: %s", collection_name, exc)
        return False


async def _collection_available(
    client: AsyncQdrantClient,
    collection_name: str,
) -> bool:
    """Return collection availability with positive-result caching."""

    if collection_name in _COLLECTION_EXISTENCE_CACHE:
        return True
    exists = await _collection_exists_safe(client, collection_name)
    if exists:
        _COLLECTION_EXISTENCE_CACHE.add(collection_name)
    return exists


async def _search_points_compat(
    client: AsyncQdrantClient,
    *,
    collection_name: str,
    query_vector: list[float],
    query_filter: Filter,
    limit: int,
    with_payload=True,
    with_vectors: bool = False,
    score_threshold: float | None = None,
):
    """Support qdrant-client before and after ``search`` was removed in 1.18."""

    search = getattr(client, "search", None)
    if callable(search):
        kwargs = {
            "collection_name": collection_name,
            "query_vector": query_vector,
            "query_filter": query_filter,
            "limit": limit,
            "with_payload": with_payload,
            "with_vectors": with_vectors,
        }
        if score_threshold is not None:
            kwargs["score_threshold"] = score_threshold
        return await search(**kwargs)
    response = await client.query_points(
        collection_name=collection_name,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=with_payload,
        with_vectors=with_vectors,
        score_threshold=score_threshold,
    )
    return list(getattr(response, "points", None) or [])


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


def _dense_vector_size(vectors_config) -> int | None:
    """Return the dense vector size from either legacy or named-vector config."""
    dense_config = vectors_config
    if isinstance(vectors_config, dict):
        dense_config = vectors_config.get("dense")
        if dense_config is None and vectors_config:
            dense_config = next(iter(vectors_config.values()))
    size = getattr(dense_config, "size", None)
    try:
        return int(size) if size is not None else None
    except (TypeError, ValueError):
        return None


def _layout_from_collection_info(collection_info: object) -> tuple[bool, bool]:
    params = getattr(getattr(collection_info, "config", None), "params", None)
    vec_cfg = getattr(params, "vectors", None)
    has_named_dense = isinstance(vec_cfg, dict) and "dense" in vec_cfg
    sparse_cfg = getattr(params, "sparse_vectors", None) or {}
    has_sparse = bool(sparse_cfg) and "sparse" in sparse_cfg
    return has_named_dense, has_sparse


async def _assert_collection_dimension(
    client: AsyncQdrantClient,
    *,
    collection_name: str,
    expected_dim: int,
) -> object | None:
    """Fail fast when an existing collection cannot serve this corpus.

    Re-indexing is required after an embedding-dimension change. Silently
    accepting a stale Qdrant collection makes ingestion appear successful while
    query-time vector search returns errors or no hits.
    """
    try:
        info = await client.get_collection(collection_name)
        vectors_config = getattr(getattr(info.config, "params", None), "vectors", None)
        actual_dim = _dense_vector_size(vectors_config)
        # Readiness already paid for this metadata request. Prime the shared
        # read-path cache so the first interactive query does not launch one
        # get_collection request per concurrent lane/corpus.
        _COLLECTION_LAYOUT_CACHE[collection_name] = _layout_from_collection_info(info)
        _COLLECTION_EXISTENCE_CACHE.add(collection_name)
    except Exception as exc:
        logger.warning(
            "Could not inspect Qdrant collection %s: %s", collection_name, exc
        )
        return None
    if actual_dim is None:
        logger.warning(
            "Could not determine Qdrant vector dimension for %s", collection_name
        )
        return info
    if actual_dim != int(expected_dim):
        raise RuntimeError(
            f"Qdrant collection {collection_name!r} has vector dimension "
            f"{actual_dim}, expected {expected_dim}. Re-index this corpus before "
            "retrieval can be deterministic."
        )
    return info


def _payload_index_fields(collection_info: object | None) -> set[str]:
    """Return fields already indexed according to one collection snapshot."""
    payload_schema = getattr(collection_info, "payload_schema", None) or {}
    if isinstance(payload_schema, dict):
        return {str(field) for field in payload_schema}
    return set()


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
            ops.append(
                CreateAliasOperation(
                    create_alias=CreateAlias(
                        collection_name=physical,
                        alias_name=new_alias,
                    )
                )
            )
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
        existing_payload_indexes: set[str] = set()
        if await client.collection_exists(name):
            info = await _assert_collection_dimension(
                client,
                collection_name=name,
                expected_dim=dim,
            )
            existing_payload_indexes = _payload_index_fields(info)
            logger.debug("Qdrant collection exists: %s", name)
        else:
            # Hybrid layout: named "dense" for the Qwen3 embedding + named
            # "sparse" with server-side IDF for BM25. New corpora always get
            # both; existing corpora (created before this change) keep their
            # legacy unnamed-dense layout and the lexical retriever falls
            # back to Mongo $text for those — see retriever/lexical.py.
            await _create_collection_with_retry(
                client,
                collection_name=name,
                vectors_config={
                    "dense": VectorParams(size=dim, distance=Distance.COSINE)
                },
                sparse_vectors_config={
                    "sparse": SparseVectorParams(modifier=Modifier.IDF)
                },
            )
        for field_name in _CHUNK_PAYLOAD_INDEXES:
            if field_name in existing_payload_indexes:
                continue
            await _create_payload_index_with_retry(
                client,
                collection_name=name,
                field_name=field_name,
            )
        _COLLECTION_EXISTENCE_CACHE.add(name)
        logger.info(
            "Ensured Qdrant collection: %s (corpus %s) [hybrid]", name, corpus_id
        )

    schemas_name = _col_for_corpus(corpus_id, "schemas")
    existing_schema_indexes: set[str] = set()
    if await client.collection_exists(schemas_name):
        info = await _assert_collection_dimension(
            client,
            collection_name=schemas_name,
            expected_dim=dim,
        )
        existing_schema_indexes = _payload_index_fields(info)
    else:
        await _create_collection_with_retry(
            client,
            collection_name=schemas_name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        logger.info(
            "Created Qdrant collection: %s (corpus %s)", schemas_name, corpus_id
        )
    for field_name in _SCHEMA_PAYLOAD_INDEXES:
        if field_name in existing_schema_indexes:
            continue
        await _create_payload_index_with_retry(
            client,
            collection_name=schemas_name,
            field_name=field_name,
        )
    _COLLECTION_EXISTENCE_CACHE.add(schemas_name)
    logger.info("Ensured Qdrant collection: %s (corpus %s)", schemas_name, corpus_id)

    if corpus_name:
        await rename_corpus_aliases(client, corpus_id, corpus_name)


async def drop_collections_for_corpus(client: AsyncQdrantClient, corpus_id: str) -> int:
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
                _COLLECTION_EXISTENCE_CACHE.discard(name)
                _COLLECTION_LAYOUT_CACHE.pop(name, None)
                logger.info(
                    "Dropped Qdrant collection: %s (corpus %s)", name, corpus_id
                )
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
_COLLECTION_LAYOUT_LOCKS: dict[str, asyncio.Lock] = {}


async def _collection_layout(
    client: AsyncQdrantClient, collection_name: str
) -> tuple[bool, bool]:
    """Return (has_named_dense, has_sparse) for a collection. Cached."""
    cached = _COLLECTION_LAYOUT_CACHE.get(collection_name)
    if cached is not None:
        return cached
    lock = _COLLECTION_LAYOUT_LOCKS.setdefault(collection_name, asyncio.Lock())
    async with lock:
        cached = _COLLECTION_LAYOUT_CACHE.get(collection_name)
        if cached is not None:
            return cached
        info = await client.get_collection(collection_name)
        layout = _layout_from_collection_info(info)
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
            "filename": c.get("filename") or c.get("doc_name") or "",
            "doc_name": c.get("doc_name") or c.get("filename") or "",
            "chunk_id": c["chunk_id"],
            "parent_id": c["parent_id"],
            "chunk_type": "child",
            "source_tier": c["source_tier"],
            "heading_path": c.get("heading_path"),
            "chunk_text": c["text"],
            **payload_text_contract(c.get("text")),
            "user_id": c.get("user_id", ""),
            # Semantic role (body / toc / bibliography / … / code). Default
            # retrieval excludes non-body via a `must_not` filter on this
            # field; missing field treated as body for backwards compat.
            "chunk_kind": c.get("chunk_kind", "body"),
            # M1: domain denormalized from Ghost-A parent for pre-retrieval
            # filtering (real taxonomy label or None; backfilled + forward).
            "domain": c.get("domain"),
            # Code lane (Phase 1) — language tag + AST-derived metadata
            # (symbols_defined / symbols_called / imports / ast_signature /
            # file_path). Empty for prose chunks. Used at retrieval time for
            # /python, /rust, … skill scoping and code-aware reranking.
            "language": c.get("language"),
            "metadata": c.get("metadata") or {},
            "facet_ids": c.get("facet_ids") or [],
            "facet_text": c.get("facet_text") or "",
            "content_facet_ids": c.get("content_facet_ids") or [],
            "content_facet_text": c.get("content_facet_text") or "",
            "content_facet_source": c.get("content_facet_source") or "",
            "content_facet_confidence": c.get("content_facet_confidence"),
            "doc_facet_ids": c.get("doc_facet_ids") or [],
            "facet_schema_version": c.get("facet_schema_version") or "",
            # Graph-promotion fields are filled after extraction by promote_doc().
            # Keep empty defaults on first write so every active point has the
            # same retrieval contract shape.
            "concepts": c.get("concepts") or [],
            "entity_ids": c.get("entity_ids") or [],
            "relation_predicates": c.get("relation_predicates") or [],
            "relation_families": c.get("relation_families") or [],
            "fact_types": c.get("fact_types") or [],
            "related_entities": c.get("related_entities") or [],
            "has_relations": bool(c.get("has_relations", False)),
            "extract_schema_version": c.get("extract_schema_version") or "",
            "promote_version": c.get("promote_version") or "",
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
                    dense=v,
                    sparse=sv,
                    has_named_dense=has_named,
                    has_sparse=has_sparse,
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
            len(points),
            name,
            has_named,
            has_sparse,
        )


async def upsert_summaries(
    client: AsyncQdrantClient,
    corpus_id: str,
    summary_payloads: list[dict],
    vectors: list[list[float]],
    target_kinds: list[str],
    sparse_vectors: list[SparseVector] | None = None,
) -> int:
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
        return 0
    assert len(summary_payloads) == len(vectors)
    if sparse_vectors is not None:
        assert len(sparse_vectors) == len(summary_payloads), "sparse length mismatch"
    sv_iter = sparse_vectors or [None] * len(summary_payloads)

    # A summary without a producing model is not an abstraction artifact. The
    # historical path copied raw parent text into the summary lane and stamped
    # ``summary_model=""``, doubling retrieval candidates while adding no new
    # information. Refuse new placeholders at the storage boundary; callers
    # that intentionally import a legacy summary must provide an explicit
    # provenance value such as ``legacy_unknown``.
    prepared = [
        (payload, vector, sparse)
        for payload, vector, sparse in zip(
            summary_payloads,
            vectors,
            sv_iter,
            strict=True,
        )
        if str(payload.get("summary_model") or "").strip()
    ]
    skipped = len(summary_payloads) - len(prepared)
    if skipped:
        logger.warning(
            "Skipped %d unmodeled summary placeholder(s) for corpus %s",
            skipped,
            corpus_id,
        )
    if not prepared:
        return 0
    summary_payloads = [row[0] for row in prepared]
    vectors = [row[1] for row in prepared]
    sv_iter = [row[2] for row in prepared]

    payloads = []
    for p in summary_payloads:
        summary_text = p.get("summary") or p.get("summary_text") or ""
        retrieval_text = p.get("retrieval_text") or summary_text
        payloads.append(
            {
                "corpus_id": p["corpus_id"],
                "doc_id": p["doc_id"],
                "filename": p.get("filename") or p.get("doc_name") or "",
                "doc_name": p.get("doc_name") or p.get("filename") or "",
                "chunk_id": f"{p['parent_id']}_summary",
                "parent_id": p["parent_id"],
                "chunk_type": "summary",
                "summary_id": p.get("summary_id") or f"{p['parent_id']}_summary",
                "schema_version": p.get("schema_version") or "parent_summary.v1",
                "summary_type": p.get("summary_type") or "parent_retrieval_replacement",
                "summary_text": summary_text,
                "retrieval_text": retrieval_text,
                "central_claim": p.get("central_claim") or "",
                "key_points": p.get("key_points") or [],
                "main_mechanism": p.get("main_mechanism") or "",
                "concept_tags": p.get("concept_tags") or [],
                "entity_hints": p.get("entity_hints") or [],
                "retrieval_uses": p.get("retrieval_uses") or [],
                "abstraction_level": p.get("abstraction_level") or "medium",
                "latent_concepts": p.get("latent_concepts") or [],
                "temporal_class": p.get("temporal_class") or "unknown",
                "time_expressions": p.get("time_expressions") or [],
                "source_child_ids": p.get("source_child_ids")
                or p.get("child_ids")
                or [],
                "source_hash": p.get("source_hash") or "",
                "summary_model": p.get("summary_model") or "",
                "summary_created_at": p.get("summary_created_at") or "",
                "validation_status": p.get("validation_status") or "",
                "repair_status": p.get("repair_status") or "",
                "quality_score": p.get("quality_score"),
                "quality_flags": p.get("quality_flags") or [],
                "source_tier": p["source_tier"],
                "heading_path": p.get("heading_path"),
                "chunk_text": retrieval_text,
                **payload_text_contract(retrieval_text),
                "user_id": p.get("user_id", ""),
                "chunk_kind": p.get("chunk_kind", "body"),
                "language": p.get("language"),
                "metadata": p.get("metadata") or {},
                "facet_ids": p.get("facet_ids") or [],
                "facet_text": p.get("facet_text") or "",
                "content_facet_ids": p.get("content_facet_ids") or [],
                "content_facet_text": p.get("content_facet_text") or "",
                "content_facet_source": p.get("content_facet_source") or "",
                "content_facet_confidence": p.get("content_facet_confidence"),
                "doc_facet_ids": p.get("doc_facet_ids") or [],
                "facet_schema_version": p.get("facet_schema_version") or "",
            }
        )

    wrote_to_collection = False
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
                    dense=v,
                    sparse=sv,
                    has_named_dense=has_named,
                    has_sparse=has_sparse,
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
        wrote_to_collection = True
        logger.debug(
            "Upserted %d summary points → %s (named=%s sparse=%s)",
            len(points),
            name,
            has_named,
            has_sparse,
        )
    return len(payloads) if wrote_to_collection else 0


# `delete_points_by_corpus` was removed in Phase 7.5 — per-corpus collections
# are dropped atomically via `drop_collections_for_corpus()`.  The legacy
# filter-delete is no longer needed because no collection holds points from
# more than one corpus.


async def delete_points_by_doc(
    client: AsyncQdrantClient,
    corpus_id: str,
    doc_id: str,
    *,
    preserve_summary_points: bool = False,
) -> dict[str, bool]:
    """Delete all points for a single document across naive / hrag / graph.

    Per-doc delete (Phase 22). Corpus-level drops go through
    `drop_collections_for_corpus`; this helper is for single-document cascade.
    Filters on `doc_id` within each per-corpus collection. By default summary
    points are removed alongside child chunks. Retry paths that intentionally
    defer Ghost A can set ``preserve_summary_points`` so replacing child
    vectors does not erase already-indexed durable summaries.

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
            selector = Filter(
                must=[
                    FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                ],
                must_not=(
                    [
                        FieldCondition(
                            key="chunk_type",
                            match=MatchValue(value="summary"),
                        )
                    ]
                    if preserve_summary_points
                    else None
                ),
            )
            op = await client.delete(
                collection_name=name,
                points_selector=selector,
            )
            results[kind] = getattr(op, "operation_id", None) is not None
        except Exception as exc:
            logger.warning(
                "Qdrant per-doc delete failed for %s (doc=%s): %s",
                name,
                doc_id[:12],
                exc,
            )
            results[kind] = False
    logger.info(
        "Qdrant: deleted points for doc %s in corpus %s preserve_summaries=%s → %s",
        doc_id[:12],
        corpus_id[:8],
        preserve_summary_points,
        results,
    )
    return results


async def delete_summary_points_by_doc(
    client: AsyncQdrantClient,
    corpus_id: str,
    doc_id: str,
) -> dict[str, bool]:
    """Delete only parent-summary points while preserving child vectors."""

    results: dict[str, bool] = {}
    for kind in ("naive", "hrag"):
        name = _col_for_corpus(corpus_id, kind)
        try:
            if not await client.collection_exists(name):
                results[kind] = False
                continue
            selector = Filter(
                must=[
                    FieldCondition(
                        key="corpus_id", match=MatchValue(value=corpus_id)
                    ),
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    FieldCondition(
                        key="chunk_type", match=MatchValue(value="summary")
                    ),
                ]
            )
            op = await client.delete(
                collection_name=name,
                points_selector=selector,
                wait=True,
            )
            results[kind] = getattr(op, "operation_id", None) is not None
        except Exception as exc:
            logger.warning(
                "Qdrant summary-only delete failed for %s (doc=%s): %s",
                name,
                doc_id[:12],
                exc,
            )
            results[kind] = False
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


# ── Corpus vocabulary bridge ──────────────────────────────────────────────

LEXICON_SCHEMA_KIND = "entity_lexicon"
SUMMARY_TREE_SCHEMA_KIND = "summary_tree"


def _summary_tree_payload(entry: dict) -> dict:
    """Bounded payload for a pre-embedded RAPTOR section or rollup node."""

    summary = str(entry.get("summary") or "").strip()
    return {
        "corpus_id": str(entry.get("corpus_id") or ""),
        "kind": SUMMARY_TREE_SCHEMA_KIND,
        "term": str(entry.get("section_range") or entry.get("node_id") or ""),
        "node_id": str(entry.get("node_id") or ""),
        "node_type": str(entry.get("node_type") or ""),
        "doc_id": str(entry.get("doc_id") or ""),
        "section_range": str(entry.get("section_range") or "")[:500],
        "summary": summary[:2400],
        "parent_ids": [str(value) for value in (entry.get("parent_ids") or [])[:64]],
        "child_node_ids": [
            str(value) for value in (entry.get("child_node_ids") or [])[:64]
        ],
        "passthrough_rollup_id": str(entry.get("passthrough_rollup_id") or ""),
        "passthrough_parent_ids": [
            str(value)
            for value in (entry.get("passthrough_parent_ids") or [])[:64]
        ],
        "passthrough_lexicon_ids": [
            str(value)
            for value in (entry.get("passthrough_lexicon_ids") or [])[:96]
        ],
        "lexicon_ids": [str(value) for value in (entry.get("lexicon_ids") or [])[:96]],
        "token_estimate": max(1, len(summary.split())),
        "schema_version": str(entry.get("schema_version") or ""),
    }


async def upsert_summary_tree_entries(
    client: AsyncQdrantClient,
    corpus_id: str,
    entries: list[dict],
    vectors: list[list[float]],
    *,
    verify_owner: bool = True,
) -> int:
    """Index section/rollup routing nodes once so queries only embed the query."""

    if not entries or not vectors:
        return 0
    if len(entries) != len(vectors):
        raise ValueError("summary-tree entries and vectors length mismatch")
    name = _col_for_corpus(corpus_id, "schemas")
    if verify_owner:
        await _assert_collection_owner(client, name, corpus_id)
    points: list[PointStruct] = []
    for entry, vector in zip(entries, vectors):
        node_id = str(entry.get("node_id") or "")
        node_type = str(entry.get("node_type") or "")
        if not node_id or node_type not in {"section", "rollup"}:
            continue
        payload = _summary_tree_payload({**entry, "corpus_id": corpus_id})
        points.append(
            PointStruct(
                id=_schema_point_id(
                    corpus_id,
                    SUMMARY_TREE_SCHEMA_KIND,
                    node_id,
                ),
                vector=vector,
                payload=payload,
            )
        )
    await _upsert_points_batched(
        client,
        collection_name=name,
        points=points,
        point_label="schema:summary_tree",
    )
    return len(points)


async def search_summary_tree_entries(
    client: AsyncQdrantClient,
    corpus_id: str,
    *,
    query_vec: list[float],
    doc_id: str,
    node_type: str,
    node_ids: list[str] | None = None,
    top_k: int = 8,
    score_threshold: float | None = 0.2,
) -> list[dict]:
    """Search one routed document's pre-embedded hierarchy nodes."""

    if not query_vec or node_type not in {"section", "rollup"}:
        return []
    name = _col_for_corpus(corpus_id, "schemas")
    if not await _collection_available(client, name):
        return []
    must = [
        FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
        FieldCondition(
            key="kind",
            match=MatchValue(value=SUMMARY_TREE_SCHEMA_KIND),
        ),
        FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
        FieldCondition(key="node_type", match=MatchValue(value=node_type)),
    ]
    scoped_ids = [str(value) for value in (node_ids or []) if str(value)]
    if scoped_ids:
        must.append(FieldCondition(key="node_id", match=MatchAny(any=scoped_ids[:256])))
    hits = await _search_points_compat(
        client,
        collection_name=name,
        query_vector=query_vec,
        query_filter=Filter(must=must),
        limit=max(1, min(int(top_k), 32)),
        with_payload=True,
        with_vectors=False,
        score_threshold=score_threshold,
    )
    output: list[dict] = []
    for hit in hits:
        payload = dict(getattr(hit, "payload", None) or {})
        if not payload.get("node_id"):
            continue
        payload["score"] = float(getattr(hit, "score", 0.0) or 0.0)
        output.append(payload)
    return output


async def search_summary_tree_entries_batch(
    client: AsyncQdrantClient,
    corpus_id: str,
    *,
    queries: list[dict],
) -> list[list[dict]]:
    """Batch hierarchy searches for one corpus while preserving input order."""

    if not queries:
        return []
    name = _col_for_corpus(corpus_id, "schemas")
    if not await _collection_available(client, name):
        return [[] for _query in queries]

    requests: list[QueryRequest] = []
    request_indexes: list[int] = []
    output: list[list[dict]] = [[] for _query in queries]
    for index, spec in enumerate(queries):
        query_vec = list(spec.get("query_vec") or [])
        node_type = str(spec.get("node_type") or "")
        doc_id = str(spec.get("doc_id") or "")
        if not query_vec or node_type not in {"section", "rollup"} or not doc_id:
            continue
        must = [
            FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
            FieldCondition(
                key="kind",
                match=MatchValue(value=SUMMARY_TREE_SCHEMA_KIND),
            ),
            FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
            FieldCondition(key="node_type", match=MatchValue(value=node_type)),
        ]
        scoped_ids = [
            str(value) for value in (spec.get("node_ids") or []) if str(value)
        ]
        if scoped_ids:
            must.append(
                FieldCondition(
                    key="node_id",
                    match=MatchAny(any=scoped_ids[:256]),
                )
            )
        requests.append(
            QueryRequest(
                query=query_vec,
                filter=Filter(must=must),
                limit=max(1, min(int(spec.get("top_k") or 8), 32)),
                with_payload=True,
                with_vector=False,
                score_threshold=spec.get("score_threshold", 0.2),
            )
        )
        request_indexes.append(index)

    if not requests:
        return output
    responses = []
    for start in range(0, len(requests), 64):
        responses.extend(
            await client.query_batch_points(
                collection_name=name,
                requests=requests[start : start + 64],
            )
        )
    for output_index, response in zip(request_indexes, responses, strict=True):
        rows: list[dict] = []
        for hit in list(getattr(response, "points", None) or []):
            payload = dict(getattr(hit, "payload", None) or {})
            if not payload.get("node_id"):
                continue
            payload["score"] = float(getattr(hit, "score", 0.0) or 0.0)
            rows.append(payload)
        output[output_index] = rows
    return output


async def delete_summary_tree_entries(
    client: AsyncQdrantClient,
    corpus_id: str,
    node_ids: list[str] | None = None,
    *,
    doc_id: str | None = None,
) -> bool:
    """Delete selected or all hierarchy vectors for one corpus."""

    name = _col_for_corpus(corpus_id, "schemas")
    if not await client.collection_exists(name):
        return False
    if node_ids:
        point_ids = [
            _schema_point_id(corpus_id, SUMMARY_TREE_SCHEMA_KIND, str(node_id))
            for node_id in dict.fromkeys(node_ids)
            if str(node_id)
        ]
        for start in range(0, len(point_ids), 2048):
            await client.delete(
                collection_name=name,
                points_selector=PointIdsList(points=point_ids[start : start + 2048]),
                wait=True,
            )
        return True
    must = [
        FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
        FieldCondition(
            key="kind",
            match=MatchValue(value=SUMMARY_TREE_SCHEMA_KIND),
        ),
    ]
    if doc_id:
        must.append(FieldCondition(key="doc_id", match=MatchValue(value=doc_id)))
    await client.delete(
        collection_name=name,
        points_selector=Filter(must=must),
        wait=True,
    )
    return True


def _lexicon_payload(entry: dict) -> dict:
    """Bounded retrieval payload for a materialized Mongo lexicon entry."""

    return {
        "corpus_id": str(entry.get("corpus_id") or ""),
        "kind": LEXICON_SCHEMA_KIND,
        "term": str(entry.get("canonical_name") or ""),
        "lexicon_id": str(entry.get("lexicon_id") or ""),
        "canonical_key": str(entry.get("canonical_key") or ""),
        "member_keys": list(entry.get("member_keys") or [])[:32],
        "aliases": list(entry.get("aliases") or [])[:32],
        "aliases_normalized": list(entry.get("aliases_normalized") or [])[:32],
        "abbreviations": list(entry.get("abbreviations") or [])[:16],
        "abbreviations_normalized": list(entry.get("abbreviations_normalized") or [])[
            :16
        ],
        # Mongo retains the complete bounded provenance projection. Qdrant
        # carries only what query-time diagnostics and hierarchy descent use;
        # oversized payloads otherwise dominate vector-backfill write cost.
        "alias_evidence": list(entry.get("alias_evidence") or [])[:12],
        "retrieval_gloss": str(entry.get("retrieval_gloss") or "")[:1800],
        "embedding_gloss": str(entry.get("embedding_gloss") or "")[:900],
        "utility_gloss": str(entry.get("utility_gloss") or "")[:900],
        "definitions": list(entry.get("definitions") or [])[:6],
        "structural_contexts": list(entry.get("structural_contexts") or [])[:12],
        "contextual_usages": list(entry.get("contextual_usages") or [])[:12],
        "entity_ids": list(entry.get("entity_ids") or [])[:32],
        "entity_types": list(entry.get("entity_types") or [])[:12],
        "object_kinds": list(entry.get("object_kinds") or [])[:12],
        "source_document_ids": list(entry.get("source_document_ids") or [])[:32],
        "source_document_support": list(entry.get("source_document_support") or [])[
            :32
        ],
        "source_parent_ids": list(entry.get("source_parent_ids") or [])[:48],
        "source_chunk_ids": list(entry.get("source_chunk_ids") or [])[:48],
        "components": list(entry.get("components") or [])[:16],
        "component_of": list(entry.get("component_of") or [])[:16],
        "application_contexts": list(entry.get("application_contexts") or [])[:16],
        "factual_relations": list(entry.get("factual_relations") or [])[:24],
        "cooccurrence_neighbors": list(entry.get("cooccurrence_neighbors") or [])[:16],
        "semantic_neighbors": list(entry.get("semantic_neighbors") or [])[:16],
        "support_count": int(entry.get("support_count") or 0),
        "mean_confidence": float(entry.get("mean_confidence") or 0.0),
        "quality_flags": list(entry.get("quality_flags") or [])[:24],
        "retrieval_eligible": bool(entry.get("retrieval_eligible", True)),
        "schema_version": str(entry.get("schema_version") or ""),
        "lexicon_state": str(entry.get("lexicon_state") or "lexicon_ready"),
    }


async def upsert_lexicon_entries(
    client: AsyncQdrantClient,
    corpus_id: str,
    entries: list[dict],
    vectors: list[list[float]],
    *,
    verify_owner: bool = True,
) -> int:
    """Mirror Mongo lexicon entries into the isolated ``schemas`` collection."""

    if not entries or not vectors:
        return 0
    if len(entries) != len(vectors):
        raise ValueError("lexicon entries and vectors length mismatch")
    name = _col_for_corpus(corpus_id, "schemas")
    if verify_owner:
        await _assert_collection_owner(client, name, corpus_id)
    points: list[PointStruct] = []
    for entry, vector in zip(entries, vectors):
        lexicon_id = str(entry.get("lexicon_id") or "")
        if not lexicon_id:
            continue
        payload = _lexicon_payload({**entry, "corpus_id": corpus_id})
        points.append(
            PointStruct(
                id=_schema_point_id(corpus_id, LEXICON_SCHEMA_KIND, lexicon_id),
                vector=vector,
                payload=payload,
            )
        )
    await _upsert_points_batched(
        client,
        collection_name=name,
        points=points,
        point_label="schema:entity_lexicon",
    )
    return len(points)


async def retrieve_lexicon_entries(
    client: AsyncQdrantClient,
    corpus_id: str,
    lexicon_ids: list[str],
    *,
    with_vectors: bool = True,
    check_exists: bool = True,
) -> dict[str, dict]:
    """Fetch deterministic lexicon points for delta-aware projection repair."""

    ids = [str(value) for value in dict.fromkeys(lexicon_ids) if str(value)]
    if not ids:
        return {}
    name = _col_for_corpus(corpus_id, "schemas")
    if check_exists and not await _collection_available(client, name):
        return {}
    rows: dict[str, dict] = {}
    for start in range(0, len(ids), 512):
        point_ids = [
            _schema_point_id(corpus_id, LEXICON_SCHEMA_KIND, lexicon_id)
            for lexicon_id in ids[start : start + 512]
        ]
        points = await client.retrieve(
            collection_name=name,
            ids=point_ids,
            with_payload=True,
            with_vectors=with_vectors,
        )
        for point in points:
            payload = dict(getattr(point, "payload", None) or {})
            lexicon_id = str(payload.get("lexicon_id") or "")
            if not lexicon_id:
                continue
            rows[lexicon_id] = {
                "payload": payload,
                "vector": getattr(point, "vector", None) if with_vectors else None,
            }
    return rows


async def delete_lexicon_entries(
    client: AsyncQdrantClient,
    corpus_id: str,
    lexicon_ids: list[str] | None = None,
) -> bool:
    """Delete selected lexicon points, or the complete lexicon projection."""

    name = _col_for_corpus(corpus_id, "schemas")
    if not await client.collection_exists(name):
        return False
    if lexicon_ids:
        point_ids = [
            _schema_point_id(corpus_id, LEXICON_SCHEMA_KIND, lexicon_id)
            for lexicon_id in dict.fromkeys(lexicon_ids)
            if lexicon_id
        ]
        if not point_ids:
            return True
        deleted = True
        for start in range(0, len(point_ids), 2048):
            result = await client.delete(
                collection_name=name,
                points_selector=PointIdsList(points=point_ids[start : start + 2048]),
                wait=True,
            )
            deleted = deleted and getattr(result, "operation_id", None) is not None
        return deleted
    else:
        result = await client.delete(
            collection_name=name,
            points_selector=Filter(
                must=[
                    FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
                    FieldCondition(
                        key="kind", match=MatchValue(value=LEXICON_SCHEMA_KIND)
                    ),
                ]
            ),
        )
    return getattr(result, "operation_id", None) is not None


async def search_lexicon_entries(
    client: AsyncQdrantClient,
    corpus_id: str,
    *,
    query_vec: list[float] | None,
    exact_terms: list[str] | None = None,
    allowed_lexicon_ids: list[str] | None = None,
    top_k: int = 8,
    score_threshold: float | None = None,
    with_vectors: bool = False,
) -> list[dict]:
    """Return exact alias hits followed by dense plain-language gloss hits.

    Exact lookup is payload-indexed and therefore available even when the
    embedder is degraded. The caller decides which short terms are safe to use;
    this function performs no fuzzy matching.
    """

    name = _col_for_corpus(corpus_id, "schemas")
    if not await _collection_available(client, name):
        return []
    limit = max(1, int(top_k))
    base_must = [
        FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
        FieldCondition(key="kind", match=MatchValue(value=LEXICON_SCHEMA_KIND)),
    ]
    allowed_ids = [str(value) for value in (allowed_lexicon_ids or []) if str(value)]
    if allowed_ids:
        base_must.append(
            FieldCondition(key="lexicon_id", match=MatchAny(any=allowed_ids[:512]))
        )
    results: list[dict] = []
    normalized_terms = list(
        dict.fromkeys(
            str(term or "").strip().lower()
            for term in (exact_terms or [])
            if str(term or "").strip()
        )
    )
    if normalized_terms:
        should = [
            FieldCondition(key=field, match=MatchAny(any=normalized_terms))
            for field in (
                "canonical_key",
                "member_keys",
                "aliases_normalized",
                "abbreviations_normalized",
            )
        ]
        points, _ = await client.scroll(
            collection_name=name,
            scroll_filter=Filter(must=base_must, should=should),
            limit=max(limit * 2, 16),
            with_payload=True,
            with_vectors=with_vectors,
        )
        for point in points:
            payload = dict(getattr(point, "payload", None) or {})
            payload["score"] = 1.0
            payload["match_type"] = "exact_alias"
            if with_vectors:
                payload["_vector"] = getattr(point, "vector", None)
            results.append(payload)

    if query_vec is not None:
        kwargs = {
            "collection_name": name,
            "query_vector": query_vec,
            "query_filter": Filter(must=base_must),
            "limit": max(limit * 2, 12),
            "with_payload": True,
            "with_vectors": with_vectors,
        }
        if score_threshold is not None:
            kwargs["score_threshold"] = float(score_threshold)
        hits = await _search_points_compat(
            client,
            collection_name=kwargs["collection_name"],
            query_vector=kwargs["query_vector"],
            query_filter=kwargs["query_filter"],
            limit=kwargs["limit"],
            with_payload=kwargs["with_payload"],
            with_vectors=kwargs["with_vectors"],
            score_threshold=kwargs.get("score_threshold"),
        )
        for dense_rank, hit in enumerate(hits, start=1):
            payload = dict(getattr(hit, "payload", None) or {})
            payload["score"] = float(getattr(hit, "score", 0.0) or 0.0)
            payload["match_type"] = "gloss_vector"
            payload["dense_rank"] = dense_rank
            if with_vectors:
                payload["_vector"] = getattr(hit, "vector", None)
            results.append(payload)

    deduped: dict[str, dict] = {}
    for item in results:
        lexicon_id = str(item.get("lexicon_id") or "")
        if not lexicon_id:
            continue
        current = deduped.get(lexicon_id)
        if current is not None:
            if item.get("match_type") == "gloss_vector":
                current["gloss_score"] = max(
                    float(current.get("gloss_score") or 0.0),
                    float(item.get("score") or 0.0),
                )
                current["dense_rank"] = min(
                    int(current.get("dense_rank") or 1_000_000),
                    int(item.get("dense_rank") or 1_000_000),
                )
                if str(current.get("match_type") or "").startswith("exact"):
                    current["match_type"] = "exact_alias+gloss_vector"
            elif current.get("match_type") == "gloss_vector":
                item["gloss_score"] = max(
                    float(item.get("gloss_score") or 0.0),
                    float(current.get("score") or 0.0),
                )
                item["dense_rank"] = int(current.get("dense_rank") or 1_000_000)
        if current is None or float(item.get("score") or 0.0) > float(
            current.get("score") or 0.0
        ):
            deduped[lexicon_id] = item
            if current is not None and item.get("match_type") == "exact_alias":
                item["match_type"] = "exact_alias+gloss_vector"
        elif current is not None and item.get("match_type") == "exact_alias":
            current["match_type"] = "exact_alias+gloss_vector"
    return sorted(
        deduped.values(),
        key=lambda item: (
            0 if str(item.get("match_type") or "").startswith("exact") else 1,
            -float(item.get("score") or 0.0),
            str(item.get("canonical_key") or ""),
        ),
    )[:limit]


async def list_lexicon_ids(
    client: AsyncQdrantClient,
    corpus_id: str,
    *,
    page_size: int = 2048,
) -> list[str]:
    """List the currently mirrored lexicon IDs for stale-point reconciliation."""

    name = _col_for_corpus(corpus_id, "schemas")
    if not await _collection_available(client, name):
        return []
    offset = None
    output: list[str] = []
    while True:
        points, next_offset = await client.scroll(
            collection_name=name,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
                    FieldCondition(
                        key="kind", match=MatchValue(value=LEXICON_SCHEMA_KIND)
                    ),
                ]
            ),
            limit=max(1, int(page_size)),
            offset=offset,
            with_payload=["lexicon_id"],
            with_vectors=False,
        )
        output.extend(
            str((getattr(point, "payload", None) or {}).get("lexicon_id") or "")
            for point in points
            if (getattr(point, "payload", None) or {}).get("lexicon_id")
        )
        if next_offset is None:
            break
        offset = next_offset
    return list(dict.fromkeys(output))


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
    if not await _collection_available(client, name):
        return []

    # Per-corpus collection already filters by corpus; payload filter on
    # corpus_id is kept as defense-in-depth, kind filter is the real selector.
    hits = await _search_points_compat(
        client,
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
