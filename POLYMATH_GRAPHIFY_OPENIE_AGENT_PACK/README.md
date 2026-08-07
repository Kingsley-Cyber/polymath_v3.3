# Polymath Graphify OpenIE Agent Pack

This is a drop-in repository-agent orchestration pack for the Polymath Graphify extraction refactor.
It is intentionally repository-local: place this folder anywhere inside the target repository, then point a capable coding agent at `PROMPT_TO_AGENT.md`.

The pack is a lightweight, plain-Python workflow controller. It behaves like a minimal LangGraph-style state machine without requiring LangGraph or LangChain.

## Target architecture

```text
SOURCE DOCUMENTS
  -> structure-aware normalization + reversible offsets
  -> zero-model corpus survey
  -> GLiNER2 CPU entity census
  -> immutable raw mentions
  -> document entity reducer
  -> document-local PhraseMatcher / EntityRuler mention completion
  -> relation-eligible sentence selection
  -> triplet-extract Balanced CPU OpenIE
  -> OpenIE Argument Adapter
  -> OpenIE Proposition Reducer
  -> existing high-precision syntax rules
  -> existing deterministic predicate compiler
  -> existing Claim / Assertion assembler
  -> existing deterministic evidence / graph gate
  -> MongoDB authoritative artifacts
  -> Neo4j rebuildable projection
```

## Scope lock

The initial implementation is **CPU-only**:

```yaml
entity_model: fastino/gliner2-base-v1
runtime: Python / PyTorch
device: CPU only
openie: triplet-extract Balanced CPU
```

Do not benchmark or integrate MPS, MLX, CUDA, REBEL, LFM, GLiREL, or Relex as runtime providers in this refactor. Relex remains an executable offline baseline only until the refactor is qualified.

## Quick start for a repo agent

From inside the target repository:

```bash
python POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK/controller.py init --repo-root .
python POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK/controller.py verify-pack
python POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK/controller.py next
```

If the folder is placed at repository root, use:

```bash
cd POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK
python controller.py init --repo-root ..
python controller.py verify-pack
python controller.py next
```

## Human instruction to give the agent

```text
Read POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK/PROMPT_TO_AGENT.md. Discover the repository around this folder. Execute the workflow stages in order. Modify only the narrowest existing seams. Persist receipts. Do not declare completion until two-document E2E, idempotency, graph rebuild, quality gates, speed gate, and reachability validation pass.
```

## Important files

- `PROMPT_TO_AGENT.md` — the exact instruction for Claude Code, Codex, Cursor, or another repo agent.
- `ARCHITECTURE.md` — target design and non-negotiable invariants.
- `ACCEPTANCE_CRITERIA.md` — measurable gates.
- `controller.py` — state-machine CLI.
- `workflow.py` — dependency-ordered nodes and stage metadata.
- `stages/` — stage guidance and artifact expectations.
- `validators/` — local validation utilities.
- `schemas/` — JSON Schemas for workflow and extraction artifacts.
- `fixtures/` — quality and throughput Markdown fixtures plus gold sidecars.
- `scripts/` — repo scanning, self-test, quality scoring, speed scoring, E2E scaffolding.
- `work/` — generated state, receipts, maps, metrics, and reports.

## Final PASS means

The refactor is not complete merely because code compiles. Final PASS requires:

1. Repository discovery map exists and validates.
2. Frozen/current baseline is captured.
3. Relation kill-switch is measured.
4. GLiNER2 CPU-only provider is live and retired runtime providers are not loaded.
5. Raw mentions are conserved.
6. Document entity reducer and mention completion pass fixture tests.
7. triplet-extract OpenIE is wired through Argument Adapter and Proposition Reducer.
8. Predicate compiler and assertion gate produce accepted facts, qualified claims, open relations, review, and reject lanes.
9. Two Markdown fixtures pass E2E.
10. Isolated graph projection rebuilds from canonical artifacts and is idempotent.
11. Metrics meet the thresholds in `ACCEPTANCE_CRITERIA.md`.
12. The final report states exact pass/fail for implementation, E2E, quality, speed, held-out qualification, and graph-write promotion.
