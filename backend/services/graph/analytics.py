# backend/services/graph/analytics.py
# Graph Analysis P1 — Domain emergence pipeline.
# Reads chunk vectors from Qdrant, clusters documents into emergent domains,
# labels clusters via a single LLM call, caches the result in Mongo.
# Pure post-ingest, read-only against documents/chunks/entities. Writes only
# to the new `graph_domain_cache` collection.
#
# Exported entry point: emerge_domains(qdrant, neo4j_driver, db, corpus_id)

import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

import numpy as np

logger = logging.getLogger(__name__)


MIN_CLUSTER_SIZE = 5
KMEANS_FALLBACK_THRESHOLD = 2
TOP_ENTITIES_PER_CLUSTER = 12
# 3000 leaves headroom for reasoning models (mimo-v2-pro, GLM-5.x, DeepSeek
# R1) that spend most of their token budget on internal <think> content
# before emitting the actual JSON. With the old 800-cap, reasoning models
# returned `finish_reason=length` with empty output → labels stuck at
# placeholder "Cluster N".
LABELER_MAX_TOKENS = 3000
LABELER_TEMPERATURE = 0.2
QDRANT_SCROLL_BATCH = 256

# P2 metrics tuning
METRICS_CACHE_SCHEMA_VERSION = 11
FRONTIER_DEGREE_MAX = 5
ANALOGY_TOPOLOGY_SIM_MIN = 0.85
QUERY_SCOPE_TOP_K = 60  # chunks to pull from Qdrant for query-scoped analysis
# Entity-level cap on the scope set. Without this, mid-sized corpora routinely
# produced 150-250 scope entities, which made filter_metrics_to_scope's
# OR-filter permissive to the point of being a no-op (most pairs passed). The
# cap is applied AFTER reciprocal-rank weighting, so the top-K entities are
# the ones most strongly associated with the highest-scoring chunks.
QUERY_SCOPE_ENTITY_CAP = 50
QUERY_VECTOR_ENTITY_FLOOR = 20
QUERY_ANCHOR_THRESHOLD = 0.72
QUERY_ANCHOR_LIMIT = 4
ANCHOR_CACHE_COLLECTION = "graph_anchor_cache"
ANCHOR_TEXT_SLICE = 20
ANALOGY_NEIGHBOR_JACCARD_MIN = 0.50  # above → terminological; below → structural
TRANSFER_CD_PR_PERCENTILE = 90
INSIGHT_TOP_K = 10  # cap each detector's output for downstream LLM prompt
CONCEPT_COMMUNITY_TOP_ENTITIES = 8
CONCEPT_COMMUNITY_RESPONSE_LIMIT = 8
CONCEPT_SCOPE_ENTITY_CAP = 90
CONCEPT_PEERS_PER_COMMUNITY = 18
NOISE_ENTITY_TYPES = {"document", "timereference", "time_reference", "date"}
NOISE_ENTITY_ID_PREFIXES = ("document:", "timereference:", "date:")
QUERY_CONCEPT_MATCH_LIMIT = 10
SHORT_QUERY_CONCEPT_TOKENS = {"ai", "ml", "kg", "llm", "rag"}
QUERY_CONCEPT_EXPANSIONS = {
    "ai": {
        "artificial", "intelligence", "neural", "network", "networks",
        "perceptron", "machine", "learning", "model", "models", "classifier",
        "classification", "algorithm", "algorithms", "generative",
    },
    "genai": {
        "generative", "artificial", "intelligence", "neural", "network",
        "model", "models", "algorithm", "algorithms",
    },
    "generative": {
        "generation", "generative", "design", "art", "algorithm",
        "algorithms", "genetic", "evolution", "evolutionary",
    },
    "users": {"user", "identity", "personal", "profile", "data", "information"},
    "user": {"users", "identity", "personal", "profile", "data", "information"},
    "information": {"data", "identity", "profile", "context", "memory"},
    "extraction": {"extract", "extracting", "identity", "classifier", "classification"},
}
QUERY_OBJECT_KIND_EXPANSIONS = {
    "library": {"Library"},
    "libraries": {"Library"},
    "package": {"Library"},
    "packages": {"Library"},
    "framework": {"Framework", "Library"},
    "frameworks": {"Framework", "Library"},
    "tool": {"Tool"},
    "tools": {"Tool"},
    "app": {"App"},
    "apps": {"App"},
    "application": {"App"},
    "applications": {"App"},
    "service": {"Service"},
    "services": {"Service"},
    "platform": {"Service"},
    "platforms": {"Service"},
    "dataset": {"Dataset"},
    "datasets": {"Dataset"},
    "corpus": {"Dataset"},
    "model": {"Model"},
    "models": {"Model"},
    "report": {"Report"},
    "reports": {"Report"},
    "book": {"Book"},
    "books": {"Book"},
    "tutorial": {"Tutorial"},
    "tutorials": {"Tutorial"},
    "paper": {"Paper"},
    "papers": {"Paper"},
}
QUERY_CANONICAL_FAMILY_EXPANSIONS = {
    "physics": {"physics_simulation"},
    "simulation": {"physics_simulation"},
    "simulations": {"physics_simulation"},
    "engine": {"physics_simulation"},
    "engines": {"physics_simulation"},
    "cymatics": {"cymatics"},
    "chladni": {"cymatics"},
    "vibration": {"cymatics"},
    "oscillation": {"cymatics"},
    "creative": {"creative_coding"},
    "coding": {"creative_coding"},
    "processing": {"creative_coding"},
    "art": {"creative_coding", "cymatics"},
    "generative": {"generative_ai", "creative_coding"},
    "ai": {"generative_ai"},
    "llm": {"generative_ai"},
    "model": {"generative_ai"},
    "models": {"generative_ai"},
    "identity": {"identity_extraction"},
    "profile": {"identity_extraction"},
    "profiles": {"identity_extraction"},
    "privacy": {"identity_extraction"},
    "pii": {"identity_extraction"},
    "user": {"identity_extraction"},
    "users": {"identity_extraction"},
    "rag": {"graph_rag"},
    "graph": {"graph_rag"},
    "graphrag": {"graph_rag"},
    "neo4j": {"graph_rag"},
    "qdrant": {"graph_rag"},
    "architecture": {"app_architecture"},
    "flutter": {"app_architecture"},
    "mobile": {"app_architecture"},
    "council": {"council_chat"},
    "gate": {"council_chat"},
    "book": {"book_generation"},
    "json": {"book_generation"},
    "local": {"mobile_ai"},
    "offline": {"mobile_ai"},
    "prd": {"prd_architecture"},
    "feasibility": {"prd_architecture", "app_architecture"},
}
QUERY_DOMAIN_TYPE_EXPANSIONS = {
    "feature": {"Feature"},
    "features": {"Feature"},
    "flow": {"Feature"},
    "flows": {"Feature"},
    "screen": {"Screen"},
    "screens": {"Screen"},
    "view": {"Screen"},
    "views": {"Screen"},
    "module": {"Module"},
    "modules": {"Module"},
    "pipeline": {"Module"},
    "pipelines": {"Module"},
    "router": {"Module"},
    "architecture": {"ArchitectureDecision", "Module"},
    "decision": {"ArchitectureDecision"},
    "decisions": {"ArchitectureDecision"},
    "constraint": {"Constraint"},
    "constraints": {"Constraint"},
    "limit": {"Constraint"},
    "limits": {"Constraint"},
    "risk": {"Risk"},
    "risks": {"Risk"},
    "blocker": {"Risk"},
    "blockers": {"Risk"},
    "milestone": {"Milestone"},
    "milestones": {"Milestone"},
    "phase": {"Milestone"},
    "phases": {"Milestone"},
    "model": {"AIModel"},
    "models": {"AIModel"},
    "llm": {"AIModel"},
    "classifier": {"AIModel"},
    "api": {"CloudService"},
    "apis": {"CloudService"},
    "service": {"CloudService"},
    "services": {"CloudService"},
    "json": {"DataObject"},
    "profile": {"DataObject"},
    "profiles": {"DataObject"},
    "signal": {"UserSignal"},
    "signals": {"UserSignal", "DataObject"},
    "persona": {"Persona"},
    "audience": {"Persona"},
    "pricing": {"PricingRule"},
    "subscription": {"PricingRule"},
    "upsell": {"PricingRule"},
    "output": {"OutputArtifact"},
    "artifact": {"OutputArtifact"},
}


@dataclass
class DomainCluster:
    cluster_id: int
    name: str
    size: int
    top_entities: list[str] = field(default_factory=list)
    centroid: list[float] = field(default_factory=list)


@dataclass
class DomainMap:
    corpus_id: str
    corpus_change_signature: str
    computed_at: datetime
    doc_assignments: dict[str, dict[str, Any]]
    clusters: dict[int, DomainCluster]
    outliers: list[str]


@dataclass
class QueryAnchor:
    """Exact-ish query anchor resolved before vector search.

    Anchors are cheap metadata matches (document filenames/headings/entities).
    They let Mission Control start from "the thing the user meant" before
    expanding through graph topology and semantic vector recall.
    """

    anchor_type: str
    anchor_id: str
    label: str
    score: float
    source: str
    doc_id: str | None = None


@dataclass
class QueryScopeResult:
    entity_ids: set[str]
    anchors: list[QueryAnchor] = field(default_factory=list)
    doc_ids: set[str] = field(default_factory=set)
    chunk_refs: list[dict[str, Any]] = field(default_factory=list)
    query_embedding: list[float] = field(default_factory=list)
    entity_scores: dict[str, float] = field(default_factory=dict)
    anchor_entity_count: int = 0
    vector_entity_count: int = 0


@dataclass
class VectorScopeResult:
    entity_ids: set[str] = field(default_factory=set)
    doc_ids: set[str] = field(default_factory=set)
    chunk_refs: list[dict[str, Any]] = field(default_factory=list)
    query_embedding: list[float] = field(default_factory=list)
    entity_scores: dict[str, float] = field(default_factory=dict)


# ── Pipeline primitives ────────────────────────────────────────────────────

def _dense_vector_from_qdrant(value) -> list[float] | None:
    """Normalize Qdrant vector payloads across legacy and named-vector layouts."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("dense")
    if not isinstance(value, list) or not value:
        return None
    return value


async def get_doc_fingerprints(qdrant, corpus_id: str) -> dict[str, list[float]]:
    """Scroll the per-corpus naive Qdrant collection and mean-aggregate child
    chunk vectors into a per-document fingerprint.

    Returns:
        {doc_id: 1024-dim fingerprint vector}
    """
    # Import inside to keep module-load time low and avoid circular risk.
    from services.storage.qdrant_writer import _col_for_corpus

    collection = _col_for_corpus(corpus_id, "naive")
    doc_vectors: dict[str, list[list[float]]] = defaultdict(list)

    offset = None
    while True:
        records, offset = await qdrant.scroll(
            collection_name=collection,
            limit=QDRANT_SCROLL_BATCH,
            with_payload=True,
            with_vectors=True,
            offset=offset,
        )
        if not records:
            break
        for r in records:
            payload = r.payload or {}
            doc_id = payload.get("doc_id")
            vector = _dense_vector_from_qdrant(r.vector)
            if not doc_id or vector is None:
                continue
            doc_vectors[doc_id].append(vector)
        if offset is None:
            break

    fingerprints: dict[str, list[float]] = {}
    for doc_id, vecs in doc_vectors.items():
        arr = np.asarray(vecs, dtype=np.float32)
        fingerprints[doc_id] = arr.mean(axis=0).tolist()

    logger.info(
        "Doc fingerprints: corpus=%s docs=%d collection=%s",
        corpus_id, len(fingerprints), collection,
    )
    return fingerprints


def cluster_docs(fingerprints: dict[str, list[float]]) -> dict[str, int]:
    """HDBSCAN on unit-normalized fingerprints (cosine surrogate via L2 on
    the unit sphere). Falls back to k-means when HDBSCAN produces fewer than
    two valid clusters. Outliers carry cluster_id = -1.

    Returns:
        {doc_id: cluster_id}
    """
    doc_ids = list(fingerprints.keys())
    if len(doc_ids) == 0:
        return {}
    if len(doc_ids) < MIN_CLUSTER_SIZE * 2:
        # Too small to split meaningfully — keep document domains broad and
        # let concept communities provide the finer local graph lens.
        return {d: 0 for d in doc_ids}

    X = np.asarray([fingerprints[d] for d in doc_ids], dtype=np.float32)

    # Try HDBSCAN first (sklearn 1.3+ ships it natively — no external C build).
    try:
        from sklearn.cluster import HDBSCAN

        norms = np.linalg.norm(X, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        Xn = X / norms
        clusterer = HDBSCAN(
            min_cluster_size=MIN_CLUSTER_SIZE,
            metric="euclidean",
            n_jobs=1,
        )
        labels = clusterer.fit_predict(Xn)
        n_valid = len({int(l) for l in labels if l != -1})
        if n_valid >= KMEANS_FALLBACK_THRESHOLD:
            return {doc_ids[i]: int(labels[i]) for i in range(len(doc_ids))}
        logger.info(
            "HDBSCAN produced %d valid clusters (< %d threshold) — k-means fallback",
            n_valid, KMEANS_FALLBACK_THRESHOLD,
        )
    except ImportError:
        logger.warning("sklearn HDBSCAN unavailable — k-means fallback")
    except Exception as exc:
        logger.warning("HDBSCAN failed (%s) — k-means fallback", exc)

    from sklearn.cluster import KMeans

    k = max(2, int(np.sqrt(len(doc_ids))))
    kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = kmeans.fit_predict(X)
    return {doc_ids[i]: int(labels[i]) for i in range(len(doc_ids))}


async def top_entities_per_cluster(
    neo4j_driver,
    cluster_assignments: dict[str, int],
    top_n: int = TOP_ENTITIES_PER_CLUSTER,
) -> dict[int, list[str]]:
    """For each cluster, pull the top-N most-mentioned Ghost-B entities across
    the cluster's documents.

    Gracefully returns empty lists per cluster when Neo4j is unavailable.
    """
    cluster_to_docs: dict[int, list[str]] = defaultdict(list)
    for doc_id, cid in cluster_assignments.items():
        cluster_to_docs[cid].append(doc_id)

    cluster_entities: dict[int, list[str]] = {cid: [] for cid in cluster_to_docs}

    if neo4j_driver is None:
        logger.warning("Neo4j driver unavailable — skipping entity extraction")
        return cluster_entities

    # display_name is the actual stored property; canonical_name is null on
    # all Entity nodes for the current Ghost B writer. Without coalesce
    # every entity comes back nameless and the labeler prompt is empty —
    # which is exactly what made every cluster keep its "Cluster 0/1"
    # placeholder name.
    cypher = """
    MATCH (d:Document)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(e:Entity)
    WHERE d.doc_id IN $doc_ids
    WITH e, count(*) AS freq
    RETURN coalesce(e.display_name, e.normalized_name, e.canonical_name) AS name
    ORDER BY freq DESC
    LIMIT $top_n
    """
    try:
        async with neo4j_driver.session() as session:
            for cid, doc_ids in cluster_to_docs.items():
                if not doc_ids:
                    continue
                result = await session.run(cypher, doc_ids=doc_ids, top_n=top_n)
                # Some Entity nodes can have null/empty canonical_name —
                # skip them rather than poisoning the labeler prompt.
                names = [
                    record["name"]
                    async for record in result
                    if record.get("name")
                ]
                cluster_entities[cid] = names
    except Exception as exc:
        logger.warning("Neo4j entity query failed: %s — returning empty lists", exc)

    return cluster_entities


def _placeholder_labels(cluster_entities: dict[int, list[str]]) -> dict[int, str]:
    fallback = {cid: f"Cluster {cid}" for cid in cluster_entities if cid != -1}
    if -1 in cluster_entities:
        fallback[-1] = "Outliers"
    return fallback


def _build_labeler_prompt(cluster_entities: dict[int, list[str]]) -> Optional[str]:
    """Return the labeler prompt, or None when there's nothing to label."""
    lines: list[str] = []
    for cid in sorted(cluster_entities.keys()):
        if cid == -1:
            continue
        entities = cluster_entities[cid]
        if not entities:
            continue
        preview = ", ".join(entities[:TOP_ENTITIES_PER_CLUSTER])
        lines.append(f"Cluster {cid}: {preview}")
    if not lines:
        return None
    return (
        "You are labeling emergent knowledge domains from clustered documents.\n"
        "For each cluster below, produce a short human-readable domain name "
        "(3-5 words, Title Case). The name should reflect the intellectual "
        "domain — not a description or a list.\n\n"
        + "\n".join(lines)
        + '\n\nRespond with JSON only, mapping cluster id (as string) to '
          'domain name. Example: {"0": "Flutter UI Development", '
          '"1": "Bayesian Inference"}'
    )


