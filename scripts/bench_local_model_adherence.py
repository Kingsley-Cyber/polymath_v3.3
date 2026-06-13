#!/usr/bin/env python3
"""Benchmark local extraction-model adherence against fixed chunk samples.

This is intentionally outside the ingest pipeline. It probes an OpenAI-style
chat endpoint and checks whether a model can satisfy the Polymath Ghost B
object contract on real chunks without touching Mongo, Qdrant, or Neo4j.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from services.ghost_b_schemas import ExtractionResponse  # noqa: E402


ENTITY_TYPES = (
    "Person, Organization, Location, Event, Concept, Method, Product, "
    "Software, Document, Standard, Rule, Law, Artifact, TimeReference, other"
)

PREDICATES = (
    "part_of, member_of, located_in, works_for, created_by, owns, "
    "affiliated_with, synonym_of, instance_of, example_of, uses, references, "
    "implements, depends_on, produces, stores, detects, supports, defines, "
    "represents, maps_to, preceded_by, causes, overlaps, during, derived_from, "
    "contradicts, excepts, overrides, related_to"
)

FACT_TYPES = (
    "property, status, timestamp, quantity, threshold, category, tag, "
    "rule_condition, rule_action"
)

JUNK_RE = re.compile(
    r"(?:index_split|calibre|mbp_pagebreak|images?/|\.html|xlink:href|"
    r"mailto:|https?://|svg|titlepage\.xhtml)",
    re.I,
)
CANON_RE = re.compile(r"^[a-z0-9][a-z0-9 ]{0,199}$")
PLACEHOLDER_RE = re.compile(
    r"(?:verbatim phrase from text|exact phrase from text|canonical_name|"
    r"lowercase no punctuation|string|entity name|source phrase)",
    re.I,
)


def build_system_prompt(*, enable_facts: bool) -> str:
    facts_rule = (
        "Facts are enabled. Use at most 3 facts, but only when the fact is explicit "
        "and has an exact evidence_phrase."
        if enable_facts
        else 'Facts are disabled for this benchmark. Always return "facts": [].'
    )
    facts_shape = (
        """[
    {
      "subject": "canonical_name from entities",
      "fact_type": "property",
      "property_name": "snake_case",
      "value": "verbatim or normalized value from text",
      "unit": "",
      "condition": "",
      "confidence": 0.8,
      "evidence_phrase": "exact phrase from text"
    }
  ]"""
        if enable_facts
        else "[]"
    )
    return f"""You are a strict Polymath Ghost B extraction engine.
Return ONLY valid JSON with exactly these top-level keys: entities, relations, facts.
Do not output markdown, comments, code fences, XML, YAML, JSONL, prose, or reasoning.
The model must never output JSONL on this path. Return one JSON object only.
{facts_rule}

If the chunk is mostly cover metadata, SVG, images, Calibre markup, index entries,
links, pagebreaks, bibliography/navigation boilerplate, or formatting artifacts,
return exactly {{"entities":[],"relations":[],"facts":[]}}.

Do not extract file paths, html ids, image names, Calibre classes, anchors, page
numbers, formatting tokens, link destinations, or placeholder names.
Extract only information explicitly stated in the document text.
Prefer fewer correct items over broad coverage.
Every surface_form and evidence_phrase must be a meaningful exact substring from the text.
Use at most 5 entities and 4 relations.

Allowed entity_type values only:
{ENTITY_TYPES}

Allowed relation predicate values only:
{PREDICATES}

Allowed fact_type values only:
{FACT_TYPES}

