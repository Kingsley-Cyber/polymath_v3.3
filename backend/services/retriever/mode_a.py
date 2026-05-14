"""
Neo4j Mode A — Chunk → Entity → Chunk co-reference expansion.
Seeds from the vector-retrieved chunk pool, traverses the graph for related chunks.
"""
import logging
from typing import List, Optional

from config import get_settings
from models.schemas import SourceChunk
from neo4j import AsyncGraphDatabase

logger = logging.getLogger(__name__)


class ModeAExpansion:
    """Traverses Chunk → Entity ← Chunk to surface structurally related context."""

    def __init__(self):
        self._settings = get_settings()
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
    ) -> List[SourceChunk]:
        if not self._settings.NEO4J_ENABLED or not self._driver:
            return []

        seed_ids = [c.chunk_id for c in merged_pool if c.chunk_id]
        if not seed_ids:
            return []

        # Phase 16.1 — confidence-weighted expansion via MENTIONS co-reference.
        # Phase 4.5 — adds a parallel CALLS-walk pass so code chunks reachable
        # through graphify-emitted entity call edges also surface. Both passes
        # run, results merge by chunk_id (scores sum, provenance concatenates).
        mention_chunks = await self._expand_via_mentions(seed_ids, corpus_ids, limit)
        calls_chunks = await self._expand_via_calls(seed_ids, corpus_ids, limit)

        merged: dict[str, SourceChunk] = {}
        for pool in (mention_chunks, calls_chunks):
            for c in pool:
                if not c.chunk_id:
                    continue
                existing = merged.get(c.chunk_id)
                if existing is None:
                    merged[c.chunk_id] = c
                else:
                    # Same chunk surfaced via both patterns — sum scores, append
                    # provenance so the prompt can show both bridge entities.
                    existing.score = min(1.0, existing.score + c.score)
                    if c.provenance:
                        existing.provenance = (existing.provenance or []) + c.provenance
        expanded = sorted(merged.values(), key=lambda c: c.score, reverse=True)[:limit]
        logger.info(
            "Mode A expansion: %d unique chunks (mentions=%d, calls=%d, top score %.3f)",
            len(expanded), len(mention_chunks), len(calls_chunks),
            expanded[0].score if expanded else 0.0,
        )
        return expanded

    async def _expand_via_mentions(
        self,
        seed_ids: List[str],
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
        MATCH (seed:Chunk)-[s:MENTIONS]->(e:Entity)<-[x:MENTIONS]-(expanded:Chunk)
        WHERE seed.chunk_id IN $seed_ids
          AND NOT expanded.chunk_id IN $seed_ids
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
                    seed_ids=seed_ids,
                    corpus_ids=corpus_ids or [],
                    limit=limit,
                )
                rows = [dict(r) async for r in result]
            return [self._row_to_chunk(row, predicate=None, relation_family=None) for row in rows]
        except Exception as e:
            logger.error("Mode A MENTIONS expansion failed: %s", e)
            return []

    async def _expand_via_calls(
        self,
        seed_ids: List[str],
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
        MATCH (seed:Chunk)-[s:MENTIONS]->(seed_e:Entity)
        MATCH (seed_e)-[c:CALLS]-(neighbor_e:Entity)
        MATCH (neighbor_e)<-[x:MENTIONS]-(expanded:Chunk)
        WHERE seed.chunk_id IN $seed_ids
          AND NOT expanded.chunk_id IN $seed_ids
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
                    seed_ids=seed_ids,
                    corpus_ids=corpus_ids or [],
                    limit=limit,
                )
                rows = [dict(r) async for r in result]
            return [
                self._row_to_chunk(row, predicate="calls", relation_family="code_call_graph")
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
