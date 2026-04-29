"""
Graph read API — Phase 9 Extraction endpoints + Phase 17 Wave 1 Discovery.

All routes are corpus-scoped and require Neo4j to be enabled.

Phase 17 adds `POST /api/graph/query` — the Agent Query backend that powers
the "Agent Query" tab in GraphView. Unlike the `/api/corpora/{id}/...` reads
below, discovery query is mounted under a separate `/api/graph` prefix
because it's not scoped to a single corpus in the URL path (corpus_id is in
the request body).
"""
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from routers.auth import get_current_user
from models.schemas import (
    ChunkExtractionResponse,
    DocExtractionItem,
    EntitySearchRequest,
    EntitySearchResponse,
    EntityResult,
    GraphAnalyzeRequest,
    GraphAnalyzeResponse,
    GraphDiscoverRequest,
    GraphDiscoverResponse,
    GraphDiscoverSession,
    GraphDiscoverSessionDetail,
    GraphResumeCandidateRequest,
    GraphResumeCandidateResponse,
    GraphSuggestionsResponse,
    GraphQueryRequest,
    GraphQueryResponse,
    RelationEdge,
)
from services.ingestion_service import ingestion_service

router = APIRouter(prefix="/api/corpora", tags=["graph"])

# Second router for the discovery endpoint — `/api/graph/query` lives at a
# different prefix than the per-corpus extraction reads. Both routers get
# registered in main.py.
discovery_router = APIRouter(prefix="/api/graph", tags=["graph-discovery"])


def _require_neo4j():
    """Return the Neo4j driver or raise 503 if not enabled."""
    driver = ingestion_service.neo4j_driver
    if driver is None:
        raise HTTPException(
            status_code=503,
            detail="Neo4j is not enabled on this server.",
        )
    return driver


@router.get(
    "/{corpus_id}/graph/overview",
    summary="Cached supernode graph overview for a corpus",
)
async def get_graph_overview(
    corpus_id: str,
    max_concepts: int = Query(default=80, ge=1, le=500),
    max_edges: int = Query(default=220, ge=1, le=2000),
):
    """Return a small cached domain/concept graph for the canvas.

    This is the scalable default view: it never streams the full entity graph
    to the browser and never runs corpus-scale graph algorithms at request
    time. If analytics are stale/missing, the response is an empty
    cache_warming payload.
    """
    db = ingestion_service.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    from services.graph.overview import get_cached_graph_overview

    return await get_cached_graph_overview(
        db,
        corpus_id,
        max_concepts=max_concepts,
        max_edges=max_edges,
    )


@router.get(
    "/{corpus_id}/graph/full",
    summary="Full entity + relation graph for a corpus (WebGL viewer)",
)
async def get_full_graph(
    corpus_id: str,
    max_nodes: int = Query(default=20000, ge=1, le=50000),
    max_edges: int = Query(default=60000, ge=1, le=200000),
):
    """Returns the complete entity graph scoped to a corpus in a shape ready
    for client-side WebGL rendering (sigma.js + graphology).

    Response:
        {
          "nodes": [ {id, display_name, entity_type, mention_count}, ... ],
          "edges": [ {source, target, predicate, confidence}, ... ],
          "truncated": bool
        }
    """
    driver = _require_neo4j()
    from services.graph.neo4j_reader import get_full_corpus_graph

    return await get_full_corpus_graph(
        driver, corpus_id, max_nodes=max_nodes, max_edges=max_edges
    )


@router.get(
    "/{corpus_id}/entities",
    response_model=list[EntityResult],
    summary="Search entities in a corpus",
)
async def list_entities(
    corpus_id: str,
    q: str = Query(default="", description="Substring filter on entity name"),
    limit: int = Query(default=20, ge=1, le=200),
    doc_id: Optional[str] = Query(default=None, description="Narrow to a single document"),
):
    driver = _require_neo4j()
    from services.graph.neo4j_reader import get_entities

    rows = await get_entities(driver, corpus_id, q=q, limit=limit, doc_id=doc_id)
    return rows


@router.get(
    "/{corpus_id}/chunks/{chunk_id}/extraction",
    response_model=ChunkExtractionResponse,
    summary="Get all entities and relations extracted from a chunk",
)
async def get_chunk_extraction(corpus_id: str, chunk_id: str):
    driver = _require_neo4j()
    from services.graph.neo4j_reader import get_chunk_extraction

    result = await get_chunk_extraction(driver, corpus_id, chunk_id)
    return result


@router.get(
    "/{corpus_id}/documents/{doc_id}/extraction",
    response_model=list[DocExtractionItem],
    summary="Get per-chunk extraction counts for a document",
)
async def get_doc_extraction(corpus_id: str, doc_id: str):
    driver = _require_neo4j()
    from services.graph.neo4j_reader import get_doc_extraction_summary

    rows = await get_doc_extraction_summary(driver, corpus_id, doc_id)
    return rows


