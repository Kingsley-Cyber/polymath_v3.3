#!/usr/bin/env python3
"""Compare production extraction lanes on one evidence/qualifier fixture.

The script is read-only with respect to ingestion artifacts.  It resolves
provider credentials through the encrypted Settings service, never prints or
writes plaintext credentials, and emits only validated predictions plus
aggregate metrics.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import sys
import time
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings  # noqa: E402
from evals.semantic_extraction_scoring import (  # noqa: E402
    score_claim_candidates,
    score_extraction_lane,
)
from services import ghost_b, ghost_b_local, runpod_flash_extraction  # noqa: E402
from services.ghost_b import (  # noqa: E402
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    ExtractionTask,
    SchemaContext,
)
from services.settings import settings_service  # noqa: E402


ALLOWED_CLAIM_TYPES = {
    "definition",
    "description_or_observation",
    "association",
    "causal",
    "comparison_or_contrast",
    "prediction",
    "recommendation_or_procedure",
    "normative",
    "argument_or_inference",
}
ALLOWED_POLARITY = {"affirmed", "negated", "mixed"}
ALLOWED_MODAL = {"asserted", "possible", "probable", "predicted", "recommended", "required"}
ALLOWED_ASSERTION_MODE = {"reported", "attributed", "hypothetical"}
ALLOWED_QUALIFIERS = {
    "modal",
    "negation",
    "condition",
    "exception",
    "attribution",
    "comparison",
    "causal",
    "temporal",
}


def _load_fixture(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("schema_version") != "polymath.semantic_extraction_gold.v1":
        raise ValueError("unsupported semantic extraction fixture")
    return fixture


def _tasks(samples: list[dict[str, Any]]) -> list[ExtractionTask]:
    return [
        ExtractionTask(
            chunk_id=str(sample["id"]),
            doc_id="semantic-extraction-gold-v1",
            corpus_id="semantic-extraction-gold-v1",
            text=str(sample["text"]),
            chunk_kind="body",
        )
        for sample in samples
    ]


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _safe_results(report: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for result in report.results:
        item = _plain(result)
        out.append(
            {
                "id": item.get("chunk_id"),
                "entities": item.get("entities") or [],
                "relations": item.get("relations") or [],
                "facts": item.get("facts") or [],
            }
        )
    return out


def _safe_report_metrics(report: Any, wall_seconds: float) -> dict[str, Any]:
    metrics = _plain(report.metrics or {})
    allowed = {
        "engine",
        "model",
        "schema_version",
        "n_chunks",
        "n_entities",
        "n_relations",
        "n_facts",
        "requested_chunks",
        "extracted_chunks",
        "failed_chunks",
        "duration_seconds",
        "chunks_per_second",
        "request_batches",
        "schema_evidence_pass_rate",
        "validation_drop_count",
        "estimated_compute_cost_usd",
        "estimated_cost_only",
        "remote_entities_emitted",
        "remote_relations_emitted",
        "account_dispatch",
        "concurrency_plan",
        "provider_distribution",
        "output_mode_distribution",
    }
    return {
        "wall_seconds": round(wall_seconds, 4),
        "wall_chunks_per_second": round(len(report.results) / wall_seconds, 4)
        if wall_seconds
        else None,
        "failure_count": len(report.failures),
        "failure_types": sorted({type(item).__name__ + ":" + str(getattr(item, "error_type", "")) for item in report.failures}),
        "reported": {key: value for key, value in metrics.items() if key in allowed},
    }


async def _run_lane(
    lane: str,
    tasks: list[ExtractionTask],
    schema: SchemaContext,
    *,
    runpod_relation_threshold: float | None = None,
) -> tuple[Any, float]:
    started = time.perf_counter()
    if lane == "gliner_glirel":
        report = await ghost_b_local.extract_entities(
            tasks,
            schema=schema,
            return_report=True,
            enable_facts=False,
            endpoint_urls=["http://host.docker.internal:8084"],
        )
    elif lane == "runpod":
        config, _legacy_key = await settings_service.get_system_runpod_flash()
        if runpod_relation_threshold is not None:
            config = config.model_copy(
                update={"relation_threshold": runpod_relation_threshold}
            )
        accounts = await settings_service.get_system_runpod_flash_accounts()
        report = await runpod_flash_extraction.extract_entities(
            tasks,
            schema=schema,
            runpod_config=config,
            accounts=accounts,
            return_report=True,
        )
    elif lane in {"deepseek", "longcat"}:
        key = await settings_service.get_plaintext_key_any_user(lane)
        if not key:
            raise RuntimeError(f"encrypted {lane} credential is not configured")
        if lane == "deepseek":
            entry = {
                "provider_preset": "deepseek",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": key,
                "max_concurrent": 2,
                "extra_params": {"disable_thinking": True},
            }
        else:
            entry = {
                "provider_preset": "longcat",
                "model": "openai/LongCat-2.0",
                "base_url": "https://api.longcat.chat/openai/v1",
                "api_key": key,
                "max_concurrent": 2,
                "extra_params": {"disable_thinking": True},
            }
        report = await ghost_b.extract_entities(
            tasks,
            schema=schema,
            pool=[entry],
            return_report=True,
            enable_facts=False,
        )
    else:
        raise ValueError(f"unknown lane: {lane}")
    return report, time.perf_counter() - started


def _balanced_object(raw: str) -> dict[str, Any] | None:
    candidate = ghost_b._extract_balanced_json_object(raw)  # noqa: SLF001 - benchmark compiler parity
    if candidate is None:
        return None
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _refinement_prompt(samples: list[dict[str, Any]]) -> tuple[str, str]:
    system = """You are a candidate claim refiner. Return one JSON object only.
