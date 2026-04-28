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

        # Phase 16.1 — confidence-weighted expansion:
        # score each candidate by sum(seed_edge.confidence * expanded_edge.confidence)
        # across all shared entities, instead of raw shared-entity count.
        # Also collect the {entity, confidence} list as provenance for the prompt.
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
                 confidence: coalesce(x.confidence, 0.5)
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

            expanded = []
            for row in rows:
                # Normalize the Cypher score to [0,1]-ish range for reranker compat.
                # Max theoretical = sum of 1.0 * 1.0 across shared entities, so we
                # soft-cap at 1.0. Reranker will re-score anyway; we just want the
                # initial ordering to reflect confidence-weighted strength.
                raw_score = float(row.get("score") or 0.0)
                norm_score = min(raw_score, 1.0)
                via_list = row.get("via") or []
                provenance = [
                    {"entity": v.get("entity", ""), "confidence": float(v.get("confidence") or 0.0)}
                    for v in via_list
                    if v and v.get("entity")
                ]
                expanded.append(
                    SourceChunk(
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
                )
            logger.info(
                "Mode A expansion: %d extra chunks (weighted, top score %.3f)",
                len(expanded),
                expanded[0].score if expanded else 0.0,
            )
            return expanded
        except Exception as e:
            logger.error("Mode A graph expansion failed: %s", e)
            return []

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()


mode_a_expansion = ModeAExpansion()
