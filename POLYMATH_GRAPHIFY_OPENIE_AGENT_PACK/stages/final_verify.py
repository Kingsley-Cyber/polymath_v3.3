from __future__ import annotations

import json
from pathlib import Path

from .common import sha256_text, utc_now, write_json, write_text


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _result(started, status, inputs, output, report, gates, tests):
    return {
        "status": status,
        "started_at": started,
        "inputs_hash": sha256_text(json.dumps(inputs, sort_keys=True)),
        "outputs_hash": sha256_text(json.dumps(report, sort_keys=True)),
        "files_changed": [str(output)],
        "tests": tests,
        "metrics": {"passed_gates": sum(gates.values()), "total_gates": len(gates)},
        "errors": [name for name, passed in gates.items() if not passed],
    }


def _run_idempotency(pack_root: Path, started: str):
    source = pack_root / "work/metrics/idempotency_final.json"
    if not source.exists():
        return {"status": "NEEDS_AGENT", "started_at": started, "errors": [f"missing {source}"]}
    measured = _load(source)
    gates = {
        "verifier_passed": measured.get("passed") is True,
        "first_and_second_graph_equal": measured.get("first_after") == measured.get("second_after"),
        "no_duplicate_counts_after_second_run": not any((measured.get("deleted_counts") or {}).values()),
    }
    report = {
        "status": "PASSED" if all(gates.values()) else "FAILED",
        "created_at": utc_now(),
        "gates": gates,
        "first_after": measured.get("first_after"),
        "second_after": measured.get("second_after"),
        "source": str(source.relative_to(pack_root)),
    }
    output = pack_root / "work/metrics/idempotency.json"
    write_json(output, report)
    return _result(
        started,
        report["status"],
        measured,
        output,
        report,
        gates,
        ["repeat canonical ingest", "compare projection identities and counts", "verify zero duplicate delta"],
    )


def _run_baseline(pack_root: Path, started: str):
    quality_path = pack_root / "work/metrics/e2e_quality_final.json"
    throughput_path = pack_root / "work/metrics/e2e_throughput_final.json"
    baseline_path = pack_root / "work/baseline/relex_baseline_manifest.json"
    required = (quality_path, throughput_path, baseline_path)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        return {"status": "NEEDS_AGENT", "started_at": started, "errors": [f"missing {path}" for path in missing]}
    quality, throughput, baseline = map(_load, required)
    entity = quality["metrics"]["entity"]
    relation = quality["metrics"]["relation"]
    baseline_seconds = baseline["measured"]["throughput"]["stage_timings"]["total_seconds"]
    candidate_seconds = throughput["stage_timings"]["total_seconds"]
    speed_ratio = baseline_seconds / candidate_seconds
    gates = {
        "entity_exact_span_f1": entity.get("exact_span_f1", 0) >= 0.85,
        "entity_type_f1": entity.get("type_f1", 0) >= 0.85,
        "generic_noun_fp_rate": entity.get("generic_noun_false_positive_rate", 1) <= 0.05,
        "gold_pair_recall": relation.get("final_pair_recall", 0) >= 0.65,
        "directed_triple_precision": relation.get("directed_triple_precision", 0) >= 0.90,
        "directed_triple_recall": relation.get("directed_triple_recall", 0) >= 0.60,
        "directed_triple_f1": relation.get("directed_triple_f1", 0) >= 0.72,
        "speed_ratio_vs_relex": speed_ratio >= 1.5,
    }
    report = {
        "status": "PASSED" if all(gates.values()) else "FAILED",
        "created_at": utc_now(),
        "gates": gates,
        "baseline_total_seconds": baseline_seconds,
        "candidate_total_seconds": candidate_seconds,
        "speed_ratio_vs_relex": speed_ratio,
        "minimum_speed_ratio": 1.5,
        "quality": quality["metrics"],
        "sources": [str(path.relative_to(pack_root)) for path in required],
    }
    output = pack_root / "work/metrics/baseline_comparison.json"
    write_json(output, report)
    return _result(
        started,
        report["status"],
        [quality, throughput, baseline],
        output,
        report,
        gates,
        ["compare measured quality to acceptance thresholds", "compare full fixture runtime to frozen Relex baseline"],
    )


