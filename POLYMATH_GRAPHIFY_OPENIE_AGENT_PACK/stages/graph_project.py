import json
from pathlib import Path

from .common import sha256_text, utc_now, write_json


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    proof_path = pack_root / "work/metrics/e2e_proof_final.json"
    quality_path = pack_root / "work/metrics/e2e_quality_final.json"
    throughput_path = pack_root / "work/metrics/e2e_throughput_final.json"
    required = (proof_path, quality_path, throughput_path)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        return {"status": "NEEDS_AGENT", "started_at": started, "errors": [f"missing {path}" for path in missing]}

    proof, quality, throughput = map(_load, required)
    entity = quality["metrics"]["entity"]
    relation = quality["metrics"]["relation"]
    gates = {
        "two_authoritative_documents": len(proof.get("fixtures") or {}) == 2,
        "canonical_entrypoint": proof.get("canonical_entrypoint") == "IngestionService.ingest",
        "single_warm_model": proof.get("model_load_count") == 1,
        "graph_rebuild_match": proof.get("graph_rebuild_match") is True,
        "entity_exact_span_f1": entity.get("exact_span_f1", 0) >= 0.85,
        "entity_type_f1": entity.get("type_f1", 0) >= 0.85,
        "generic_noun_fp_rate": entity.get("generic_noun_false_positive_rate", 1) <= 0.05,
        "directed_triple_precision": relation.get("directed_triple_precision", 0) >= 0.90,
        "directed_triple_recall": relation.get("directed_triple_recall", 0) >= 0.60,
        "directed_triple_f1": relation.get("directed_triple_f1", 0) >= 0.72,
        "qualification_accuracy": relation.get("qualification_fixture_accuracy") == 1.0,
        "forced_related_to_fallbacks_zero": relation.get("forced_related_to_fallbacks") == 0,
        "retired_runtime_models_zero": quality["checks"].get("retired_runtime_models_loaded") == 0,
        "quality_report_passed": quality.get("status") == "passed",
        "throughput_report_passed": throughput.get("status") == "passed",
    }
    metrics = {
        "status": "PASSED" if all(gates.values()) else "FAILED",
        "created_at": utc_now(),
        "namespace": proof.get("namespace"),
        "mongo_database": proof.get("mongo_database"),
        "corpus_id": proof.get("corpus_id"),
        "graph": proof.get("after"),
        "quality": quality["metrics"],
        "throughput": throughput["metrics"],
        "gates": gates,
        "artifacts": [str(path.relative_to(pack_root)) for path in required],
    }
    output = pack_root / "work/metrics/e2e_metrics.json"
    write_json(output, metrics)
    return {
        "status": metrics["status"],
        "started_at": started,
        "inputs_hash": sha256_text(json.dumps([proof, quality, throughput], sort_keys=True)),
        "outputs_hash": sha256_text(json.dumps(metrics, sort_keys=True)),
        "files_changed": [str(output)],
        "tests": ["canonical two-document ingest", "isolated graph delete and rebuild", "quality fixture", "throughput fixture"],
        "metrics": {"passed_gates": sum(gates.values()), "total_gates": len(gates)},
        "errors": [name for name, passed in gates.items() if not passed],
    }
