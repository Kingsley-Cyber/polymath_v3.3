#!/usr/bin/env python3
"""Quick local extractor contract benchmark.

This is intentionally standalone: it does not touch Mongo, Qdrant, Neo4j, or
the ingest workers. It probes an OpenAI-compatible local model server and
checks the minimum contract a local Ghost-B-style extractor must satisfy:

  - valid JSON object
  - no reasoning / <think> leakage
  - expected top-level fields
  - evidence strings are exact substrings of the source chunk
  - wall-clock latency and estimated decode throughput

The production parser should remain the source of truth for real ingestion.
This script is a fast isolation harness for model/server selection.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SAMPLES = [
    {
        "id": "sample_architecture",
        "text": (
            "Polymath ingests documents through parse, parent chunking, child "
            "chunking, Ghost A summaries, Ghost B extraction, Mongo storage, "
            "Qdrant vector writes, and Neo4j graph writes. Backend restarts can "
            "strand in-memory batch runners unless durable recovery reclaims "
            "stale running items."
        ),
    },
    {
        "id": "sample_theory",
        "text": (
            "Carl Jung described archetypes as recurring psychic patterns. The "
            "shadow is not a literal person; it is a concept used to discuss "
            "disowned aspects of the psyche. Evidence-based extraction should "
            "avoid inventing relationships not present in the passage."
        ),
    },
    {
        "id": "sample_systems",
        "text": (
            "Qdrant stores child vectors for retrieval, while MongoDB stores raw "
            "artifacts and parent chunks. Neo4j stores evidence-backed graph "
            "facts. A local extractor must return JSON cleanly and include exact "
            "evidence spans for every entity, relationship, and fact."
        ),
    },
]


SYSTEM_PROMPT = """You are a strict information extraction engine.
Return exactly one JSON object. Do not return markdown, code fences, comments,
XML, YAML, JSONL, natural language, or reasoning traces. Do not include <think>.
Every evidence field must be an exact substring from the provided text.
"""


ENTITY_TYPES = {
    "Person",
    "Organization",
    "Location",
    "Event",
    "Concept",
    "Method",
    "Product",
    "Software",
    "Document",
    "Standard",
    "Rule",
    "Law",
    "Artifact",
    "TimeReference",
    "other",
}


RELATION_TYPES = {
    "uses",
    "stores",
    "part_of",
    "defines",
    "supports",
    "references",
    "causes",
    "related_to",
}


def build_user_prompt(text: str) -> str:
    return f"""Extract grounded information from the TEXT.

Return this exact JSON shape:
{{
  "chunk_summary": "one sentence grounded in the text",
  "entities": [
    {{"name": "string", "type": "Person|Organization|Location|Event|Concept|Method|Product|Software|Document|Standard|Rule|Law|Artifact|TimeReference|other", "evidence": "exact substring", "confidence": 0.0}}
  ],
  "relationships": [
    {{"source": "entity name", "relation": "uses|stores|part_of|defines|supports|references|causes|related_to", "target": "entity or literal", "evidence": "exact substring", "confidence": 0.0}}
  ],
  "graph_facts": [
    {{"subject": "entity name", "predicate": "short predicate", "object": "literal or entity", "evidence": "exact substring", "confidence": 0.0}}
  ],
  "warnings": ["string"]
}}

Rules:
- Keep arrays short: at most 5 entities, 5 relationships, 5 graph_facts.
- Use [] when no item is clearly supported.
- Do not invent facts outside the TEXT.
- Evidence must be copied exactly from the TEXT.

