"""Backfill bridge `mechanisms` onto parent chunks (bridge retrieval B1).

The retrieval layer has domain + topics but NO mechanism layer, so a Robert
Greene chapter and an Atomic Habits chapter that both describe "compounding via
repetition" share zero matchable metadata — the root cause of low cross-domain
recall (see CONTINUITY/BRIDGE_RETRIEVAL_DESIGN.md).

This reads each parent's ALREADY-WRITTEN Ghost-A summary (no re-summarization,
no re-embedding) and asks a cheap model for 2-5 transferable, snake_case
mechanisms — the abstract process, not the surface topic — then stores them on
parent_chunks.mechanisms (Mongo) and the Qdrant summary payload (+ keyword
index). Idempotent (skips parents already tagged), resumable, and independent
of the live ingest pipeline.

Usage (in the backend container):
  python -m scripts.backfill_mechanisms --corpus <id> --limit 30 --dry-run
  python -m scripts.backfill_mechanisms --corpus <id> [--concurrency 8]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from config import get_settings
from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

from services.llm import llm_service
from services.storage import qdrant_writer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backfill_mechanisms")

_SYSTEM = (
    "You extract TRANSFERABLE MECHANISMS from a passage summary. A mechanism is "
    "the abstract underlying process or dynamic — named so it could match a "
    "DIFFERENT field, not the surface topic. Examples: feedback_loop, "
    "compounding, reinforcement, spaced_repetition, threshold_dynamics, "
    "state_transition, hierarchical_decomposition, incentive_shaping, "
    "environment_shapes_behavior, small_inputs_large_outputs, "
    "repetition_changes_system_state, constraints_drive_adaptation. "
    "Return ONLY a JSON object {\"mechanisms\": [\"mech1\", \"mech2\"]} with 2-5 "
    "lowercase snake_case items, or {\"mechanisms\": []} if the summary describes "
    "no general mechanism."
)


def _parse(raw: str) -> list[str]:
    text = (raw or "").strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e <= s:
        return []
    try:
        obj = json.loads(text[s : e + 1])
    except Exception:
        return []
    out: list[str] = []
    for m in obj.get("mechanisms") or []:
        m = str(m).strip().lower().replace(" ", "_").replace("-", "_")
        m = "".join(ch for ch in m if ch.isalnum() or ch == "_").strip("_")
        if 3 <= len(m) <= 40 and m not in out:
            out.append(m)
    return out[:5]


async def _mechanisms_for(summary: str, model: str) -> list[str]:
    reply = await llm_service.complete_sync(
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"SUMMARY:\n{summary}\n\nReturn only the JSON."},
        ],
        model=model,
        temperature=0.0,
        max_tokens=120,
        timeout=60.0,
    )
    return _parse(reply)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = all untagged")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--model", default=None, help="default: deepseek-chat (cheap, non-thinking)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    model = args.model or "deepseek/deepseek-chat"
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client.get_default_database()
    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL, timeout=settings.QDRANT_TIMEOUT_SECONDS)

    query = {
        "corpus_id": args.corpus,
        "summary": {"$exists": True, "$ne": None, "$ne": ""},
        "mechanisms": {"$exists": False},
    }
    projection = {"_id": 0, "parent_id": 1, "summary": 1}
    cursor = db["parent_chunks"].find(query, projection)
    if args.limit:
        cursor = cursor.limit(args.limit)
    parents = await cursor.to_list(length=args.limit or None)
    log.info("corpus=%s untagged_parents=%d model=%s", args.corpus[:8], len(parents), model)
    if not parents:
        return

    sem = asyncio.Semaphore(max(1, args.concurrency))
    hrag = qdrant_writer._col_for_corpus(args.corpus, "hrag")
    naive = qdrant_writer._col_for_corpus(args.corpus, "naive")
    tagged = 0
    samples: list[tuple[str, list[str]]] = []

    async def _one(p: dict) -> None:
        nonlocal tagged
        async with sem:
            try:
                mechs = await _mechanisms_for(str(p.get("summary") or ""), model)
            except Exception as exc:  # noqa: BLE001
                log.debug("mechanism extraction failed for %s: %s", p.get("parent_id"), exc)
                return
        if not mechs:
            mechs = []  # store empty so idempotent re-runs skip it
        if len(samples) < 12:
            samples.append((str(p.get("parent_id"))[:16], mechs))
        if args.dry_run:
            return
        pid = str(p.get("parent_id"))
        await db["parent_chunks"].update_one(
            {"corpus_id": args.corpus, "parent_id": pid},
            {"$set": {"mechanisms": mechs}},
        )
        # denormalize to the parent's SUMMARY point (chunk_id = f"{pid}_summary")
        for col in (hrag, naive):
            try:
                await qdrant.set_payload(
                    collection_name=col,
                    payload={"mechanisms": mechs},
                    points=qm.Filter(must=[qm.FieldCondition(key="parent_id", match=qm.MatchValue(value=pid))]),
                    wait=False,
                )
            except Exception:  # noqa: BLE001
                pass
        tagged += 1

    await asyncio.gather(*[_one(p) for p in parents])

    log.info("SAMPLE mechanisms:")
    for pid, mechs in samples:
        log.info("  %s -> %s", pid, mechs)
    if args.dry_run:
        log.info("[dry-run] would tag %d parents", len(parents))
        return

    for col in (hrag, naive):
        try:
            await qdrant.create_payload_index(
                collection_name=col, field_name="mechanisms",
                field_schema=qm.PayloadSchemaType.KEYWORD, wait=True,
            )
        except Exception:  # noqa: BLE001
            pass
    log.info("tagged=%d parents (mechanisms written to Mongo + Qdrant summary payloads)", tagged)


if __name__ == "__main__":
    asyncio.run(main())
