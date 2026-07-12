"""Runpod Flash queue adapter for burst GLiNER-Relex extraction.

Flash is compute only. The remote function receives bounded text batches and
returns the same validated wire shape as ``ghost_b_local``; this process keeps
ownership of durable jobs, validation, Mongo staging, and graph promotion.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from typing import Any

import httpx

from models.schemas import RunpodFlashExtractionSettings
from services.ghost_b import (
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    ExtractionBatchReport,
    ExtractionFailureItem,
    SchemaContext,
)
from services.ghost_b_local import SCHEMA_VERSION, _to_results
from services.ghost_b_schemas import LLMEntity, LLMRelation
from services.ingestion.enrich import extract_aliases, extract_definitional_phrases

logger = logging.getLogger(__name__)

RUNPOD_API_BASE = "https://api.runpod.ai/v2"
RUNPOD_CONTRACT_VERSION = "polymath.runpod_gliner_relex.v2"
TERMINAL_FAILURES = {"FAILED", "CANCELLED", "TIMED_OUT"}


def _task_dict(task: Any) -> dict[str, Any]:
    data = task.model_dump() if hasattr(task, "model_dump") else vars(task)
    metadata = data.get("metadata") or {}
    return {
        "chunk_id": str(data.get("chunk_id") or ""),
        "doc_id": str(data.get("doc_id") or ""),
        "corpus_id": str(data.get("corpus_id") or ""),
        "text": str(data.get("text") or ""),
        "chunk_kind": str(data.get("chunk_kind") or "body"),
        "columns": list(metadata.get("columns") or []),
    }


def _batch_id(tasks: list[dict[str, Any]], config: RunpodFlashExtractionSettings) -> str:
    identity = {
        "chunks": [task["chunk_id"] for task in tasks],
        "model": config.model_id,
        "model_revision": config.model_revision,
        "entity_threshold": config.entity_threshold,
        "adjacency_threshold": config.adjacency_threshold,
        "relation_threshold": config.relation_threshold,
        "entity_lens_enabled": config.entity_lens_enabled,
        "entity_lens_max_labels": config.entity_lens_max_labels,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"runpod-flash:{digest}"


def _safe_error(value: Any) -> str:
    text = str(value or "runpod request failed").replace("\n", " ")
    return text[:1000]


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return max(0.25, min(float(raw), 30.0))
        except ValueError:
            pass
    return min(float(2**attempt), 8.0)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * weight)


def _extract_output(payload: dict[str, Any]) -> dict[str, Any]:
    output: Any = payload.get("output", payload)
    if isinstance(output, dict) and isinstance(output.get("output"), dict):
        output = output["output"]
    if not isinstance(output, dict):
        raise RuntimeError("Runpod job returned a non-object output")
    if output.get("success") is False:
        raise RuntimeError(
            f"Runpod worker rejected the job: {_safe_error(output.get('error'))}"
        )
    return output


async def _cancel_job(
    client: httpx.AsyncClient,
    endpoint_id: str,
    job_id: str,
    headers: dict[str, str],
) -> None:
    try:
        await client.post(
            f"{RUNPOD_API_BASE}/{endpoint_id}/cancel/{job_id}", headers=headers
        )
    except Exception:  # noqa: BLE001 - cancellation is best effort
        logger.debug("Runpod cancellation failed endpoint=%s job=%s", endpoint_id, job_id)


async def _submit_and_wait(
    client: httpx.AsyncClient,
    *,
    endpoint_id: str,
    api_key: str,
    request: dict[str, Any],
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    submit_url = f"{RUNPOD_API_BASE}/{endpoint_id}/run"
    response: httpx.Response | None = None
    for attempt in range(3):
        # Flash's generated handler calls ``extract_batch(**job_input)``. The
        # worker function has one named argument (payload), so direct REST
        # calls must preserve that keyword envelope.
        response = await client.post(
            submit_url,
            headers=headers,
            json={"input": {"payload": request}},
        )
        if response.status_code < 500 and response.status_code != 429:
            break
        if attempt < 2:
            await asyncio.sleep(_retry_delay(response, attempt))
    assert response is not None
    response.raise_for_status()
    submitted = response.json()
    job_id = str(submitted.get("id") or "")
    if not job_id:
        raise RuntimeError("Runpod submission returned no job id")

    deadline = time.monotonic() + timeout_seconds
    status_url = f"{RUNPOD_API_BASE}/{endpoint_id}/status/{job_id}"
    try:
        while True:
            if time.monotonic() >= deadline:
                await _cancel_job(client, endpoint_id, job_id, headers)
                raise TimeoutError(
                    f"Runpod job exceeded {timeout_seconds}s timeout"
                )
            status_response: httpx.Response | None = None
            for attempt in range(3):
                status_response = await client.get(status_url, headers=headers)
                if status_response.status_code < 500 and status_response.status_code != 429:
                    break
                if attempt < 2:
                    await asyncio.sleep(_retry_delay(status_response, attempt))
            assert status_response is not None
            status_response.raise_for_status()
            body = status_response.json()
            status = str(body.get("status") or "").upper()
            if status == "COMPLETED":
                output = _extract_output(body)
                output["_runpod_job"] = {
                    "job_id": job_id,
                    "delay_time_ms": body.get("delayTime"),
                    "execution_time_ms": body.get("executionTime"),
                }
                return output
            if status in TERMINAL_FAILURES:
                raise RuntimeError(
                    f"Runpod job {status.lower()}: {_safe_error(body.get('error'))}"
                )
            await asyncio.sleep(poll_interval_seconds)
    except asyncio.CancelledError:
        await _cancel_job(client, endpoint_id, job_id, headers)
        raise


def _validate_wire_result(
    row: dict[str, Any],
    *,
    task: dict[str, Any],
    schema: SchemaContext,
) -> dict[str, Any]:
    text = task["text"]
    allowed_entities = set(schema.entity_vocab)
    allowed_relations = set(schema.relation_vocab)
    strict = str(schema.strict or "soft")
    entity_drop = relation_drop = evidence_drop = 0
    entities: list[dict[str, Any]] = []
    canonical_names: set[str] = set()

    for raw in row.get("entities") or []:
        if not isinstance(raw, dict):
            entity_drop += 1
            continue
        surface = str(raw.get("surface_form") or raw.get("text") or "").strip()
        canonical = str(raw.get("canonical_name") or "").strip()
        entity_type = str(raw.get("entity_type") or raw.get("label") or "other")
        if not surface or not canonical:
            entity_drop += 1
            continue
        try:
            start = int(raw.get("char_start"))
            end = int(raw.get("char_end"))
        except (TypeError, ValueError):
            start = text.lower().find(surface.lower())
            end = start + len(surface) if start >= 0 else -1
        if start < 0 or end <= start or text[start:end] != surface:
            start = text.lower().find(surface.lower())
            end = start + len(surface) if start >= 0 else -1
        if start < 0 or end <= start or text[start:end].lower() != surface.lower():
            entity_drop += 1
            continue
        if entity_type not in allowed_entities:
            if strict == "hard":
                entity_drop += 1
                continue
            entity_type = SchemaContext.ENTITY_SENTINEL
        try:
            valid = LLMEntity(
                canonical_name=canonical,
                surface_form=surface[:300],
                entity_type=entity_type,
                confidence=float(raw.get("confidence") or raw.get("score") or 0.0),
                query_aliases=[
                    str(value)
                    for value in (raw.get("query_aliases") or [])
                    if str(value).strip()
                ][:5],
                definitional_phrase=str(raw.get("definitional_phrase") or "")[:200],
                object_kind=str(raw.get("object_kind") or "")[:100],
            )
        except Exception:  # noqa: BLE001 - strict external artifact boundary
            entity_drop += 1
            continue
        dumped = valid.model_dump()
        dumped["char_start"] = start
        dumped["char_end"] = end
        entities.append(dumped)
        canonical_names.add(canonical)

    aliases = extract_aliases(text, entities)
    definitions = extract_definitional_phrases(text, entities)
    for entity in entities:
        canonical = str(entity.get("canonical_name") or "")
        entity["query_aliases"] = list(
            dict.fromkeys(
                [
                    *(entity.get("query_aliases") or []),
                    *(aliases.get(canonical) or []),
                ]
            )
        )[:5]
        if not entity.get("definitional_phrase") and definitions.get(canonical):
            entity["definitional_phrase"] = definitions[canonical][:200]

    relations: list[dict[str, Any]] = []
    for raw in row.get("relations") or []:
        if not isinstance(raw, dict):
            relation_drop += 1
            continue
        subject = str(raw.get("subject") or "").strip()
        object_ = str(raw.get("object") or "").strip()
        predicate = str(raw.get("predicate") or raw.get("relation") or "").strip()
        evidence = str(raw.get("evidence_phrase") or "").strip()
        if subject not in canonical_names or object_ not in canonical_names:
            relation_drop += 1
            continue
        if predicate not in allowed_relations:
            if strict == "hard":
                relation_drop += 1
                continue
            predicate = SchemaContext.RELATION_SENTINEL
        if not evidence or evidence not in text:
            evidence_drop += 1
            continue
        try:
            valid = LLMRelation(
                subject=subject,
                predicate=predicate,
                object=object_,
                object_kind="entity",
                confidence=float(raw.get("confidence") or raw.get("score") or 0.0),
                evidence_phrase=evidence[:500],
                relation_cue=str(raw.get("relation_cue") or "")[:200],
            )
        except Exception:  # noqa: BLE001
            relation_drop += 1
            continue
        relations.append(valid.model_dump())

    return {
        "schema_version": SCHEMA_VERSION,
        "chunk_id": task["chunk_id"],
        "doc_id": task["doc_id"],
        "corpus_id": task["corpus_id"],
        "entities": entities,
        "relations": relations,
        "facts": [],
        "text": text,
        "entity_drop_count": entity_drop,
        "relation_drop_count": relation_drop,
        "evidence_drop_count": evidence_drop,
        "fact_drop_count": 0,
        "schema_lens_id": row.get("schema_lens_id"),
    }


async def extract_entities(
    tasks: list[Any],
    model: str | None = None,
    schema: SchemaContext | None = None,
    schema_lens: Any = None,
    chunk_vectors: dict[str, list[float]] | None = None,
    schema_resolver: Any = None,
    *,
    pool: list[dict] | None = None,
    return_report: bool = False,
    enable_facts: bool | None = None,
    audit_event_sink: Any = None,
    audit_run_id: str | None = None,
    runpod_config: RunpodFlashExtractionSettings | None = None,
    runpod_api_key: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> list[Any] | ExtractionBatchReport:
    """Extract one task set through a bounded Runpod Flash queue endpoint."""

    del model, schema_lens, chunk_vectors, schema_resolver, pool, enable_facts
    del audit_event_sink, audit_run_id
    if not tasks:
        return []
    if runpod_config is None or runpod_api_key is None:
        from services.settings import settings_service

        stored_config, stored_key = await settings_service.get_system_runpod_flash()
        runpod_config = runpod_config or stored_config
        runpod_api_key = runpod_api_key or stored_key
    if not runpod_config.enabled:
        raise RuntimeError("Runpod Flash extraction is disabled in Settings")
    endpoint_id = runpod_config.endpoint_id.strip()
    if not endpoint_id:
        raise RuntimeError("Runpod Flash endpoint_id is not configured in Settings")
    if not runpod_api_key:
        raise RuntimeError("Runpod API key is not configured in Settings")

    schema = schema or SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    if not schema.entity_vocab or not schema.relation_vocab:
        schema = SchemaContext(
            entity_schema=schema.entity_schema or list(UNIVERSAL_ENTITY_SCHEMA),
            relation_schema=schema.relation_schema or list(UNIVERSAL_RELATION_SCHEMA),
            strict=schema.strict,
        )
    task_dicts = [_task_dict(task) for task in tasks]
    by_chunk = {task["chunk_id"]: task for task in task_dicts}
    slices = [
        task_dicts[start : start + runpod_config.request_batch_size]
        for start in range(0, len(task_dicts), runpod_config.request_batch_size)
    ]
    semaphore = asyncio.Semaphore(runpod_config.request_concurrency)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(runpod_config.timeout_seconds + 30, connect=20)
    )
    started = time.perf_counter()

    async def run_slice(index: int, batch: list[dict[str, Any]]) -> tuple[int, Any]:
        request = {
            "contract_version": RUNPOD_CONTRACT_VERSION,
            "batch_id": _batch_id(batch, runpod_config),
            "tasks": batch,
            "model_id": runpod_config.model_id,
            "model_revision": runpod_config.model_revision,
            "spacy_pipeline": runpod_config.spacy_pipeline,
            # Sentinels are validation fallbacks, not extraction targets. Giving
            # a zero-shot model the generic ``other`` label causes it to absorb
            # specific classes and prevents coherent relation candidates.
            "entity_labels": list(schema.entity_schema or schema.entity_vocab),
            "relation_labels": list(schema.relation_schema or schema.relation_vocab),
            "entity_threshold": runpod_config.entity_threshold,
            "adjacency_threshold": runpod_config.adjacency_threshold,
            "relation_threshold": runpod_config.relation_threshold,
            "entity_lens_enabled": runpod_config.entity_lens_enabled,
            "entity_lens_max_labels": runpod_config.entity_lens_max_labels,
            "model_batch_size": runpod_config.model_batch_size,
            "max_window_words": runpod_config.max_window_words,
        }
        async with semaphore:
            try:
                output = await _submit_and_wait(
                    client,
                    endpoint_id=endpoint_id,
                    api_key=runpod_api_key or "",
                    request=request,
                    timeout_seconds=runpod_config.timeout_seconds,
                    poll_interval_seconds=runpod_config.poll_interval_seconds,
                )
                if output.get("contract_version") != RUNPOD_CONTRACT_VERSION:
                    raise RuntimeError("Runpod worker contract revision mismatch")
                return index, output
            except Exception as exc:  # noqa: BLE001 - durable failures below
                return index, exc

    try:
        completed = await asyncio.gather(
            *(run_slice(index, batch) for index, batch in enumerate(slices))
        )
    finally:
        if owns_client:
            await client.aclose()

    raw_results: list[dict[str, Any]] = []
    failures: list[ExtractionFailureItem] = []
    remote_metrics: list[dict[str, Any]] = []
    for index, outcome in sorted(completed, key=lambda item: item[0]):
        batch = slices[index]
        if isinstance(outcome, Exception):
            for task in batch:
                failures.append(
                    ExtractionFailureItem(
                        chunk_id=task["chunk_id"],
                        doc_id=task["doc_id"],
                        corpus_id=task["corpus_id"],
                        model=runpod_config.model_id,
                        lane=index,
                        attempts=1,
                        error_type=type(outcome).__name__,
                        error_message=_safe_error(outcome),
                        provider="runpod_flash",
                        schema_mode="ontology_labels",
                        output_mode="joint_relex",
                        json_repair_mode="deterministic_wire_validation",
                        semantic_verifier_mode="strict",
                    )
                )
            continue
        rows = outcome.get("results") or []
        remote_metrics.append(
            {
                **(outcome.get("metrics") or {}),
                "runpod_job": outcome.get("_runpod_job") or {},
            }
        )
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            chunk_id = str(row.get("chunk_id") or "")
            task = by_chunk.get(chunk_id)
            if task is None or chunk_id in seen:
                continue
            seen.add(chunk_id)
            raw_results.append(_validate_wire_result(row, task=task, schema=schema))
        for task in batch:
            if task["chunk_id"] in seen:
                continue
            failures.append(
                ExtractionFailureItem(
                    chunk_id=task["chunk_id"],
                    doc_id=task["doc_id"],
                    corpus_id=task["corpus_id"],
                    model=runpod_config.model_id,
                    lane=index,
                    attempts=1,
                    error_type="missing_remote_artifact",
                    error_message="Runpod batch completed without this chunk artifact",
                    provider="runpod_flash",
                    schema_mode="ontology_labels",
                    output_mode="joint_relex",
                    json_repair_mode="deterministic_wire_validation",
                    semantic_verifier_mode="strict",
                )
            )

    results = _to_results(raw_results)
    for result in results:
        result.model = runpod_config.model_id
        result.provider = "runpod_flash"
        result.schema_mode = "ontology_labels"
        result.output_mode = "joint_relex"
        result.json_repair_mode = "deterministic_wire_validation"
        result.semantic_verifier_mode = "strict"
        result.provider_card = {
            "provider": "runpod_flash",
            "model": runpod_config.model_id,
            "model_revision": runpod_config.model_revision,
            "endpoint": endpoint_id,
            "schema_mode": "ontology_labels",
            "concurrency_policy": "serverless_burst",
        }
    duration = time.perf_counter() - started
    execution_ms = sum(
        float((item.get("runpod_job") or {}).get("execution_time_ms") or 0.0)
        for item in remote_metrics
    )
    delay_ms = sum(
        float((item.get("runpod_job") or {}).get("delay_time_ms") or 0.0)
        for item in remote_metrics
    )
    estimated_compute_cost = (
        (execution_ms / 1000.0)
        * runpod_config.estimated_gpu_rate_per_second_usd
        * runpod_config.cost_overhead_multiplier
    )
    emitted_entities = sum(
        int(item.get("entities_emitted") or 0) for item in remote_metrics
    )
    emitted_relations = sum(
        int(item.get("relations_emitted") or 0) for item in remote_metrics
    )
    validation_drops = sum(result.validation_rejection_count for result in results)
    emitted_items = emitted_entities + emitted_relations
    schema_pass_rate = (
        max(0.0, (emitted_items - validation_drops) / emitted_items)
        if emitted_items
        else 1.0
    )
    batch_durations = [
        float(item.get("duration_seconds") or 0.0) for item in remote_metrics
    ]
    batch_throughputs = [
        float(item.get("chunks_per_second") or 0.0) for item in remote_metrics
    ]
    remote_summary = {
        "batches": len(remote_metrics),
        "model_sources": sorted(
            {
                str(item.get("model_source"))
                for item in remote_metrics
                if item.get("model_source")
            }
        ),
        "entity_lens_groups": sum(
            int(item.get("entity_lens_groups") or 0) for item in remote_metrics
        ),
        "entity_lens_second_pass_windows": sum(
            int(item.get("entity_lens_second_pass_windows") or 0)
            for item in remote_metrics
        ),
        "batch_duration_seconds_p50": round(
            _percentile(batch_durations, 0.5) or 0.0, 4
        ),
        "batch_duration_seconds_p95": round(
            _percentile(batch_durations, 0.95) or 0.0, 4
        ),
        "batch_duration_seconds_max": round(max(batch_durations, default=0.0), 4),
        "batch_chunks_per_second_p50": round(
            _percentile(batch_throughputs, 0.5) or 0.0, 3
        ),
        "batch_chunks_per_second_p95": round(
            _percentile(batch_throughputs, 0.95) or 0.0, 3
        ),
    }
    metrics = {
        "engine": "runpod_flash",
        "model": runpod_config.model_id,
        "model_revision": runpod_config.model_revision or None,
        "requested_chunks": len(task_dicts),
        "extracted_chunks": len(results),
        "failed_chunks": len(failures),
        "request_batches": len(slices),
        "request_batch_size": runpod_config.request_batch_size,
        "request_concurrency": runpod_config.request_concurrency,
        "duration_seconds": round(duration, 3),
        "chunks_per_second": round(len(results) / duration, 3) if duration else 0.0,
        "aggregate_execution_seconds": round(execution_ms / 1000.0, 3),
        "aggregate_queue_delay_seconds": round(delay_ms / 1000.0, 3),
        "estimated_compute_cost_usd": round(estimated_compute_cost, 6),
        "estimated_cost_only": True,
        "budget_cap_usd": runpod_config.budget_cap_usd,
        "estimated_budget_exceeded": bool(
            math.isfinite(estimated_compute_cost)
            and runpod_config.budget_cap_usd > 0
            and estimated_compute_cost > runpod_config.budget_cap_usd
        ),
        "remote_entities_emitted": emitted_entities,
        "remote_relations_emitted": emitted_relations,
        "validation_drop_count": validation_drops,
        "schema_evidence_pass_rate": round(schema_pass_rate, 6),
        "remote": remote_summary,
    }
    report = ExtractionBatchReport(results=results, failures=failures, metrics=metrics)
    return report if return_report else results
