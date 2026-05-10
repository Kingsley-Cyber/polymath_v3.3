"""Dump real production Ghost B request payloads to a JSON file.

Picks 3 chunks from the Design Patterns doc:
  - 0000  : sparse catalog (12 entities, no relations expected)
  - 0029  : known-failed prose chunk (template meta-section)
  - largest: the largest body chunk by token_count

For each chunk emits the EXACT LiteLLM /chat/completions request body
that worker.py would send through extract_entities at attempt=1
normal profile, including schema/schema_lens scaffolding from the
corpus document. No network calls are made.
"""

import asyncio
import json
import os
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ghost_b import _SYSTEM, SchemaContext, build_user_prompt
from services.ingestion.schema_lens import SchemaLens


DOC_ID = "e1066f67cf98bd3cb4db4d0b9d74a2b4728cee8b63b2fa885d5f1f38d3559391"
TARGET_CHUNK_SUFFIXES = ["_0000", "_0029"]
OUT_PATH = "/tmp/ghost_b_payloads.json"


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    db = client.get_default_database()

    doc = await db.documents.find_one({"doc_id": DOC_ID})
    if not doc:
        raise SystemExit(f"doc not found: {DOC_ID}")
    corpus_id = doc["corpus_id"]

    corpus = await db.corpora.find_one({"_id": corpus_id}) or await db.corpora.find_one(
        {"corpus_id": corpus_id}
    )
    if not corpus:
        raise SystemExit(f"corpus not found: {corpus_id}")
    cfg = corpus.get("default_ingestion_config") or {}

    schema_ctx = SchemaContext(
        entity_schema=cfg.get("entity_schema") or [],
        relation_schema=cfg.get("relation_schema") or [],
        strict=bool(cfg.get("schema_strict", False)),
    )

    stored_lens = corpus.get("schema_lens")
    schema_lens = SchemaLens.from_dict(stored_lens) if stored_lens else None

    target_chunks = []
    for sfx in TARGET_CHUNK_SUFFIXES:
        c = await db.chunks.find_one({"chunk_id": DOC_ID + sfx})
        if c:
            target_chunks.append(c)

    largest_cursor = (
        db.chunks.find({"doc_id": DOC_ID, "chunk_kind": "body"})
        .sort([("token_count", -1)])
        .limit(1)
    )
    async for c in largest_cursor:
        if c["chunk_id"] not in {x["chunk_id"] for x in target_chunks}:
            target_chunks.append(c)

    out = []
    for c in target_chunks:
        prompt = build_user_prompt(
            chunk_id=c["chunk_id"],
            doc_id=c["doc_id"],
            corpus_id=c["corpus_id"],
            text=c["text"],
            max_entities=settings.EXTRACTION_MAX_ENTITIES_PER_CHUNK,
            max_relations=settings.EXTRACTION_MAX_RELATIONS_PER_CHUNK,
            schema=schema_ctx,
            schema_lens=schema_lens,
            enable_facts=settings.EXTRACTION_ENABLE_FACTS,
            max_facts=settings.EXTRACTION_MAX_FACTS_PER_CHUNK,
            max_total_lines=settings.EXTRACTION_MAX_TOTAL_LINES,
        )
        model_name = (
            settings.DEFAULT_COMPLETION_MODEL
            or "deepseek/deepseek-v4-flash"
        )
        payload = {
            "model": model_name,
            "temperature": 0,
            "max_tokens": settings.EXTRACTION_MAX_TOKENS,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }
        out.append(
            {
                "chunk_id": c["chunk_id"],
                "chunk_kind": c.get("chunk_kind"),
                "token_count": c.get("token_count"),
                "char_len": len(c.get("text") or ""),
                "litellm_endpoint": f"{settings.LITELLM_URL}/chat/completions",
                "payload": payload,
            }
        )

    Path(OUT_PATH).write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {len(out)} payloads to {OUT_PATH}")
    for entry in out:
        print(
            f"  {entry['chunk_id'][-5:]} kind={entry['chunk_kind']} "
            f"tokens={entry['token_count']} chars={entry['char_len']} "
            f"prompt_chars={len(entry['payload']['messages'][1]['content'])}"
        )


if __name__ == "__main__":
    asyncio.run(main())