@router.get(
    "/{corpus_id}/entities/{entity_id}/relations",
    response_model=list[RelationEdge],
    summary="Get outgoing and incoming relations for an entity",
)
async def get_entity_relations(
    corpus_id: str,
    entity_id: str,
    limit: int = Query(default=20, ge=1, le=200),
):
    driver = _require_neo4j()
    from services.graph.neo4j_reader import get_entity_relations

    rows = await get_entity_relations(
        driver, corpus_id, entity_id=entity_id, limit=limit
    )
    return rows


# ────────────────────────────────────────────────────────────────────────────
# Phase 17 Wave 1 — Agent Query discovery endpoint
# ────────────────────────────────────────────────────────────────────────────


@discovery_router.post(
    "/query",
    response_model=GraphQueryResponse,
    summary="Agent Query: extract entities from question, expand subgraph, find bridges/hubs/gaps",
)
async def graph_query(body: GraphQueryRequest = Body(...)) -> GraphQueryResponse:
    """
    Phase 17 Wave 1 — the backend for the "Agent Query" tab in GraphView.

    Flow:
      1. Tokenize the query and match tokens against Entity nodes in this corpus
      2. N-hop RELATES_TO expansion from matched seeds
      3. Bridge detection: entities connected to ≥2 seeds (no shortestPath — Community-safe)
      4. Hub detection: Python degree count on the returned subgraph
      5. Gap detection: seed pairs with no direct RELATES_TO edge

    Returns the discovery payload for the frontend's DiscoveryPanel + canvas.
    """
    driver = _require_neo4j()
    from services.graph.graph_query import (
        expand_subgraph,
        extract_query_entities,
        find_bridges,
        find_gaps,
        find_hubs,
    )

    # Step 1 — seed entities
    seeds = await extract_query_entities(body.query, body.corpus_id, driver)
    if not seeds:
        # Return an empty-but-well-formed response; the UI will render a
        # friendly "no entities matched" state.
        return GraphQueryResponse(
            nodes=[],
            links=[],
            bridges=[],
            hubs=[],
            gaps=[],
            seed_entities=[],
        )

    seed_ids = [s["entity_id"] for s in seeds]

    # Step 2 — expand subgraph
    subgraph = await expand_subgraph(
        entity_ids=seed_ids,
        corpus_id=body.corpus_id,
        driver=driver,
        max_hops=body.max_hops,
        limit=body.limit,
    )
    nodes = subgraph["nodes"]
    links = subgraph["links"]

    # Step 3 — bridges
    bridges = await find_bridges(
        driver=driver,
        entity_ids=seed_ids,
        corpus_id=body.corpus_id,
        max_hops=body.max_hops,
    )

    # Step 4 — hubs (Python, on the subgraph we already have)
    hubs = find_hubs(nodes, links)

    # Step 5 — gaps (between seed pairs only — not all subgraph pairs)
    gaps = await find_gaps(driver=driver, entity_ids=seed_ids)

    return GraphQueryResponse(
        nodes=nodes,
        links=links,
        bridges=bridges,
        hubs=hubs,
        gaps=gaps,
        seed_entities=[
            {
                "id": s["entity_id"],
                "display_name": s.get("display_name", ""),
                "entity_type": s.get("entity_type", "other"),
                "mention_count": s.get("mention_count", 0),
                "is_seed": True,
            }
            for s in seeds
        ],
    )


# ────────────────────────────────────────────────────────────────────────────
# Phase 17 Wave 3 — LLM structural synthesis (Agent Query → Analysis section)
# ────────────────────────────────────────────────────────────────────────────


