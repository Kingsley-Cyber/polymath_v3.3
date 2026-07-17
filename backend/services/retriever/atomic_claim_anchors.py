"""Add exact-sentence atomic-claim anchors to already-selected evidence.

Claims remain candidate-only.  This service performs one bounded aggregation
for the selected source chunks, resolves selected parent-summary evidence
through its durable parent-to-child mapping, revalidates each immutable
sentence-keyed compilation against the current document/child, and attaches
only query-overlapping exact sentence anchors. Retrieval order, chunk text,
and scores are never changed.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from config import get_settings
from models.schemas import SourceChunk
from models.semantic_artifacts import domain_hash
from models.semantic_digest_claim_input import (
    COMPILATION_COLLECTION,
    CompiledChildCandidateExportV1,
    parse_materialized_row_document,
)
from services.ingestion.semantic_digest_claim_inputs import (
    document_source_version_id,
    validate_candidate_against_source,
)
from services.retriever.query_semantics import lexical_terms
from services.storage.mongo_contracts import restore_bson_utc_awareness

ANCHOR_SCHEMA_VERSION = "atomic_claim_anchor.v1"
MAX_ANCHOR_CLAIM_CHARS = 240
MAX_ANCHOR_SENTENCE_CHARS = 500
MAX_MAPPED_COMPILATION_ROWS_PER_SOURCE = 128


def _selected_source_key(source: SourceChunk) -> tuple[str, str, str] | None:
    key = (
        str(source.corpus_id or ""),
        str(source.doc_id or ""),
        str(source.chunk_id or ""),
    )
    return key if all(key) else None


def _summary_parent_key(source: SourceChunk) -> tuple[str, str, str] | None:
    selected_key = _selected_source_key(source)
    if selected_key is None or not selected_key[2].endswith("_summary"):
        return None
    derived_parent_id = selected_key[2].removesuffix("_summary")
    parent_id = str(source.parent_id or derived_parent_id)
    if not parent_id or parent_id != derived_parent_id:
        return None
    return selected_key[0], selected_key[1], parent_id


def _mapped_child_ids(parent: dict[str, Any]) -> list[str]:
    child_ids = [str(value) for value in (parent.get("child_ids") or []) if value]
    source_child_ids = [
        str(value) for value in (parent.get("source_child_ids") or []) if value
    ]
    for values in (child_ids, source_child_ids):
        if len(values) != len(set(values)):
            raise ValueError("parent source-child mapping contains duplicates")
    if child_ids and source_child_ids and set(child_ids) != set(source_child_ids):
        raise ValueError("parent source-child mappings disagree")
    mapped = source_child_ids or child_ids
    if not mapped:
        raise ValueError("parent source-child mapping is empty")
    return mapped


def _candidate_export(row) -> CompiledChildCandidateExportV1:
    return CompiledChildCandidateExportV1(
        schema_version="semantic_digest_claim_compilation_export.v1",
        corpus_id=row.corpus_id,
        document_id=row.document_id,
        source_version_id=row.source_version_id,
        child_id=row.child_id,
        source_text_hash=row.source_text_hash,
        observation_bundle_id=row.observation_bundle_id,
        observation_recipe_hash=row.observation_recipe_hash,
        local_extraction_recipe_hash=row.local_extraction_recipe_hash,
        normalization_registry_hash=row.normalization_registry_hash,
        compiler_version=row.compiler_version,
        compiler_recipe_hash=row.compiler_recipe_hash,
        spacy_library_version=row.spacy_library_version,
        spacy_model=row.spacy_model,
        spacy_model_version=row.spacy_model_version,
        parser_version=row.parser_version,
        evidence_refs=row.evidence_refs,
        compilation=row.envelope.body,
    )


def _anchor_candidates(row, *, query_terms: set[str]) -> list[dict[str, Any]]:
    evidence = {item.evidence_ref_id: item for item in row.evidence_refs}
    anchors: list[dict[str, Any]] = []
    for claim in row.envelope.body.claims:
        evidence_ref = evidence.get(claim.evidence_sentence_ids[0])
        if evidence_ref is None:
            continue
        if (
            len(claim.canonical_proposition) > MAX_ANCHOR_CLAIM_CHARS
            or len(evidence_ref.quote) > MAX_ANCHOR_SENTENCE_CHARS
        ):
            continue
        claim_terms = set(
            lexical_terms(f"{claim.canonical_proposition} {evidence_ref.quote}")
        )
        overlap = sorted(query_terms & claim_terms)
        required_overlap = 1 if len(query_terms) <= 3 else 2
        if len(overlap) < required_overlap:
            continue
        coverage = len(overlap) / max(1, len(query_terms))
        anchors.append(
            {
                "schema_version": ANCHOR_SCHEMA_VERSION,
                "claim_id": claim.claim_id,
                "claim_text": claim.canonical_proposition,
                "exact_sentence": evidence_ref.quote,
                "evidence_ref_id": evidence_ref.evidence_ref_id,
                "source_version_id": row.source_version_id,
                "child_id": row.child_id,
                "start": evidence_ref.start,
                "end": evidence_ref.end,
                "compilation_revision_id": row.envelope.artifact_revision_id,
                "compiler_recipe_hash": row.compiler_recipe_hash,
                "lexical_overlap": overlap,
                "lexical_overlap_count": len(overlap),
                "query_term_coverage": round(coverage, 6),
                "knowledge_status": "candidate_exact_sentence_anchor",
            }
        )
    return sorted(
        anchors,
        key=lambda item: (
            -int(item["lexical_overlap_count"]),
            -float(item["query_term_coverage"]),
            str(item["claim_id"]),
        ),
    )


async def attach_atomic_claim_anchors(
    db: Any,
    sources: list[SourceChunk],
    *,
    query: str,
    per_source: int,
    total: int,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Return metadata-enriched copies plus count-only diagnostics."""

    ordered_sources = list(sources or [])
    keys = [key for source in ordered_sources if (key := _selected_source_key(source))]
    unique_keys = list(dict.fromkeys(keys))
    direct_keys = {
        key
        for source in ordered_sources
        if (key := _selected_source_key(source)) and _summary_parent_key(source) is None
    }
    summary_sources = {
        parent_key: selected_key
        for source in ordered_sources
        if (selected_key := _selected_source_key(source))
        and (parent_key := _summary_parent_key(source))
    }
    diagnostics: dict[str, Any] = {
        "enabled": True,
        "selected_source_count": len(unique_keys),
        "direct_selected_source_count": len(direct_keys),
        "mapped_summary_source_count": len(summary_sources),
        "aggregate_calls": 0,
        "rows_seen": 0,
        "rows_valid": 0,
        "rows_rejected": 0,
        "mapped_rows_valid": 0,
        "ambiguous_mappings": 0,
        "ambiguous_compilations": 0,
        "anchors_attached": 0,
        "sources_anchored": 0,
    }
    query_terms = set(lexical_terms(query))
    if not unique_keys or not query_terms:
        diagnostics["reason"] = "missing_selected_chunks_or_query_terms"
        return ordered_sources, diagnostics

    direct_match_terms = [
        {"corpus_id": corpus_id, "document_id": doc_id, "child_id": child_id}
        for corpus_id, doc_id, child_id in sorted(direct_keys)
    ]
    mapped_doc_match_terms = [
        {"corpus_id": corpus_id, "document_id": doc_id}
        for corpus_id, doc_id in sorted({(key[0], key[1]) for key in summary_sources})
    ]
    parent_match_terms = [
        {"corpus_id": corpus_id, "doc_id": doc_id, "parent_id": parent_id}
        for corpus_id, doc_id, parent_id in sorted(summary_sources)
    ]
    initial_match_terms = direct_match_terms + mapped_doc_match_terms
    direct_match_expr = [
        {
            "$and": [
                {"$eq": ["$corpus_id", corpus_id]},
                {"$eq": ["$document_id", doc_id]},
                {"$eq": ["$child_id", child_id]},
            ]
        }
        for corpus_id, doc_id, child_id in sorted(direct_keys)
    ]
    pipeline = [
        {"$match": {"$or": initial_match_terms}},
        {
            "$lookup": {
                "from": "chunks",
                "let": {"c": "$corpus_id", "d": "$document_id", "ch": "$child_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$corpus_id", "$$c"]},
                                    {"$eq": ["$doc_id", "$$d"]},
                                    {"$eq": ["$chunk_id", "$$ch"]},
                                ]
                            }
                        }
                    },
                    {
                        "$match": {
                            "$or": [
                                {"status": {"$exists": False}},
                                {"status": "active"},
                            ]
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "corpus_id": 1,
                            "doc_id": 1,
                            "chunk_id": 1,
                            "text": 1,
                        }
                    },
                    {"$limit": 2},
                ],
                "as": "_current_children",
            }
        },
        {
            "$lookup": {
                "from": "documents",
                "let": {"c": "$corpus_id", "d": "$document_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$corpus_id", "$$c"]},
                                    {"$eq": ["$doc_id", "$$d"]},
                                ]
                            }
                        }
                    },
                    {
                        "$match": {
                            "$or": [
                                {"status": {"$exists": False}},
                                {"status": "active"},
                            ]
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "corpus_id": 1,
                            "doc_id": 1,
                            "source_identity": 1,
                        }
                    },
                    {"$limit": 2},
                ],
                "as": "_current_documents",
            }
        },
    ]
    if parent_match_terms:
        pipeline.append(
            {
                "$lookup": {
                    "from": "parent_chunks",
                    "let": {
                        "c": "$corpus_id",
                        "d": "$document_id",
                        "ch": "$child_id",
                    },
                    "pipeline": [
                        {"$match": {"$or": parent_match_terms}},
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$corpus_id", "$$c"]},
                                        {"$eq": ["$doc_id", "$$d"]},
                                        {
                                            "$or": [
                                                {
                                                    "$in": [
                                                        "$$ch",
                                                        {
                                                            "$ifNull": [
                                                                "$source_child_ids",
                                                                [],
                                                            ]
                                                        },
                                                    ]
                                                },
                                                {
                                                    "$in": [
                                                        "$$ch",
                                                        {
                                                            "$ifNull": [
                                                                "$child_ids",
                                                                [],
                                                            ]
                                                        },
                                                    ]
                                                },
                                            ]
                                        },
                                    ]
                                }
                            }
                        },
                        {
                            "$match": {
                                "$or": [
                                    {"status": {"$exists": False}},
                                    {"status": "active"},
                                ]
                            }
                        },
                        {
                            "$project": {
                                "_id": 0,
                                "corpus_id": 1,
                                "doc_id": 1,
                                "parent_id": 1,
                                "child_ids": 1,
                                "source_child_ids": 1,
                            }
                        },
                        # A second exact mapping is enough to prove ambiguity.
                        {"$limit": 2},
                    ],
                    "as": "_selected_parent_mappings",
                }
            }
        )
    else:
        pipeline.append({"$set": {"_selected_parent_mappings": []}})

    admissible_expr = list(direct_match_expr)
    if parent_match_terms:
        admissible_expr.append({"$gt": [{"$size": "$_selected_parent_mappings"}, 0]})
    pipeline.append({"$match": {"$expr": {"$or": admissible_expr}}})
    max_rows = max(
        1,
        (len(direct_keys) * 2)
        + (len(summary_sources) * MAX_MAPPED_COMPILATION_ROWS_PER_SOURCE),
    )
    # Fetch one sentinel row beyond the bound and fail closed if it exists.
    pipeline.append({"$limit": max_rows + 1})

    rows = await db[COMPILATION_COLLECTION].aggregate(pipeline).to_list(length=None)
    diagnostics["aggregate_calls"] = 1
    diagnostics["rows_seen"] = len(rows)
    diagnostics["row_limit"] = max_rows
    if len(rows) > max_rows:
        diagnostics["reason"] = "bounded_mapping_row_limit_exceeded"
        return ordered_sources, diagnostics

    valid_by_source: dict[
        tuple[str, str, str],
        dict[str, list[tuple[Any, list[dict[str, Any]], str | None]]],
    ] = defaultdict(lambda: defaultdict(list))
    for raw in rows:
        children = list(raw.pop("_current_children", []) or [])
        documents = list(raw.pop("_current_documents", []) or [])
        parent_mappings = list(raw.pop("_selected_parent_mappings", []) or [])
        try:
            if len(children) != 1 or len(documents) != 1:
                raise ValueError("current source ownership is missing or ambiguous")
            row = parse_materialized_row_document(restore_bson_utc_awareness(raw))
            child = children[0]
            document = documents[0]
            if row.source_version_id != document_source_version_id(document):
                raise ValueError("source version drifted")
            text = child.get("text")
            if not isinstance(text, str) or row.source_text_hash != domain_hash(
                "normalized-text", text
            ):
                raise ValueError("source text hash drifted")
            validate_candidate_against_source(
                _candidate_export(row),
                corpus_id=row.corpus_id,
                document=document,
                child=child,
            )
            anchors = _anchor_candidates(row, query_terms=query_terms)
            owner_keys: list[tuple[tuple[str, str, str], str | None]] = []
            direct_key = (row.corpus_id, row.document_id, row.child_id)
            if direct_key in direct_keys:
                owner_keys.append((direct_key, None))

            if len(parent_mappings) > 1:
                diagnostics["ambiguous_mappings"] += 1
            elif parent_mappings:
                parent = parent_mappings[0]
                parent_key = (
                    str(parent.get("corpus_id") or ""),
                    str(parent.get("doc_id") or ""),
                    str(parent.get("parent_id") or ""),
                )
                selected_key = summary_sources.get(parent_key)
                mapped_children = _mapped_child_ids(parent)
                if (
                    selected_key is None
                    or parent_key[:2] != (row.corpus_id, row.document_id)
                    or row.child_id not in mapped_children
                ):
                    raise ValueError("claim child is outside selected parent mapping")
                owner_keys.append((selected_key, parent_key[2]))
                diagnostics["mapped_rows_valid"] += 1

            if not owner_keys:
                raise ValueError("compilation is not owned by a selected source")
            for owner_key, mapped_parent_id in owner_keys:
                valid_by_source[owner_key][row.child_id].append(
                    (row, anchors, mapped_parent_id)
                )
            diagnostics["rows_valid"] += 1
        except Exception:
            diagnostics["rows_rejected"] += 1

    remaining = max(0, int(total))
    enriched: list[SourceChunk] = []
    for source in ordered_sources:
        key = _selected_source_key(source)
        by_child = valid_by_source.get(key, {}) if key else {}
        candidate_anchors: list[dict[str, Any]] = []
        for child_id in sorted(by_child):
            candidates = by_child[child_id]
            if len(candidates) != 1:
                diagnostics["ambiguous_compilations"] += 1
                continue
            row, anchors, mapped_parent_id = candidates[0]
            for anchor in anchors:
                attached = dict(anchor)
                attached["selected_chunk_id"] = str(source.chunk_id or "")
                if mapped_parent_id:
                    attached["mapped_parent_id"] = mapped_parent_id
                candidate_anchors.append(attached)
        candidate_anchors.sort(
            key=lambda item: (
                -int(item["lexical_overlap_count"]),
                -float(item["query_term_coverage"]),
                str(item["claim_id"]),
                str(item["child_id"]),
            )
        )
        selected = (
            candidate_anchors[: min(max(1, int(per_source)), remaining)]
            if remaining
            else []
        )
        if not selected:
            enriched.append(source)
            continue
        metadata = dict(source.metadata or {})
        metadata["atomic_claim_anchors"] = selected
        enriched.append(source.model_copy(update={"metadata": metadata}))
        remaining -= len(selected)
        diagnostics["anchors_attached"] += len(selected)
        diagnostics["sources_anchored"] += 1
    return enriched, diagnostics


async def maybe_attach_atomic_claim_anchors(
    db: Any,
    sources: list[SourceChunk],
    *,
    query: str,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    settings = get_settings()
    if not settings.ATOMIC_CLAIM_ANCHORS_ENABLED:
        return list(sources or []), {"enabled": False, "aggregate_calls": 0}
    return await attach_atomic_claim_anchors(
        db,
        sources,
        query=query,
        per_source=settings.ATOMIC_CLAIM_ANCHORS_PER_SOURCE,
        total=settings.ATOMIC_CLAIM_ANCHORS_TOTAL,
    )
