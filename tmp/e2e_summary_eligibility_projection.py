#!/usr/bin/env python3
"""Project summary-volume savings from deterministic parent-size floors."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.summary_cost_control import summary_cost_snapshot


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
CHAR_FLOORS = (80, 120, 200, 300, 500, 800)


async def main() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    corpus_id = str(state["corpus_id"])
    run_id = str(state["batch_id"])
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        rows = await database["parent_chunks"].find(
            {
                "corpus_id": corpus_id,
                "summary": {"$exists": True, "$nin": [None, ""]},
            },
            {"_id": 0, "text": 1, "chunk_kind": 1},
        ).to_list(length=None)
        if not rows:
            raise RuntimeError("summary-eligible parent population is empty")
        snapshot = await summary_cost_snapshot(database, run_id)
        current_cost = float(snapshot.get("ceiling_basis_usd") or 0.0)
        calls = int(snapshot.get("calls_completed") or 0)
        lengths = [len(str(row.get("text") or "").strip()) for row in rows]
        result_rows = []
        for floor in CHAR_FLOORS:
            skipped = sum(1 for size in lengths if size < floor)
            cut = skipped / len(lengths)
            result_rows.append(
                {
                    "minimum_parent_characters": floor,
                    "parents_skipped": skipped,
                    "parents_retained": len(lengths) - skipped,
                    "projected_volume_cut_fraction": round(cut, 6),
                    "projected_calls": round(calls * (1.0 - cut)),
                    "projected_summary_api_cost_usd": round(
                        current_cost * (1.0 - cut), 6
                    ),
                    "projected_api_savings_usd": round(current_cost * cut, 6),
                }
            )
        result = {
            "schema_version": "runpod_e2e_summary_eligibility_projection.v1",
            "measurement_basis": {
                "corpus_id": corpus_id,
                "summarized_parents": len(rows),
                "summary_calls": calls,
                "summary_ceiling_basis_usd": round(current_cost, 9),
                "minimum_observed_characters": min(lengths),
                "median_observed_characters": sorted(lengths)[len(lengths) // 2],
                "maximum_observed_characters": max(lengths),
            },
            "floors": result_rows,
            "classification": "INFERRED deterministic volume/cost projection",
            "assumptions": [
                "call volume and cost decline proportionally with skipped summarized parents",
                "the floor applies only to summary generation; source chunks remain retrievable",
                "quality impact is unmeasured and requires a retrieval canary before activation",
            ],
            "recommended_canary_floor_characters": 200,
            "secret_values_emitted": 0,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        client.close()


asyncio.run(main())
