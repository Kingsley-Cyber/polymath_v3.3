"""Replay specific Ghost B chunks against LiteLLM/local endpoints and tabulate results.

Reads chunk_ids from /tmp/replay_targets.json (a JSON list).

For each chunk:
  - Builds the exact production extraction payload (system prompt,
    schema vocab, schema_lens, line-shape rules, TEXT block).
  - Overrides max_tokens to the value supplied via REPLAY_MAX_TOKENS env
    (defaults to settings.EXTRACTION_MAX_TOKENS).
  - POSTs to LiteLLM by default, or to REPLAY_BASE_URL(S) for local
    OpenAI-compatible extraction servers.
  - Captures finish_reason, usage tokens, latency, tok/s, JSONL parse stats,
    production normalization stats, and a pass/fail verdict.

Writes /tmp/replay_results.json plus prints a tabular summary.
Concurrency is bounded so we don't hammer the gateway. Multiple base URLs are
used round-robin to test true multi-server lanes.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import tiktoken
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import ValidationError

from config import get_settings
from services.ghost_b import (
    FACT_TYPES,
    _SYSTEM,
    _JSON_OBJECT_SYSTEM,
    ExtractionTask,
    SchemaContext,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    _parse,
    _parse_jsonl_items,
    _parse_jsonl_lines,
    build_json_object_prompt,
    build_user_prompt,
)
from services.ghost_b_schemas import ExtractionResponse
from services.ingestion.schema_lens import SchemaLens


TARGETS_PATH = os.environ.get("REPLAY_TARGETS_PATH") or "/tmp/replay_targets.json"
OUT_PATH = os.environ.get("REPLAY_OUT_PATH") or "/tmp/replay_results.json"
CONCURRENCY = int(os.environ.get("REPLAY_CONCURRENCY") or "1")
TOKENIZER = tiktoken.get_encoding("cl100k_base")
PROMPT_MODE = (os.environ.get("REPLAY_PROMPT_MODE") or "jsonl").strip().lower()
OBJECT_ADAPTER = (os.environ.get("REPLAY_OBJECT_ADAPTER") or "object").strip().lower()


def _completion_url(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _replay_base_urls(settings) -> list[str]:
    raw = os.environ.get("REPLAY_BASE_URLS") or os.environ.get("REPLAY_BASE_URL")
    if raw:
        return [_completion_url(part.strip()) for part in raw.split(",") if part.strip()]
    return [_completion_url(settings.LITELLM_URL)]


def _headers(settings, using_custom_base: bool) -> dict:
    explicit_key = os.environ.get("REPLAY_API_KEY")
    api_key = explicit_key if explicit_key is not None else (
        "" if using_custom_base else settings.LITELLM_MASTER_KEY
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _token_count(text: str) -> int:
    try:
        return len(TOKENIZER.encode(str(text or ""), disallowed_special=()))
    except Exception:
        return 0


def _extract_json_object(raw: str) -> tuple[dict | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        return None, "empty_raw"
    try:
        data = json.loads(text)
        return (data, None) if isinstance(data, dict) else (None, "not_object")
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None, "no_json_object"
    try:
        data = json.loads(text[start : end + 1])
        if not isinstance(data, dict):
            return None, "not_object"
        return data, "json_salvaged"
    except json.JSONDecodeError as exc:
        return None, f"json_parse_error:{exc.msg}"


def _json_object_to_jsonl_items(data: dict) -> tuple[list[dict], str | None]:
    """Convert a complete JSON-object extraction into Ghost B JSONL items.

    Some local extraction models, notably document-extraction-tuned models, are
    better at one JSON object than line-delimited JSON. This adapter keeps the
    provider contract model-friendly while still letting us exercise the JSONL
    continuation/normalization path when needed.
    """

    try:
        obj = ExtractionResponse.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ()))
        msg = str(first.get("msg") or "validation failed")
        return [], f"schema_validate_error:{loc}:{msg}"

    items: list[dict] = []
    for entity in obj.entities:
        item = {
            "t": "e",
            "cn": entity.canonical_name,
            "sf": entity.surface_form or entity.canonical_name,
            "et": entity.entity_type,
            "cf": entity.confidence,
        }
        if entity.object_kind:
            item["ek"] = entity.object_kind
        if entity.query_aliases:
            item["qa"] = entity.query_aliases[:5]
        if entity.definitional_phrase:
            item["def"] = entity.definitional_phrase
        items.append(item)

    for relation in obj.relations:
        item = {
            "t": "r",
            "sub": relation.subject,
            "pred": relation.predicate,
            "obj": relation.object,
            "ok": relation.object_kind,
            "cf": relation.confidence,
            "ev": relation.evidence_phrase,
        }
        if relation.relation_cue:
            item["cue"] = relation.relation_cue
        items.append(item)

    for fact in obj.facts:
        item = {
            "t": "f",
            "sub": fact.subject,
            "ft": fact.fact_type,
            "pn": fact.property_name,
            "val": fact.value,
            "cf": fact.confidence,
            "ev": fact.evidence_phrase,
        }
        if fact.unit:
            item["unit"] = fact.unit
        if fact.condition:
            item["cond"] = fact.condition
        items.append(item)
    return items, None


def _jsonl_from_items(items: list[dict]) -> str:
    lines = [
        json.dumps(item, ensure_ascii=True, separators=(",", ":"))
        for item in items
    ]
    lines.append('{"t":"x"}')
    return "\n".join(lines)


def _lfm_schema_system_prompt(schema: SchemaContext, settings) -> str:
    entity_vocab = (
        schema.entity_vocab
        if schema and schema.has_entity_schema
        else list(UNIVERSAL_ENTITY_SCHEMA)
    )
    relation_vocab = (
        schema.relation_vocab
        if schema and schema.has_relation_schema
        else list(UNIVERSAL_RELATION_SCHEMA)
    )
    return (
        "Identify and extract information matching the following schema.\n"
        "Return data as a JSON object. Missing data should be omitted.\n"
        "Extract only information explicitly stated in the document text.\n"
        "Do not explain your answer.\n"
        "Return compact JSON with no markdown or comments.\n"
        "Prefer fewer correct items over broad coverage.\n"
        f"Use at most {settings.EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK} entities, "
        f"{settings.EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK} relations, and "
        f"{settings.EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK} facts.\n"
        "Every surface_form and evidence_phrase must be copied exactly from the text.\n\n"
        "Schema:\n"
        "- entities: list of objects\n"
        "  - canonical_name: lowercase name with punctuation removed\n"
        "  - surface_form: exact phrase copied from the text\n"
        f"  - entity_type: one of {', '.join(entity_vocab)}\n"
        "  - confidence: number from 0 to 1\n"
        "  - query_aliases: optional list of aliases from the text\n"
        "  - definitional_phrase: optional exact definition phrase from the text\n"
        "  - object_kind: optional specific kind, if explicitly supported\n"
        "- relations: list of objects\n"
        "  - subject: entity canonical_name\n"
        f"  - predicate: one of {', '.join(relation_vocab)}\n"
        "  - object: entity canonical_name or literal value\n"
        "  - object_kind: \"entity\" or \"literal\"\n"
        "  - confidence: number from 0 to 1\n"
        "  - evidence_phrase: short exact phrase copied from the text\n"
        "- facts: list of objects\n"
        "  - subject: entity canonical_name\n"
        f"  - fact_type: one of {', '.join(FACT_TYPES)}\n"
        "  - property_name: snake_case property name\n"
        "  - value: verbatim or normalized value from the text\n"
        "  - unit: optional unit\n"
        "  - condition: optional condition\n"
        "  - confidence: number from 0 to 1\n"
        "  - evidence_phrase: short exact phrase copied from the text\n"
    )


def _build_messages(
    *,
    mode: str,
    chunk: dict,
    settings,
    schema_ctx: SchemaContext,
    schema_lens: SchemaLens | None,
) -> list[dict]:
    if mode == "json_object":
        prompt = build_json_object_prompt(
            chunk_id=chunk["chunk_id"],
            doc_id=chunk["doc_id"],
            corpus_id=chunk["corpus_id"],
            text=chunk["text"],
            max_entities=settings.EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK,
            max_relations=settings.EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK,
            schema=schema_ctx,
            schema_lens=schema_lens,
            enable_facts=settings.EXTRACTION_ENABLE_FACTS,
            max_facts=settings.EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK,
            chunk_kind=chunk.get("chunk_kind") or "body",
            metadata=chunk.get("metadata") or {},
        )
        return [
            {"role": "system", "content": _JSON_OBJECT_SYSTEM},
            {"role": "user", "content": prompt},
        ]
    if mode == "lfm_schema":
        return [
            {"role": "system", "content": _lfm_schema_system_prompt(schema_ctx, settings)},
            {
                "role": "user",
                "content": (
                    "Extract structured information from this document chunk.\n\n"
                    f"TEXT:\n{chunk['text']}"
                ),
            },
        ]
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
        chunk_kind=chunk.get("chunk_kind") or "body",
        metadata=chunk.get("metadata") or {},
    )
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ]


async def _call_litellm(
    client: httpx.AsyncClient, url: str, headers: dict, payload: dict
) -> dict:
    started = time.perf_counter()
    r = await client.post(
        url,
        headers=headers,
        json=payload,
        timeout=float(os.environ.get("REPLAY_TIMEOUT_SECONDS") or "180"),
    )
    latency_s = time.perf_counter() - started
    body = (
        r.json()
        if r.headers.get("content-type", "").startswith("application/json")
        else {"raw": r.text}
    )
    return {"status": r.status_code, "body": body, "latency_s": latency_s}


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

    urls = _replay_base_urls(settings)
    using_custom_base = bool(
        os.environ.get("REPLAY_BASE_URLS") or os.environ.get("REPLAY_BASE_URL")
    )
    headers = _headers(settings, using_custom_base)
    payload_extra = json.loads(os.environ.get("REPLAY_PAYLOAD_EXTRA_JSON") or "{}")

    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[dict] = []

    async def _process(idx: int, chunk_id: str, client: httpx.AsyncClient) -> dict:
        chunk = await db.chunks.find_one({"chunk_id": chunk_id})
        if not chunk:
            return {"chunk_id": chunk_id, "verdict": "missing"}
        url = urls[idx % len(urls)]
        model_name = os.environ.get("REPLAY_MODEL") or settings.DEFAULT_COMPLETION_MODEL or "deepseek/deepseek-v4-flash"
        payload = {
            "model": model_name,
            "temperature": 0,
            "max_tokens": max_tokens_override,
            "messages": _build_messages(
                mode=PROMPT_MODE,
                chunk=chunk,
                settings=settings,
                schema_ctx=schema_ctx,
                schema_lens=schema_lens,
            ),
        }
        if payload_extra:
            payload.update(payload_extra)
        if os.environ.get("REPLAY_DISABLE_THINKING") == "1":
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
                    "url": url,
                }
        body = resp.get("body") or {}
        choices = body.get("choices") or []
        choice = choices[0] if choices else {}
        finish = choice.get("finish_reason")
        usage = body.get("usage") or {}
        raw = (choice.get("message") or {}).get("content", "") or ""
        line_cap = settings.EXTRACTION_MAX_TOTAL_LINES
        parsed = _parse_jsonl_lines(raw) if PROMPT_MODE == "jsonl" else None
        items = parsed.items if parsed else []
        item_types = [str(it.get("t") or "") for it in items]
        finished_emitted = parsed.finished if parsed else False
        valid_lines = parsed.valid_lines if parsed else 0
        invalid_tail = bool(parsed.invalid_line) if parsed else False
        task = ExtractionTask(
            chunk_id=chunk["chunk_id"],
            doc_id=chunk["doc_id"],
            corpus_id=chunk["corpus_id"],
            text=chunk["text"],
            chunk_kind=chunk.get("chunk_kind") or "body",
            metadata=chunk.get("metadata") or {},
        )
        normalized = None
        json_error = None
        schema_error = None
        compat_jsonl = None
        compat_parsed = None
        object_counts = {"entities": 0, "relations": 0, "facts": 0}
        if PROMPT_MODE == "jsonl" and items:
            normalized = _parse_jsonl_items(
                items,
                task,
                threshold=settings.ENTITY_CONFIDENCE_THRESHOLD,
                schema=schema_ctx,
                schema_lens=schema_lens,
                enable_facts=settings.EXTRACTION_ENABLE_FACTS,
                max_facts=settings.EXTRACTION_MAX_FACTS_PER_CHUNK,
            )
        elif PROMPT_MODE in {"json_object", "lfm_schema"}:
            data, json_error = _extract_json_object(raw)
            if data is not None:
                compat_items, schema_error = _json_object_to_jsonl_items(data)
                compat_jsonl = _jsonl_from_items(compat_items)
                compat_parsed = _parse_jsonl_lines(compat_jsonl)
                if schema_error:
                    normalized = None
                elif OBJECT_ADAPTER == "jsonl":
                    normalized = _parse_jsonl_items(
                        compat_parsed.items,
                        task,
                        threshold=settings.ENTITY_CONFIDENCE_THRESHOLD,
                        schema=schema_ctx,
                        schema_lens=schema_lens,
                        enable_facts=settings.EXTRACTION_ENABLE_FACTS,
                        max_facts=settings.EXTRACTION_MAX_FACTS_PER_CHUNK,
                    )
                else:
                    normalized = _parse(
                        json.dumps(data, ensure_ascii=True, separators=(",", ":")),
                        task,
                        threshold=settings.ENTITY_CONFIDENCE_THRESHOLD,
                        schema=schema_ctx,
                        schema_lens=schema_lens,
                        enable_facts=settings.EXTRACTION_ENABLE_FACTS,
                        max_facts=settings.EXTRACTION_MAX_FACTS_PER_CHUNK,
                    )
                object_counts = {
                    "entities": len(list(data.get("entities") or [])),
                    "relations": len(list(data.get("relations") or [])),
                    "facts": len(list(data.get("facts") or [])),
                }
                items = compat_items
        verdict_parts = []
        if resp.get("status") and int(resp["status"]) >= 400:
            verdict_parts.append(f"http_{resp['status']}")
        if finish == "length":
            verdict_parts.append("truncated")
        if PROMPT_MODE == "jsonl":
            if not finished_emitted:
                verdict_parts.append("truncated")
            if invalid_tail:
                verdict_parts.append("invalid_tail")
            if valid_lines == 0:
                verdict_parts.append("empty")
            if valid_lines > line_cap + 1:  # +1 for the {"t":"x"} sentinel
                verdict_parts.append("line_cap_exceeded")
        elif json_error and json_error != "json_salvaged":
            verdict_parts.append(json_error)
        if schema_error:
            verdict_parts.append(schema_error)
        if items and normalized is None:
            verdict_parts.append("normalize_failed")
        if normalized is not None and not (
            normalized.entities or normalized.relations or normalized.facts
        ):
            verdict_parts.append("empty_normalized")
        raw_completion_tokens = usage.get("completion_tokens")
        estimated_completion_tokens = _token_count(raw)
        completion_tokens = (
            raw_completion_tokens
            if raw_completion_tokens is not None
            else estimated_completion_tokens
        )
        latency_s = float(resp.get("latency_s") or 0.0)
        completion_tok_s = (
            float(completion_tokens) / latency_s if completion_tokens and latency_s > 0 else None
        )
        verdict = ",".join(verdict_parts) or "pass"
        return {
            "chunk_id": chunk_id,
            "chunk_kind": chunk.get("chunk_kind"),
            "input_tokens": chunk.get("token_count"),
            "char_len": len(chunk.get("text") or ""),
            "url": url,
            "status": resp.get("status"),
            "latency_s": latency_s,
            "completion_tok_s": completion_tok_s,
            "finish_reason": finish,
            "completion_tokens": completion_tokens,
            "completion_tokens_reported": raw_completion_tokens,
            "completion_tokens_estimated": estimated_completion_tokens,
            "prompt_tokens": usage.get("prompt_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "valid_lines": valid_lines,
            "compat_valid_lines": compat_parsed.valid_lines if compat_parsed else 0,
            "compat_finished_sentinel": compat_parsed.finished if compat_parsed else False,
            "invalid_tail": invalid_tail,
            "finished_sentinel": finished_emitted,
            "entities": item_types.count("e") if PROMPT_MODE == "jsonl" else object_counts["entities"],
            "relations": item_types.count("r") if PROMPT_MODE == "jsonl" else object_counts["relations"],
            "facts": item_types.count("f") if PROMPT_MODE == "jsonl" else object_counts["facts"],
            "normalized_entities": len(normalized.entities) if normalized else 0,
            "normalized_relations": len(normalized.relations) if normalized else 0,
            "normalized_facts": len(normalized.facts) if normalized else 0,
            "evidence_drop_count": getattr(normalized, "evidence_drop_count", 0) if normalized else 0,
            "fact_drop_count": getattr(normalized, "fact_drop_count", 0) if normalized else 0,
            "entity_drop_count": getattr(normalized, "entity_drop_count", 0) if normalized else 0,
            "relation_drop_count": getattr(normalized, "relation_drop_count", 0) if normalized else 0,
            "verdict": verdict,
            "prompt_mode": PROMPT_MODE,
            "object_adapter": OBJECT_ADAPTER,
            "json_error": json_error,
            "schema_error": schema_error,
            "raw_first_400": raw[:400],
            "raw_last_400": raw[-400:] if len(raw) > 400 else "",
            "compat_jsonl_first_400": compat_jsonl[:400] if compat_jsonl else "",
        }

    async with httpx.AsyncClient() as client:
        coros = [_process(idx, cid, client) for idx, cid in enumerate(targets)]
        results = await asyncio.gather(*coros, return_exceptions=False)

    wall_latencies = [float(r.get("latency_s") or 0) for r in results]
    completion_rates = [
        float(r["completion_tok_s"])
        for r in results
        if r.get("completion_tok_s") is not None
    ]
    url_counts: dict[str, int] = {}
    for r in results:
        if r.get("url"):
            url_counts[r["url"]] = url_counts.get(r["url"], 0) + 1
    Path(OUT_PATH).write_text(
        json.dumps(
            {
                "doc_id": doc_id,
                "corpus_id": corpus_id,
                "max_tokens": max_tokens_override,
                "prompt_mode": PROMPT_MODE,
                "object_adapter": OBJECT_ADAPTER,
                "concurrency": CONCURRENCY,
                "urls": urls,
                "url_counts": url_counts,
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
    normalized_entities = sum(int(r.get("normalized_entities") or 0) for r in results)
    normalized_relations = sum(int(r.get("normalized_relations") or 0) for r in results)
    normalized_facts = sum(int(r.get("normalized_facts") or 0) for r in results)
    total_latency = sum(wall_latencies)

    print(f"\n=== REPLAY SUMMARY ({max_tokens_override} max_tokens) ===")
    print(f"  model       : {os.environ.get('REPLAY_MODEL') or settings.DEFAULT_COMPLETION_MODEL or 'deepseek/deepseek-v4-flash'}")
    print(f"  prompt_mode : {PROMPT_MODE}")
    if PROMPT_MODE in {"json_object", "lfm_schema"}:
        print(f"  obj adapter : {OBJECT_ADAPTER}")
    print(f"  endpoints   : {len(urls)}  {url_counts}")
    print(f"  concurrency : {CONCURRENCY}")
    print(f"  total       : {len(results)}")
    print(f"  pass        : {pass_count}  ({pass_count/len(results)*100:.0f}%)")
    print(f"  truncated   : {truncated}")
    print(f"  invalid_tail: {invalid}")
    print(f"  line_cap    : {line_cap_hits}")
    print(f"  empty       : {empty}")
    print(f"  errors      : {errors}")
    print(
        f"  normalized : e={normalized_entities} r={normalized_relations} f={normalized_facts}"
    )
    if wall_latencies:
        wall_latencies_sorted = sorted(wall_latencies)
        print(
            f"  latency s  : min={wall_latencies_sorted[0]:.2f} "
            f"median={wall_latencies_sorted[len(wall_latencies_sorted)//2]:.2f} "
            f"max={wall_latencies_sorted[-1]:.2f} "
            f"sum={total_latency:.2f}"
        )
    if completion_rates:
        completion_rates.sort()
        print(
            f"  tok/s       : min={completion_rates[0]:.1f} "
            f"median={completion_rates[len(completion_rates)//2]:.1f} "
            f"max={completion_rates[-1]:.1f}"
        )
    if completions:
        completions.sort()
        n = len(completions)
        print(
            f"  completion tokens — min={completions[0]}  "
            f"median={completions[n // 2]}  max={completions[-1]}  "
            f"avg={sum(completions) // n}"
        )
    print()
    print(f"{'chunk':<8} {'kind':<14} {'in':>5} {'out':>5} {'sec':>6} {'tok/s':>7} {'finish':<8} {'lines':>5} {'e/r/f':>9} {'norm':>9} {'verdict'}")
    for r in results:
        cid = r["chunk_id"][-5:] if r.get("chunk_id") else "?"
        erf = f"{r.get('entities','-')}/{r.get('relations','-')}/{r.get('facts','-')}"
        norm = f"{r.get('normalized_entities','-')}/{r.get('normalized_relations','-')}/{r.get('normalized_facts','-')}"
        tok_s = r.get("completion_tok_s")
        print(
            f"{cid:<8} {str(r.get('chunk_kind','?'))[:14]:<14} "
            f"{str(r.get('input_tokens','?')):>5} "
            f"{str(r.get('completion_tokens','?')):>5} "
            f"{float(r.get('latency_s') or 0):>6.2f} "
            f"{(f'{tok_s:.1f}' if tok_s is not None else '?'):>7} "
            f"{str(r.get('finish_reason','?')):<8} "
            f"{str(r.get('valid_lines','?')):>5} "
            f"{erf:>9} "
            f"{norm:>9} "
            f"{r.get('verdict','?')}"
        )


if __name__ == "__main__":
    asyncio.run(main())
