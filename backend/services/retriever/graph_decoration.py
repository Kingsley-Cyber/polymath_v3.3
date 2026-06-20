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
import time
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


# ── GERG: query-relevance edge ranking ────────────────────────────────────
# Generic domain tokens that must NOT, on their own, count as a query-concept
# hit — an edge needs a real subject match (e.g. 'nlp'), not bare
# 'model'/'fine'/'tuning'. Without this guard, ranking edges by raw confidence
# surfaces catalog noise like 'OpenAI--works_for-->Sam Altman' on an NLP query.
_GENERIC_QUERY_CONCEPTS: frozenset[str] = frozenset(
    {
        "model", "models", "modeling", "modelling", "system", "systems",
        "method", "methods", "approach", "approaches", "technique",
        "techniques", "data", "dataset", "datasets", "process", "processes",
        "framework", "frameworks", "tool", "tools", "task", "tasks", "fine",
        "tuning", "tune", "tuned", "assist", "assists", "use", "used", "using",
        "uses", "help", "helps", "work", "works", "thing", "things", "way",
        "ways", "type", "types", "kind", "kinds", "value", "values", "result",
        "results",
    }
)
# Predicates that are inherently definitional/explanatory — such an edge earns
# a relevance point even without a subject-token match, so 'NLP uses X'-style
# relations survive even when the partner entity is a generic token.
_DEFINITIONAL_PREDICATES: frozenset[str] = frozenset(
    {
        "uses", "used_for", "part_of", "instance_of", "is_a", "type_of",
        "defines", "implements", "depends_on", "produces", "enables",
        "applies_to", "performs",
    }
)


def _edge_query_relevance(
    seed_entity: str,
    neighbor_entity: str,
    predicate: str,
    groups,
) -> int:
    """Query-relevance score for one typed edge.

    A SUBJECT MATCH IS REQUIRED: the edge's seed OR neighbor must alias-match a
    NON-generic query concept (e.g. 'nlp'), scoring +1 per matched concept. A
    definitional predicate adds +1 but ONLY on top of a subject match — it is
    never a standalone qualifier (otherwise 'machine learning --uses--> JavaScript'
    would pass on an NLP query purely because 'uses' is definitional). An edge
    with no subject match scores 0 and is dropped. This is the floodgate guard
    that replaces confidence-DESC catalog noise once the facts>=3 gate is gone;
    when the graph has NO query-relevant edges, the decoration is correctly
    EMPTY rather than padded with tangential relations.
    """
    from services.retriever.query_grounding import group_matches_text

    subject_hits = 0
    for group in groups:
        if getattr(group, "key", "") in _GENERIC_QUERY_CONCEPTS:
            continue
        if group_matches_text(group, seed_entity) or group_matches_text(
            group, neighbor_entity
        ):
            subject_hits += 1
    if subject_hits == 0:
        return 0
    rel = subject_hits
    if str(predicate or "").strip().lower() in _DEFINITIONAL_PREDICATES:
        rel += 1
    return rel


