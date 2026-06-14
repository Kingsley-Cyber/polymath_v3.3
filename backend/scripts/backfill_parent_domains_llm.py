"""LLM backfill of `domain` + `topics` onto parent_chunks (Ghost-A-style tags).

Upgrades the generic cluster labels (from backfill_parent_domains.py) to real
semantic domains and adds topic keywords, in the shape Ghost A will eventually
emit at summary time:

    {"domain": "generative_ai", "topics": ["attention", "transformer_architecture"]}

The domain (constrained to TAXONOMY) feeds domain-aware final selection
(select_facet_final max_per_domain) for BROAD queries; topics are stored for
future faceting. Resumable: only processes parents that have a summary and lack
`topics`, so re-running continues where it left off.

Pool/keys are read from an UNTRACKED file (never committed):
    /tmp/domain_pool.json  ==  [{"model","base_url","api_key","max_concurrent"}, ...]

Run in-container:
    docker exec -i polymath_v33-backend-1 python scripts/backfill_parent_domains_llm.py --sample 25
    docker exec -d polymath_v33-backend-1 python scripts/backfill_parent_domains_llm.py
"""
import argparse
import asyncio
import json
import logging
import os

import httpx

from services.conversation import conversation_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_domains_llm")

POOL_PATH = os.environ.get("DOMAIN_POOL_PATH", "/tmp/domain_pool.json")

TAXONOMY = [
    "generative_ai", "machine_learning", "deep_learning", "nlp",
    "computer_vision", "software_engineering", "web_development",
    "data_engineering", "devops_cloud", "cybersecurity", "game_development",
    "creative_coding", "product_design", "ux_design", "psychology",
    "business_strategy", "research_methods", "mathematics", "other",
]

_SYSTEM = (
    "You classify a document chunk into exactly one domain from a fixed taxonomy "
    "and extract 2-4 short topic keywords. Respond with ONLY a JSON object: "
    '{"domain": "<one of the taxonomy values>", "topics": ["kw1", "kw2"]}. '
    "Pick the single closest domain. Topics are lowercase noun phrases.\n"
    "Taxonomy: " + ", ".join(TAXONOMY)
)


def _load_pool() -> list[dict]:
    with open(POOL_PATH) as fh:
        pool = json.load(fh)
    if not isinstance(pool, list) or not pool:
        raise RuntimeError(f"{POOL_PATH} must be a non-empty JSON list of lane configs")
    return pool


async def _classify(client: httpx.AsyncClient, lane: dict, summary: str) -> dict | None:
    payload = {
        "model": lane["model"],
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": summary[:4000]},
        ],
        "temperature": 0,
        "max_tokens": 160,
        "response_format": {"type": "json_object"},
        **(lane.get("extra_params") or {}),
    }
    try:
        resp = await client.post(
            f"{lane['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {lane['api_key']}", "Content-Type": "application/json"},
            json=payload,
            timeout=60.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        obj = json.loads(content)
    except Exception as exc:
        logger.debug("classify failed: %s", exc)
        return None
    domain = str(obj.get("domain") or "").strip().lower().replace(" ", "_")
    if domain not in TAXONOMY:
        domain = "other"
    topics = [str(t).strip().lower() for t in (obj.get("topics") or []) if str(t).strip()][:4]
    return {"domain": domain, "topics": topics}


async def run(sample: int | None = None, limit: int | None = None) -> dict:
    pool = _load_pool()
    total_conc = sum(int(l.get("max_concurrent", 8)) for l in pool)
    sem = asyncio.Semaphore(max(1, total_conc))
    await conversation_service.connect()
    db = conversation_service._db

    query = {"summary": {"$type": "string", "$ne": ""}, "topics": {"$exists": False}}
    projection = {"_id": 0, "parent_id": 1, "doc_id": 1, "summary": 1}
    pending = await db["parent_chunks"].count_documents(query)
    logger.info("parents pending (summary present, no topics): %d", pending)

    cursor = db["parent_chunks"].find(query, projection)
    if sample:
        cursor = cursor.limit(sample)
    elif limit:
        cursor = cursor.limit(limit)
    rows = await cursor.to_list(length=None)

    done = {"ok": 0, "fail": 0}
    results_preview: list[dict] = []

    async with httpx.AsyncClient() as client:
        async def worker(row: dict, lane: dict) -> None:
            async with sem:
                tags = await _classify(client, lane, row["summary"])
            if not tags:
                done["fail"] += 1
                return
            await db["parent_chunks"].update_one(
                {"parent_id": row["parent_id"], "doc_id": row["doc_id"]},
                {"$set": {"domain": tags["domain"], "topics": tags["topics"]}},
            )
            done["ok"] += 1
            if sample and len(results_preview) < sample:
                results_preview.append({"domain": tags["domain"], "topics": tags["topics"],
                                        "summary_head": row["summary"][:90]})
            if done["ok"] % 500 == 0:
                logger.info("progress: ok=%d fail=%d", done["ok"], done["fail"])

        # round-robin rows across lanes
        tasks = [worker(row, pool[i % len(pool)]) for i, row in enumerate(rows)]
        await asyncio.gather(*tasks)

    logger.info("DONE ok=%d fail=%d (of %d fetched, %d total pending)",
                done["ok"], done["fail"], len(rows), pending)
    for r in results_preview:
        logger.info("  domain=%-18s topics=%s | %s", r["domain"], r["topics"], r["summary_head"])
    return {"ok": done["ok"], "fail": done["fail"], "fetched": len(rows), "pending": pending}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=None, help="process only N parents (validation)")
    ap.add_argument("--limit", type=int, default=None, help="cap total processed this run")
    args = ap.parse_args()
    asyncio.run(run(sample=args.sample, limit=args.limit))
