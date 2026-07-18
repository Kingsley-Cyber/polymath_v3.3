#!/usr/bin/env python3
"""Read-only live-store assertions for REBATCH_RUNBOOK Phase A gates."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from typing import Any

from bson import ObjectId
from config import get_settings
from neo4j import GraphDatabase
from pymongo import MongoClient
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from services.ingestion.section_classifier import (
    GHOST_B_SKIP_KINDS,
    PARENT_SUMMARY_KINDS,
)
from services.auth import auth_service


EXPECTED_HEADINGS = [
    "Chapter 1: Soil Preparation Cycles",
    "Chapter 2: Water Allocation Among Plot Holders",
    "Chapter 3: Recorded Yields",
    "Part I: Provisioning Remote Stations",
    "Part II: The Shift to Automation",
]
EXPECTED_BIBLIOGRAPHY = {
    "fixture-garden-stewardship-2019.pdf": ("Maria Okafor", 2019),
    "fixture-lighthouse-logistics-2004.pdf": ("Edwin Halvorsen", 2004),
}
EXPECTED_TEMPORAL_PHRASES = ["winter 1911", "2018 drought summer"]
HONEST_NULL_REASONS = {"no_date_source", "file_date_only", "unparseable_date"}
SUBSTANTIVE_MIN_WORDS = 50


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def year(value: Any) -> int | None:
    match = re.search(r"\b(?:18|19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def normalized_filename(document: dict) -> str:
    return str(
        document.get("filename")
        or document.get("original_filename")
        or document.get("deterministic_filename")
        or ""
    ).replace("_", "-").casefold()


class Stores:
    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.mongo = MongoClient(settings.MONGODB_URI)
        self.db = self.mongo[settings.MONGODB_DATABASE]
        self.qdrant = QdrantClient(
            url=settings.QDRANT_URL,
            timeout=settings.QDRANT_TIMEOUT_SECONDS,
        )
        self.neo4j = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )

    def close(self) -> None:
        self.neo4j.close()
        self.qdrant.close()
        self.mongo.close()


def rows(db, collection: str, corpus_id: str, projection: dict | None = None):
    return list(
        db[collection].find(
            {"corpus_id": corpus_id},
            projection or {"_id": 0},
        )
    )


def summary_required(parent: dict) -> bool:
    kind = parent.get("chunk_kind")
    return not kind or kind in PARENT_SUMMARY_KINDS


def collection_name(stores: Stores, corpus_id: str, kind: str) -> str:
    return f"{stores.settings.QDRANT_COLLECTION_PREFIX}{corpus_id[:8]}_{kind}"


def mint_probe_token(stores: Stores, corpus_id: str) -> str:
    corpus = stores.db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "user_id": 1},
    )
    require(bool(corpus and corpus.get("user_id")), "corpus owner is absent")
    user_id = str(corpus["user_id"])
    require(ObjectId.is_valid(user_id), "corpus owner is not a valid user id")
    user = stores.db["users"].find_one(
        {"_id": ObjectId(user_id)},
        {"_id": 1, "username": 1},
    )
    require(bool(user and user.get("username")), "corpus owner user is absent")
    return auth_service.create_access_token(
        user_id=str(user["_id"]),
        username=str(user["username"]),
    )


def api_json(
    stores: Stores,
    corpus_id: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = mint_probe_token(stores, corpus_id)
    base = os.environ.get("PROBE_BASE", "http://localhost:8000").rstrip("/")
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        require(200 <= response.status < 300, f"API status {response.status}")
        return json.loads(response.read().decode("utf-8"))


def g1(stores: Stores, corpus_id: str) -> None:
    documents = rows(stores.db, "documents", corpus_id)
    parents = rows(stores.db, "parent_chunks", corpus_id)
    children = rows(stores.db, "chunks", corpus_id)
    document_by_id = {document.get("doc_id"): document for document in documents}
    heading_parts = {
        norm(part)
        for parent in parents
        for part in (parent.get("heading_path") or [])
        if str(part).strip()
    }
    missing_expected = [
        heading for heading in EXPECTED_HEADINGS if norm(heading) not in heading_parts
    ]
    fixture_pdf_doc_ids = {
        document.get("doc_id")
        for document in documents
        if normalized_filename(document) in EXPECTED_BIBLIOGRAPHY
    }
    fixture_pdf_parents = [
        parent for parent in parents if parent.get("doc_id") in fixture_pdf_doc_ids
    ]
    empty_fixture_pdf_parents = [
        parent.get("parent_id")
        for parent in fixture_pdf_parents
        if not any(str(part).strip() for part in (parent.get("heading_path") or []))
    ]
    all_empty = [
        {
            "parent_id": parent.get("parent_id"),
            "filename": normalized_filename(document_by_id.get(parent.get("doc_id"), {})),
            "source_tier": parent.get("source_tier"),
        }
        for parent in parents
        if not any(str(part).strip() for part in (parent.get("heading_path") or []))
    ]
    result = {
        "gate": "g1",
        "documents": len(documents),
        "parents": len(parents),
        "children": len(children),
        "fixture_pdf_parent_count": len(fixture_pdf_parents),
        "fixture_pdf_empty_heading_parent_count": len(empty_fixture_pdf_parents),
        "all_empty_heading_parents_diagnostic": all_empty,
        "expected_fixture_headings": EXPECTED_HEADINGS,
        "missing_fixture_headings": missing_expected,
        "fixture_pdf_heading_paths": [
            parent.get("heading_path") for parent in fixture_pdf_parents
        ],
    }
    emit(result)
    require(len(documents) == 5, f"expected 5 documents, found {len(documents)}")
    require(bool(parents), "no parent chunks")
    require(bool(children), "no child chunks")
    require(len(children) >= len(parents), "children < parents")
    require(len(fixture_pdf_doc_ids) == 2, "did not discover both fixture PDFs")
    require(bool(fixture_pdf_parents), "fixture PDFs produced no parents")
    require(not empty_fixture_pdf_parents, "fixture PDF structural parent lacks heading_path")
    require(not missing_expected, f"fixture headings absent: {missing_expected}")


def g2(stores: Stores, corpus_id: str) -> None:
    parents = rows(stores.db, "parent_chunks", corpus_id)
    required = [parent for parent in parents if summary_required(parent)]
    summarized = [parent for parent in required if str(parent.get("summary") or "").strip()]
    substantive = [
        parent
        for parent in required
        if len(str(parent.get("text") or "").split()) >= SUBSTANTIVE_MIN_WORDS
    ]
    latent = [parent for parent in substantive if parent.get("latent_concepts")]
    missing_temporal = [
        parent.get("parent_id")
        for parent in summarized
        if not str(parent.get("temporal_class") or "").strip()
    ]
    missing_aliases = []
    for parent in required:
        concepts = parent.get("latent_concepts") or []
        if concepts and any(
            not isinstance(concept, dict) or not concept.get("aliases")
            for concept in concepts
        ):
            missing_aliases.append(parent.get("parent_id"))
    latent_ratio = len(latent) / len(substantive) if substantive else 0.0
    result = {
        "gate": "g2",
        "summary_required": len(required),
        "summarized": len(summarized),
        "summary_missing": len(required) - len(summarized),
        "substantive_min_words": SUBSTANTIVE_MIN_WORDS,
        "substantive": len(substantive),
        "substantive_with_latent": len(latent),
        "latent_ratio": latent_ratio,
        "missing_temporal_class": missing_temporal,
        "latent_rows_missing_aliases": missing_aliases,
    }
    emit(result)
    require(bool(required), "no summary-required parents")
    require(len(summarized) == len(required), "not all summary-required parents have summaries")
    require(bool(substantive), "no substantive parents for latent coverage")
    require(latent_ratio >= 0.30, f"latent coverage {latent_ratio:.3f} < 0.30")
    require(not missing_temporal, "summarized parent lacks temporal_class")


def g3(stores: Stores, corpus_id: str) -> None:
    documents = rows(stores.db, "documents", corpus_id)
    unsupported = []
    by_filename = {normalized_filename(document): document for document in documents}
    for document in documents:
        provenance = document.get("bibliographic_provenance") or {}
        has_supported_value = any(
            value not in (None, "", [])
            for value in (
                document.get("author"),
                document.get("language"),
                document.get("document_date"),
            )
        )
        honest_null = provenance.get("reason") in HONEST_NULL_REASONS
        if not provenance or (not has_supported_value and not honest_null):
            unsupported.append(normalized_filename(document))
    fixture_results = {}
    for filename, (expected_author, expected_year) in EXPECTED_BIBLIOGRAPHY.items():
        document = by_filename.get(filename)
        fixture_results[filename] = {
            "found": bool(document),
            "author": document.get("author") if document else None,
            "document_date": document.get("document_date") if document else None,
            "author_match": bool(document and norm(document.get("author")) == norm(expected_author)),
            "year_match": bool(document and year(document.get("document_date")) == expected_year),
        }
    result = {
        "gate": "g3",
        "documents": len(documents),
        "unsupported_or_unexplained": unsupported,
        "fixture_bibliography": fixture_results,
    }
    emit(result)
    require(len(documents) == 5, "bibliographic gate expected 5 documents")
    require(not unsupported, f"unsupported/unexplained bibliography: {unsupported}")
    require(
        all(item["author_match"] and item["year_match"] for item in fixture_results.values()),
        "fixture author/year ground truth mismatch",
    )


def g4(stores: Stores, corpus_id: str) -> None:
    children = rows(stores.db, "chunks", corpus_id)
    extractions = rows(stores.db, "ghost_b_extractions", corpus_id)
    eligible = [
        child for child in children if child.get("chunk_kind") not in GHOST_B_SKIP_KINDS
    ]
    extraction_by_chunk = {
        extraction.get("chunk_id"): extraction for extraction in extractions
    }
    missing = [
        child.get("chunk_id")
        for child in eligible
        if extraction_by_chunk.get(child.get("chunk_id"), {}).get("status") != "ok"
    ]
    providers = sorted(
        {
            str(extraction.get("provider") or "")
            for extraction in extractions
            if extraction.get("status") == "ok"
        }
    )
    captures = [
        norm(capture.get("text"))
        for extraction in extractions
        for capture in (extraction.get("temporal_captures") or [])
        if isinstance(capture, dict)
    ]
    missing_phrases = [
        phrase
        for phrase in EXPECTED_TEMPORAL_PHRASES
        if norm(phrase) not in captures
    ]
    result = {
        "gate": "g4",
        "children": len(children),
        "eligible_children": len(eligible),
        "extraction_rows": len(extractions),
        "eligible_missing_ok_extraction": len(missing),
        "providers": providers,
        "temporal_capture_texts": sorted(set(captures)),
        "missing_required_temporal_phrases": missing_phrases,
    }
    emit(result)
    require(bool(eligible), "no extraction-eligible chunks")
    require(not missing, f"{len(missing)} eligible chunks lack ok extraction")
    require(providers == ["runpod_flash"], f"unexpected extraction providers: {providers}")
    require(not missing_phrases, f"required temporal phrases absent: {missing_phrases}")


def g5(stores: Stores, corpus_id: str) -> None:
    lexicon_rows = rows(stores.db, "corpus_lexicon", corpus_id)
    require(bool(lexicon_rows), "corpus_lexicon has no entries")

    with stores.neo4j.session() as session:
        record = session.run(
            """
            MATCH (c:Chunk {corpus_id: $corpus_id})-[:MENTIONS]->(e:Entity)
            RETURN collect(DISTINCT e.entity_id) AS entity_ids
            """,
            corpus_id=corpus_id,
        ).single()
    graph_entity_ids = set((record or {}).get("entity_ids") or [])
    require(bool(graph_entity_ids), "Neo4j has no corpus-linked entities")

    schemas = collection_name(stores, corpus_id, "schemas")
    checks: list[dict[str, Any]] = []
    checked_entity_ids: set[str] = set()
    for entry in sorted(lexicon_rows, key=lambda item: str(item.get("lexicon_id") or "")):
        lexicon_id = str(entry.get("lexicon_id") or "")
        canonical_key = str(entry.get("canonical_key") or "")
        entity_ids = [str(value) for value in (entry.get("entity_ids") or []) if value]
        candidate_ids = [
            entity_id
            for entity_id in entity_ids
            if entity_id in graph_entity_ids and entity_id not in checked_entity_ids
        ]
        if not lexicon_id or not canonical_key or not candidate_ids:
            continue
        points, _ = stores.qdrant.scroll(
            collection_name=schemas,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id)),
                    FieldCondition(key="kind", match=MatchValue(value="entity_lexicon")),
                    FieldCondition(key="lexicon_id", match=MatchValue(value=lexicon_id)),
                ]
            ),
            limit=2,
            with_payload=True,
            with_vectors=False,
        )
        require(len(points) == 1, f"lexicon vector join count != 1 for {lexicon_id}")
        payload = points[0].payload or {}
        entity_id = candidate_ids[0]
        require(
            str(payload.get("canonical_key") or "") == canonical_key,
            f"canonical_key mismatch for {lexicon_id}",
        )
        require(
            entity_id in [str(value) for value in (payload.get("entity_ids") or [])],
            f"graph entity id absent from vector payload for {lexicon_id}",
        )
        checks.append(
            {
                "lexicon_id": lexicon_id,
                "canonical_key": canonical_key,
                "entity_id": entity_id,
                "vector_payload_join": True,
                "graph_join": True,
            }
        )
        checked_entity_ids.add(entity_id)
        if len(checks) == 3:
            break

    result = {
        "gate": "g5",
        "corpus_lexicon_entries": len(lexicon_rows),
        "neo4j_corpus_entities": len(graph_entity_ids),
        "schemas_collection": schemas,
        "discovered_join_checks": checks,
    }
    emit(result)
    require(len(checks) == 3, f"only {len(checks)} exact vector-graph joins found")


def g6(stores: Stores, corpus_id: str) -> None:
    documents = rows(stores.db, "documents", corpus_id)
    cards = rows(stores.db, "librarian_cards", corpus_id)
    document_ids = {str(document.get("doc_id") or "") for document in documents}
    cards_by_document: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        cards_by_document.setdefault(str(card.get("doc_id") or ""), []).append(card)
    missing = sorted(doc_id for doc_id in document_ids if len(cards_by_document.get(doc_id, [])) == 0)
    duplicates = sorted(doc_id for doc_id in document_ids if len(cards_by_document.get(doc_id, [])) != 1)
    foreign = sorted(doc_id for doc_id in cards_by_document if doc_id not in document_ids)
    empty_subjects = sorted(
        str(card.get("doc_id") or "")
        for card in cards
        if not isinstance(card.get("central_subjects"), list)
        or not card.get("central_subjects")
    )
    result = {
        "gate": "g6",
        "documents": len(documents),
        "librarian_cards": len(cards),
        "missing_card_doc_ids": missing,
        "duplicate_or_missing_card_doc_ids": duplicates,
        "foreign_card_doc_ids": foreign,
        "empty_central_subject_doc_ids": empty_subjects,
    }
    emit(result)
    require(bool(documents), "no documents")
    require(len(cards) == len(documents), "librarian card count != document count")
    require(not missing and not duplicates and not foreign, "cards are not exactly one per document")
    require(not empty_subjects, "librarian card central_subjects is empty")


def g7(stores: Stores, corpus_id: str) -> None:
    children = rows(stores.db, "chunks", corpus_id)
    parents = rows(stores.db, "parent_chunks", corpus_id)
    eligible_parents = [parent for parent in parents if summary_required(parent)]
    missing_summaries = [
        parent.get("parent_id")
        for parent in eligible_parents
        if not str(parent.get("summary") or "").strip()
    ]
    require(not missing_summaries, "summary-eligible parents lack summaries")
    # Senior ruling 2026-07-14T16:20Z applies the pre-existing projection
    # contract receipted in RECONCILIATION_2026-07-13_postS2.txt: summary
    # replacements live in naive/hrag; graph carries child points only.
    expected = {
        "naive": len(children) + len(eligible_parents),
        "hrag": len(children) + len(eligible_parents),
        "graph": len(children),
    }
    point_counts: dict[str, int] = {}
    for kind in ("naive", "hrag", "graph"):
        name = collection_name(stores, corpus_id, kind)
        info = stores.qdrant.get_collection(name)
        point_counts[kind] = int(info.points_count or 0)
    with stores.neo4j.session() as session:
        record = session.run(
            """
            MATCH (c:Chunk {corpus_id: $corpus_id})-[:MENTIONS]->(e:Entity)
            RETURN count(DISTINCT e) AS entities
            """,
            corpus_id=corpus_id,
        ).single()
    entity_count = int((record or {}).get("entities") or 0)
    result = {
        "gate": "g7",
        "mongo_children": len(children),
        "mongo_summary_eligible_parents": len(eligible_parents),
        "computed_expected_points_per_collection": expected,
        "qdrant_points": point_counts,
        "neo4j_corpus_entities": entity_count,
    }
    emit(result)
    require(bool(children), "no Mongo child chunks")
    require(bool(eligible_parents), "no Mongo summary-eligible parents")
    require(
        all(point_counts[kind] == expected[kind] for kind in expected),
        f"Qdrant point counts do not equal computed expectations={expected}: {point_counts}",
    )
    require(entity_count > 0, "Neo4j has no entities for corpus")


def g8(stores: Stores, corpus_id: str) -> None:
    response = api_json(stores, corpus_id, f"/api/corpora/{corpus_id}")
    readiness = response.get("readiness") or {}
    status = str(readiness.get("status") or "")
    blocking = readiness.get("blocking")
    next_actions = readiness.get("next_actions")
    explicit_partial_reasons = bool(
        status == "queryable_partial"
        and isinstance(blocking, list)
        and blocking
        and all(isinstance(reason, str) and reason.strip() for reason in blocking)
        and isinstance(next_actions, list)
    )
    result = {
        "gate": "g8",
        "api_endpoint": f"/api/corpora/{corpus_id}",
        "probe_token_used": True,
        "readiness_status": status,
        "stale": readiness.get("stale"),
        "blocking_reason_codes": blocking,
        "next_action_ids": [
            action.get("action_id")
            for action in (next_actions or [])
            if isinstance(action, dict)
        ],
        "explicit_partial_reasons": explicit_partial_reasons,
    }
    emit(result)
    require(not readiness.get("stale"), "readiness endpoint returned stale snapshot")
    require(
        status == "fully_enriched" or explicit_partial_reasons,
        f"readiness is neither fully_enriched nor explicitly-reasoned partial: {status}",
    )


GATES = {
    "g1": g1,
    "g2": g2,
    "g3": g3,
    "g4": g4,
    "g5": g5,
    "g6": g6,
    "g7": g7,
    "g8": g8,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("gate", choices=sorted(GATES))
    parser.add_argument("--corpus-id", required=True)
    args = parser.parse_args()
    stores = Stores()
    try:
        GATES[args.gate](stores, args.corpus_id)
    finally:
        stores.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
