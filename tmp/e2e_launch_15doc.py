"""Create and start the frozen 15-document E2E through the public API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from models.schemas import CorpusCreate, IngestionConfig
from services.auth import auth_service
from services.ingestion.batches import discover_local_files
from services.ingestion.summary_provider_pool import resolve_summary_provider_pool
from services.settings import settings_service


ROOT = "/ingest-source/runpod_e2e_15doc_20260715"
STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
SELECTION_SHA = "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
RETRIEVAL_SHA = "8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110"
PRIMARY_ENDPOINT = "hk81nfl5cnwufx"
SECONDARY_ENDPOINT = "8tafde7potcsjw"


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _api(token: str, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://localhost:8000{path}",
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"API status {response.status} for {path}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"API status {exc.code} for {path}: {detail}") from exc


async def main() -> None:
    if STATE.exists():
        raise RuntimeError(f"refusing duplicate E2E launch; state exists: {STATE}")
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        settings_service.attach(database)
        users = await database["users"].find(
            {}, {"_id": 1, "username": 1}
        ).to_list(length=2)
        if len(users) != 1:
            raise RuntimeError("exactly one API owner must be discoverable")
        owner = users[0]
        owner_id = str(owner.get("_id") or "")
        if not ObjectId.is_valid(owner_id) or not owner.get("username"):
            raise RuntimeError("API owner identity is invalid")
        _, files = discover_local_files(
            ROOT,
            recursive=False,
            extensions=[".md"],
            max_files=15,
        )
        if len(files) != 15 or any(path.name.startswith("._") for path in files):
            raise RuntimeError("production discovery did not resolve exactly 15 real files")
        existing = await database["corpora"].find_one(
            {"name": "runpod_e2e_15doc_20260715"}, {"_id": 1}
        )
        if existing:
            raise RuntimeError("fresh E2E corpus name already exists")
        runtime = await settings_service.get_runtime_ingestion_settings(owner_id)
        max_summary_tokens = int(runtime.summary.max_summary_tokens)
        configured_summary = [
            {
                "provider_preset": "deepseek",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com",
                "max_concurrent": int(runtime.summary.max_concurrent or 1),
                "extra_params": {"disable_thinking": True},
            }
        ]
        pool, resolution = await resolve_summary_provider_pool(
            configured_refs=configured_summary,
            runtime_refs=runtime.summary.summary_models,
            user_id=owner_id,
            db=database,
        )
        if (
            not resolution.get("flash_primary")
            or not resolution.get("flash_key_available")
            or len(pool) != 1
        ):
            raise RuntimeError("certified DeepSeek Flash summary route is unavailable")
        config = IngestionConfig(
            preset="deep",
            embedding_model="Qwen/Qwen3-Embedding-0.6B",
            embedding_dimension=1024,
            embedding_model_id="qwen3-embedding-0.6b-v1",
            embed_mode="local",
            extraction_engine="runpod_flash",
            runpod_wire_contract="local_extraction_v1",
            runpod_local_extraction_routes=[
                {"account_name": "primary", "endpoint_id": PRIMARY_ENDPOINT},
                {"account_name": "secondary", "endpoint_id": SECONDARY_ENDPOINT},
            ],
            models_linked=False,
            extraction_models=[],
            summary_models=configured_summary,
            max_summary_tokens=max_summary_tokens,
            use_neo4j=True,
            chunk_summarization=True,
            target_qdrant_collections=["naive", "hrag", "graph"],
            docling_ocr_enabled=False,
        )
        corpus_body = CorpusCreate(
            name="runpod_e2e_15doc_20260715",
            description=(
                "Fresh owner-authorized 15-document RunPod max-burst E2E; "
                "frozen preregistration 2026-07-15."
            ),
            default_ingestion_config=config,
        ).model_dump(mode="json", exclude_none=True)
        config_hash = hashlib.sha256(
            json.dumps(
                corpus_body["default_ingestion_config"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        token = auth_service.create_access_token(
            user_id=owner_id,
            username=str(owner["username"]),
        )
        corpus = _api(token, "POST", "/api/corpora", corpus_body)
        corpus_id = str(corpus.get("corpus_id") or "")
        if not corpus_id:
            raise RuntimeError("create-corpus response did not contain corpus_id")
        batch_body = {
            "root_path": ROOT,
            "profile": "runpod_burst",
            "recursive": False,
            "extensions": [".md"],
            "max_files": 15,
            "store_files": True,
            "max_total_bytes": 100_000_000,
            "use_neo4j": True,
            "chunk_summarization": True,
            "model": "",
            "concurrency": 1,
            "summary_cost_authority_usd": "30.00",
            "start": True,
        }
        batch = _api(
            token,
            "POST",
            f"/api/corpora/{corpus_id}/ingest-batches/local",
            batch_body,
        )
        batch_id = str(batch.get("batch_id") or "")
        if not batch_id or int(batch.get("total") or 0) != 15:
            raise RuntimeError("local-batch response did not close at 15 items")
        state = {
            "schema_version": "runpod_e2e_15doc_launch.v1",
            "launched_at_utc": datetime.now(timezone.utc).isoformat(),
            "corpus_id": corpus_id,
            "batch_id": batch_id,
            "corpus_name": str(corpus.get("name") or ""),
            "config_sha256": config_hash,
            "selection_sha256": SELECTION_SHA,
            "retrieval_preregistration_sha256": RETRIEVAL_SHA,
            "discovered_file_count": len(files),
            "expected_parent_count": 7031,
            "expected_child_count": 22675,
            "expected_summary_parent_count": 6757,
            "expected_runpod_request_count": 709,
            "summary_authority_usd": "30.00",
            "runpod_authority_usd": "5.00",
            "combined_authority_usd": "35.00",
            "runner_started_in_api_process": bool(batch.get("runner_started")),
            "routes": [
                {"account": "primary", "endpoint": PRIMARY_ENDPOINT},
                {"account": "secondary", "endpoint": SECONDARY_ENDPOINT},
            ],
        }
        _atomic_write(STATE, state)
        print(json.dumps(state, indent=2, sort_keys=True))
    finally:
        client.close()


asyncio.run(main())