@discovery_router.post(
    "/analyze",
    response_model=GraphAnalyzeResponse,
    summary="Run LLM structural synthesis on a knowledge/discourse/split graph snapshot",
)
async def graph_analyze(body: GraphAnalyzeRequest = Body(...)) -> GraphAnalyzeResponse:
    """
    Phase 17 Wave 3 — narrates the STRUCTURE of the current Agent Query
    canvas. Accepts snapshots from the client so this stays stateless and
    doesn't re-run the expensive graph_query / discourse computations.

    Modes:
      - knowledge: needs `knowledge` snapshot (nodes + links + seed_ids)
      - discourse: needs `discourse` snapshot (nodes + links + clusters + bridges + gaps + shape)
      - split:     needs both snapshots

    Invariant: the LLM reads STRUCTURE (hubs, bridges, gaps, alignment), not
    raw document text. Any prompt built from chunks is rejected at the
    analyzer layer.
    """
    from services.graph.graph_analyzer import (
        synthesize_discourse,
        synthesize_knowledge,
        synthesize_split,
    )

    mode = (body.mode or "").strip().lower()
    if mode == "knowledge":
        if not body.knowledge or not body.knowledge.nodes:
            raise HTTPException(
                status_code=400,
                detail="Knowledge mode requires a non-empty `knowledge` snapshot",
            )
        result = await synthesize_knowledge(
            query=body.query,
            nodes=body.knowledge.nodes,
            links=body.knowledge.links,
            seed_ids=body.knowledge.seed_ids,
            model=body.model,
        )
        return GraphAnalyzeResponse(
            mode="knowledge",
            markdown=result["markdown"],
            structural_summary=result["structural_summary"],
            handoff_prompt=result["handoff_prompt"],
        )

    if mode == "discourse":
        if not body.discourse or not body.discourse.nodes:
            raise HTTPException(
                status_code=400,
                detail="Discourse mode requires a non-empty `discourse` snapshot",
            )
        result = await synthesize_discourse(
            nodes=body.discourse.nodes,
            links=body.discourse.links,
            clusters=body.discourse.clusters,
            bridges=body.discourse.bridges,
            gaps=body.discourse.gaps,
            shape=body.discourse.shape,
            model=body.model,
        )
        return GraphAnalyzeResponse(
            mode="discourse",
            markdown=result["markdown"],
            structural_summary=result["structural_summary"],
            handoff_prompt=result["handoff_prompt"],
        )

    if mode == "split":
        if (
            not body.knowledge
            or not body.discourse
            or not body.knowledge.nodes
            or not body.discourse.nodes
        ):
            raise HTTPException(
                status_code=400,
                detail="Split mode requires both `knowledge` and `discourse` snapshots",
            )
        result = await synthesize_split(
            query=body.query,
            knowledge_nodes=body.knowledge.nodes,
            knowledge_links=body.knowledge.links,
            seed_ids=body.knowledge.seed_ids,
            discourse_nodes=body.discourse.nodes,
            discourse_links=body.discourse.links,
            clusters=body.discourse.clusters,
            bridges=body.discourse.bridges,
            gaps=body.discourse.gaps,
            shape=body.discourse.shape,
            model=body.model,
        )
        return GraphAnalyzeResponse(
            mode="split",
            markdown=result["markdown"],
            structural_summary=result["structural_summary"],
            overlay=result.get("overlay"),
            handoff_prompt=result["handoff_prompt"],
        )

    raise HTTPException(
        status_code=400,
        detail=f"Unknown mode '{body.mode}' — must be knowledge | discourse | split",
    )


# ────────────────────────────────────────────────────────────────────────────
# Mode B — Entity-first retrieval (was orphan until this endpoint)
# ────────────────────────────────────────────────────────────────────────────


@discovery_router.post(
    "/entity-search",
    response_model=EntitySearchResponse,
    summary="Entity-first search: match Entity nodes, return mentioning chunks",
)
async def entity_search(body: EntitySearchRequest = Body(...)) -> EntitySearchResponse:
    """
    Mode B search — finds chunks that mention entities matching the query string.
    Ranks by summed MENTIONS.confidence across matched entities. Optionally hydrates
    parent text + corpus/doc names from MongoDB.
    """
    _require_neo4j()
    from services.retriever.mode_b import mode_b_expansion
    from services.retriever.hydrate import hydrate_chunks

    chunks = await mode_b_expansion.search(
        query=body.query,
        corpus_ids=body.corpus_ids,
        limit=body.limit,
    )

    if body.hydrate and chunks:
        try:
            chunks = await hydrate_chunks(chunks, body.corpus_ids)
        except Exception as exc:
            # Hydration failure should not fail the whole request —
            # return the unhydrated chunks so the UI can still render IDs.
            import logging
            logging.getLogger(__name__).warning(
                "Entity search hydration failed, returning unhydrated: %s", exc
            )

    return EntitySearchResponse(chunks=chunks, neo4j_enabled=True)


# ═══════════════════════════════════════════════════════════════════════════
# Mission Control — cross-domain synthesis (P3)
# ═══════════════════════════════════════════════════════════════════════════


