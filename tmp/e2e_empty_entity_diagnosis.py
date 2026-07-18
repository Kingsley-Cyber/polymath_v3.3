"""Classify the E2E empty-canonical failure without emitting source text."""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.gliner_mentions import normalize_mention_name
from services.runpod_flash_extraction import RUNPOD_API_BASE, _extract_output
from services.settings import settings_service


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")
EXPECTED_ERROR = "LocalExtractionV1 entity canonical label is empty"


def _category_counts(value: str) -> dict[str, int]:
    return dict(sorted(Counter(unicodedata.category(ch) for ch in value).items()))


async def main() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = mongo[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        failed = await db["ingest_batch_items"].find_one(
            {
                "batch_id": batch_id,
                "corpus_id": corpus_id,
                "status": "failed",
                "error": EXPECTED_ERROR,
            },
            {"_id": 0, "doc_id": 1, "filename": 1},
        )
        if not failed or not failed.get("doc_id"):
            raise RuntimeError("failed item lacks durable document identity")
        failed_doc_id = str(failed["doc_id"])

        accounts = await settings_service.get_system_runpod_flash_accounts()
        account_keys = {
            account.name: key for account, key in accounts if account.enabled and key
        }
        corpus_hash = hashlib.sha256(corpus_id.encode("utf-8")).hexdigest()
        journal_path = JOURNAL_ROOT / f"corpus-{corpus_hash}.jsonl"
        journal = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        submitted = [row for row in journal if row.get("event") == "submitted"]
        if len({str(row.get("job_id") or "") for row in submitted}) != len(submitted):
            raise RuntimeError("submitted job IDs are not unique")

        semaphore = asyncio.Semaphore(20)

        async def fetch(row: dict[str, Any], client: httpx.AsyncClient) -> dict[str, Any]:
            account_name = str(row.get("account_name") or "")
            endpoint_id = str(row.get("endpoint_id") or "")
            job_id = str(row.get("job_id") or "")
            key = account_keys.get(account_name)
            if not key or not endpoint_id or not job_id:
                raise RuntimeError("journal route identity is incomplete")
            async with semaphore:
                response = await client.get(
                    f"{RUNPOD_API_BASE}/{endpoint_id}/status/{job_id}",
                    headers={"Authorization": f"Bearer {key}"},
                )
            response.raise_for_status()
            body = response.json()
            if str(body.get("status") or "").upper() != "COMPLETED":
                raise RuntimeError("retained provider job is not completed")
            return _extract_output(body)

        async with httpx.AsyncClient(timeout=60) as http:
            outputs = await asyncio.gather(*(fetch(row, http) for row in submitted))

        matching_results = []
        matching_outputs = 0
        for output in outputs:
            rows = output.get("results") if isinstance(output, dict) else None
            if not isinstance(rows, list):
                raise RuntimeError("retained output result shape drifted")
            selected = [row for row in rows if row.get("document_id") == failed_doc_id]
            if selected:
                matching_outputs += 1
                matching_results.extend(selected)
        if not matching_results:
            raise RuntimeError("no retained outputs match failed document identity")

        child_ids = [str(row.get("child_id") or "") for row in matching_results]
        chunks = await db["chunks"].find(
            {"corpus_id": corpus_id, "chunk_id": {"$in": child_ids}},
            {"_id": 0, "chunk_id": 1, "text": 1},
        ).to_list(length=len(child_ids))
        text_by_child = {str(row["chunk_id"]): str(row.get("text") or "") for row in chunks}
        if set(text_by_child) != set(child_ids):
            raise RuntimeError("failed-document child text closure is incomplete")

        empty_shapes = []
        entity_count = 0
        for result in matching_results:
            extraction = result.get("extraction") or {}
            entities = extraction.get("entities") or []
            entity_count += len(entities)
            child_id = str(result.get("child_id") or "")
            source = text_by_child[child_id]
            for entity in entities:
                canonical = str(entity.get("canonical_label") or "")
                if canonical.strip():
                    continue
                surface = str(entity.get("text") or "")
                start = entity.get("start_char")
                end = entity.get("end_char")
                exact_round_trip = (
                    type(start) is int
                    and type(end) is int
                    and 0 <= start < end <= len(source)
                    and source[start:end] == surface
                )
                empty_shapes.append(
                    {
                        "surface_length": len(surface),
                        "surface_category_counts": _category_counts(surface),
                        "surface_has_letter_or_number": any(ch.isalnum() for ch in surface),
                        "surface_normalizes_empty": normalize_mention_name(surface) == "",
                        "canonical_length": len(canonical),
                        "canonical_category_counts": _category_counts(canonical),
                        "canonical_whitespace_only": bool(canonical) and not canonical.strip(),
                        "span_round_trip": exact_round_trip,
                        "entity_type_present": bool(str(entity.get("entity_type") or "").strip()),
                        "confidence_in_range": isinstance(entity.get("confidence"), (int, float))
                        and 0.0 <= float(entity["confidence"]) <= 1.0,
                    }
                )
        if not empty_shapes:
            raise RuntimeError("retained outputs do not reproduce empty canonical label")
        if any(not row["surface_normalizes_empty"] for row in empty_shapes):
            raise RuntimeError("diagnosis found a legitimate surface losing canonical content")
        if any(row["surface_has_letter_or_number"] for row in empty_shapes):
            raise RuntimeError("diagnosis found alphanumeric content in an empty canonical surface")
        if any(not row["span_round_trip"] for row in empty_shapes):
            raise RuntimeError("diagnosis found a source span defect")

        print(
            json.dumps(
                {
                    "schema_version": "e2e_empty_canonical_diagnosis.v1",
                    "retained_jobs_fetched": len(outputs),
                    "failed_document_outputs": matching_outputs,
                    "failed_document_results": len(matching_results),
                    "failed_document_entities": entity_count,
                    "empty_canonical_mentions": len(empty_shapes),
                    "classification": "normalization_empty_non_alphanumeric_noise",
                    "all_source_spans_round_trip": all(row["span_round_trip"] for row in empty_shapes),
                    "all_surfaces_normalize_empty": all(row["surface_normalizes_empty"] for row in empty_shapes),
                    "all_surfaces_non_alphanumeric": all(not row["surface_has_letter_or_number"] for row in empty_shapes),
                    "shape_rows": empty_shapes,
                    "raw_text_emitted": 0,
                    "secret_values_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
