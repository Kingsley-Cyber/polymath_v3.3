"""Strict additive adapter for the certified RunPod LocalExtractionV1 worker.

This path is selected only by a frozen per-corpus wire-contract override.  It
reuses the legacy RunPod HTTP transport but never its v2/v3 request builder,
multi-account failover, or wire-to-ontology adapter.  One explicitly named
encrypted account key is paired with one explicitly pinned endpoint, and every
remote artifact is recompiled against the canonical local spaCy observations
before it can enter durable ingestion state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import httpx

from models.extraction_registry import extraction_registry_hashes
from models.local_extraction import LocalExtractionV1
from models.schemas import RunpodFlashAccount, RunpodFlashExtractionSettings
from services.ghost_b import (
    EntityItem,
    ExtractionBatchReport,
    ExtractionResult,
)
from services.ingestion.claim_compiler import compile_claim_records_v1
from services.ingestion.semantic_observations import (
    build_spacy_observation_bundle,
    compile_local_extraction_v1,
)
from services.runpod_flash_extraction import _submit_and_wait


CONTRACT_VERSION = "polymath.runpod_local_extraction.v1"
DETERMINISM_PROFILE = "polymath.torch_cuda_deterministic.v1"
EXPECTED_SOURCE_CLOSURE_SHA256 = (
    "2e47c86fe41db25b3a0fc81408ff775a829be59871a5479a1bfd1a4dad0e8010"
)
SPACY_VERSION = "3.8.14"
SPACY_MODEL = "en_core_web_sm"
SPACY_MODEL_VERSION = "3.8.0"
PARSER_VERSION = "spacy:3.8.14;model:3.8.0"
GLINER_MODEL_ID = "urchade/gliner_medium-v2.1"
GLINER_MODEL_REVISION = "40ec419335d09393f298636f471328b722c6da9e"
EXPECTED_ASSET_CONTRACT = {
    "extraction_vocabulary_sha256": (
        "47ea44fee2341c3cc65ef2bb4f99795947aa0c1cc9e1d55314efc7647af89612"
    ),
    "predicate_normalization_sha256": (
        "0ba7cdc3d8dd6f643e7ccce74b46f4711940947fa73020adaf130f5efd727ce8"
    ),
    "gliner_config_sha256": (
        "a8f3c2ecc57deb70077be6940962aa60e82d861a153a5cd2839b91795968ae7d"
    ),
    "gliner_weights_sha256": (
        "922214c0c60f7835bb5c00f52ad1769d38518d5183f85de7bc03893a8403c023"
    ),
}
EXPECTED_DISTRIBUTIONS = {
    "gliner": "0.2.26",
    "huggingface-hub": "0.36.2",
    "numpy": "2.2.6",
    "pydantic": "2.13.4",
    "safetensors": "0.7.0",
    "sentencepiece": "0.2.1",
    "spacy": SPACY_VERSION,
    "tokenizers": "0.22.2",
    "torch": "2.12.0",
    "transformers": "4.57.6",
    "en-core-web-sm": SPACY_MODEL_VERSION,
}
EXPECTED_DETERMINISM = {
    "profile": DETERMINISM_PROFILE,
    "seed": 0,
    "environment": {
        "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
        "NVIDIA_TF32_OVERRIDE": "0",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "PYTHONHASHSEED": "0",
    },
    "torch_deterministic_algorithms": True,
    "torch_deterministic_warn_only": False,
    "torch_float32_matmul_precision": "highest",
    "cuda_matmul_allow_tf32": False,
    "cudnn_allow_tf32": False,
    "cudnn_benchmark": False,
    "cudnn_deterministic": True,
    "cuda_matmul_allow_fp16_reduced_precision_reduction": False,
    "cuda_matmul_allow_bf16_reduced_precision_reduction": False,
    "torch_num_threads": 1,
    "torch_num_interop_threads": 1,
    "cuda_available": True,
}
_OUTPUT_FIELDS = {
    "contract_version",
    "batch_id",
    "results",
    "runtime_identity",
    "metrics",
    "_runpod_job",
}
_RESULT_FIELDS = {
    "document_id",
    "child_id",
    "source_version_id",
    "extraction",
    "temporal_captures",
    "temporal_captures_truncated",
    "mention_selection_counts",
    "compilation_receipt",
}
_TEMPORAL_FIELDS = {
    "text",
    "char_start",
    "char_end",
    "detector",
    "role_candidates",
}
_NLP: Any = None


def _load_nlp() -> Any:
    global _NLP
    if _NLP is not None:
        return _NLP
    import importlib.metadata as metadata
    import spacy

    if metadata.version("spacy") != SPACY_VERSION:
        raise RuntimeError("canonical spaCy distribution differs from B7 lock")
    if metadata.version("en-core-web-sm") != SPACY_MODEL_VERSION:
        raise RuntimeError("canonical spaCy model distribution differs from B7 lock")
    loaded = spacy.load(SPACY_MODEL)
    if str(loaded.meta.get("version") or "") != SPACY_MODEL_VERSION:
        raise RuntimeError("canonical spaCy pipeline metadata differs from B7 lock")
    _NLP = loaded
    return loaded


def _task_dict(task: Any) -> dict[str, str]:
    data = task.model_dump() if hasattr(task, "model_dump") else vars(task)
    metadata = data.get("metadata") or {}
    row = {
        "document_id": str(data.get("doc_id") or "").strip(),
        "child_id": str(data.get("chunk_id") or "").strip(),
        "source_version_id": str(metadata.get("source_version_id") or "").strip(),
        "text": str(data.get("text") or ""),
        "corpus_id": str(data.get("corpus_id") or "").strip(),
    }
    if any(not row[key] for key in ("document_id", "child_id", "source_version_id")):
        raise ValueError("LocalExtractionV1 task identity is incomplete")
    if not row["text"].strip():
        raise ValueError("LocalExtractionV1 task text is empty")
    if len(row["text"]) > 200_000:
        raise ValueError("LocalExtractionV1 task text exceeds the wire bound")
    if not row["corpus_id"]:
        raise ValueError("LocalExtractionV1 task corpus identity is incomplete")
    return row


def _batch_id(tasks: list[dict[str, str]]) -> str:
    identity = {
        "contract_version": CONTRACT_VERSION,
        "tasks": [
            {
                "document_id": row["document_id"],
                "child_id": row["child_id"],
                "source_version_id": row["source_version_id"],
                "text_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
            }
            for row in tasks
        ],
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return f"runpod-local:{hashlib.sha256(encoded).hexdigest()}"


def _request(tasks: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "batch_id": _batch_id(tasks),
        "model_id": GLINER_MODEL_ID,
        "model_revision": GLINER_MODEL_REVISION,
        "spacy_pipeline": SPACY_MODEL,
        "asset_contract": dict(EXPECTED_ASSET_CONTRACT),
        "determinism_profile": DETERMINISM_PROFILE,
        "tasks": [
            {
                "document_id": row["document_id"],
                "child_id": row["child_id"],
                "source_version_id": row["source_version_id"],
                "text": row["text"],
            }
            for row in tasks
        ],
    }


def _validate_runtime_identity(identity: Any) -> None:
    if not isinstance(identity, dict):
        raise RuntimeError("LocalExtractionV1 runtime identity is missing")
    expected_scalars = {
        "python": "3.11.15",
        "spacy_model": SPACY_MODEL,
        "spacy_model_version": SPACY_MODEL_VERSION,
        "parser_version": PARSER_VERSION,
        "gliner_model_id": GLINER_MODEL_ID,
        "gliner_model_revision": GLINER_MODEL_REVISION,
    }
    if any(identity.get(key) != value for key, value in expected_scalars.items()):
        raise RuntimeError("LocalExtractionV1 runtime scalar identity drifted")
    if identity.get("distributions") != EXPECTED_DISTRIBUTIONS:
        raise RuntimeError("LocalExtractionV1 distribution closure drifted")
    if identity.get("asset_contract") != EXPECTED_ASSET_CONTRACT:
        raise RuntimeError("LocalExtractionV1 asset contract drifted")
    if identity.get("registry_namespace_hashes") != extraction_registry_hashes():
        raise RuntimeError("LocalExtractionV1 registry namespace hashes drifted")
    source_closure = identity.get("source_closure")
    if (
        not isinstance(source_closure, dict)
        or source_closure.get("closure_sha256") != EXPECTED_SOURCE_CLOSURE_SHA256
    ):
        raise RuntimeError("LocalExtractionV1 source closure drifted")
    if identity.get("determinism") != EXPECTED_DETERMINISM:
        raise RuntimeError("LocalExtractionV1 determinism attestation drifted")
    snapshot = identity.get("model_snapshot")
    if not isinstance(snapshot, dict) or snapshot != {
        "config_sha256": EXPECTED_ASSET_CONTRACT["gliner_config_sha256"],
        "weights_sha256": EXPECTED_ASSET_CONTRACT["gliner_weights_sha256"],
    }:
        raise RuntimeError("LocalExtractionV1 model snapshot drifted")


def _validate_temporal(rows: Any, text: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise RuntimeError("LocalExtractionV1 temporal captures are not a list")
    validated: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != _TEMPORAL_FIELDS:
            raise RuntimeError("LocalExtractionV1 temporal capture shape drifted")
        start, end = raw.get("char_start"), raw.get("char_end")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise RuntimeError("LocalExtractionV1 temporal offsets are invalid")
        if end > len(text) or text[start:end] != raw.get("text"):
            raise RuntimeError(
                "LocalExtractionV1 temporal span failed source round trip"
            )
        if raw.get("detector") not in {"regex", "spacy"}:
            raise RuntimeError("LocalExtractionV1 temporal detector is invalid")
        roles = raw.get("role_candidates")
        if not isinstance(roles, list) or any(type(role) is not str for role in roles):
            raise RuntimeError("LocalExtractionV1 temporal roles are invalid")
        if len(roles) != len(set(roles)):
            raise RuntimeError("LocalExtractionV1 temporal roles are duplicated")
        if any(left < end and start < right for left, right in occupied):
            raise RuntimeError("LocalExtractionV1 temporal captures overlap")
        occupied.append((start, end))
        validated.append(dict(raw))
    if occupied != sorted(occupied):
        raise RuntimeError("LocalExtractionV1 temporal captures are not ordered")
    return validated


def _compile_result(
    raw: Any,
    *,
    task: dict[str, str],
    nlp: Any,
    endpoint_id: str,
    account_name: str,
) -> ExtractionResult:
    if not isinstance(raw, dict) or set(raw) != _RESULT_FIELDS:
        raise RuntimeError("LocalExtractionV1 result shape drifted")
    if raw.get("document_id") != task["document_id"]:
        raise RuntimeError("LocalExtractionV1 document identity mismatch")
    if raw.get("child_id") != task["child_id"]:
        raise RuntimeError("LocalExtractionV1 child identity mismatch")
    if raw.get("source_version_id") != task["source_version_id"]:
        raise RuntimeError("LocalExtractionV1 source-version identity mismatch")
    try:
        extraction = LocalExtractionV1.model_validate(raw.get("extraction"))
    except Exception as exc:
        raise RuntimeError("LocalExtractionV1 schema validation failed") from exc
    if (
        extraction.document_id != task["document_id"]
        or extraction.child_id != task["child_id"]
    ):
        raise RuntimeError("LocalExtractionV1 extraction ownership mismatch")
    if extraction.relations:
        raise RuntimeError("LocalExtractionV1 relation lane must remain empty")
    for entity in extraction.entities:
        if not entity.canonical_label.strip():
            raise RuntimeError("LocalExtractionV1 entity canonical label is empty")
        if task["text"][entity.start_char : entity.end_char] != entity.text:
            raise RuntimeError("LocalExtractionV1 entity span failed source round trip")
    for predicate in extraction.predicates:
        if (
            task["text"][predicate.start_char : predicate.end_char]
            != predicate.surface_text
        ):
            raise RuntimeError(
                "LocalExtractionV1 predicate span failed source round trip"
            )

    bundle = build_spacy_observation_bundle(
        text=task["text"],
        nlp=nlp,
        source_version_id=task["source_version_id"],
        hierarchy_node_id=task["child_id"],
        parser_id=SPACY_MODEL,
        parser_version=PARSER_VERSION,
    )
    local_compile = compile_local_extraction_v1(
        bundle,
        document_id=task["document_id"],
        child_id=task["child_id"],
    )
    remote_predicate_view = extraction.model_dump(mode="json")
    remote_predicate_view["entities"] = []
    remote_predicate_view["relations"] = []
    if remote_predicate_view != local_compile.extraction.model_dump(mode="json"):
        raise RuntimeError("LocalExtractionV1 remote/local spaCy compilation drifted")
    if raw.get("compilation_receipt") != local_compile.receipt():
        raise RuntimeError("LocalExtractionV1 compilation receipt drifted")
    counts = raw.get("mention_selection_counts")
    if (
        not isinstance(counts, dict)
        or any(type(key) is not str for key in counts)
        or any(type(value) is not int or value < 0 for value in counts.values())
    ):
        raise RuntimeError("LocalExtractionV1 mention-selection accounting drifted")
    truncated = raw.get("temporal_captures_truncated")
    if type(truncated) is not bool:
        raise RuntimeError("LocalExtractionV1 temporal truncation flag is invalid")
    temporal = _validate_temporal(raw.get("temporal_captures"), task["text"])
    claim_compilation = compile_claim_records_v1(
        bundle=bundle,
        extraction=extraction,
    )
    entities = [
        EntityItem(
            canonical_name=item.canonical_label,
            surface_form=item.text,
            entity_type=item.entity_type,
            confidence=item.confidence,
        )
        for item in extraction.entities
    ]
    return ExtractionResult(
        schema_version="polymath.extract.local_extraction.v1",
        chunk_id=task["child_id"],
        doc_id=task["document_id"],
        corpus_id=task["corpus_id"],
        entities=entities,
        relations=[],
        facts=[],
        text=task["text"],
        temporal_captures=temporal,
        temporal_capture_version=CONTRACT_VERSION,
        source_version_id=task["source_version_id"],
        local_extraction=extraction.model_dump(mode="json"),
        claim_compilation=claim_compilation.model_dump(mode="json"),
        model=GLINER_MODEL_ID,
        provider="runpod_local_extraction",
        attempts=1,
        schema_mode="local_extraction.v1",
        output_mode="deterministic_spans",
        json_repair_mode="none_fail_closed",
        semantic_verifier_mode="strict_remote_local_recompile",
        provider_card={
            "provider": "runpod_flash",
            "model": GLINER_MODEL_ID,
            "model_revision": GLINER_MODEL_REVISION,
            "endpoint": endpoint_id,
            "account": account_name,
            "wire_contract": CONTRACT_VERSION,
            "concurrency_policy": "single_account_pinned_endpoint",
        },
    )


async def extract_entities(
    tasks: list[Any],
    schema: Any = None,
    schema_lens: Any = None,
    chunk_vectors: Any = None,
    schema_resolver: Any = None,
    *,
    pool: list[dict[str, Any]] | None = None,
    model: str | None = None,
    return_report: bool = False,
    enable_facts: bool | None = None,
    audit_event_sink: Any = None,
    audit_run_id: str | None = None,
    endpoint_id: str | None,
    account_name: str | None,
    user_id: str | None = None,
    runpod_config: RunpodFlashExtractionSettings | None = None,
    accounts: list[tuple[RunpodFlashAccount, str]] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> list[ExtractionResult] | ExtractionBatchReport:
    """Dispatch through one named encrypted account and one pinned endpoint."""

    del schema, schema_lens, chunk_vectors, schema_resolver, pool, model
    del enable_facts, audit_event_sink, audit_run_id, user_id
    if not tasks:
        empty = ExtractionBatchReport(
            results=[],
            failures=[],
            metrics={
                "engine": "runpod_local_extraction",
                "requested_chunks": 0,
                "extracted_chunks": 0,
                "failed_chunks": 0,
            },
        )
        return empty if return_report else []
    endpoint = str(endpoint_id or "").strip()
    selected_name = str(account_name or "").strip()
    if not endpoint or not selected_name:
        raise RuntimeError(
            "LocalExtractionV1 requires explicit endpoint and account identity"
        )
    if not endpoint.isalnum():
        raise RuntimeError("LocalExtractionV1 endpoint identity is malformed")
    if runpod_config is None or accounts is None:
        from services.settings import settings_service

        if runpod_config is None:
            runpod_config, _ = await settings_service.get_system_runpod_flash()
        if accounts is None:
            accounts = await settings_service.get_system_runpod_flash_accounts()
    if not runpod_config.enabled:
        raise RuntimeError("RunPod extraction is disabled in system Settings")
    matches = [
        (account, key)
        for account, key in accounts
        if account.enabled and account.name == selected_name and key
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "LocalExtractionV1 named account did not resolve exactly once"
        )
    account, api_key = matches[0]
    task_rows = [_task_dict(task) for task in tasks]
    if len({row["child_id"] for row in task_rows}) != len(task_rows):
        raise RuntimeError("LocalExtractionV1 child identities are not unique")
    batch_size = min(64, int(runpod_config.request_batch_size))
    slices = [
        task_rows[start : start + batch_size]
        for start in range(0, len(task_rows), batch_size)
    ]
    request_limit = min(
        int(runpod_config.request_concurrency),
        int(account.request_concurrency),
    )
    semaphore = asyncio.Semaphore(max(1, request_limit))
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(runpod_config.timeout_seconds + 30, connect=20)
    )
    started = time.perf_counter()

    async def run_slice(index: int, rows: list[dict[str, str]]) -> tuple[int, Any]:
        request = _request(rows)
        async with semaphore:
            output = await _submit_and_wait(
                client,
                endpoint_id=endpoint,
                api_key=api_key,
                request=request,
                timeout_seconds=runpod_config.timeout_seconds,
                poll_interval_seconds=runpod_config.poll_interval_seconds,
            )
        if not isinstance(output, dict) or set(output) != _OUTPUT_FIELDS:
            raise RuntimeError("LocalExtractionV1 response shape drifted")
        if output.get("contract_version") != CONTRACT_VERSION:
            raise RuntimeError("LocalExtractionV1 response contract drifted")
        if output.get("batch_id") != request["batch_id"]:
            raise RuntimeError("LocalExtractionV1 response batch identity drifted")
        _validate_runtime_identity(output.get("runtime_identity"))
        metrics = output.get("metrics")
        if (
            not isinstance(metrics, dict)
            or metrics.get("chunks") != len(rows)
            or metrics.get("relations") != 0
        ):
            raise RuntimeError("LocalExtractionV1 remote metrics failed closure")
        remote_rows = output.get("results")
        if not isinstance(remote_rows, list) or len(remote_rows) != len(rows):
            raise RuntimeError("LocalExtractionV1 response cardinality mismatch")
        return index, output

    try:
        completed = await asyncio.gather(
            *(run_slice(index, rows) for index, rows in enumerate(slices))
        )
    finally:
        if owns_client:
            await client.aclose()

    nlp = _load_nlp()
    results_by_child: dict[str, ExtractionResult] = {}
    remote_jobs: list[dict[str, Any]] = []
    for index, output in sorted(completed, key=lambda item: item[0]):
        rows = slices[index]
        task_by_child = {row["child_id"]: row for row in rows}
        for raw in output["results"]:
            if not isinstance(raw, dict):
                raise RuntimeError("LocalExtractionV1 result is not an object")
            child_id = str(raw.get("child_id") or "")
            task = task_by_child.get(child_id)
            if task is None or child_id in results_by_child:
                raise RuntimeError(
                    "LocalExtractionV1 returned an unknown/duplicate child"
                )
            results_by_child[child_id] = _compile_result(
                raw,
                task=task,
                nlp=nlp,
                endpoint_id=endpoint,
                account_name=selected_name,
            )
        remote_jobs.append(dict(output.get("_runpod_job") or {}))
    expected_children = {row["child_id"] for row in task_rows}
    if set(results_by_child) != expected_children:
        raise RuntimeError("LocalExtractionV1 result closure is incomplete")
    ordered = [results_by_child[row["child_id"]] for row in task_rows]
    duration = time.perf_counter() - started
    metrics = {
        "engine": "runpod_local_extraction",
        "wire_contract": CONTRACT_VERSION,
        "model": GLINER_MODEL_ID,
        "model_revision": GLINER_MODEL_REVISION,
        "requested_chunks": len(task_rows),
        "extracted_chunks": len(ordered),
        "failed_chunks": 0,
        "request_batches": len(slices),
        "request_batch_size": batch_size,
        "request_concurrency": request_limit,
        "account": selected_name,
        "endpoint": endpoint,
        "duration_seconds": round(duration, 3),
        "chunks_per_second": round(len(ordered) / duration, 3) if duration else 0.0,
        "remote_jobs": remote_jobs,
        "claims_compiled": sum(
            len((row.claim_compilation or {}).get("claims") or []) for row in ordered
        ),
        "relations": 0,
    }
    report = ExtractionBatchReport(results=ordered, failures=[], metrics=metrics)
    return report if return_report else ordered
