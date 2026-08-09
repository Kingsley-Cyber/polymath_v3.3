"""CorpusCoordinator — factory execution plane, station B.

Owner-ratified (2026-08-08): stop scheduling documents, schedule work.
CORPUS = scheduling/throughput unit · DOCUMENT = semantic correctness unit ·
WINDOW = batching unit · MODEL = warm residency owner.

The coordinator overlaps document pipelines under a bounded concurrency
budget while every model stays a single warm owner:

    GLiNER2   — one warm instance, inference-locked, windows batched
    OpenIE    — N-process warm farm, units from ANY document keep it fed
    spaCy     — one shared Language; only the parse section serializes
    compilers — pure Python, overlap freely across documents
    writers   — async Mongo, per-document batches

Correctness contract (the owner's hard rule): parallelism changes WHEN work
happens, never WHAT semantic output exists. Each document's pipeline is
deterministic and isolated; the corpus digest — the ordered per-document
identity digests — must be identical for max_active=1 and max_active=N
(verified by scripts/run_corpus_factory.py).

Backpressure exists only against resource exhaustion (the bounded document
budget), never to reserve capacity for queries.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Sequence

from models.graphify_contracts import stable_digest

logger = logging.getLogger(__name__)

CORPUS_COORDINATOR_RELEASE = "graphify-corpus-coordinator-v1"


def default_max_active_documents() -> int:
    raw = os.environ.get("GRAPHIFY_MAX_ACTIVE_DOCS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning("GRAPHIFY_MAX_ACTIVE_DOCS=%r is not an int; using auto", raw)
    return max(1, min(3, (os.cpu_count() or 2) // 3))


@dataclass(frozen=True)
class CorpusDocument:
    doc_id: str
    text: str
    children: Sequence[Any]


@dataclass(frozen=True)
class CorpusFactoryResult:
    corpus_digest: str
    per_document: tuple[dict[str, Any], ...]
    report: dict[str, Any]


async def run_corpus_factory(
    *,
    db,
    corpus_id: str,
    documents: Sequence[CorpusDocument],
    provider,
    max_active: int | None = None,
) -> CorpusFactoryResult:
    """Run every document's Graphify pipeline under a bounded overlap budget."""
    from services.extraction.graphify_pipeline import run_graphify_pipeline

    budget = max_active or default_max_active_documents()
    semaphore = asyncio.Semaphore(budget)
    started = time.perf_counter()
    waiting = 0
    peak_active = 0
    active = 0
    lock = asyncio.Lock()
    rows: dict[str, dict[str, Any]] = {}

    async def run_one(document: CorpusDocument) -> None:
        nonlocal waiting, active, peak_active
        waiting += 1
        async with semaphore:
            waiting -= 1
            async with lock:
                active += 1
                peak_active = max(peak_active, active)
            doc_started = time.perf_counter()
            try:
                output = await run_graphify_pipeline(
                    db=db,
                    corpus_id=corpus_id,
                    doc_id=document.doc_id,
                    text=document.text,
                    children=document.children,
                    provider=provider,
                )
                rows[document.doc_id] = {
                    "doc_id": document.doc_id,
                    "status": "passed",
                    "identity_digest": output.identity_digest,
                    "elapsed_seconds": time.perf_counter() - doc_started,
                    "resumed_stages": len(output.resumed_stages),
                }
            except Exception as exc:  # noqa: BLE001 — one document never sinks a corpus
                rows[document.doc_id] = {
                    "doc_id": document.doc_id,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                    "elapsed_seconds": time.perf_counter() - doc_started,
                }
            finally:
                async with lock:
                    active -= 1

    await asyncio.gather(*(run_one(document) for document in documents))
    elapsed = time.perf_counter() - started

    # Deterministic corpus identity: per-document digests in input order.
    per_document = tuple(rows[document.doc_id] for document in documents)
    corpus_digest = stable_digest([
        {"doc_id": row["doc_id"], "digest": row.get("identity_digest", ""), "status": row["status"]}
        for row in per_document
    ])
    passed = sum(row["status"] == "passed" for row in per_document)
    document_seconds = sum(row["elapsed_seconds"] for row in per_document)
    report = {
        "schema_version": "polymath.corpus_factory_report.v1",
        "coordinator_release": CORPUS_COORDINATOR_RELEASE,
        "documents": len(documents),
        "passed": passed,
        "failed": len(documents) - passed,
        "max_active_documents": budget,
        "peak_active_documents": peak_active,
        "wall_seconds": elapsed,
        "document_seconds": document_seconds,
        "overlap_factor": (document_seconds / elapsed) if elapsed > 0 else 1.0,
        "documents_per_minute": (len(documents) / elapsed * 60) if elapsed > 0 else 0.0,
    }
    return CorpusFactoryResult(corpus_digest, per_document, report)
