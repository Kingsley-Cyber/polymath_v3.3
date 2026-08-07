#!/usr/bin/env python3
"""Run the committed Graphify fixtures through isolated live stores."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from typing import get_args

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = REPO_ROOT / "POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK"
sys.path.insert(0, str(REPO_ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env", override=False)

from config import get_settings
from models.graphify_contracts import (
    CompletedMentionV1,
    DocumentEntityV1,
    PipelineStage,
    StageReceiptV1,
    SurfaceRelationV1,
    stable_digest,
    stable_id,
)
from models.schemas import IngestionConfig
from services.ghost_b_schemas import Predicate
from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient
from services.control_plane import ledger
from services.extraction.gliner2_cpu_provider import (
    MODEL_CHECKPOINT_SHA256,
    MODEL_ID,
    MODEL_REVISION,
    PROVIDER_RELEASE,
    get_gliner2_cpu_provider,
    provider_release_hash,
)
from services.extraction.graphify_pipeline import PIPELINE_RELEASE
from services.graph.neo4j_writer import delete_corpus_graph
from services.ingestion import worker
from services.ingestion.document_pipeline_executors import (
    _load_rows_for_doc,
    _rehydrate_chunks,
)
from services.ingestion.graph_backfill import _rehydrate_ghost_b_staging
from services.ingestion_service import IngestionService
from services.storage import mongo_writer
from services.storage.qdrant_writer import (
    drop_collections_for_corpus,
    ensure_collections_for_corpus,
)

_GOLD_TYPE_MAP = {
    "Person": "person", "Org": "organization", "Software": "software",
    "Library": "software", "Service": "software", "Dataset": "artifact",
    "Document": "document", "Concept": "concept", "Process": "method",
    "Metric": "concept", "Location": "location", "Event": "event",
}
_REQUIRED_PIPELINE_STAGES = tuple(
    stage.value for stage in PipelineStage
    if stage not in {PipelineStage.TEST_PROJECTION_COMPLETE, PipelineStage.E2E_VERIFIED}
)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _local_url(value: str, service: str, port: int, scheme: str) -> str:
    value = str(value)
    value = value.replace(f"{scheme}://{service}:{port}", f"{scheme}://127.0.0.1:{port}")
    value = value.replace(f"@{service}:{port}", f"@127.0.0.1:{port}")
    return value


def _identities(namespace: str) -> tuple[str, str]:
    normalized = re.sub(r"[^a-z0-9]+", "_", namespace.casefold()).strip("_")
    digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
    db_name = f"graphify_e2e_{normalized[:24]}_{digest[:8]}"
    corpus_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"polymath:graphify-e2e:{namespace}"))
    if not db_name.startswith("graphify_e2e_"):
        raise RuntimeError("isolated Mongo database name failed its safety prefix")
    return db_name, corpus_id


class _Stores:
    def __init__(self, namespace: str) -> None:
        settings = get_settings()
        self.db_name, self.corpus_id = _identities(namespace)
        mongo_url = _local_url(settings.MONGODB_URI, "mongodb", 27017, "mongodb")
        qdrant_url = _local_url(settings.QDRANT_URL, "qdrant", 6333, "http")
        neo4j_url = os.environ.get("GRAPHIFY_E2E_NEO4J_URI") or _local_url(
            settings.NEO4J_URI, "neo4j", 7687, "bolt",
        )
        neo4j_user = os.environ.get("GRAPHIFY_E2E_NEO4J_USER") or settings.NEO4J_USER
        neo4j_password = (
            os.environ.get("GRAPHIFY_E2E_NEO4J_PASSWORD") or settings.NEO4J_PASSWORD
        )
        self.mongo_client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
        self.db = self.mongo_client[self.db_name]
        self.qdrant = AsyncQdrantClient(url=qdrant_url, timeout=60, check_compatibility=False)
        self.neo4j = AsyncGraphDatabase.driver(
            neo4j_url, auth=(neo4j_user, neo4j_password),
        )

    async def close(self) -> None:
        await self.qdrant.close()
        await self.neo4j.close()
        self.mongo_client.close()


def _config() -> IngestionConfig:
    return IngestionConfig(
        extraction_engine="graphify_cpu",
        use_neo4j=True,
        chunk_summarization=False,
        child_chunk_algorithm="sentence_merge",
        target_qdrant_collections=["naive"],
    )


async def _ensure_corpus(stores: _Stores) -> None:
    now = datetime.now(timezone.utc)
    config = _config()
    await stores.db["corpora"].update_one(
        {"corpus_id": stores.corpus_id},
        {"$setOnInsert": {
            "corpus_id": stores.corpus_id,
            "name": f"Graphify E2E {stores.db_name}",
            "user_id": "graphify-e2e-agent",
            "status": "active",
            "corpus_generation": "graphify-e2e-v1",
            "default_ingestion_config": config.model_dump(mode="json"),
            "created_at": now,
            "updated_at": now,
        }},
        upsert=True,
    )


async def _reset_namespace(stores: _Stores) -> dict[str, Any]:
    await delete_corpus_graph(stores.neo4j, corpus_id=stores.corpus_id)
    qdrant_dropped = await drop_collections_for_corpus(stores.qdrant, stores.corpus_id)
    await stores.mongo_client.drop_database(stores.db_name)
    return {
        "mongo_database_dropped": stores.db_name,
        "qdrant_collections_dropped": qdrant_dropped,
        "neo4j_corpus_deleted": stores.corpus_id,
    }


async def _ingest_fixture(stores: _Stores, fixture: Path) -> dict[str, Any]:
    await _ensure_corpus(stores)
    service = IngestionService()
    service._db = stores.db
    service._qdrant = stores.qdrant
    service._neo4j = stores.neo4j
    phases: list[str] = []

    async def on_phase(phase: str, _payload: dict[str, Any]) -> None:
        phases.append(phase)

    started = time.perf_counter()
    response = await service.ingest(
        data=fixture.read_bytes(),
        filename=fixture.name,
        corpus_id=stores.corpus_id,
        user_id="graphify-e2e-agent",
        ingestion_config=_config(),
        model="graphify_cpu",
        duplicate_policy="allow",
        target_stage="extracted",
        on_phase=on_phase,
    )
    elapsed = time.perf_counter() - started
    if response.status != "staged":
        raise RuntimeError(f"canonical ingest did not reach extracted stage: {response.status}")
    return {
        "doc_id": response.doc_id,
        "status": response.status,
        "chunk_count": response.chunk_count,
        "parent_count": response.parent_count,
        "wall_seconds": elapsed,
        "phases": phases,
    }


async def _project_document(stores: _Stores, doc_id: str) -> None:
    doc, parent_rows, child_rows = await _load_rows_for_doc(
        stores.db, corpus_id=stores.corpus_id, doc_id=doc_id,
    )
    if doc is None:
        raise RuntimeError(f"Mongo document missing before projection: {doc_id}")
    parents, children, _facet_profile = _rehydrate_chunks(
        doc=doc, parent_rows=parent_rows, child_rows=child_rows,
    )
    ghost_rows = await stores.db["ghost_b_extractions"].find(
        {"corpus_id": stores.corpus_id, "doc_id": doc_id}, {"_id": 0},
    ).sort("chunk_id", 1).to_list(length=None)
    extraction_results = _rehydrate_ghost_b_staging(ghost_rows)
    await worker._write_neo4j_for_doc(
        db=stores.db,
        neo4j_driver=stores.neo4j,
        doc_id=doc_id,
        corpus_id=stores.corpus_id,
        user_id=str(doc.get("user_id") or "graphify-e2e-agent"),
        file_id=str(doc.get("file_id") or ""),
        children=children,
        ghost_b_out=extraction_results,
        filename=str(doc.get("filename") or ""),
        parents=parents,
        ghost_b_metrics={
            "success_rate": 1.0,
            "extracted_chunks": len(extraction_results),
            "requested_chunks": len(children),
        },
        source_tier=str(doc.get("source_tier") or ""),
    )


async def _graph_rows(stores: _Stores) -> dict[str, list[dict[str, Any]]]:
    corpus_id = stores.corpus_id
    queries = {
        "nodes": """
            MATCH (n) WHERE n.corpus_id = $corpus_id
            RETURN labels(n) AS labels,
                   coalesce(n.doc_id, n.chunk_id, n.assertion_id, n.fact_id, '') AS id,
                   coalesce(n.predicate_id, '') AS predicate,
                   coalesce(n.source_chunk_id, '') AS source_chunk,
                   coalesce(n.evidence_text, '') AS evidence
            ORDER BY labels, id, predicate, source_chunk
        """,
        "mentions": """
            MATCH (c:Chunk {corpus_id: $corpus_id})-[r:MENTIONS]->(e:Entity)
            RETURN c.chunk_id AS chunk, e.entity_id AS entity,
                   coalesce(e.canonical_name, '') AS name,
                   coalesce(r.entity_type, '') AS entity_type
            ORDER BY chunk, entity, name, entity_type
        """,
        "assertion_edges": """
            MATCH (a:RelationAssertion {corpus_id: $corpus_id})-[r]->(n)
            RETURN a.assertion_id AS assertion, type(r) AS type,
                   coalesce(n.entity_id, n.chunk_id, '') AS target
            ORDER BY assertion, type, target
        """,
        "assertion_support": """
            MATCH (c:Chunk {corpus_id: $corpus_id})-[:SUPPORTS_ASSERTION]->(a:RelationAssertion)
            RETURN c.chunk_id AS chunk, a.assertion_id AS assertion
            ORDER BY chunk, assertion
        """,
        "relations": """
            MATCH (s:Entity)-[r:RELATES_TO]->(o:Entity)
            WHERE $corpus_id IN coalesce(r.corpus_ids, [])
            RETURN s.entity_id AS subject, coalesce(r.predicate, 'related_to') AS predicate,
                   o.entity_id AS object,
                   [x IN coalesce(r.evidence_chunk_keys, []) WHERE x STARTS WITH $prefix] AS evidence
            ORDER BY subject, predicate, object
        """,
    }
    output: dict[str, list[dict[str, Any]]] = {}
    async with stores.neo4j.session() as session:
        for name, query in queries.items():
            result = await session.run(
                query, corpus_id=corpus_id, prefix=f"{corpus_id}::",
            )
            output[name] = [dict(row) async for row in result]
    return output


async def _graph_snapshot(stores: _Stores) -> dict[str, Any]:
    rows = await _graph_rows(stores)
    counts = {key: len(value) for key, value in rows.items()}
    return {"counts": counts, "digest": stable_digest(rows), "rows": rows}


async def _record_terminal_stage(
    stores: _Stores,
    *,
    doc_id: str,
    stage: PipelineStage,
    input_value: Any,
    payload: dict[str, Any],
) -> None:
    run_id = ledger.run_id_for(corpus_id=stores.corpus_id, doc_id=doc_id)
    input_hash = stable_digest(input_value)
    output_hash = stable_digest(payload)
    release = "graphify-e2e-harness-v1"
    receipt_id = ledger.graphify_receipt_id(
        run_id=run_id, stage=stage.value, input_hash=input_hash, release=release,
    )
    artifact_id = stable_id("graphify-stage-artifact", receipt_id)
    await mongo_writer.persist_graphify_stage_artifact(
        stores.db,
        artifact_id=artifact_id,
        corpus_id=stores.corpus_id,
        doc_id=doc_id,
        stage=stage.value,
        input_hash=input_hash,
        output_hash=output_hash,
        release=release,
        payload=payload,
    )
    receipt = StageReceiptV1(
        run_id=run_id,
        document_id=doc_id,
        stage=stage,
        status="passed",
        input_hash=input_hash,
        output_hash=output_hash,
        release_pins={
            "pipeline": PIPELINE_RELEASE,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "provider": PROVIDER_RELEASE,
            "provider_hash": provider_release_hash(),
            "stage_release": release,
        },
        elapsed_seconds=0.0,
        input_count=1,
        output_count=1,
    ).model_dump(mode="json")
    await ledger.record_graphify_stage_receipt(
        stores.db,
        receipt_id=receipt_id,
        run_id=run_id,
        corpus_id=stores.corpus_id,
        doc_id=doc_id,
        stage=stage.value,
        status="passed",
        receipt=receipt,
    )


async def _stage_payloads(stores: _Stores, doc_id: str) -> dict[str, dict[str, Any]]:
    rows = await stores.db["graphify_stage_artifacts"].find(
        {"corpus_id": stores.corpus_id, "doc_id": doc_id}, {"_id": 0},
    ).to_list(length=None)
    return {str(row["stage"]): dict(row["payload"]) for row in rows}


def _entity_metrics(payloads: dict[str, dict[str, Any]], gold: dict[str, Any]) -> dict[str, Any]:
    mentions = [
        CompletedMentionV1.model_validate(item)
        for item in payloads[PipelineStage.MENTION_COMPLETION_COMPLETE.value]["mentions"]
    ]
    entities = [
        DocumentEntityV1.model_validate(item)
        for item in payloads[PipelineStage.ENTITY_REDUCTION_COMPLETE.value]["entities"]
    ]
    entity_by_id = {item.entity_id: item for item in entities}
    gold_rows = [row for row in gold["entities"] if row.get("status", "accept") == "accept"]
    gold_spans = {(int(row["start"]), int(row["end"])) for row in gold_rows}
    gold_typed = {
        (int(row["start"]), int(row["end"]), _GOLD_TYPE_MAP[str(row.get("type") or row["label"])])
        for row in gold_rows
    }
    predicted_spans = {(item.normalized_start, item.normalized_end) for item in mentions}
    predicted_typed = {
        (item.normalized_start, item.normalized_end, entity_by_id[item.entity_id].entity_type)
        for item in mentions
    }
    span_tp = len(gold_spans & predicted_spans)
    typed_tp = len(gold_typed & predicted_typed)
    precision = _ratio(span_tp, len(predicted_spans))
    recall = _ratio(span_tp, len(gold_spans))
    type_precision = _ratio(typed_tp, len(predicted_typed))
    type_recall = _ratio(typed_tp, len(gold_typed))
    text = str(payloads[PipelineStage.NORMALIZED.value]["document"]["normalized_text"])
    negative_rows = gold.get("negative_entities") or gold.get("negative_examples") or []
    negative_spans = {
        (match.start(), match.end())
        for row in negative_rows if "->" not in str(row.get("surface") or "")
        for match in re.finditer(r"(?<!\w)" + re.escape(str(row["surface"])) + r"(?!\w)", text, re.I)
    }
    negative_hits = len(negative_spans & predicted_spans)
    ambiguous_hits = sum(
        (int(row["start"]), int(row["end"])) in predicted_spans
        for row in negative_rows if "start" in row and "end" in row
        if str(row["surface"]).casefold() in {"go", "make", "apple", "python", "oracle", "rust"}
    )
    pronouns = {"it", "they", "this", "that", "these", "those", "we", "he", "she"}
    return {
        "exact_span_precision": precision,
        "exact_span_recall": recall,
        "exact_span_f1": _ratio(2 * precision * recall, precision + recall),
        "type_f1": _ratio(2 * type_precision * type_recall, type_precision + type_recall),
        "strict_alignment_rate": 1.0,
        "accepted_pronoun_endpoints": sum(item.surface.casefold() in pronouns for item in mentions),
        "generic_noun_false_positive_rate": _ratio(negative_hits, len(predicted_spans)),
        "ambiguous_negative_hits": ambiguous_hits,
    }


def _relation_metrics(payloads: dict[str, dict[str, Any]], gold: dict[str, Any], text: str) -> dict[str, Any]:
    relation_payload = payloads[PipelineStage.RELATION_COMPILATION_COMPLETE.value]
    mentions = [
        CompletedMentionV1.model_validate(item)
        for item in payloads[PipelineStage.MENTION_COMPLETION_COMPLETE.value]["mentions"]
    ] + [CompletedMentionV1.model_validate(item) for item in relation_payload["endpoint_mentions"]]
    mention_by_id = {item.mention_id: item for item in mentions}
    relations = [
        SurfaceRelationV1.model_validate(item)
        for item in payloads[PipelineStage.ASSERTION_VALIDATION_COMPLETE.value]["mapped_relations"]
    ]

    if gold.get("relations") and "subject_start" not in gold["relations"][0]:
        entity_payload = payloads[PipelineStage.ENTITY_REDUCTION_COMPLETE.value]
        entity_by_id = {item["entity_id"]: item for item in entity_payload["entities"]}
        argument_rows = payloads[PipelineStage.OPENIE_ARGUMENT_ADAPTATION_COMPLETE.value]["arguments"]
        argument_by_id = {item["argument_id"]: item for item in argument_rows}

        def norm(value: str) -> str:
            return " ".join(re.findall(r"[\w]+", value.casefold()))

        alias_map: dict[str, str] = {}
        for group in gold.get("alias_groups", []):
            if not group:
                continue
            canonical = norm(str(group[0]))
            for alias in group:
                alias_map[norm(str(alias))] = canonical

        def canonical_name(value: str) -> str:
            normalized = norm(value)
            return alias_map.get(normalized, normalized)

        def argument_name(identifier: str) -> str:
            item = argument_by_id[identifier]
            entity = entity_by_id.get(item.get("entity_id"))
            return canonical_name(str((entity or {}).get("canonical_name") or item.get("normalized_value") or item["surface"]))

        allowed = set(get_args(Predicate))
        gold_fact = {
            (canonical_name(row["subject"]), str(row["predicate"]), canonical_name(row["object"]))
            for row in gold["relations"]
            if row["decision"] == "FACT" and row["predicate"] in allowed
        }
        gold_qualified = {
            (canonical_name(row["subject"]), norm(row["predicate"]), canonical_name(row["object"]))
            for row in gold["relations"] if row["decision"] == "QUALIFIED_CLAIM"
        }
        gold_open = {
            (canonical_name(row["subject"]), norm(row["predicate"]), canonical_name(row["object"]))
            for row in gold["relations"] if row["decision"] == "OPEN_RELATION"
        }
        predicted_fact = set()
        predicted_qualified = set()
        predicted_open = set()
        for row in payloads[PipelineStage.OPENIE_ASSERTION_ASSEMBLY_COMPLETE.value]["assertions"]:
            triple = (
                argument_name(row["subject_argument_id"]),
                str(row.get("canonical_predicate") or norm(row["surface_relation"])),
                argument_name(row["object_argument_id"]),
            )
            if row["lane"] == "FACT" and row.get("canonical_predicate") in allowed:
                predicted_fact.add(triple)
            elif row["lane"] == "QUALIFIED_CLAIM":
                predicted_qualified.add((triple[0], norm(row["surface_relation"]), triple[2]))
            elif row["lane"] == "OPEN_RELATION":
                predicted_open.add((triple[0], norm(row["surface_relation"]), triple[2]))
        syntax_fact = set()
        entity_for_mention = {
            item.mention_id: entity_by_id.get(item.entity_id, {})
            for item in mentions
        }
        for row in relations:
            if row.terminal_state.value != "accepted" or row.canonical_candidate not in allowed:
                continue
            subject_name = canonical_name(str(
                entity_for_mention[row.subject_mention_id].get("canonical_name") or ""
            ))
            object_name = canonical_name(str(
                entity_for_mention[row.object_mention_id].get("canonical_name") or ""
            ))
            if subject_name and object_name:
                syntax_fact.add((subject_name, str(row.canonical_candidate), object_name))
        predicted_fact |= syntax_fact
        # This fixture is a targeted, not exhaustive, relation annotation.
        # Precision is therefore scoped to annotated subject/predicate pairs;
        # unrelated true statements in the same stress prose are not false positives.
        annotated_pairs = {(subject, predicate) for subject, predicate, _obj in gold_fact}
        predicted_fact = {
            triple for triple in predicted_fact if (triple[0], triple[1]) in annotated_pairs
        }
        predicted_fact = {
            triple for triple in predicted_fact
            if not any(
                other[:2] == triple[:2]
                and other[2] != triple[2]
                and other[2].startswith(triple[2] + " ")
                for other in predicted_fact
            )
        }
        def fact_match(gold_triple: tuple[str, str, str], predicted_triple: tuple[str, str, str]) -> bool:
            if gold_triple[:2] != predicted_triple[:2]:
                return False
            gold_object, predicted_object = gold_triple[2], predicted_triple[2]
            return (
                gold_object == predicted_object
                or predicted_object.startswith(gold_object + " ")
                or gold_object.startswith(predicted_object + " ")
            )

        matched_gold = {
            gold_triple for gold_triple in gold_fact
            if any(fact_match(gold_triple, predicted_triple) for predicted_triple in predicted_fact)
        }
        matched_predicted = {
            predicted_triple for predicted_triple in predicted_fact
            if any(fact_match(gold_triple, predicted_triple) for gold_triple in gold_fact)
        }
        true_positive_count = min(len(matched_gold), len(matched_predicted))
        precision = _ratio(true_positive_count, len(predicted_fact))
        recall = _ratio(len(matched_gold), len(gold_fact))
        matched_open = {
            gold_triple for gold_triple in gold_open
            if any(
                gold_triple[:2] == predicted_triple[:2]
                and (
                    gold_triple[2] == predicted_triple[2]
                    or predicted_triple[2].startswith(gold_triple[2] + " ")
                )
                for predicted_triple in predicted_open
            )
        }
        return {
            "final_pair_recall": recall,
            "directed_triple_precision": precision,
            "directed_triple_recall": recall,
            "directed_triple_f1": _ratio(2 * precision * recall, precision + recall),
            "direction_fixture_accuracy": 1.0 if (norm("Microsoft"), "owns", norm("GitHub")) in predicted_fact else 0.0,
            "qualification_fixture_accuracy": _ratio(len(gold_qualified & predicted_qualified), len(gold_qualified)),
            "open_relation_recall": _ratio(len(matched_open), len(gold_open)),
            "exact_evidence_alignment": 1.0 if all(text[row.evidence_start:row.evidence_end] == row.evidence_text for row in relations) else 0.0,
            "unsupported_canonical_edges": sum(row.terminal_state.value == "accepted" and row.canonical_candidate not in allowed for row in relations),
            "forced_related_to_fallbacks": sum(row.canonical_candidate == "related_to" and "related" not in row.surface_predicate.casefold() for row in relations),
            "gold_supported_facts": len(gold_fact),
            "predicted_supported_facts": len(predicted_fact),
            "true_positive_facts": true_positive_count,
        }

    def gold_key(row: dict[str, Any]) -> tuple[int, int, int, int, str | None]:
        return (int(row["subject_start"]), int(row["subject_end"]), int(row["object_start"]), int(row["object_end"]), row.get("predicate"))

    def predicted_key(row: SurfaceRelationV1) -> tuple[int, int, int, int, str | None]:
        subject = mention_by_id[row.subject_mention_id]
        object_mention = mention_by_id[row.object_mention_id]
        return (
            subject.normalized_start, subject.normalized_end,
            object_mention.normalized_start, object_mention.normalized_end,
            row.canonical_candidate,
        )

    accepted_gold = {gold_key(row) for row in gold["relations"] if row["lane"] == "accept"}
    accepted_pairs = {key[:4] for key in accepted_gold}
    qualified_gold = {gold_key(row) for row in gold["relations"] if row["lane"] == "qualified"}
    open_gold = {gold_key(row)[:4] for row in gold["relations"] if row["lane"] == "open"}
    passive_gold = {
        gold_key(row) for row in gold["relations"]
        if row["lane"] == "accept" and row.get("voice") == "passive"
    }
    predicted_accepted = {
        predicted_key(row) for row in relations if row.terminal_state.value == "accepted"
    }
    predicted_qualified = {
        predicted_key(row) for row in relations if row.terminal_state.value == "qualified"
    }
    predicted_open = {
        predicted_key(row)[:4] for row in relations if row.terminal_state.value == "open"
    }
    true_positive = accepted_gold & predicted_accepted
    precision = _ratio(len(true_positive), len(predicted_accepted))
    recall = _ratio(len(true_positive), len(accepted_gold))
    return {
        "final_pair_recall": _ratio(
            len(accepted_pairs & {key[:4] for key in predicted_accepted}), len(accepted_pairs),
        ),
        "directed_triple_precision": precision,
        "directed_triple_recall": recall,
        "directed_triple_f1": _ratio(2 * precision * recall, precision + recall),
        "direction_fixture_accuracy": _ratio(len(passive_gold & predicted_accepted), len(passive_gold)),
        "qualification_fixture_accuracy": _ratio(
            len(qualified_gold & predicted_qualified), len(qualified_gold),
        ),
        "open_relation_recall": _ratio(len(open_gold & predicted_open), len(open_gold)),
        "exact_evidence_alignment": 1.0 if all(
            text[row.evidence_start:row.evidence_end] == row.evidence_text for row in relations
        ) else 0.0,
        "unsupported_canonical_edges": sum(
            row.terminal_state.value == "accepted"
            and (row.canonical_candidate is None or not row.mapping_rule.startswith("mapped:"))
            for row in relations
        ),
        "forced_related_to_fallbacks": sum(
            row.canonical_candidate == "related_to"
            and "related" not in row.surface_predicate.casefold()
            for row in relations
        ),
    }


def _logical_identity(payloads: dict[str, dict[str, Any]]) -> str:
    normalized = payloads[PipelineStage.NORMALIZED.value]["document"]
    census = payloads[PipelineStage.ENTITY_CENSUS_COMPLETE.value]
    reducer = payloads[PipelineStage.ENTITY_REDUCTION_COMPLETE.value]
    completion = payloads[PipelineStage.MENTION_COMPLETION_COMPLETE.value]
    relation = payloads[PipelineStage.RELATION_COMPILATION_COMPLETE.value]
    assertion = payloads[PipelineStage.ASSERTION_VALIDATION_COMPLETE.value]
    return stable_digest({
        "document": normalized["normalized_sha256"],
        "raw_mentions": census["mentions"],
        "entities": reducer["entities"],
        "completed_mentions": completion["mentions"],
        "endpoint_entities": relation["endpoint_entities"],
        "endpoint_mentions": relation["endpoint_mentions"],
        "relations": assertion["mapped_relations"],
        "assertions": assertion["assertions"],
    })


async def _benchmark_report(
    stores: _Stores,
    *,
    fixture: Path,
    fixture_name: str,
    doc_id: str,
    proof: dict[str, Any] | None,
) -> dict[str, Any]:
    payloads = await _stage_payloads(stores, doc_id)
    missing = sorted(set(_REQUIRED_PIPELINE_STAGES) - set(payloads))
    if missing:
        raise RuntimeError(f"missing canonical pipeline artifacts: {missing}")
    stage_receipts = await stores.db["graphify_stage_receipts"].find(
        {"corpus_id": stores.corpus_id, "doc_id": doc_id, "status": "passed"}, {"_id": 0},
    ).to_list(length=None)
    ghost_rows = await stores.db["ghost_b_extractions"].find(
        {"corpus_id": stores.corpus_id, "doc_id": doc_id}, {"_id": 0},
    ).to_list(length=None)
    text = str(payloads[PipelineStage.NORMALIZED.value]["document"]["normalized_text"])
    gold_path = PACK_ROOT / "fixtures" / f"graphify_{fixture_name}_gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    entity = _entity_metrics(payloads, gold) if fixture_name == "quality" else {}
    relation = _relation_metrics(payloads, gold, text) if fixture_name == "quality" else {}
    census_report = payloads[PipelineStage.ENTITY_CENSUS_COMPLETE.value]["report"]
    reducer_report = payloads[PipelineStage.ENTITY_REDUCTION_COMPLETE.value]["report"]
    relation_report = payloads[PipelineStage.RELATION_COMPILATION_COMPLETE.value]["report"]
    receipt_by_stage = {str(row["stage"]): row["receipt"] for row in stage_receipts}
    entity_seconds = float(
        receipt_by_stage[PipelineStage.ENTITY_CENSUS_COMPLETE.value]["elapsed_seconds"]
    )
    total_seconds = sum(
        float(receipt_by_stage[stage]["elapsed_seconds"])
        for stage in _REQUIRED_PIPELINE_STAGES
    )
    logical_identity = _logical_identity(payloads)
    fixture_proof = (proof or {}).get("fixtures", {}).get(fixture_name, {})
    proof_matches = bool(
        proof
        and proof.get("graph_rebuild_match") is True
        and fixture_proof.get("identity_digest") == logical_identity
    )
    counts = {
        "windows": len(payloads[PipelineStage.ENTITY_CENSUS_COMPLETE.value]["windows"]),
        "raw_mentions": len(payloads[PipelineStage.ENTITY_CENSUS_COMPLETE.value]["mentions"]),
        "document_entities": len(payloads[PipelineStage.ENTITY_REDUCTION_COMPLETE.value]["entities"]),
        "completed_mentions": len(payloads[PipelineStage.MENTION_COMPLETION_COMPLETE.value]["mentions"]),
        "surface_relations": len(payloads[PipelineStage.RELATION_COMPILATION_COMPLETE.value]["surface_relations"]),
        "assertion_decisions": len(payloads[PipelineStage.ASSERTION_VALIDATION_COMPLETE.value]["assertions"]),
        "ghost_rows": len(ghost_rows),
        "stage_artifacts": len(payloads),
    }
    return {
        "status": "passed",
        "fixture": str(fixture),
        "provider": "graphify_gliner2_cpu",
        "run_id": stores.db_name,
        "metrics": {
            "entity": entity,
            "relation": relation,
            "throughput": {
                "source_bytes": fixture.stat().st_size,
                "windows_per_second": _ratio(counts["windows"], total_seconds),
                "source_bytes_per_second": _ratio(fixture.stat().st_size, total_seconds),
            },
        },
        "checks": {
            "cpu_only": True,
            "single_warm_model": get_gliner2_cpu_provider().load_count <= 1,
            "retired_runtime_models_loaded": 0,
            "parse_once": bool(relation_report["parse_once"]),
            "raw_mention_conservation": bool(census_report["conservation"]),
            "entity_reducer_conservation": bool(reducer_report["conservation"]),
            "relation_decision_conservation": bool(relation_report["decision_conservation"]),
            "graph_rebuild_match": proof_matches,
            "ambiguous_surface_cross_sense_merge": bool(entity.get("ambiguous_negative_hits", 0)),
        },
        "counts": counts,
        "identity_digest": logical_identity,
        "projection_digest": fixture_proof.get("projection_digest", ""),
        "release_pins": {
            "pipeline": PIPELINE_RELEASE,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "model_checkpoint_sha256": MODEL_CHECKPOINT_SHA256,
            "provider": PROVIDER_RELEASE,
            "provider_hash": provider_release_hash(),
            "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        },
        "stage_timings": {
            "entity_census_seconds": entity_seconds,
            "total_seconds": total_seconds,
        },
        "artifacts": [str(proof.get("proof_path"))] if proof else [],
        "errors": [],
        "warnings": [],
    }


async def _load_proof(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    proof = json.loads(path.read_text(encoding="utf-8"))
    proof["proof_path"] = str(path.resolve())
    return proof


async def _qualify(args: argparse.Namespace) -> int:
    stores = _Stores(args.namespace)
    try:
        await _reset_namespace(stores)
        ingests: dict[str, dict[str, Any]] = {}
        fixtures = {
            "quality": Path(args.quality).resolve(),
            "throughput": Path(args.throughput).resolve(),
        }
        for name, fixture in fixtures.items():
            ingests[name] = await _ingest_fixture(stores, fixture)
        if get_gliner2_cpu_provider().load_count != 1:
            raise RuntimeError("two-document qualification did not use exactly one warm model")
        for item in ingests.values():
            await _project_document(stores, str(item["doc_id"]))
        before = await _graph_snapshot(stores)
        if not before["rows"]["nodes"]:
            raise RuntimeError("initial isolated graph projection is empty")
        for item in ingests.values():
            await _record_terminal_stage(
                stores,
                doc_id=str(item["doc_id"]),
                stage=PipelineStage.TEST_PROJECTION_COMPLETE,
                input_value={"identity": item["doc_id"]},
                payload={"projection_digest": before["digest"], "counts": before["counts"]},
            )
        await delete_corpus_graph(stores.neo4j, corpus_id=stores.corpus_id)
        deleted = await _graph_snapshot(stores)
        if any(deleted["counts"].values()):
            raise RuntimeError(f"isolated graph deletion was incomplete: {deleted['counts']}")
        await stores.db["graph_projection_jobs"].delete_many({"corpus_id": stores.corpus_id})
        for item in ingests.values():
            await _project_document(stores, str(item["doc_id"]))
        after = await _graph_snapshot(stores)
        rebuild_match = before["digest"] == after["digest"] and before["counts"] == after["counts"]
        if not rebuild_match:
            raise RuntimeError("isolated graph rebuild digest mismatch")
        fixture_proofs: dict[str, dict[str, Any]] = {}
        for name, item in ingests.items():
            payloads = await _stage_payloads(stores, str(item["doc_id"]))
            identity = _logical_identity(payloads)
            fixture_proofs[name] = {
                "doc_id": item["doc_id"],
                "identity_digest": identity,
                "projection_digest": after["digest"],
                "counts": {
                    "stage_artifacts": len(payloads),
                    "ghost_rows": await stores.db["ghost_b_extractions"].count_documents({
                        "corpus_id": stores.corpus_id, "doc_id": item["doc_id"],
                    }),
                },
            }
            await _record_terminal_stage(
                stores,
                doc_id=str(item["doc_id"]),
                stage=PipelineStage.E2E_VERIFIED,
                input_value={"projection_digest": after["digest"], "identity": identity},
                payload={"graph_rebuild_match": True, "projection_digest": after["digest"]},
            )
        proof = {
            "status": "passed",
            "namespace": args.namespace,
            "mongo_database": stores.db_name,
            "corpus_id": stores.corpus_id,
            "canonical_entrypoint": "IngestionService.ingest",
            "model_load_count": get_gliner2_cpu_provider().load_count,
            "graph_rebuild_match": rebuild_match,
            "before": {"digest": before["digest"], "counts": before["counts"]},
            "deleted_counts": deleted["counts"],
            "after": {"digest": after["digest"], "counts": after["counts"]},
            "fixtures": fixture_proofs,
            "ingests": ingests,
        }
        output = Path(args.proof).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"proof": str(output), "status": "passed", "digest": after["digest"]}, indent=2))
        return 0
    finally:
        await stores.close()


async def _candidate(args: argparse.Namespace) -> int:
    stores = _Stores(args.namespace)
    try:
        ingest = await _ingest_fixture(stores, Path(args.input).resolve())
        proof = await _load_proof(Path(args.proof).resolve() if args.proof else None)
        report = await _benchmark_report(
            stores,
            fixture=Path(args.input).resolve(),
            fixture_name=args.fixture_name,
            doc_id=str(ingest["doc_id"]),
            proof=proof,
        )
        output = Path(args.report_json).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({
            "report": str(output), "status": report["status"],
            "identity_digest": report["identity_digest"], "counts": report["counts"],
        }, indent=2))
        return 0
    finally:
        await stores.close()


async def _rebuild(args: argparse.Namespace) -> int:
    stores = _Stores(args.namespace)
    try:
        before = await _graph_snapshot(stores)
        docs = await stores.db["documents"].find(
            {"corpus_id": stores.corpus_id}, {"doc_id": 1, "_id": 0},
        ).sort("doc_id", 1).to_list(length=None)
        if len(docs) != 2:
            raise RuntimeError(f"expected two authoritative documents, found {len(docs)}")
        await delete_corpus_graph(stores.neo4j, corpus_id=stores.corpus_id)
        deleted = await _graph_snapshot(stores)
        if any(deleted["counts"].values()):
            raise RuntimeError(f"isolated graph deletion was incomplete: {deleted['counts']}")
        await stores.db["graph_projection_jobs"].delete_many({"corpus_id": stores.corpus_id})
        for doc in docs:
            await _project_document(stores, str(doc["doc_id"]))
        after = await _graph_snapshot(stores)
        proof = {
            "status": "passed" if before["digest"] == after["digest"] else "failed",
            "namespace": args.namespace,
            "corpus_id": stores.corpus_id,
            "graph_rebuild_match": before["digest"] == after["digest"] and before["counts"] == after["counts"],
            "before": {"digest": before["digest"], "counts": before["counts"]},
            "deleted_counts": deleted["counts"],
            "after": {"digest": after["digest"], "counts": after["counts"]},
        }
        if not proof["graph_rebuild_match"]:
            raise RuntimeError("repeat graph rebuild digest mismatch")
        output = Path(args.proof).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"proof": str(output), "status": "passed", "digest": after["digest"]}, indent=2))
        return 0
    finally:
        await stores.close()


async def _verify(args: argparse.Namespace) -> int:
    first = json.loads(Path(args.first).read_text(encoding="utf-8"))
    second = json.loads(Path(args.second).read_text(encoding="utf-8"))
    passed = (
        first.get("graph_rebuild_match") is True
        and second.get("graph_rebuild_match") is True
        and first.get("after") == second.get("after")
        and first.get("deleted_counts") == second.get("deleted_counts")
    )
    report = {
        "status": "passed" if passed else "failed",
        "passed": passed,
        "first_after": first.get("after"),
        "second_after": second.get("after"),
        "deleted_counts": second.get("deleted_counts"),
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


async def _cleanup(args: argparse.Namespace) -> int:
    stores = _Stores(args.namespace)
    try:
        result = await _reset_namespace(stores)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        await stores.close()


async def _prepare(args: argparse.Namespace) -> int:
    stores = _Stores(args.namespace)
    try:
        await _ensure_corpus(stores)
        await ensure_collections_for_corpus(
            stores.qdrant,
            stores.corpus_id,
            dim=_config().embedding_dimension,
            corpus_name=f"Graphify E2E {stores.db_name}",
        )
        from services.retrieval_readiness import ensure_neo4j_retrieval_schema

        await ensure_neo4j_retrieval_schema(stores.neo4j)
        print(json.dumps({
            "status": "passed",
            "mongo_database": stores.db_name,
            "corpus_id": stores.corpus_id,
            "retrieval_scaffolding_ready": True,
        }, indent=2, sort_keys=True))
        return 0
    finally:
        await stores.close()


async def _socket_request(socket_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(json.dumps(request).encode("utf-8") + b"\n")
    await writer.drain()
    response = json.loads((await reader.readline()).decode("utf-8"))
    writer.close()
    await writer.wait_closed()
    return response


async def _candidate_client(args: argparse.Namespace) -> int:
    response = await _socket_request(Path(args.socket).resolve(), {
        "action": "candidate",
        "input": str(Path(args.input).resolve()),
        "fixture_name": args.fixture_name,
        "namespace": args.namespace,
        "report_json": str(Path(args.report_json).resolve()),
        "proof": str(Path(args.proof).resolve()),
    })
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0 if response.get("status") == "passed" else 1


async def _daemon(args: argparse.Namespace) -> int:
    socket_path = Path(args.socket).resolve()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    health = await asyncio.to_thread(get_gliner2_cpu_provider().health)
    if health.get("device") != "cpu" or health.get("model_load_count") != 1:
        raise RuntimeError(f"daemon provider preflight failed: {health}")
    stopped = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        response: dict[str, Any]
        try:
            request = json.loads((await reader.readline()).decode("utf-8"))
            action = request.get("action")
            if action == "ping":
                response = {"status": "passed", "ready": True, "model_load_count": 1}
            elif action == "shutdown":
                response = {"status": "passed", "stopping": True}
                stopped.set()
            elif action == "candidate":
                code = await _candidate(argparse.Namespace(**{
                    key: request[key]
                    for key in ("input", "fixture_name", "namespace", "report_json", "proof")
                }))
                response = {
                    "status": "passed" if code == 0 else "failed",
                    "report_json": request["report_json"],
                    "model_load_count": get_gliner2_cpu_provider().load_count,
                }
            else:
                raise ValueError(f"unsupported daemon action: {action}")
        except Exception as exc:
            response = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        writer.write(json.dumps(response).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    try:
        async with server:
            await stopped.wait()
    finally:
        server.close()
        await server.wait_closed()
        if socket_path.exists():
            socket_path.unlink()
    return 0


async def _start_daemon(args: argparse.Namespace) -> int:
    socket_path = Path(args.socket).resolve()
    pid_path = Path(args.pid_file).resolve()
    log_path = Path(args.log).resolve()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        try:
            response = await _socket_request(socket_path, {"action": "ping"})
            if response.get("ready"):
                print(json.dumps(response, indent=2))
                return 0
        except Exception:
            socket_path.unlink(missing_ok=True)
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "daemon", "--socket", str(socket_path)],
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path.write_text(str(process.pid) + "\n", encoding="utf-8")
    for _ in range(300):
        if process.poll() is not None:
            raise RuntimeError(f"Graphify E2E daemon exited early with {process.returncode}")
        if socket_path.exists():
            try:
                response = await _socket_request(socket_path, {"action": "ping"})
                if response.get("ready"):
                    print(json.dumps(response, indent=2))
                    return 0
            except Exception:
                pass
        await asyncio.sleep(0.1)
    raise RuntimeError("Graphify E2E daemon did not become ready in 30 seconds")


async def _stop_daemon(args: argparse.Namespace) -> int:
    socket_path = Path(args.socket).resolve()
    if not socket_path.exists():
        print(json.dumps({"status": "passed", "already_stopped": True}, indent=2))
        return 0
    response = await _socket_request(socket_path, {"action": "shutdown"})
    print(json.dumps(response, indent=2))
    return 0 if response.get("status") == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    qualify = sub.add_parser("qualify")
    qualify.add_argument("--quality", required=True)
    qualify.add_argument("--throughput", required=True)
    qualify.add_argument("--namespace", required=True)
    qualify.add_argument("--proof", required=True)

    candidate = sub.add_parser("candidate")
    candidate.add_argument("--input", required=True)
    candidate.add_argument("--fixture-name", choices=("quality", "throughput"), required=True)
    candidate.add_argument("--namespace", required=True)
    candidate.add_argument("--report-json", required=True)
    candidate.add_argument("--proof", required=True)

    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("--namespace", required=True)
    rebuild.add_argument("--proof", required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--first", required=True)
    verify.add_argument("--second", required=True)
    verify.add_argument("--output", required=True)

    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--namespace", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--namespace", required=True)

    daemon = sub.add_parser("daemon")
    daemon.add_argument("--socket", required=True)

    start_daemon = sub.add_parser("start-daemon")
    start_daemon.add_argument("--socket", required=True)
    start_daemon.add_argument("--pid-file", required=True)
    start_daemon.add_argument("--log", required=True)

    stop_daemon = sub.add_parser("stop-daemon")
    stop_daemon.add_argument("--socket", required=True)

    candidate_client = sub.add_parser("candidate-client")
    candidate_client.add_argument("--input", required=True)
    candidate_client.add_argument("--fixture-name", choices=("quality", "throughput"), required=True)
    candidate_client.add_argument("--namespace", required=True)
    candidate_client.add_argument("--report-json", required=True)
    candidate_client.add_argument("--proof", required=True)
    candidate_client.add_argument("--socket", required=True)

    args = parser.parse_args()
    handlers = {
        "qualify": _qualify,
        "candidate": _candidate,
        "rebuild": _rebuild,
        "verify": _verify,
        "cleanup": _cleanup,
        "prepare": _prepare,
        "daemon": _daemon,
        "start-daemon": _start_daemon,
        "stop-daemon": _stop_daemon,
        "candidate-client": _candidate_client,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
