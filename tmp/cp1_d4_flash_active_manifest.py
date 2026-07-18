#!/usr/bin/env python3
"""Print safe active-build identity for the RunPod Flash extraction app."""

from __future__ import annotations

import asyncio
import json

from runpod_flash.core.resources.app import FlashApp


async def main() -> None:
    app = await FlashApp.from_name("runpod_flash_extractor")
    environment = await app.get_environment_by_name("production")
    build_id = str(
        environment.get("activeBuildId")
        or environment.get("activeBuildid")
        or environment.get("buildId")
        or ""
    )
    if not build_id:
        raise RuntimeError(f"active build absent; keys={sorted(environment)}")
    manifest = await app.get_build_manifest(build_id)
    resource = (manifest.get("resources") or {}).get("polymath-gliner-relex") or {}
    safe_env = {
        key: value
        for key, value in (resource.get("env") or {}).items()
        if key == "_FLASH_SOURCE_FINGERPRINT"
    }
    print(
        json.dumps(
            {
                "active_build_id": build_id,
                "source_fingerprint": manifest.get("source_fingerprint"),
                "resource_source_env": safe_env,
                "resource_endpoint_id": resource.get("endpoint_id"),
                "workersMax": resource.get("workersMax"),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
