"""Retrieval readiness guards for install, ingest, and repair paths.

The retrieval stack only works when the three storage layers agree:

* MongoDB has corpus/document/chunk rows and text indexes.
* Qdrant has the per-corpus vector collections, payload indexes, and matching
  embedding dimension.
* Neo4j has graph constraints/indexes, including full-text indexes for
  anchored Entity/Fact lookup when graph retrieval is enabled.

Mongo indexes are handled by ``db.indexes.create_all_indexes`` at application
startup. This module covers the Qdrant + Neo4j side and is intentionally
idempotent so it can run on every startup and every ingest resume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from config import get_settings
from services.storage.qdrant_writer import ensure_collections_for_corpus

logger = logging.getLogger(__name__)


@dataclass
class CorpusReadinessReport:
    corpus_id: str
    qdrant_ready: bool = False
    neo4j_ready: bool | None = None
    embedding_dimension: int | None = None
    qdrant_collections: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and self.qdrant_ready and self.neo4j_ready is not False

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_id": self.corpus_id,
            "ok": self.ok,
            "qdrant_ready": self.qdrant_ready,
            "neo4j_ready": self.neo4j_ready,
            "embedding_dimension": self.embedding_dimension,
            "qdrant_collections": list(self.qdrant_collections),
            "errors": list(self.errors),
        }


def _config_dict(
    corpus_doc: dict[str, Any] | None,
    ingestion_config: Any | None,
) -> dict[str, Any]:
    if ingestion_config is not None:
        if hasattr(ingestion_config, "model_dump"):
            return dict(ingestion_config.model_dump())
        if isinstance(ingestion_config, dict):
            return dict(ingestion_config)
    return dict((corpus_doc or {}).get("default_ingestion_config") or {})


def _embedding_dimension(
    corpus_doc: dict[str, Any] | None,
    ingestion_config: Any | None,
    *,
    default_dim: int,
) -> int:
    cfg = _config_dict(corpus_doc, ingestion_config)
    try:
        return int(cfg.get("embedding_dimension") or default_dim)
    except (TypeError, ValueError):
        return int(default_dim)


def _target_collections(
    corpus_doc: dict[str, Any] | None,
    ingestion_config: Any | None,
) -> list[str]:
    cfg = _config_dict(corpus_doc, ingestion_config)
    raw = cfg.get("target_qdrant_collections") or ["naive", "hrag"]
    return [str(item) for item in raw if item]


def _uses_neo4j(
    corpus_doc: dict[str, Any] | None,
    ingestion_config: Any | None,
) -> bool:
    cfg = _config_dict(corpus_doc, ingestion_config)
    return bool(cfg.get("use_neo4j"))


async def ensure_neo4j_retrieval_schema(
    neo4j_driver: Any,
    *,
    wait_timeout_s: float = 15.0,
) -> dict[str, str]:
    """Ensure Neo4j schema and wait for retrieval-critical fulltext indexes."""
    from services.graph.schema import initialize_schema, wait_for_retrieval_indexes

    await initialize_schema(neo4j_driver)
    return await wait_for_retrieval_indexes(
        neo4j_driver,
        timeout_s=wait_timeout_s,
    )


async def ensure_corpus_retrieval_ready(
    *,
    db: Any,
    qdrant_client: Any,
    neo4j_driver: Any | None,
    corpus_id: str,
    corpus_doc: dict[str, Any] | None = None,
    corpus_name: str | None = None,
    ingestion_config: Any | None = None,
    neo4j_enabled: bool | None = None,
    neo4j_schema_ready: bool = False,
    default_dim: int | None = None,
) -> CorpusReadinessReport:
    """Make a single corpus queryable by the configured retrieval routes.

    This is safe to call:
    * during startup over every existing corpus,
    * before an ingest writes Qdrant/Neo4j,
    * during a future repair command.
    """
    settings = get_settings()
    default_dim = int(default_dim or settings.EMBEDDING_DIMENSION)
    if corpus_doc is None and db is not None:
        corpus_doc = await db["corpora"].find_one(
            {"corpus_id": corpus_id},
            {
                "_id": 0,
                "corpus_id": 1,
                "name": 1,
                "default_ingestion_config": 1,
            },
        )
    corpus_doc = corpus_doc or {"corpus_id": corpus_id}
    corpus_name = corpus_name or corpus_doc.get("name")
    dim = _embedding_dimension(corpus_doc, ingestion_config, default_dim=default_dim)
    targets = _target_collections(corpus_doc, ingestion_config)
    report = CorpusReadinessReport(
        corpus_id=corpus_id,
        embedding_dimension=dim,
        qdrant_collections=targets,
    )

    if qdrant_client is None:
        report.errors.append("qdrant: client unavailable")
    else:
        try:
            await ensure_collections_for_corpus(
                qdrant_client,
                corpus_id,
                dim=dim,
                corpus_name=corpus_name,
            )
            report.qdrant_ready = True
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"qdrant: {type(exc).__name__}: {exc}")

    neo4j_enabled = settings.NEO4J_ENABLED if neo4j_enabled is None else neo4j_enabled
    neo4j_required = bool(neo4j_enabled and _uses_neo4j(corpus_doc, ingestion_config))
    if not neo4j_required:
        report.neo4j_ready = None
    elif neo4j_driver is None:
        report.neo4j_ready = False
        report.errors.append("neo4j: required but driver unavailable")
    elif neo4j_schema_ready:
        report.neo4j_ready = True
    else:
        try:
            await ensure_neo4j_retrieval_schema(neo4j_driver)
            report.neo4j_ready = True
        except Exception as exc:  # noqa: BLE001
            report.neo4j_ready = False
            report.errors.append(f"neo4j: {type(exc).__name__}: {exc}")

    return report


async def repair_retrieval_readiness_for_all_corpora(
    *,
    db: Any,
    qdrant_client: Any,
    neo4j_driver: Any | None,
    neo4j_enabled: bool | None = None,
    default_dim: int | None = None,
) -> dict[str, Any]:
    """Startup repair sweep for all existing corpora.

    Returns an aggregate summary for logs/tests. Individual corpus failures are
    recorded in the summary and do not stop startup; the ingest worker still
    enforces readiness as a document-level failure before writing.
    """
    settings = get_settings()
    neo4j_enabled = settings.NEO4J_ENABLED if neo4j_enabled is None else neo4j_enabled
    default_dim = int(default_dim or settings.EMBEDDING_DIMENSION)
    neo4j_schema_ready = False
    neo4j_schema_error: str | None = None
    if neo4j_enabled:
        if neo4j_driver is None:
            neo4j_schema_error = "neo4j: enabled but driver unavailable"
        else:
            try:
                await ensure_neo4j_retrieval_schema(neo4j_driver)
                neo4j_schema_ready = True
            except Exception as exc:  # noqa: BLE001
                neo4j_schema_error = f"neo4j: {type(exc).__name__}: {exc}"
                logger.warning("Neo4j retrieval schema repair failed: %s", exc)

    reports: list[dict[str, Any]] = []
    cursor = db["corpora"].find(
        {},
        {
            "_id": 0,
            "corpus_id": 1,
            "name": 1,
            "default_ingestion_config": 1,
        },
    )
    async for corpus_doc in cursor:
        cid = str(corpus_doc.get("corpus_id") or "")
        if not cid:
            continue
        report = await ensure_corpus_retrieval_ready(
            db=db,
            qdrant_client=qdrant_client,
            neo4j_driver=neo4j_driver,
            corpus_id=cid,
            corpus_doc=corpus_doc,
            neo4j_enabled=bool(neo4j_enabled and neo4j_schema_error is None),
            neo4j_schema_ready=neo4j_schema_ready,
            default_dim=default_dim,
        )
        if neo4j_schema_error and _uses_neo4j(corpus_doc, None):
            report.neo4j_ready = False
            report.errors.append(neo4j_schema_error)
        reports.append(report.to_dict())

    failed = [r for r in reports if not r["ok"]]
    return {
        "scanned": len(reports),
        "ready": len(reports) - len(failed),
        "failed": len(failed),
        "neo4j_schema_ready": neo4j_schema_ready,
        "reports": reports,
    }
