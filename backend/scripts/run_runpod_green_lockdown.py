#!/usr/bin/env python3
"""Run B4/B5/B6 against the discovered private RunPod green endpoint.

The script runs inside the backend container so RunPod API keys never leave
the encrypted-settings boundary. The semantic gold fixture contributes only
the preregistered sample IDs and source text; no expected entity/relation row
is loaded. Green endpoint IDs are discovered by name from the RunPod API.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from models.extraction_registry import load_extraction_registries
from models.local_extraction import LocalExtractionV1


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = ROOT / "evals/runpod_same_chunk_lockdown_v1.json"
GREEN_NAME = "polymath-local-extraction-green-08385fa"
CONTRACT_VERSION = "polymath.runpod_local_extraction.v1"
RUNPOD_API_BASE = "https://api.runpod.ai/v2"
GRAPHQL_URL = "https://api.runpod.io/graphql"
TERMINAL_FAILURES = {"FAILED", "CANCELLED", "TIMED_OUT"}

ENDPOINT_QUERY = """
query EndpointState {
  myself {
    endpoints {
      id name templateId gpuIds gpuCount idleTimeout scalerType scalerValue
      workersMin workersMax executionTimeoutMs minCudaVersion flashBootType
    }
    podTemplates { id imageName containerRegistryAuthId }
  }
}
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_tasks(spec_path: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if spec.get("schema_version") != "polymath.runpod_same_chunk_lockdown.v1":
        raise ValueError("unsupported same-chunk spec")
    source = ROOT / spec["source_fixture"]["path"].removeprefix("backend/")
    if sha256(source) != spec["source_fixture"]["sha256"]:
        raise ValueError("same-chunk source fixture hash drifted")
    fixture = json.loads(source.read_text(encoding="utf-8"))
    # Anti-gaming: no fixture field other than id/text is read.
    text_by_id = {str(row["id"]): str(row["text"]) for row in fixture["samples"]}
    samples = [
        {"id": sample_id, "text": text_by_id[sample_id]}
        for sample_id in spec["source_fixture"]["sample_ids"]
    ]
    for row in spec["synthetic_samples"]:
        text = (
            str(row["text"])
            if "text" in row
            else str(row["repeat_text"]) * int(row["repeat_count"]) + str(row["suffix"])
        )
        samples.append({"id": str(row["id"]), "text": text})
    tasks = [
        {
            "document_id": f"doc:runpod-lockdown:{row['id']}",
            "child_id": f"child:runpod-lockdown:{row['id']}",
            "source_version_id": f"srcv:runpod-lockdown:{row['id']}",
            "text": row["text"],
        }
        for row in samples
    ]
    if len(tasks) != 12 or len({row["child_id"] for row in tasks}) != 12:
        raise ValueError("same-chunk task cardinality/identity drifted")
    return spec, tasks


def build_request(
    baseline: dict[str, Any], tasks: list[dict[str, str]]
) -> dict[str, Any]:
    identity = baseline["runtime_identity"]
    request = {
        "contract_version": CONTRACT_VERSION,
        "batch_id": "runpod-lockdown:same-chunk-v1",
        "model_id": identity["gliner_model_id"],
        "model_revision": identity["gliner_model_revision"],
        "spacy_pipeline": identity["spacy_model"],
        "asset_contract": identity["asset_contract"],
        "tasks": tasks,
    }
    if canonical_hash(tasks) != baseline["task_input_sha256"]:
        raise ValueError("task input hash differs from frozen local reference")
    return request


def _mongo() -> tuple[AsyncIOMotorClient, Any, Any]:
    from config import get_settings
    from services.settings import settings_service

    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client.get_default_database()
    except Exception:  # noqa: BLE001
        db = client[settings.MONGODB_DATABASE]
    settings_service.attach(db)
    return client, db, settings_service


async def _discover_green(
    client: httpx.AsyncClient, api_key: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    response = await client.post(
        GRAPHQL_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"query": ENDPOINT_QUERY},
    )
    body = response.json()
    if response.status_code >= 400 or body.get("errors"):
        raise RuntimeError("RunPod green discovery failed")
    myself = (body.get("data") or {}).get("myself") or {}
    matches = [
        row for row in myself.get("endpoints") or [] if row.get("name") == GREEN_NAME
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"green endpoint must match exactly once; observed={len(matches)}"
        )
    endpoint = matches[0]
    templates = [
        row
        for row in myself.get("podTemplates") or []
        if row.get("id") == endpoint.get("templateId")
    ]
    if len(templates) != 1:
        raise RuntimeError("green endpoint template must match exactly once")
    return str(endpoint["id"]), endpoint, templates[0]


async def _submit_and_wait(
    client: httpx.AsyncClient,
    *,
    endpoint_id: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    headers = {"Authorization": f"Bearer {api_key}"}
    started = time.monotonic()
    response = await client.post(
        f"{RUNPOD_API_BASE}/{endpoint_id}/run",
        headers=headers,
        json={"input": {"payload": payload}},
    )
    response.raise_for_status()
    submitted = response.json()
    job_id = str(submitted.get("id") or "")
    if not job_id:
        raise RuntimeError("RunPod submission returned no job ID")
    deadline = time.monotonic() + timeout_seconds
    while True:
        if time.monotonic() >= deadline:
            await client.post(
                f"{RUNPOD_API_BASE}/{endpoint_id}/cancel/{job_id}", headers=headers
            )
            raise TimeoutError(f"RunPod job exceeded {timeout_seconds}s")
        status_response = await client.get(
            f"{RUNPOD_API_BASE}/{endpoint_id}/status/{job_id}", headers=headers
        )
        status_response.raise_for_status()
        body = status_response.json()
        status = str(body.get("status") or "").upper()
        if status == "COMPLETED":
            output = body.get("output")
            if isinstance(output, dict) and isinstance(output.get("output"), dict):
                output = output["output"]
            if not isinstance(output, dict):
                raise RuntimeError("RunPod completed with non-object output")
            receipt = {
                "job_id": job_id,
                "delay_time_ms": body.get("delayTime"),
                "execution_time_ms": body.get("executionTime"),
                "wall_seconds": round(time.monotonic() - started, 6),
            }
            return output, receipt
        if status in TERMINAL_FAILURES:
            raise RuntimeError(f"RunPod job terminated status={status}")
        await asyncio.sleep(2.0)


def validate_canary(
    output: dict[str, Any], tasks: list[dict[str, str]]
) -> dict[str, Any]:
    if output.get("contract_version") != CONTRACT_VERSION:
        raise AssertionError("green response contract version mismatch")
    if len(output.get("results") or []) != len(tasks):
        raise AssertionError("green response cardinality mismatch")
    task_by_child = {row["child_id"]: row for row in tasks}
    controlled = set(load_extraction_registries()["vocab"]["entity_types"])
    temporal_texts: set[str] = set()
    modalities: set[str] = set()
    negated = 0
    predicates = 0
    entities = 0
    for row in output["results"]:
        task = task_by_child.get(str(row.get("child_id") or ""))
        if task is None:
            raise AssertionError("green returned an unknown child ID")
        extraction = LocalExtractionV1.model_validate(row["extraction"])
        if extraction.document_id != task["document_id"]:
            raise AssertionError("green document identity mismatch")
        text = task["text"]
        for entity in extraction.entities:
            entities += 1
            if entity.entity_type not in controlled:
                raise AssertionError("green emitted an out-of-registry entity label")
            if text[entity.start_char : entity.end_char] != entity.text:
                raise AssertionError("green entity span failed source round trip")
        for predicate in extraction.predicates:
            predicates += 1
            modalities.add(predicate.modality)
            negated += int(predicate.negated)
            if (
                text[predicate.start_char : predicate.end_char]
                != predicate.surface_text
            ):
                raise AssertionError("green predicate span failed source round trip")
        if extraction.relations:
            raise AssertionError("green relations must remain empty")
        for capture in row.get("temporal_captures") or []:
            start, end = int(capture["char_start"]), int(capture["char_end"])
            if text[start:end] != capture["text"]:
                raise AssertionError("green temporal span failed source round trip")
            temporal_texts.add(str(capture["text"]))
    required_temporal = {"winter 1911", "2018 drought summer"}
    if not required_temporal <= temporal_texts:
        raise AssertionError("green temporal canary phrases are incomplete")
    if negated < 1 or "recommended" not in modalities or predicates < 1 or entities < 1:
        raise AssertionError(
            "green predicate/negation/modality/entity canary incomplete"
        )
    return {
        "chunks": len(tasks),
        "entities": entities,
        "predicates": predicates,
        "negated_predicates": negated,
        "modalities": sorted(modalities),
        "required_temporal": sorted(required_temporal),
        "relations": 0,
    }


def _normalized_for_exact(value: dict[str, Any]) -> tuple[dict[str, Any], list[float]]:
    normalized = copy.deepcopy(value)
    normalized.get("metrics", {}).pop("duration_seconds", None)
    normalized.get("runtime_identity", {}).pop("platform", None)
    confidences: list[float] = []
    for row in normalized.get("results") or []:
        for entity in row["extraction"]["entities"]:
            confidences.append(float(entity.pop("confidence")))
    return normalized, confidences


def compare_to_reference(
    reference: dict[str, Any], live: dict[str, Any], tolerance: float
) -> dict[str, Any]:
    reference_normalized, reference_confidences = _normalized_for_exact(reference)
    live_normalized, live_confidences = _normalized_for_exact(live)
    if reference_normalized != live_normalized:
        raise AssertionError("green output has an exact-field semantic mismatch")
    if len(reference_confidences) != len(live_confidences):
        raise AssertionError("green confidence cardinality mismatch")
    deltas = [
        abs(left - right)
        for left, right in zip(reference_confidences, live_confidences, strict=True)
    ]
    maximum = max(deltas, default=0.0)
    if maximum > tolerance:
        raise AssertionError(
            f"green confidence delta {maximum} exceeds tolerance {tolerance}"
        )
    return {
        "exact_semantic_mismatches": 0,
        "missing_or_extra_results": 0,
        "confidence_count": len(deltas),
        "confidence_max_abs_delta": maximum,
        "confidence_tolerance": tolerance,
        "threshold_side_selection_match": True,
    }


def invalid_requests(valid: dict[str, Any]) -> dict[str, dict[str, Any]]:
    malformed = copy.deepcopy(valid)
    malformed["contract_version"] = "polymath.invalid"
    injected = copy.deepcopy(valid)
    injected["entity_types"] = ["OUT_OF_REGISTRY_LABEL"]
    bad_source = copy.deepcopy(valid)
    bad_source["tasks"][0]["source_version_id"] = ""
    return {
        "malformed_contract": malformed,
        "out_of_registry_label_injection": injected,
        "bad_source_identity": bad_source,
    }


def validate_refusal(name: str, output: dict[str, Any]) -> dict[str, Any]:
    if output.get("success") is not False:
        raise AssertionError(f"invalid case {name} did not fail closed")
    if output.get("error_code") != "extraction_contract_rejected":
        raise AssertionError(f"invalid case {name} returned wrong refusal code")
    return {
        "case": name,
        "success": False,
        "error_code": output.get("error_code"),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    spec, tasks = load_tasks(args.spec)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    if baseline.get("spec_sha256") != sha256(args.spec):
        raise ValueError("baseline/spec identity mismatch")
    request = build_request(baseline, tasks)
    tolerance = float(spec["comparison"]["confidence_absolute_tolerance"])
    mongo_client, _, runtime_settings_service = _mongo()
    try:
        accounts = await runtime_settings_service.get_system_runpod_flash_accounts()
        primary = [item for item in accounts if item[0].name == "primary"]
        if len(primary) != 1:
            raise RuntimeError("primary RunPod account must resolve exactly once")
        account, api_key = primary[0]
        async with httpx.AsyncClient(timeout=60) as http:
            endpoint_id, endpoint, template = await _discover_green(http, api_key)
            live_output, valid_job = await _submit_and_wait(
                http,
                endpoint_id=endpoint_id,
                api_key=api_key,
                payload=request,
                timeout_seconds=args.timeout_seconds,
            )
            canary = validate_canary(live_output, tasks)
            parity = compare_to_reference(baseline["output"], live_output, tolerance)
            refusals = []
            if args.mode == "canary":
                for name, invalid in invalid_requests(request).items():
                    invalid_output, job = await _submit_and_wait(
                        http,
                        endpoint_id=endpoint_id,
                        api_key=api_key,
                        payload=invalid,
                        timeout_seconds=300,
                    )
                    refusals.append(
                        {**validate_refusal(name, invalid_output), "job": job}
                    )
        result = {
            "schema_version": "polymath.runpod_green_lockdown_receipt.v1",
            "mode": args.mode,
            "account": account.name,
            "endpoint": endpoint,
            "template": {
                "id": template.get("id"),
                "imageName": template.get("imageName"),
                "containerRegistryAuthId_present": bool(
                    template.get("containerRegistryAuthId")
                ),
            },
            "spec_sha256": sha256(args.spec),
            "task_input_sha256": canonical_hash(tasks),
            "canary": canary,
            "parity": parity,
            "valid_job": valid_job,
            "invalid_refusals": refusals,
            "live_output_sha256": canonical_hash(_normalized_for_exact(live_output)[0]),
            "live_output": live_output,
            "run_mode": {
                "provider_calls": 0,
                "database_writes": 0,
                "graph_writes": 0,
                "vector_writes": 0,
            },
            "secret_values_emitted": 0,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            key: value for key, value in result.items() if key not in {"live_output"}
        } | {"out": str(args.out)}
    finally:
        mongo_client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("canary", "retry"), required=True)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(parse_args())), indent=2, sort_keys=True))