JSON shape:
{{
  "entities": [
    {{
      "canonical_name": "lowercase no punctuation",
      "surface_form": "verbatim phrase from text",
      "entity_type": "Concept",
      "confidence": 0.9,
      "query_aliases": [],
      "definitional_phrase": "",
      "object_kind": ""
    }}
  ],
  "relations": [
    {{
      "subject": "canonical_name from entities",
      "predicate": "defines",
      "object": "canonical_name or literal",
      "object_kind": "entity",
      "confidence": 0.8,
      "evidence_phrase": "exact phrase from text",
      "relation_cue": ""
    }}
  ],
  "facts": {facts_shape}
}}
"""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _canonical_supported(canonical: str, surface: str) -> bool:
    canon_words = [w for w in _norm(canonical).split() if len(w) > 2]
    surface_norm = _norm(surface)
    if not canon_words:
        return True
    return any(word in surface_norm for word in canon_words)


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc


def _extract_json(raw: str) -> tuple[dict[str, Any] | None, list[str]]:
    text = str(raw or "").strip()
    errors: list[str] = []
    if not text:
        return None, ["empty_raw"]
    if "<think" in text.lower() or "</think" in text.lower():
        errors.append("think_leak")
    if text.startswith("```"):
        errors.append("code_fence")
    try:
        obj = json.loads(text)
        return (obj, errors) if isinstance(obj, dict) else (None, errors + ["not_object"])
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj, errors + ["json_salvaged"]
            return None, errors + ["not_object"]
        except json.JSONDecodeError as exc:
            return None, errors + [f"json_parse_error:{exc.msg}"]
    return None, errors + ["json_parse_error:no_object"]


def _empty_counts() -> dict[str, int]:
    return {"entities": 0, "relations": 0, "facts": 0}


def _empty_checks(*, pydantic_ok: bool = False) -> dict[str, int | bool]:
    return {
        "pydantic_ok": pydantic_ok,
        "evidence_errors": 0,
        "invalid_enum_errors": 0,
        "placeholder_errors": 0,
        "citation_pollution_errors": 0,
        "facts_disabled_errors": 0,
    }


def _contract_schema(*, enable_facts: bool) -> dict[str, Any]:
    schema = ExtractionResponse.model_json_schema()
    schema["required"] = ["entities", "relations", "facts"]
    if not enable_facts:
        facts_schema = schema.setdefault("properties", {}).setdefault("facts", {})
        facts_schema["maxItems"] = 0
    return schema


def _validate(
    obj: dict[str, Any] | None,
    text: str,
    *,
    enable_facts: bool,
) -> tuple[list[str], dict, dict, dict, dict]:
    raw_counts = _empty_counts()
    accepted = _empty_counts()
    dropped = _empty_counts()
    checks = _empty_checks()
    if obj is None:
        return ["missing_json_object"], raw_counts, accepted, dropped, checks

    errors: list[str] = []
    for key in raw_counts:
        if key not in obj:
            errors.append(f"missing_top_key:{key}")
        elif not isinstance(obj[key], list):
            errors.append(f"not_list:{key}")
        else:
            raw_counts[key] = len(obj[key])

    try:
        parsed = ExtractionResponse.model_validate(obj)
    except Exception as exc:
        pydantic_errors = getattr(exc, "errors", lambda: [])()
        if pydantic_errors:
            for item in pydantic_errors[:20]:
                loc = ".".join(str(part) for part in item.get("loc", ())) or "root"
                err_type = str(item.get("type") or "")
                msg = str(item.get("msg") or "")[:120]
                if "literal" in err_type or "Input should be" in msg:
                    checks["invalid_enum_errors"] += 1
                errors.append(f"pydantic:{loc}:{err_type}:{msg}")
        else:
            first_line = str(exc).splitlines()[0][:140]
            errors.append(f"pydantic:{type(exc).__name__}:{first_line}")
        return errors, raw_counts, accepted, dropped, checks
    checks["pydantic_ok"] = True

    if not enable_facts and parsed.facts:
        checks["facts_disabled_errors"] += len(parsed.facts)
        errors.append("facts_not_allowed")

    entity_names: set[str] = set()
    for idx, entity in enumerate(parsed.entities):
        ok = True
        if not CANON_RE.match(entity.canonical_name):
            errors.append(f"entity[{idx}].canonical_not_lower_no_punct")
            ok = False
        if not entity.surface_form or entity.surface_form not in text:
            errors.append(f"entity[{idx}].surface_not_substring")
            checks["evidence_errors"] += 1
            ok = False
        if entity.surface_form and JUNK_RE.search(entity.surface_form):
            errors.append(f"entity[{idx}].surface_is_markup")
            checks["citation_pollution_errors"] += 1
            ok = False
        if entity.surface_form and PLACEHOLDER_RE.search(entity.surface_form):
            errors.append(f"entity[{idx}].surface_is_placeholder")
            checks["placeholder_errors"] += 1
            ok = False
        if entity.surface_form and not _canonical_supported(entity.canonical_name, entity.surface_form):
            errors.append(f"entity[{idx}].canonical_not_supported_by_surface")
            ok = False
        if ok:
            accepted["entities"] += 1
            entity_names.add(entity.canonical_name)
        else:
            dropped["entities"] += 1

    for idx, relation in enumerate(parsed.relations):
        ok = True
        if relation.subject not in entity_names:
            errors.append(f"relation[{idx}].subject_not_entity")
            ok = False
        if relation.object_kind == "entity" and relation.object not in entity_names:
            errors.append(f"relation[{idx}].object_not_entity")
            ok = False
        if not relation.evidence_phrase or relation.evidence_phrase not in text:
            errors.append(f"relation[{idx}].evidence_not_substring")
            checks["evidence_errors"] += 1
            ok = False
        if relation.evidence_phrase and JUNK_RE.search(relation.evidence_phrase):
            errors.append(f"relation[{idx}].evidence_is_markup")
            checks["citation_pollution_errors"] += 1
            ok = False
        if relation.evidence_phrase and PLACEHOLDER_RE.search(relation.evidence_phrase):
            errors.append(f"relation[{idx}].evidence_is_placeholder")
            checks["placeholder_errors"] += 1
            ok = False
        if ok:
            accepted["relations"] += 1
        else:
            dropped["relations"] += 1

    for idx, fact in enumerate(parsed.facts):
        ok = True
        if fact.subject not in entity_names:
            errors.append(f"fact[{idx}].subject_not_entity")
            ok = False
        if not fact.evidence_phrase or fact.evidence_phrase not in text:
            errors.append(f"fact[{idx}].evidence_not_substring")
            checks["evidence_errors"] += 1
            ok = False
        if fact.evidence_phrase and JUNK_RE.search(fact.evidence_phrase):
            errors.append(f"fact[{idx}].evidence_is_markup")
            checks["citation_pollution_errors"] += 1
            ok = False
        if fact.evidence_phrase and PLACEHOLDER_RE.search(fact.evidence_phrase):
            errors.append(f"fact[{idx}].evidence_is_placeholder")
            checks["placeholder_errors"] += 1
            ok = False
        if ok:
            accepted["facts"] += 1
        else:
            dropped["facts"] += 1

    return errors, raw_counts, accepted, dropped, checks


def _is_router_skip(meta: dict[str, Any]) -> bool:
    return int(meta.get("noise") or 0) >= 90 or float(meta.get("alpha") or 1.0) < 0.6


def run_one(args: argparse.Namespace, sample: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    text = str(sample["text"])
    router_skip = _is_router_skip(meta)
    if router_skip and not args.no_router_skip:
        return {
            "id": sample["id"],
            **meta,
            "router_should_skip": True,
            "router_skipped": True,
            "model_called": False,
            "valid_json": True,
            "latency_s": 0.0,
            "finish_reason": "router_skip",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "completion_tok_s": None,
            "raw_counts": _empty_counts(),
            "accepted": _empty_counts(),
            "dropped": _empty_counts(),
            "checks": _empty_checks(pydantic_ok=True),
            "contract_pass": True,
            "production_useful": True,
            "errors": [],
            "raw_first_500": "",
            "raw_last_300": "",
        }

    payload: dict[str, Any] = {
        "model": args.model,
        "temperature": 0,
        "max_tokens": args.max_tokens,
        "messages": [
            {"role": "system", "content": build_system_prompt(enable_facts=args.enable_facts)},
            {"role": "user", "content": "Document text:\n" + text},
        ],
    }
    if args.response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    elif args.response_format == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "polymath_extraction",
                "strict": True,
                "schema": _contract_schema(enable_facts=args.enable_facts),
            },
        }

    started = time.perf_counter()
    try:
        body = _post_json(args.base_url.rstrip("/") + "/chat/completions", payload, args.timeout)
        latency_s = time.perf_counter() - started
        choice = (body.get("choices") or [{}])[0]
        raw = ((choice.get("message") or {}).get("content") or "").strip()
        finish = choice.get("finish_reason")
        usage = body.get("usage") or {}
    except Exception as exc:
        return {
            "id": sample["id"],
            **meta,
            "router_should_skip": router_skip,
            "router_skipped": False,
            "model_called": True,
            "valid_json": False,
            "latency_s": time.perf_counter() - started,
            "raw_counts": _empty_counts(),
            "accepted": _empty_counts(),
            "dropped": _empty_counts(),
            "checks": _empty_checks(),
            "contract_pass": False,
            "production_useful": False,
            "errors": [str(exc)],
        }

    obj, parse_errors = _extract_json(raw)
    valid_json = bool(obj is not None and not parse_errors)
    validation_errors, raw_counts, accepted, dropped, checks = _validate(
        obj,
        text,
        enable_facts=args.enable_facts,
    )
    errors = parse_errors + validation_errors
    if finish == "length":
        errors.append("truncated")

    emitted_total = accepted["entities"] + accepted["relations"] + accepted["facts"]
    raw_emitted_total = raw_counts["entities"] + raw_counts["relations"] + raw_counts["facts"]
    contract_pass = not errors
    production_useful = bool(
        contract_pass
        and (
            (router_skip and raw_emitted_total == 0)
            or (not router_skip and accepted["entities"] >= 1)
        )
    )

    completion_tokens = usage.get("completion_tokens")
    completion_tok_s = None
    if isinstance(completion_tokens, int) and latency_s > 0:
        completion_tok_s = completion_tokens / latency_s

    return {
        "id": sample["id"],
        **meta,
        "router_should_skip": router_skip,
        "router_skipped": False,
        "model_called": True,
        "valid_json": valid_json,
        "latency_s": latency_s,
        "finish_reason": finish,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": completion_tokens,
        "total_tokens": usage.get("total_tokens"),
        "completion_tok_s": completion_tok_s,
        "raw_counts": raw_counts,
        "accepted": accepted,
        "dropped": dropped,
        "checks": checks,
        "contract_pass": contract_pass,
        "production_useful": production_useful,
        "errors": errors[:40],
        "raw_first_500": raw[:500],
        "raw_last_300": raw[-300:] if len(raw) > 300 else "",
    }


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * pct))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8094/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--samples", default="/tmp/zto_chunk_samples_balanced.jsonl")
    parser.add_argument("--meta", default="/tmp/zto_chunk_samples_meta.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument(
        "--response-format",
        choices=["none", "json_object", "json_schema"],
        default="none",
    )
    parser.add_argument(
        "--enable-facts",
        action="store_true",
        help="Allow fact extraction. Defaults off so facts must be [].",
    )
    parser.add_argument(
        "--no-router-skip",
        action="store_true",
        help="Send noisy/front-matter chunks to the model instead of pre-skipping them.",
    )
    args = parser.parse_args()

    samples = [
        json.loads(line)
        for line in Path(args.samples).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta_by_id = {
        str(item["id"]): item
        for item in json.loads(Path(args.meta).read_text(encoding="utf-8"))
    }

    wall_started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(
            pool.map(lambda sample: run_one(args, sample, meta_by_id.get(sample["id"], {})), samples)
        )
    wall_s = time.perf_counter() - wall_started

    model_results = [r for r in results if r.get("model_called")]
    model_called = len(model_results)
    latencies = [
        float(r["latency_s"]) for r in model_results if r.get("latency_s") is not None
    ]
    rates = [float(r["completion_tok_s"]) for r in model_results if r.get("completion_tok_s")]
    router_should_skip = [r for r in results if r.get("router_should_skip")]
    valid_json_count = sum(1 for r in model_results if r.get("valid_json"))
    pydantic_pass_count = sum(
        1 for r in model_results if (r.get("checks") or {}).get("pydantic_ok")
    )
    evidence_pass_count = sum(
        1
        for r in model_results
        if (r.get("checks") or {}).get("pydantic_ok")
        and int((r.get("checks") or {}).get("evidence_errors") or 0) == 0
    )
    invalid_enum_count = sum(
        int((r.get("checks") or {}).get("invalid_enum_errors") or 0) for r in model_results
    )
    placeholder_field_count = sum(
        int((r.get("checks") or {}).get("placeholder_errors") or 0) for r in model_results
    )
    citation_pollution_count = sum(
        int((r.get("checks") or {}).get("citation_pollution_errors") or 0)
        for r in model_results
    )
    facts_disabled_count = sum(
        int((r.get("checks") or {}).get("facts_disabled_errors") or 0) for r in model_results
    )
    truncated_json_count = sum(
        1
        for r in model_results
        if r.get("finish_reason") == "length" or "truncated" in (r.get("errors") or [])
    )
    empty_chunk_false_positive_count = sum(
        1
        for r in router_should_skip
        if sum(int((r.get("raw_counts") or {}).get(key) or 0) for key in ("entities", "relations", "facts"))
        > 0
    )
    denominator = model_called or 1
    valid_json_rate = valid_json_count / denominator
    pydantic_pass_rate = pydantic_pass_count / denominator
    evidence_pass_rate = evidence_pass_count / denominator
    empty_denominator = len(router_should_skip) or 1
    empty_chunk_false_positive_rate = empty_chunk_false_positive_count / empty_denominator
    gate_failures: list[str] = []
    if model_called == 0:
        gate_failures.append("no_model_calls")
    if valid_json_rate < 1.0:
        gate_failures.append("valid_json_rate_below_100")
    if pydantic_pass_rate < 0.98:
        gate_failures.append("pydantic_pass_rate_below_98")
    if evidence_pass_rate < 0.95:
        gate_failures.append("evidence_pass_rate_below_95")
    if invalid_enum_count:
        gate_failures.append("invalid_enum_count_nonzero")
    if placeholder_field_count:
        gate_failures.append("placeholder_field_count_nonzero")
    if citation_pollution_count:
        gate_failures.append("citation_pollution_count_nonzero")
    if facts_disabled_count:
        gate_failures.append("facts_disabled_count_nonzero")
    if empty_chunk_false_positive_rate > 0.05:
        gate_failures.append("empty_chunk_false_positive_rate_above_5")
    if truncated_json_count:
        gate_failures.append("truncated_json_count_nonzero")

    summary = {
        "model": args.model,
        "base_url": args.base_url,
        "samples": len(results),
        "model_called": model_called,
        "router_skipped": sum(1 for r in results if r.get("router_skipped")),
        "concurrency": args.concurrency,
        "max_tokens": args.max_tokens,
        "facts_enabled": args.enable_facts,
        "response_format": args.response_format,
        "wall_s": wall_s,
        "chunks_per_hour_wall": (len(results) / wall_s * 3600) if wall_s else None,
        "contract_pass": sum(1 for r in results if r.get("contract_pass")),
        "production_useful": sum(1 for r in results if r.get("production_useful")),
        "truncated_json_count": truncated_json_count,
        "router_should_skip": len(router_should_skip),
        "valid_json_rate": valid_json_rate,
        "pydantic_pass_rate": pydantic_pass_rate,
        "evidence_pass_rate": evidence_pass_rate,
        "invalid_enum_count": invalid_enum_count,
        "placeholder_field_count": placeholder_field_count,
        "citation_pollution_count": citation_pollution_count,
        "facts_disabled_count": facts_disabled_count,
        "empty_chunk_false_positive_count": empty_chunk_false_positive_count,
        "empty_chunk_false_positive_rate": empty_chunk_false_positive_rate,
        "eligible_for_ghost_b": not gate_failures,
        "gate_failures": gate_failures,
        "latency_p50_s": statistics.median(latencies) if latencies else None,
        "latency_p95_s": percentile(latencies, 0.95),
        "tok_s_median": statistics.median(rates) if rates else None,
        "tok_s_min": min(rates) if rates else None,
        "tok_s_max": max(rates) if rates else None,
        "accepted_totals": {
            "entities": sum(int(r.get("accepted", {}).get("entities") or 0) for r in results),
            "relations": sum(int(r.get("accepted", {}).get("relations") or 0) for r in results),
            "facts": sum(int(r.get("accepted", {}).get("facts") or 0) for r in results),
        },
    }

    Path(args.out).write_text(
        json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("SUMMARY", json.dumps(summary, indent=2))
    print(
        f"{'id':<24} {'tok':>5} {'noise':>5} {'skip':>4} {'sec':>6} {'out':>5} "
        f"{'tok/s':>7} {'raw':>9} {'acc':>9} {'json':>5} {'pass':>5} {'use':>5} errors"
    )
    for r in results:
        rate = r.get("completion_tok_s")
        raw = r.get("raw_counts") or {}
        accepted = r.get("accepted") or {}
        print(
            f"{r['id']:<24} {str(r.get('tokens', '?')):>5} "
            f"{str(r.get('noise', '?')):>5} {str(bool(r.get('router_skipped'))):>4} "
            f"{float(r.get('latency_s') or 0):>6.2f} "
            f"{str(r.get('completion_tokens', '?')):>5} "
            f"{(f'{rate:.1f}' if rate else '-'):>7} "
            f"{raw.get('entities', 0)}/{raw.get('relations', 0)}/{raw.get('facts', 0):<3} "
            f"{accepted.get('entities', 0)}/{accepted.get('relations', 0)}/{accepted.get('facts', 0):<3} "
            f"{str(r.get('valid_json')):>5} {str(r.get('contract_pass')):>5} "
            f"{str(r.get('production_useful')):>5} "
            f"{','.join((r.get('errors') or [])[:4])}"
        )
    print("wrote", args.out)


if __name__ == "__main__":
    main()
