# Graphify GLiNER2 CPU Refactor — Agent Operating Contract

## Authority

This folder is a stage-gated execution contract for a repository agent. `AGENTS.md` is authoritative. `RUNBOOK.md` supplies the implementation detail. `workflow.json` supplies machine-readable sequencing. `acceptance_gates.json` supplies measurable completion thresholds.

When repository conventions conflict with guessed paths in this pack, preserve repository conventions and record the actual attachment points in the discovery artifact. Do not create duplicate services merely because this pack uses generic names.

## Mission

Refactor the canonical Graphify extraction path so that:

1. `fastino/gliner2-base-v1` on **CPU only** is the sole production semantic entity extractor.
2. The existing lightweight spaCy parser and deterministic Python stack remain responsible for grammatical relation proposals, surface-predicate records, predicate compilation, assertion qualification, and graph admission.
3. Entity work is performed as a corpus-batched census with immutable raw mentions, document-local adjudication, deterministic mention completion, and selective relation parsing.
4. A bounded GLiNER2 CPU semantic relation rescue lane is implemented only when the gold-entity syntax ceiling requires it.
5. Mongo/evidence records remain authoritative; Neo4j remains a rebuildable projection.
6. The existing public Graphify entrypoint remains canonical.
7. The refactor is not complete until both committed Markdown fixtures pass end to end and the isolated graph can be rebuilt identically from authoritative records.

## Locked decisions

- Runtime: Python/PyTorch CPU.
- Primary checkpoint: `fastino/gliner2-base-v1`.
- Primary hot-path task: entity extraction only.
- One warm model instance per Graphify process.
- No model multiprocessing.
- Initial GLiNER2 batch size: 8; adjust only within CPU after measurement.
- spaCy: existing non-transformer pipeline, CPU, statistical NER disabled, `nlp.pipe`, initial `n_process=1`.
- Relex: executable offline benchmark baseline only until final qualification.
- Graph writes: isolated test projection only until repository release policy promotes production writes.

## Prohibited scope

Do not add or benchmark MPS, MLX, CUDA, LFM, a different model size, model fine-tuning, a second Graphify entrypoint, a permanent provider router, a hidden fallback, an ontology redesign, a query-layer redesign, UI work, MCP redesign, or a new database abstraction.

Do not allow ordinary GLiNER, GLiREL, or GLiNER-Relex to remain as live production routes after promotion. Historical reports and one isolated offline Relex benchmark runner may remain.

## Operating loop

At the start of every work session:

```bash
python3 controller.py status
python3 controller.py next
```

For each stage:

1. Mark it started.
2. Perform only the work required by that stage.
3. Run its validators and repository tests.
4. Write a JSON receipt using `schemas/stage_receipt.schema.json`.
5. Complete the stage through `controller.py complete`.
6. Continue to the next ready stage without asking the user for routine decisions.

Commands:

```bash
python3 controller.py start STAGE_ID
python3 controller.py complete STAGE_ID --receipt path/to/receipt.json
python3 controller.py fail STAGE_ID --reason "observable failure"
```

## Kill-switch law

`S03_GOLD_ENTITY_SYNTAX_CEILING` is an architecture decision, not a late checklist item.

- Gold pair recall >= 0.65: grammar-only relations may proceed to the full benchmark.
- Gold pair recall from 0.50 through 0.649999: grammar is the fast path and semantic rescue is mandatory.
- Gold pair recall < 0.50: pure syntax-only replacement is prohibited; grammar-first plus semantic rescue is mandatory.

The stage receipt must expose `facts.gold_pair_recall`. The controller uses that fact to activate or skip the semantic-rescue stage.

## Core invariants

- Batch globally; adjudicate document-locally.
- Persist every raw model mention before corpus-dependent gating.
- GLiNER2 and spaCy receive byte-for-byte identical normalized text.
- Character offsets are never used as spaCy token indexes.
- A failed strict `Doc.char_span` alignment cannot authorize a graph assertion.
- Mention, document entity, and corpus entity are separate identities.
- Normalized surface equality alone never merges corpus entities.
- Recurrence is evidence, not truth; strong singletons remain possible.
- Word frequency is a weak genericity feature, never an unconditional veto.
- Decoy labels are not part of the initial production implementation.
- PhraseMatcher/EntityRuler completion is document-local and preserves every occurrence offset.
- Grammar proposes local relation pairs; document-wide all-pairs generation is prohibited.
- `related_to` is accepted only when the source explicitly expresses it; it is never a fallback.
- Surface predicates are durable and remappable without rerunning the model.
- Negative, modal, conditional, and attributed propositions remain qualified claims, not positive facts.
- No model output writes directly to Neo4j.
- Every candidate reaches a recorded terminal lane.
- Identical reruns are idempotent.

## Definition of done

Code compilation and unit tests are insufficient. Completion requires all of the following:

- CPU-only provider enforcement passes.
- No retired production model loads in the candidate path.
- Raw mention conservation passes.
- Entity reducer conservation passes.
- Relation decision conservation passes.
- One spaCy parse per eligible text is proven.
- Both committed Markdown fixtures run through the canonical Graphify entrypoint.
- Exact evidence and direction checks pass.
- Negation, modality, attribution, and open-relation lanes pass.
- Isolated Neo4j projection is deleted and rebuilt from Mongo artifacts with identical identity/count digests.
- A second full run creates no duplicate mentions, entities, assertions, nodes, or edges.
- Quality and speed reports satisfy the applicable gates.
- Existing unaffected tests remain green.
- Final status is emitted in the required JSON and Markdown reports.

## Evidence discipline

Never claim a gate passed without an artifact containing the command, exit code, metrics, and output location. When a result is uncertain, report the uncertainty and the exact missing evidence. Do not replace failed measurements with estimates.
