#!/usr/bin/env python3
"""Run the owner-authorized fresh-baseline claim-anchor verification window.

This harness leaves the historical v1/v2 replay scripts unchanged.  The OFF
capture delegates retrieval and synthesis to the existing micro A/B harness,
then adds a runtime attestation proving that temporal routing was already ON
and claim anchors were OFF.  The ON arm is provider-free: it attaches anchors
to the exact selected evidence sealed by that fresh OFF artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from config import get_settings
from models.schemas import SourceChunk
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from scripts.run_claim_anchor_additivity_replay import (
    SEALED_V1_OFF_SHA256,
    V2_SCHEMA,
    V2_SPEC,
    _anchor_rows,
    _prompt_anchor_count,
    _source_keys,
)
from scripts.run_claim_anchor_micro_ab import (
    _atomic_write,
    _fingerprint,
    _load_contract,
    _source_fingerprint,
    _validate_anchor,
)
from services.context_manager import context_manager
from services.retriever.atomic_claim_anchors import (
    attach_atomic_claim_anchors,
    source_additivity_receipt,
)
from services.retriever.claim_anchor_rendering import render_claim_proposition


OWNER_WINDOW_SCHEMA = "claim_anchor_owner_window.v1"
OWNER_OFF_ATTESTATION_SCHEMA = "claim_anchor_owner_window_off_attestation.v1"
V2_SPEC_SHA256 = "42eb718dfee0ffd47e1310d1605f02514308bfc9941e8115644ab7434be91783"
MICRO_HARNESS = Path(__file__).resolve().with_name("run_claim_anchor_micro_ab.py")
EVAL_LOCK_PATH = Path("/tmp/polymath-eval.lock")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UNREADABLE_MACHINE_TOKEN_RE = re.compile(
    r"\b(?:POSITIVE|NEGATIVE|ASSERTED|POSSIBLE|PROBABLE|NECESSARY|"
    r"RECOMMENDED|HYPOTHETICAL|UNTYPED)\b|UNTYPED\["
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_eval_lock(owner: str) -> None:
    expected_owner = owner.strip()
    if not expected_owner:
        raise RuntimeError("eval lock owner must be non-empty")
    try:
        observed_owner = EVAL_LOCK_PATH.read_text(
            encoding="utf-8",
            errors="replace",
        ).strip()
    except FileNotFoundError as exc:
        raise RuntimeError("claim owner window requires the eval lock") from exc
    if observed_owner != expected_owner:
        raise RuntimeError(
            f"eval lock owner mismatch: {observed_owner or 'unknown'} "
            f"!= {expected_owner}"
        )


def _runtime_snapshot(settings: Any) -> dict[str, bool]:
    return {
        "claim_anchors_enabled": bool(settings.ATOMIC_CLAIM_ANCHORS_ENABLED),
        "temporal_query_routing_enabled": bool(settings.TEMPORAL_QUERY_ROUTING_ENABLED),
        "relationship_evidence_allocation_enabled": bool(
            settings.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED
        ),
        "answerability_corpus_scope_v2_enabled": bool(
            settings.ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED
        ),
    }


def _require_runtime(
    runtime: dict[str, bool],
    *,
    claim_anchors_enabled: bool,
) -> None:
    expected = {
        "claim_anchors_enabled": claim_anchors_enabled,
        "temporal_query_routing_enabled": True,
        "relationship_evidence_allocation_enabled": True,
        "answerability_corpus_scope_v2_enabled": True,
    }
    if runtime != expected:
        raise RuntimeError(
            "claim owner-window runtime mismatch: "
            f"observed={json.dumps(runtime, sort_keys=True)} "
            f"expected={json.dumps(expected, sort_keys=True)}"
        )


def _load_v2_contract(spec_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    actual_sha = _sha256_file(spec_path)
    if actual_sha != V2_SPEC_SHA256:
        raise RuntimeError(
            f"claim-anchor v2 spec hash drifted: {actual_sha} != {V2_SPEC_SHA256}"
        )
    spec, questions = _load_contract(spec_path)
    if spec.get("schema_version") != V2_SCHEMA:
        raise RuntimeError("owner-window harness requires the frozen v2 spec")
    return spec, questions


def _validate_off_payload_base(
    *,
    off: dict[str, Any],
    spec: dict[str, Any],
) -> None:
    failures: list[str] = []
    if off.get("arm") != "off":
        failures.append("arm_not_off")
    if off.get("runtime_flag_enabled") is not False:
        failures.append("off_runtime_claim_flag_not_false")
    if off.get("passed") is not True:
        failures.append("off_arm_not_green")
    if off.get("spec") != spec:
        failures.append("off_spec_drift")
    if off.get("model_contract") != spec.get("model_contract"):
        failures.append("off_model_contract_drift")
    if not str(off.get("corpus_id") or ""):
        failures.append("off_corpus_id_absent")
    if off.get("corpus_fingerprint_equal") is not True:
        failures.append("off_corpus_fingerprint_not_equal")
    if off.get("corpus_fingerprint_before") != off.get("corpus_fingerprint_after"):
        failures.append("off_corpus_fingerprint_drift")
    compilation_fingerprint = (
        (off.get("corpus_fingerprint_after") or {})
        .get("collections", {})
        .get("semantic_digest_claim_compilations", {})
        .get("sha256")
    )
    if not _SHA256_RE.fullmatch(str(compilation_fingerprint or "")):
        failures.append("off_raw_claim_store_fingerprint_absent")

    rows = list(off.get("results") or [])
    if [row.get("query_id") for row in rows] != spec.get("query_ids"):
        failures.append("off_query_order_drift")
    for row in rows:
        query_id = str(row.get("query_id") or "unknown")
        if row.get("errors") or row.get("done_received") is not True:
            failures.append(f"{query_id}:technical")
        if row.get("model_used") != spec.get("model_contract"):
            failures.append(f"{query_id}:model_contract")
        if int(row.get("anchor_count") or 0) != 0:
            failures.append(f"{query_id}:off_anchor_exposure")
        if int(row.get("prompt_render_count") or 0) != 0:
            failures.append(f"{query_id}:off_render_exposure")
        sources = list(row.get("selected_sources") or [])
        if not sources:
            failures.append(f"{query_id}:selected_sources_absent")
            continue
        if _source_keys(sources) != row.get("source_keys"):
            failures.append(f"{query_id}:source_keys_drift")
        if _source_fingerprint(sources) != row.get(
            "selected_evidence_sha256_without_anchors"
        ):
            failures.append(f"{query_id}:selected_evidence_hash_drift")
    if failures:
        raise RuntimeError("fresh OFF artifact invalid: " + ",".join(failures))


def _attest_off_payload(
    *,
    off: dict[str, Any],
    spec: dict[str, Any],
    runtime: dict[str, bool],
) -> dict[str, Any]:
    _require_runtime(runtime, claim_anchors_enabled=False)
    _validate_off_payload_base(off=off, spec=spec)
    attested = copy.deepcopy(off)
    attested["owner_window_attestation"] = {
        "schema_version": OWNER_OFF_ATTESTATION_SCHEMA,
        "attested_at_utc": datetime.now(timezone.utc).isoformat(),
        "spec_sha256": V2_SPEC_SHA256,
        "source_harness_sha256": _sha256_file(MICRO_HARNESS),
        "capture_runtime": runtime,
    }
    return attested


def _validate_fresh_off_artifact(
    *,
    off_path: Path,
    expected_sha256: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    normalized_sha = expected_sha256.strip().lower()
    if not _SHA256_RE.fullmatch(normalized_sha):
        raise RuntimeError("--off-artifact-sha256 must be 64 lowercase hex characters")
    actual_sha = _sha256_file(off_path)
    if actual_sha != normalized_sha:
        raise RuntimeError(
            f"fresh OFF artifact SHA drifted: {actual_sha} != {normalized_sha}"
        )
    if actual_sha == SEALED_V1_OFF_SHA256:
        raise RuntimeError("stale pinned v1 OFF packet is forbidden in owner window")

    off = json.loads(off_path.read_text(encoding="utf-8"))
    _validate_off_payload_base(off=off, spec=spec)
    attestation = off.get("owner_window_attestation")
    if not isinstance(attestation, dict):
        raise RuntimeError("fresh OFF artifact lacks owner-window attestation")
    if attestation.get("schema_version") != OWNER_OFF_ATTESTATION_SCHEMA:
        raise RuntimeError("fresh OFF artifact attestation schema drifted")
    if attestation.get("spec_sha256") != V2_SPEC_SHA256:
        raise RuntimeError("fresh OFF artifact v2 spec hash drifted")
    if attestation.get("source_harness_sha256") != _sha256_file(MICRO_HARNESS):
        raise RuntimeError("fresh OFF capture harness hash drifted")
    capture_runtime = attestation.get("capture_runtime")
    if not isinstance(capture_runtime, dict):
        raise RuntimeError("fresh OFF artifact capture runtime is absent")
    _require_runtime(capture_runtime, claim_anchors_enabled=False)
    return off


def _render_is_readable(value: str) -> bool:
    compact = " ".join(str(value or "").split())
    return bool(compact) and not _UNREADABLE_MACHINE_TOKEN_RE.search(compact)


def _prompt_claim_block_is_readable(prompt: str) -> bool:
    start = prompt.find("<atomic_claim_anchors>")
    end = prompt.find("</atomic_claim_anchors>", start + 1)
    if start < 0 or end < 0:
        return False
    return not _UNREADABLE_MACHINE_TOKEN_RE.search(prompt[start:end])


async def _replay_owner_window(
    *,
    settings: Any,
    sync_db: Any,
    questions: list[dict[str, Any]],
    off_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    motor = AsyncIOMotorClient(settings.MONGODB_URI)
    async_db = motor[settings.MONGODB_DATABASE]
    results: list[dict[str, Any]] = []
    try:
        for question, off_row in zip(questions, off_rows, strict=True):
            started = time.perf_counter()
            off_sources = copy.deepcopy(off_row.get("selected_sources") or [])
            selected = [SourceChunk.model_validate(source) for source in off_sources]
            before = source_additivity_receipt(selected)
            enriched, diagnostics = await attach_atomic_claim_anchors(
                async_db,
                selected,
                query=question["question"],
                per_source=settings.ATOMIC_CLAIM_ANCHORS_PER_SOURCE,
                total=settings.ATOMIC_CLAIM_ANCHORS_TOTAL,
            )
            after = source_additivity_receipt(enriched)
            enriched_payload = [source.model_dump(mode="json") for source in enriched]
            anchors = _anchor_rows(enriched_payload)
            checks = [
                _validate_anchor(sync_db, source=source, anchor=anchor)
                for source, anchor in anchors
            ]
            raw_claims_before_render = [
                str(anchor.get("claim_text") or "") for _, anchor in anchors
            ]
            rendered_claims = [
                render_claim_proposition(
                    raw,
                    exact_sentence=str(anchor.get("exact_sentence") or ""),
                )
                for raw, (_, anchor) in zip(
                    raw_claims_before_render,
                    anchors,
                    strict=True,
                )
            ]
            prompt = context_manager.build_augmented_prompt(
                question["question"],
                enriched,
            )
            after_render_payload = [
                source.model_dump(mode="json") for source in enriched
            ]
            raw_claims_after_render = [
                str(anchor.get("claim_text") or "")
                for _, anchor in _anchor_rows(after_render_payload)
            ]
            source_keys = _source_keys(enriched_payload)
            valid_count = sum(int(check["valid"]) for check in checks)
            readable_count = sum(
                int(_render_is_readable(rendered)) for rendered in rendered_claims
            )
            encoded_rendered = json.dumps(
                rendered_claims,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            source_ids_equal = source_keys == off_row["source_keys"]
            evidence_bytes_equal = (
                before["non_anchor_evidence_sha256"]
                == after["non_anchor_evidence_sha256"]
                and before["non_anchor_evidence_bytes"]
                == after["non_anchor_evidence_bytes"]
                and _source_fingerprint(enriched_payload)
                == off_row["selected_evidence_sha256_without_anchors"]
            )
            result = {
                "query_id": question["id"],
                "shape": question["shape"],
                "source_keys": source_keys,
                "source_count": len(source_keys),
                "off_source_ids_sha256": before["source_ids_sha256"],
                "on_source_ids_sha256": after["source_ids_sha256"],
                "off_non_anchor_evidence_sha256": before["non_anchor_evidence_sha256"],
                "on_non_anchor_evidence_sha256": after["non_anchor_evidence_sha256"],
                "source_ids_equal": source_ids_equal,
                "non_anchor_evidence_bytes_equal": evidence_bytes_equal,
                "service_additivity_verified": diagnostics.get("additivity_verified"),
                "anchor_count": len(anchors),
                "valid_anchor_count": valid_count,
                "all_citations_valid": (
                    all(check["valid"] for check in checks) if checks else None
                ),
                "prompt_render_count": _prompt_anchor_count(prompt),
                "readable_claim_count": readable_count,
                "prompt_claim_block_readable": _prompt_claim_block_is_readable(prompt),
                "rendered_claims_sha256": hashlib.sha256(encoded_rendered).hexdigest(),
                "render_cleaned_claim_count": sum(
                    int(rendered != raw)
                    for raw, rendered in zip(
                        raw_claims_before_render,
                        rendered_claims,
                        strict=True,
                    )
                ),
                "raw_claim_text_preserved": (
                    raw_claims_before_render == raw_claims_after_render
                ),
                "diagnostics": diagnostics,
                "model_contract": off_row.get("model_used"),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
            results.append(result)
            print(
                json.dumps(
                    {
                        "query_id": result["query_id"],
                        "sources_equal": source_ids_equal,
                        "evidence_equal": evidence_bytes_equal,
                        "anchors": len(anchors),
                        "valid": valid_count,
                        "rendered": result["prompt_render_count"],
                        "readable": readable_count,
                        "raw_preserved": result["raw_claim_text_preserved"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    finally:
        motor.close()
    return results


def _replay_failures(
    *,
    spec: dict[str, Any],
    off: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    results: list[dict[str, Any]],
) -> list[str]:
    failures: list[str] = []
    for row in results:
        query_id = str(row["query_id"])
        if not row["source_ids_equal"]:
            failures.append(f"{query_id}:source_identity")
        if not row["non_anchor_evidence_bytes_equal"]:
            failures.append(f"{query_id}:non_anchor_evidence")
        if row["service_additivity_verified"] is not True:
            failures.append(f"{query_id}:service_additivity")
        if row["anchor_count"] and not row["all_citations_valid"]:
            failures.append(f"{query_id}:citation_invalid")
        if row["prompt_render_count"] != row["anchor_count"]:
            failures.append(f"{query_id}:not_all_anchors_rendered")
        if row["readable_claim_count"] != row["anchor_count"]:
            failures.append(f"{query_id}:not_all_claims_readable")
        if row["anchor_count"] and not row["prompt_claim_block_readable"]:
            failures.append(f"{query_id}:prompt_claim_block_unreadable")
        if not row["raw_claim_text_preserved"]:
            failures.append(f"{query_id}:raw_claim_mutated")
        if row["model_contract"] != spec["model_contract"]:
            failures.append(f"{query_id}:model_contract")

    total_anchors = sum(int(row["anchor_count"]) for row in results)
    total_valid = sum(int(row["valid_anchor_count"]) for row in results)
    if total_anchors < int(spec["minimum_structural_anchor_count_when_on"]):
        failures.append("structural_anchor_count_below_minimum")
    if total_valid != total_anchors:
        failures.append("not_all_emitted_anchors_valid")
    q021 = next((row for row in results if row["query_id"] == "q021"), None)
    if q021 is None or int(q021["prompt_render_count"]) < int(
        spec["q021_min_rendered_anchors_when_on"]
    ):
        failures.append("q021:rendered_anchor_floor")

    sealed_fingerprint = off.get("corpus_fingerprint_after")
    if before != sealed_fingerprint:
        failures.append("fresh_off_corpus_fingerprint_drifted_before_replay")
    if before != after:
        failures.append("corpus_fingerprint_changed_during_replay")
    raw_claim_store_before = (
        before.get("collections", {})
        .get("semantic_digest_claim_compilations", {})
        .get("sha256")
    )
    raw_claim_store_after = (
        after.get("collections", {})
        .get("semantic_digest_claim_compilations", {})
        .get("sha256")
    )
    if (
        not _SHA256_RE.fullmatch(str(raw_claim_store_before or ""))
        or raw_claim_store_before != raw_claim_store_after
    ):
        failures.append("raw_claim_store_changed")
    return failures


def _capture_off(args: argparse.Namespace) -> int:
    _require_eval_lock(args.lock_owner)
    spec, _ = _load_v2_contract(args.spec)
    settings = get_settings()
    runtime = _runtime_snapshot(settings)
    _require_runtime(runtime, claim_anchors_enabled=False)
    command = [
        sys.executable,
        str(MICRO_HARNESS),
        "--spec",
        str(args.spec),
        "--expected-flag",
        "off",
        "--output",
        str(args.output),
        "--base",
        args.base,
    ]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        return int(completed.returncode)
    off = json.loads(args.output.read_text(encoding="utf-8"))
    attested = _attest_off_payload(off=off, spec=spec, runtime=runtime)
    _atomic_write(args.output, attested)
    print(
        "FRESH_OFF_ARTIFACT="
        + json.dumps(
            {
                "path": str(args.output),
                "sha256": _sha256_file(args.output),
                "runtime": runtime,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def _replay_on(args: argparse.Namespace) -> int:
    _require_eval_lock(args.lock_owner)
    spec, questions = _load_v2_contract(args.spec)
    settings = get_settings()
    runtime = _runtime_snapshot(settings)
    _require_runtime(runtime, claim_anchors_enabled=True)
    off = _validate_fresh_off_artifact(
        off_path=args.off_artifact,
        expected_sha256=args.off_artifact_sha256,
        spec=spec,
    )
    off_rows = list(off["results"])
    normalized_off_sha256 = _sha256_file(args.off_artifact)

    mongo = MongoClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    try:
        before = _fingerprint(db, str(off["corpus_id"]))
        results = asyncio.run(
            _replay_owner_window(
                settings=settings,
                sync_db=db,
                questions=questions,
                off_rows=off_rows,
            )
        )
        after = _fingerprint(db, str(off["corpus_id"]))
        failures = _replay_failures(
            spec=spec,
            off=off,
            before=before,
            after=after,
            results=results,
        )
        total_anchors = sum(int(row["anchor_count"]) for row in results)
        total_valid = sum(int(row["valid_anchor_count"]) for row in results)
        total_rendered = sum(int(row["prompt_render_count"]) for row in results)
        total_readable = sum(int(row["readable_claim_count"]) for row in results)
        compilation_before = (
            before.get("collections", {})
            .get("semantic_digest_claim_compilations", {})
            .get("sha256")
        )
        compilation_after = (
            after.get("collections", {})
            .get("semantic_digest_claim_compilations", {})
            .get("sha256")
        )
        output = {
            "schema_version": OWNER_WINDOW_SCHEMA,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "arm": "on_post_fresh_final_selection_replay",
            "runtime": runtime,
            "spec": spec,
            "spec_sha256": V2_SPEC_SHA256,
            "corpus_id": off["corpus_id"],
            "off_artifact": str(args.off_artifact),
            "off_artifact_sha256": normalized_off_sha256,
            "model_contract": spec["model_contract"],
            "provider_calls": 0,
            "corpus_fingerprint_before": before,
            "corpus_fingerprint_after": after,
            "fresh_off_corpus_fingerprint_equal": (
                before == off.get("corpus_fingerprint_after")
            ),
            "corpus_fingerprint_equal": before == after,
            "raw_claim_store_sha256_before": compilation_before,
            "raw_claim_store_sha256_after": compilation_after,
            "raw_claim_store_byte_unchanged": (
                bool(compilation_before) and compilation_before == compilation_after
            ),
            "total_anchor_count": total_anchors,
            "total_valid_anchor_count": total_valid,
            "total_rendered_anchor_count": total_rendered,
            "total_readable_claim_count": total_readable,
            "structural_citation_precision": (
                total_valid / total_anchors if total_anchors else None
            ),
            "results": results,
            "failures": failures,
            "passed": not failures,
        }
        _atomic_write(args.output, output)
        print(
            json.dumps(
                {
                    "passed": not failures,
                    "failures": failures,
                    "anchors": total_anchors,
                    "valid": total_valid,
                    "rendered": total_rendered,
                    "readable": total_readable,
                    "corpus_fingerprint_equal": before == after,
                    "raw_claim_store_byte_unchanged": (
                        bool(compilation_before)
                        and compilation_before == compilation_after
                    ),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0 if not failures else 1
    finally:
        mongo.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser(
        "capture-off",
        help="capture and runtime-attest a fresh temporal-ON, claims-OFF packet",
    )
    capture.add_argument("--spec", type=Path, default=V2_SPEC)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--base", default="http://127.0.0.1:8000")
    capture.add_argument("--lock-owner", required=True)
    capture.set_defaults(handler=_capture_off)

    replay = subparsers.add_parser(
        "replay-on",
        help="provider-free replay over an explicitly SHA-bound fresh OFF packet",
    )
    replay.add_argument("--spec", type=Path, default=V2_SPEC)
    replay.add_argument("--off-artifact", type=Path, required=True)
    replay.add_argument("--off-artifact-sha256", required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--lock-owner", required=True)
    replay.set_defaults(handler=_replay_on)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
