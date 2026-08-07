"""q8 (owner directive 2026-08-04) — canary parity harness.

Runs the SAME frozen queries through the exact API route the frontend
dropdown uses (POST /api/chat) against BOTH layouts:

* legacy  — no header: reads corpus_{cid8}_naive / _hrag / _graph
* shadow  — X-Polymath-Q8-Shadow-Read header: reads corpus_{cid8}_evidence
            with route-eligibility filters

Routes map to retrieval tiers:
    focused      -> qdrant_only          (old: naive)
    hierarchical -> qdrant_mongo         (old: hrag summaries + naive children)
    graph        -> qdrant_mongo_graph   (old: graph seeds + naive)

For every (route, layout) pair it measures wall latency and harvests the
final evidence bundle (chunk_id / doc_id / parent_id) out of the SSE
stream, then reports:

* candidate_set_drift per route — membership symmetric difference;
* route_leakage — shadow ids absent from the legacy membership;
* exact doc_id / chunk_id preservation per query;
* parent hydration key preservation (parent_id set equality);
* p50 / p95 latency per route per layout.

Read-only against production: it only issues chat requests on the canary
corpus; no writes, no topology changes.

Usage:
  python -m scripts.run_q8_parity_harness --token-file /tmp/pm_token.txt \
      [--api-base http://localhost:8000] [--runs 1] \
      [--output /Users/king/polymath_v3.3/data_eval/q8_parity_report.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from typing import Any

import requests

CANARY_CORPUS = "c6518e7b-1327-4694-85c8-08a81542e425"

ROUTES: dict[str, str] = {
    "focused": "qdrant_only",
    "hierarchical": "qdrant_mongo",
    "graph": "qdrant_mongo_graph",
}

# Frozen query set (owner proof requirement: same queries on both layouts).
FROZEN_QUERIES: list[str] = [
    "How does Benesh notation represent movement over time?",
    "What are the basic symbols of Benesh movement notation?",
    "How is a score page structured in Benesh notation?",
    "What is the relationship between Labanotation and Benesh notation?",
    "Which body parts does the notation track on the staff?",
]


def _harvest_sources(obj: Any, found: dict[str, dict]) -> None:
    """Collect chunk_id/doc_id/parent_id from any SSE payload carrying a
    `sources` list (sources events, answer_status payloads, done events)."""
    if isinstance(obj, dict):
        sources = obj.get("sources")
        if isinstance(sources, list):
            for entry in sources:
                if not isinstance(entry, dict):
                    continue
                chunk_id = entry.get("chunk_id") or entry.get("id")
                if not chunk_id or chunk_id in found:
                    continue
                found[str(chunk_id)] = {
                    "chunk_id": str(chunk_id),
                    "doc_id": entry.get("doc_id") or "",
                    "parent_id": entry.get("parent_id") or "",
                }
        for value in obj.values():
            if isinstance(value, (dict, list)):
                _harvest_sources(value, found)
    elif isinstance(obj, list):
        for value in obj:
            _harvest_sources(value, found)


def _parse_sse_sources(text: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except Exception:
            continue
        _harvest_sources(payload, found)
    return found


def run_one(
    api_base: str,
    token: str,
    query: str,
    tier: str,
    *,
    shadow: bool,
) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if shadow:
        headers["X-Polymath-Q8-Shadow-Read"] = CANARY_CORPUS
    body = {
        "message": query,
        "corpus_ids": [CANARY_CORPUS],
        "retrieval_tier": tier,
        "conversation_id": None,
    }
    started = time.perf_counter()
    resp = requests.post(
        f"{api_base}/api/chat",
        headers=headers,
        json=body,
        stream=True,
        timeout=600,
    )
    resp.raise_for_status()
    text = resp.text  # drains the whole SSE stream
    elapsed = time.perf_counter() - started
    sources = _parse_sse_sources(text)
    return {
        "status_code": resp.status_code,
        "latency_seconds": round(elapsed, 4),
        "source_count": len(sources),
        "sources": sources,
    }


def _pctl(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return round(ordered[index], 4)


# ── stage 2: deterministic universe parity (no top-k cutoff, no ties) ───────


def _scroll_universe(collection: str, payload_filter: dict) -> dict[str, dict]:
    """Scroll EVERY point matching the route filter — the full candidate
    universe the route can see (no cutoff, therefore no tie noise)."""
    base = "http://localhost:6333"
    out: dict[str, dict] = {}
    offset = None
    while True:
        body = {
            "filter": payload_filter,
            "limit": 256,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        resp = requests.post(
            f"{base}/collections/{collection}/points/scroll",
            json=body,
            timeout=60,
        ).json()["result"]
        for point in resp.get("points", []):
            payload = point.get("payload") or {}
            out[str(point["id"])] = {
                "chunk_id": payload.get("chunk_id"),
                "doc_id": payload.get("doc_id"),
                "parent_id": payload.get("parent_id"),
                "source_tier": payload.get("source_tier"),
                "chunk_kind": payload.get("chunk_kind"),
                "text_len": payload.get("text_len"),
                "text_hash": payload.get("text_hash"),
            }
        offset = resp.get("next_page_offset")
        if offset is None:
            break
    return out


def universe_parity(cid8: str) -> dict:
    """Owner proof per route: old collection universe == evidence universe
    under the route's eligibility filter. Deterministic — drift must be
    exactly 0."""
    child = {"must": [{"key": "chunk_type", "match": {"value": "child"}}]}
    summary = {"must": [{"key": "chunk_type", "match": {"value": "summary"}}]}

    def flagged(flag: str, base: dict) -> dict:
        return {
            "must": base["must"]
            + [{"key": flag, "match": {"value": True}}]
        }

    evidence = f"corpus_{cid8}_evidence"
    routes = {
        "focused": (
            f"corpus_{cid8}_naive",
            child,
            evidence,
            flagged("eligible_focused", child),
        ),
        "hierarchical": (
            f"corpus_{cid8}_hrag",
            summary,
            evidence,
            flagged("eligible_hierarchical", summary),
        ),
        "graph_seed": (
            f"corpus_{cid8}_graph",
            child,
            evidence,
            flagged("eligible_graph_seed", child),
        ),
    }
    report: dict[str, Any] = {}
    for route, (old_col, old_flt, new_col, new_flt) in routes.items():
        old_universe = _scroll_universe(old_col, old_flt)
        new_universe = _scroll_universe(new_col, new_flt)
        old_ids = set(old_universe)
        new_ids = set(new_universe)
        payload_mismatches = []
        for pid in old_ids & new_ids:
            old_row = old_universe[pid]
            new_row = new_universe[pid]
            for field in (
                "chunk_id",
                "doc_id",
                "parent_id",
                "source_tier",
                "chunk_kind",
                "text_len",
                "text_hash",
            ):
                if old_row[field] != new_row[field]:
                    payload_mismatches.append(
                        {"point": pid, "field": field}
                    )
        report[route] = {
            "old_collection": old_col,
            "new_collection": new_col,
            "old_universe_count": len(old_ids),
            "new_universe_count": len(new_ids),
            "candidate_set_drift": len(old_ids ^ new_ids),
            "route_leakage": len(new_ids - old_ids),
            "payload_identity_mismatches": len(payload_mismatches),
            "payload_mismatch_sample": payload_mismatches[:5],
        }
    return report


def vector_census(cid8: str) -> dict:
    """Acceptance: 1 dense + 1 sparse embedding per candidate point."""
    base = "http://localhost:6333"
    evidence = f"corpus_{cid8}_evidence"
    resp = requests.post(
        f"{base}/collections/{evidence}/points/scroll",
        json={
            "limit": 512,
            "with_payload": ["record_kind"],
            "with_vector": True,
        },
        timeout=120,
    ).json()["result"]
    census = {"points_checked": 0, "dense_ok": 0, "sparse_ok": 0}
    kinds: dict[str, int] = {}
    for point in resp.get("points", []):
        census["points_checked"] += 1
        vector = point.get("vector") or {}
        kinds[(point.get("payload") or {}).get("record_kind", "?")] = (
            kinds.get((point.get("payload") or {}).get("record_kind", "?"), 0)
            + 1
        )
        if isinstance(vector, dict) and vector.get("dense"):
            census["dense_ok"] += 1
        if isinstance(vector, dict) and vector.get("sparse"):
            census["sparse_ok"] += 1
    census["record_kinds"] = kinds
    return census


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", default="/tmp/pm_token.txt")
    ap.add_argument("--api-base", default="http://localhost:8000")
    ap.add_argument(
        "--runs",
        type=int,
        default=1,
        help="repeat the frozen query set N times (latency sampling)",
    )
    ap.add_argument(
        "--output",
        default="/Users/king/polymath_v3.3/data_eval/q8_parity_report.json",
    )
    ap.add_argument(
        "--baselines",
        type=int,
        default=0,
        help="extra same-layout repeat runs for the nondeterminism baseline "
        "(legacy-vs-legacy / shadow-vs-shadow drift bands)",
    )
    args = ap.parse_args()

    token = open(args.token_file).read().strip()

    # Stage 2 first: deterministic universe parity (no top-k cutoff).
    cid8 = CANARY_CORPUS[:8]
    universe_report = universe_parity(cid8)
    vector_census_report = vector_census(cid8)
    print("\nUNIVERSE PARITY (deterministic):")
    print(json.dumps(universe_report, indent=2))
    print("\nVECTOR CENSUS:")
    print(json.dumps(vector_census_report, indent=2))

    report: dict[str, Any] = {
        "canary_corpus": CANARY_CORPUS,
        "routes": ROUTES,
        "frozen_queries": FROZEN_QUERIES,
        "runs": args.runs,
        "universe_parity": universe_report,
        "vector_census": vector_census_report,
        "results": {},
        "parity": {},
    }

    for route, tier in ROUTES.items():
        route_block: dict[str, Any] = {"legacy": [], "shadow": []}
        total_runs = args.runs + args.baselines
        for layout in ("legacy", "shadow"):
            for run_index in range(total_runs):
                for query in FROZEN_QUERIES:
                    result = run_one(
                        args.api_base,
                        token,
                        query,
                        tier,
                        shadow=(layout == "shadow"),
                    )
                    result["query"] = query
                    result["run"] = run_index
                    route_block[layout].append(result)
                    print(
                        f"[{route}/{layout}] run={run_index} "
                        f"sources={result['source_count']} "
                        f"latency={result['latency_seconds']}s "
                        f"q={query[:48]!r}"
                    )
        report["results"][route] = route_block

        # ── parity metrics ─────────────────────────────────────────────
        legacy_lat = [r["latency_seconds"] for r in route_block["legacy"]]
        shadow_lat = [r["latency_seconds"] for r in route_block["shadow"]]
        drift_total = 0
        leakage_total = 0
        doc_ids_preserved = True
        chunk_ids_preserved = True
        parent_hydration_preserved = True
        pairs = min(len(route_block["legacy"]), len(route_block["shadow"]))
        for i in range(pairs):
            legacy_sources = route_block["legacy"][i]["sources"]
            shadow_sources = route_block["shadow"][i]["sources"]
            legacy_ids = set(legacy_sources)
            shadow_ids = set(shadow_sources)
            drift_total += len(legacy_ids ^ shadow_ids)
            leakage_total += len(shadow_ids - legacy_ids)
            if legacy_ids != shadow_ids:
                chunk_ids_preserved = False
                doc_ids_preserved = False
            else:
                legacy_docs = {s["doc_id"] for s in legacy_sources.values()}
                shadow_docs = {s["doc_id"] for s in shadow_sources.values()}
                legacy_parents = {
                    s["parent_id"] for s in legacy_sources.values()
                }
                shadow_parents = {
                    s["parent_id"] for s in shadow_sources.values()
                }
                if legacy_docs != shadow_docs:
                    doc_ids_preserved = False
                if legacy_parents != shadow_parents:
                    parent_hydration_preserved = False
        latency = {
            "legacy": {
                "p50": _pctl(legacy_lat, 50),
                "p95": _pctl(legacy_lat, 95),
                "count": len(legacy_lat),
            },
            "shadow": {
                "p50": _pctl(shadow_lat, 50),
                "p95": _pctl(shadow_lat, 95),
                "count": len(shadow_lat),
            },
        }
        report["parity"][route] = {
            "candidate_set_drift": drift_total,
            "route_leakage": leakage_total,
            "exact_chunk_ids_preserved": chunk_ids_preserved,
            "exact_doc_ids_preserved": doc_ids_preserved,
            "parent_hydration_preserved": parent_hydration_preserved,
            "latency_seconds": latency,
            "queries_compared": pairs,
        }
        # Same-layout nondeterminism baseline: the pipeline is not
        # deterministic across identical runs (RRF ties, reranker), so the
        # cross-layout drift must be judged against this band, not zero.
        def _within_layout_drift(layout: str) -> int:
            entries = route_block[layout]
            drift = 0
            by_query: dict[str, list[set]] = {}
            for entry in entries:
                by_query.setdefault(entry["query"], []).append(
                    set(entry["sources"])
                )
            for sets in by_query.values():
                anchor = sets[0]
                for other in sets[1:]:
                    drift += len(anchor ^ other)
            return drift

        report["parity"][route]["baseline_within_layout_drift"] = {
            "legacy": _within_layout_drift("legacy"),
            "shadow": _within_layout_drift("shadow"),
        }
        print(
            f"[{route}] parity: drift={drift_total} leakage={leakage_total} "
            f"chunks_ok={chunk_ids_preserved} docs_ok={doc_ids_preserved} "
            f"parents_ok={parent_hydration_preserved} latency={latency}"
        )

    # Physical point counts: one point per child in the candidate vs three
    # copies in the legacy family (read from Qdrant REST).
    try:
        qdrant_base = "http://localhost:6333"
        counts = {}
        for name in (
            f"corpus_{CANARY_CORPUS[:8]}_naive",
            f"corpus_{CANARY_CORPUS[:8]}_hrag",
            f"corpus_{CANARY_CORPUS[:8]}_graph",
            f"corpus_{CANARY_CORPUS[:8]}_evidence",
        ):
            info = requests.get(
                f"{qdrant_base}/collections/{name}", timeout=30
            ).json()["result"]
            counts[name] = {
                "points_count": info.get("points_count"),
                "indexed_vectors_count": info.get("indexed_vectors_count"),
            }
        report["collection_point_counts"] = counts
    except Exception as exc:  # noqa: BLE001
        report["collection_point_counts"] = {"error": str(exc)}

    with open(args.output, "w") as handle:
        json.dump(report, handle, indent=2)
    print(f"\nreport written: {args.output}")
    print(json.dumps(report["parity"], indent=2))


if __name__ == "__main__":
    main()
