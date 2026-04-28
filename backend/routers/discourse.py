"""
Discourse read API — Phase 17 Wave 2.

Endpoint:
    GET /api/corpora/{corpus_id}/discourse
        ?top_terms=80&min_cooccur=3&chunk_limit=2000

Computes a term co-occurrence graph on-the-fly from MongoDB chunks for the
corpus, then runs structural analytics (clusters + bridges + gaps + shape).
No Neo4j dependency — the endpoint works even when NEO4J_ENABLED=false.

The Mongo `(corpus_id, chunk_id)` compound index (created in `db/indexes.py`)
already covers the read pattern; no new index is required.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from models.schemas import DiscourseGraphResponse
from routers.auth import get_current_user
from services.discourse import build_discourse
from services.ingestion_service import ingestion_service

router = APIRouter(prefix="/api/corpora", tags=["discourse"])


@router.get(
    "/{corpus_id}/discourse",
    response_model=DiscourseGraphResponse,
    summary="Build the corpus discourse graph (lexeme co-occurrence)",
)
async def get_discourse(
    corpus_id: str,
    top_terms: int = Query(
        default=80,
        ge=10,
        le=300,
        description="Number of highest-frequency terms to keep as nodes",
    ),
    min_cooccur: int = Query(
        default=3,
        ge=1,
        le=50,
        description="Minimum number of chunks two terms must co-occur in to form an edge",
    ),
    chunk_limit: int = Query(
        default=2000,
        ge=100,
        le=10000,
        description="Max child chunks to scan (bounds worst-case runtime)",
    ),
    current_user: dict = Depends(get_current_user),
):
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")

    db = ingestion_service._db
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is not connected — discourse graph unavailable",
        )

    result = await build_discourse(
        db=db,
        corpus_id=corpus_id,
        top_terms=top_terms,
        min_cooccur=min_cooccur,
        chunk_limit=chunk_limit,
    )
    return result
