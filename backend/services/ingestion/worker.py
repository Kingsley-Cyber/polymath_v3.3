"""
Ingestion pipeline worker — orchestrates the full pipeline from raw bytes to stored vectors.

Pipeline legs:
  Leg 1 (MongoDB):  format → classify → chunk → mongo_writer
  Leg 2 (Qdrant):   embed → [GHOST A] → qdrant_writer
  Leg 3 (Neo4j):    [GHOST B] → neo4j_writer  (only when use_neo4j=True)

Each leg is tracked via write_state for idempotent resume on re-run.
"""
import logging
import uuid
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient

from config import get_settings
from models.schemas import IngestionConfig, IngestJobResponse, SourceTier, WriteState
from services.embedder import embed_batch
from services.ingestion import format_router, source_classifier, tier_chunker
from services.secrets import decrypt as _decrypt_api_key
from services.storage import mongo_reader, mongo_writer, qdrant_writer

logger = logging.getLogger(__name__)
settings = get_settings()


def _build_ghost_pool(refs) -> list[dict]:
    """
    Turn a list[ModelProfileRef] (Pydantic) or list[dict] into the plain-dict
    pool that ghost_a / ghost_b accept. Decrypts each entry's api_key exactly
    once here so the ghost layers stay ignorant of the secret format.

    Empty input → [] (ghost layer synthesizes a single-model default).
    """
    if not refs:
        return []
    out: list[dict] = []
    for ref in refs:
        data = ref.model_dump() if hasattr(ref, "model_dump") else dict(ref)
        ct = data.get("api_key")
        # Plaintext api_keys flow through unchanged; Fernet tokens get
        # decrypted here. decrypt() returns None on non-token input.
        if ct:
            pt = _decrypt_api_key(ct)
            data["api_key"] = pt if pt is not None else ct
        out.append(
            {
                "model": data.get("model"),
                "base_url": data.get("base_url") or None,
                "api_key": data.get("api_key") or None,
                "max_concurrent": int(data.get("max_concurrent") or 1) or 1,
                "extra_params": data.get("extra_params") or {},
            }
        )
    return out


