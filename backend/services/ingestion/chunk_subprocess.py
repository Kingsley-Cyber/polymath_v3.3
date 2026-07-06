"""Lean chunk-stage subprocess entrypoint.

Spawn workers import THIS module, not services.ingestion.worker — the worker
module's import graph (~1GB RSS per child: ghost clients, qdrant, embedder,
schema machinery) is what OOM-killed pool children inside the 10g container
on 2026-07-06 (407 docs mass-failed: a killed child bricks a
ProcessPoolExecutor permanently). Children here import only tier_chunker and
its direct deps.
"""

from __future__ import annotations


def chunk_entry(parse_result, doc_id, corpus_id, config):
    from services.ingestion import tier_chunker

    return tier_chunker.chunk(
        parse_result=parse_result,
        doc_id=doc_id,
        corpus_id=corpus_id,
        config=config,
    )
