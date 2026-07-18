"""Retry only the durable batch POST on the response-discovered empty corpus."""

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
from services.auth import auth_service
from services.ingestion.batches import discover_local_files


ROOT = "/ingest-source/runpod_e2e_15doc_20260715"
CORPUS_NAME = "runpod_e2e_15doc_20260715"
STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
SELECTION_SHA = "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
RETRIEVAL_SHA = "8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110"
PRIMARY_ENDPOINT = "hk81nfl5cnwufx"
SECONDARY_ENDPOINT = "8tafde7potcsjw"


def _atomic_write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
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


def _api(token: str, corpus_id: str, body: dict[str, Any]) -> dict[str, Any]:
    path = f"/api/corpora/{corpus_id}/ingest-batches/local"
    request = urllib.request.Request(
        f"http://localhost:8000{path}",
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
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
        users = await database["users"].find(
            {}, {"_id": 1, "username": 1}
        ).to_list(length=2)
        if len(users) != 1:
            raise RuntimeError("exactly one API owner must be discoverable")
        owner = users[0]
        owner_id = str(owner.get("_id") or "")
        if not ObjectId.is_valid(owner_id) or not owner.get("username"):
            raise RuntimeError("API owner identity is invalid")
        corpora = await database["corpora"].find(
            {"name": CORPUS_NAME}, {"_id": 0}
        ).to_list(length=2)
        if len(corpora) != 1:
            raise RuntimeError("fresh empty corpus did not resolve exactly once")
        corpus = corpora[0]
        corpus_id = str(corpus.get("corpus_id") or "")
        if not corpus_id:
            raise RuntimeError("fresh corpus identity is absent")
        zero_checks = {
            "documents": await database["documents"].count_documents(
                {"corpus_id": corpus_id}
            ),
            "ingest_batches": await database["ingest_batches"].count_documents(
                {"corpus_id": corpus_id}
            ),
            "ingest_batch_items": await database[
                "ingest_batch_items"
            ].count_documents({"corpus_id": corpus_id}),
        }
        if any(zero_checks.values()):
            raise RuntimeError(f"corpus is no longer empty: {zero_checks}")
        config = corpus.get("default_ingestion_config") or {}
        expected_routes = [
            {"account_name": "primary", "endpoint_id": PRIMARY_ENDPOINT},
            {"account_name": "secondary", "endpoint_id": SECONDARY_ENDPOINT},
        ]
        if (
            config.get("extraction_engine") != "runpod_flash"
            or config.get("runpod_wire_contract") != "local_extraction_v1"
            or config.get("runpod_local_extraction_routes") != expected_routes
            or config.get("chunk_summarization") is not True
            or config.get("use_neo4j") is not True
            or int(config.get("embedding_dimension") or 0) != 1024
        ):
            raise RuntimeError("frozen fresh-corpus config failed closure")
        _, files = discover_local_files(
            ROOT, recursive=False, extensions=[".md"], max_files=15
        )
        if len(files) != 15 or any(path.name.startswith("._") for path in files):
            raise RuntimeError("production discovery did not resolve 15 real files")
        token = auth_service.create_access_token(
            user_id=owner_id, username=str(owner["username"])
        )
        batch = _api(
            token,
            corpus_id,
            {
                "root_path": ROOT,
                "profile": "runpod_burst",
                "recursive": False,
                "extensions": [".md"],
                "max_files": 15,
                "store_files": True,
                "use_neo4j": True,
                "chunk_summarization": True,
                "model": "",
                "concurrency": 1,
                "summary_cost_authority_usd": "30.00",
                "start": True,
            },
        )
        batch_id = str(batch.get("batch_id") or "")
        if not batch_id or int(batch.get("total") or 0) != 15:
            raise RuntimeError("local-batch response did not close at 15 items")
        config_hash = hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        state = {
            "schema_version": "runpod_e2e_15doc_launch.v1",
            "launched_at_utc": datetime.now(timezone.utc).isoformat(),
            "corpus_id": corpus_id,
            "batch_id": batch_id,
            "corpus_name": CORPUS_NAME,
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