async def run_ingest_job(
    job_id: str,
    data: bytes,
    filename: str,
    corpus_id: str,
    user_id: str,
    ingestion_config: IngestionConfig,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    neo4j_driver,
    model: str,
) -> IngestJobResponse:
    """
    Run the full ingestion pipeline for a single document.
    Idempotent: checks write_state and resumes from the first incomplete leg.
    """

    # ── Decode + Classify + Chunk ─────────────────────────────────────────────
    decode_result = format_router.route(data, filename=filename)
    doc_id = decode_result.doc_id
    source_tier = source_classifier.classify(
        decode_result.text, decode_result.source_mime, decode_result.pages
    )
    parents, children, injected_headers = tier_chunker.chunk(
        text=decode_result.text,
        source_tier=source_tier,
        doc_id=doc_id,
        corpus_id=corpus_id,
        pages=decode_result.pages,
    )

    # Derive per-doc chunking_config snapshot (Wave A.1) — captures which strategy
    # actually ran on this doc, for audit/migration long after ingest.
    _tier_to_parent_strategy = {
        SourceTier.tier_a: "heading_bound",
        SourceTier.tier_b: "heading_bound",
        SourceTier.tier_b_plus: "heading_bound_injected",
        SourceTier.tier_c: "paragraph_grouped",
        SourceTier.ocr_ast: "page_layout",
    }
    chunking_config = {
        "parent_strategy": _tier_to_parent_strategy.get(source_tier, "unknown"),
        "child_strategy": getattr(
            ingestion_config, "child_chunk_algorithm", "sentence_merge"
        ),
        "token_budgets": {
            "parent_target": 1200,  # matches tier_chunker.PARENT_TARGET_TOKENS
            "child_target": 350,    # matches tier_chunker.CHILD_TARGET_TOKENS
        },
        "semantic_split_threshold": getattr(
            ingestion_config, "semantic_split_threshold", 0.65
        ),
    }
    if injected_headers:
        chunking_config["injected_headers"] = [
            {
                "line_no": h.line_no,
                "level": h.level,
                "pattern": h.pattern,
                "original_line": h.original_line,
            }
            for h in injected_headers
        ]
        logger.info(
            "Tier B+ [%s]: injected %d synthetic headers",
            doc_id[:12],
            len(injected_headers),
        )

    # ── Resume: check existing write_state ───────────────────────────────────
    # Scope by corpus — same content-hashed doc_id in a different corpus must
    # not shadow a new ingest.
    existing_doc = await mongo_reader.get_document(db, doc_id, corpus_id=corpus_id)
    if existing_doc and existing_doc.get("write_state"):
        ws = WriteState(**existing_doc["write_state"])
    else:
        ws = WriteState()

    file_id = existing_doc.get("file_id", str(uuid.uuid4())) if existing_doc else str(uuid.uuid4())

    # ── Leg 1: MongoDB ────────────────────────────────────────────────────────
    if not ws.mongo_written:
        parent_dicts = [
            {
                "parent_id": p.parent_id,
                "doc_id": p.doc_id,
                "corpus_id": p.corpus_id,
                "text": p.text,
                "heading_path": p.heading_path,
                "source_tier": p.source_tier,
                "summary": None,
                "child_ids": [c.chunk_id for c in p.children],
            }
            for p in parents
        ]
        child_dicts = [
            {
                "chunk_id": c.chunk_id,
                "parent_id": c.parent_id,
                "doc_id": c.doc_id,
                "corpus_id": c.corpus_id,
                "user_id": user_id,
                "text": c.text,
                "heading_path": c.heading_path,
                "source_tier": c.source_tier,
                "token_count": c.token_count,
            }
            for c in children
        ]
        doc_record = {
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "user_id": user_id,
            "file_id": file_id,
            "filename": filename,
            "source_mime": decode_result.source_mime,
            "source_tier": source_tier.value,
            "ingestion_config": ingestion_config.model_dump(),
            "chunking_config": chunking_config,  # Wave A.1 — per-doc strategy audit
            "write_state": ws.model_dump(),
            "parent_chunks": parent_dicts,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        await mongo_writer.upsert_document(db, doc_record)
        await mongo_writer.upsert_chunks(db, child_dicts)
        await mongo_writer.update_write_state(db, doc_id, corpus_id=corpus_id, mongo_written=True)
        ws.mongo_written = True
        logger.info(
            "Leg 1 done [%s]: %d parents, %d children → MongoDB",
            doc_id[:12],
            len(parents),
            len(children),
        )

    # ── Leg 2: Qdrant ─────────────────────────────────────────────────────────
    # vec_map is hoisted outside Leg 2 so Leg 3 (Phase 14.2 schema retrieval) can
    # reuse the already-computed child vectors. On a Leg 3-only resume (qdrant_written
    # already True), this stays empty and ghost_b falls back to inline schema.
    vec_map: dict[str, list[float]] = {}
    if not ws.qdrant_written:
        target_cols = ingestion_config.target_qdrant_collections

        # Embed all children exactly once.
        # Dispatcher reads mode + corpus's frozen embedding_dimension + embedding_model_id
        # so every cloud response gets asserted against the corpus's vector-space identity.
        all_vectors = await embed_batch(
            [c.text for c in children],
            mode=getattr(ingestion_config, "embed_mode", "local_st"),
            expected_dim=getattr(ingestion_config, "embedding_dimension", 1024),
            expected_model_id=getattr(ingestion_config, "embedding_model_id", None),
        )
        vec_map = {c.chunk_id: v for c, v in zip(children, all_vectors)}

        def _as_payload(c) -> dict:
            return {
                "chunk_id": c.chunk_id,
                "parent_id": c.parent_id,
                "doc_id": c.doc_id,
                "corpus_id": c.corpus_id,
                "user_id": user_id,
                "text": c.text,
                "source_tier": c.source_tier,
                "heading_path": c.heading_path,
            }

        if "naive" in target_cols:
            naive_dicts = [_as_payload(c) for c in children]
            naive_vecs = [vec_map[c.chunk_id] for c in children]
            await qdrant_writer.upsert_children(qdrant_client, corpus_id, naive_dicts, naive_vecs, ["naive"])

        hrag_eligible = [
            c for c in children
            if c.source_tier in (SourceTier.tier_a.value, SourceTier.tier_b.value, SourceTier.tier_b_plus.value)
        ]
        if "hrag" in target_cols and hrag_eligible:
            hrag_dicts = [_as_payload(c) for c in hrag_eligible]
            hrag_vecs = [vec_map[c.chunk_id] for c in hrag_eligible]
            await qdrant_writer.upsert_children(qdrant_client, corpus_id, hrag_dicts, hrag_vecs, ["hrag"])

        if "graph" in target_cols:
            graph_dicts = [_as_payload(c) for c in children]
            graph_vecs = [vec_map[c.chunk_id] for c in children]
            await qdrant_writer.upsert_children(qdrant_client, corpus_id, graph_dicts, graph_vecs, ["graph"])

        # ── GHOST A: summarize parents ──────────────────────────────────────
        if ingestion_config.chunk_summarization:
            from services.ghost_a import SummaryTask, summarize_parents

            tasks = [
                SummaryTask(
                    parent_id=p.parent_id,
                    doc_id=p.doc_id,
                    corpus_id=p.corpus_id,
                    text=p.text,
                    source_tier=p.source_tier,
                )
                for p in parents
            ]
            # Per-corpus GHOST A pool: decrypt each entry's api_key once,
            # then pass the list to summarize_parents for round-robin dispatch.
            summary_pool = _build_ghost_pool(ingestion_config.summary_models)
            summary_results = await summarize_parents(
                tasks,
                max_summary_tokens=ingestion_config.max_summary_tokens,
                pool=summary_pool,
                model=model,
            )

            summary_payloads: list[dict] = []
            summary_texts: list[str] = []
            hp_map = {p.parent_id: p.heading_path for p in parents}

            for r in summary_results:
                await mongo_writer.update_parent_summary(db, r.doc_id, r.parent_id, r.summary, corpus_id=corpus_id)
                summary_payloads.append(
                    {
                        "parent_id": r.parent_id,
                        "doc_id": r.doc_id,
                        "corpus_id": r.corpus_id,
                        "source_tier": r.source_tier,
                        "summary": r.summary,
                        "heading_path": hp_map.get(r.parent_id),
                        "user_id": user_id,
                    }
                )
                summary_texts.append(r.summary)

            if summary_texts:
                summary_vectors = await embed_batch(summary_texts)
                await qdrant_writer.upsert_summaries(
                    qdrant_client, corpus_id, summary_payloads, summary_vectors, target_cols
                )
                logger.info("GHOST A: %d summaries stored [%s]", len(summary_texts), doc_id[:12])

        await mongo_writer.update_write_state(db, doc_id, corpus_id=corpus_id, qdrant_written=True)
        ws.qdrant_written = True
        logger.info("Leg 2 done [%s]: Qdrant upsert complete", doc_id[:12])

    # ── Leg 3: Neo4j (optional) ───────────────────────────────────────────────
    if ingestion_config.use_neo4j and settings.NEO4J_ENABLED and not ws.neo4j_written:
        if neo4j_driver is None:
            logger.warning("Neo4j enabled in config but driver not initialized; skipping leg 3")
        else:
            from services.ghost_b import (
                ExtractionTask,
                SchemaContext,
                extract_entities,
            )
            from services.graph.neo4j_writer import write_document_graph

            tasks = [
                ExtractionTask(
                    chunk_id=c.chunk_id,
                    doc_id=c.doc_id,
                    corpus_id=c.corpus_id,
                    text=c.text,
                )
                for c in children
            ]
            schema_ctx = SchemaContext(
                entity_schema=ingestion_config.entity_schema,
                relation_schema=ingestion_config.relation_schema,
                strict=ingestion_config.schema_strict,
            )

            # Phase 14.2 — schema_resolver closure over qdrant_client + corpus_id.
            # ghost_b calls this only when the user's vocab > SCHEMA_INLINE_LIMIT.
            from services.storage.qdrant_writer import retrieve_schema_for_chunk

            async def _schema_resolver(
                kind: str, query_vec: list[float], top_k: int
            ) -> list[str]:
                return await retrieve_schema_for_chunk(
                    qdrant_client, corpus_id, kind, query_vec, top_k
                )

            # Per-corpus GHOST B pool. When models_linked is True OR
            # extraction_models is empty, reuse the summary pool as the
            # extraction pool (so one chip pool powers both ghosts).
            if ingestion_config.models_linked or not ingestion_config.extraction_models:
                extraction_pool = _build_ghost_pool(ingestion_config.summary_models)
            else:
                extraction_pool = _build_ghost_pool(ingestion_config.extraction_models)
            extraction_results = await extract_entities(
                tasks,
                schema=schema_ctx,
                chunk_vectors=vec_map,
                schema_resolver=_schema_resolver,
                pool=extraction_pool,
                model=model,
            )
            await write_document_graph(
                driver=neo4j_driver,
                doc_id=doc_id,
                corpus_id=corpus_id,
                extraction_results=extraction_results,
                user_id=user_id,
                file_id=file_id,
            )
            await mongo_writer.update_write_state(db, doc_id, corpus_id=corpus_id, neo4j_written=True)
            ws.neo4j_written = True
            logger.info("Leg 3 done [%s]: Neo4j graph written", doc_id[:12])

    # Update corpus counters (increment on each ingest, not idempotent — only do when leg 1 just ran)
    if ws.mongo_written and not existing_doc:
        await db["corpora"].update_one(
            {"corpus_id": corpus_id},
            {
                "$inc": {"doc_count": 1, "chunk_count": len(children)},
                "$set": {"updated_at": datetime.utcnow()},
            },
        )

    return IngestJobResponse(
        job_id=job_id,
        doc_id=doc_id,
        corpus_id=corpus_id,
        filename=filename,
        source_tier=source_tier.value,
        status="done",
        write_state=ws,
        chunk_count=len(children),
        parent_count=len(parents),
    )
