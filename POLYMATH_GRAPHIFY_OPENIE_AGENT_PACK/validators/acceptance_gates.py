from __future__ import annotations
import json
from pathlib import Path

DEFAULT_THRESHOLDS = {
    "entity_exact_span_f1": 0.85,
    "entity_type_f1": 0.85,
    "generic_noun_false_positive_rate_max": 0.05,
    "openie_plus_precision_gold_pair_recall": 0.65,
    "directed_canonical_triple_precision": 0.90,
    "directed_canonical_triple_recall": 0.60,
    "directed_canonical_triple_f1": 0.72,
    "speed_ratio_vs_relex": 1.5,
}


def check_metrics(metrics_path: str | Path, thresholds: dict | None = None) -> dict:
    thresholds = thresholds or DEFAULT_THRESHOLDS
    metrics = json.loads(Path(metrics_path).read_text(encoding="utf-8"))
    errors = []
    def get(name):
        return metrics.get(name)
    for key, threshold in thresholds.items():
        if key.endswith("_max"):
            metric_name = key[:-4]
            value = get(metric_name)
            if value is None or value > threshold:
                errors.append(f"{metric_name}={value} exceeds max {threshold}")
        else:
            value = get(key)
            if value is None or value < threshold:
                errors.append(f"{key}={value} below min {threshold}")
    if metrics.get("accepted_pronoun_endpoints", 1) != 0:
        errors.append("accepted_pronoun_endpoints must be 0")
    if metrics.get("unsupported_graph_edges", 1) != 0:
        errors.append("unsupported_graph_edges must be 0")
    if metrics.get("forced_related_to_fallback_count", 1) != 0:
        errors.append("forced_related_to_fallback_count must be 0")
    return {"passed": not errors, "errors": errors, "metrics": metrics}


if __name__ == "__main__":
    import sys
    print(json.dumps(check_metrics(sys.argv[1]), indent=2))
