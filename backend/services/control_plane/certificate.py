"""Query-ready certificates and the proof contract.

A certificate is the only source of `query_ready: true`. It is issued per
(corpus_id, doc_id, contract fingerprint) exclusively from an artifact census
whose ID-set differences are all empty and whose observations all succeeded.
Certificates are insert-only: a contract change produces a new certificate
identity, never a mutation.

The proof contract returned to callers (HTTP + MCP) is intentionally
incapable of expressing flag-derived completion: there is no `complete`
boolean, and `query_ready` is set only from an issued certificate or a live
all-clear census.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from services.control_plane.desired_state import (
    collect_doc_artifact_census,
)

logger = logging.getLogger(__name__)

CERTIFICATE_COLLECTION = "query_ready_certificates"
PROOF_SCHEMA_VERSION = "certificate.v1"
_PROOF_MISSING_KEYS = (
    "qdrant_child_ids",
    "summary_ids",
    "summary_vector_ids",
    "extraction_ids",
    "fact_ids",
)
_MISSING_SAMPLE_LIMIT = 25

# Failure statuses per durable queue, used to surface blocking reasons in the
# proof. Mirrors the status vocabularies in services.ingestion.readiness.
_BLOCKING_JOB_QUERIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "source_parse",
        "source_parse_jobs",
        ("failed", "failed_recoverable", "blocked_source_missing", "dead_letter"),
    ),
    (
        "document_pipeline",
        "document_pipeline_jobs",
        (
            "failed",
            "blocked_no_source",
            "blocked_missing_chunks",
            "blocked_mongo_state",
            "dead_letter",
        ),
    ),
    (
        "extraction",
        "extraction_jobs",
        (
            "provider_failed",
            "validation_failed",
            "failed",
            "dead_letter",
            "blocked_provider_contract",
        ),
    ),
    (
        "summary",
        "summary_jobs",
        ("failed", "blocked_empty_source", "dead_letter"),
    ),
    (
        "graph_promotion",
        "graph_promotion_jobs",
        ("failed", "dead_letter"),
    ),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def contract_fingerprint(contract: dict[str, Any] | None) -> str:
    contract = contract or {}
    seed = ":".join(
        [
            str(contract.get("extraction_contract_hash") or ""),
            str(contract.get("summary_contract_hash") or ""),
            ",".join(sorted(contract.get("target_qdrant_collections") or [])),
            "graph" if contract.get("graph_required") else "nograph",
            "extract" if contract.get("extraction_required") else "noextract",
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def certificate_id_for(
    *, corpus_id: str, doc_id: str, fingerprint: str
) -> str:
    digest = hashlib.sha256(
        f"{corpus_id}:{doc_id}:{fingerprint}".encode("utf-8")
    ).hexdigest()
    return f"cert_{digest[:24]}"


async def issue_certificate_if_complete(
    db: Any,
    census: dict[str, Any],
) -> dict[str, Any] | None:
    """Issue (insert-only) a certificate when the census proves completeness.

    Returns the certificate row, or None when the census does not qualify.
    Never mutates an existing certificate; re-issuing the same identity is a
    no-op that returns the stored row.
    """

    if not census.get("complete") or census.get("excluded"):
        return None
    if census.get("observation_errors"):
        return None
    corpus_id = str(census.get("corpus_id") or "")
    doc_id = str(census.get("doc_id") or "")
    contract = census.get("contract") or {}
    fingerprint = contract_fingerprint(contract)
    certificate_id = certificate_id_for(
        corpus_id=corpus_id, doc_id=doc_id, fingerprint=fingerprint
    )
    existing = await db[CERTIFICATE_COLLECTION].find_one(
        {"certificate_id": certificate_id}, {"_id": 0}
    )
    if existing:
        return existing
    row = {
        "certificate_id": certificate_id,
        "schema_version": PROOF_SCHEMA_VERSION,
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "contract_fingerprint": fingerprint,
        "contract": {
            "extraction_contract_hash": contract.get("extraction_contract_hash"),
            "summary_contract_hash": contract.get("summary_contract_hash"),
            "target_qdrant_collections": contract.get("target_qdrant_collections"),
            "extraction_required": contract.get("extraction_required"),
            "graph_required": contract.get("graph_required"),
        },
        "verified": dict(census.get("required") or {}),
        "issued_at": _utcnow(),
    }
    try:
        await db[CERTIFICATE_COLLECTION].insert_one(dict(row))
    except Exception as exc:  # noqa: BLE001 — duplicate insert race is benign
        logger.info(
            "certificate insert race certificate_id=%s: %s", certificate_id, exc
        )
        stored = await db[CERTIFICATE_COLLECTION].find_one(
            {"certificate_id": certificate_id}, {"_id": 0}
        )
        if stored:
            return stored
        raise
    row.pop("_id", None)
    return row


async def load_certificate(
    db: Any,
    *,
    corpus_id: str,
    doc_id: str,
    contract: dict[str, Any] | None,
) -> dict[str, Any] | None:
    fingerprint = contract_fingerprint(contract)
    return await db[CERTIFICATE_COLLECTION].find_one(
        {
            "certificate_id": certificate_id_for(
                corpus_id=corpus_id, doc_id=doc_id, fingerprint=fingerprint
            )
        },
        {"_id": 0},
    )


async def _blocking_receipts(
    db: Any,
    *,
    corpus_id: str,
    doc_id: str,
) -> list[dict[str, Any]]:
    """Failure receipts from the durable queues, grouped by failure class."""

    blocking: list[dict[str, Any]] = []
    for stage, collection, statuses in _BLOCKING_JOB_QUERIES:
        try:
            rows = await db[collection].aggregate(
                [
                    {
                        "$match": {
                            "corpus_id": corpus_id,
                            "doc_id": doc_id,
                            "status": {"$in": list(statuses)},
                        }
                    },
                    {
                        "$group": {
                            "_id": {
                                "status": "$status",
                                "failure_class": {
                                    "$ifNull": ["$failure_class", "$reason"]
                                },
                            },
                            "count": {"$sum": 1},
                        }
                    },
                ]
            ).to_list(length=50)
        except Exception:  # noqa: BLE001 — proof must still render
            continue
        for row in rows:
            key = row.get("_id") or {}
            blocking.append(
                {
                    "stage": stage,
                    "status": str(key.get("status") or ""),
                    "failure_class": str(key.get("failure_class") or "unknown"),
                    "count": int(row.get("count") or 0),
                }
            )
    return blocking


def _proof_missing(census: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    missing_src = census.get("missing") or {}
    missing: dict[str, Any] = {}
    counts: dict[str, int] = {}
    for key in _PROOF_MISSING_KEYS:
        values = list(missing_src.get(key) or [])
        counts[key] = len(values)
        missing[key] = values[:_MISSING_SAMPLE_LIMIT]
    missing["chunk_source"] = bool(missing_src.get("chunk_source"))
    missing["document_summary"] = bool(missing_src.get("document_summary"))
    return missing, counts


def build_proof(
    census: dict[str, Any],
    *,
    certificate: dict[str, Any] | None,
    blocking: list[dict[str, Any]] | None = None,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the proof contract. `query_ready` requires a certificate."""

    missing, missing_counts = _proof_missing(census)
    observation_errors = list(census.get("observation_errors") or [])
    query_ready = bool(certificate) and bool(census.get("complete"))
    return {
        "schema_version": PROOF_SCHEMA_VERSION,
        "readiness_source": (
            PROOF_SCHEMA_VERSION if not observation_errors else "unavailable"
        ),
        "corpus_id": census.get("corpus_id"),
        "doc_id": census.get("doc_id"),
        "query_ready": query_ready,
        "certificate_id": (certificate or {}).get("certificate_id"),
        "excluded": bool(census.get("excluded")),
        "missing": missing,
        "missing_counts": missing_counts,
        "required": dict(census.get("required") or {}),
        "blocking": list(blocking or []),
        "recovery": dict(
            recovery or {"jobs_created": 0, "next_retry_at": None}
        ),
        "observation_errors": observation_errors,
    }


