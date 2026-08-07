"""Control Plane V2 router — run ledger + readiness proof endpoints.

Endpoints:
  GET  /api/runs/{run_id}                     — authoritative per-doc proof
  GET  /api/corpora/{corpus_id}/runs          — run ledger for a corpus
  GET  /api/corpora/{corpus_id}/readiness-proof — aggregate corpus proof
  POST /api/corpora/{corpus_id}/reconcile     — manual reconcile tick

The proof responses carry no interpretable completion booleans other than
`query_ready`, which is derivable only from an issued certificate.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from routers.auth import get_current_user
from services.control_plane import ledger
from services.control_plane.certificate import (
    corpus_readiness_proof,
    doc_readiness_proof,
)
from services.ingestion_service import ingestion_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["control-plane"])


def _db() -> Any:
    db = getattr(ingestion_service, "_db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="database not ready")
    return db


async def _require_corpus(corpus_id: str, user_id: str) -> dict[str, Any]:
    corpus = await _db()["corpora"].find_one(
        {"corpus_id": corpus_id, "user_id": user_id},
        {"_id": 0, "corpus_id": 1, "user_id": 1},
    )
    if not corpus:
        raise HTTPException(status_code=404, detail="corpus not found")
    return corpus


@router.get("/runs/{run_id}")
async def get_run_proof(
    run_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the run row plus a live, recomputed readiness proof."""

    db = _db()
    run = await ledger.get_run(db, run_id=run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    corpus_id = str(run.get("corpus_id") or "")
    await _require_corpus(corpus_id, current_user["user_id"])
    proof = await doc_readiness_proof(
        db,
        getattr(ingestion_service, "_qdrant", None),
        corpus_id=corpus_id,
        doc_id=str(run.get("doc_id") or ""),
    )
    await ledger.update_run_from_proof(db, run_id=run_id, proof=proof)
    run = await ledger.get_run(db, run_id=run_id) or run
    return {"run": run, "proof": proof}


@router.get("/corpora/{corpus_id}/runs")
async def list_corpus_runs(
    corpus_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    await _require_corpus(corpus_id, current_user["user_id"])
    runs = await ledger.list_runs(
        _db(), corpus_id=corpus_id, status=status, limit=limit
    )
    return {"corpus_id": corpus_id, "runs": runs, "count": len(runs)}


@router.get("/corpora/{corpus_id}/readiness-proof")
async def get_corpus_readiness_proof(
    corpus_id: str,
    max_docs: int = Query(default=500, ge=1, le=5000),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    await _require_corpus(corpus_id, current_user["user_id"])
    return await corpus_readiness_proof(
        _db(),
        getattr(ingestion_service, "_qdrant", None),
        corpus_id=corpus_id,
        max_docs=max_docs,
    )


@router.post("/corpora/{corpus_id}/reconcile")
async def reconcile_corpus_now(
    corpus_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Manually trigger one reconcile cycle for this corpus."""

    from services.control_plane.reconciler import reconcile_corpus

    await _require_corpus(corpus_id, current_user["user_id"])
    try:
        return await reconcile_corpus(
            _db(),
            ingestion_service=ingestion_service,
            corpus_id=corpus_id,
            user_id=current_user["user_id"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("manual reconcile failed corpus=%s", corpus_id)
        raise HTTPException(
            status_code=500, detail=f"reconcile failed: {type(exc).__name__}"
        ) from exc
