"""
Backfill :Document anchor properties on Neo4j from MongoDB.

Existing Documents (ingested before the Brain View refactor) lack the
anchor properties — `is_cluster_anchor`, `kind`, `filename`,
`chunk_count`, and the three flat ghost_b_* metrics. Without these the
Brain View Cypher (`WHERE d.is_cluster_anchor = true`) returns nothing
for legacy corpora.

This script:
  1. Reads every `documents.{doc_id, corpus_id, filename, ghost_b_metrics,
     parent_chunks, ingestion_config}` from MongoDB.
  2. Counts each doc's chunks and parents from MongoDB.
  3. Calls `_upsert_document` on Neo4j to mirror the rich anchor properties.
  4. Reports per-corpus counts.

Idempotent — re-running on already-anchored docs only refreshes timestamps
and the `ghost_b_*` flat metrics. Cypher MERGE semantics keep first ingest
time intact via `ON CREATE`.

Usage (from repo root, with backend env loaded):
    docker exec polymath_v3_3-backend-1 python scripts/backfill_doc_anchors.py
        --corpus-id <id>            # filter to one corpus, repeatable
        --dry-run                   # report counts without writing
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_doc_anchors")

# Resolve the backend module path whether the script is run from the repo
# root (`python backend/scripts/backfill_doc_anchors.py`) or from inside the
# container (`python /app/scripts/backfill_doc_anchors.py`).
HERE = Path(__file__).resolve()
for candidate in (HERE.parent.parent, HERE.parent.parent / "backend"):
    if (candidate / "services" / "graph" / "neo4j_writer.py").exists():
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        break


async def _amain(corpus_ids: list[str] | None, dry_run: bool) -> int:
    from motor.motor_asyncio import AsyncIOMotorClient
    from neo4j import AsyncGraphDatabase

    # Backend uses MONGODB_URI / NEO4J_URI; legacy env var names kept as fallback.
    mongo_url = (
        os.environ.get("MONGODB_URI")
        or os.environ.get("MONGO_URL")
        or os.environ.get("MONGODB_URL")
    )
    if not mongo_url:
        logger.error("MONGODB_URI / MONGO_URL / MONGODB_URL not set")
        return 2

    neo4j_url = (
        os.environ.get("NEO4J_URI")
        or os.environ.get("NEO4J_URL")
        or "bolt://neo4j:7687"
    )
    neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
    neo4j_password = os.environ.get("NEO4J_PASSWORD") or os.environ.get(
        "NEO4J_AUTH_PASSWORD"
    )
    if not neo4j_password:
        logger.error("NEO4J_PASSWORD not set")
        return 2

    mongo_client = AsyncIOMotorClient(mongo_url)
    db = mongo_client.get_default_database()
    driver = AsyncGraphDatabase.driver(neo4j_url, auth=(neo4j_user, neo4j_password))

    from services.graph.neo4j_writer import _upsert_document  # noqa: WPS437

    # Pt 6 add-on: compute dominant_family / dominant_entity_type per doc
    # from the live Neo4j Entity graph (since the in-memory ExtractionResult
    # list isn't available for legacy docs). Single Cypher per doc.
    from neo4j import AsyncDriver as _D  # noqa: F401 (typing reuse)

    async def _compute_dominant_facets(
        nd, corpus_id: str, doc_id: str
    ) -> tuple[str | None, str | None]:
        async with nd.session() as session:
            r = await session.run(
                """
                MATCH (d:Document {corpus_id: $corpus_id, doc_id: $doc_id})
                      -[:HAS_CHUNK]->(c:Chunk)-[:MENTIONS]->(e:Entity)
                WITH e.canonical_family AS fam, e.primary_entity_type AS typ
                WITH collect(fam) AS fams, collect(typ) AS types
                RETURN
                  // Top non-null canonical_family by frequency
                  reduce(top = '', f IN [x IN fams WHERE x IS NOT NULL] |
                    CASE WHEN size([y IN [x IN fams WHERE x IS NOT NULL] WHERE y = f]) >
                              size([y IN [x IN fams WHERE x IS NOT NULL] WHERE y = top]) THEN f
                         ELSE top END) AS dom_family,
                  // Top non-null primary_entity_type
                  reduce(top = '', t IN [x IN types WHERE x IS NOT NULL] |
                    CASE WHEN size([y IN [x IN types WHERE x IS NOT NULL] WHERE y = t]) >
                              size([y IN [x IN types WHERE x IS NOT NULL] WHERE y = top]) THEN t
                         ELSE top END) AS dom_type
                """,
                corpus_id=corpus_id,
                doc_id=doc_id,
            )
            row = await r.single()
            if not row:
                return None, None
            df = row.get("dom_family") or None
            dt = row.get("dom_type") or None
            return (df or None, dt or None)

    query = {}
    if corpus_ids:
        query["corpus_id"] = {"$in": corpus_ids}

    projection = {
        "doc_id": 1,
        "corpus_id": 1,
        "user_id": 1,
        "file_id": 1,
        "filename": 1,
        "ghost_b_metrics": 1,
        "ingestion_config": 1,
        "_id": 0,
    }

    cursor = db["documents"].find(query, projection)
    per_corpus_counts: dict[str, int] = {}
    written = 0
    skipped_missing_id = 0

    async for doc in cursor:
        doc_id = doc.get("doc_id")
        corpus_id = doc.get("corpus_id")
        if not doc_id or not corpus_id:
            skipped_missing_id += 1
            continue
        per_corpus_counts[corpus_id] = per_corpus_counts.get(corpus_id, 0) + 1

        # Cheap server-side counts so we don't drag full chunk text across.
        chunk_count = await db["chunks"].count_documents(
            {"doc_id": doc_id, "corpus_id": corpus_id}
        )
        parent_count = await db["chunks"].count_documents(
            {"doc_id": doc_id, "corpus_id": corpus_id, "chunk_kind": "parent"}
        )
        # Fallback: many docs store parents on the Document record itself.
        if parent_count == 0:
            parents = doc.get("parent_chunks") or []
            parent_count = len(parents) if isinstance(parents, list) else 0

        metrics = doc.get("ghost_b_metrics") or {}
        success_rate = metrics.get("success_rate")
        extracted = metrics.get("extracted_chunks")
        total = metrics.get("requested_chunks")
        schema_lens_id = (doc.get("ingestion_config") or {}).get(
            "schema_lens_id"
        ) or metrics.get("schema_lens")

        # Pt 6 add-on: compute dominant_family / dominant_entity_type for
        # this legacy doc by querying its Entity graph in Neo4j (since the
        # in-memory ExtractionResult list isn't available for backfill).
        dom_family, dom_type = await _compute_dominant_facets(
            driver,
            corpus_id,
            doc_id,
        )

        if dry_run:
            logger.info(
                "[dry] doc=%s corpus=%s filename=%s chunks=%d parents=%d success=%s fam=%s type=%s",
                doc_id[:12],
                corpus_id[:8],
                doc.get("filename"),
                chunk_count,
                parent_count,
                success_rate,
                dom_family,
                dom_type,
            )
            continue

        await _upsert_document(
            driver,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=doc.get("user_id"),
            file_id=doc.get("file_id"),
            filename=doc.get("filename"),
            chunk_count=chunk_count,
            parent_count=parent_count,
            schema_lens_id=schema_lens_id if isinstance(schema_lens_id, str) else None,
            ghost_b_success_rate=float(success_rate)
            if success_rate is not None
            else None,
            ghost_b_extracted=int(extracted) if extracted is not None else None,
            ghost_b_total=int(total) if total is not None else None,
            dominant_family=dom_family,
            dominant_entity_type=dom_type,
        )
        written += 1
        if written % 50 == 0:
            logger.info("...backfilled %d documents", written)

    logger.info(
        "Backfill complete: wrote=%d dry_run=%s skipped_missing_id=%d corpus_counts=%s",
        written,
        dry_run,
        skipped_missing_id,
        per_corpus_counts,
    )

    await driver.close()
    mongo_client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill :Document anchor properties from MongoDB."
    )
    parser.add_argument(
        "--corpus-id",
        action="append",
        default=None,
        help="Restrict to one or more corpora (repeatable). Defaults to all.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report counts without writing."
    )
    args = parser.parse_args()

    return asyncio.run(_amain(args.corpus_id, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