async def doc_readiness_proof(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
    doc_id: str,
    issue: bool = True,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Authoritative per-document readiness proof.

    Recomputes the artifact census live (never trusts flags), issues the
    certificate when the census qualifies, and returns the proof contract.
    """

    census = await collect_doc_artifact_census(
        db,
        qdrant_client,
        corpus_id=corpus_id,
        doc_id=doc_id,
    )
    certificate: dict[str, Any] | None = None
    if census.get("complete") and not census.get("excluded"):
        if issue:
            certificate = await issue_certificate_if_complete(db, census)
        else:
            certificate = await load_certificate(
                db,
                corpus_id=corpus_id,
                doc_id=doc_id,
                contract=census.get("contract"),
            )
    blocking = await _blocking_receipts(db, corpus_id=corpus_id, doc_id=doc_id)
    return build_proof(
        census,
        certificate=certificate,
        blocking=blocking,
        recovery=recovery,
    )


async def corpus_readiness_proof(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
    doc_ids: list[str] | None = None,
    max_docs: int = 500,
) -> dict[str, Any]:
    """Aggregate proof across a corpus: ready/blocked doc sets with reasons."""

    from services.control_plane.desired_state import active_document_ids

    scoped_doc_ids = doc_ids or await active_document_ids(
        db, corpus_id=corpus_id, limit=max_docs
    )
    ready: list[str] = []
    not_ready: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for doc_id in scoped_doc_ids:
        proof = await doc_readiness_proof(
            db, qdrant_client, corpus_id=corpus_id, doc_id=doc_id
        )
        if proof.get("readiness_source") == "unavailable":
            unavailable.append(doc_id)
        elif proof.get("query_ready"):
            ready.append(doc_id)
        else:
            not_ready.append(
                {
                    "doc_id": doc_id,
                    "missing_counts": proof.get("missing_counts"),
                    "missing": proof.get("missing"),
                    "blocking": proof.get("blocking"),
                }
            )
    return {
        "schema_version": PROOF_SCHEMA_VERSION,
        "readiness_source": PROOF_SCHEMA_VERSION,
        "corpus_id": corpus_id,
        "query_ready": bool(scoped_doc_ids) and not not_ready and not unavailable,
        "docs_total": len(scoped_doc_ids),
        "docs_ready": len(ready),
        "docs_not_ready": len(not_ready),
        "docs_unavailable": len(unavailable),
        "not_ready": not_ready[:50],
        "unavailable_doc_ids": unavailable[:50],
    }
