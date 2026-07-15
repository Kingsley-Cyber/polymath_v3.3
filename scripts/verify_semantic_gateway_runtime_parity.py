#!/usr/bin/env python3
"""Verify the paid semantic-gateway runtime closure in canonical containers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
DEFAULT_CONTAINERS = (
    "polymath_v33-backend-1",
    "polymath_v33-ingest-worker-1",
)

# This closure is intentionally explicit. It contains the original 36-file
# semantic paid-pass closure plus the wrapper's transport-adjacent imports and
# structured-output capability registries. A missing runtime file is a failure.
SEMANTIC_GATEWAY_RUNTIME_CLOSURE = (
    "scripts/semantic_gateway_mark_prose_phase2.py",
    "scripts/semantic_gateway_mark_paid_pass.py",
    "scripts/semantic_gateway_ugo_canary.py",
    "scripts/materialize_semantic_digest_claim_inputs.py",
    "services/semantic_gateway.py",
    "services/llm.py",
    "services/provider_payload.py",
    "services/thinking_mapper.py",
    "services/streaming_normalizer.py",
    "services/settings.py",
    "services/ingestion/paid_cost_reservation.py",
    "services/ingestion/semantic_parent_eligibility.py",
    "services/ingestion/semantic_digest_claim_inputs.py",
    "services/ingestion/claim_compiler.py",
    "services/ingestion/semantic_observations.py",
    "services/ingestion/job_leases.py",
    "models/hash_taxonomy.py",
    "models/semantic_digest.py",
    "models/semantic_validator.py",
    "models/structured_output_capabilities.py",
    "models/schemas.py",
    "models/semantic_digest_claim_input.py",
    "models/semantic_parent_packet.py",
    "models/semantic_parent_eligibility.py",
    "models/claim_record.py",
    "models/artifact_envelope.py",
    "models/semantic_artifacts.py",
    "models/local_extraction.py",
    "models/registry_loader.py",
    "db/queue_integrity.py",
    "config.py",
    "registries/domain_registry.v1.json",
    "registries/superframe_registry.v1.json",
    "registries/domain_superframe_affinity.v1.json",
    "registries/motif_registry.v1.json",
    "registries/extraction_vocabularies.v1.json",
    "registries/latent_concept_policy.v1.json",
    "registries/motif_stage_superframe_binding.v1.json",
    "registries/embedding_instruction_registry.v1.json",
    "registries/predicate_normalization.v1.json",
    "registries/domain_resolution_policy.v1.json",
    "registries/superframe_rule_registry.v1.json",
    "registries/frame_role_binding_policy.v1.json",
    "registries/motif_matching_policy.v1.json",
    "registries/semantic_parent_eligibility.v2.json",
    "registries/semantic_gateway_provider_prices.v1.json",
    "registries/semantic_gateway_route_parameters.v1.json",
    "registries/structured_output_capabilities.v1.json",
    "registries/structured_output_probe_routes.v1.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _container_sha256(container: str, relative_path: str) -> str | None:
    result = subprocess.run(
        ["docker", "exec", container, "sha256sum", f"/app/{relative_path}"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    return parts[0] if parts else None


def verify(containers: tuple[str, ...]) -> dict[str, Any]:
    if len(set(SEMANTIC_GATEWAY_RUNTIME_CLOSURE)) != len(
        SEMANTIC_GATEWAY_RUNTIME_CLOSURE
    ):
        raise RuntimeError("semantic gateway runtime closure contains duplicates")
    required = {
        "services/llm.py",
        "services/semantic_gateway.py",
        "scripts/semantic_gateway_mark_paid_pass.py",
        "scripts/semantic_gateway_mark_prose_phase2.py",
    }
    if not required <= set(SEMANTIC_GATEWAY_RUNTIME_CLOSURE):
        raise RuntimeError("semantic gateway runtime closure lost a critical file")

    host_hashes: dict[str, str | None] = {}
    mismatches: list[dict[str, str | None]] = []
    for relative_path in SEMANTIC_GATEWAY_RUNTIME_CLOSURE:
        host_path = BACKEND_ROOT / relative_path
        host_hashes[relative_path] = _sha256(host_path) if host_path.is_file() else None
    for container in containers:
        for relative_path, host_sha256 in host_hashes.items():
            runtime_sha256 = _container_sha256(container, relative_path)
            if host_sha256 != runtime_sha256:
                mismatches.append(
                    {
                        "container": container,
                        "path": relative_path,
                        "host_sha256": host_sha256,
                        "runtime_sha256": runtime_sha256,
                    }
                )
    return {
        "schema_version": "polymath.semantic_gateway_runtime_parity.v1",
        "closure_count": len(SEMANTIC_GATEWAY_RUNTIME_CLOSURE),
        "containers": list(containers),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "all_green": not mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", action="append", dest="containers")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    containers = tuple(args.containers or DEFAULT_CONTAINERS)
    report = verify(containers)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["all_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
