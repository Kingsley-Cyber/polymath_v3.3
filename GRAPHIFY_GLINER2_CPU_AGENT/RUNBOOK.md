# Graphify GLiNER2 CPU Refactor Runbook

## Target execution flow

```text
canonical Graphify entrypoint
  -> source normalization with reversible offsets
  -> zero-model survey
  -> structure-aware entity windows
  -> one warm GLiNER2 Base CPU entity census
  -> immutable raw mentions
  -> document-local entity reduction
  -> document-local mention completion
  -> relation-eligibility decisions
  -> one spaCy parse per eligible text
  -> DependencyMatcher / FrameExtractor / SVO / dep-path relation proposals
  -> durable surface-predicate records
  -> startup-compiled predicate and endpoint-signature maps
  -> assertion qualification and terminal lanes
  -> authoritative persistence
  -> isolated graph projection
  -> projection rebuild and idempotency verification
```

## S00 — Bootstrap

- Initialize the controller.
- Verify the pack.
- Capture repository HEAD, branch, and dirty state.
- Create an isolated artifact namespace for this run.

Pass evidence: controller state, pack self-check, fixture validation.

## S01 — Repository discovery and change map

Discover actual repository seams rather than guessing filenames:

- Graphify command/API entrypoint.
- Extraction orchestrator and provider registry.
- Every GLiNER, GLiREL, Relex, and model-loader route.
- spaCy loader and every `nlp(text)` or `nlp.pipe` owner.
- DependencyMatcher, FrameExtractor, SVO, dep-path, coordination, apposition, voice, negation, modality, attribution, and nominalization logic.
- Entity quality, canonicalization, alias, and clustering logic.
- Predicate maps, endpoint signatures, acceptance policies, output lanes.
- Mongo repositories, Neo4j writer, control-plane ledger, tests, fixture conventions, and benchmark commands.

Create a change map with: current component, owner, retained/extended/replaced/removed classification, downstream consumers, and tests protecting the seam.

Populate `.agent_state/extraction_scope.json` from `templates/extraction_scope.template.json`.

## S02 — Frozen executable baseline

Keep the existing Relex path executable offline. Run the current canonical pipeline over both committed fixtures before refactoring. Save immutable outputs, logs, stage timings, counts, graph digests, and environment/model/configuration hashes.

Two identical baseline invocations must produce stable identity/count digests. Do not edit the stored baseline.

## S03 — Gold-entity syntax ceiling

Feed gold mention spans from the quality fixture into the complete current deterministic relation stack. Measure pair recall and directed triple metrics. Write `facts.gold_pair_recall` into the receipt.

This stage decides whether pure syntax is viable or semantic rescue is mandatory. It does not cancel the entity-census refactor.

## S04 — Survey, normalized offset space, and contracts

Implement a no-model survey that inventories repeated blocks, structural positions, heading paths, references/TOC/index/furniture candidates, capitalization and identifier shapes, explicit acronym/alias/definition patterns, frequency features, and survey-derived gazetteer candidates.

Furniture suppression requires multiple signals. Survey-derived gazetteer entries are weak candidates. Canonical entity types remain the repository's frozen inventory.

Define typed/versioned records for normalized documents, extraction windows, raw mentions, document entities, completed mentions, surface relations, assertion decisions, stage receipts, and benchmark results.

One normalized character space must support both GLiNER2 and spaCy, with reversible original offsets.

## S05 — CPU-only GLiNER2 provider

Replace the production semantic entity provider with `fastino/gliner2-base-v1` on CPU. Load exactly one warm instance. Assert every parameter/tensor is on CPU. Prohibit silent accelerator or provider fallback.

Use versioned natural-language type descriptions. Hash checkpoint, label set, descriptions, thresholds, normalization release, and survey release.

Retain Relex only in the isolated baseline runner.

## S06 — Corpus-batched entity census and raw mention conservation

Build structure-aware 512–1024-token candidate windows; sweep only if required inside CPU, without introducing new deployment paths. Length-bucket, batch with initial size 8, preserve deterministic order, and persist raw mentions immediately.

For every model span:

- Validate local bounds and exact surface equality.
- Translate to normalized document offsets.
- Validate document-level surface equality.
- Translate to original offsets where representable.
- Record explicit alignment failures.

Conservation: emitted mentions = persisted raw mentions + explicit alignment failures.

No spaCy, embeddings, graph writes, or other model inference runs during the census stage.

## S07 — Document entity reducer

Cluster mention evidence document-locally before cross-document linking. Use normalized surface, type, context, section, domain, definitions, aliases, neighboring mentions, and later relation signatures.

Promotion routes include curated/explicit gazetteer evidence, definitions, aliases/acronyms, high-confidence specific singletons, repeated coherent mentions, stable type, and trusted relation participation.

