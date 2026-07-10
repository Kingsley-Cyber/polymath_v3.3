"""Stage-level idempotency keys for ingestion jobs.

Source identity prevents re-ingesting the same file. Stage identity goes one
step deeper: each parse/embed/extract/summary job carries the exact input and
contract hashes that make the work safe to retry, skip, or invalidate.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def stable_stage_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalized_text_hash(text: Any) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def source_file_hash(doc: dict[str, Any] | None = None, source_identity: dict[str, Any] | None = None) -> str:
    doc = doc or {}
    identity = source_identity or (doc.get("source_identity") or {})
    for value in (
        identity.get("content_sha256"),
        doc.get("content_sha256"),
        doc.get("source_file_hash"),
    ):
        if value:
            return str(value)
    return ""


def chunk_hash(chunk: dict[str, Any] | None) -> str:
    chunk = chunk or {}
    for value in (chunk.get("chunk_hash"), chunk.get("text_hash")):
        if value:
            return str(value)
    return normalized_text_hash(chunk.get("text"))


def embedding_model_hash(doc: dict[str, Any] | None) -> str:
    doc = doc or {}
    cfg = doc.get("ingestion_config") or {}
    return stable_stage_hash(
        {
            "embedding_model_id": doc.get("embedding_model_id") or cfg.get("embedding_model_id"),
            "embedding_model": cfg.get("embedding_model"),
            "embedding_dimension": cfg.get("embedding_dimension"),
            "embed_mode": cfg.get("embed_mode"),
            "embedding_models": cfg.get("embedding_models"),
        }
    )


def document_stage_identity(
    *,
    doc: dict[str, Any],
    pipeline_contract_hash: str,
) -> dict[str, Any]:
    return {
        "identity_version": "stage_identity.v1",
        "source_file_hash": source_file_hash(doc),
        "source_key": doc.get("source_key") or (doc.get("source_identity") or {}).get("source_key"),
        "embedding_model_hash": embedding_model_hash(doc),
        "pipeline_contract_hash": pipeline_contract_hash,
    }


def source_parse_stage_identity(
    *,
    item: dict[str, Any],
    batch: dict[str, Any] | None,
    source_fingerprint: str,
    source_parse_contract_hash: str,
) -> dict[str, Any]:
    source_identity = item.get("source_identity") or {}
    source_pointer = (
        item.get("stored_path")
        or item.get("source_path")
        or item.get("relative_path")
        or item.get("filename")
    )
    return {
        "identity_version": "stage_identity.v1",
        "source_file_hash": source_file_hash(item, source_identity),
        "source_key": item.get("source_key") or source_identity.get("source_key"),
        "source_fingerprint": source_fingerprint,
        "source_pointer": source_pointer,
        "source_pointer_hash": stable_stage_hash(
            {
                "source": item.get("source") or (batch or {}).get("source"),
                "source_path": item.get("source_path"),
                "stored_path": item.get("stored_path"),
                "relative_path": item.get("relative_path"),
                "filename": item.get("filename"),
                "size_bytes": item.get("size_bytes"),
                "mtime": item.get("mtime"),
            }
        ),
        "source_parse_contract_hash": source_parse_contract_hash,
    }


def extraction_stage_identity(
    *,
    chunk: dict[str, Any],
    doc: dict[str, Any] | None,
    extraction_contract_hash: str,
) -> dict[str, Any]:
    c_hash = chunk_hash(chunk)
    return {
        "identity_version": "stage_identity.v1",
        "source_file_hash": source_file_hash(doc),
        "source_key": (doc or {}).get("source_key") or ((doc or {}).get("source_identity") or {}).get("source_key"),
        "normalized_text_hash": normalized_text_hash(chunk.get("text")),
        "chunk_hash": c_hash,
        "chunk_version": chunk.get("chunk_version") or chunk.get("updated_at"),
        "doc_version": (doc or {}).get("updated_at"),
        "extraction_contract_hash": extraction_contract_hash,
    }


def summary_stage_identity(
    *,
    source: dict[str, Any],
    doc: dict[str, Any] | None,
    source_hash: str,
    summary_contract_hash: str,
) -> dict[str, Any]:
    return {
        "identity_version": "stage_identity.v1",
        "source_file_hash": source_file_hash(doc or source),
        "source_key": (doc or {}).get("source_key") or ((doc or {}).get("source_identity") or {}).get("source_key"),
        "normalized_text_hash": normalized_text_hash(source.get("text") or source.get("summary_input") or ""),
        "source_hash": source_hash,
        "summary_contract_hash": summary_contract_hash,
    }


def graph_promotion_stage_identity(
    *,
    doc: dict[str, Any],
    extraction_artifact_ids: list[str] | tuple[str, ...],
    graph_contract_hash: str,
) -> dict[str, Any]:
    artifact_ids = sorted({str(value) for value in extraction_artifact_ids if str(value)})
    return {
        "identity_version": "stage_identity.v1",
        "source_file_hash": source_file_hash(doc),
        "source_key": doc.get("source_key") or (doc.get("source_identity") or {}).get("source_key"),
        "doc_version": doc.get("updated_at"),
        "extraction_artifact_ids": artifact_ids,
        "extraction_artifact_set_hash": stable_stage_hash(artifact_ids),
        "graph_contract_hash": graph_contract_hash,
    }