TEXT:
<<<
{text}
>>>
"""


@dataclass
class Result:
    sample_id: str
    ok: bool
    latency_s: float
    completion_tokens: int | None
    prompt_tokens: int | None
    decode_tok_s: float | None
    errors: list[str]
    raw: str


def _post_json(url: str, payload: dict[str, Any], api_key: str | None, timeout: float) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc


def _extract_json_object(raw: str) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    stripped = raw.strip()
    if "<think" in stripped.lower() or "</think" in stripped.lower():
        errors.append("think_leak")
    if stripped.startswith("```"):
        errors.append("code_fence")
    try:
        return json.loads(stripped), errors
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            errors.append("json_salvaged_from_wrapping_text")
            return json.loads(stripped[start : end + 1]), errors
        except json.JSONDecodeError as exc:
            errors.append(f"json_parse_error:{exc.msg}")
            return None, errors
    errors.append("json_parse_error:no_object")
    return None, errors


def _validate_contract(obj: dict[str, Any] | None, text: str) -> list[str]:
    if obj is None:
        return ["missing_json_object"]
    errors: list[str] = []
    for key in ("chunk_summary", "entities", "relationships", "graph_facts", "warnings"):
        if key not in obj:
            errors.append(f"missing_key:{key}")
    for key in ("entities", "relationships", "graph_facts", "warnings"):
        if key in obj and not isinstance(obj[key], list):
            errors.append(f"not_array:{key}")
    for key in ("entities", "relationships", "graph_facts"):
        for idx, item in enumerate(obj.get(key) or []):
            if not isinstance(item, dict):
                errors.append(f"{key}[{idx}]:not_object")
                continue
            if key == "entities" and item.get("type") not in ENTITY_TYPES:
                errors.append(f"{key}[{idx}]:bad_type")
            if key == "relationships" and item.get("relation") not in RELATION_TYPES:
                errors.append(f"{key}[{idx}]:bad_relation")
            evidence = item.get("evidence")
            if not evidence or not isinstance(evidence, str):
                errors.append(f"{key}[{idx}]:missing_evidence")
            elif evidence not in text:
                errors.append(f"{key}[{idx}]:evidence_not_substring")
            conf = item.get("confidence")
            if not isinstance(conf, (int, float)) or not 0 <= float(conf) <= 1:
                errors.append(f"{key}[{idx}]:bad_confidence")
    return errors


def run_one(args: argparse.Namespace, sample: dict[str, str]) -> Result:
    url = args.base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": args.model,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(sample["text"])},
        ],
    }
    if args.response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    started = time.perf_counter()
    try:
        body = _post_json(url, payload, args.api_key, args.timeout)
        latency = time.perf_counter() - started
    except Exception as exc:
        return Result(sample["id"], False, time.perf_counter() - started, None, None, None, [str(exc)], "")

    choices = body.get("choices") or []
    raw = ""
    if choices:
        raw = ((choices[0].get("message") or {}).get("content") or "").strip()
    usage = body.get("usage") or {}
    completion_tokens = usage.get("completion_tokens")
    prompt_tokens = usage.get("prompt_tokens")
    obj, parse_errors = _extract_json_object(raw)
    errors = parse_errors + _validate_contract(obj, sample["text"])
    decode_tok_s = None
    if isinstance(completion_tokens, int) and latency > 0:
        decode_tok_s = completion_tokens / latency
    return Result(
        sample["id"],
        not errors,
        latency,
        completion_tokens if isinstance(completion_tokens, int) else None,
        prompt_tokens if isinstance(prompt_tokens, int) else None,
        decode_tok_s,
        errors,
        raw,
    )


def load_samples(path: str | None) -> list[dict[str, str]]:
    if not path:
        return DEFAULT_SAMPLES
    samples: list[dict[str, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        samples.append({"id": str(obj.get("id") or len(samples)), "text": str(obj["text"])})
    return samples


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    idx = round((len(ordered) - 1) * pct)
    return ordered[idx]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8083/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key")
    parser.add_argument("--samples-jsonl")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=800)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--response-format", choices=["none", "json_object"], default="none")
    parser.add_argument("--out", default="/tmp/local_extractor_contract_results.json")
    args = parser.parse_args()

    base_samples = load_samples(args.samples_jsonl)
    samples = []
    for i in range(args.repeat):
        for sample in base_samples:
            samples.append({"id": f"{sample['id']}#{i + 1}", "text": sample["text"]})

    wall_started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda sample: run_one(args, sample), samples))
    wall_s = time.perf_counter() - wall_started

    latencies = [r.latency_s for r in results]
    decode_rates = [r.decode_tok_s for r in results if r.decode_tok_s is not None]
    pass_count = sum(1 for r in results if r.ok)
    total_completion_tokens = sum(r.completion_tokens or 0 for r in results)
    summary = {
        "model": args.model,
        "base_url": args.base_url,
        "concurrency": args.concurrency,
        "total": len(results),
        "pass": pass_count,
        "pass_rate": pass_count / len(results) if results else 0,
        "wall_s": wall_s,
        "chunks_per_hour": (len(results) / wall_s * 3600) if wall_s > 0 else None,
        "p50_latency_s": statistics.median(latencies) if latencies else None,
        "p95_latency_s": percentile(latencies, 0.95),
        "avg_decode_tok_s": (sum(decode_rates) / len(decode_rates)) if decode_rates else None,
        "total_completion_tokens": total_completion_tokens,
    }
    payload = {
        "summary": summary,
        "results": [
            {
                "sample_id": r.sample_id,
                "ok": r.ok,
                "latency_s": r.latency_s,
                "completion_tokens": r.completion_tokens,
                "prompt_tokens": r.prompt_tokens,
                "decode_tok_s": r.decode_tok_s,
                "errors": r.errors,
                "raw": r.raw,
            }
            for r in results
        ],
    }
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        tok_s = f"{r.decode_tok_s:.1f}" if r.decode_tok_s is not None else "-"
        print(f"{status} {r.sample_id:<24} latency={r.latency_s:6.2f}s out={r.completion_tokens} tok/s={tok_s} errors={','.join(r.errors) or '-'}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
