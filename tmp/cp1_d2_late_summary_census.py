import asyncio
import json
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings


CORPUS_ID = "62193743-4175-40da-b861-ba1e1e567b9a"


def provider_for_model(model: str) -> str:
    normalized = model.lower()
    if "hy3" in normalized:
        return "siliconflow"
    if "deepseek" in normalized:
        return "deepseek"
    if "longcat" in normalized:
        return "longcat"
    return "unknown"


async def main() -> None:
    diagnosis = json.loads(
        Path("/tmp/rebatch_v2_g2_diagnosis.log").read_text(encoding="utf-8")
    )
    expected = {
        str(row["parent_id"]): row for row in diagnosis["missing_samples"]
    }
    assert len(expected) == 19
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    rows = await db.parent_chunks.find(
        {"corpus_id": CORPUS_ID, "parent_id": {"$in": sorted(expected)}},
        {
            "_id": 0,
            "parent_id": 1,
            "doc_id": 1,
            "filename": 1,
            "chunk_kind": 1,
            "summary_model": 1,
            "summary_provider": 1,
            "summary_created_at": 1,
            "validation_status": 1,
            "schema_version": 1,
            "temporal_class": 1,
            "summary": 1,
        },
    ).sort("parent_id", 1).to_list(length=19)
    assert len(rows) == 19
    census = []
    for row in rows:
        model = str(row.get("summary_model") or "")
        census.append(
            {
                "parent_id": row["parent_id"],
                "filename": row.get("filename")
                or expected[row["parent_id"]].get("filename"),
                "chunk_kind": row.get("chunk_kind")
                or expected[row["parent_id"]].get("kind"),
                "provider": row.get("summary_provider")
                or provider_for_model(model),
                "model": model,
                "summary_created_at": row.get("summary_created_at"),
                "validation_status": row.get("validation_status"),
                "schema_version": row.get("schema_version"),
                "temporal_class": row.get("temporal_class"),
                "summary_present": bool(str(row.get("summary") or "").strip()),
            }
        )
    assert all(row["summary_present"] for row in census)
    assert all(row["model"] for row in census)
    watch_list = [row["parent_id"] for row in census if "hy3" in row["model"].lower()]
    model_counts = {}
    for row in census:
        key = f"{row['provider']}::{row['model']}"
        model_counts[key] = model_counts.get(key, 0) + 1
    print(
        json.dumps(
            {
                "row_count": len(census),
                "model_counts": model_counts,
                "hy3_watch_list": watch_list,
                "rows": census,
            },
            default=str,
            indent=2,
            sort_keys=True,
        )
    )
    client.close()


asyncio.run(main())
