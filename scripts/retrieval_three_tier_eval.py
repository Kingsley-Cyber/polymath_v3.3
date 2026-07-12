#!/usr/bin/env python3
"""Run the real /api/chat path across all three UI retrieval routes.

The output is a route-comparable E2E report: timings, source summaries, anchor
coverage, trace diagnostics, and Graph Advantage checks. Full source text is
excluded by default so reports can be committed/shared without leaking corpus
chunks.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
VALIDATION_PATH = BACKEND / "services" / "retriever" / "three_tier_eval.py"
_spec = importlib.util.spec_from_file_location(
    "three_tier_eval_core",
    VALIDATION_PATH,
)
if _spec is None or _spec.loader is None:
    raise SystemExit(f"Could not load validation module at {VALIDATION_PATH}")
_validation = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _validation
_spec.loader.exec_module(_validation)

ROUTES = _validation.ROUTES
ROUTE_LATENCY_BUDGETS = _validation.ROUTE_LATENCY_BUDGETS
evaluate_route_result = _validation.evaluate_route_result
summarize_report = _validation.summarize_report

DEFAULT_QUERY_SET = ROOT / "scripts" / "retrieval_three_tier_queries.json"
DEFAULT_CORPUS_ID = "f8a0aa85-6cb4-4f64-a973-f9183f1546bb"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _post_json(base_url: str, path: str, body: dict[str, Any], token: str | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("PROBE_TOKEN")
    if token:
        return token
    username = args.username or os.environ.get("DEFAULT_ADMIN_USERNAME") or "admin"
    password = args.password or os.environ.get("DEFAULT_ADMIN_PASSWORD")
    if not password:
        raise SystemExit(
            "No token supplied. Set PROBE_TOKEN or DEFAULT_ADMIN_PASSWORD, "
            "or pass --token/--password."
        )
    login = _post_json(
        args.base_url,
        "/api/auth/login",
        {"username": username, "password": password},
    )
    token = login.get("access_token")
    if not token:
        raise SystemExit("Login response did not include access_token.")
    return token


def parse_sse_line(line: str, current_event: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if line.startswith("event:"):
        return line.split(":", 1)[1].strip(), None
    if not line.startswith("data:"):
        return current_event, None
    data = line[5:].strip()
    if not data:
        return current_event, None
    try:
        return current_event, json.loads(data)
    except json.JSONDecodeError:
        return current_event, None


def run_chat_case(
    *,
    base_url: str,
    token: str,
    corpus_ids: list[str],
    route: dict[str, str],
    query_case: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "hyde_enabled": False,
        "temperature": 0,
        "max_tokens": args.max_answer_tokens,
        "final_top_k": args.final_top_k,
    }
    if args.disable_rerank:
        overrides["rerank_enabled"] = False
    if args.model:
        overrides["model"] = args.model
    if args.query_profile:
        overrides["query_profile"] = args.query_profile

    payload = {
        "message": query_case["query"],
        "corpus_ids": corpus_ids,
        "retrieval_tier": route["tier"],
        "overrides": overrides,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip()}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    started = time.perf_counter()
    current_event: str | None = None
    answer_parts: list[str] = []
    thinking_chars = 0
    token_events = 0
    sources: list[dict[str, Any]] = []
    trace_events: list[dict[str, Any]] = []
    error_events: list[str] = []
    done_obj: dict[str, Any] = {}
    marks: dict[str, float] = {}

    def mark(name: str) -> None:
        marks.setdefault(name, time.perf_counter() - started)

    try:
        with urllib.request.urlopen(req, timeout=args.http_timeout_s) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                current_event, obj = parse_sse_line(line, current_event)
                if obj is None:
                    continue
                event_type = obj.get("type") or current_event
                if event_type == "trace_event" or obj.get("trace_event"):
                    event = obj.get("trace_event") or obj
                    event["_at_s"] = round(time.perf_counter() - started, 3)
                    trace_events.append(event)
                elif event_type == "sources":
                    mark("retrieval_done_sources")
                    raw_sources = obj.get("sources") or obj.get("data") or []
                    sources = raw_sources if isinstance(raw_sources, list) else []
                    if args.stop_after_sources:
                        break
                elif event_type == "thinking":
                    mark("first_thinking")
                    thinking_chars += len(str(obj.get("thinking") or ""))
                    marks["last_thinking"] = time.perf_counter() - started
                elif event_type == "token":
                    content = str(obj.get("content") or "")
                    if content:
                        mark("first_answer_token")
                        token_events += 1
                        answer_parts.append(content)
                elif event_type == "error":
                    error_events.append(str(obj.get("content") or obj)[:500])
                elif event_type == "done":
                    mark("done")
                    done_obj = obj
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:1000]
        error_events.append(f"HTTP {exc.code}: {body}")
    except Exception as exc:  # noqa: BLE001 - CLI report should capture failures.
        error_events.append(f"{type(exc).__name__}: {exc}")

    total = time.perf_counter() - started
    answer = "".join(answer_parts)
    sources_at = marks.get("retrieval_done_sources")
    first_answer = marks.get("first_answer_token")
    timings = {
        "total": round(total, 3),
        "retrieval_done_sources": round(sources_at, 3) if sources_at is not None else None,
        "first_answer_token": round(first_answer, 3) if first_answer is not None else None,
        "done": round(marks.get("done", total), 3),
        "generation_after_sources": (
            round((marks.get("done", total) - sources_at), 3)
            if sources_at is not None and not args.stop_after_sources
            else None
        ),
    }
    raw_result = {
        "query_id": query_case.get("id"),
        "query": query_case.get("query"),
        "route": route["ui_name"],
        "tier": route["tier"],
        "answer": answer,
        "answer_chars": len(answer),
        "answer_excerpt": answer[: args.answer_excerpt_chars],
        "thinking_chars": thinking_chars,
        "token_events": token_events,
        "sources": sources,
        "trace_events": trace_events,
        "error_events": error_events,
        "done": done_obj,
        "timings_s": timings,
        "stop_after_sources": bool(args.stop_after_sources),
    }
    validation = evaluate_route_result(
        query_case=query_case,
        route_name=route["ui_name"],
        result=raw_result,
        max_total_s=args.max_total_s,
        max_retrieval_s=args.max_retrieval_s,
        max_generation_s=args.max_generation_s,
        fail_on_total_budget=args.fail_total_budget,
        fail_on_generation_budget=args.fail_generation_budget,
    )

    public_result = {
        "query_id": query_case.get("id"),
        "query": query_case.get("query"),
        "route": route["ui_name"],
        "tier": route["tier"],
        "status": validation["status"],
        "answer_chars": len(answer),
        "answer_excerpt": answer[: args.answer_excerpt_chars],
        "thinking_chars": thinking_chars,
        "token_events": token_events,
        "timings_s": timings,
        "source_summary": validation["source_summary"],
        "source_anchor_coverage": validation["source_anchor_coverage"],
        "answer_anchor_coverage": validation["answer_anchor_coverage"],
        "grounding_quality": validation["grounding_quality"],
        "corpus_ids": corpus_ids,
        "trace_summary": {
            "trace_titles": validation["trace_summary"]["trace_titles"],
            "effective_tier": validation["trace_summary"]["effective_tier"],
            "has_graph_advantage": validation["trace_summary"]["has_graph_advantage"],
            "local_rag_duration_s": validation["trace_summary"]["local_rag_duration_s"],
            "graph_advantage": validation["trace_summary"]["graph_advantage"],
            "retrieval_diagnostics": validation["trace_summary"][
                "retrieval_diagnostics"
            ],
        },
        "issues": validation["issues"],
        "validation": validation,
    }
    if args.include_source_text:
        public_result["sources"] = sources
    return public_result


def select_queries(query_set: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    queries = list(query_set.get("queries") or [])
    if args.query_id:
        wanted = set(args.query_id)
        queries = [case for case in queries if case.get("id") in wanted]
    if args.max_queries:
        queries = queries[: args.max_queries]
    if not queries:
        raise SystemExit("No query cases selected.")
    return queries


def select_routes(args: argparse.Namespace) -> list[dict[str, str]]:
    if not args.route:
        return list(ROUTES)
    normalized = {value.casefold().replace("_", " ").strip() for value in args.route}
    selected = []
    for route in ROUTES:
        names = {
            route["ui_name"].casefold(),
            route["tier"].casefold(),
            route["tier"].casefold().replace("_", " "),
        }
        if normalized & names:
            selected.append(route)
    if not selected:
        raise SystemExit(f"No routes matched {args.route!r}.")
    return selected


def build_report(
    *,
    query_set: dict[str, Any],
    queries: list[dict[str, Any]],
    routes: list[dict[str, str]],
    results: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    failures = [
        {
            "query_id": result["query_id"],
            "route": result["route"],
            "code": issue["code"],
            "message": issue["message"],
        }
        for result in results
        for issue in result.get("issues", [])
        if issue.get("level") == "fail"
    ]
    warnings = [
        {
            "query_id": result["query_id"],
            "route": result["route"],
            "code": issue["code"],
            "message": issue["message"],
        }
        for result in results
        for issue in result.get("issues", [])
        if issue.get("level") == "warn"
    ]
    return {
        "status": "fail" if failures or (args.strict and warnings) else "pass",
        "created_at_unix": int(time.time()),
        "contract": {
            "routes": list(ROUTES),
            "latency_budget_s": {
                "route_defaults": ROUTE_LATENCY_BUDGETS,
                "cli_overrides": {
                    "total": args.max_total_s,
                    "retrieval_or_sources": args.max_retrieval_s,
                    "generation_after_sources": args.max_generation_s,
                    "fail_total_budget": args.fail_total_budget,
                    "fail_generation_budget": args.fail_generation_budget,
                    "rerank_enabled": not args.disable_rerank,
                },
            },
            "metrics_positioning": {
                "live_e2e": [
                    "route latency",
                    "p50/p95 route latency",
                    "source hydration",
                    "anchor coverage",
                    "trace diagnostics",
                    "Graph Advantage",
                    "MRR@5",
                    "MAP@20",
                    "Recall@20",
                    "NDCG@8",
                    "context precision",
                    "corpus representation",
                    "answer sufficiency",
                ],
                "note": (
                    "Ranking metrics are computed from the checked-in labeled "
                    "document relevance judgments against real live retrieval."
                ),
            },
            "github_reference_stack": query_set.get("github_reference_stack", []),
        },
        "corpus_ids": args.corpus_id,
        "query_count": len(queries),
        "route_count": len(routes),
        "case_count": len(results),
        "query_ids": [case.get("id") for case in queries],
        "route_summaries": summarize_report(results),
        "failures": failures,
        "warnings": warnings,
        "results": results,
    }


def print_table(report: dict[str, Any]) -> None:
    print("\n===== THREE-TIER RETRIEVAL E2E =====")
    print(f"status={report['status']} cases={report['case_count']}")
    for route, summary in report["route_summaries"].items():
        print(
            f"{route:19} cases={summary['cases']} fail={summary['failures']} "
            f"warn={summary['warnings']} avg_total={summary['avg_total_s']:.2f}s "
            f"p50={summary['p50_total_s']:.2f}s "
            f"p95={summary['p95_total_s']:.2f}s "
            f"avg_sources={summary['avg_retrieval_or_sources_s']:.2f}s "
            f"avg_gen={summary['avg_generation_after_sources_s']:.2f}s"
        )
    print("\n-- cases --")
    for result in report["results"]:
        validation = result["validation"]
        src = validation["source_summary"]
        graph = validation["trace_summary"].get("graph_advantage") or {}
        print(
            f"{result['query_id']:28} | {result['route']:19} | "
            f"{result['status']:4} | total={result['timings_s']['total']:.2f}s "
            f"src_at={validation['timings_s']['retrieval_or_sources']:.2f}s "
            f"sources={src['source_count']} docs={src['unique_doc_count']} "
            f"src_cov={validation['source_anchor_coverage']['required_coverage']:.2f} "
            f"ans_cov={validation['answer_anchor_coverage']['required_coverage']:.2f} "
            f"lane_cov={validation['grounding_quality']['required_lane_coverage']:.2f} "
            f"ctx_precision={validation['grounding_quality']['context_precision']:.2f} "
            f"facts={int(graph.get('facts_used') or 0)} "
            f"rels={int(graph.get('relations_used') or 0)}"
        )
        for issue in result.get("issues", []):
            print(f"    {issue['level'].upper()}: {issue['code']} - {issue['message']}")
    if report["failures"]:
        print("\n-- failures --")
        for failure in report["failures"]:
            print(
                f"{failure['query_id']} / {failure['route']}: "
                f"{failure['code']} - {failure['message']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run all three UI retrieval routes over a deterministic query set."
    )
    parser.add_argument("--base-url", default=os.environ.get("PROBE_BASE", "http://localhost:8000"))
    parser.add_argument("--token", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument(
        "--corpus-id",
        action="append",
        default=None,
        help="Corpus id to query. May be repeated. Defaults to PROBE_CORPUS or the local dev corpus.",
    )
    parser.add_argument("--query-set", type=Path, default=DEFAULT_QUERY_SET)
    parser.add_argument("--query-id", action="append", default=None)
    parser.add_argument("--max-queries", type=int, default=0)
    parser.add_argument("--route", action="append", default=None)
    parser.add_argument("--model", default="")
    parser.add_argument("--query-profile", default="balanced")
    parser.add_argument(
        "--disable-rerank",
        action="store_true",
        help="Run a controlled E2E ablation with cross-encoder reranking disabled.",
    )
    parser.add_argument("--max-answer-tokens", type=int, default=512)
    parser.add_argument("--final-top-k", type=int, default=8)
    parser.add_argument(
        "--max-total-s",
        type=float,
        default=None,
        help=(
            "Override route-specific total warning budget. Defaults: Fast 20s, "
            "Hybrid 20s, Graph 25s. Warning unless --fail-total-budget is set."
        ),
    )
    parser.add_argument(
        "--max-retrieval-s",
        type=float,
        default=None,
        help=(
            "Override route-specific hard retrieval/source budget. Defaults: "
            "Fast 2s, Hybrid 8s, Graph 10s."
        ),
    )
    parser.add_argument(
        "--max-generation-s",
        type=float,
        default=None,
        help=(
            "Override route-specific generation-after-sources warning budget. "
            "Defaults: Fast 14s, Hybrid 14s, Graph 16s."
        ),
    )
    parser.add_argument(
        "--fail-total-budget",
        action="store_true",
        help="Make total end-to-end budget violations fail instead of warn.",
    )
    parser.add_argument(
        "--fail-generation-budget",
        action="store_true",
        help="Make model generation budget violations fail instead of warn.",
    )
    parser.add_argument("--http-timeout-s", type=float, default=600.0)
    parser.add_argument("--answer-excerpt-chars", type=int, default=420)
    parser.add_argument("--stop-after-sources", action="store_true")
    parser.add_argument("--include-source-text", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--assert", dest="assert_mode", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    query_set = _load_json(args.query_set)
    queries = select_queries(query_set, args)
    routes = select_routes(args)
    corpus_ids = args.corpus_id or [
        os.environ.get("PROBE_CORPUS") or DEFAULT_CORPUS_ID
    ]
    args.corpus_id = corpus_ids
    token = resolve_token(args)

    print(
        f"Running {len(queries)} queries x {len(routes)} routes "
        f"against {args.base_url} corpus_ids={corpus_ids}"
    )
    results: list[dict[str, Any]] = []
    for query_case in queries:
        case_corpus_ids = [
            str(value)
            for value in (query_case.get("corpus_ids") or corpus_ids)
            if str(value)
        ]
        for route in routes:
            print(f"→ {route['ui_name']}: {query_case['id']} :: {query_case['query']}")
            result = run_chat_case(
                base_url=args.base_url,
                token=token,
                corpus_ids=case_corpus_ids,
                route=route,
                query_case=query_case,
                args=args,
            )
            results.append(result)
            print(
                f"  {result['status']} total={result['timings_s']['total']:.2f}s "
                f"sources={result['source_summary']['source_count']} "
                f"warnings={sum(1 for i in result['issues'] if i['level'] == 'warn')} "
                f"failures={sum(1 for i in result['issues'] if i['level'] == 'fail')}"
            )

    report = build_report(
        query_set=query_set,
        queries=queries,
        routes=routes,
        results=results,
        args=args,
    )
    print_table(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2 if args.pretty else None, sort_keys=True),
            encoding="utf-8",
        )
        print(f"\nWrote report: {args.output}")
    elif args.pretty:
        print(json.dumps(report, indent=2, sort_keys=True))

    if args.assert_mode and report["status"] != "pass":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