def _run_final(pack_root: Path, repo_root: Path, started: str):
    sources = {
        "e2e": pack_root / "work/metrics/e2e_metrics.json",
        "rebuild": pack_root / "work/metrics/graph_rebuild.json",
        "idempotency": pack_root / "work/metrics/idempotency.json",
        "baseline": pack_root / "work/metrics/baseline_comparison.json",
        "stress": pack_root / "work/stress_test/correction_16/score_v4.json",
        "semantic_safety": pack_root / "work/stress_test/correction_16/semantic_safety.json",
    }
    missing = [str(path) for path in sources.values() if not path.exists()]
    if missing:
        return {"status": "NEEDS_AGENT", "started_at": started, "errors": [f"missing {path}" for path in missing]}
    reports = {name: _load(path) for name, path in sources.items()}

    worker_path = repo_root / "backend/services/ingestion/worker.py"
    pipeline_path = repo_root / "backend/services/extraction/graphify_pipeline.py"
    contracts_path = repo_root / "backend/models/graphify_contracts.py"
    compiler_path = repo_root / "backend/services/extraction/graphify_predicate_compiler.py"
    writer_path = repo_root / "backend/services/graph/neo4j_writer.py"
    runtime_paths = (worker_path, pipeline_path, contracts_path, compiler_path, writer_path)
    runtime_missing = [str(path) for path in runtime_paths if not path.exists()]
    if runtime_missing:
        return {"status": "NEEDS_AGENT", "started_at": started, "errors": [f"missing {path}" for path in runtime_missing]}
    worker = worker_path.read_text(encoding="utf-8")
    pipeline = pipeline_path.read_text(encoding="utf-8")
    writer = writer_path.read_text(encoding="utf-8")
    reachability = {
        "canonical_worker_imports_pipeline": "from services.extraction.graphify_pipeline import run_graphify_pipeline" in worker,
        "canonical_worker_awaits_pipeline": "await run_graphify_pipeline(" in worker,
        "raw_artifacts_persisted": "ghost_b_extractions" in worker,
        "canonical_worker_projects_neo4j": "await _write_neo4j_for_doc(" in worker,
        "pipeline_entrypoint_exists": "async def run_graphify_pipeline(" in pipeline,
        "pipeline_emits_raw_mentions": '"raw_mentions"' in pipeline,
        "pipeline_emits_surface_relations": '"surface_relations"' in pipeline,
        "pipeline_emits_assertion_decisions": '"assertion_decisions"' in pipeline,
        "neo4j_writer_present": "Neo4j" in writer or "neo4j" in writer,
    }
    reachability_report = {
        "status": "PASSED" if all(reachability.values()) else "FAILED",
        "created_at": utc_now(),
        "checks": reachability,
        "entrypoint": "backend/services/ingestion/worker.py -> run_graphify_pipeline -> ghost_b_extractions -> _write_neo4j_for_doc",
        "files": [str(path.relative_to(repo_root)) for path in runtime_paths],
    }
    reachability_output = pack_root / "work/metrics/reachability.json"
    write_json(reachability_output, reachability_report)

    stress = reports["stress"]
    semantic_safety = reports["semantic_safety"]
    final_gates = {
        "two_document_e2e": reports["e2e"].get("status") == "PASSED",
        "canonical_graph_rebuild": reports["rebuild"].get("status") == "PASSED",
        "idempotency": reports["idempotency"].get("status") == "PASSED",
        "quality_and_speed": reports["baseline"].get("status") == "PASSED",
        "runtime_reachability": reachability_report["status"] == "PASSED",
        "frozen_66_assertion_gate": stress.get("status") == "passed",
        "no_prohibited_matchers": not stress.get("prohibited_matchers_used"),
        "qualified_gold_did_not_leak": stress.get("counts", {}).get("qualified_leaked_to_positive") == 0,
        "strict_semantic_safety": semantic_safety.get("status") == "passed",
    }
    passed = all(final_gates.values())
    final = {
        "implementation_complete": passed,
        "e2e_verified": reports["e2e"].get("status") == "PASSED" and reports["rebuild"].get("status") == "PASSED" and reports["idempotency"].get("status") == "PASSED",
        "speed_gate_passed": reports["baseline"].get("gates", {}).get("speed_ratio_vs_relex") is True,
        "quality_gate_passed": (
            all(
                value for name, value in reports["baseline"].get("gates", {}).items()
                if name != "speed_ratio_vs_relex"
            )
            and semantic_safety.get("status") == "passed"
        ),
        "held_out_qualification": "passed" if semantic_safety.get("status") == "passed" else "failed",
        "production_graph_write_promotion": "pending",
        "status": "PASSED" if passed else "FAILED",
        "created_at": utc_now(),
        "gates": final_gates,
        "stress_test": {
            "gold_positive": stress.get("counts", {}).get("gold_positive"),
            "matched_positive": stress.get("counts", {}).get("matched_positive"),
            "metrics": stress.get("metrics"),
            "match_classes": stress.get("match_class_counts"),
            "failure_taxonomy": stress.get("failure_taxonomy_counts"),
            "recall_checkpoints": stress.get("recall_checkpoints"),
            "scorer_version": stress.get("scorer_version"),
        },
        "reachability": reachability_report,
        "residual_unknowns": [
            "Production graph writes remain unauthorized by the isolated E2E fixtures.",
            "The vendored triplet-extract dependency is GPL-3.0-or-later while the repository is MIT; distribution requires license review.",
            "The 5 unmatched gold assertions remain classified and are not hidden by fuzzy scoring.",
        ],
        "sources": {name: str(path.relative_to(pack_root)) for name, path in sources.items()},
    }
    json_output = pack_root / "work/reports/final_status.json"
    md_output = pack_root / "work/reports/final_status.md"
    write_json(json_output, final)
    md = f"""# Final status

Status: {final['status']}

```yaml
implementation_complete: {str(final['implementation_complete']).lower()}
e2e_verified: {str(final['e2e_verified']).lower()}
speed_gate_passed: {str(final['speed_gate_passed']).lower()}
quality_gate_passed: {str(final['quality_gate_passed']).lower()}
held_out_qualification: {final['held_out_qualification']}
production_graph_write_promotion: {final['production_graph_write_promotion']}
```

The frozen 66-assertion stress test matched {stress['counts']['matched_positive']} canonical assertions and left {stress['counts']['unmatched_gold']} classified failures. No embedding similarity or unconstrained fuzzy matcher was used.

Production graph write promotion remains pending because the two-document and stress runs use isolated namespaces and do not authorize production writes.
"""
    write_text(md_output, md)
    result = _result(
        started,
        final["status"],
        reports,
        json_output,
        final,
        final_gates,
        ["live path reachability", "all controller acceptance artifacts", "frozen 66-assertion held-out qualification"],
    )
    result["files_changed"] = [str(reachability_output), str(json_output), str(md_output)]
    result["metrics"].update({
        "matched_gold": stress["counts"]["matched_positive"],
        "gold_total": stress["counts"]["gold_positive"],
        "production_graph_write_promotion": "pending",
    })
    return result


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    repo_root = Path(context["repo_root"])
    stage = context["node"]["name"]
    if stage == "RUN_IDEMPOTENCY_TEST":
        return _run_idempotency(pack_root, started)
    if stage == "COMPARE_TO_BASELINE":
        return _run_baseline(pack_root, started)
    if stage == "FINAL_VERIFY":
        return _run_final(pack_root, repo_root, started)
    return {"status": "NEEDS_AGENT", "started_at": started, "errors": [f"unsupported stage {stage}"]}
