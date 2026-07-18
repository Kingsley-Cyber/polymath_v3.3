#!/usr/bin/env python3
"""Inject one verified endpoint identity and run Flash's standard deploy."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from runpod_flash.cli.utils.deployment import deploy_from_uploaded_build
from runpod_flash.core.resources.app import FlashApp


APP_NAME = "runpod_flash_extractor"
ENV_NAME = "production"
RESOURCE_NAME = "polymath-gliner-relex"


async def deploy(project: Path, artifact: Path, endpoint_id: str) -> None:
    manifest_path = project / ".flash" / "flash_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resources = manifest.get("resources") or {}
    if set(resources) != {RESOURCE_NAME}:
        raise RuntimeError(f"unexpected generated resources: {sorted(resources)}")

    endpoint_url = f"https://api.runpod.ai/v2/{endpoint_id}"
    resources[RESOURCE_NAME]["endpoint_id"] = endpoint_id
    manifest["resources_endpoints"] = {RESOURCE_NAME: endpoint_url}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"INJECTED_ENDPOINT_ID={endpoint_id}", flush=True)
    print(f"INJECTED_ENDPOINT_URL={endpoint_url}", flush=True)
    print(f"SOURCE_FINGERPRINT={manifest.get('source_fingerprint')}", flush=True)

    app = await FlashApp.from_name(APP_NAME)
    build = await app.upload_build(artifact)
    build_id = str(build.get("id") or "")
    if not build_id:
        raise RuntimeError("Flash upload returned no build id")
    print(f"UPLOADED_BUILD_ID={build_id}", flush=True)
    result = await deploy_from_uploaded_build(
        app,
        build_id,
        ENV_NAME,
        manifest,
    )
    deployed_manifest = result.get("local_manifest") or {}
    deployed_id = str(
        ((deployed_manifest.get("resources") or {}).get(RESOURCE_NAME) or {}).get(
            "endpoint_id"
        )
        or ""
    )
    deployed_url = str(
        (result.get("resources_endpoints") or {}).get(RESOURCE_NAME) or ""
    )
    print(f"DEPLOYED_ENDPOINT_ID={deployed_id}", flush=True)
    print(f"DEPLOYED_ENDPOINT_URL={deployed_url}", flush=True)
    if deployed_id != endpoint_id or deployed_url != endpoint_url:
        raise RuntimeError(
            f"deployed endpoint mismatch: expected {endpoint_id}/{endpoint_url}, "
            f"got {deployed_id}/{deployed_url}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--endpoint-id", required=True)
    args = parser.parse_args()
    asyncio.run(deploy(args.project, args.artifact, args.endpoint_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