def _parse_labeler_response(
    raw: Optional[str],
    cluster_entities: dict[int, list[str]],
) -> dict[int, str]:
    labels = _placeholder_labels(cluster_entities)
    try:
        parsed = json.loads((raw or "").strip())
        if not isinstance(parsed, dict):
            raise ValueError("labeler did not return a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Labeler JSON parse failed (%s) — placeholder labels; raw=%r",
            exc, (raw or "")[:120],
        )
        return labels

    for cid_str, name in parsed.items():
        try:
            cid = int(cid_str)
        except (ValueError, TypeError):
            continue
        if cid in cluster_entities and cid != -1:
            labels[cid] = str(name).strip() or labels[cid]
    return labels


async def label_clusters(
    cluster_entities: dict[int, list[str]],
    *,
    user_id: Optional[str] = None,
    model_override: Optional[str] = None,
) -> dict[int, str]:
    """ONE LLM call that labels every non-outlier cluster. Returns
    {cluster_id: human-readable name}. Falls back to placeholder names on any
    error so the pipeline never fails because the labeler misbehaves.

    Resolves the model via the same user-pool path chat uses — not the raw
    DEFAULT_COMPLETION_MODEL env — so users' actual configured models drive
    Mission Control labeling too.
    """
    from services.llm import llm_service
    from services.graph.orchestrator import _resolve_graph_model

    prompt = _build_labeler_prompt(cluster_entities)
    if prompt is None:
        logger.info("No labelable clusters — returning placeholder labels")
        return _placeholder_labels(cluster_entities)

    creds = await _resolve_graph_model(user_id, model_override)
    if not creds["model"]:
        logger.warning(
            "Labeler skipped — no model resolved (user=%s) — placeholder labels",
            user_id,
        )
        return _placeholder_labels(cluster_entities)

    # See orchestrator._call_llm for why response_format is not set.
    try:
        raw = await llm_service.complete_sync(
            messages=[{"role": "user", "content": prompt}],
            model=creds["model"],
            temperature=LABELER_TEMPERATURE,
            max_tokens=LABELER_MAX_TOKENS,
            api_base=creds["api_base"],
            api_key=creds["api_key"],
            extra_params=creds["extra_params"],
            timeout=60.0,
        )
    except Exception as exc:
        logger.warning(
            "Labeler LLM call failed (model=%s user=%s): %s — placeholder labels",
            creds["model"], user_id, exc,
        )
        return _placeholder_labels(cluster_entities)

    return _parse_labeler_response(raw, cluster_entities)


async def compute_corpus_change_signature(db, corpus_id: str) -> str:
    """sha256 of sorted doc_ids paired with their updated_at timestamps.
    Signature changes whenever a doc is added, removed, or re-ingested."""
    cursor = db["documents"].find(
        {"corpus_id": corpus_id},
        {"doc_id": 1, "updated_at": 1, "_id": 0},
    ).sort("doc_id", 1)
    docs = await cursor.to_list(length=None)
    parts = []
    for d in docs:
        did = d.get("doc_id", "")
        ts = d.get("updated_at")
        ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts or "")
        parts.append(f"{did}:{ts_str}")
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def get_cached_domain_map(db, corpus_id: str) -> DomainMap | None:
    """Load the current DomainMap without running document clustering.

    This is the graph-query safe path: user requests may check whether the
    index-time cache is ready, but they must not run clustering/labeling work.
    """
    signature = await compute_corpus_change_signature(db, corpus_id)
    cached = await db["graph_domain_cache"].find_one(
        {"corpus_id": corpus_id, "corpus_change_signature": signature}
    )
    if not cached:
        return None
    return _deserialize_domain_map(cached)


async def get_cached_metrics(
    db,
    corpus_id: str,
    corpus_change_signature: str,
) -> "CorpusMetrics | None":
    """Load the current CorpusMetrics without running graph algorithms.

    Used by Mission Control query/suggestion endpoints so Louvain, PageRank,
    bridge detection, and related corpus-scale analytics stay in the ingestion
    warmup path instead of the interactive query path.
    """
    cached = await db["graph_metrics_cache"].find_one(
        {
            "corpus_id": corpus_id,
            "corpus_change_signature": corpus_change_signature,
        }
    )
    if not cached or cached.get("schema_version") != METRICS_CACHE_SCHEMA_VERSION:
        return None
    return _deserialize_metrics(cached)


# ── Public orchestrator ────────────────────────────────────────────────────

async def emerge_domains(
    qdrant,
    neo4j_driver,
    db,
    corpus_id: str,
    *,
    force: bool = False,
    user_id: Optional[str] = None,
) -> DomainMap:
    """Run the domain emergence pipeline end-to-end. Cache-first: returns a
    stored DomainMap if `corpus_change_signature` matches.

    Args:
        qdrant:      AsyncQdrantClient
        neo4j_driver: neo4j.AsyncDriver or None (tolerates disabled Neo4j)
        db:          motor AsyncIOMotorDatabase
        corpus_id:   target corpus UUID
        force:       if True, bypass cache and recompute
        user_id:     owner — used to resolve the labeler LLM model from the
                     user's query pool (same path chat uses)

    Returns:
        DomainMap
    """
    signature = await compute_corpus_change_signature(db, corpus_id)

    if not force:
        cached = await db["graph_domain_cache"].find_one(
            {"corpus_id": corpus_id, "corpus_change_signature": signature}
        )
        if cached:
            logger.info(
                "Domain cache HIT corpus=%s sig=%s", corpus_id, signature[:8]
            )
            return _deserialize_domain_map(cached)

    logger.info(
        "Domain cache MISS corpus=%s sig=%s — running emergence",
        corpus_id, signature[:8],
    )

    fingerprints = await get_doc_fingerprints(qdrant, corpus_id)
    if not fingerprints:
        raise ValueError(
            f"No documents found in Qdrant naive collection for corpus {corpus_id}"
        )

    cluster_assignments = cluster_docs(fingerprints)
    cluster_entities = await top_entities_per_cluster(neo4j_driver, cluster_assignments)
    labels = await label_clusters(cluster_entities, user_id=user_id)

    # Assemble per-doc assignments + per-cluster summaries.
    cluster_sizes: dict[int, int] = defaultdict(int)
    cluster_members: dict[int, list[str]] = defaultdict(list)
    doc_assignments: dict[str, dict[str, Any]] = {}
    outliers: list[str] = []

    for doc_id, cid in cluster_assignments.items():
        cluster_sizes[cid] += 1
        cluster_members[cid].append(doc_id)
        name = labels.get(cid, f"Cluster {cid}")
        doc_assignments[doc_id] = {
            "cluster_id": cid,
            "cluster_name": name,
            "confidence": 0.0 if cid == -1 else 1.0,
        }
        if cid == -1:
            outliers.append(doc_id)

    centroids: dict[int, list[float]] = {}
    for cid, docs in cluster_members.items():
        vecs = np.asarray([fingerprints[d] for d in docs], dtype=np.float32)
        centroids[cid] = vecs.mean(axis=0).tolist()

    clusters = {
        cid: DomainCluster(
            cluster_id=cid,
            name=labels.get(cid, f"Cluster {cid}"),
            size=cluster_sizes[cid],
            top_entities=cluster_entities.get(cid, []),
            centroid=centroids.get(cid, []),
        )
        for cid in cluster_sizes
    }

    domain_map = DomainMap(
        corpus_id=corpus_id,
        corpus_change_signature=signature,
        computed_at=datetime.utcnow(),
        doc_assignments=doc_assignments,
        clusters=clusters,
        outliers=outliers,
    )

    await _cache_domain_map(db, domain_map)
    return domain_map


# ── Cache I/O ──────────────────────────────────────────────────────────────

async def _cache_domain_map(db, dm: DomainMap) -> None:
    await db["graph_domain_cache"].update_one(
        {"corpus_id": dm.corpus_id},
        {
            "$set": {
                "corpus_id": dm.corpus_id,
                "corpus_change_signature": dm.corpus_change_signature,
                "computed_at": dm.computed_at,
                "doc_assignments": dm.doc_assignments,
                "clusters": {
                    str(cid): {
                        "cluster_id": c.cluster_id,
                        "name": c.name,
                        "size": c.size,
                        "top_entities": c.top_entities,
                        "centroid": c.centroid,
                    }
                    for cid, c in dm.clusters.items()
                },
                "outliers": dm.outliers,
            }
        },
        upsert=True,
    )


def _deserialize_domain_map(doc: dict) -> DomainMap:
    clusters: dict[int, DomainCluster] = {}
    for cid_str, c in (doc.get("clusters") or {}).items():
        cid = int(cid_str)
        clusters[cid] = DomainCluster(
            cluster_id=c["cluster_id"],
            name=c["name"],
            size=c["size"],
            top_entities=c.get("top_entities", []),
            centroid=c.get("centroid", []),
        )
    return DomainMap(
        corpus_id=doc["corpus_id"],
        corpus_change_signature=doc["corpus_change_signature"],
        computed_at=doc["computed_at"],
        doc_assignments=doc.get("doc_assignments", {}),
        clusters=clusters,
        outliers=doc.get("outliers", []),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Query-scoped subgraph (InfraNodus-style)
# ═══════════════════════════════════════════════════════════════════════════
#
# Without this, every Mission Control turn computed insights against the
# full corpus regardless of query — so "what are the main themes?" and
# "how does X connect to Y?" returned the same generic graph stats.
#
# With this, the user's query first goes through Qdrant vector search to
# pull the top-K most semantically relevant chunks. The entity_ids those
# chunks mention become the QUERY SCOPE. All cached corpus-wide candidate
# lists (frontier, bridges, analogies, transfers) are then filtered to
# only items whose endpoints fall in that scope.
#
# Cache stays warm — heavy compute still runs once per corpus. Per-query
# overhead is ~150-300ms for embed + scroll + Cypher. LLM prompt shrinks
# dramatically because we only show it query-relevant items, which both
# speeds synthesis AND makes the result actually about the user's question.
# ═══════════════════════════════════════════════════════════════════════════


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "how",
    "in", "into", "is", "it", "me", "of", "on", "or", "so", "that",
    "the", "this", "to", "with", "what", "why",
}