You may label and structure only propositions explicitly present in each supplied text.
Every evidence and qualifier cue must be copied exactly from its text.
Never call a candidate accepted, asserted truth, proven, or verified.

Output shape:
{"items":[{"id":"input id","claims":[{"evidence":"exact text substring","predicate_lemma":"lowercase lemma","claim_type":"definition|description_or_observation|association|causal|comparison_or_contrast|prediction|recommendation_or_procedure|normative|argument_or_inference","polarity":"affirmed|negated|mixed","modal_force":"asserted|possible|probable|predicted|recommended|required","assertion_mode":"reported|attributed|hypothetical","conditions":["exact substring"],"exceptions":["exact substring"],"qualifiers":[{"kind":"modal|negation|condition|exception|attribution|comparison|causal|temporal","cue":"exact substring"}]}]}]}

Prefer separate claims when predicates have different scope. Preserve negation on the predicate it modifies. Return an empty claims list when no proposition is present."""
    user = json.dumps(
        {
            "items": [
                {"id": str(sample["id"]), "text": str(sample["text"])}
                for sample in samples
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return system, user


def _validate_refinement(
    value: dict[str, Any] | None,
    samples: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    text_by_id = {str(sample["id"]): str(sample["text"]) for sample in samples}
    candidates: dict[str, list[dict[str, Any]]] = {key: [] for key in text_by_id}
    qualifiers: dict[str, list[dict[str, Any]]] = {key: [] for key in text_by_id}
    drops: Counter[str] = Counter()
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return candidates, qualifiers, {"valid_claims": 0, "drops": {"invalid_envelope": 1}}
    seen_ids: set[str] = set()
    for item in value["items"]:
        if not isinstance(item, dict):
            drops["invalid_item"] += 1
            continue
        sample_id = str(item.get("id") or "")
        if sample_id not in text_by_id or sample_id in seen_ids:
            drops["unknown_or_duplicate_id"] += 1
            continue
        seen_ids.add(sample_id)
        text = text_by_id[sample_id]
        for claim in item.get("claims") or []:
            if not isinstance(claim, dict):
                drops["invalid_claim"] += 1
                continue
            evidence = str(claim.get("evidence") or "")
            predicate = str(claim.get("predicate_lemma") or "").strip().lower()
            if not evidence or evidence not in text or not predicate:
                drops["ungrounded_claim"] += 1
                continue
            fields_ok = (
                claim.get("claim_type") in ALLOWED_CLAIM_TYPES
                and claim.get("polarity") in ALLOWED_POLARITY
                and claim.get("modal_force") in ALLOWED_MODAL
                and claim.get("assertion_mode") in ALLOWED_ASSERTION_MODE
            )
            if not fields_ok:
                drops["off_schema_claim"] += 1
                continue
            conditions = [str(value) for value in (claim.get("conditions") or [])]
            exceptions = [str(value) for value in (claim.get("exceptions") or [])]
            if any(value not in text for value in conditions + exceptions):
                drops["ungrounded_scope"] += 1
                continue
            valid_qualifiers: list[dict[str, Any]] = []
            qualifier_failed = False
            for qualifier in claim.get("qualifiers") or []:
                if not isinstance(qualifier, dict):
                    qualifier_failed = True
                    break
                kind = str(qualifier.get("kind") or "")
                cue = str(qualifier.get("cue") or "")
                if kind not in ALLOWED_QUALIFIERS or not cue or cue not in text:
                    qualifier_failed = True
                    break
                valid_qualifiers.append({"kind": kind, "cue": cue})
            if qualifier_failed:
                drops["ungrounded_qualifier"] += 1
                continue
            candidates[sample_id].append(
                {
                    "predicate_lemma": predicate,
                    "claim_type": claim["claim_type"],
                    "polarity": claim["polarity"],
                    "modal_force": claim["modal_force"],
                    "assertion_mode": claim["assertion_mode"],
                    "conditions": conditions,
                    "exceptions": exceptions,
                    "evidence": evidence,
                    "knowledge_status": "candidate",
                    "validation_status": "candidate",
                }
            )
            qualifiers[sample_id].extend(valid_qualifiers)
    return candidates, qualifiers, {
        "valid_claims": sum(len(items) for items in candidates.values()),
        "drops": dict(sorted(drops.items())),
        "returned_sample_ids": len(seen_ids),
    }


async def _run_refinement(
    provider: str,
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    settings = get_settings()
    key = await settings_service.get_plaintext_key_any_user(provider)
    if not key:
        raise RuntimeError(f"encrypted {provider} credential is not configured")
    if provider == "deepseek":
        model = "deepseek/deepseek-v4-flash"
        api_base = "https://api.deepseek.com/v1"
    elif provider == "longcat":
        model = "openai/LongCat-2.0"
        api_base = "https://api.longcat.chat/openai/v1"
    else:
        raise ValueError(provider)
    system, user = _refinement_prompt(samples)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": 6000,
        "thinking": {"type": "disabled"},
        "api_key": key,
        "api_base": api_base,
    }
    if provider == "deepseek":
        payload["response_format"] = {"type": "json_object"}
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20)) as client:
        response = await client.post(
            settings.LITELLM_URL.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    wall = time.perf_counter() - started
    response.raise_for_status()
    body = response.json()
    content = str(
        ((((body.get("choices") or [{}])[0].get("message") or {}).get("content")) or "")
    )
    parsed = _balanced_object(content)
    candidates, qualifiers, validation = _validate_refinement(parsed, samples)
    usage = body.get("usage") or {}
    return {
        "provider": provider,
        "model": model,
        "wall_seconds": round(wall, 4),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
        "validation": validation,
        "score": score_claim_candidates(samples, candidates, qualifiers),
        "candidates_by_sample": candidates,
        "qualifiers_by_sample": qualifiers,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    fixture = _load_fixture(args.fixture)
    samples = list(fixture["samples"])
    task_rows = _tasks(samples)
    schema = SchemaContext(
        entity_schema=list(UNIVERSAL_ENTITY_SCHEMA),
        relation_schema=list(UNIVERSAL_RELATION_SCHEMA),
        strict="soft",
    )
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:
            db = client[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        lanes: dict[str, Any] = {}
        for lane in args.lanes:
            report, wall = await _run_lane(
                lane,
                task_rows,
                schema,
                runpod_relation_threshold=args.runpod_relation_threshold,
            )
            safe_results = _safe_results(report)
            lanes[lane] = {
                "metrics": _safe_report_metrics(report, wall),
                "score": score_extraction_lane(samples, safe_results),
                "results": safe_results,
            }
        refinements: dict[str, Any] = {}
        for provider in args.refiners:
            refinements[provider] = await _run_refinement(provider, samples)
        return {
            "schema_version": "polymath.semantic_extraction_remote_benchmark.v1",
            "fixture_schema": fixture["schema_version"],
            "sample_count": len(samples),
            "runpod_relation_threshold_override": args.runpod_relation_threshold,
            "lanes": lanes,
            "claim_refinement": refinements,
            "security": {
                "credentials_from_encrypted_settings": True,
                "plaintext_credentials_in_artifact": False,
                "raw_provider_output_in_artifact": False,
            },
        }
    finally:
        client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=ROOT / "evals" / "semantic_extraction_gold_v1.json",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--runpod-relation-threshold",
        type=float,
        default=None,
        help="Optional fixture-only threshold override; Settings are not mutated.",
    )
    parser.add_argument(
        "--lanes",
        default="gliner_glirel,runpod,deepseek,longcat",
        help="Comma-separated: gliner_glirel,runpod,deepseek,longcat",
    )
    parser.add_argument(
        "--refiners",
        default="deepseek,longcat",
        help="Comma-separated: deepseek,longcat; empty disables",
    )
    args = parser.parse_args()
    args.lanes = [item.strip() for item in args.lanes.split(",") if item.strip()]
    args.refiners = [
        item.strip() for item in args.refiners.split(",") if item.strip()
    ]
    return args


def main() -> None:
    args = parse_args()
    report = asyncio.run(run(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "out": str(args.out),
                "lanes": {
                    lane: {
                        "entity_f1": value["score"]["entities"]["f1"],
                        "relation_f1": value["score"]["relations"]["f1"],
                        "failures": value["metrics"]["failure_count"],
                    }
                    for lane, value in report["lanes"].items()
                },
                "refiners": {
                    name: {
                        "claim_match_rate": value["score"]["claim_match_rate"],
                        "field_accuracy": value["score"]["claim_field_accuracy_overall"],
                        "valid_claims": value["validation"]["valid_claims"],
                    }
                    for name, value in report["claim_refinement"].items()
                },
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
