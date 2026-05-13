"""
Neo4j Mode B — Entity-first search.
Matches entities by name, returns chunks that mention those entities.
Used for explicit entity-query flows (not the default chat path).
"""
import logging
from typing import List, Optional

from config import get_settings
from models.schemas import SourceChunk
from neo4j import AsyncGraphDatabase

logger = logging.getLogger(__name__)


class ModeBExpansion:
    """Entity-first retrieval: search Entity nodes, then collect mentioning Chunks."""

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
                logger.error("Mode B: failed to init Neo4j driver: %s", e)

    async def search(
        self,
        query: str,
        corpus_ids: Optional[List[str]] = None,
        limit: int = 20,
    ) -> List[SourceChunk]:
        if not self._settings.NEO4J_ENABLED or not self._driver:
            return []
        if not query or not query.strip():
            return []

        # Phase 16.1 — confidence-weighted entity-first traversal.
        # Rank by sum of MENTIONS.confidence across matched entities, collect
        # the matched entity names + confidences as provenance.
        # Pt 10c — also match against query_aliases (alternate names /
        # abbreviations / spelling variants the LLM emits at extraction time).
        # `coalesce(e.query_aliases, [])` makes this safe against pre-Pt-10c
        # entities that lack the property entirely. Pure recall improvement
        # for entity-first search (this endpoint, /api/graph/entity-search);
        # zero impact on the default chat retrieval path which doesn't call
        # Mode B.
        #
        # Pt 10c drive-by — the parameter name was `$query` historically,
        # which collided with Neo4j's `AsyncSession.run(query, parameters,
        # **kwparameters)` first positional (Cypher gets bound positionally,
        # then kwarg `query=...` tries to rebind the same parameter →
        # TypeError). Renamed to `$q` to unblock this endpoint. Mode B was
        # unused in chat retrieval so the bug never surfaced; surfaced here
        # only because the Pt 10c smoke test exercises the endpoint
        # directly.
        cypher = """
        MATCH (e:Entity)<-[m:MENTIONS]-(c:Chunk)
        WHERE toLower(e.normalized_name) CONTAINS toLower($q)
           OR toLower(e.display_name) CONTAINS toLower($q)
           OR ANY(alias IN coalesce(e.query_aliases, [])
                  WHERE toLower(alias) CONTAINS toLower($q))
        """
        if corpus_ids:
            cypher += "  AND c.corpus_id IN $corpus_ids\n"
        cypher += """
        WITH c,
             sum(coalesce(m.confidence, 0.5)) AS score,
             collect(DISTINCT {
                 entity: coalesce(e.display_name, e.normalized_name, ''),
                 confidence: coalesce(m.confidence, 0.5)
             })[..5] AS via
        ORDER BY score DESC
        LIMIT $limit
        RETURN
            c.chunk_id  AS chunk_id,
            c.doc_id    AS doc_id,
            c.corpus_id AS corpus_id,
            score,
            via
        """

        try:
            async with self._driver.session() as session:
                result = await session.run(
                    cypher,
                    q=query.strip(),
                    corpus_ids=corpus_ids or [],
                    limit=limit,
                )
                rows = [dict(r) async for r in result]

            chunks = []
            for row in rows:
                raw_score = float(row.get("score") or 0.0)
                norm_score = min(raw_score, 1.0)
                via_list = row.get("via") or []
                provenance = [
                    {"entity": v.get("entity", ""), "confidence": float(v.get("confidence") or 0.0)}
                    for v in via_list
                    if v and v.get("entity")
                ]
                chunks.append(
                    SourceChunk(
                        chunk_id=row.get("chunk_id") or "",
                        parent_id="",
                        doc_id=row.get("doc_id") or "",
                        corpus_id=row.get("corpus_id") or "",
                        text="",  # hydrate step fills this
                        summary=None,
                        score=norm_score,
                        source_tier="graph_mode_b",
                        provenance=provenance or None,
                    )
                )
            logger.info(
                "Mode B entity search '%s': %d chunks (top score %.3f)",
                query,
                len(chunks),
                chunks[0].score if chunks else 0.0,
            )
            return chunks
        except Exception as e:
            logger.error("Mode B entity search failed: %s", e)
            return []

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()


mode_b_expansion = ModeBExpansion()
