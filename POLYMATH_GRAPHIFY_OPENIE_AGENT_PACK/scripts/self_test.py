from __future__ import annotations
import importlib.util
import json
from pathlib import Path


def run_self_tests(pack_root: Path) -> dict:
    tests = []
    errors = []

    required = [
        "PROMPT_TO_AGENT.md", "README.md", "ARCHITECTURE.md", "ACCEPTANCE_CRITERIA.md",
        "controller.py", "workflow.py", "state.py", "config.yaml",
        "fixtures/graphify_quality_fixture.md", "fixtures/graphify_quality_gold.json",
        "fixtures/graphify_throughput_fixture.md", "fixtures/graphify_throughput_gold.json",
    ]
    for rel in required:
        exists = (pack_root/rel).exists()
        tests.append({"name": f"exists:{rel}", "passed": exists})
        if not exists:
            errors.append(f"Missing required file: {rel}")

    for schema in (pack_root/"schemas").glob("*.json"):
        try:
            json.loads(schema.read_text(encoding="utf-8"))
            tests.append({"name": f"schema_json:{schema.name}", "passed": True})
        except Exception as e:
            tests.append({"name": f"schema_json:{schema.name}", "passed": False})
            errors.append(f"Invalid JSON schema {schema}: {e}")

    # validate gold offsets
    spec = importlib.util.spec_from_file_location("exact_offsets", pack_root/"validators/exact_offsets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    for base in ["graphify_quality", "graphify_throughput"]:
        result = mod.validate_gold_offsets(pack_root/f"fixtures/{base}_fixture.md", pack_root/f"fixtures/{base}_gold.json")
        tests.append({"name": f"gold_offsets:{base}", "passed": result["passed"], "checked_entities": result.get("checked_entities")})
        if not result["passed"]:
            errors.append(f"Gold offset failure for {base}: {result['errors'][:3]}")

    # import workflow and validate DAG names
    try:
        import sys
        sys.path.insert(0, str(pack_root))
        import workflow
        names = [n.name for n in workflow.WORKFLOW]
        if len(names) != len(set(names)):
            errors.append("Duplicate workflow node names")
        for node in workflow.WORKFLOW:
            for dep in node.depends_on:
                if dep not in names:
                    errors.append(f"Unknown dependency {dep} for {node.name}")
        tests.append({"name": "workflow_dag", "passed": not any("dependency" in e.lower() for e in errors)})
    except Exception as e:
        tests.append({"name": "workflow_import", "passed": False})
        errors.append(f"Workflow import failed: {e}")

    passed = not errors and all(t.get("passed") for t in tests)
    return {"passed": passed, "tests": tests, "errors": errors}


if __name__ == "__main__":
    print(json.dumps(run_self_tests(Path(__file__).resolve().parents[1]), indent=2, sort_keys=True))
