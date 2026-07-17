#!/usr/bin/env python3
"""Run the owner-bounded canonical temporal activation window.

The ten-query selection is fixed in code before deployment: four temporal
probes inherited from the sealed 24-execution diagnostic, two frozen direct,
two frozen lay-language, and two canonical held-out negatives.  The runner
uses the real chat SSE path and the canonical three-state journal contract.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from config import get_settings
from scripts.run_canonical_heldout_negative_eval import (
    CORPUS_ID,
    SELECTION_PATH,
    SELECTION_SHA256,
    TEMPERATURE,
    TIER,
    TOP_K,
    _atomic_write,
    _build_execution,
    _canonical_bytes,
    _cost_envelope,
    _embedder_preflight,
    _eval_lock,
    _load_hashed_json,
    _prompt_template_receipt,
    _run_sse,
    _seal_journal,
    _sha256_bytes,
    _utc_now,
    _validate_local_api,
    _validate_same_container_runtime,
    _verify_corpus,
)
from scripts.run_two_lane_canonical_window import (
    _normalized_doc_name,
    _score_sources,
    _trace_metadata,
)


BACKEND = Path(__file__).resolve().parents[1]
PREREG_PATH = BACKEND / "evals/runpod_e2e_retrieval_preregister_v1.json"
PREREG_SHA256 = "8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110"
NEGATIVE_PATH = BACKEND / "evals/e2e_heldout_negative_v2_20260717.json"
NEGATIVE_SHA256 = "3b35c14c165f6be89202b809ea01a1cd6ad0f5c0217e4167b86e4b5dc0b09960"
BASELINE_PATH = (
    BACKEND.parent / "docs/baselines/BUILD_FIRST_CANONICAL_BASELINE_10_2026-07-18.json"
)
PRIOR_TEMPORAL_ARTIFACT_SHA256 = (
    "112c090b964fb0a1bc379ebe1b004831581810d07a0d97b9c1b9d8474f58dff9"
)

SELECTION_NAME = "temporal-canonical-10.v1"
TEMPORAL_CASES = (
    {
        "id": "temporal_1929_dialog",
        "shape": "temporal",
        "family": "temporal",
        "question": (
            "What production change happened in 1929, and why does Blain Brown "
            "compare it with the modern digital shift?"
        ),
        "expected_any": [
            "Blain Brown - Cinematography - Theory and Practice (2016).md"
        ],
        "expected_min_distinct": 1,
        "anchors": ["1929", "dialog recording"],
    },
    {
        "id": "temporal_nicole_2006",
        "shape": "temporal",
        "family": "temporal",
        "question": (
            "What happened at the University of Coimbra in June 2006, and how "
            "did the Nicole robot test use movement analysis?"
        ),
        "expected_any": [
            (
                "[International Journal of Reasoning-based Intelligent Systems "
                "vol. 2 iss. 1] Bayesian reasoning for Laban Movement Analysis "
                "used in human-machine interaction{Rett, Jorg_ Dias, Jorge_ "
                "Ahuactzin, Juan Manuel}(2010)[1.md"
            )
        ],
        "expected_min_distinct": 1,
        "anchors": ["June 2006", "Nicole"],
    },
    {
        "id": "temporal_editing_1927",
        "shape": "temporal",
        "family": "temporal",
        "question": "How did film editing change before and after sound arrived in 1927?",
        "expected_any": ["Walter Murch - In the Blink of an Eye (2001).md"],
        "expected_min_distinct": 1,
        "anchors": ["1927", "sound"],
    },
    {
        "id": "temporal_noir_1940s_1950s",
        "shape": "temporal",
        "family": "temporal",
        "question": (
            "How did American film noir of the 1940s and 1950s use lighting "
            "as storytelling?"
        ),
        "expected_any": [
            "Blain Brown - Cinematography - Theory and Practice (2016).md"
        ],
        "expected_min_distinct": 1,
        "anchors": ["film noir", "forties and fifties"],
    },
)
DIRECT_QUERY_IDS = ("direct_facs", "direct_rule_of_six")
LAY_QUERY_IDS = ("lay_dynamic_figure", "lay_natural_cut")
NEGATIVE_QUERY_IDS = ("negv2_f2_oscar_2026", "negv2_f1_crispr")
QUERY_IDS = (
    *(row["id"] for row in TEMPORAL_CASES),
    *DIRECT_QUERY_IDS,
    *LAY_QUERY_IDS,
    *NEGATIVE_QUERY_IDS,
)
CONCURRENCY = 3
JOURNAL_SCHEMA = "polymath.temporal_canonical_window.v1"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def select_cases(
    prereg: dict[str, Any],
    negative: dict[str, Any],
) -> list[dict[str, Any]]:
    frozen_by_id = {
        str(row.get("id") or ""): row for row in prereg.get("queries") or []
    }
    negative_by_id = {
        str(row.get("id") or ""): row for row in negative.get("queries") or []
    }
    missing_frozen = [
        query_id
        for query_id in (*DIRECT_QUERY_IDS, *LAY_QUERY_IDS)
        if query_id not in frozen_by_id
    ]
    missing_negative = [
        query_id for query_id in NEGATIVE_QUERY_IDS if query_id not in negative_by_id
    ]
    require(not missing_frozen, f"frozen compact ids missing: {missing_frozen}")
    require(not missing_negative, f"negative compact ids missing: {missing_negative}")
    cases = [dict(row) for row in TEMPORAL_CASES]
    cases.extend(
        dict(frozen_by_id[query_id]) for query_id in (*DIRECT_QUERY_IDS, *LAY_QUERY_IDS)
    )
    cases.extend(dict(negative_by_id[query_id]) for query_id in NEGATIVE_QUERY_IDS)
    require([str(row["id"]) for row in cases] == list(QUERY_IDS), "selection drift")
    require(len(cases) == len(set(QUERY_IDS)) == 10, "selection must be 10 unique ids")
    return cases


def _normalize_search_text(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def score_temporal_anchors(
    case: dict[str, Any],
    raw_sources: Sequence[dict[str, Any]],
    document_names: dict[str, str],
) -> dict[str, Any]:
    expected = {_normalized_doc_name(value) for value in case.get("expected_any") or []}
    anchors = [str(value) for value in case.get("anchors") or []]
    expected_sources = []
    for source in raw_sources:
        doc_id = str(source.get("doc_id") or "")
        name = str(
            document_names.get(doc_id)
            or source.get("doc_name")
            or source.get("filename")
            or ""
        )
        if _normalized_doc_name(name) in expected:
            expected_sources.append(source)
    source_haystacks = [
        _normalize_search_text(json.dumps(source, sort_keys=True, default=str))
        for source in expected_sources
    ]
    anchor_hits = [
        anchor
        for anchor in anchors
        if any(
            _normalize_search_text(anchor) in haystack for haystack in source_haystacks
        )
    ]
    return {
        "expected_source_count": len(expected_sources),
        "anchors": anchors,
        "anchor_hits": anchor_hits,
        "all_anchors_hit": bool(
            expected_sources and anchors and len(anchor_hits) == len(anchors)
        ),
    }


def _runtime_flags() -> dict[str, bool]:
    settings = get_settings()
    expected = {
        "RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED": True,
        "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED": True,
        "TEMPORAL_QUERY_ROUTING_ENABLED": True,
        "RERANK_EVIDENCE_SUPPORT": False,
        "ATOMIC_CLAIM_ANCHORS_ENABLED": False,
        "PARENT_EXCERPT_ENABLED": False,
        "WATERFALL_ASSEMBLY": False,
        "TWO_LANE_ANCHORING": False,
        "TWO_LANE_ANCHORING_ENABLED": False,
        "HYDE_ENABLED": False,
        "SHELF_RESERVE_ENABLED": False,
        "GROUNDED_QUERY_PLANNER_ENABLED": False,
        "FOUR_LANE_TIER0_ROUTER_ENABLED": False,
        "FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED": False,
        "AGENTIC_MODE_ENABLED": False,
    }
    observed = {name: bool(getattr(settings, name)) for name in expected}
    require(
        observed == expected,
        "runtime flags do not match temporal canonical contract: "
        + json.dumps({"expected": expected, "observed": observed}, sort_keys=True),
    )
    return observed


def _baseline_states() -> tuple[dict[str, str], str]:
    require(BASELINE_PATH.is_file(), "canonical compact baseline is unavailable")
    baseline_bytes = BASELINE_PATH.read_bytes()
    baseline = json.loads(baseline_bytes)
    states = {
        str(row.get("query_id") or ""): str(
            (row.get("classification") or {}).get("state") or ""
        )
        for row in baseline.get("executions") or []
        if str(row.get("query_id") or "") in NEGATIVE_QUERY_IDS
    }
    require(
        set(states) == set(NEGATIVE_QUERY_IDS),
        "canonical baseline lacks temporal compact negative ids",
    )
    return states, _sha256_bytes(baseline_bytes)


def _augment_execution(
    *,
    row: dict[str, Any],
    case: dict[str, Any],
    raw: dict[str, Any],
    document_names: dict[str, str],
) -> dict[str, Any]:
    shape = str(case.get("shape") or "")
    retrieval_meta = _trace_metadata(raw["traces"], "Local RAG retrieval")
    diagnostics = retrieval_meta.get("retrieval_diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    temporal = diagnostics.get("temporal_routing")
    if not isinstance(temporal, dict):
        temporal = {}
    evaluation: dict[str, Any] = {
        "shape": shape,
        "source_score": (
            _score_sources(case, row["sources"])
            if shape != "negative_control"
            else None
        ),
        "temporal_routing": json.loads(json.dumps(temporal, default=str)),
    }
    if shape == "temporal":
        evaluation["anchor_score"] = score_temporal_anchors(
            case,
            raw["sources"],
            document_names,
        )
        if temporal.get("active") is not True:
            row["technical"]["errors"].append(
                "temporal probe lacks active temporal-routing trace"
            )
    row["evaluation"] = evaluation
    if not diagnostics:
        row["technical"]["errors"].append("missing retrieval diagnostics")
    if row["technical"]["errors"]:
        row["technical"]["ok"] = False
        row["technical"]["status"] = "failed"
    return row


def summarize(
    executions: Sequence[dict[str, Any]],
    baseline_states: dict[str, str],
) -> dict[str, Any]:
    temporal = [row for row in executions if row["evaluation"]["shape"] == "temporal"]
    direct = [
        row
        for row in executions
        if str(row["evaluation"]["shape"]).startswith("direct_")
    ]
    lay = [row for row in executions if row["evaluation"]["shape"] == "lay_language"]
    negatives = [
        row for row in executions if row["evaluation"]["shape"] == "negative_control"
    ]

    def rate(values: Sequence[bool]) -> float:
        return (
            round(sum(value is True for value in values) / len(values), 6)
            if values
            else 0.0
        )

    temporal_doc_hit = rate(
        [row["evaluation"]["source_score"]["doc_hit"] for row in temporal]
    )
    full_anchor = rate(
        [row["evaluation"]["anchor_score"]["all_anchors_hit"] for row in temporal]
    )
    direct_rate = rate([row["evaluation"]["source_score"]["doc_hit"] for row in direct])
    lay_rate = rate([row["evaluation"]["source_score"]["doc_hit"] for row in lay])
    observed_answered = sum(
        row["classification"]["state"] == "answered" for row in negatives
    )
    baseline_answered = sum(
        baseline_states[query_id] == "answered" for query_id in NEGATIVE_QUERY_IDS
    )
    states = Counter(row["classification"]["state"] for row in executions)
    gates = {
        "technical": all(row["technical"]["ok"] is True for row in executions),
        "journal_complete": all(row["journal_complete"] is True for row in executions),
        "positive_answerability": all(
            row["classification"]["state"] == "answered"
            for row in (*temporal, *direct, *lay)
        ),
        "temporal_doc_hit": temporal_doc_hit >= 0.90,
        "temporal_full_anchor": full_anchor >= 0.70,
        "temporal_trace_consumed": all(
            row["evaluation"]["temporal_routing"].get("active") is True
            for row in temporal
        ),
        "direct_floor": direct_rate >= 0.85,
        "lay_floor": lay_rate >= 0.75,
        "negative_non_degradation": observed_answered <= baseline_answered,
        "corpus_citation_membership": all(
            row["sources"]["all_in_selected_corpus"] is True for row in executions
        ),
    }
    return {
        "execution_count": len(executions),
        "classification_counts": dict(sorted(states.items())),
        "temporal_count": len(temporal),
        "temporal_doc_hit_rate": temporal_doc_hit,
        "temporal_full_anchor_rate": full_anchor,
        "direct_count": len(direct),
        "direct_doc_hit_rate": direct_rate,
        "lay_count": len(lay),
        "lay_doc_hit_rate": lay_rate,
        "negative_count": len(negatives),
        "baseline_negative_answered_count": baseline_answered,
        "observed_negative_answered_count": observed_answered,
        "gates": gates,
        "all_green": all(gates.values()),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument(
        "--lock-owner",
        default="codex/build-first-queue-20260718",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    token = os.getenv("POLYMATH_EVAL_TOKEN")
    require(bool(token), "POLYMATH_EVAL_TOKEN is required")
    require(not args.output.exists(), "output already exists; use a fresh journal")
    endpoint = _validate_same_container_runtime(_validate_local_api(args.api))
    prereg = _load_hashed_json(PREREG_PATH, PREREG_SHA256, "frozen retrieval spec")
    negative = _load_hashed_json(
        NEGATIVE_PATH,
        NEGATIVE_SHA256,
        "held-out negative spec",
    )
    cases = select_cases(prereg, negative)
    selection = _load_hashed_json(
        SELECTION_PATH,
        SELECTION_SHA256,
        "15-document selection",
    )
    selected_filenames = {
        str(row["filename"]) for row in selection.get("selected") or []
    }
    require(len(selected_filenames) == 15, "15-document selection drifted")
    flags = _runtime_flags()
    baseline_states, baseline_sha = _baseline_states()
    cost_envelope = _cost_envelope(len(cases))
    process_run_id = str(uuid.uuid4())

    with _eval_lock(args.lock_owner, 0, mode="assert-held"):
        preflight = _embedder_preflight(args.api)
        document_names, corpus_receipt = _verify_corpus(
            args.api,
            str(token),
            selected_filenames,
        )
        prompt_receipt, prompt_rendered_at = _prompt_template_receipt()
        state: dict[str, Any] = {
            "schema_version": JOURNAL_SCHEMA,
            "started_at_utc": _utc_now(),
            "completed_at_utc": None,
            "sealed": False,
            "selection": {
                "name": SELECTION_NAME,
                "query_ids": list(QUERY_IDS),
                "query_id_sha256": _sha256_bytes(_canonical_bytes(list(QUERY_IDS))),
                "temporal_query_ids": [str(row["id"]) for row in TEMPORAL_CASES],
                "direct_query_ids": list(DIRECT_QUERY_IDS),
                "lay_query_ids": list(LAY_QUERY_IDS),
                "negative_query_ids": list(NEGATIVE_QUERY_IDS),
            },
            "frozen_spec_sha256": PREREG_SHA256,
            "negative_spec_sha256": NEGATIVE_SHA256,
            "corpus_selection_sha256": SELECTION_SHA256,
            "prior_temporal_artifact_sha256": PRIOR_TEMPORAL_ARTIFACT_SHA256,
            "comparison_baseline": {
                "path": str(BASELINE_PATH.relative_to(BACKEND.parent)),
                "file_sha256": baseline_sha,
                "states": baseline_states,
            },
            "corpus": corpus_receipt,
            "tier": TIER,
            "top_k": TOP_K,
            "temperature": TEMPERATURE,
            "concurrency": CONCURRENCY,
            "runtime_flags": flags,
            "endpoint_binding": endpoint,
            "authentication": {"token_source": "POLYMATH_EVAL_TOKEN"},
            "embedder_preflight": preflight,
            "system_prompt_template": prompt_receipt,
            "cost_envelope": cost_envelope,
            "process_run_id": process_run_id,
            "executions": [],
            "summary": None,
            "seal": None,
        }
        _atomic_write(args.output, state)
        print(
            "TEMPORAL_CANONICAL_START "
            + json.dumps(
                {
                    "queries": len(cases),
                    "concurrency": CONCURRENCY,
                    "temperature": TEMPERATURE,
                    "cost_envelope_usd": cost_envelope["total_usd"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

        def execute_one(
            ordinal: int,
            case: dict[str, Any],
        ) -> tuple[int, dict[str, Any]]:
            print(f"TEMPORAL_EXECUTION_START {ordinal}/10 {case['id']}", flush=True)
            raw = _run_sse(
                api=args.api,
                token=str(token),
                question=str(case["question"]),
                timeout=args.request_timeout,
            )
            row = _build_execution(
                case={**case, "family": str(case.get("family") or case.get("shape"))},
                ordinal=ordinal,
                process_run_id=process_run_id,
                raw=raw,
                prompt_receipt=prompt_receipt,
                document_names=document_names,
                selected_filenames=selected_filenames,
                concurrency=CONCURRENCY,
            )
            return ordinal, _augment_execution(
                row=row,
                case=case,
                raw=raw,
                document_names=document_names,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = [
                pool.submit(execute_one, ordinal, case)
                for ordinal, case in enumerate(cases, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                ordinal, row = future.result()
                state["executions"].append(row)
                state["executions"].sort(
                    key=lambda item: item["prior_call_state"]["request_ordinal"]
                )
                _atomic_write(args.output, state)
                print(
                    "TEMPORAL_EXECUTION_DONE "
                    + json.dumps(
                        {
                            "ordinal": ordinal,
                            "query_id": row["query_id"],
                            "state": row["classification"]["state"],
                            "doc_hit": (
                                (row["evaluation"].get("source_score") or {}).get(
                                    "doc_hit"
                                )
                            ),
                            "full_anchor": (
                                (row["evaluation"].get("anchor_score") or {}).get(
                                    "all_anchors_hit"
                                )
                            ),
                            "temporal_active": row["evaluation"][
                                "temporal_routing"
                            ].get("active"),
                            "technical_ok": row["technical"]["ok"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

        final_prompt_receipt, _ = _prompt_template_receipt(prompt_rendered_at)
        prompt_stable = final_prompt_receipt == prompt_receipt
        if not prompt_stable:
            for row in state["executions"]:
                row["technical"]["ok"] = False
                row["technical"]["status"] = "failed"
                row["technical"]["errors"].append(
                    "system-prompt rendered hash or source SHA changed during batch"
                )
        state["completed_at_utc"] = _utc_now()
        state["prompt_render_context_stable"] = prompt_stable
        state["summary"] = summarize(state["executions"], baseline_states)
        technically_sealable = (
            len(state["executions"]) == len(cases)
            and state["summary"]["gates"]["technical"]
            and state["summary"]["gates"]["journal_complete"]
            and prompt_stable
        )
        state["sealed"] = technically_sealable
        state["seal"] = _seal_journal(state) if technically_sealable else None
        _atomic_write(args.output, state)
        print(
            "TEMPORAL_CANONICAL_SUMMARY "
            + json.dumps(state["summary"], sort_keys=True),
            flush=True,
        )
        if not technically_sealable:
            return 2
        return 0 if state["summary"]["all_green"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run(args)
    except (RuntimeError, OSError, ValueError, urllib.error.URLError) as exc:
        print(
            f"TEMPORAL_CANONICAL_ABORT={type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
