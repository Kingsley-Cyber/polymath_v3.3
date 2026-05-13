"""
Pt 10d (Cluster 2 — Graph Decoration) — post-retrieval schema enrichment.

The earlier "Mode C" framing tried to make schema-aware graph reach a new
retrieval tier competing with Mode A. That baked in three risks: ranking
changes with no A/B baseline, cross-doc incoherence (neighbor chunks pulled
from a different parent doc can argue the opposite conclusion to the
winning chunk), and conflict with the chat orchestrator's reasoning modes
that already instruct the LLM to infer the graph itself
(`graph_reason` / `kg_augmented` / `graphrag_integrated`).

The correct primitive is **graph decoration**: a read-only post-retrieval
step that takes the winning chunks Mode A + Funnel A/B already chose, runs
one quality-filtered Cypher to gather their neighbor-entity edges, and
attaches that structured context to synthesis. Winning chunks are
unchanged. No ranking risk. One revert rolls everything back.

Cypher quality gates (write-only properties from the ingest writer that
finally have a read path):
  - r.eligible_for_synthesis = true        (explicitly designed for chat)
  - r.edge_strength IN ['strong', 'repaired']  (drops weak + thin)

Cypher coherence guard:
  - parent_boost CASE on winner.doc_id      (prefer sibling chunks of the
                                              same parent doc over cross-doc
                                              chunks that may argue
                                              opposing positions)

Reasoning-mode gating happens UPSTREAM in chat_orchestrator — this module
is read-only against Neo4j and always produces decoration data; the caller
decides whether to render it inline, hand it to the reasoning cascade, or
skip it entirely.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from config import get_settings
from models.schemas import GraphDecoration, GraphDecorationEvidence, SourceChunk
from neo4j import AsyncGraphDatabase

logger = logging.getLogger(__name__)


# Mode names from services/reasoning.py:ReasoningMode + REASONING_TEMPLATES
# where the prompt itself tells the LLM to construct/infer the graph. When
# the active reasoning_mode (or any entry in reasoning_blend) is one of
# these, the chat_orchestrator should NOT pass decoration to
# build_augmented_prompt — handing the LLM a pre-built graph while also
# instructing it to infer one creates a "contradict or ignore" conflict.
GRAPH_REASONING_MODES: frozenset[str] = frozenset(
    {"graph_reason", "kg_augmented", "graphrag_integrated"}
)


def should_skip_inline_decoration(
    reasoning_mode: Optional[str],
    reasoning_blend: Optional[List[str]],
) -> bool:
    """Returns True if the LLM is already instructed to build the graph itself.

    Used by chat_orchestrator to decide whether to pass decoration into the
    chat prompt. The decoration is still computed (cheap, read-only) so it
    can feed the reasoning cascade as structured input even when inline
    rendering is skipped.
    """
    if reasoning_mode and reasoning_mode in GRAPH_REASONING_MODES:
        return True
    if reasoning_blend:
        for entry in reasoning_blend:
            if entry and entry in GRAPH_REASONING_MODES:
                return True
    return False


class GraphDecorator:
    """Runs the quality-gated, parent-aware decoration Cypher against Neo4j."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._driver = None
        if self._settings.NEO4J_ENABLED:
            try:
                self._driver = AsyncGraphDatabase.driver(
                    self._settings.NEO4J_URI,
                    auth=(self._settings.NEO4J_USER, self._settings.NEO4J_PASSWORD),
                )
            except Exception as exc:
                logger.error("GraphDecorator: failed to init Neo4j driver: %s", exc)

    async def decorate_winners(
        self,
        winning_chunks: List[SourceChunk],
        corpus_ids: Optional[List[str]] = None,
        *,
        wanted_families: Optional[List[str]] = None,
        neighbor_limit: int = 8,
        chunks_per_neighbor: int = 3,
    ) -> List[GraphDecoration]:
        """Attach edge-level graph context to chunks that already won retrieval.

        Returns one GraphDecoration per (winner_chunk, seed_entity,
        neighbor_entity) tuple, capped at `neighbor_limit`. Each decoration
        carries the predicate + family + edge evidence + quality flags and
        up to `chunks_per_neighbor` supporting chunks (with parent_boost so
        same-doc siblings rank above cross-doc neighbors).

        Returns an empty list on any failure — the caller (chat
        orchestrator) treats decoration as additive and falls back to
        plain chunk-only synthesis. NEVER raises into the chat path.
        """
        if not self._settings.NEO4J_ENABLED or not self._driver:
            return []
        if not winning_chunks:
            return []

        winning_chunk_ids = [c.chunk_id for c in winning_chunks if c.chunk_id]
        if not winning_chunk_ids:
            return []

        cypher = """
        // Pt 10d — graph decoration over winners
        MATCH (winner:Chunk)-[:MENTIONS]->(seed:Entity)-[r:RELATES_TO]-(neighbor:Entity)
        WHERE winner.chunk_id IN $winning_chunk_ids
          AND r.eligible_for_synthesis = true
          AND r.edge_strength IN ['strong', 'repaired']
          AND (size($wanted_families) = 0 OR r.relation_family IN $wanted_families)
        WITH winner, seed, neighbor, r,
             coalesce(r.confidence, 0.5) * CASE r.edge_strength
                 WHEN 'strong'   THEN 1.0
                 WHEN 'repaired' THEN 0.7
                 ELSE 0.0
             END AS edge_weight
        // Step 2 — find supporting chunks for the neighbor entity, parent-
        // boost siblings of the winner's doc so cross-doc neighbors don't
        // smuggle in opposing-argument context.
        OPTIONAL MATCH (neighbor)<-[:MENTIONS]-(evidence:Chunk)
        WHERE (size($corpus_ids) = 0 OR evidence.corpus_id IN $corpus_ids)
          AND evidence.chunk_id <> winner.chunk_id
        WITH winner, seed, neighbor, r, edge_weight,
             evidence,
             CASE WHEN evidence.doc_id = winner.doc_id THEN 2 ELSE 1 END AS parent_boost
        ORDER BY parent_boost DESC, edge_weight DESC
        WITH winner.chunk_id              AS winner_chunk_id,
             coalesce(seed.display_name, seed.normalized_name, '')     AS seed_entity,
             coalesce(neighbor.display_name, neighbor.normalized_name, '') AS neighbor_entity,
             coalesce(r.predicate, '')        AS predicate,
             coalesce(r.relation_family, '')  AS relation_family,
             coalesce(r.evidence_phrase, '')  AS edge_evidence,
             coalesce(r.direction_repaired, false) AS direction_repaired,
             coalesce(r.predicate_refined,  false) AS predicate_refined,
             edge_weight,
             collect(DISTINCT {
                 chunk_id: evidence.chunk_id,
                 doc_id: evidence.doc_id,
                 parent_boost: parent_boost
             })[..$chunks_per_neighbor] AS evidence_chunks
        ORDER BY edge_weight DESC
        RETURN winner_chunk_id, seed_entity, neighbor_entity, predicate,
               relation_family, edge_evidence, direction_repaired,
               predicate_refined, edge_weight, evidence_chunks
        LIMIT $neighbor_limit
        """

        try:
            async with self._driver.session() as session:
                result = await session.run(
                    cypher,
                    winning_chunk_ids=winning_chunk_ids,
                    corpus_ids=corpus_ids or [],
                    wanted_families=wanted_families or [],
                    neighbor_limit=int(neighbor_limit),
                    chunks_per_neighbor=int(chunks_per_neighbor),
                )
                rows = [dict(r) async for r in result]

            decorations: List[GraphDecoration] = []
            for row in rows:
                if not row.get("predicate"):
                    continue
                evidence_chunks: List[GraphDecorationEvidence] = []
                for ev in row.get("evidence_chunks") or []:
                    cid = ev.get("chunk_id") if isinstance(ev, dict) else None
                    if not cid:
                        continue
                    evidence_chunks.append(
                        GraphDecorationEvidence(
                            chunk_id=str(cid),
                            doc_id=ev.get("doc_id"),
                            parent_boost=int(ev.get("parent_boost") or 1),
                        )
                    )
                decorations.append(
                    GraphDecoration(
                        winner_chunk_id=str(row.get("winner_chunk_id") or ""),
                        seed_entity=str(row.get("seed_entity") or ""),
                        neighbor_entity=str(row.get("neighbor_entity") or ""),
                        predicate=str(row.get("predicate") or ""),
                        relation_family=str(row.get("relation_family") or ""),
                        edge_evidence=str(row.get("edge_evidence") or ""),
                        direction_repaired=bool(row.get("direction_repaired") or False),
                        predicate_refined=bool(row.get("predicate_refined") or False),
                        edge_weight=float(row.get("edge_weight") or 0.0),
                        evidence_chunks=evidence_chunks,
                    )
                )
            logger.info(
                "Graph decoration: %d arrows over %d winners (wanted_families=%s)",
                len(decorations),
                len(winning_chunk_ids),
                wanted_families,
            )
            return decorations
        except Exception as exc:
            logger.warning("Graph decoration failed (chunk-only fallback): %s", exc)
            return []

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()


# Module-level singleton (mirrors mode_a_expansion + fact_retrieval lifecycle).
graph_decorator = GraphDecorator()
