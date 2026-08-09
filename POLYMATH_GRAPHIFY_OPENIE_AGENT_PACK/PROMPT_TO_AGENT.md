# Agent instruction: execute the Graphify GLiNER2 + OpenIE refactor

You are inside a repository containing this orchestration pack.

Do not ask the user for repository paths. Discover the environment. Read workflow state. Map repository capabilities to actual files. Execute the next incomplete stage. Modify the narrowest existing seams. Run validators and tests after each stage. Persist receipts. Resume until final verification.

Do not declare completion until:

- the two Markdown E2E fixtures run through the canonical Graphify entrypoint,
- graph projection is rebuilt from canonical artifacts,
- idempotency checks pass,
- quality gates pass,
- speed gate passes,
- reachability validation proves the new live path is actually executed,
- retired runtime providers are not loaded.

## Commands

If this folder is at repository root:

```bash
cd POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK
python controller.py init --repo-root ..
python controller.py verify-pack
python controller.py status
python controller.py next
```

If this folder is nested elsewhere, pass the actual repository root:

```bash
python /path/to/POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK/controller.py init --repo-root /path/to/repo
```

## Execution law

```text
The model proposes.
The linguistic extractor proposes.
The repository's deterministic compiler interprets.
The evidence gate decides.
The graph never becomes the source of truth.
```

## Scope lock

Do not broaden scope.

Do not:

- redesign retrieval,
- redesign Query IR,
- redesign MCP,
- change databases,
- introduce LangChain or LangGraph,
- benchmark MPS,
- benchmark MLX,
- benchmark CUDA,
- fine-tune models,
- add another neural relation model by default,
- replace the existing ontology,
- invent repository paths before discovery.

Use:

```yaml
entity_model: fastino/gliner2-base-v1
runtime: Python / PyTorch
execution: CPU only
openie: triplet-extract Balanced CPU
```

## Stage behavior

For every stage:

1. Read the stage instructions printed by `controller.py next`.
2. Inspect the repository and use existing seams first.
3. Make the minimum required change.
4. Run the stage's validators.
5. Save receipts under `work/receipts/`.
6. Run `python controller.py status` before moving on.

If a validator fails, stop and fix that stage. Do not work around the workflow by editing state manually.

## Final status required

Your final response to the user must include:

```yaml
implementation_complete: true|false
e2e_verified: true|false
speed_gate_passed: true|false
quality_gate_passed: true|false
held_out_qualification: pending|passed|failed
production_graph_write_promotion: pending|passed|failed
```

Two-document fixture success does not authorize production graph writes. Closed-world and held-out qualification remain separate release gates.
