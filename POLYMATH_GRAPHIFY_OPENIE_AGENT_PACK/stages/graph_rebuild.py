import json
from pathlib import Path

from .common import sha256_text, utc_now, write_json


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    first_path = pack_root / "work/metrics/e2e_proof_final.json"
    repeat_path = pack_root / "work/metrics/graph_rebuild_repeat_final.json"
    idempotency_path = pack_root / "work/metrics/idempotency_final.json"
    required = (first_path, repeat_path, idempotency_path)
    if any(not path.exists() for path in required):
        return {"status": "NEEDS_AGENT", "started_at": started, "errors": ["final graph rebuild artifacts are missing"]}
    first, repeat, idempotency = [json.loads(path.read_text(encoding="utf-8")) for path in required]
    gates = {
        "first_rebuild_passed": first.get("graph_rebuild_match") is True,
        "repeat_rebuild_passed": repeat.get("graph_rebuild_match") is True,
        "identical_graph_snapshot": first.get("after") == repeat.get("after"),
        "empty_after_delete": not any((repeat.get("deleted_counts") or {}).values()),
        "idempotency_verifier_passed": idempotency.get("passed") is True,
    }
    report = {
        "status": "PASSED" if all(gates.values()) else "FAILED",
        "created_at": utc_now(),
        "gates": gates,
        "first": first.get("after"),
        "repeat": repeat.get("after"),
        "deleted_counts": repeat.get("deleted_counts"),
    }
    output = pack_root / "work/metrics/graph_rebuild.json"
    write_json(output, report)
    return {
        "status": report["status"],
        "started_at": started,
        "inputs_hash": sha256_text(json.dumps([first, repeat, idempotency], sort_keys=True)),
        "outputs_hash": sha256_text(json.dumps(report, sort_keys=True)),
        "files_changed": [str(output)],
        "tests": ["delete isolated projection", "rebuild from Mongo artifacts", "compare graph digest and counts"],
        "metrics": {"graph_digest": (repeat.get("after") or {}).get("digest")},
        "errors": [name for name, passed in gates.items() if not passed],
    }
