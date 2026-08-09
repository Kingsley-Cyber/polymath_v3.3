from pathlib import Path
import json
from .common import utc_now, write_json, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    proof_path = pack_root / "work/validation/cpu_only_provider.json"
    if proof_path.is_file():
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        health = proof.get("health", {})
        checks = {
            "runtime_passed": proof.get("status") == "passed",
            "model_id_pinned": health.get("model_id") == "fastino/gliner2-base-v1",
            "device_cpu": proof.get("cpu_only") is True and health.get("device") == "cpu",
            "single_warm_model": proof.get("single_warm_model") is True
            and health.get("model_load_count") == 1,
            "singleton_provider": proof.get("same_provider_object") is True,
            "checkpoint_pinned": len(str(health.get("model_checkpoint_sha256") or "")) == 64,
            "retired_providers_absent": proof.get("retired_providers_not_loaded") is True
            and not proof.get("retired_provider_modules_loaded"),
            "exact_span_predictions": all(
                item.get("end", 0) > item.get("start", -1)
                for row in proof.get("predictions", []) for item in row
            ),
        }
        passed = all(checks.values())
        write_text(
            pack_root / "work/reports/IMPLEMENT_CPU_ENTITY_PROVIDER.md",
            "# CPU entity provider\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Model: `{health.get('model_id')}` at revision `{health.get('model_revision')}`.\n\n"
            f"Device: `{health.get('device')}`; warm load count: `{health.get('model_load_count')}`.\n\n"
            f"Live proof seconds: `{proof.get('elapsed_seconds', 0.0):.6f}`.\n",
        )
        return {
            "status": "PASSED" if passed else "FAILED",
            "started_at": started,
            "files_changed": [str(proof_path)],
            "tests": ["backend/tests/extraction/test_gliner2_cpu_provider.py"],
            "metrics": {
                "model_load_count": health.get("model_load_count"),
                "live_proof_seconds": proof.get("elapsed_seconds"),
                "prediction_rows": len(proof.get("predictions", [])),
            },
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    validation = {
        "status": "NEEDS_AGENT",
        "provider": "fastino/gliner2-base-v1",
        "device": "cpu",
        "must_fail_on": ["cuda", "mps", "mlx", "gpu"],
        "required_runtime_proof": "A repository test or trace showing all model tensors/parameters run on CPU and no retired provider loads.",
    }
    write_json(pack_root/"work/validation/cpu_only_provider.json", validation)
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Implement CPU-only provider in target repo and replace validation status."]}