@discovery_router.post("/discover", response_model=GraphDiscoverResponse)
async def graph_discover(
    body: GraphDiscoverRequest,
    current_user: dict = Depends(get_current_user),
) -> GraphDiscoverResponse:
    """Mission Control synthesis — one-shot cross-domain analysis.

    Cache-first: domain emergence + metrics served from Mongo when signatures
    match the live corpus state. Single LLM call per turn. Returns structured
    JSON for the seven Mission Control cards.
    """
    from services.graph.orchestrator import discover

    qdrant = ingestion_service.qdrant_client
    if qdrant is None:
        raise HTTPException(status_code=503, detail="Qdrant not connected")
    db = ingestion_service.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    neo4j = ingestion_service.neo4j_driver  # may be None — orchestrator tolerates

    # If the caller passed a session_id, verify they own it before reusing.
    if body.session_id:
        owner = await db["graph_sessions"].find_one(
            {"session_id": body.session_id},
            {"user_id": 1, "_id": 0},
        )
        if owner and owner.get("user_id") and owner["user_id"] != current_user["user_id"]:
            raise HTTPException(status_code=404, detail="Session not found")

    try:
        result = await discover(
            qdrant=qdrant,
            neo4j_driver=neo4j,
            db=db,
            corpus_id=body.corpus_id,
            query=body.query,
            mode=body.mode,
            session_id=body.session_id,
            user_id=current_user["user_id"],
            model_override=body.model,
            agentic=body.agentic,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("Discover failed: %s", exc)
        raise HTTPException(status_code=500, detail="Mission Control synthesis failed")

    return GraphDiscoverResponse(
        session_id=result.session_id,
        corpus_id=result.corpus_id,
        query=result.query,
        mode=result.mode,
        interpretation=result.interpretation,
        frontier=result.frontier,
        analogies=result.analogies,
        bridges=result.bridges,
        weak_links=result.weak_links,
        transfers=result.transfers,
        questions=result.questions,
        strategic_read=result.strategic_read,
        intent_profile=result.intent_profile,
        atomic_trace=result.atomic_trace,
        socratic_prompts=result.socratic_prompts,
        metrics=result.metrics,
        domain_map_summary=result.domain_map_summary,
        graph=result.graph,
        anchors=result.anchors,
        concept_communities=result.concept_communities,
        entity_concept_map=result.entity_concept_map,
        headline=result.headline,
        themes=result.themes,
        bridges_v2=result.bridges_v2,
        gaps_v2=result.gaps_v2,
        latent_topics=result.latent_topics,
        tensions=result.tensions,
        trace=result.trace,
        auto_synthesis=result.auto_synthesis,
        insight_packet_summary=result.insight_packet_summary,
        context_graph=result.context_graph,
    )


@discovery_router.get("/sessions", response_model=list[GraphDiscoverSession])
async def list_graph_sessions(
    corpus_id: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """List Mission Control sessions owned by the current user."""
    from services.graph.orchestrator import list_sessions as _list
    db = ingestion_service.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    sessions = await _list(db, corpus_id=corpus_id, user_id=current_user["user_id"])
    return [GraphDiscoverSession(**s) for s in sessions]


@discovery_router.post("/resume-candidate", response_model=GraphResumeCandidateResponse)
async def graph_resume_candidate(
    body: GraphResumeCandidateRequest,
    current_user: dict = Depends(get_current_user),
) -> GraphResumeCandidateResponse:
    """Find a prior Mission Control thread by query-embedding cosine similarity."""
    from services.graph.orchestrator import find_resume_candidate

    db = ingestion_service.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")

    candidate = await find_resume_candidate(
        db,
        corpus_id=body.corpus_id,
        query=body.query,
        user_id=current_user["user_id"],
        threshold=body.threshold,
    )
    if not candidate:
        return GraphResumeCandidateResponse()
    return GraphResumeCandidateResponse(
        session=GraphDiscoverSession(**candidate["session"]),
        score=candidate["score"],
    )


@discovery_router.get("/sessions/{session_id}", response_model=GraphDiscoverSessionDetail)
async def get_graph_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Restore a Mission Control session owned by the current user."""
    from services.graph.orchestrator import get_session as _get_session

    db = ingestion_service.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    session = await _get_session(db, session_id, user_id=current_user["user_id"])
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return GraphDiscoverSessionDetail(**session)


@discovery_router.get("/suggestions", response_model=GraphSuggestionsResponse)
async def graph_suggestions(
    corpus_id: str = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """Return corpus-seeded Mission Control suggestions without a synthesis turn."""
    from services.graph.orchestrator import build_corpus_suggestions

    qdrant = ingestion_service.qdrant_client
    if qdrant is None:
        raise HTTPException(status_code=503, detail="Qdrant not connected")
    db = ingestion_service.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")

    payload = await build_corpus_suggestions(
        qdrant=qdrant,
        neo4j_driver=ingestion_service.neo4j_driver,
        db=db,
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
    )
    return GraphSuggestionsResponse(**payload)


@discovery_router.delete("/sessions/{session_id}")
async def delete_graph_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete a Mission Control session. Only the owning user may delete."""
    from services.graph.orchestrator import delete_session as _delete
    db = ingestion_service.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    deleted = await _delete(db, session_id, user_id=current_user["user_id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": True}
