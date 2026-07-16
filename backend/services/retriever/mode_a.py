"""
Neo4j Mode A — Chunk → Entity → Chunk co-reference expansion.
Seeds from the vector-retrieved chunk pool, traverses the graph for related chunks.
"""
import asyncio
import logging
from typing import List, Optional

from config import get_settings
from models.schemas import SourceChunk
from neo4j import AsyncGraphDatabase

from services.retriever.graph_payload import (
    ExpansionCache,
    prefer_relation_seeds,
    score_payload_neighbors,
    slug_candidates,
)

logger = logging.getLogger(__name__)


def _chunk_ref_key(corpus_id: str, chunk_id: str) -> str:
    return f"{corpus_id}|{chunk_id}"


def _chunk_ref_payload(corpus_id: str, chunk_id: str) -> dict[str, str]:
    return {"corpus_id": corpus_id, "chunk_id": chunk_id}


class ModeAExpansion:
    """Traverses Chunk → Entity ← Chunk to surface structurally related context."""

    def __init__(self):
        self._settings = get_settings()
        self._cache: ExpansionCache | None = None
        self._driver = None
        if self._settings.NEO4J_ENABLED:
            try:
                self._driver = AsyncGraphDatabase.driver(
                    self._settings.NEO4J_URI,
                    auth=(self._settings.NEO4J_USER, self._settings.NEO4J_PASSWORD),
                )
            except Exception as e:
                logger.error("Mode A: failed to init Neo4j driver: %s", e)

    async def expand(
        self,
        merged_pool: List[SourceChunk],
        corpus_ids: Optional[List[str]] = None,
        limit: int = 20,
        seed_limit: int | None = None,
        db=None,
        query: Optional[str] = None,
    ) -> List[SourceChunk]:
        if not self._settings.NEO4J_ENABLED or not self._driver:
            return []
        if int(limit or 0) <= 0:
            return []

        st = self._settings
        effective_seed_limit = max(
            1,
            int(seed_limit or getattr(st, "GRAPH_SEED_CHUNKS", 8)),
        )
        ordered_pool_refs = [
            (str(c.corpus_id or ""), str(c.chunk_id or ""))
            for c in sorted(
                merged_pool,
                key=lambda chunk: float(getattr(chunk, "score", 0.0) or 0.0),
                reverse=True,
            )
            if c.chunk_id and c.corpus_id
        ]
        if not ordered_pool_refs:
            return []
        # G1 preference may reorder within this window but never reach past it,
        # so a rank-40 relation-bearing chunk cannot displace the true top pool.
        candidate_refs = ordered_pool_refs[
            : max(effective_seed_limit * 3, effective_seed_limit)
        ]
        candidate_keys = [_chunk_ref_key(*ref) for ref in candidate_refs]
        candidate_ref_by_key = dict(zip(candidate_keys, candidate_refs))

        # G3 — whole-result TTL cache. Everything below is deterministic given
        # (corpora, candidate window, limit, query) + DB state; TTL bounds staleness.
        ttl = float(getattr(st, "GRAPH_EXPANSION_CACHE_TTL_SECONDS", 180.0) or 0.0)
        _copy = lambda lst: [c.model_copy(deep=True) for c in lst]  # noqa: E731
        ckey = None
        if ttl > 0:
            if self._cache is None or self._cache.ttl != ttl:
                self._cache = ExpansionCache(ttl)
            # Q4 — the emphasis mode changes the bridge-lane budget, so it
            # must be part of the cache identity or a mode flip inside the
            # TTL serves the previous mode's expansion.
            _cd = str(getattr(st, "CROSS_DOMAIN_EMPHASIS", "balanced"))
            ckey = ExpansionCache.key(
                corpus_ids, candidate_keys, limit, f"{query or ''}|cd={_cd}"
            )
            cached = self._cache.get(ckey, _copy)
            if cached is not None:
                logger.info(
                    "Mode A expansion cache HIT (%d chunks, hits=%d misses=%d)",
                    len(cached),
                    self._cache.hits,
                    self._cache.misses,
                )
                return cached

        # §12.6 offline-graph payload read — ONE indexed Mongo $in over the
        # candidate window serves BOTH G1 seed preference (has_relations) and
        # the payload-first mentions rung (neighbor_chunks). db=None or old
        # unpromoted docs -> empty map -> exactly the legacy path.
        payload_map: dict[str, dict] = {}
        if db is not None and (
            getattr(st, "GRAPH_SEED_PREFER_RELATIONS", True)
            or getattr(st, "GRAPH_PAYLOAD_FIRST", True)
        ):
            try:
                async for d in db["chunks"].find(
                    {
                        "$or": [
                            {"corpus_id": corpus_id, "chunk_id": chunk_id}
                            for corpus_id, chunk_id in candidate_refs
                        ]
                    },
                    {
                        "_id": 0,
                        "corpus_id": 1,
                        "chunk_id": 1,
                        "has_relations": 1,
                        "neighbor_chunks": 1,
                        "entity_ids": 1,
                    },
                ):
                    payload_map[
                        _chunk_ref_key(str(d["corpus_id"]), str(d["chunk_id"]))
                    ] = d
            except Exception as exc:  # noqa: BLE001
                logger.warning("Mode A payload read failed (%s) — legacy path", exc)
                payload_map = {}

        if getattr(st, "GRAPH_SEED_PREFER_RELATIONS", True) and payload_map:
            seed_keys = prefer_relation_seeds(
                candidate_keys,
                {key: bool(d.get("has_relations")) for key, d in payload_map.items()},
                effective_seed_limit,
            )
        else:
            seed_keys = candidate_keys[:effective_seed_limit]
        seed_refs = [candidate_ref_by_key[key] for key in seed_keys]
        if not seed_refs:
            return []

        # A1 — query-side entity linking: query n-gram slugs -> indexed
        # entity_id existence -> top-mention chunks become DIRECT seeds AND
        # direct evidence candidates. Two small indexed lookups.
        linked_chunks: List[SourceChunk] = []
        if (
            getattr(st, "GRAPH_QUERY_ENTITY_LINKING", True)
            and query
            and int(getattr(st, "GRAPH_ENTITY_LINK_MAX_SEEDS", 4)) > 0
        ):
            try:
                from services.graph.neo4j_writer import entity_id_from_name

                cands = slug_candidates(query, entity_id_fn=entity_id_from_name)
                if cands:
                    async with self._driver.session() as session:
                        res = await session.run(
                            "MATCH (e:Entity) WHERE e.entity_id IN $cands "
                            "RETURN e.entity_id AS eid",
                            cands=cands,
                        )
                        linked = sorted({r["eid"] async for r in res})
                    if linked:
                        link_cypher = (
                            "MATCH (e:Entity)<-[m:MENTIONS]-(c:Chunk) "
                            "WHERE e.entity_id IN $linked "
                        )
                        if corpus_ids:
                            link_cypher += "AND c.corpus_id IN $corpus_ids "
                        link_cypher += (
                            "WITH c, max(coalesce(m.confidence, 0.5)) AS conf, "
                            "collect(DISTINCT e.entity_id)[..3] AS eids "
                            "ORDER BY conf DESC, c.chunk_id "
                            "LIMIT $k "
                            "RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id, "
                            "c.corpus_id AS corpus_id, conf, eids"
                        )
                        async with self._driver.session() as session:
                            res = await session.run(
                                link_cypher,
                                linked=linked,
                                corpus_ids=corpus_ids or [],
                                k=int(getattr(st, "GRAPH_ENTITY_LINK_MAX_SEEDS", 4)),
                            )
                            rows = [dict(r) async for r in res]
                        seen_seeds = set(seed_refs)
                        for row in rows:
                            cid = row.get("chunk_id") or ""
                            corpus_id = row.get("corpus_id") or ""
                            ref = (str(corpus_id), str(cid))
                            if not cid or not corpus_id:
                                continue
                            if ref not in seen_seeds:
                                seed_refs.append(ref)
                                seen_seeds.add(ref)
                            if ref in set(ordered_pool_refs):
                                continue  # already evidence — seed only
                            linked_chunks.append(
                                SourceChunk(
                                    chunk_id=cid,
                                    parent_id="",
                                    doc_id=row.get("doc_id") or "",
                                    corpus_id=row.get("corpus_id") or "",
                                    text="",
                                    summary=None,
                                    score=min(1.0, float(row.get("conf") or 0.5)),
                                    source_tier="graph_mode_a",
                                    provenance=[
                                        {
                                            "entity": str(e)
                                            .removeprefix("entity:")
                                            .replace("_", " "),
                                            "confidence": float(row.get("conf") or 0.5),
                                            "surface_form": "",
                                            "evidence_phrase": "",
                                            "domain_type": "",
                                            "canonical_family": "",
                                            "entity_type": "",
                                            "definitional_phrase": "",
                                            "predicate": None,
                                            "relation_family": "query_entity_link",
                                        }
                                        for e in (row.get("eids") or [])[:3]
                                    ],
                                )
                            )
                        if linked:
                            logger.info(
                                "Mode A entity linking: %d linked entities, "
                                "%d direct chunks",
                                len(linked),
                                len(rows),
                            )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Mode A entity linking failed (%s) — skipped", exc)

        # §12.6 payload rung — serve the mentions hop from neighbor_chunks[]
        # (zero Cypher). Escalate to the live co-mention Cypher ONLY when the
        # validated payload candidates land under the floor.
        payload_chunks: List[SourceChunk] = []
        run_mentions_cypher = True
        if getattr(st, "GRAPH_PAYLOAD_FIRST", True) and payload_map and db is not None:
            seed_union_eids: set[str] = set()
            for ref in seed_refs:
                seed_union_eids.update(
                    (payload_map.get(_chunk_ref_key(*ref)) or {}).get("entity_ids")
                    or []
                )
            ranked = score_payload_neighbors(
                {
                    _chunk_ref_key(*ref): (
                        payload_map.get(_chunk_ref_key(*ref)) or {}
                    ).get("neighbor_chunks")
                    or []
                    for ref in seed_refs
                },
                exclude={chunk_id for _corpus_id, chunk_id in ordered_pool_refs},
                cap=max(int(limit) * 2, 16),
            )
            if ranked:
                try:
                    from services.ingestion.section_classifier import NOISY_KINDS

                    meta: dict[str, list[dict]] = {}
                    async for d in db["chunks"].find(
                        {
                            "chunk_id": {"$in": [cid for cid, _, _ in ranked]},
                            "corpus_id": {"$in": list(corpus_ids or [])},
                        },
                        {
                            "_id": 0,
                            "chunk_id": 1,
                            "doc_id": 1,
                            "corpus_id": 1,
                            "chunk_kind": 1,
                            "entity_ids": 1,
                        },
                    ):
                        meta.setdefault(str(d["chunk_id"]), []).append(d)
                    allowed = set(corpus_ids or [])
                    for cid, votes, score in ranked:
                        for m in meta.get(cid) or []:
                            if allowed and m.get("corpus_id") not in allowed:
                                continue
                            if m.get("chunk_kind") in NOISY_KINDS:
                                continue
                            shared = sorted(
                                set(m.get("entity_ids") or []) & seed_union_eids
                            )[:3]
                            payload_chunks.append(
                                SourceChunk(
                                    chunk_id=cid,
                                    parent_id="",
                                    doc_id=m.get("doc_id") or "",
                                    corpus_id=m.get("corpus_id") or "",
                                    text="",
                                    summary=None,
                                    score=score,
                                    source_tier="graph_mode_a",
                                    provenance=[
                                        {
                                            "entity": str(e)
                                            .removeprefix("entity:")
                                            .replace("_", " "),
                                            "confidence": score,
                                            "surface_form": "",
                                            "evidence_phrase": "",
                                            "domain_type": "",
                                            "canonical_family": "",
                                            "entity_type": "",
                                            "definitional_phrase": "",
                                            "predicate": None,
                                            "relation_family": "graph_payload",
                                        }
                                        for e in shared
                                    ]
                                    or None,
                                )
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Mode A payload validation failed (%s) — escalating",
                        exc,
                    )
                    payload_chunks = []
            if len(payload_chunks) >= int(
                getattr(st, "GRAPH_PAYLOAD_MIN_CANDIDATES", 4)
            ):
                run_mentions_cypher = False

        # Phase 16.1 — confidence-weighted expansion via MENTIONS co-reference.
        # Phase 4.5 — adds a parallel CALLS-walk pass so code chunks reachable
        # through graphify-emitted entity call edges also surface.
        # Phase 5b — cache-driven bonus bridge expansion (fragile / analogy /
        # terminological / transfer), capped at max(2, limit // 4) so the
        # mention/calls pool dominates 3:1. Gated on the flag + a db handle.
        #
        # All three passes seed from the SAME chunk pool and are independent, so
        # run their Neo4j round-trips CONCURRENTLY (they were sequential, ~7s
        # total on a large graph) and merge by chunk_id afterward. The bridge
        # pass keeps its own error isolation so a metrics-cache miss can't break
        # the mention/calls result.
        bridge_enabled = (
            getattr(self._settings, "RETRIEVAL_CACHE_MODE_A_METRICS", True)
            and db is not None
            and corpus_ids
        )

        async def _bridges() -> List[SourceChunk]:
            if not bridge_enabled:
                return []
            try:
                from services.retriever.cross_domain import bridge_cap, normalize_mode

                cap = bridge_cap(
                    limit,
                    normalize_mode(getattr(st, "CROSS_DOMAIN_EMPHASIS", "balanced")),
                )
                if cap <= 0:
                    return []
                return await self._expand_via_bridges(
                    seed_refs=[_chunk_ref_payload(*ref) for ref in seed_refs],
                    corpus_ids=corpus_ids,
                    db=db,
                    limit=cap,
                )
            except Exception as exc:
                logger.warning(
                    "Mode A bridge expansion failed (%s) — continuing with "
                    "mentions + calls only",
                    exc,
                )
                return []

        async def _mentions() -> List[SourceChunk]:
            # §12.6 ladder — the payload rung covered this hop; skip the
            # (historically multi-second) co-mention traversal entirely.
            if not run_mentions_cypher:
                return []
            return await self._expand_via_mentions(
                [_chunk_ref_payload(*ref) for ref in seed_refs], corpus_ids, limit
            )

        mention_chunks, calls_chunks, bridge_chunks = await asyncio.gather(
            _mentions(),
            self._expand_via_calls(
                [_chunk_ref_payload(*ref) for ref in seed_refs], corpus_ids, limit
            ),
            _bridges(),
        )

        merged: dict[tuple[str, str], SourceChunk] = {}
        for pool in (
            payload_chunks,
            linked_chunks,
            mention_chunks,
            calls_chunks,
            bridge_chunks,
        ):
            for c in pool:
                if not c.chunk_id:
                    continue
                key = (str(c.corpus_id or ""), str(c.chunk_id))
                existing = merged.get(key)
                if existing is None:
                    merged[key] = c
                else:
                    # Same chunk surfaced via two or more patterns — sum
                    # scores, append provenance so the prompt can show
                    # all bridge entities that pulled this chunk.
                    existing.score = min(1.0, existing.score + c.score)
                    if c.provenance:
                        existing.provenance = (existing.provenance or []) + c.provenance

        # NOISY_KINDS filter — Neo4j Chunk nodes carry NO chunk_kind (props only
        # doc_id/chunk_id/corpus_id), so co-mention noise (bibliography/index/toc
        # reference-list chunks) can't be filtered in-Cypher the way the lexical
        # lane does. ONE batched Mongo lookup over the merged union of all 3
        # lanes asks for the small NOISY set to drop. Chunks missing chunk_kind
        # never match $in -> kept (same legacy invariant as the lexical lane).
        # db=None -> no-op; any Mongo error -> unfiltered (degrades to prior
        # behavior), never raises. Runs BEFORE the [:limit] cap so the cap
        # applies to the cleaned set.
        if db is not None and merged:
            try:
                from services.ingestion.section_classifier import NOISY_KINDS

                noisy_refs = {
                    (str(doc["corpus_id"]), str(doc["chunk_id"]))
                    async for doc in db["chunks"].find(
                        {
                            "$or": [
                                {"corpus_id": corpus_id, "chunk_id": chunk_id}
                                for corpus_id, chunk_id in merged
                            ],
                            "chunk_kind": {"$in": list(NOISY_KINDS)},
                        },
                        {"_id": 0, "corpus_id": 1, "chunk_id": 1},
                    )
                }
                for ref in noisy_refs:
                    merged.pop(ref, None)
                if noisy_refs:
                    logger.info(
                        "Mode A NOISY_KINDS filter: dropped %d co-mention chunk(s)",
                        len(noisy_refs),
                    )
            except Exception as exc:
                logger.warning(
                    "Mode A NOISY_KINDS filter skipped (%s) — returning unfiltered",
                    exc,
                )

        expanded = sorted(merged.values(), key=lambda c: c.score, reverse=True)[:limit]
        logger.info(
            "Mode A expansion: %d unique chunks (payload=%d, linked=%d, "
            "mentions=%d, calls=%d, bridges=%d, seeds=%d, cap=%d, "
            "mentions_cypher=%s, top score %.3f)",
            len(expanded),
            len(payload_chunks),
            len(linked_chunks),
            len(mention_chunks),
            len(calls_chunks),
            len(bridge_chunks),
            len(seed_refs),
            int(limit),
            "ran" if run_mentions_cypher else "skipped",
            expanded[0].score if expanded else 0.0,
        )
        if ckey is not None and self._cache is not None:
            self._cache.put(ckey, expanded, _copy)
        return expanded

    async def _expand_via_bridges(
        self,
        *,
        seed_refs: List[dict[str, str]],
        corpus_ids: List[str],
        db,
        limit: int,
    ) -> List[SourceChunk]:
        """Phase 5b — cache-driven bonus expansion via bridge endpoints.

        Steps:
          1. Cypher: collect entity_ids that the seed chunks MENTION.
             ~1 ms on warm Neo4j (uses the chunk_id index).
          2. Mongo: per-corpus get_cached_metrics. Skipped corpora
             contribute nothing.
          3. Pure Python: walk fragile_bridges + structural_analogies +
             terminological_gaps + transfer_candidates. For each entry
             where one endpoint is a seed entity, the OTHER endpoint
             becomes a bonus entity. Score is derived from the entry's
             own signal strength so synthetic scores stay in the
             0.0-1.0 range that Mode A's other passes use.
          4. Cypher: fetch chunks mentioning the top-K bonus entities,
             scored by per-chunk MENTIONS confidence × bonus magnitude.

        Returns up to `limit` SourceChunk objects, each with provenance
        carrying the bridge type and the bonus entity name so the
        downstream prompt template can surface why each chunk landed.
        """
        # Step 1 — seed entity_ids
        seed_entity_cypher = """
        UNWIND $seed_refs AS seed_ref
        MATCH (c:Chunk {corpus_id: seed_ref.corpus_id, chunk_id: seed_ref.chunk_id})
              -[:MENTIONS]->(e:Entity)
        RETURN collect(DISTINCT e.entity_id) AS seed_entity_ids
        """
        try:
            async with self._driver.session() as session:
                result = await session.run(seed_entity_cypher, seed_refs=seed_refs)
                row = await result.single()
                seed_entity_ids = (
                    set(row.get("seed_entity_ids") or []) if row else set()
                )
        except Exception as exc:
            logger.debug("Mode A bridges: seed entity Cypher failed: %s", exc)
            return []
        if not seed_entity_ids:
            return []

        # Step 2 — load + merge cache fields across warm corpora
        try:
            from services.graph.analytics import (
                compute_corpus_change_signature,
                get_cached_metrics,
            )
        except ImportError as exc:
            logger.warning(
                "Mode A bridges: analytics module import failed (%s) — "
                "skipping bonus expansion",
                exc,
            )
            return []

        # bonus_scores: entity_id → (score, label) where label is for
        # provenance ("fragile to X via path_count=N", "analog of Y", etc.)
        bonus_scores: dict[str, tuple[float, str, str]] = {}

        def _consider(
            eid: str, score: float, bridge_type: str, partner_name: str
        ) -> None:
            if not eid or eid in seed_entity_ids:
                return
            cur = bonus_scores.get(eid)
            if cur is None or score > cur[0]:
                bonus_scores[eid] = (score, bridge_type, partner_name)

        warm_corpora = 0
        for corpus_id in corpus_ids:
            try:
                sig = await compute_corpus_change_signature(db, corpus_id)
                metrics = await get_cached_metrics(db, corpus_id, sig)
                if metrics is None:
                    continue
                # Sparse-graph guard — same shape as Phase 5a's. When the
                # corpus has 0 RELATES_TO edges, fragile_bridges and friends
                # are guaranteed to be empty lists (the analytics pipeline
                # only emits them when structure exists). Skip the per-
                # bridge-type loops to save iteration cost. Same semantic
                # outcome (empty bonus_scores contribution from this corpus)
                # but avoids ~4 redundant `or []` walks.
                if int(getattr(metrics, "edge_count", 0) or 0) == 0:
                    logger.debug(
                        "Mode A bridges: corpus=%s edge_count=0 — skipping "
                        "bridge iteration (no structure)",
                        corpus_id,
                    )
                    continue
                warm_corpora += 1
                # fragile_bridges — articulation edges. path_count is
                # always 1 for true articulation edges; the score floor
                # of 0.4 reflects "structurally important by construction."
                for fb in getattr(metrics, "fragile_bridges", None) or []:
                    src = fb.get("source")
                    tgt = fb.get("target")
                    path_count = int(fb.get("path_count") or 1)
                    base_score = 0.4 + 0.05 * min(path_count, 4)
                    if src in seed_entity_ids and tgt not in seed_entity_ids:
                        _consider(
                            tgt, base_score, "fragile", fb.get("source_name") or src
                        )
                    elif tgt in seed_entity_ids and src not in seed_entity_ids:
                        _consider(
                            src, base_score, "fragile", fb.get("target_name") or tgt
                        )
                # terminological_gaps — high topology + high jaccard.
                # Score blends both signals; ranges naturally 0.0-0.9.
                for tg in getattr(metrics, "terminological_gaps", None) or []:
                    src = tg.get("source")
                    tgt = tg.get("target")
                    try:
                        sim = float(tg.get("topology_sim") or 0.0)
                        jac = float(tg.get("neighbor_jaccard") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    score = min(0.9, sim * jac * 2.0)
                    if src in seed_entity_ids and tgt not in seed_entity_ids:
                        _consider(
                            tgt, score, "terminological", tg.get("source_name") or src
                        )
                    elif tgt in seed_entity_ids and src not in seed_entity_ids:
                        _consider(
                            src, score, "terminological", tg.get("target_name") or tgt
                        )
                # structural_analogies — high topology, low jaccard.
                # Slightly capped to keep analogies below
                # terminological/fragile in calibration.
                for sa in getattr(metrics, "structural_analogies", None) or []:
                    src = sa.get("source")
                    tgt = sa.get("target")
                    try:
                        sim = float(sa.get("topology_sim") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    score = min(0.7, sim)
                    if src in seed_entity_ids and tgt not in seed_entity_ids:
                        _consider(tgt, score, "analogy", sa.get("source_name") or src)
                    elif tgt in seed_entity_ids and src not in seed_entity_ids:
                        _consider(src, score, "analogy", sa.get("target_name") or tgt)
                # transfer_candidates — flattened to (hub, analog) pairs.
                # Hub must be a seed entity; each analog becomes a bonus.
                for tc in getattr(metrics, "transfer_candidates", None) or []:
                    hub = tc.get("hub")
                    if hub not in seed_entity_ids:
                        continue
                    hub_name = tc.get("hub_name") or hub
                    for analog in tc.get("analogs", []) or []:
                        analog_eid = analog.get("entity")
                        try:
                            sim = float(analog.get("topology_sim") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        score = min(0.6, sim)
                        _consider(analog_eid, score, "transfer", hub_name)
            except Exception as exc:
                logger.debug("Mode A bridges: cache miss corpus=%s: %s", corpus_id, exc)

        if not bonus_scores or warm_corpora == 0:
            return []

        # Step 4 — fetch chunks for the top-K bonus entities.
        # Sort by score desc, take the top entity_ids whose bonus chunks
        # could fill the cap. Cap on entities not chunks: each entity
        # contributes up to ~2-3 chunks via the MENTIONS Cypher's
        # natural breadth, so capping at limit is enough to fill the
        # `limit` cap downstream.
        top_entity_ids = sorted(
            bonus_scores.items(), key=lambda kv: kv[1][0], reverse=True
        )[: max(limit, 8)]
        entity_id_list = [eid for eid, _ in top_entity_ids]

        bonus_cypher = """
        UNWIND $entity_ids AS eid
        MATCH (e:Entity {entity_id: eid})<-[m:MENTIONS]-(c:Chunk)
        WHERE c.corpus_id IN $corpus_ids
          AND none(seed_ref IN $seed_refs WHERE
              seed_ref.corpus_id = c.corpus_id AND seed_ref.chunk_id = c.chunk_id)
        WITH eid, c, max(coalesce(m.confidence, 0.5)) AS conf
        WITH eid, c, conf
        ORDER BY conf DESC
        RETURN c.chunk_id AS chunk_id,
               c.doc_id   AS doc_id,
               c.corpus_id AS corpus_id,
               conf       AS mention_conf,
               eid        AS via_entity_id
        LIMIT $hard_cap
        """
        hard_cap = limit * 4  # over-fetch then trim after scoring
        try:
            async with self._driver.session() as session:
                result = await session.run(
                    bonus_cypher,
                    entity_ids=entity_id_list,
                    corpus_ids=corpus_ids,
                    seed_refs=seed_refs,
                    hard_cap=hard_cap,
                )
                rows = [dict(r) async for r in result]
        except Exception as exc:
            logger.debug("Mode A bridges: bonus Cypher failed: %s", exc)
            return []

        # Score each row: bonus_score × mention_confidence. Cap at 1.0.
        out: list[SourceChunk] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            cid = str(row.get("chunk_id") or "")
            corpus_id = str(row.get("corpus_id") or "")
            ref = (corpus_id, cid)
            if not cid or not corpus_id or ref in seen:
                continue
            seen.add(ref)
            via_eid = str(row.get("via_entity_id") or "")
            bonus_tuple = bonus_scores.get(via_eid)
            if bonus_tuple is None:
                continue
            bonus_score, bridge_type, partner_name = bonus_tuple
            try:
                mention_conf = float(row.get("mention_conf") or 0.5)
            except (TypeError, ValueError):
                mention_conf = 0.5
            final_score = min(1.0, bonus_score * mention_conf)
            # Same field defaults as _row_to_chunk — parent_id + text
            # are filled by the downstream Mongo hydrate step, not here.
            # source_tier="graph_mode_a_bridge" so the renderer can tell
            # bridge chunks apart from mention/calls chunks.
            out.append(
                SourceChunk(
                    chunk_id=cid,
                    parent_id="",
                    doc_id=str(row.get("doc_id") or ""),
                    corpus_id=str(row.get("corpus_id") or ""),
                    text="",
                    summary=None,
                    score=final_score,
                    source_tier="graph_mode_a_bridge",
                    provenance=[
                        {
                            "via": "bridge",
                            "bridge_type": bridge_type,
                            "via_entity": partner_name,
                            "bonus_score": round(bonus_score, 3),
                            "mention_conf": round(mention_conf, 3),
                        }
                    ],
                )
            )

        out.sort(key=lambda c: c.score, reverse=True)
        out = out[:limit]
        logger.info(
            "Mode A bridges: warm_corpora=%d seed_entities=%d bonus_entities=%d "
            "chunks=%d (top score %.3f)",
            warm_corpora,
            len(seed_entity_ids),
            len(bonus_scores),
            len(out),
            out[0].score if out else 0.0,
        )
        return out

    async def _expand_via_mentions(
        self,
        seed_refs: List[dict[str, str]],
        corpus_ids: Optional[List[str]],
        limit: int,
    ) -> List[SourceChunk]:
        """Phase 16.1 — confidence-weighted Chunk → Entity ← Chunk expansion.

        Pt 10a (Cluster 5) — enriched provenance: surface_form + evidence_phrase
        from the MENTIONS edge, domain_type + canonical_family from the Entity.
        Predicate/relation_family stay null here; Mode C (RELATES_TO walk) fills
        them; CALLS-walk (below) fills them with predicate='calls'.
        """
        cypher = """
        UNWIND $seed_refs AS seed_ref
        MATCH (seed:Chunk {corpus_id: seed_ref.corpus_id, chunk_id: seed_ref.chunk_id})
              -[s:MENTIONS]->(e:Entity)<-[x:MENTIONS]-(expanded:Chunk)
        WHERE none(other_ref IN $seed_refs WHERE
              other_ref.corpus_id = expanded.corpus_id
              AND other_ref.chunk_id = expanded.chunk_id)
        """
        if corpus_ids:
            cypher += "  AND expanded.corpus_id IN $corpus_ids\n"
        cypher += """
        WITH expanded,
             sum(coalesce(s.confidence, 0.5) * coalesce(x.confidence, 0.5)) AS score,
             collect(DISTINCT {
                 entity: coalesce(e.display_name, e.normalized_name, ''),
                 confidence: coalesce(x.confidence, 0.5),
                 surface_form: coalesce(x.surface_form, ''),
                 evidence_phrase: coalesce(x.evidence_phrase, ''),
                 domain_type: coalesce(e.domain_type, ''),
                 canonical_family: coalesce(e.canonical_family, ''),
                 entity_type: coalesce(e.primary_entity_type, e.entity_type, ''),
                 definitional_phrase: coalesce(e.definitional_phrase, '')
             })[..5] AS via
        ORDER BY score DESC
        LIMIT $limit
        RETURN
            expanded.chunk_id   AS chunk_id,
            expanded.doc_id     AS doc_id,
            expanded.corpus_id  AS corpus_id,
            score,
            via
        """

        try:
            async with self._driver.session() as session:
                result = await session.run(
                    cypher,
                    seed_refs=seed_refs,
                    corpus_ids=corpus_ids or [],
                    limit=limit,
                )
                rows = [dict(r) async for r in result]
            return [
                self._row_to_chunk(row, predicate=None, relation_family=None)
                for row in rows
            ]
        except Exception as e:
            logger.error("Mode A MENTIONS expansion failed: %s", e)
            return []

    async def _expand_via_calls(
        self,
        seed_refs: List[dict[str, str]],
        corpus_ids: Optional[List[str]],
        limit: int,
    ) -> List[SourceChunk]:
        """Phase 4.5 — walk graphify-emitted CALLS edges between entities to
        surface chunks reachable through the call graph.

        Path: seed:Chunk -[MENTIONS]-> seed_e:Entity -[CALLS]- neighbor_e:Entity
              <-[MENTIONS]- expanded:Chunk

        The CALLS hop is undirected so callers AND callees of the seed's
        entities both surface. Per-edge confidence multiplies the score
        (graphify writes confidence=1.0; lower-confidence sources reduce
        the path's contribution proportionally). When `corpus_ids` is
        non-empty we filter BOTH the expanded chunk's corpus and the CALLS
        edge's corpus membership (graphify writes corpus_ids as an array,
        same convention as RELATES_TO).
        """
        cypher = """
        UNWIND $seed_refs AS seed_ref
        MATCH (seed:Chunk {corpus_id: seed_ref.corpus_id, chunk_id: seed_ref.chunk_id})
              -[s:MENTIONS]->(seed_e:Entity)
        MATCH (seed_e)-[c:CALLS]-(neighbor_e:Entity)
        MATCH (neighbor_e)<-[x:MENTIONS]-(expanded:Chunk)
        WHERE none(other_ref IN $seed_refs WHERE
              other_ref.corpus_id = expanded.corpus_id
              AND other_ref.chunk_id = expanded.chunk_id)
          AND seed_e <> neighbor_e
        """
        if corpus_ids:
            cypher += "  AND expanded.corpus_id IN $corpus_ids\n"
            cypher += "  AND any(cid IN $corpus_ids WHERE cid IN coalesce(c.corpus_ids, []))\n"
        cypher += """
        WITH expanded,
             sum(
                 coalesce(s.confidence, 0.5)
                 * coalesce(c.confidence, 1.0)
                 * coalesce(x.confidence, 0.5)
             ) AS score,
             collect(DISTINCT {
                 entity: coalesce(neighbor_e.display_name, neighbor_e.normalized_name, ''),
                 confidence: coalesce(c.confidence, 1.0),
                 surface_form: '',
                 evidence_phrase: '',
                 domain_type: coalesce(neighbor_e.domain_type, ''),
                 canonical_family: coalesce(neighbor_e.canonical_family, ''),
                 entity_type: coalesce(neighbor_e.primary_entity_type, neighbor_e.entity_type, ''),
                 definitional_phrase: coalesce(neighbor_e.definitional_phrase, '')
             })[..5] AS via
        ORDER BY score DESC
        LIMIT $limit
        RETURN
            expanded.chunk_id   AS chunk_id,
            expanded.doc_id     AS doc_id,
            expanded.corpus_id  AS corpus_id,
            score,
            via
        """

        try:
            async with self._driver.session() as session:
                result = await session.run(
                    cypher,
                    seed_refs=seed_refs,
                    corpus_ids=corpus_ids or [],
                    limit=limit,
                )
                rows = [dict(r) async for r in result]
            return [
                self._row_to_chunk(
                    row, predicate="calls", relation_family="code_call_graph"
                )
                for row in rows
            ]
        except Exception as e:
            logger.error("Mode A CALLS expansion failed: %s", e)
            return []

    @staticmethod
    def _row_to_chunk(
        row: dict,
        *,
        predicate: Optional[str],
        relation_family: Optional[str],
    ) -> SourceChunk:
        """Translate a Mode A row to SourceChunk. Predicate/relation_family
        differ per pattern: MENTIONS-walk uses None/None, CALLS-walk uses
        'calls'/'code_call_graph'. context_manager renders both for the LLM."""
        raw_score = float(row.get("score") or 0.0)
        norm_score = min(raw_score, 1.0)
        via_list = row.get("via") or []
        provenance = [
            {
                "entity": v.get("entity", ""),
                "confidence": float(v.get("confidence") or 0.0),
                "surface_form": v.get("surface_form") or "",
                "evidence_phrase": v.get("evidence_phrase") or "",
                "domain_type": v.get("domain_type") or "",
                "canonical_family": v.get("canonical_family") or "",
                "entity_type": v.get("entity_type") or "",
                "definitional_phrase": v.get("definitional_phrase") or "",
                "predicate": predicate,
                "relation_family": relation_family,
            }
            for v in via_list
            if v and v.get("entity")
        ]
        return SourceChunk(
            chunk_id=row.get("chunk_id") or "",
            parent_id="",
            doc_id=row.get("doc_id") or "",
            corpus_id=row.get("corpus_id") or "",
            text="",  # hydrate step fills this
            summary=None,
            score=norm_score,
            source_tier="graph_mode_a",
            provenance=provenance or None,
        )

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()


mode_a_expansion = ModeAExpansion()