def _query_rank_rows(rows: list[dict], query: str, top_k: int) -> list[dict]:
    """Keep the top_k most query-relevant edge rows (GERG).

    Ranks candidate edges by (query_relevance, edge_weight) and keeps only
    edges clearing relevance >= 1. Returns [] when nothing is query-relevant —
    so on a query with no matching typed structure the decoration is correctly
    ABSENT rather than padded with confidence-ranked noise. If the query yields
    no non-generic concept to anchor on, falls back to the edge_weight order
    (avoids over-pruning to empty on a purely generic query).
    """
    from services.retriever.query_grounding import concept_groups

    groups = concept_groups(query or "")
    non_generic = [
        g for g in groups
        if getattr(g, "key", "") not in _GENERIC_QUERY_CONCEPTS
    ]
    if not non_generic:
        return rows[:top_k]
    scored: list[tuple[int, float, dict]] = []
    for row in rows:
        rel = _edge_query_relevance(
            str(row.get("seed_entity") or ""),
            str(row.get("neighbor_entity") or ""),
            str(row.get("predicate") or ""),
            groups,
        )
        if rel >= 1:
            scored.append((rel, float(row.get("edge_weight") or 0.0), row))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [row for _, _, row in scored[:top_k]]


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
        db=None,
        query: Optional[str] = None,
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

        relates_to_decorations = await self._decorate_via_relates_to(
            winning_chunk_ids=winning_chunk_ids,
            corpus_ids=corpus_ids,
            wanted_families=wanted_families,
            neighbor_limit=neighbor_limit,
            chunks_per_neighbor=chunks_per_neighbor,
            query=query,
        )
        calls_decorations = await self._decorate_via_calls(
            winning_chunk_ids=winning_chunk_ids,
            corpus_ids=corpus_ids,
            neighbor_limit=neighbor_limit,
            chunks_per_neighbor=chunks_per_neighbor,
        )

        # Concat; both lists already capped individually. The downstream
        # arrow renderer in context_manager has its own per-chunk and total
        # arrow budgets (Pt 10d.1), so over-counting here is safe — the
        # renderer trims.
        all_decorations = relates_to_decorations + calls_decorations

        # Phase 5b — additive cache annotation. Gated on the
        # RETRIEVAL_CACHE_DECORATION_METRICS flag. When the flag is on
        # AND a db handle was passed AND the cache row exists for a
        # corpus, each decoration gets:
        #   seed_betweenness / neighbor_betweenness   (from entity_betweenness dict)
        #   seed_pagerank / neighbor_pagerank         (from top_pagerank list)
        #   is_fragile_bridge                          (from fragile_bridges set)
        # Failure modes (no db, cache cold, lookup raises) leave the
        # decorations exactly as the Cypher returned them — base
        # behavior is preserved.
        if (
            getattr(self._settings, "RETRIEVAL_CACHE_DECORATION_METRICS", True)
            and db is not None
            and all_decorations
            and corpus_ids
        ):
            await self._annotate_from_cache(all_decorations, corpus_ids, db)

        return all_decorations

    async def _annotate_from_cache(
        self,
        decorations: List[GraphDecoration],
        corpus_ids: List[str],
        db,
    ) -> None:
        """Phase 5b — annotate decorations with cached structural metrics.

        Mutates each decoration in place. Best-effort: any failure (cache
        miss, deserialization error, mongo down) leaves the decorations
        unchanged. The base GraphDecoration shape never breaks.
        """
        try:
            from services.graph.analytics import (
                compute_corpus_change_signature,
                get_cached_metrics,
            )
        except ImportError as exc:
            logger.warning(
                "decorate_winners cache annotation: analytics import "
                "failed (%s) — skipping enrichment", exc,
            )
            return

        # Merge cache fields across all warm corpora — same pattern as
        # Phase 5a's multi-corpus PageRank merge. Higher score wins on
        # overlap; betweenness is corpus-scoped but entity_ids are
        # unique system-wide so we just take max.
        merged_betweenness: dict[str, float] = {}
        merged_pagerank: dict[str, float] = {}
        fragile_pairs: set[tuple[str, str]] = set()
        warm_corpora = 0
        for corpus_id in corpus_ids:
            try:
                sig = await compute_corpus_change_signature(db, corpus_id)
                metrics = await get_cached_metrics(db, corpus_id, sig)
                if metrics is None:
                    continue
                # Sparse-graph guard — entity_betweenness, top_pagerank,
                # and fragile_bridges are all empty / uniform on a 0-edge
                # graph. Skip the per-field loops to avoid no-op iteration.
                # The base decoration (predicate + relation_family + edge
                # evidence from Cypher) is unaffected — only annotations
                # are skipped.
                if int(getattr(metrics, "edge_count", 0) or 0) == 0:
                    logger.debug(
                        "decorate_winners cache annotation: corpus=%s "
                        "edge_count=0 — skipping annotation merge",
                        corpus_id,
                    )
                    continue
                warm_corpora += 1
                for eid, score in (
                    getattr(metrics, "entity_betweenness", None) or {}
                ).items():
                    try:
                        cur = merged_betweenness.get(eid, 0.0)
                        val = float(score)
                        if val > cur:
                            merged_betweenness[eid] = val
                    except (TypeError, ValueError):
                        continue
                for entry in getattr(metrics, "top_pagerank", None) or []:
                    eid = entry.get("entity_id")
                    try:
                        val = float(entry.get("score") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if eid and val > merged_pagerank.get(eid, 0.0):
                        merged_pagerank[eid] = val
                for fb in getattr(metrics, "fragile_bridges", None) or []:
                    src = fb.get("source")
                    tgt = fb.get("target")
                    if src and tgt:
                        # Store both directions so lookup is symmetric.
                        fragile_pairs.add((src, tgt))
                        fragile_pairs.add((tgt, src))
            except Exception as exc:
                logger.debug(
                    "decorate_winners cache annotation: corpus=%s miss: %s",
                    corpus_id, exc,
                )

        if warm_corpora == 0:
            # No cache hits across any corpus — nothing to annotate.
            return

        annotated_count = 0
        fragile_count = 0
        for d in decorations:
            seed_id = (d.seed_entity_id or "").strip()
            neighbor_id = (d.neighbor_entity_id or "").strip()
            if seed_id and seed_id in merged_betweenness:
                d.seed_betweenness = merged_betweenness[seed_id]
                annotated_count += 1
            if neighbor_id and neighbor_id in merged_betweenness:
                d.neighbor_betweenness = merged_betweenness[neighbor_id]
            if seed_id and seed_id in merged_pagerank:
                d.seed_pagerank = merged_pagerank[seed_id]
            if neighbor_id and neighbor_id in merged_pagerank:
                d.neighbor_pagerank = merged_pagerank[neighbor_id]
            if seed_id and neighbor_id and (seed_id, neighbor_id) in fragile_pairs:
                d.is_fragile_bridge = True
                fragile_count += 1

        logger.info(
            "decorate_winners cache annotation: warm_corpora=%d arrows=%d "
            "annotated=%d fragile=%d (between_dict=%d, pr_lookup=%d)",
            warm_corpora,
            len(decorations),
            annotated_count,
            fragile_count,
            len(merged_betweenness),
            len(merged_pagerank),
        )

    async def _decorate_via_relates_to(
        self,
        *,
        winning_chunk_ids: List[str],
        corpus_ids: Optional[List[str]],
        wanted_families: Optional[List[str]],
        neighbor_limit: int,
        chunks_per_neighbor: int,
        query: Optional[str] = None,
    ) -> List[GraphDecoration]:
        cypher = """
        // Pt 10d — graph decoration over winners (RELATES_TO walk)
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
             // Phase 5b — entity_ids surfaced so the cache annotation
             // step can look up entity_betweenness + top_pagerank.
             coalesce(seed.entity_id, '')      AS seed_entity_id,
             coalesce(neighbor.entity_id, '')  AS neighbor_entity_id,
             coalesce(r.predicate, '')        AS predicate,
             coalesce(r.relation_family, '')  AS relation_family,
             // GERG: prefer the (reliably-populated) evidence_phrases[0] over
             // the often-empty singular evidence_phrase, so the rendered edge
             // carries real justification text the LLM can ground on.
             coalesce(r.evidence_phrases[0], r.evidence_phrase, '') AS edge_evidence,
             coalesce(r.direction_repaired, false) AS direction_repaired,
             coalesce(r.predicate_refined,  false) AS predicate_refined,
             edge_weight,
             collect(DISTINCT {
                 chunk_id: evidence.chunk_id,
                 doc_id: evidence.doc_id,
                 parent_boost: parent_boost
             })[..$chunks_per_neighbor] AS evidence_chunks
        ORDER BY edge_weight DESC
        RETURN winner_chunk_id, seed_entity, neighbor_entity,
               seed_entity_id, neighbor_entity_id,
               predicate, relation_family, edge_evidence,
               direction_repaired, predicate_refined,
               edge_weight, evidence_chunks
        LIMIT $neighbor_limit
        """

        # Pt 10d.1 — latency log. Without this we're blind to decoration
        # speed in production. The decoration runs on every chat turn that
        # isn't already short-circuited by the Facts gate; latency above
        # ~200ms p95 is the threshold where users start to feel it.
        t0 = time.perf_counter()
        try:
            # GERG: when query-ranking, fetch a wide candidate pool ($neighbor_limit
            # is bound to a larger fetch size) and select the most query-relevant
            # edges in Python — the Cypher only knows edge_weight. Without a query,
            # keep the original edge_weight top-k (back-compat).
            _fetch_limit = max(int(neighbor_limit), 200) if query else int(neighbor_limit)
            async with self._driver.session() as session:
                result = await session.run(
                    cypher,
                    winning_chunk_ids=winning_chunk_ids,
                    corpus_ids=corpus_ids or [],
                    wanted_families=wanted_families or [],
                    neighbor_limit=_fetch_limit,
                    chunks_per_neighbor=int(chunks_per_neighbor),
                )
                rows = [dict(r) async for r in result]

            if query:
                rows = _query_rank_rows(rows, query, int(neighbor_limit))
            else:
                rows = rows[: int(neighbor_limit)]
            decorations = self._rows_to_decorations(rows)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "decorate_winners[RELATES_TO] ms=%.1f winners=%d arrows=%d "
                "neighbor_limit=%d chunks_per_neighbor=%d families=%s",
                elapsed_ms,
                len(winning_chunk_ids),
                len(decorations),
                neighbor_limit,
                chunks_per_neighbor,
                wanted_families or "[]",
            )
            return decorations
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning(
                "decorate_winners[RELATES_TO] FAILED ms=%.1f winners=%d "
                "(chunk-only fallback): %s",
                elapsed_ms, len(winning_chunk_ids), exc,
            )
            return []

    async def _decorate_via_calls(
        self,
        *,
        winning_chunk_ids: List[str],
        corpus_ids: Optional[List[str]],
        neighbor_limit: int,
        chunks_per_neighbor: int,
    ) -> List[GraphDecoration]:
        """Phase 4.5 — emit decorations for graphify-emitted CALLS edges.

        Mirrors `_decorate_via_relates_to` but walks `:CALLS` (entity→entity,
        from `graphify` extractor) instead of `:RELATES_TO` (from Ghost B).
        CALLS edges are deterministic — every one is treated as 'strong'
        (no `edge_strength` gate, no `eligible_for_synthesis` check). The
        emitted GraphDecoration carries `predicate='calls'` and
        `relation_family='code_call_graph'`, which the existing arrow
        renderer in context_manager prints as
        `seed --calls(code_call_graph)--> neighbor` in the LLM prompt.
        """
        if not self._settings.NEO4J_ENABLED or not self._driver:
            return []

        cypher = """
        // Phase 4.5 — graph decoration over winners (CALLS walk)
        MATCH (winner:Chunk)-[:MENTIONS]->(seed:Entity)-[r:CALLS]-(neighbor:Entity)
        WHERE winner.chunk_id IN $winning_chunk_ids
          AND seed <> neighbor
        """
        if corpus_ids:
            cypher += "  AND any(cid IN $corpus_ids WHERE cid IN coalesce(r.corpus_ids, []))\n"
        cypher += """
        WITH winner, seed, neighbor, r,
             coalesce(r.confidence, 1.0) AS edge_weight
        OPTIONAL MATCH (neighbor)<-[:MENTIONS]-(evidence:Chunk)
        WHERE (size($corpus_ids) = 0 OR evidence.corpus_id IN $corpus_ids)
          AND evidence.chunk_id <> winner.chunk_id
        WITH winner, seed, neighbor, r, edge_weight, evidence,
             CASE WHEN evidence.doc_id = winner.doc_id THEN 2 ELSE 1 END AS parent_boost
        ORDER BY parent_boost DESC, edge_weight DESC
        WITH winner.chunk_id              AS winner_chunk_id,
             coalesce(seed.display_name, seed.normalized_name, '')         AS seed_entity,
             coalesce(neighbor.display_name, neighbor.normalized_name, '') AS neighbor_entity,
             // Phase 5b — entity_ids for cache annotation lookup.
             coalesce(seed.entity_id, '')      AS seed_entity_id,
             coalesce(neighbor.entity_id, '')  AS neighbor_entity_id,
             'calls'           AS predicate,
             'code_call_graph' AS relation_family,
             // Use source_file:source_location as evidence — graphify writes both.
             coalesce(r.source_file, '') + CASE WHEN r.source_location IS NULL THEN '' ELSE ':' + r.source_location END AS edge_evidence,
             false AS direction_repaired,
             false AS predicate_refined,
             edge_weight,
             collect(DISTINCT {
                 chunk_id: evidence.chunk_id,
                 doc_id: evidence.doc_id,
                 parent_boost: parent_boost
             })[..$chunks_per_neighbor] AS evidence_chunks
        ORDER BY edge_weight DESC
        RETURN winner_chunk_id, seed_entity, neighbor_entity,
               seed_entity_id, neighbor_entity_id,
               predicate, relation_family, edge_evidence,
               direction_repaired, predicate_refined,
               edge_weight, evidence_chunks
        LIMIT $neighbor_limit
        """

        t0 = time.perf_counter()
        try:
            async with self._driver.session() as session:
                result = await session.run(
                    cypher,
                    winning_chunk_ids=winning_chunk_ids,
                    corpus_ids=corpus_ids or [],
                    neighbor_limit=int(neighbor_limit),
                    chunks_per_neighbor=int(chunks_per_neighbor),
                )
                rows = [dict(r) async for r in result]

            decorations = self._rows_to_decorations(rows)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "decorate_winners[CALLS] ms=%.1f winners=%d arrows=%d",
                elapsed_ms, len(winning_chunk_ids), len(decorations),
            )
            return decorations
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning(
                "decorate_winners[CALLS] FAILED ms=%.1f winners=%d (skipping): %s",
                elapsed_ms, len(winning_chunk_ids), exc,
            )
            return []

    @staticmethod
    def _rows_to_decorations(rows: list[dict]) -> List[GraphDecoration]:
        """Shared row → GraphDecoration translation. Used by both the
        RELATES_TO and CALLS query paths."""
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
                    # Phase 5b — entity_ids carried alongside display names
                    # so the cache annotation step can look up structural
                    # metrics. Default to empty string when older corpora
                    # don't have entity_id populated.
                    seed_entity_id=str(row.get("seed_entity_id") or ""),
                    neighbor_entity_id=str(row.get("neighbor_entity_id") or ""),
                    predicate=str(row.get("predicate") or ""),
                    relation_family=str(row.get("relation_family") or ""),
                    edge_evidence=str(row.get("edge_evidence") or ""),
                    direction_repaired=bool(row.get("direction_repaired") or False),
                    predicate_refined=bool(row.get("predicate_refined") or False),
                    edge_weight=float(row.get("edge_weight") or 0.0),
                    evidence_chunks=evidence_chunks,
                    # Annotations land in _annotate_from_cache; defaults
                    # here so cold-cache decorations have well-defined
                    # values.
                )
            )
        return decorations

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()


# Module-level singleton (mirrors mode_a_expansion + fact_retrieval lifecycle).
graph_decorator = GraphDecorator()
