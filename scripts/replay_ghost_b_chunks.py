"""Replay specific Ghost B chunks against LiteLLM and tabulate results.

Reads chunk_ids from /tmp/replay_targets.json (a JSON list).

For each chunk:
  - Builds the exact production extraction payload (system prompt,
    schema vocab, schema_lens, line-shape rules, TEXT block).
  - Overrides max_tokens to the value supplied via REPLAY_MAX_TOKENS env
    (defaults to settings.EXTRACTION_MAX_TOKENS).
  - POSTs to LiteLLM at settings.LITELLM_URL with the master key.
  - Captures finish_reason, usage tokens, JSONL parse stats, and a
    pass/fail verdict.

Writes /tmp/replay_results.json plus prints a tabular summary.
Concurrency is bounded so we don't hammer the gateway.
"""

import asyncio
import json
import os
from pathlib import Path

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ghost_b import (
    _SYSTEM,
    SchemaContext,
    _parse_jsonl_lines,
    build_user_prompt,
)
from services.ingestion.schema_lens import SchemaLens


TARGETS_PATH = "/tmp/replay_targets.json"
OUT_PATH = "/tmp/replay_results.json"
CONCURRENCY = int(os.environ.get("REPLAY_CONCURRENCY") or "1")


async def _call_litellm(
    client: httpx.AsyncClient, url: str, headers: dict, payload: dict
) -> dict:
    r = await client.post(url, headers=headers, json=payload, timeout=120.0)
    return {"status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}}


async def main() -> None:
    settings = get_settings()
    max_tokens_override = int(
        os.environ.get("REPLAY_MAX_TOKENS") or settings.EXTRACTION_MAX_TOKENS
    )

    targets = json.loads(Path(TARGETS_PATH).read_text(encoding="utf-8"))
    if not targets:
        raise SystemExit("no targets loaded")

    mongo = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    db = mongo.get_default_database()

    sample_chunk = await db.chunks.find_one({"chunk_id": targets[0]})
    if not sample_chunk:
        raise SystemExit(f"sample chunk not found: {targets[0]}")
    doc_id = sample_chunk["doc_id"]
    corpus_id = sample_chunk["corpus_id"]

    corpus = (
        await db.corpora.find_one({"_id": corpus_id})
        or await db.corpora.find_one({"corpus_id": corpus_id})
    )
    cfg = (corpus or {}).get("default_ingestion_config") or {}
    schema_ctx = SchemaContext(
        entity_schema=cfg.get("entity_schema") or [],
        relation_schema=cfg.get("relation_schema") or [],
        strict=bool(cfg.get("schema_strict", False)),
    )
    stored_lens = (corpus or {}).get("schema_lens")
    schema_lens = SchemaLens.from_dict(stored_lens) if stored_lens else None

    headers = {
        "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{settings.LITELLM_URL}/chat/completions"

    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[dict] = []

    async def _process(chunk_id: str, client: httpx.AsyncClient) -> dict:
        chunk = await db.chunks.find_one({"chunk_id": chunk_id})
        if not chunk:
            return {"chunk_id": chunk_id, "verdict": "missing"}
        prompt = build_user_prompt(
            chunk_id=chunk["chunk_id"],
            doc_id=chunk["doc_id"],
            corpus_id=chunk["corpus_id"],
            text=chunk["text"],
            max_entities=settings.EXTRACTION_MAX_ENTITIES_PER_CHUNK,
            max_relations=settings.EXTRACTION_MAX_RELATIONS_PER_CHUNK,
            schema=schema_ctx,
            schema_lens=schema_lens,
            enable_facts=settings.EXTRACTION_ENABLE_FACTS,
            max_facts=settings.EXTRACTION_MAX_FACTS_PER_CHUNK,
            max_total_lines=settings.EXTRACTION_MAX_TOTAL_LINES,
        )
        model_name = os.environ.get("REPLAY_MODEL") or settings.DEFAULT_COMPLETION_MODEL or "deepseek/deepseek-v4-flash"
        payload = {
            "model": model_name,
            "temperature": 0,
            "max_tokens": max_tokens_override,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }
        if os.environ.get("REPLAY_DISABLE_THINKING") == "1" and model_name.startswith("deepseek/"):
            payload["thinking"] = {"type": "disabled"}
        async with sem:
            try:
                resp = await _call_litellm(client, url, headers, payload)
            except Exception as exc:
                return {
                    "chunk_id": chunk_id,
                    "input_tokens": chunk.get("token_count"),
                    "verdict": "error",
                    "error": str(exc)[:200],
                }
        body = resp.get("body") or {}
        choices = body.get("choices") or []
        choice = choices[0] if choices else {}
        finish = choice.get("finish_reason")
        usage = body.get("usage") or {}
        raw = (choice.get("message") or {}).get("content", "") or ""
        parsed = _parse_jsonl_lines(raw)
        items = parsed.items
        item_types = [str(it.get("t") or "") for it in items]
        finished_emitted = parsed.finished
        line_cap = settings.EXTRACTION_MAX_TOTAL_LINES
        valid_lines = parsed.valid_lines
        invalid_tail = bool(parsed.invalid_line)
        verdict_parts = []
        if finish == "length" or not finished_emitted:
            verdict_parts.append("truncated")
        if invalid_tail:
            verdict_parts.append("invalid_tail")
        if valid_lines == 0:
            verdict_parts.append("empty")
        if valid_lines > line_cap + 1:  # +1 for the {"t":"x"} sentinel
            verdict_parts.append("line_cap_exceeded")
        verdict = ",".join(verdict_parts) or "pass"
        return {
            "chunk_id": chunk_id,
            "chunk_kind": chunk.get("chunk_kind"),
            "input_tokens": chunk.get("token_count"),
            "char_len": len(chunk.get("text") or ""),
            "finish_reason": finish,
            "completion_tokens": usage.get("completion_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "valid_lines": valid_lines,
            "invalid_tail": invalid_tail,
            "finished_sentinel": finished_emitted,
            "entities": item_types.count("e"),
            "relations": item_types.count("r"),
            "facts": item_types.count("f"),
            "verdict": verdict,
            "raw_first_400": raw[:400],
            "raw_last_400": raw[-400:] if len(raw) > 400 else "",
        }

    async with httpx.AsyncClient() as client:
        coros = [_process(cid, client) for cid in targets]
        results = await asyncio.gather(*coros, return_exceptions=False)

    Path(OUT_PATH).write_text(
        json.dumps(
            {
                "doc_id": doc_id,
                "corpus_id": corpus_id,
                "max_tokens": max_tokens_override,
                "total": len(results),
                "results": results,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    pass_count = sum(1 for r in results if r.get("verdict") == "pass")
    truncated = sum(1 for r in results if "truncated" in (r.get("verdict") or ""))
    invalid = sum(1 for r in results if "invalid_tail" in (r.get("verdict") or ""))
    line_cap_hits = sum(
        1 for r in results if "line_cap_exceeded" in (r.get("verdict") or "")
    )
    empty = sum(1 for r in results if "empty" in (r.get("verdict") or ""))
    errors = sum(1 for r in results if r.get("verdict") == "error")
    completions = [r["completion_tokens"] for r in results if r.get("completion_tokens") is not None]

    print(f"\n=== REPLAY SUMMARY ({max_tokens_override} max_tokens) ===")
    print(f"  total       : {len(results)}")
    print(f"  pass        : {pass_count}  ({pass_count/len(results)*100:.0f}%)")
    print(f"  truncated   : {truncated}")
    print(f"  invalid_tail: {invalid}")
    print(f"  line_cap    : {line_cap_hits}")
    print(f"  empty       : {empty}")
    print(f"  errors      : {errors}")
    if completions:
        completions.sort()
        n = len(completions)
        print(
            f"  completion tokens — min={completions[0]}  "
            f"median={completions[n // 2]}  max={completions[-1]}  "
            f"avg={sum(completions) // n}"
        )
    print()
    print(f"{'chunk':<8} {'kind':<14} {'in':>5} {'out':>5} {'finish':<8} {'lines':>5} {'e/r/f':>9} {'verdict'}")
    for r in results:
        cid = r["chunk_id"][-5:] if r.get("chunk_id") else "?"
        erf = f"{r.get('entities','-')}/{r.get('relations','-')}/{r.get('facts','-')}"
        print(
            f"{cid:<8} {str(r.get('chunk_kind','?'))[:14]:<14} "
            f"{str(r.get('input_tokens','?')):>5} "
            f"{str(r.get('completion_tokens','?')):>5} "
            f"{str(r.get('finish_reason','?')):<8} "
            f"{str(r.get('valid_lines','?')):>5} "
            f"{erf:>9} "
            f"{r.get('verdict','?')}"
        )


if __name__ == "__main__":
    asyncio.run(main())
