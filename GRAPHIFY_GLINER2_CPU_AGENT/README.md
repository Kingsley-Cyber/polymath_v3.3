# Graphify GLiNER2 CPU Agent Pack

A drop-in, dependency-free workflow controller for a repository agent performing the Graphify extraction refactor.

## Drop-in use

Place this folder at the repository root or one level below it, then direct the coding agent to `PROMPT_TO_AGENT.md`.

Manual initialization:

```bash
cd GRAPHIFY_GLINER2_CPU_AGENT
python3 controller.py init --repo-root ..
python3 controller.py verify-pack
python3 controller.py next
```

The controller maintains `.agent_state/state.json`, enforces stage dependencies, activates the semantic-rescue branch from the measured gold-pair recall, verifies receipt artifacts, and refuses finalization until all required stages have passed or been conditionally skipped.

## Contents

- `AGENTS.md` — controlling behavior and scope.
- `RUNBOOK.md` — implementation and validation requirements.
- `workflow.json` — state graph.
- `workflow.mmd` — visual graph.
- `acceptance_gates.json` — measurable gates.
- `controller.py` — persistent workflow ledger.
- `scripts/repo_probe.py` — discovery assistant.
- `scripts/run_e2e.py` — repository-command-agnostic E2E runner.
- `scripts/evaluate_result.py` — gate evaluator.
- `scripts/validate_fixtures.py` — fixture integrity verifier.
- `scripts/check_cpu_policy.py` — candidate runtime policy check.
- `scripts/check_parse_once.py` — spaCy ownership check.
- `fixtures/graphify_quality_fixture.md` — quality/adversarial document.
- `fixtures/graphify_throughput_fixture.md` — fixed throughput/tail document.

## Agent completion sentence

The agent should not report completion until `python3 controller.py finalize` exits zero and the final report states:

```text
implementation_complete: true
e2e_verified: true
speed_gate_passed: true
quality_gate_passed: true
held_out_qualification: pending|passed
production_graph_write_promotion: pending|passed
```