Negative signals include pronouns/deictics, furniture, generic single nouns, frequency, missing name shape, missing definition/gazetteer support, unstable type, low coherence, malformed spans, and low per-type confidence.

Word frequency is one feature. Do not require two mentions. Do not cap valid entities per document. Every aligned raw mention ends in promoted, document-local, review, or suppressed state.

## S08 — Document-local mention completion

Compile promoted document names, exact aliases, acronyms, curated variants, and explicit document variants into PhraseMatcher/EntityRuler patterns. Scan the complete normalized document and create a distinct mention record for every exact occurrence.

Ambiguous names such as Go, Make, Apple, Python, Oracle, and Rust require contextual rules. Completion must not convert ordinary word uses into entities.

Measure completion precision and recall on the quality fixture.

## S09 — Relation eligibility, parse-once, grammar fast path, and predicate compiler

Persist relation-eligibility decisions. Eligible inputs include two promoted mentions or one promoted mention plus a definition, metric, temporal, attribution, alias/apposition, or trusted relation-bearing cue. Exclude/reroute structural noise and code.

Use one lightweight spaCy `Doc` per eligible text through `nlp.pipe`. Statistical NER is disabled. All relation consumers reuse the same `Doc`; none may invoke spaCy again.

Convert character spans through strict `Doc.char_span`. Alignment failure is terminal for graph admission.

Generate local pairs from grammar frames, never from document-wide entity combinations. Persist lemma, particle, preposition, dependency frame/path, voice, polarity, modality, attribution, exact endpoints, exact evidence, canonical candidate, mapping rule, and release.

Compile predicate maps and endpoint signatures once at startup. Accept a canonical predicate only when evidence, direction, type signature, scope, assertion status, and mapping are unambiguous. Otherwise retain an open surface relation or review state. Never force `related_to`.

Track p50/p95/p99/max mentions and pairs, coordination expansion, and relation-eligible rate.

## S10 — Conditional semantic rescue

This stage is automatically required when `gold_pair_recall < 0.65`.

Use the same GLiNER2 Base CPU checkpoint. Trigger only for promoted endpoints plus a relation cue when grammar produced no usable pair or mapping is ambiguous. Initial hard budget: at most 15% of relation-eligible windows. Overflow becomes `DEFERRED_SEMANTIC_RESCUE`, never a silent drop.

Semantic endpoints must align to promoted local mentions and pass scope, type, polarity, modality, attribution, and predicate checks. Semantic-only candidates are review-only until independently calibrated; grammar/semantic agreement can enter the corroborated lane.

## S11 — Graphify orchestration and state ledger

Preserve the public Graphify entrypoint. Internally emit stage receipts with input/output hashes, release pins, timings, counts, errors, warnings, conservation checks, status, and retry metadata.

Required progression:

```text
DISCOVERED -> NORMALIZED -> SURVEY_COMPLETE -> ENTITY_CENSUS_COMPLETE
-> ENTITY_REDUCTION_COMPLETE -> MENTION_COMPLETION_COMPLETE
-> RELATION_ELIGIBILITY_COMPLETE -> RELATION_COMPILATION_COMPLETE
-> ASSERTION_VALIDATION_COMPLETE -> TEST_PROJECTION_COMPLETE -> E2E_VERIFIED
```

Fail closed. Resume deterministically. Identical reruns may not duplicate artifacts.

## S12 — Two-document E2E and rebuild

Populate `.agent_state/run_config.json` from its template. Run `scripts/run_e2e.py` so the canonical Graphify command processes both committed fixtures. Execute candidate runs twice. Execute the frozen Relex baseline where configured.

Verify exact evidence, entity lookup, relation lookup, direction, qualified claims, open relations, suppression of known junk, graph uniqueness, graph deletion/rebuild, and identical identity/count digests on rerun.

Emit machine-readable benchmark results conforming to `schemas/benchmark_result.schema.json`.

## S13 — Runtime cleanup and documentation

Only after candidate E2E gates pass, remove live ordinary GLiNER, GLiREL, and Relex provider routes, aliases, fallbacks, eager loads, and dead production switches. Retain the isolated offline Relex baseline runner and historical artifacts.

Update architecture/operations docs and dependency manifests. Run the provider and CPU-policy scans.

## S14 — Final verification

Run all relevant unit, integration, E2E, conservation, policy, rebuild, idempotency, and unaffected regression tests. Evaluate candidate metrics against `acceptance_gates.json`. Generate final JSON and Markdown reports.

Do not promote production graph writes from the two fixtures alone. Continue through closed-world development calibration and document-separated held-out qualification under the repository's existing release policy.
