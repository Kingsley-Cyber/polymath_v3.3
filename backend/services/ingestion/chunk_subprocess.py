"""Lean chunk-stage subprocess entrypoint.

Spawn workers import THIS module, not services.ingestion.worker — the worker
module's import graph (~1GB RSS per child: ghost clients, qdrant, embedder,
schema machinery) is what OOM-killed pool children inside the 10g container
on 2026-07-06 (407 docs mass-failed: a killed child bricks a
ProcessPoolExecutor permanently). Children here import only tier_chunker and
its direct deps.
"""

from __future__ import annotations

import copy
import os


_PATHOLOGICAL_ENV = {
    "CHUNKER_SENTENCE_ENGINE": "regex",
    "CHUNKER_SEMANTIC_ESCALATION": "false",
    "CHUNKER_SEMANTIC_PARENTS": "false",
}


def _pathological_config(config):
    """Copy a corpus config while forcing the bounded greedy child splitter."""
    if isinstance(config, dict):
        fallback = dict(config)
        fallback["child_chunk_algorithm"] = "sentence_merge"
        return fallback
    if hasattr(config, "model_copy"):
        return config.model_copy(update={"child_chunk_algorithm": "sentence_merge"})
    fallback = copy.copy(config)
    setattr(fallback, "child_chunk_algorithm", "sentence_merge")
    return fallback


def chunk_entry(parse_result, doc_id, corpus_id, config):
    from services.ingestion import tier_chunker

    return tier_chunker.chunk(
        parse_result=parse_result,
        doc_id=doc_id,
        corpus_id=corpus_id,
        config=config,
    )


def chunk_entry_pathological(parse_result, doc_id, corpus_id, config):
    """Deterministic fallback for documents that time out in SaT/semantic routing.

    This runs in a one-shot child process. Environment overrides therefore
    cannot alter another document's chunking contract.
    """
    previous = {key: os.environ.get(key) for key in _PATHOLOGICAL_ENV}
    try:
        os.environ.update(_PATHOLOGICAL_ENV)
        from config import get_settings

        get_settings.cache_clear()
        from services.ingestion import tier_chunker

        return tier_chunker.chunk(
            parse_result=parse_result,
            doc_id=doc_id,
            corpus_id=corpus_id,
            config=_pathological_config(config),
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            from config import get_settings

            get_settings.cache_clear()
        except Exception:
            pass