def _normalize_anchor_text(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"\.[a-z0-9]{1,8}$", "", value)
    value = re.sub(r"[_\-\/\\]+", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _anchor_tokens(value: str) -> set[str]:
    return {
        t for t in _normalize_anchor_text(value).split()
        if len(t) > 2 and t not in _STOPWORDS
    }


def _score_anchor_match(query_norm: str, label_norm: str) -> float:
    if not query_norm or not label_norm or len(label_norm) < 3:
        return 0.0
    if query_norm == label_norm:
        return 1.0

    q_tokens = _anchor_tokens(query_norm)
    label_tokens = _anchor_tokens(label_norm)
    if not q_tokens or not label_tokens:
        return 0.0

    if len(label_tokens) >= 2 and label_norm in query_norm:
        return 0.95
    if len(q_tokens) >= 2 and query_norm in label_norm:
        return 0.88

    overlap = q_tokens & label_tokens
    if len(overlap) < 2:
        return 0.0
    coverage = len(overlap) / max(1, len(label_tokens))
    query_coverage = len(overlap) / max(1, len(q_tokens))
    return min(0.86, 0.55 + 0.25 * coverage + 0.10 * query_coverage)


def _heading_aliases_from_text(text: str, *, limit: int = 8) -> list[str]:
    headings: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or len(line) > 120:
            continue
        md = re.match(r"^#{1,4}\s+(.+)$", line)
        if md:
            headings.append(md.group(1).strip())
        elif (
            len(line.split()) <= 8
            and len(line) >= 4
            and line.upper() == line
            and any(ch.isalpha() for ch in line)
        ):
            headings.append(line.title())
        if len(headings) >= limit:
            break
    # Stable de-dupe.
    seen = set()
    out = []
    for h in headings:
        n = _normalize_anchor_text(h)
        if n and n not in seen:
            seen.add(n)
            out.append(h)
    return out


async def _entity_anchor_rows(neo4j_driver, corpus_id: str) -> list[dict]:
    if neo4j_driver is None:
        return []
    cypher = """
    MATCH (c:Chunk {corpus_id: $corpus_id})-[:MENTIONS]->(e:Entity)
    WITH DISTINCT e
    RETURN e.entity_id AS anchor_id,
           coalesce(e.display_name, e.normalized_name, e.canonical_name, e.entity_id) AS label
    LIMIT 2000
    """
    rows: list[dict] = []
    try:
        async with neo4j_driver.session() as s:
            result = await s.run(cypher, corpus_id=corpus_id)
            async for rec in result:
                label = rec.get("label") or rec.get("anchor_id")
                if label:
                    rows.append({
                        "anchor_type": "entity",
                        "anchor_id": rec["anchor_id"],
                        "label": label,
                        "source": "entity_name",
                        "normalized": _normalize_anchor_text(label),
                    })
    except Exception as exc:
        logger.warning("anchor_index: entity anchor load failed: %s", exc)
    return rows


async def _load_or_build_anchor_rows(db, neo4j_driver, corpus_id: str) -> list[dict]:
    """Return compact, cached anchor rows for a corpus.

    This is intentionally metadata-only. It avoids per-query chunk scans while
    giving Mission Control exact-ish handles for filenames, headings, and
    canonical entity names.
    """
    if db is None:
        return []
    try:
        sig = await compute_corpus_change_signature(db, corpus_id)
        cached = await db[ANCHOR_CACHE_COLLECTION].find_one(
            {"corpus_id": corpus_id, "signature": sig},
            {"_id": 0, "anchors": 1},
        )
        if cached and isinstance(cached.get("anchors"), list):
            return cached["anchors"]

        anchors: list[dict] = []
        cursor = db["documents"].find(
            {"corpus_id": corpus_id},
            {
                "_id": 0,
                "doc_id": 1,
                "filename": 1,
                "title": 1,
                "parent_chunks": {"$slice": ANCHOR_TEXT_SLICE},
            },
        )
        async for doc in cursor:
            doc_id = doc.get("doc_id")
            filename = doc.get("filename") or doc.get("title") or doc_id
            if not doc_id or not filename:
                continue
            for label, source in (
                (filename, "filename"),
                (re.sub(r"\.[a-zA-Z0-9]{1,8}$", "", filename), "filename_stem"),
            ):
                norm = _normalize_anchor_text(label)
                if norm:
                    anchors.append({
                        "anchor_type": "document",
                        "anchor_id": doc_id,
                        "doc_id": doc_id,
                        "label": label,
                        "source": source,
                        "normalized": norm,
                    })
            for parent in doc.get("parent_chunks") or []:
                for heading in _heading_aliases_from_text(parent.get("text", "")):
                    norm = _normalize_anchor_text(heading)
                    if norm:
                        anchors.append({
                            "anchor_type": "heading",
                            "anchor_id": parent.get("parent_id") or doc_id,
                            "doc_id": doc_id,
                            "label": heading,
                            "source": "document_heading",
                            "normalized": norm,
                        })

        anchors.extend(await _entity_anchor_rows(neo4j_driver, corpus_id))

        # Compact de-dupe: same type/id/normalized carries no extra signal.
        deduped: list[dict] = []
        seen = set()
        for a in anchors:
            key = (a.get("anchor_type"), a.get("anchor_id"), a.get("normalized"))
            if not a.get("normalized") or key in seen:
                continue
            seen.add(key)
            deduped.append(a)

        await db[ANCHOR_CACHE_COLLECTION].replace_one(
            {"corpus_id": corpus_id},
            {
                "corpus_id": corpus_id,
                "signature": sig,
                "computed_at": datetime.utcnow(),
                "anchors": deduped,
            },
            upsert=True,
        )
        logger.info(
            "anchor_index: built corpus=%s anchors=%d sig=%s",
            corpus_id, len(deduped), sig[:8],
        )
        return deduped
    except Exception as exc:
        logger.warning("anchor_index: load/build failed: %s", exc)
        return []


async def resolve_query_anchors(
    db,
    neo4j_driver,
    corpus_id: str,
    query: str,
    *,
    limit: int = QUERY_ANCHOR_LIMIT,
) -> list[QueryAnchor]:
    query_norm = _normalize_anchor_text(query)
    if not query_norm:
        return []
    rows = await _load_or_build_anchor_rows(db, neo4j_driver, corpus_id)
    scored: list[QueryAnchor] = []
    for row in rows:
        score = _score_anchor_match(query_norm, row.get("normalized", ""))
        if score < QUERY_ANCHOR_THRESHOLD:
            continue
        scored.append(QueryAnchor(
            anchor_type=row.get("anchor_type") or "unknown",
            anchor_id=row.get("anchor_id") or "",
            doc_id=row.get("doc_id"),
            label=row.get("label") or row.get("anchor_id") or "",
            score=round(score, 3),
            source=row.get("source") or "anchor_index",
        ))

    type_rank = {"document": 4, "heading": 3, "entity": 2}
    scored.sort(
        key=lambda a: (a.score, type_rank.get(a.anchor_type, 0), len(a.label)),
        reverse=True,
    )

    out: list[QueryAnchor] = []
    seen = set()
    for a in scored:
        key = (a.anchor_type, a.anchor_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
        if len(out) >= limit:
            break
    if out:
        logger.info(
            "query_anchors: corpus=%s query=%r anchors=%s",
            corpus_id, query[:60],
            [(a.anchor_type, a.label, a.score) for a in out],
        )
    return out


async def _anchor_scope_entities(
    neo4j_driver,
    corpus_id: str,
    anchors: list[QueryAnchor],
    *,
    entity_cap: int,
) -> list[str]:
    if neo4j_driver is None or not anchors or entity_cap <= 0:
        return []
    doc_ids = [a.doc_id or a.anchor_id for a in anchors
               if a.anchor_type in {"document", "heading"} and (a.doc_id or a.anchor_id)]
    entity_ids = [a.anchor_id for a in anchors if a.anchor_type == "entity" and a.anchor_id]
    weights: dict[str, float] = defaultdict(float)
    try:
        async with neo4j_driver.session() as s:
            if doc_ids:
                result = await s.run(
                    """
                    MATCH (d:Document {corpus_id: $corpus_id})-[:HAS_CHUNK]->(:Chunk)-[m:MENTIONS]->(e:Entity)
                    WHERE d.doc_id IN $doc_ids
                    RETURN e.entity_id AS eid, count(m) AS weight
                    """,
                    corpus_id=corpus_id,
                    doc_ids=doc_ids,
                )
                async for rec in result:
                    if rec["eid"]:
                        weights[rec["eid"]] += float(rec.get("weight") or 1)

            if entity_ids:
                result = await s.run(
                    """
                    MATCH (seed:Entity)
                    WHERE seed.entity_id IN $entity_ids
                    RETURN seed.entity_id AS eid, 100.0 AS weight
                    UNION
                    MATCH (seed:Entity)-[:RELATES_TO]-(n:Entity)<-[:MENTIONS]-(:Chunk {corpus_id: $corpus_id})
                    WHERE seed.entity_id IN $entity_ids
                    RETURN n.entity_id AS eid, 10.0 AS weight
                    """,
                    corpus_id=corpus_id,
                    entity_ids=entity_ids,
                )
                async for rec in result:
                    if rec["eid"]:
                        weights[rec["eid"]] += float(rec.get("weight") or 1)
    except Exception as exc:
        logger.warning("query_anchors: scope expansion failed: %s", exc)
        return []

    ranked = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    return [eid for eid, _ in ranked[:entity_cap]]


async def query_scope_entities(
    qdrant,
    neo4j_driver,
    corpus_id: str,
    query: str,
    *,
    top_k: int = QUERY_SCOPE_TOP_K,
    entity_cap: int = QUERY_SCOPE_ENTITY_CAP,
) -> set[str]:
    """Resolve the user's query to the top-N most relevant entity_ids.

    1. Embed query via the shared embedder service.
    2. Vector-search the corpus's `naive` collection for top-K chunks,
       preserving rank order.
    3. Pull (entity, chunk) pairs from Neo4j.
    4. Score each entity by reciprocal-rank fusion — sum of 1/(rank+1)
       over all chunks it's mentioned in. Entities appearing in the top
       chunk weigh ~1.0; entities only in the tail chunks weigh ~0.02.
       Entities appearing in many relevant chunks accumulate weight.
    5. Return the top `entity_cap` entities as a set.

    Returns an empty set when query is empty/short or Neo4j is unavailable.
    """
    if not query or len(query.strip()) < 3:
        return set()
    if neo4j_driver is None:
        return set()

    from services.embedder import embed_query
    from services.storage.qdrant_writer import _col_for_corpus

    try:
        qv = await embed_query(query)
    except Exception as exc:
        logger.warning("query_scope: embed_query failed (%s) — empty scope", exc)
        return set()

    collection = _col_for_corpus(corpus_id, "naive")
    try:
        resp = await qdrant.query_points(
            collection_name=collection,
            query=qv,
            limit=top_k,
            with_payload=True,
        )
    except Exception as exc:
        logger.warning("query_scope: qdrant search failed (%s) — empty scope", exc)
        return set()

    # Preserve per-chunk rank so we can weight entities by the best chunk
    # they appear in. Qdrant returns points in descending similarity order.
    chunk_rank: dict[str, int] = {}
    for rank, h in enumerate(resp.points):
        cid = (h.payload or {}).get("chunk_id")
        if cid and cid not in chunk_rank:
            chunk_rank[cid] = rank
    if not chunk_rank:
        return set()

    cypher = """
    MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
    WHERE c.chunk_id IN $chunk_ids
    RETURN e.entity_id AS eid, c.chunk_id AS cid
    """
    entity_weight: dict[str, float] = defaultdict(float)
    try:
        async with neo4j_driver.session() as s:
            r = await s.run(cypher, chunk_ids=list(chunk_rank.keys()))
            async for rec in r:
                eid = rec["eid"]
                cid = rec["cid"]
                if not eid or cid not in chunk_rank:
                    continue
                entity_weight[eid] += 1.0 / (chunk_rank[cid] + 1)
    except Exception as exc:
        logger.warning("query_scope: neo4j scope query failed (%s)", exc)
        return set()

    ranked = sorted(entity_weight.items(), key=lambda kv: kv[1], reverse=True)
    scope = {eid for eid, _ in ranked[:entity_cap]}

    logger.info(
        "query_scope: corpus=%s query=%r chunks=%d raw_entities=%d scope=%d cap=%d",
        corpus_id, query[:60], len(chunk_rank), len(ranked), len(scope), entity_cap,
    )
    return scope


async def query_scope_details(
    qdrant,
    neo4j_driver,
    corpus_id: str,
    query: str,
    *,
    top_k: int = QUERY_SCOPE_TOP_K,
    entity_cap: int = QUERY_SCOPE_ENTITY_CAP,
) -> VectorScopeResult:
    """Resolve query scope with bounded chunk evidence for synthesis.

    This mirrors query_scope_entities(), but carries the query embedding,
    ranked chunk refs, source doc ids, and entity relevance weights so the
    orchestrator can persist scope metadata and give the LLM a small evidence
    packet without re-querying stores.
    """
    if not query or len(query.strip()) < 3 or neo4j_driver is None:
        return VectorScopeResult()

    from services.embedder import embed_query
    from services.storage.qdrant_writer import _col_for_corpus

    try:
        qv = await embed_query(query)
    except Exception as exc:
        logger.warning("query_scope: embed_query failed (%s) — empty scope", exc)
        return VectorScopeResult()

    collection = _col_for_corpus(corpus_id, "naive")
    try:
        resp = await qdrant.query_points(
            collection_name=collection,
            query=qv,
            limit=top_k,
            with_payload=True,
        )
    except Exception as exc:
        logger.warning("query_scope: qdrant search failed (%s) — empty scope", exc)
        return VectorScopeResult(query_embedding=qv)

    chunk_rank: dict[str, int] = {}
    chunk_refs: list[dict[str, Any]] = []
    doc_ids: set[str] = set()
    for rank, hit in enumerate(resp.points):
        payload = hit.payload or {}
        chunk_id = payload.get("chunk_id")
        if not chunk_id or chunk_id in chunk_rank:
            continue
        chunk_rank[chunk_id] = rank
        doc_id = payload.get("doc_id")
        if doc_id:
            doc_ids.add(str(doc_id))
        # Qdrant child/summary payloads store bounded excerpts as
        # `chunk_text`; older callers used `text`. Accept both so graph
        # synthesis receives evidence instead of empty excerpt slots.
        text = str(payload.get("text") or payload.get("chunk_text") or "").strip()
        chunk_refs.append({
            "chunk_id": str(chunk_id),
            "doc_id": str(doc_id or ""),
            "score": round(float(getattr(hit, "score", 0.0) or 0.0), 4),
            "heading_path": payload.get("heading_path") or [],
            "text": text[:1200],
        })
    if not chunk_rank:
        return VectorScopeResult(query_embedding=qv)

    cypher = """
    MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
    WHERE c.chunk_id IN $chunk_ids
    RETURN e.entity_id AS eid, c.chunk_id AS cid
    """
    entity_weight: dict[str, float] = defaultdict(float)
    try:
        async with neo4j_driver.session() as s:
            result = await s.run(cypher, chunk_ids=list(chunk_rank.keys()))
            async for rec in result:
                eid = rec["eid"]
                cid = rec["cid"]
                if not eid or cid not in chunk_rank:
                    continue
                entity_weight[eid] += 1.0 / (chunk_rank[cid] + 1)
    except Exception as exc:
        logger.warning("query_scope: neo4j scope query failed (%s)", exc)
        return VectorScopeResult(
            doc_ids=doc_ids,
            chunk_refs=chunk_refs,
            query_embedding=qv,
        )

    ranked = sorted(entity_weight.items(), key=lambda kv: kv[1], reverse=True)
    capped = ranked[:entity_cap]
    max_weight = max((weight for _, weight in capped), default=1.0) or 1.0
    entity_scores = {eid: round(weight / max_weight, 4) for eid, weight in capped}
    scope = set(entity_scores)

    logger.info(
        "query_scope_details: corpus=%s query=%r chunks=%d raw_entities=%d scope=%d cap=%d",
        corpus_id, query[:60], len(chunk_rank), len(ranked), len(scope), entity_cap,
    )
    return VectorScopeResult(
        entity_ids=scope,
        doc_ids=doc_ids,
        chunk_refs=chunk_refs,
        query_embedding=qv,
        entity_scores=entity_scores,
    )


async def resolve_query_scope(
    qdrant,
    neo4j_driver,
    db,
    corpus_id: str,
    query: str,
    *,
    top_k: int = QUERY_SCOPE_TOP_K,
    entity_cap: int = QUERY_SCOPE_ENTITY_CAP,
) -> QueryScopeResult:
    """Hybrid scope resolver: exact anchors first, vector recall second.

    Anchors are O(1-ish) cached metadata/entity lookups. When they resolve,
    they reserve the first slice of the entity budget, and vector search fills
    only the remaining capacity. This preserves speed while making document-
    and entity-specific queries feel grounded rather than fuzzy.
    """
    anchors = await resolve_query_anchors(db, neo4j_driver, corpus_id, query)
    # Anchors are precision handles, but they should not consume the entire
    # scope budget. Mission Control still needs vector-ranked chunks for
    # textual evidence and for recall around the user's actual wording.
    vector_floor = (
        min(QUERY_VECTOR_ENTITY_FLOOR, max(1, int(entity_cap * 0.4)))
        if anchors and entity_cap > 0
        else 0
    )
    anchor_cap = max(0, entity_cap - vector_floor) if anchors else entity_cap
    anchor_entities = await _anchor_scope_entities(
        neo4j_driver, corpus_id, anchors, entity_cap=anchor_cap
    )
    remaining = max(0, entity_cap - len(anchor_entities))
    vector_scope = VectorScopeResult()
    if remaining > 0:
        vector_scope = await query_scope_details(
            qdrant,
            neo4j_driver,
            corpus_id,
            query,
            top_k=20 if anchors else top_k,
            entity_cap=remaining,
        )

    anchor_scores = {
        eid: round(1.0 - (idx * 0.02), 4)
        for idx, eid in enumerate(anchor_entities)
    }
    entity_scores = dict(vector_scope.entity_scores)
    for eid, score in anchor_scores.items():
        entity_scores[eid] = max(entity_scores.get(eid, 0.0), score)

    combined = set(anchor_entities) | set(vector_scope.entity_ids)
    doc_ids = set(vector_scope.doc_ids)
    for anchor in anchors:
        if anchor.doc_id:
            doc_ids.add(anchor.doc_id)
        elif anchor.anchor_type == "document" and anchor.anchor_id:
            doc_ids.add(anchor.anchor_id)
    logger.info(
        "query_scope_resolved: corpus=%s query=%r anchors=%d anchor_entities=%d "
        "vector_entities=%d total=%d cap=%d",
        corpus_id, query[:60], len(anchors), len(anchor_entities),
        len(vector_scope.entity_ids), len(combined), entity_cap,
    )
    return QueryScopeResult(
        entity_ids=combined,
        anchors=anchors,
        doc_ids=doc_ids,
        chunk_refs=vector_scope.chunk_refs,
        query_embedding=vector_scope.query_embedding,
        entity_scores=entity_scores,
        anchor_entity_count=len(anchor_entities),
        vector_entity_count=len(vector_scope.entity_ids),
    )


def filter_metrics_to_scope(
    metrics: "CorpusMetrics", scope: set[str]
) -> "CorpusMetrics":
    """Return a shallow-copied metrics object whose detector lists are
    restricted and RE-RANKED by scope overlap.

    Ranking rule for pair-oriented detectors (bridges, analogies,
    terminological gaps):
      1. Both endpoints in scope (strong query relevance) — ranked first
      2. Exactly one endpoint in scope (partial relevance) — ranked second
      3. Neither in scope — dropped entirely

    Without this reranking, OR-matching on scope sets of 150+ entities
    produced "filtered" lists that still contained every corpus-wide
    candidate, defeating the purpose of scoping. With reranking, the
    top slots in the prompt are candidates whose both-endpoints sit
    in the user's query neighborhood.

    Corpus-wide totals (node_count, edge_count, density, modularity,
    cross_pct) are kept intact so the LLM sees the global baseline.
    """
    if not scope:
        return metrics

    def _rank_pair(item: dict) -> int:
        src_in = item.get("source") in scope
        tgt_in = item.get("target") in scope
        if src_in and tgt_in:
            return 2
        if src_in or tgt_in:
            return 1
        return 0

    def _rerank_pairs(items: list[dict]) -> list[dict]:
        # Drop pairs with zero scope overlap, then stable-sort by rank desc
        # so downstream [:N] slicing takes the most query-relevant pairs.
        scored = [(it, _rank_pair(it)) for it in items]
        scored = [(it, r) for it, r in scored if r > 0]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return _diversify_items(
            [it for it, _ in scored],
            key_fn=lambda it: (
                metrics.entity_concept_map.get(it.get("source"), {}).get("concept_id"),
                metrics.entity_concept_map.get(it.get("target"), {}).get("concept_id"),
            ),
        )

    def _diversify_items(
        items: list[dict],
        *,
        key_fn,
        max_per_key: int = 2,
    ) -> list[dict]:
        buckets: dict[Any, list[dict]] = defaultdict(list)
        for item in items:
            key = key_fn(item)
            buckets[key].append(item)
        ordered: list[dict] = []
        while buckets:
            for key in list(buckets.keys()):
                if buckets[key]:
                    ordered.append(buckets[key].pop(0))
                    if len([it for it in ordered if key_fn(it) == key]) >= max_per_key:
                        buckets.pop(key, None)
                if key in buckets and not buckets[key]:
                    buckets.pop(key, None)
        return ordered

    from dataclasses import replace as _replace
    scoped_frontier = [
        f for f in metrics.frontier_candidates if f.get("entity_id") in scope
    ]
    scoped_frontier = _diversify_items(
        scoped_frontier,
        key_fn=lambda it: metrics.entity_concept_map
        .get(it.get("entity_id"), {})
        .get("concept_id"),
    )
    scoped_transfers = [
        t for t in metrics.transfer_candidates if t.get("hub") in scope
    ]
    scoped_transfers = _diversify_items(
        scoped_transfers,
        key_fn=lambda it: metrics.entity_concept_map
        .get(it.get("hub"), {})
        .get("concept_id"),
    )
    scoped_concept_ids = {
        str(c.get("concept_id"))
        for eid in scope
        if (c := metrics.entity_concept_map.get(eid))
    }
    scoped_cluster_gaps = [
        g for g in metrics.cluster_pair_gaps
        if str(g.get("cluster_a")) in scoped_concept_ids
        or str(g.get("cluster_b")) in scoped_concept_ids
    ] or metrics.cluster_pair_gaps[:5]
    scoped_latent_topics = [
        t for t in metrics.latent_topics if t.get("entity_id") in scope
    ] or metrics.latent_topics[:5]
    return _replace(
        metrics,
        frontier_candidates=scoped_frontier,
        fragile_bridges=_rerank_pairs(metrics.fragile_bridges),
        terminological_gaps=_rerank_pairs(metrics.terminological_gaps),
        structural_analogies=_rerank_pairs(metrics.structural_analogies),
        transfer_candidates=scoped_transfers,
        # Top-PR list also filtered; if nothing intersects, keep top corpus PR
        # so the user still sees SOMETHING for orientation.
        top_pagerank=(
            [p for p in metrics.top_pagerank if p.get("entity_id") in scope]
            or metrics.top_pagerank[:5]
        ),
        top_cross_domain_pagerank=(
            [p for p in metrics.top_cross_domain_pagerank if p.get("entity_id") in scope]
            or metrics.top_cross_domain_pagerank[:5]
        ),
        concept_communities=relevant_concept_communities(metrics, scope),
        cluster_pair_gaps=scoped_cluster_gaps,
        latent_topics=scoped_latent_topics,
    )


def select_working_entities(
    metrics: "CorpusMetrics",
    scope: set[str],
    *,
    mode: str = "auto",
    entity_scores: Optional[dict[str, float]] = None,
    anchors: Optional[list[QueryAnchor]] = None,
    limit: int = 25,
) -> set[str]:
    """Choose a compact, concept-diverse entity set for the LLM working set."""
    if not scope:
        return set()

    entity_scores = entity_scores or {}
    anchor_ids = {a.anchor_id for a in (anchors or []) if a.anchor_type == "entity"}
    detector_counts: Counter[str] = Counter()
    for item in metrics.frontier_candidates:
        if item.get("entity_id"):
            detector_counts[item["entity_id"]] += 1
    for item in (
        metrics.fragile_bridges
        + metrics.terminological_gaps
        + metrics.structural_analogies
    ):
        if item.get("source"):
            detector_counts[item["source"]] += 1
        if item.get("target"):
            detector_counts[item["target"]] += 1
    for item in metrics.transfer_candidates:
        if item.get("hub"):
            detector_counts[item["hub"]] += 1

    mode_weights = {
        "auto": (2.0, 1.0, 0.2, 0.8, 1.2),
        "connect": (1.6, 0.9, 0.15, 1.8, 0.8),
        "gaps": (1.8, 0.8, 0.15, 0.7, 1.8),
        "themes": (1.0, 1.8, 0.25, 0.5, 0.6),
    }
    alpha, beta, gamma, delta, theta = mode_weights.get(mode, mode_weights["auto"])

    scored: list[tuple[float, str]] = []
    for eid in scope:
        concept = metrics.entity_concept_map.get(eid, {})
        query_relevance = float(entity_scores.get(eid, 0.0))
        centrality = float(concept.get("pagerank") or 0.0) * 1000.0
        degree = min(1.0, int(concept.get("degree") or 0) / 50.0)
        bridge_value = min(1.0, int(concept.get("bridge_count") or 0) / 10.0)
        detector_overlap = min(1.0, detector_counts.get(eid, 0) / 3.0)
        anchor_boost = 1.0 if eid in anchor_ids else 0.0
        score = (
            alpha * query_relevance
            + beta * centrality
            + gamma * degree
            + delta * bridge_value
            + theta * detector_overlap
            + 1.5 * anchor_boost
        )
        scored.append((score, eid))

    scored.sort(key=lambda item: item[0], reverse=True)
    selected: list[str] = []
    concept_counts: Counter[str] = Counter()
    remaining = scored
    while remaining and len(selected) < limit:
        best_idx = 0
        best_score = -1.0
        for idx, (score, eid) in enumerate(remaining):
            concept_id = str(
                metrics.entity_concept_map.get(eid, {}).get("concept_id") or ""
            )
            adjusted = score * (0.7 ** concept_counts[concept_id])
            if adjusted > best_score:
                best_score = adjusted
                best_idx = idx
        _, eid = remaining.pop(best_idx)
        selected.append(eid)
        concept_id = str(metrics.entity_concept_map.get(eid, {}).get("concept_id") or "")
        concept_counts[concept_id] += 1

    logger.info(
        "working_entity_select: mode=%s scope=%d selected=%d concepts=%d",
        mode, len(scope), len(selected), len([c for c in concept_counts if c]),
    )
    return set(selected)


# ═══════════════════════════════════════════════════════════════════════════
# P2 — Cross-domain metrics + insight detectors
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class CorpusMetrics:
    """Cached structural snapshot of the Neo4j graph for one corpus."""
    corpus_id: str
    corpus_change_signature: str
    computed_at: datetime
    node_count: int
    edge_count: int
    density: float
    cross_domain_edge_pct: float
    modularity_proxy: float  # fraction of edges inside same domain
    domain_density: dict[str, float]  # domain_id_str → density
    per_domain_edge_counts: dict[str, dict[str, int]]  # {domain: {internal, external}}
    relation_family_counts: dict[str, int]  # Structural / Operational / WeakAssociation, etc.
    top_pagerank: list[dict[str, Any]]
    top_cross_domain_pagerank: list[dict[str, Any]]
    node_domain_map: dict[str, str]  # entity_id → primary domain name
    node_domains_touched: dict[str, list[str]]  # entity_id → list of domains
    # Precomputed insight-candidate lists, ready for filter/rank at query time.
    frontier_candidates: list[dict[str, Any]]
    fragile_bridges: list[dict[str, Any]]
    terminological_gaps: list[dict[str, Any]]
    structural_analogies: list[dict[str, Any]]
    transfer_candidates: list[dict[str, Any]]
    entity_name_map: dict[str, str] = field(default_factory=dict)
    concept_communities: list[dict[str, Any]] = field(default_factory=list)
    entity_concept_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    entity_facet_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    ontology_version: str = ""
    entity_betweenness: dict[str, float] = field(default_factory=dict)
    cluster_pair_gaps: list[dict[str, Any]] = field(default_factory=list)
    latent_topics: list[dict[str, Any]] = field(default_factory=list)
    metrics_engine: str = "networkx"


# ── Graph construction from Neo4j ───────────────────────────────────────────

_ENTITY_CYPHER = """
MATCH (e:Entity)<-[:MENTIONS]-(:Chunk)<-[:HAS_CHUNK]-(d:Document)
WHERE d.corpus_id = $corpus_id
WITH e, d.doc_id AS doc_id, count(*) AS mentions
RETURN e.entity_id AS entity_id,
       coalesce(e.display_name, e.normalized_name, e.canonical_name) AS name,
       coalesce(e.primary_entity_type, e.entity_type) AS entity_type,
       e.object_kind AS object_kind,
       e.object_kind_parent AS object_kind_parent,
       e.object_kind_root AS object_kind_root,
       e.domain_type AS domain_type,
       e.domain_type_parent AS domain_type_parent,
       e.domain_type_root AS domain_type_root,
       e.canonical_family AS canonical_family,
       e.observed_entity_types AS observed_entity_types,
       e.ontology_version AS ontology_version,
       doc_id, mentions
"""

_RELATION_CYPHER = """
MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
WHERE a.entity_id IN $entity_ids AND b.entity_id IN $entity_ids
  AND (
      $corpus_id IN coalesce(r.corpus_ids, [])
      OR EXISTS {
          MATCH (c:Chunk {corpus_id: $corpus_id})
          WHERE c.chunk_id IN coalesce(r.evidence_chunk_ids, [])
      }
  )
RETURN a.entity_id AS source,
       b.entity_id AS target,
       r.predicate AS predicate,
       coalesce(r.relation_family, 'WeakAssociation') AS relation_family,
       coalesce(r.edge_strength, CASE WHEN r.predicate = 'related_to' THEN 'weak' ELSE 'strong' END) AS edge_strength,
       coalesce(r.eligible_for_synthesis, r.predicate <> 'related_to') AS eligible_for_synthesis
"""


async def _load_entities_with_mentions(
    session, corpus_id: str, doc_to_domain: dict[str, str],
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, dict[str, Any]],
    dict[str, Counter],
    dict[str, int],
    dict[str, int],
]:
    """Read entities + their per-document mention counts from Neo4j.
    Returns (entity_to_name, entity_to_type, entity_to_facets, entity_to_domain_counts)."""
    entity_to_name: dict[str, str] = {}
    entity_to_type: dict[str, str] = {}
    entity_to_facets: dict[str, dict[str, Any]] = {}
    entity_to_domains: dict[str, Counter] = defaultdict(Counter)
    entity_mention_counts: dict[str, int] = defaultdict(int)
    entity_doc_ids: dict[str, set[str]] = defaultdict(set)

    result = await session.run(_ENTITY_CYPHER, corpus_id=corpus_id)
    async for rec in result:
        eid = rec["entity_id"]
        if not eid:
            continue
        # Prefer the coalesced `name` from the Cypher; if all the name fields
        # are null on a node, derive a readable name from the slugified
        # entity_id suffix ("concept:real-user-data" → "real user data") so
        # the node still shows up in Mission Control rather than being
        # silently dropped.
        name = rec.get("name")
        if not name:
            slug = eid.split(":", 1)[-1] if ":" in eid else eid
            name = slug.replace("-", " ").replace("_", " ").strip() or eid
        entity_to_name[eid] = name
        entity_to_type[eid] = rec.get("entity_type") or "other"
        facets = {
            "object_kind": rec.get("object_kind"),
            "object_kind_parent": rec.get("object_kind_parent"),
            "object_kind_root": rec.get("object_kind_root"),
            "domain_type": rec.get("domain_type"),
            "domain_type_parent": rec.get("domain_type_parent"),
            "domain_type_root": rec.get("domain_type_root"),
            "canonical_family": rec.get("canonical_family"),
            "observed_entity_types": rec.get("observed_entity_types"),
            "ontology_version": rec.get("ontology_version"),
        }
        entity_to_facets[eid] = {k: v for k, v in facets.items() if v}
        doc_id = rec["doc_id"]
        mentions = int(rec.get("mentions", 1) or 1)
        entity_mention_counts[eid] += mentions
        if doc_id:
            entity_doc_ids[eid].add(str(doc_id))
        domain = doc_to_domain.get(doc_id)
        if domain:
            entity_to_domains[eid][domain] += mentions
    entity_doc_counts = {eid: len(doc_ids) for eid, doc_ids in entity_doc_ids.items()}
    return (
        entity_to_name,
        entity_to_type,
        entity_to_facets,
        entity_to_domains,
        dict(entity_mention_counts),
        entity_doc_counts,
    )


def _resolve_entity_domains(
    entity_ids: Iterable[str],
    entity_to_domains: dict[str, Counter],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Collapse per-entity domain-count Counters into (primary, touched).
    Entities with no observed mentions fall back to 'unknown'."""
    primary: dict[str, str] = {}
    touched: dict[str, list[str]] = {}
    for eid in entity_ids:
        counts = entity_to_domains.get(eid)
        if counts:
            primary[eid] = counts.most_common(1)[0][0]
            touched[eid] = [d for d, _ in counts.most_common()]
        else:
            primary[eid] = "unknown"
            touched[eid] = []
    return primary, touched


async def _load_relations_into_graph(
    session, G, entity_ids: list[str], corpus_id: str
) -> None:
    """Pull RELATES_TO edges and add them to the graph, folding repeat
    relations between the same pair into a weight increment."""
    result = await session.run(
        _RELATION_CYPHER, entity_ids=entity_ids, corpus_id=corpus_id
    )
    async for rec in result:
        s, t = rec["source"], rec["target"]
        if s == t:
            continue
        if G.has_edge(s, t):
            G[s][t]["weight"] = G[s][t].get("weight", 1) + 1
            family = rec.get("relation_family") or "WeakAssociation"
            families = G[s][t].setdefault("relation_families", [])
            if family not in families:
                families.append(family)
            strength = rec.get("edge_strength") or "strong"
            strengths = G[s][t].setdefault("edge_strengths", [])
            if strength not in strengths:
                strengths.append(strength)
            G[s][t]["eligible_for_synthesis"] = bool(
                G[s][t].get("eligible_for_synthesis")
                or rec.get("eligible_for_synthesis")
            )
        else:
            G.add_edge(
                s, t,
                predicate=rec.get("predicate") or "related_to",
                relation_family=rec.get("relation_family") or "WeakAssociation",
                relation_families=[rec.get("relation_family") or "WeakAssociation"],
                edge_strength=rec.get("edge_strength") or "strong",
                edge_strengths=[rec.get("edge_strength") or "strong"],
                eligible_for_synthesis=bool(rec.get("eligible_for_synthesis")),
                weight=1,
            )


async def load_graph_from_neo4j(
    neo4j_driver,
    corpus_id: str,
    domain_map: DomainMap,
) -> tuple[Any, dict[str, str], dict[str, list[str]]]:
    """Pull entities + relations from Neo4j into a NetworkX graph.

    Returns:
        (G, entity_to_primary_domain, entity_to_domains_touched)
    """
    import networkx as nx

    G = nx.Graph()
    if neo4j_driver is None:
        return G, {}, {}

    doc_to_domain = {
        doc_id: info.get("cluster_name", f"Cluster {info.get('cluster_id', 0)}")
        for doc_id, info in domain_map.doc_assignments.items()
    }

    try:
        async with neo4j_driver.session() as session:
            (
                names,
                types,
                facets,
                domain_counts,
                mention_counts,
                doc_counts,
            ) = await _load_entities_with_mentions(session, corpus_id, doc_to_domain)
            if not names:
                return G, {}, {}

            primary, touched = _resolve_entity_domains(names.keys(), domain_counts)
            for eid, name in names.items():
                G.add_node(
                    eid,
                    canonical_name=name,
                    entity_type=types.get(eid, "other"),
                    **facets.get(eid, {}),
                    domain=primary[eid],
                    domains_touched=touched[eid],
                    mention_count=int(mention_counts.get(eid, 0) or 0),
                    doc_count=int(doc_counts.get(eid, 0) or 0),
                )

            await _load_relations_into_graph(session, G, list(names.keys()), corpus_id)

    except Exception as exc:
        logger.warning("Neo4j graph load failed: %s — returning empty graph", exc)
        return nx.Graph(), {}, {}

    return G, primary, touched


# ── Core metrics ────────────────────────────────────────────────────────────

def compute_pagerank(G) -> dict[str, float]:
    import networkx as nx
    if G.number_of_nodes() == 0:
        return {}
    try:
        return nx.pagerank(G, alpha=0.85, max_iter=100)
    except Exception as exc:
        logger.warning("PageRank failed: %s", exc)
        return {n: 0.0 for n in G.nodes}


def compute_betweenness(G) -> dict[str, float]:
    """Compute cached bridge centrality for sitrep cards.

    This runs only during metrics warmup. For larger graphs we switch to
    NetworkX's sampled approximation so the cache job stays bounded; query
    time only reads the resulting scores.
    """
    import math
    import networkx as nx

    n_nodes = G.number_of_nodes()
    if n_nodes < 2:
        return {}
    try:
        kwargs: dict[str, Any] = {"normalized": True, "seed": 42}
        if n_nodes > 1500:
            kwargs["k"] = min(300, max(50, int(math.sqrt(n_nodes) * 4)))
        scores = nx.betweenness_centrality(G, **kwargs)
        return {str(n): round(float(score or 0.0), 8) for n, score in scores.items()}
    except Exception as exc:
        logger.warning("Betweenness failed: %s", exc)
        return {str(n): 0.0 for n in G.nodes}


def compute_cross_domain_edge_pct(G, node_to_domain: dict[str, str]) -> float:
    if G.number_of_edges() == 0:
        return 0.0
    cross = 0
    for u, v in G.edges:
        du = node_to_domain.get(u)
        dv = node_to_domain.get(v)
        if du and dv and du != dv:
            cross += 1
    return round(100.0 * cross / G.number_of_edges(), 2)


def compute_per_domain_edge_counts(
    G, node_to_domain: dict[str, str]
) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"internal": 0, "external": 0})
    for u, v in G.edges:
        du = node_to_domain.get(u, "unknown")
        dv = node_to_domain.get(v, "unknown")
        if du == dv:
            counts[du]["internal"] += 1
        else:
            counts[du]["external"] += 1
            counts[dv]["external"] += 1
    return dict(counts)


def compute_relation_family_counts(G) -> dict[str, int]:
    """Count relation-family coverage over graph edges.

    A pair can carry multiple extracted predicates; when the loader folds those
    into one NetworkX edge, `relation_families` keeps the distinct families so
    Mission Control can tell whether a query rests on strong structural /
    operational edges or mostly weak associations.
    """
    counts: Counter[str] = Counter()
    for _, _, attrs in G.edges(data=True):
        families = attrs.get("relation_families") or [attrs.get("relation_family")]
        for family in families:
            counts[str(family or "WeakAssociation")] += 1
    return dict(counts)


def compute_domain_density(G, node_to_domain: dict[str, str]) -> dict[str, float]:
    import networkx as nx
    by_domain: dict[str, list[str]] = defaultdict(list)
    for node, domain in node_to_domain.items():
        by_domain[domain].append(node)
    densities: dict[str, float] = {}
    for domain, nodes in by_domain.items():
        if len(nodes) < 2:
            densities[domain] = 0.0
            continue
        subG = G.subgraph(nodes)
        densities[domain] = round(nx.density(subG), 5)
    return densities


def compute_cd_pagerank(
    pagerank: dict[str, float],
    node_domains_touched: dict[str, list[str]],
) -> dict[str, float]:
    """CD-PR = PageRank × distinct domains touched. Rewards hubs that span
    multiple domains more than single-domain celebrities."""
    return {
        node: score * max(1, len(node_domains_touched.get(node, [])))
        for node, score in pagerank.items()
    }


# ── Concept communities ────────────────────────────────────────────────────

def _node_entity_type(G, node: str) -> str:
    return str(G.nodes[node].get("entity_type") or "").strip().lower()


def _node_name(G, node: str) -> str:
    return str(G.nodes[node].get("canonical_name") or node).strip()


def _is_numeric_or_date_label(value: str) -> bool:
    label = value.strip().lower()
    if not label:
        return True
    if re.fullmatch(r"\d{1,4}", label):
        return True
    if re.fullmatch(r"\d{1,4}[-/]\d{1,2}(?:[-/]\d{1,2})?", label):
        return True
    if re.fullmatch(r"(19|20)\d{2}[a-z]?", label):
        return True
    return False


def is_noise_entity_node(G, node: str) -> bool:
    """Nodes useful for provenance but harmful as insight candidates."""
    entity_type = _node_entity_type(G, node)
    if entity_type in NOISE_ENTITY_TYPES:
        return True
    entity_id = str(node).lower()
    if entity_id.startswith(NOISE_ENTITY_ID_PREFIXES):
        return True
    return _is_numeric_or_date_label(_node_name(G, node))


def build_insight_graph(G):
    """Return a copy of G with provenance/date nodes removed.

    Corpus-level metrics still use the raw graph. Detectors use this graph so
    cards stop surfacing document-title/date analogies unless the query anchors
    explicitly point at a document.
    """
    H = G.copy()
    H.remove_nodes_from([n for n in list(H.nodes) if is_noise_entity_node(H, n)])
    return H


def _concept_label_from_nodes(G, ranked_nodes: list[str]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for node in ranked_nodes:
        label = re.sub(r"\s+", " ", _node_name(G, node)).strip(" -:;,.")
        label = re.sub(r"\.(docx?|pdf|md|txt|html?)$", "", label, flags=re.I)
        if not label or _is_numeric_or_date_label(label):
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(label)
        if len(" ".join(parts).split()) >= 4 or len(parts) >= 3:
            break
    if not parts:
        return "Concept Neighborhood"
    # Fallback labels must still read like labels, not dumped enumerations.
    # The LLM can later improve them, but the deterministic cache should never
    # poison downstream prose with slash-joined concept lists.
    words = " ".join(parts).split()[:4]
    return " ".join(words)


def compute_concept_communities(
    G,
    pagerank: dict[str, float],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Louvain concept neighborhoods over the entity graph.

    These are graph-level communities, distinct from document-domain clusters.
    They give Mission Control a local topology lens so a small corpus with two
    broad document domains can still expose many meaningful concept regions.
    """
    import networkx as nx

    H = build_insight_graph(G)
    H.remove_nodes_from([n for n, degree in list(H.degree()) if degree == 0])
    if H.number_of_nodes() == 0:
        return [], {}

    try:
        raw_communities = list(
            nx.community.louvain_communities(
                H, seed=42, resolution=1.0, weight="weight"
            )
        )
    except Exception as exc:
        logger.warning(
            "Concept community Louvain failed (%s) — connected-component fallback",
            exc,
        )
        raw_communities = [set(c) for c in nx.connected_components(H)]

    raw_communities = [set(c) for c in raw_communities if c]
    node_to_raw: dict[str, int] = {}
    for idx, members in enumerate(raw_communities):
        for node in members:
            node_to_raw[node] = idx

    bridge_counts: Counter[int] = Counter()
    for u, v in H.edges:
        cu = node_to_raw.get(u)
        cv = node_to_raw.get(v)
        if cu is not None and cv is not None and cu != cv:
            bridge_counts[cu] += 1
            bridge_counts[cv] += 1

    ranked_communities: list[tuple[float, int, int, set[str]]] = []
    for idx, members in enumerate(raw_communities):
        pr_sum = sum(pagerank.get(n, 0.0) for n in members)
        ranked_communities.append((pr_sum, len(members), idx, members))
    ranked_communities.sort(key=lambda item: (item[0], item[1]), reverse=True)

    summaries: list[dict[str, Any]] = []
    entity_map: dict[str, dict[str, Any]] = {}

    for rank, (pr_sum, size, raw_idx, members) in enumerate(ranked_communities):
        concept_id = f"c{rank}"
        ranked_nodes = sorted(
            members,
            key=lambda n: (
                pagerank.get(n, 0.0),
                int(G.nodes[n].get("mention_count") or 0),
                H.degree(n),
            ),
            reverse=True,
        )
        top_nodes = ranked_nodes[:CONCEPT_COMMUNITY_TOP_ENTITIES]
        top_entities = [_node_name(G, n) for n in top_nodes]
        label = _concept_label_from_nodes(G, top_nodes)
        summary = {
            "concept_id": concept_id,
            "label": label,
            "size": size,
            "top_entities": top_entities,
            "top_entity_ids": top_nodes,
            "member_ids": sorted(members),
            "pagerank_sum": round(pr_sum, 6),
            "bridge_count": int(bridge_counts.get(raw_idx, 0)),
        }
        summaries.append(summary)
        for node in members:
            entity_map[node] = {
                "concept_id": concept_id,
                "label": label,
                "top_entities": top_entities[:5],
                "top_entity_ids": top_nodes[:5],
                "pagerank": round(pagerank.get(node, 0.0), 8),
                "degree": int(H.degree(node)),
                "bridge_count": int(bridge_counts.get(raw_idx, 0)),
            }

    logger.info(
        "Concept communities computed: nodes=%d edges=%d communities=%d",
        H.number_of_nodes(), H.number_of_edges(), len(summaries),
    )
    return summaries, entity_map


def public_concept_community(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "concept_id": str(summary.get("concept_id", "")),
        "label": str(summary.get("label", "Concept Neighborhood")),
        "size": int(summary.get("size") or 0),
        "top_entities": list(summary.get("top_entities") or [])[:5],
        "bridge_count": int(summary.get("bridge_count") or 0),
        "scope_count": int(summary.get("scope_count") or 0),
    }


def relevant_concept_communities(
    metrics: "CorpusMetrics",
    entity_ids: Iterable[str],
    *,
    limit: int = CONCEPT_COMMUNITY_RESPONSE_LIMIT,
) -> list[dict[str, Any]]:
    scope = set(entity_ids or [])
    if not metrics.concept_communities:
        return []
    concept_scope_counts: Counter[str] = Counter()
    for eid in scope:
        concept = metrics.entity_concept_map.get(eid)
        if concept:
            concept_scope_counts[str(concept.get("concept_id"))] += 1

    ranked: list[dict[str, Any]] = []
    for concept in metrics.concept_communities:
        copied = dict(concept)
        copied["scope_count"] = concept_scope_counts.get(
            str(concept.get("concept_id")), 0
        )
        ranked.append(copied)

    ranked.sort(
        key=lambda c: (
            int(c.get("scope_count") or 0),
            float(c.get("pagerank_sum") or 0.0),
            int(c.get("bridge_count") or 0),
            int(c.get("size") or 0),
        ),
        reverse=True,
    )
    return [public_concept_community(c) for c in ranked[:limit]]


def entity_concept_subset(
    metrics: "CorpusMetrics",
    entity_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for eid in entity_ids:
        concept = metrics.entity_concept_map.get(eid)
        if not concept:
            continue
        out[eid] = {
            "concept_id": str(concept.get("concept_id", "")),
            "label": str(concept.get("label", "Concept Neighborhood")),
            "top_entities": list(concept.get("top_entities") or [])[:5],
        }
    return out


def _concept_semantic_tokens(concept: dict[str, Any]) -> set[str]:
    text = " ".join(
        [
            str(concept.get("label") or ""),
            " ".join(str(v) for v in (concept.get("top_entities") or [])),
        ]
    )
    return _anchor_tokens(text)


_GAP_GENERIC_TOKENS = {
    "app",
    "application",
    "based",
    "chart",
    "code",
    "data",
    "design",
    "document",
    "example",
    "file",
    "graph",
    "image",
    "kit",
    "library",
    "method",
    "model",
    "module",
    "object",
    "option",
    "process",
    "project",
    "result",
    "service",
    "strategy",
    "support",
    "system",
    "tool",
    "user",
    "view",
}


def _concept_member_facets(G, members: set[str], key: str) -> set[str]:
    values: set[str] = set()
    for node in members:
        if node not in G:
            continue
        value = str(G.nodes[node].get(key) or "").strip().lower()
        if value and value not in {"unknown", "none", "null"}:
            values.add(value)
    return values


def _concept_gap_is_coherent(
    G,
    a_members: set[str],
    b_members: set[str],
    a_tokens: set[str],
    b_tokens: set[str],
    lexical_similarity: float,
    shared_neighbor_jaccard: float,
    shared_neighbor_names: list[str],
) -> tuple[bool, dict[str, Any]]:
    """Reject false-positive gaps between unrelated corpus regions.

    A polymath corpus can have mobile-development and finance clusters in the
    same graph. Bridge centrality alone should not make those a "gap." Keep a
    pair only when it has a meaningful lexical/facet/domain reason to compare.
    """
    overlap = sorted((a_tokens & b_tokens) - _GAP_GENERIC_TOKENS)
    a_families = _concept_member_facets(G, a_members, "canonical_family")
    b_families = _concept_member_facets(G, b_members, "canonical_family")
    family_overlap = sorted(a_families & b_families)
    a_domain_types = _concept_member_facets(G, a_members, "domain_type")
    b_domain_types = _concept_member_facets(G, b_members, "domain_type")
    domain_type_overlap = sorted(a_domain_types & b_domain_types)
    a_domains = _concept_member_facets(G, a_members, "domain")
    b_domains = _concept_member_facets(G, b_members, "domain")
    domain_overlap = sorted(a_domains & b_domains)

    coherent = bool(
        overlap
        or family_overlap
        or domain_type_overlap
        or (shared_neighbor_jaccard >= 0.04 and shared_neighbor_names)
    )
    # Same document-domain alone is weak, especially when a corpus has only one
    # broad cluster. Allow it only with some lexical similarity beyond stopwords.
    if not coherent and domain_overlap and lexical_similarity >= 0.14:
        coherent = True

    return coherent, {
        "shared_terms": overlap[:5],
        "shared_families": family_overlap[:5],
        "shared_domain_types": domain_type_overlap[:5],
        "shared_domains": domain_overlap[:5],
        "shared_neighbors": shared_neighbor_names[:6],
        "shared_neighbor_jaccard": round(float(shared_neighbor_jaccard), 4),
    }


def _community_external_neighbors(G, members: set[str], other: set[str]) -> set[str]:
    neighbors: set[str] = set()
    banned = members | other
    for node in members:
        if node not in G:
            continue
        for neighbor in G.neighbors(node):
            if neighbor not in banned and not is_noise_entity_node(G, neighbor):
                neighbors.add(neighbor)
    return neighbors


def compute_cluster_pair_gaps(
    G,
    concept_communities: list[dict[str, Any]],
    *,
    limit: int = INSIGHT_TOP_K * 2,
) -> list[dict[str, Any]]:
    """Find concept-community pairs that are semantically close but sparsely linked.

    The expensive community detection has already happened. This detector only
    compares cached community summaries and direct inter-community edge density,
    making the resulting records safe to read during graph-query turns.
    """
    if not concept_communities or G.number_of_edges() == 0:
        return []

    ranked = sorted(
        concept_communities,
        key=lambda c: (
            float(c.get("pagerank_sum") or 0.0),
            int(c.get("size") or 0),
            int(c.get("bridge_count") or 0),
        ),
        reverse=True,
    )[:48]
    scored: list[dict[str, Any]] = []

    for i, a in enumerate(ranked):
        a_members = set(a.get("member_ids") or [])
        if not a_members:
            continue
        a_tokens = _concept_semantic_tokens(a)
        for b in ranked[i + 1:]:
            b_members = set(b.get("member_ids") or [])
            if not b_members:
                continue
            b_tokens = _concept_semantic_tokens(b)
            token_union = a_tokens | b_tokens
            lexical_similarity = (
                len(a_tokens & b_tokens) / len(token_union)
                if token_union else 0.0
            )
            a_neighbors = _community_external_neighbors(G, a_members, b_members)
            b_neighbors = _community_external_neighbors(G, b_members, a_members)
            shared_neighbor_nodes = a_neighbors & b_neighbors
            neighbor_union = a_neighbors | b_neighbors
            shared_neighbor_jaccard = (
                len(shared_neighbor_nodes) / len(neighbor_union)
                if neighbor_union else 0.0
            )
            shared_neighbor_names = [
                _node_name(G, node)
                for node in sorted(
                    shared_neighbor_nodes,
                    key=lambda n: (
                        int(G.nodes[n].get("mention_count") or 0),
                        G.degree(n),
                    ),
                    reverse=True,
                )
                if _node_name(G, node).lower() not in _GAP_GENERIC_TOKENS
            ][:8]
            coherent, coherence = _concept_gap_is_coherent(
                G,
                a_members,
                b_members,
                a_tokens,
                b_tokens,
                lexical_similarity,
                shared_neighbor_jaccard,
                shared_neighbor_names,
            )
            if not coherent:
                continue
            # A small bridge-count prior keeps structurally central communities
            # from being missed when labels use different vocabulary, but only
            # after coherence says the two neighborhoods are meaningfully
            # comparable. This prevents ML-library ↔ finance false positives.
            bridge_prior = min(
                0.16,
                (int(a.get("bridge_count") or 0) + int(b.get("bridge_count") or 0)) / 220.0,
            )
            facet_boost = 0.06 if (
                coherence["shared_families"] or coherence["shared_domain_types"]
            ) else 0.0
            semantic_similarity = max(lexical_similarity + facet_boost, bridge_prior)
            semantic_similarity = max(semantic_similarity, shared_neighbor_jaccard * 1.4)
            if semantic_similarity < 0.08:
                continue

            inter_edges = 0
            for u in a_members:
                if u not in G:
                    continue
                for v in G.neighbors(u):
                    if v in b_members:
                        inter_edges += 1
            possible = max(1, len(a_members) * len(b_members))
            density = inter_edges / possible
            expected = min(1.0, semantic_similarity)
            gap_score = max(0.0, expected - min(1.0, density * 8.0))
            if gap_score <= 0.04:
                continue

            scored.append({
                "cluster_a": str(a.get("concept_id") or ""),
                "cluster_b": str(b.get("concept_id") or ""),
                "cluster_a_label": str(a.get("label") or "Concept Neighborhood"),
                "cluster_b_label": str(b.get("label") or "Concept Neighborhood"),
                "semantic_similarity": round(float(semantic_similarity), 4),
                "structural_connectivity": round(float(density), 6),
                "expected_connectivity": round(float(expected), 4),
                "gap_score": round(float(gap_score), 4),
                "edge_count": int(inter_edges),
                "coherence": coherence,
                "anchor_concepts": (
                    list(a.get("top_entities") or [])[:3]
                    + list(b.get("top_entities") or [])[:3]
                )[:6],
            })

    scored.sort(
        key=lambda row: (
            float(row.get("gap_score") or 0.0),
            float(row.get("semantic_similarity") or 0.0),
        ),
        reverse=True,
    )
    return scored[:limit]


def detect_latent_topics(
    G,
    pagerank: dict[str, float],
    betweenness: dict[str, float],
    node_primary: dict[str, str],
    *,
    limit: int = INSIGHT_TOP_K * 2,
) -> list[dict[str, Any]]:
    """Surface mentioned-but-underintegrated concepts for the sitrep.

    Latent topics are not "important hubs"; they are entities with evidence
    spread across the corpus but relatively little structural integration.
    """
    if G.number_of_nodes() == 0:
        return []

    scored: list[dict[str, Any]] = []
    for node in G.nodes:
        if is_noise_entity_node(G, node):
            continue
        degree = int(G.degree(node))
        mentions = int(G.nodes[node].get("mention_count") or 0)
        doc_count = int(G.nodes[node].get("doc_count") or 0)
        if mentions < 2 and doc_count < 2:
            continue
        pr = float(pagerank.get(node, 0.0) or 0.0)
        btw = float(betweenness.get(node, 0.0) or 0.0)
        integration_penalty = 1.0 + (degree * 0.22) + (btw * 10.0)
        evidence_weight = np.log1p(max(0, mentions)) + (doc_count * 0.35) + (pr * 50.0)
        latent_score = evidence_weight / integration_penalty
        if latent_score <= 0:
            continue
        scored.append({
            "entity_id": str(node),
            "canonical_name": _node_name(G, node),
            "domain": node_primary.get(node, "unknown"),
            "mention_count": mentions,
            "doc_count": doc_count,
            "degree": degree,
            "pagerank": round(pr, 8),
            "betweenness": round(btw, 8),
            "latent_score": round(float(latent_score), 4),
            "rationale": (
                "Mentioned across the corpus but only lightly connected to "
                "nearby concepts."
            ),
        })

    scored.sort(
        key=lambda row: (
            float(row.get("latent_score") or 0.0),
            int(row.get("doc_count") or 0),
            int(row.get("mention_count") or 0),
        ),
        reverse=True,
    )
    return scored[:limit]


def build_entity_facet_map(G) -> dict[str, dict[str, Any]]:
    """Collect object-kind ontology facets from graph node properties."""
    out: dict[str, dict[str, Any]] = {}
    for eid, attrs in G.nodes(data=True):
        facets = {
            "object_kind": attrs.get("object_kind"),
            "object_kind_parent": attrs.get("object_kind_parent"),
            "object_kind_root": attrs.get("object_kind_root"),
            "domain_type": attrs.get("domain_type"),
            "domain_type_parent": attrs.get("domain_type_parent"),
            "domain_type_root": attrs.get("domain_type_root"),
            "canonical_family": attrs.get("canonical_family"),
            "ontology_version": attrs.get("ontology_version"),
        }
        compact = {k: v for k, v in facets.items() if v}
        if (
            compact.get("object_kind")
            or compact.get("domain_type")
            or compact.get("canonical_family")
        ):
            out[eid] = compact
    return out


def resolve_metrics_ontology_version(G) -> str:
    """Return the dominant ontology version represented in this corpus graph."""
    versions = Counter(
        str(attrs.get("ontology_version"))
        for _, attrs in G.nodes(data=True)
        if attrs.get("ontology_version")
    )
    return versions.most_common(1)[0][0] if versions else ""


def expand_scope_with_concepts(
    metrics: "CorpusMetrics",
    scope: set[str],
    *,
    entity_cap: int = CONCEPT_SCOPE_ENTITY_CAP,
) -> set[str]:
    """Expand query seeds with high-signal peers from the same neighborhoods."""
    if not scope or not metrics.entity_concept_map or not metrics.concept_communities:
        return scope

    concept_by_id = {
        str(c.get("concept_id")): c for c in metrics.concept_communities
    }
    concept_ids = {
        str(concept.get("concept_id"))
        for eid in scope
        if (concept := metrics.entity_concept_map.get(eid))
    }
    if not concept_ids:
        return scope

    optional_scores: dict[str, float] = {}
    for concept_id in concept_ids:
        community = concept_by_id.get(concept_id)
        if not community:
            continue
        members = community.get("member_ids") or []
        ranked_members = sorted(
            members,
            key=lambda eid: (
                float(metrics.entity_concept_map.get(eid, {}).get("pagerank") or 0.0),
                int(metrics.entity_concept_map.get(eid, {}).get("bridge_count") or 0),
                int(metrics.entity_concept_map.get(eid, {}).get("degree") or 0),
            ),
            reverse=True,
        )[:CONCEPT_PEERS_PER_COMMUNITY]
        for eid in ranked_members:
            if eid in scope:
                continue
            concept = metrics.entity_concept_map.get(eid, {})
            optional_scores[eid] = max(
                optional_scores.get(eid, 0.0),
                float(concept.get("pagerank") or 0.0) * 1000.0
                + int(concept.get("bridge_count") or 0) * 0.5
                + int(concept.get("degree") or 0) * 0.05,
            )

    required = list(scope)
    room = max(0, entity_cap - len(required))
    optional = [
        eid for eid, _ in sorted(
            optional_scores.items(), key=lambda item: item[1], reverse=True
        )[:room]
    ]
    expanded = set(required) | set(optional)
    logger.info(
        "concept_scope_expand: seeds=%d concepts=%d expanded=%d cap=%d",
        len(scope), len(concept_ids), len(expanded), entity_cap,
    )
    return expanded


def _expanded_query_concept_tokens(query: str) -> tuple[set[str], set[str]]:
    normalized_tokens = {
        t for t in _normalize_anchor_text(query).split()
        if t and t not in _STOPWORDS
    }
    direct = _anchor_tokens(query) | (normalized_tokens & SHORT_QUERY_CONCEPT_TOKENS)
    expanded = set(direct)
    for token in list(direct):
        expanded.update(QUERY_CONCEPT_EXPANSIONS.get(token, set()))
    query_norm = _normalize_anchor_text(query)
    if ("gen" in normalized_tokens and "ai" in normalized_tokens) or "generative ai" in query_norm:
        expanded.update(QUERY_CONCEPT_EXPANSIONS["genai"])
    return direct, expanded


def query_matched_concept_communities(
    metrics: "CorpusMetrics",
    query: str,
    *,
    limit: int = QUERY_CONCEPT_MATCH_LIMIT,
) -> list[dict[str, Any]]:
    """Lexically match the query against concept-neighborhood labels.

    Vector recall is good for prose, but short phrases like "generative AI"
    need this explicit topology vocabulary match so Mission Control reels in
    neighborhoods such as generative design, neural networks, classifiers, and
    genetic algorithms even when the rest of the query points elsewhere.
    """
    direct_tokens, expanded_tokens = _expanded_query_concept_tokens(query)
    if not expanded_tokens or not metrics.concept_communities:
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    query_norm = _normalize_anchor_text(query)
    for concept in metrics.concept_communities:
        text = " ".join(
            [
                str(concept.get("label") or ""),
                " ".join(concept.get("top_entities") or []),
            ]
        )
        concept_tokens = _anchor_tokens(text)
        if not concept_tokens:
            continue
        direct_overlap = direct_tokens & concept_tokens
        expanded_overlap = expanded_tokens & concept_tokens
        if not direct_overlap and len(expanded_overlap) < 2:
            continue

        label_norm = _normalize_anchor_text(str(concept.get("label") or ""))
        phrase_bonus = 1.5 if label_norm and label_norm in query_norm else 0.0
        score = (
            len(direct_overlap) * 3.0
            + len(expanded_overlap) * 1.0
            + phrase_bonus
            + float(concept.get("pagerank_sum") or 0.0) * 5.0
            + min(1.0, int(concept.get("bridge_count") or 0) / 50.0)
        )
        scored.append((score, concept))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [concept for _, concept in scored[:limit]]


def expand_scope_with_query_concepts(
    metrics: "CorpusMetrics",
    scope: set[str],
    query: str,
    *,
    entity_cap: int = CONCEPT_SCOPE_ENTITY_CAP,
) -> set[str]:
    matched = query_matched_concept_communities(metrics, query)
    if not matched:
        return scope

    ranked_by_concept: list[list[str]] = []
    for concept in matched:
        members = concept.get("member_ids") or []
        scored_members: list[tuple[float, str]] = []
        for eid in members:
            if eid in scope:
                continue
            entity_concept = metrics.entity_concept_map.get(eid, {})
            score = (
                float(entity_concept.get("pagerank") or 0.0) * 1000.0
                + int(entity_concept.get("bridge_count") or 0) * 0.5
                + int(entity_concept.get("degree") or 0) * 0.05
            )
            scored_members.append((score, eid))
        scored_members.sort(key=lambda item: item[0], reverse=True)
        ranked_by_concept.append([eid for _, eid in scored_members])

    required = set(scope)
    room = max(0, entity_cap - len(required))
    selected: list[str] = []
    seen = set(required)
    cursor = 0
    while len(selected) < room and any(cursor < len(items) for items in ranked_by_concept):
        for items in ranked_by_concept:
            if len(selected) >= room:
                break
            if cursor >= len(items):
                continue
            eid = items[cursor]
            if eid in seen:
                continue
            seen.add(eid)
            selected.append(eid)
        cursor += 1

    expanded = required | set(selected)
    logger.info(
        "query_concept_scope_expand: query=%r matched=%s seeds=%d expanded=%d cap=%d",
        query[:60],
        [c.get("label") for c in matched],
        len(scope),
        len(expanded),
        entity_cap,
    )
    return expanded


def expand_scope_with_query_facets(
    metrics: "CorpusMetrics",
    scope: set[str],
    query: str,
    *,
    entity_cap: int = CONCEPT_SCOPE_ENTITY_CAP,
) -> set[str]:
    """Expand query scope with entities matching object-kind facet language.

    This is intentionally cheap: the expensive ontology enrichment happens at
    ingestion/write time, and this function only matches query tokens against
    cached facet properties such as Library, App, Report, and Dataset.
    """
    if not metrics.entity_facet_map:
        return scope
    query_tokens = {
        t for t in _normalize_anchor_text(query).split()
        if t and t not in _STOPWORDS
    }
    query_norm = _normalize_anchor_text(query)
    wanted_kinds: set[str] = set()
    wanted_families: set[str] = set()
    wanted_domain_types: set[str] = set()
    for token in query_tokens:
        wanted_kinds.update(QUERY_OBJECT_KIND_EXPANSIONS.get(token, set()))
        wanted_families.update(QUERY_CANONICAL_FAMILY_EXPANSIONS.get(token, set()))
        wanted_domain_types.update(QUERY_DOMAIN_TYPE_EXPANSIONS.get(token, set()))
    if "generative ai" in query_norm or ("gen" in query_tokens and "ai" in query_tokens):
        wanted_families.add("generative_ai")
    if "user information" in query_norm or "user data" in query_norm:
        wanted_families.add("identity_extraction")
    if "physics simulation" in query_norm:
        wanted_families.add("physics_simulation")
    if "book json" in query_norm:
        wanted_domain_types.add("DataObject")
        wanted_families.add("book_generation")
    if "me book" in query_norm or "prose book" in query_norm:
        wanted_domain_types.add("OutputArtifact")
        wanted_families.add("book_generation")
    if "the council" in query_norm or "message limit" in query_norm or "gate c" in query_norm:
        wanted_domain_types.update({"Feature", "Constraint"})
        wanted_families.add("council_chat")
    if "on device" in query_norm or "local model" in query_norm or "local llm" in query_norm:
        wanted_domain_types.add("AIModel")
        wanted_families.add("mobile_ai")
    if not wanted_kinds and not wanted_families and not wanted_domain_types:
        return scope

    scored: list[tuple[float, str]] = []
    for eid, facets in metrics.entity_facet_map.items():
        if eid in scope:
            continue
        kind_match = facets.get("object_kind") in wanted_kinds
        family_match = facets.get("canonical_family") in wanted_families
        domain_match = facets.get("domain_type") in wanted_domain_types
        if not kind_match and not family_match and not domain_match:
            continue
        concept = metrics.entity_concept_map.get(eid, {})
        score = (
            float(concept.get("pagerank") or 0.0) * 1000.0
            + int(concept.get("bridge_count") or 0) * 0.5
            + int(concept.get("degree") or 0) * 0.05
            + (0.5 if family_match else 0.0)
            + (0.4 if domain_match else 0.0)
        )
        scored.append((score, eid))

    room = max(0, entity_cap - len(scope))
    selected = [
        eid for _, eid in sorted(scored, key=lambda item: item[0], reverse=True)[:room]
    ]
    expanded = set(scope) | set(selected)
    logger.info(
        "query_facet_scope_expand: query=%r kinds=%s families=%s domain_types=%s seeds=%d expanded=%d cap=%d",
        query[:60], sorted(wanted_kinds), sorted(wanted_families),
        sorted(wanted_domain_types),
        len(scope), len(expanded), entity_cap,
    )
    return expanded


# ── Topology fingerprints ──────────────────────────────────────────────────

def topology_fingerprint(G, node, node_to_domain: dict[str, str]) -> dict:
    """Lightweight structural signature used to find analogies."""
    neighbors = list(G.neighbors(node))
    if not neighbors:
        return {
            "degree": 0,
            "neighbor_degree_profile": [],
            "domain_mix_ratio": 0.0,
            "two_hop_size": 0,
        }
    two_hop: set[str] = set()
    for n in neighbors:
        two_hop.update(G.neighbors(n))
    two_hop.discard(node)
    neighbor_domains = {node_to_domain.get(n) for n in neighbors if node_to_domain.get(n)}
    return {
        "degree": len(neighbors),
        "neighbor_degree_profile": sorted(
            [G.degree(n) for n in neighbors], reverse=True
        )[:10],
        "domain_mix_ratio": round(len(neighbor_domains) / len(neighbors), 3),
        "two_hop_size": len(two_hop),
    }


def topology_similarity(fp_a: dict, fp_b: dict) -> float:
    """0–1 score. Compares degree profile (cosine-like) + domain-mix similarity."""
    prof_a = fp_a.get("neighbor_degree_profile", [])
    prof_b = fp_b.get("neighbor_degree_profile", [])
    if not prof_a or not prof_b:
        return 0.0
    # Pad to common length.
    L = max(len(prof_a), len(prof_b))
    a = np.array(prof_a + [0] * (L - len(prof_a)), dtype=np.float32)
    b = np.array(prof_b + [0] * (L - len(prof_b)), dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    cos = float(np.dot(a, b) / (na * nb))
    # Penalize very different degrees.
    deg_a, deg_b = fp_a["degree"], fp_b["degree"]
    deg_ratio = min(deg_a, deg_b) / max(deg_a, deg_b) if max(deg_a, deg_b) > 0 else 0
    mix_close = 1 - abs(fp_a["domain_mix_ratio"] - fp_b["domain_mix_ratio"])
    return round((cos * 0.6 + deg_ratio * 0.25 + mix_close * 0.15), 3)


def neighbor_jaccard(G, a, b) -> float:
    na = set(G.neighbors(a))
    nb = set(G.neighbors(b))
    if not na and not nb:
        return 0.0
    return round(len(na & nb) / len(na | nb), 3)


# ── Insight detectors ──────────────────────────────────────────────────────

def detect_frontier_nodes(
    G, node_to_domain: dict[str, str], node_domains_touched: dict[str, list[str]],
    degree_max: int = FRONTIER_DEGREE_MAX,
) -> list[dict[str, Any]]:
    """Low-degree nodes whose neighborhood spans ≥2 domains."""
    out: list[dict[str, Any]] = []
    for node in G.nodes:
        degree = G.degree(node)
        if degree == 0 or degree > degree_max:
            continue
        touched = node_domains_touched.get(node, [])
        if len(touched) < 2:
            # Check neighborhood diversity too
            neighbor_domains = {
                node_to_domain.get(n) for n in G.neighbors(node)
                if node_to_domain.get(n)
            }
            if len(neighbor_domains) < 2:
                continue
            touched = list(neighbor_domains)
        out.append({
            "entity_id": node,
            "canonical_name": G.nodes[node].get("canonical_name", node),
            "primary_domain": node_to_domain.get(node, "unknown"),
            "degree": degree,
            "domains_touched": touched,
            "cross_domain_potential": len(touched),
        })
    out.sort(key=lambda x: x["cross_domain_potential"], reverse=True)
    return out[:INSIGHT_TOP_K * 2]  # keep more for query-time filtering


def detect_fragile_bridges(
    G, node_to_domain: dict[str, str]
) -> list[dict[str, Any]]:
    """Cross-domain articulation edges: removing one disconnects a component.

    Uses NetworkX `bridges()` which finds ALL articulation edges in a single
    O(V + E) DFS pass, replacing the former per-edge G.copy() quadratic hot
    path. Returns cross-domain bridges ordered with the most fragile first.
    """
    import networkx as nx

    try:
        all_bridges = list(nx.bridges(G))
    except nx.NetworkXNotImplemented:
        # bridges() requires undirected — we always build undirected graphs
        # but guard anyway.
        return []

    out: list[dict[str, Any]] = []
    for u, v in all_bridges:
        du = node_to_domain.get(u)
        dv = node_to_domain.get(v)
        if not du or not dv or du == dv:
            continue
        source_name = G.nodes[u].get("canonical_name", u)
        target_name = G.nodes[v].get("canonical_name", v)
        out.append({
            "source": u,
            "source_name": source_name,
            "source_domain": du,
            "target": v,
            "target_name": target_name,
            "target_domain": dv,
            "path_count": 1,  # articulation edge → no alternative path
            "path_entity_ids": [u, v],
            "path_entities": [source_name, target_name],
            "evidence": (
                f"Articulation edge: removing {source_name} ↔ {target_name} "
                "disconnects this cross-domain route."
            ),
        })
    return out[: INSIGHT_TOP_K * 2]


def detect_analogies_and_terminological_gaps(
    G, node_to_domain: dict[str, str], fingerprints: dict[str, dict],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cross-domain node pairs with high topology similarity.
    Jaccard(neighbors) > threshold → terminological gap.
    Jaccard(neighbors) ≤ threshold → structural analogy.
    Only considers nodes with degree ≥ 2 to avoid noise.
    """
    analogies: list[dict[str, Any]] = []
    terminological: list[dict[str, Any]] = []
    nodes = [n for n in G.nodes if G.degree(n) >= 2]
    # O(n²) pairs — capped via degree filter and early stopping.
    if len(nodes) > 500:
        # Sample by top-degree to keep this tractable at large graphs.
        nodes.sort(key=lambda n: G.degree(n), reverse=True)
        nodes = nodes[:500]

    seen_pairs: set[tuple[str, str]] = set()
    for i, a in enumerate(nodes):
        fa = fingerprints.get(a)
        if not fa:
            continue
        da = node_to_domain.get(a)
        for b in nodes[i + 1:]:
            db = node_to_domain.get(b)
            if not da or not db or da == db:
                continue
            key = (min(a, b), max(a, b))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            fb = fingerprints.get(b)
            if not fb:
                continue
            sim = topology_similarity(fa, fb)
            if sim < ANALOGY_TOPOLOGY_SIM_MIN:
                continue
            jac = neighbor_jaccard(G, a, b)
            entry = {
                "source": a,
                "source_name": G.nodes[a].get("canonical_name", a),
                "source_domain": da,
                "target": b,
                "target_name": G.nodes[b].get("canonical_name", b),
                "target_domain": db,
                "topology_sim": sim,
                "neighbor_jaccard": jac,
            }
            if jac >= ANALOGY_NEIGHBOR_JACCARD_MIN:
                terminological.append(entry)
            else:
                analogies.append(entry)

    terminological.sort(key=lambda x: (x["topology_sim"], x["neighbor_jaccard"]), reverse=True)
    analogies.sort(key=lambda x: x["topology_sim"], reverse=True)
    return analogies[:INSIGHT_TOP_K], terminological[:INSIGHT_TOP_K]


def detect_transfer_opportunities(
    G,
    node_to_domain: dict[str, str],
    cd_pagerank: dict[str, float],
    analogies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hubs (top CD-PR) with structural analogs in ≥2 different domains."""
    if not cd_pagerank:
        return []
    sorted_nodes = sorted(cd_pagerank.items(), key=lambda kv: kv[1], reverse=True)
    threshold_idx = max(1, len(sorted_nodes) * (100 - TRANSFER_CD_PR_PERCENTILE) // 100)
    top_hubs = {n for n, _ in sorted_nodes[:threshold_idx]}

    # Build analog index: node → list of (other_node, other_domain, sim)
    analog_by_node: dict[str, list[dict]] = defaultdict(list)
    for a in analogies:
        analog_by_node[a["source"]].append({
            "entity": a["target"], "name": a["target_name"],
            "domain": a["target_domain"], "topology_sim": a["topology_sim"],
        })
        analog_by_node[a["target"]].append({
            "entity": a["source"], "name": a["source_name"],
            "domain": a["source_domain"], "topology_sim": a["topology_sim"],
        })

    out: list[dict[str, Any]] = []
    for hub in top_hubs:
        analogs = analog_by_node.get(hub, [])
        # Dedupe by target domain (most similar per domain)
        by_domain: dict[str, dict] = {}
        for a in analogs:
            d = a["domain"]
            if d not in by_domain or a["topology_sim"] > by_domain[d]["topology_sim"]:
                by_domain[d] = a
        hub_domain = node_to_domain.get(hub)
        target_domains = [d for d in by_domain if d != hub_domain]
        if len(target_domains) < 2:
            continue
        out.append({
            "hub": hub,
            "hub_name": G.nodes[hub].get("canonical_name", hub),
            "hub_domain": hub_domain,
            "cd_pagerank": round(cd_pagerank.get(hub, 0.0), 5),
            "target_domains": target_domains,
            "analogs": [by_domain[d] for d in target_domains],
        })
    out.sort(key=lambda x: x["cd_pagerank"], reverse=True)
    return out[:INSIGHT_TOP_K]


# ── Orchestrator + cache ───────────────────────────────────────────────────

async def compute_all_metrics(
    neo4j_driver,
    db,
    corpus_id: str,
    domain_map: DomainMap,
    *,
    force: bool = False,
) -> CorpusMetrics:
    """Compute (or fetch from cache) the full metrics snapshot for a corpus.

    Cached by (corpus_id, corpus_change_signature). Writes to
    `graph_metrics_cache` Mongo collection.
    """
    signature = domain_map.corpus_change_signature

    if not force:
        cached = await db["graph_metrics_cache"].find_one(
            {"corpus_id": corpus_id, "corpus_change_signature": signature}
        )
        if cached and cached.get("schema_version") == METRICS_CACHE_SCHEMA_VERSION:
            logger.info("Metrics cache HIT corpus=%s sig=%s", corpus_id, signature[:8])
            return _deserialize_metrics(cached)
        if cached:
            logger.info(
                "Metrics cache STALE corpus=%s sig=%s schema=%s — recomputing",
                corpus_id, signature[:8], cached.get("schema_version"),
            )

    logger.info(
        "Metrics cache MISS corpus=%s — loading graph + computing metrics",
        corpus_id,
    )

    G, node_primary, node_touched = await load_graph_from_neo4j(
        neo4j_driver, corpus_id, domain_map
    )
    n_nodes = G.number_of_nodes()
    n_edges = G.number_of_edges()

    # If graph is empty (no Neo4j data), return a minimal metrics shell.
    if n_nodes == 0:
        logger.info("Empty graph for corpus=%s — returning minimal metrics", corpus_id)
        metrics = CorpusMetrics(
            corpus_id=corpus_id,
            corpus_change_signature=signature,
            computed_at=datetime.utcnow(),
            node_count=0,
            edge_count=0,
            density=0.0,
            cross_domain_edge_pct=0.0,
            modularity_proxy=0.0,
            domain_density={},
            per_domain_edge_counts={},
            relation_family_counts={},
            top_pagerank=[],
            top_cross_domain_pagerank=[],
            node_domain_map={},
            node_domains_touched={},
            frontier_candidates=[],
            fragile_bridges=[],
            terminological_gaps=[],
            structural_analogies=[],
            transfer_candidates=[],
            entity_name_map={},
            concept_communities=[],
            entity_concept_map={},
            entity_facet_map={},
            ontology_version="",
            entity_betweenness={},
            cluster_pair_gaps=[],
            latent_topics=[],
            metrics_engine="networkx",
        )
        await _cache_metrics(db, metrics)
        return metrics

    import networkx as nx

    pagerank = compute_pagerank(G)
    cd_pagerank = compute_cd_pagerank(pagerank, node_touched)
    concept_communities, entity_concept_map = compute_concept_communities(G, pagerank)
    entity_facet_map = build_entity_facet_map(G)
    entity_name_map = {
        n: str(G.nodes[n].get("canonical_name") or n)
        for n in G.nodes
    }
    ontology_version = resolve_metrics_ontology_version(G)
    cross_pct = compute_cross_domain_edge_pct(G, node_primary)
    per_domain = compute_per_domain_edge_counts(G, node_primary)
    relation_families = compute_relation_family_counts(G)
    dom_density = compute_domain_density(G, node_primary)
    modularity_proxy = round(
        1 - (cross_pct / 100.0) if cross_pct else 1.0, 3
    )

    insight_G = build_insight_graph(G)
    insight_node_primary = {
        n: node_primary.get(n, "unknown") for n in insight_G.nodes
    }
    insight_node_touched = {
        n: node_touched.get(n, []) for n in insight_G.nodes
    }
    insight_cd_pagerank = {
        n: cd_pagerank.get(n, 0.0) for n in insight_G.nodes
    }
    betweenness = compute_betweenness(insight_G)
    for eid, concept in entity_concept_map.items():
        concept["betweenness"] = round(float(betweenness.get(eid, 0.0) or 0.0), 8)

    # Topology fingerprints per non-noisy insight node.
    fingerprints = {
        n: topology_fingerprint(insight_G, n, insight_node_primary)
        for n in insight_G.nodes
    }

    # Insight detectors. Fragile-bridge detector is O(V+E) now, so no edge cap.
    frontier = detect_frontier_nodes(
        insight_G, insight_node_primary, insight_node_touched
    )
    fragile = detect_fragile_bridges(insight_G, insight_node_primary)
    analogies, terminological = detect_analogies_and_terminological_gaps(
        insight_G, insight_node_primary, fingerprints
    )
    transfers = detect_transfer_opportunities(
        insight_G, insight_node_primary, insight_cd_pagerank, analogies
    )
    cluster_pair_gaps = compute_cluster_pair_gaps(insight_G, concept_communities)
    latent_topics = detect_latent_topics(
        insight_G, pagerank, betweenness, insight_node_primary
    )

    # Top-N convenience lists (for inspection / metrics card).
    def _top_list(scores: dict[str, float], k: int = 10) -> list[dict]:
        ranked = [
            (n, s)
            for n, s in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            if n in G and not is_noise_entity_node(G, n)
        ][:k]
        return [
            {
                "entity_id": n,
                "canonical_name": G.nodes[n].get("canonical_name", n),
                "domain": node_primary.get(n, "unknown"),
                "score": round(s, 5),
                "domains_touched": node_touched.get(n, []),
                **entity_facet_map.get(n, {}),
            }
            for n, s in ranked
        ]

    metrics = CorpusMetrics(
        corpus_id=corpus_id,
        corpus_change_signature=signature,
        computed_at=datetime.utcnow(),
        node_count=n_nodes,
        edge_count=n_edges,
        density=round(nx.density(G), 5),
        cross_domain_edge_pct=cross_pct,
        modularity_proxy=modularity_proxy,
        domain_density=dom_density,
        per_domain_edge_counts=per_domain,
        relation_family_counts=relation_families,
        top_pagerank=_top_list(pagerank),
        top_cross_domain_pagerank=_top_list(cd_pagerank),
        node_domain_map=node_primary,
        node_domains_touched=node_touched,
        frontier_candidates=frontier,
        fragile_bridges=fragile,
        terminological_gaps=terminological,
        structural_analogies=analogies,
        transfer_candidates=transfers,
        entity_name_map=entity_name_map,
        concept_communities=concept_communities,
        entity_concept_map=entity_concept_map,
        entity_facet_map=entity_facet_map,
        ontology_version=ontology_version,
        entity_betweenness=betweenness,
        cluster_pair_gaps=cluster_pair_gaps,
        latent_topics=latent_topics,
        metrics_engine="networkx",
    )

    await _cache_metrics(db, metrics)
    logger.info(
        "Metrics computed: corpus=%s nodes=%d edges=%d cross=%.1f%% "
        "frontier=%d analogies=%d transfers=%d concepts=%d gaps=%d latent=%d "
        "facets=%d ontology=%s",
        corpus_id, n_nodes, n_edges, cross_pct,
        len(frontier), len(analogies), len(transfers), len(concept_communities),
        len(cluster_pair_gaps), len(latent_topics), len(entity_facet_map),
        ontology_version or "none",
    )
    return metrics


async def _cache_metrics(db, m: CorpusMetrics) -> None:
    await db["graph_metrics_cache"].update_one(
        {"corpus_id": m.corpus_id},
        {"$set": _serialize_metrics(m)},
        upsert=True,
    )


def _serialize_metrics(m: CorpusMetrics) -> dict:
    return {
        "schema_version": METRICS_CACHE_SCHEMA_VERSION,
        "corpus_id": m.corpus_id,
        "corpus_change_signature": m.corpus_change_signature,
        "computed_at": m.computed_at,
        "node_count": m.node_count,
        "edge_count": m.edge_count,
        "density": m.density,
        "cross_domain_edge_pct": m.cross_domain_edge_pct,
        "modularity_proxy": m.modularity_proxy,
        "domain_density": m.domain_density,
        "per_domain_edge_counts": m.per_domain_edge_counts,
        "relation_family_counts": m.relation_family_counts,
        "top_pagerank": m.top_pagerank,
        "top_cross_domain_pagerank": m.top_cross_domain_pagerank,
        "node_domain_map": m.node_domain_map,
        "node_domains_touched": m.node_domains_touched,
        "entity_name_map": m.entity_name_map,
        "frontier_candidates": m.frontier_candidates,
        "fragile_bridges": m.fragile_bridges,
        "terminological_gaps": m.terminological_gaps,
        "structural_analogies": m.structural_analogies,
        "transfer_candidates": m.transfer_candidates,
        "concept_communities": m.concept_communities,
        "entity_concept_map": m.entity_concept_map,
        "entity_facet_map": m.entity_facet_map,
        "ontology_version": m.ontology_version,
        "entity_betweenness": m.entity_betweenness,
        "cluster_pair_gaps": m.cluster_pair_gaps,
        "latent_topics": m.latent_topics,
        "metrics_engine": m.metrics_engine,
    }


def _deserialize_metrics(doc: dict) -> CorpusMetrics:
    return CorpusMetrics(
        corpus_id=doc["corpus_id"],
        corpus_change_signature=doc["corpus_change_signature"],
        computed_at=doc["computed_at"],
        node_count=doc.get("node_count", 0),
        edge_count=doc.get("edge_count", 0),
        density=doc.get("density", 0.0),
        cross_domain_edge_pct=doc.get("cross_domain_edge_pct", 0.0),
        modularity_proxy=doc.get("modularity_proxy", 0.0),
        domain_density=doc.get("domain_density", {}),
        per_domain_edge_counts=doc.get("per_domain_edge_counts", {}),
        relation_family_counts=doc.get("relation_family_counts", {}),
        top_pagerank=doc.get("top_pagerank", []),
        top_cross_domain_pagerank=doc.get("top_cross_domain_pagerank", []),
        node_domain_map=doc.get("node_domain_map", {}),
        node_domains_touched=doc.get("node_domains_touched", {}),
        entity_name_map=doc.get("entity_name_map", {}),
        frontier_candidates=doc.get("frontier_candidates", []),
        fragile_bridges=doc.get("fragile_bridges", []),
        terminological_gaps=doc.get("terminological_gaps", []),
        structural_analogies=doc.get("structural_analogies", []),
        transfer_candidates=doc.get("transfer_candidates", []),
        concept_communities=doc.get("concept_communities", []),
        entity_concept_map=doc.get("entity_concept_map", {}),
        entity_facet_map=doc.get("entity_facet_map", {}),
        ontology_version=doc.get("ontology_version", ""),
        entity_betweenness=doc.get("entity_betweenness", {}),
        cluster_pair_gaps=doc.get("cluster_pair_gaps", []),
        latent_topics=doc.get("latent_topics", []),
        metrics_engine=doc.get("metrics_engine", "networkx"),
    )
