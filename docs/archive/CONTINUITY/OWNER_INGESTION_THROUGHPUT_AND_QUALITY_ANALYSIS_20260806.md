# Owner Analysis — Ingestion Throughput & Semantic Quality (2026-08-06)

**Status:** OWNER CURRENT ANALYSIS + **WAY AHEAD** (GLiNER2 entity-census architecture, 2026-08-06).
**Authority:** Owner-authored diagnosis after Phase 0–7 + refined architecture verdict (execute globally, decide locally).
**Pairs with:** `SPEED_BENCH_CLOSEOUT_AND_PERFORMANCE_PLAN_20260805.md` (**implementation tracker — G0–G13**), `KNOWLEDGE_PIPELINE_LINEAGE_MAP_20260804.md`, `PIPELINED_INGESTION_CLOSEOUT_20260805.md`, `MPS_BATCH_QUALIFICATION_20260805.md`.
**Production candidate:** GLiNER2 census → doc reducer → mention completion → selective spaCy → existing predicate/gate.
**Frozen baseline:** Relex recoverable; not the production optimization target.

## BLUF

Two separate problems (do not conflate):

1. **Problem 1 — Extraction quality** — bad entities and low-information relations entering the durable graph.
2. **Problem 2 — Extraction throughput** — too many expensive semantic inference units on one M1 GPU.

```yaml
pipeline_health:
  parsing_and_chunking: strong
  deterministic_runtime: strong
  extraction_throughput: weak
  entity_quality: uneven
  relation_utility: uneven
  higher_summary_quality: weak
  ontology_vocabulary_compliance: strong
  ontology_semantic_enforcement: partial
  graph_structure: strong
  graph_reasoning_quality: moderate
```

**Central conclusion (owner 2026-08-06 afternoon):** Quality gates alone cannot remove Relex pair-matrix cost. **Way ahead is correct and likely faster:** corpus-batched GLiNER2 entity census + document-local adjudication + grammar relations (no all-pairs). Exact speedup unverified. Highest levers = GLiNER2 batched entity-only + selective spaCy + vector-first Pass-1 + windows. Primary risk = relation recall vs old syntax ceiling — requalify with gold entities before flip.

**Principle:** You do not need every raw extraction to be correct. You need every graph write to be sufficiently justified.

## Measured evidence (optimized bench, corpus `d9f87093`)

| Phase | Total (5 docs) | Share of ~31 min |
|---|---:|---:|
| ghosts / Relex | ~15.4 min (922s) | ~49% |
| neo4j | ~8.8 min (528s) | ~28% |
| embed | ~4.5 min (269s) | ~14% |
| qdrant + other | ~1.9 min | ~6% |

*Fundamentals of Data Engineering* alone: 1,695 children → **542s Relex** (~9 min), then Neo4j 235s, embed 177s.

Corpus totals:
```yaml
child_chunks: 2736
extraction_rows: 2704
entity_mentions: 57499
accepted_relations: 413
accepted_relations_per_chunk: ~0.15
parent_summaries_nonempty: 1360 / 1384
relex_batch_decision: retain_batch_1  # gold flips=99, long-chunk speedup=0
```

Central coupling:
```text
one retrieval child chunk
→ one spaCy analysis
→ one serial GLiNER-Relex inference
→ one extraction artifact
```

Polymath pays full relation-extraction cost on almost every child while receiving an accepted relation from only a small minority of them.

---

# Throughput shortcomings

## 1. Retrieval granularity and extraction granularity are incorrectly coupled

Small child chunks are useful for precise vector retrieval, citations, MMR, local hydration, and parent-child context — but not necessarily the most efficient unit for Relex inference.

Current:
```text
1,695 retrieval children → approximately 1,695 Relex calls
```

Better conceptual separation:
```text
Retrieval unit  → small child chunks
Extraction unit → bounded semantic windows of adjacent children
Citation unit   → original child spans
```

Example:
```text
Parent
├── Child 1
├── Child 2
├── Child 3
└── Child 4
One bounded extraction window → GLiNER-Relex once
→ map entities/relation evidence back to Child 1–4
```

**Constraint:** Not a silent change. Larger context can change entity/relation decisions; more entities can increase ordered-pair scoring. Requires a separate extraction-release benchmark + gold parity.

## 2. Batch-one Relex is a hard throughput ceiling

```yaml
batch_sizes_tested: [4, 8, 16]
gold_threshold_flips: 99
long_chunk_speedup: 0
decision: retain_batch_1
```

Credible next Relex optimizations (only):
1. Reduce the number of extraction units.
2. Avoid full relation scoring on obviously low-value chunks.
3. Optimize the inference implementation itself.
4. Use a separate faster ingestion device while keeping the same model contract.

Simply adding more workers on one M1 will likely increase MPS contention rather than throughput.

## 3. The extraction path does too much uniform work

Deterministic preclassification lanes:
```yaml
full_relation_lane:
  - multiple compatible entities
  - finite predicate or dependency relation
  - definition or causality language
  - relation-bearing prose
entity_only_lane:
  - named concepts or identifiers
  - little evidence of a relationship
skip_lane:
  - TOC / bibliography / headers / boilerplate / navigation
  - sparse heading-only text / known noise
```

**Key question:** Can Relex execute a cheaper **entity-only mode** without full ordered-pair relation scoring? If yes — safest next model-side experiment.

A spaCy-derived prefilter may nominate relation-bearing text, but **must** be evaluated against the accepted-relation gold set. Missing one important relation to gain speed is an unacceptable trade.

---

# Semantic quality shortcomings

## 4. Entity volume is high, but entity quality is uneven

57,499 entities is not automatically a strength. Observed weaknesses:
```text
successor / directive / this approach / owner / end goal
operatorauthorized account
experi- ence / Data\nengineers
```

Problems: generic nouns as entities; broken line-wrap surfaces; descriptions as identity-like records; incomplete phrase normalization; duplicate nested surfaces; incorrect types (concepts labeled `software`); near-identical endpoints surviving consolidation.

**For Graph RAG:** one clean entity > five overlapping/malformed variants.

Required quality work:
```text
surface repair
→ generic-entity rejection
→ nested-span consolidation
→ type compatibility
→ document identity
→ corpus identity
```

## 5. Accepted relation density is sparse

```yaml
child_chunks: 2736
accepted_relations: 413
accepted_relations_per_chunk: ~0.15
```

Sparse is not necessarily incorrect (conservative gate). Cost-vs-value concern remains:
```text
2,704 expensive extraction executions → 413 accepted relationships
```

Deterministic relation-likelihood routing is worth investigating.

## 6. Some accepted relations are low value or semantically weak

Examples:
```text
owner → owns → end goal
Taylor & Francis → produces → information
repository owner → owns → agreement
```

Separate:
```yaml
valid_relation:
  source_supported: true
useful_graph_assertion:
  source_supported: true
  endpoint_quality: high
  predicate_specificity: meaningful
  likely_query_value: meaningful
```

Add **graph-utility classification** without deleting source-backed assertions:
```yaml
assertion_utility: [core, supporting, generic, administrative, noisy]
```
Graph traversal prioritizes `core` and `supporting`.

## 7. Ontology compliance ≠ ontology quality

```yaml
oov_entity_type_rate: 0
oov_predicate_rate: 0
```

Proves vocabulary fit only — not type correctness, best predicate mapping, domain/range, traversal worth, or well-formed endpoints.

Gate must operate as:
```text
candidate relation
→ canonical predicate
→ endpoint type compatibility
→ domain/range validation
→ semantic specificity
→ acceptance lane
```

---

# Summary shortcomings

## 8. Parent summaries are functional, but not yet truly child-aware

Current parents: Topic + Content count + extractive key-point sentences (mostly from parent text). Children mostly influence inventory counts, not evidence selection.

Next algorithm — **hybrid C** (owner-preferred):
```text
C = sentence MMR + obligation/claim coverage
```

### Deterministic parent summary v2
```text
Children under parent
→ collect candidate source sentences
→ attach: entities, accepted assertions, definitions, dates, quantities, source child ID
→ score relevance to heading
→ group by semantic obligation/entity cluster
→ select diverse evidence using MMR
→ maximum contribution per child
→ render bounded deterministic summary
```

Must be a **new version**, not in-place mutation:
```yaml
algorithm: deterministic_parent.v2
```

## 9. Section and rollup summaries are not actual summaries

Current behavior is topic-index concatenation, not semantic rollup.

Section algorithm should aggregate **structured parent information records**:
```text
Parent summaries + entity clusters + accepted assertions + definitions + dates
→ deduplicate → rank major obligations → preserve contradictions → deterministic section rollup
```

## 10. Document summaries are title stubs

`Topic: Fundamentals of Data Engineering.epub.` has almost no retrieval value.

Proper deterministic document profile:
```yaml
document_profile:
  purpose:
  primary_domains:
  major_sections:
  core_entities:
  core_assertions:
  definitions:
  important methods:
  dependencies:
  contradictions:
  temporal_scope:
  representative_child_ids:
```

Not elegant prose — a useful routing representation of the whole document.

---

# Graph shortcomings

## 11. Neo4j can traverse, but traversal quality is bounded by endpoint quality

Structure is correct:
```text
Chunk → SUPPORTS_ASSERTION → RelationAssertion
Entity → SUBJECT_OF → RelationAssertion → OBJECT → Entity
```

Filter traversal by:
```yaml
minimum_graph_quality:
  entity_specificity:
  assertion_utility:
  predicate_specificity:
  evidence_completeness:
  type_compatibility:
  confidence_or_corroboration:
```

## 12. Multi-hop is possible, but not uniformly reliable

Concern is composition quality, not Neo4j walkability. Query layer should: prefer core assertions; penalize generic entities; reject missing child support; restrict predicate families; cap hubs; preserve contradictions; never infer transitivity unless ontology rules permit.

---

# What is already good — do not rebuild

```yaml
parsing: {speed: good, structure: good}
chunking: {speed: good, parent_child_model: useful}
deterministic_summary_runtime: {speed: excellent, cost: zero}
embedding: {speed: acceptable}
qdrant: {speed: good}
control_plane: {recovery: substantially_fixed, graph_jobs: durable, pass_overlap: working}
```

---

# Correct priority order (speed first)

| Pri | Work | Type | Gate |
|---:|---|---|---|
| **0** | Vector-first Pass-1 (Qdrant before Relex) | throughput (user-visible) | searchable ≤5 without waiting on Relex |
| **1** | Profile Relex call internally | throughput | receipt artifact |
| **2** | Cache invariant model inputs | throughput | parity vs batch-1 gold |
| **3** | Relation-likelihood routing (full / entity-only / skip) | throughput | `accepted_relation_recall: 1.0` |
| **4** | Entity-bounded extraction windows | throughput (highest Relex lever) | new extraction-release gold; soft≤20 / hard≤30 ents |
| **5a** | Label definitions + per-type thresholds (no decoys yet) | quality | gold re-qualify; **before next large extract** |
| **5b** | Specificity-ladder entity gate on ALL Relex entities | quality | named counters; preserve `pandas`/`curl`/`redis` |
| **5** | Entity consolidation (mention→doc→corpus) | quality (+ Neo4j) | no global merge on surface alone |
| **5c** | EntityRuler/SpanRuler mention union | quality assist | never sole NER |
| **5d** | Full syntax lane (DependencyMatcher ∪ FrameExtractor ∪ SVO + qualifiers + appos/coord/alias) | quality (+ bounded recall) | surface record retained; union with Relex |
| **5d.1** | Preserve surface relation (lemma/particle/prep/voice/canonical_candidate) | quality / ontology agility | rematch without re-extract |
| **5d.2** | Compile endpoint signatures before runtime | quality / gate speed | VALID/AMBIGUOUS/INVALID lookup only |
| **5e** | Decoy labels — **ablation only** | experiment | entity F1 + pair-count; not production-first |
| **6** | Deterministic summary v2 | quality | config-hash bump; not v1 mutation |
| **Bake-off** | A/B/C/D on frozen windows | decision | C must beat B on precision/recall/ungrounded/speed |

Priority 6 is primarily quality, not speed.
Quality order: **5a → 5b → 5 → 5c → 5d**; **5e last as ablation**. Do not run a large re-extract before 5a lands.

---

# Entity quality fix ladder — refined 2026-08-06 (supersedes earlier “labeling bug only” framing)

## Principle

```text
model candidates
  ∪ syntax candidates
  ∪ EntityRuler / acronym / definition spans
  → entity quality (specificity ladder)
  → type / scope / direction validation
  → evidence arbitration
  → accepted facts | review | open relations | rejects
```

Raw Relex may stay noisy. Durable Neo4j writes must be justified.

## Observed problems → correct fix

| Observed | Actual cause (multi-factor) | Correct fix | Expected |
|---|---|---|---|
| `successor` → SOFTWARE | Broad label + priors + low threshold + generic noun | Better label wording + per-type threshold + specificity gate | Reject or downgrade as software |
| `directive` → SOFTWARE | Generic concept attracted to software | Named-product pattern / gazetteer / version / definition | Usually reject as software |
| `this approach` → SOFTWARE | Deictic/generic phrase | Pronoun/deictic endpoint reject | Rejected |
| `experi-\nence` | Line-wrap / cleanup | Dehyphenation **before** extract + reversible offsets | Corrected span |
| `Data\nengineers` | Layout break inside phrase | Layout normalization before tokenization | Corrected span |
| `owner -[owns]→ end goal` | Generic endpoints; low info | Specificity + graph-utility gate | No canonical edge |
| `Taylor & Francis -[produces]→ information` | Syntax OK; generic object / broad predicate | Endpoint-type + object-specificity | Downgrade or reject |
| Generic two-hop chains | Low-specificity nodes survived | Accepted-lane filter + evidence hydration at query | Not used as precise evidence |

## Correction: “labeling bug only” is overstated

`"successor" → SOFTWARE` may be **any combination of**: broad label wording, model training priors, technical-corpus context, low global threshold, missing competing types, bad document cleanup, or a real model classification error.

Label wording is a **valid, versioned model-configuration intervention** (GLiNER-Relex supports natural-language label descriptions) and must be **measured on the closed-world development set**. It is not a guaranteed cure.

## 5a — Label definitions + per-type thresholds (WHAT / HOW / WHY)

### WHAT
1. Improve label definitions (e.g. `software` → `named software product, executable tool, library, framework, platform, or application`).
2. Calibrate **per-type thresholds** from the frozen development split. Illustrative only (not final):

```yaml
entity_thresholds:  # ILLUSTRATIVE — calibrate on frozen split
  person: 0.45
  organization: 0.50
  location: 0.50
  dataset: 0.58
  software: 0.68
  concept: 0.72
  process: 0.74
```

One global threshold lets broad categories absorb too much of the corpus.

### HOW
Version bump `extractor_release` / schema hash → edit sidecar `ENTITY_LABELS` + threshold map → gold re-qualify (span/type/relation parity).

### WHY
Changes the attractor and the accept bar before Python mopping. Must land before the next large extract so junk is not baked.

### Explicitly NOT in 5a
**Decoy labels** (`generic technical term`, `abstract concept`, `common noun`) are **5e ablation only**.

## 5b — Specificity ladder (WHAT / HOW / WHY)

### WHAT
Entity-quality gate on **all** Relex entities (not relation-anchor-only). **Do not** use “reject every lowercase common noun” — legitimate tech names include `pandas`, `curl`, `make`, `go`, `redis`, `transformers`.

### HOW — strong automatic acceptance signals
```text
EntityRuler / curated gazetteer match
explicit acronym or long-form/acronym pair
proper name / versioned name / product naming pattern
quoted or titled expression
explicit definitional construction
repeated technical multiword term
known canonical alias
high model score + compatible context
```

### HOW — downgrade signals
```text
single lowercase common noun
pronoun or demonstrative phrase
generic head noun: system, approach, method, process, data, information, goal, owner, successor
no modifiers / no definition / no repetition / no gazetteer / no trusted relation participation
```

A generic term may still be meaningful when explicitly defined. `successor` is **not globally forbidden** — it fails automatic *software* acceptance unless context establishes a named/defined technical object.

Also: layout dehyphenation + newline collapse **before** extraction with reversible offset mapping.

### WHY
Protects graph writes without killing real tool names.

## 5 — Consolidation

Mention → document entity → corpus entity. Nested-span merge + type compatibility.
**Do not** let `normalized_surface == normalized_surface` alone create a global merge.

## 5c — EntityRuler / SpanRuler (WHAT / HOW / WHY)

### WHAT
Deterministic patterns (`{"label":"TOOL","pattern":"Qdrant","id":"qdrant"}` → `ent.ent_id_`) as **one source** in the mention union:

```text
Relex spans ∪ EntityRuler/SpanRuler ∪ acronym spans ∪ definition spans
```

### HOW
Periodic Neo4j/curated freeze → JSONL patterns → spaCy ruler. Prefer disable spaCy statistical NER; retain tagger/tok2vec, parser, lemmatizer, sents, EntityRuler/SpanRuler, DependencyMatcher. Relex remains the broad statistical entity system.

### WHY / limits
Gives deterministic canonicalization **for exact curated patterns only**. Does **not** solve ambiguous names, same-name different entities, versioned products, org-vs-product, contextual aliases, or cross-doc coreference. A spaCy EntityRuler **cannot** overwrite Relex’s internal predictions. `before="ner"` is not the key issue here.

**Status today: EntityRuler/PhraseMatcher gazetteer-from-Neo4j is NOT running.**

## 5d — Full syntax lane (WHAT / HOW / WHY)

### WHAT — components (all first-class, not “matcher only”)

| Component | Role |
|---|---|
| **DependencyMatcher** | High-precision pattern lane for bounded ontology predicates |
| **FrameExtractor** | Construction / frame candidates (prep, voice, argument roles) |
| **SVO** | Native subject–verb–object candidates on the same spaCy Doc |
| **negation** | Polarity — do not accept asserted facts from negated clauses as positive |
| **modality** | ASSERTED / HYPOTHETICAL / CONDITIONAL — gates acceptance lanes |
| **voice** | active / passive / nominal — direction and role assignment |
| **apposition** | Typed appos observations → alias / definition signals |
| **coordination** | Split coordinated arguments without inventing false pairs |
| **alias extraction** | Appos + acronym + definition → mention union / identity (not sole NER) |

Same spaCy Doc once per chunk/window. DependencyMatcher and SVO share `source_family=spacy_dependency_parse` — agreement between them is **not** independent corroboration of Relex.

### HOW — union with Relex (unchanged law)

```text
Relex semantic candidates
  ∪ DependencyMatcher / FrameExtractor / SVO candidates
  → entity + relation gate (specificity, type, direction, evidence)
  → ACCEPT_CORROBORATED | ACCEPT_SYNTAX_HIGH | ACCEPT_RELEX_HIGH | REVIEW_* | REJECT_*
```

### 5d.1 — Preserve the surface relation (REQUIRED)

Always retain the surface form alongside the canonical mapping:

```json
{
  "surface_predicate": "was built on top of",
  "predicate_lemma": "build",
  "particle": null,
  "preposition": "on top of",
  "voice": "passive",
  "canonical_candidate": "depends_on"
}
```

**WHY**
- Mapping errors are auditable (`built on top of` → `depends_on` can be challenged without re-reading the model).
- Ontology / synonym tables can improve by rematching **surface → canonical** without re-extracting source text or re-running Relex.
- Voice/prep/particle explain direction and argument-role decisions when the gate or humans review.

Repo today (`SyntaxEvidence`): already has `surface_predicate`, `canonical_predicate`, `voice`, `negated`, `modal`. Gap to close: durable persistence of lemma/particle/preposition/`canonical_candidate` on the stored extraction/assertion artifact (not only in-memory gate structs), and consistent fill from FrameExtractor + DependencyMatcher + SVO.

### 5d.2 — Compile endpoint signatures before runtime

**WHAT**
Per-predicate endpoint type zones (VALID / AMBIGUOUS / INVALID) compiled once at process start from `ontology.yaml` `allowed_pairs` + `relation_acceptance.yaml` — not rediscovered per chunk.

**HOW**
```text
ontology.yaml allowed_pairs
  + acceptance INVALID/AMBIGUOUS zones
        ↓ compile at sidecar/worker startup
in-memory signature tables
        ↓ runtime O(1) lookup
GateStatus.REJECT_ENDPOINT_SIGNATURE | REVIEW_TYPE_COMPATIBILITY | continue
```

**WHY**
- Deterministic, cheap, auditable type-pair policy.
- Separates **policy compilation** from **inference**. Post-inference type-pair filtering improves precision/graph volume; it does **not** speed the released Relex all-pairs scorer (see above).
- Repo already rejects via `reject_endpoint_signature` in `corroboration_gate` — doctrine is: those tables must be **precompiled**, version-stamped, and shared — never rebuilt ad-hoc mid-batch.

### Status vs lineage map

`KNOWLEDGE_PIPELINE_LINEAGE_MAP_20260804.md`: FrameExtractor / DependencyMatcher / SVO / corroboration_gate **exist**. Drift is caller disconnection + incomplete durable surface/alias packaging — **do not rebuild** the syntax stack; wire + persist the surface record and ensure signature compile-at-start.

## 5e — Decoy labels = ablation only (correction)

Earlier research overstated decoys as a strong production fix.

- Thresholds already allow spans to remain unselected; decoys are **not** a true NONE class.
- Relex `"other"` is for relation-bearing entities whose exact type was not supplied — **not** a garbage/rejection class.
- Costs: more label-prompt tokens, more emitted candidates, more confusing boundaries, **possibly more entity pairs** (relation cost grows **quadratically** with entity count).

**Order:** (1) improve label definitions (2) calibrate per-type thresholds (3) specificity gates (4) EntityRuler (5) **test decoys only as ablation**. Do not add decoys to the production ontology without measuring entity F1 and downstream pair-count effects.

## Type-pair allowlists — precision ≠ released-Relex speed

Released GLiNER-Relex enumerates **all ordered pairs** `E×(E−1)` internally; adjacency pruning is **not** active in the released checkpoint.

```text
10 ents → 90 pairs
20 → 380
25 → 600
30 → 870
× 30 predicates → 18k–26k pair-predicate scores
```

Post-inference type-pair allowlist → precision + graph-volume win; **little or no encoder/relation inference speedup**. Real inference speedup needs pruning **inside** relation scoring. Claim of routine “3–10× Relex speedup” from a Python type-pair allowlist is **not established** for the released checkpoint.

Practical controls on current model: fewer windows, bounded entities/window, smaller predicate packs, better entity precision, adaptive split of entity-dense windows.

## Query-time: generic two-hop paths

Do not answer from `DevOps engineers → data → streaming source` alone. Treat path as retrieval hint → hydrate exact evidence for each edge → verify identity/predicate → synthesize or reject. Prefer edges from `ACCEPT_CORROBORATED`, `ACCEPT_SYNTAX_HIGH`, qualified `ACCEPT_RELEX_HIGH`.

## Owner decisions still open

- Void corpus `7d801816` disposition.
- Entity-only Relex (Pri 3) only after `accepted_relation_recall: 1.0`.
- Extraction-window activation only after gold gate (Pri 4).
- Liquid bake-off stays shadow until replacement gates pass.
- Decoy labels stay ablation-only (Pri 5e).

---

# Owner performance targets (same five files, 32 GB M1 Max) — 2026-08-06

## BLUF

Realistic M1 Max target: **~9–13 minutes for complete enrichment**, with the corpus **searchable in ~4–6 minutes** if Qdrant no longer waits for Relex.

A full completion time **below five minutes is not plausible** on this Mac while preserving the current GLiNER-Relex quality contract. Embedding alone consumed ~4.5 minutes, and Relex + embedding share the same MPS device.

## Why Relex-only cannot hit 5–10 min full completion

| If Relex becomes… | Full wall ≈ |
|---|---:|
| 2× faster | 31 − 15.4 + 7.7 ≈ **23.3 min** |
| 3× faster | 31 − 15.4 + 5.1 ≈ **20.7 min** |
| Instant (0) | 31 − 15.4 ≈ **15.6 min** |

**Conclusion:** Relex alone cannot reduce complete enrichment to 5 or 10 minutes. Neo4j and embedding must shrink **or** leave the blocking critical path (queryable-before-Relex / overlap).

## Plausible targets by optimization level

### Level 1 — Low-risk Relex improvements only

Cache label/ontology inputs; remove unnecessary sync; entity-only for some chunks; deterministic skip for noise; still one child per inference unit.

```yaml
Relex: 9_to_11_minutes
Neo4j: 7_to_9_minutes
Embedding: 4_to_5_minutes
full_completion: 20_to_24_minutes
```

Safer, but does not solve scalability.

### Level 2 — Recommended architecture (entity-bounded extraction windows)

Keep small children for retrieval/citation; group adjacent children into **entity-bounded** extraction windows; one Relex pass per window; map spans back to exact children. Do **not** Relex an entire large parent blindly — entity-pair count can explode.

```yaml
current_Relex_units: 2704
target_extraction_windows: 700_to_1000
extraction_window:
  sentences: {target: 2_to_5}
  words: {target: 150_to_400}
  entities: {soft_limit: 20, hard_limit: 30}
  split_on: [sentence_boundary, heading_boundary, entity_density, table_or_code_boundary]
  skip: [navigation, repeated_headers, boilerplate, copyright_furniture, empty_fragments]
```

| Stage | Plausible target |
|---|---:|
| Relex + spaCy | 4–6 min |
| Embedding | 4–4.5 min |
| Neo4j | 2–4 min |
| Other | ~1 min |

With Neo4j overlapping other docs / not blocking Qdrant:
```yaml
searchable_after_Qdrant: 4_to_6_minutes
assertion_extraction_complete: 8_to_11_minutes
fully_graph_enriched: 9_to_13_minutes
```

**Most credible target on the existing M1 Max.**

### Level 3 — Stretch

Windows ~600–800; perfect accepted-relation recall on routing; early reject of malformed/generic entities; smaller graph writes; corpus-batched Neo4j cert; continuously fed pipelines:

```yaml
searchable: 3_to_5_minutes
fully_enriched: 7_to_9_minutes
```

Requires measured extraction + retrieval parity. Not the default commitment.

## Honest success SLOs (same five files)

```yaml
target:
  searchable: <= 5_minutes
  hybrid_ready: <= 6_minutes
  extraction_complete: <= 10_minutes
  graph_ready: <= 12_minutes
  total_hard_ceiling: 15_minutes
quality:
  extraction_decision_regressions: 0
  lost_gold_relations: 0
  unsupported_new_relations: 0
  citation_span_failures: 0
```

Most reasonable success result:

> **About five minutes to search the documents and approximately ten to twelve minutes for complete Relex and Neo4j enrichment.**

Fully enriched **below five minutes** requires a substantially faster extraction runtime or a separate GPU ingestion machine.

## Entity quality also affects speed

```yaml
entities: 57499
accepted_relations: 413
```

Neo4j processes tens of thousands of mentions/entities for only 413 accepted relations. Removing duplicate nested entities, malformed line-wraps, generic endpoints, mislabeled descriptions, and duplicate canonical variants can cut graph records **~25–50%** without losing useful assertions — semantic win + Neo4j wall reduction.

## Summaries are not a speed problem

Python summaries took **< ~1 min** total. Parent/section/document quality work (`deterministic_parent.v2` → section rollups → document profiles) proceeds **independently** as quality repair, not a performance bottleneck.

---

# M1 Max Relex scheduling model

```yaml
relex_runtime:
  loaded_model_instances: 1
  active_MPS_forward_passes: 1
  input_prefetch_depth: 2_to_4
  CPU_tokenization_workers: measured_2_to_4
  spaCy_workers: measured_2_to_4
  output_gate_workers: measured_2_to_4
  bounded_result_queue: true
```

Conceptually:
```text
CPU tokenizer queue
        ↓
ready tensors
        ↓
single Relex MPS consumer
        ↓
decoded outputs
        ↓
parallel Python arbitration workers
```

Eliminates GPU idle gaps **without** MPS contention.

## What can still provide a speedup (around Relex, not inside concurrent Relex)

1. **Pre-tokenization and prefetch** — prepare next 2–4 chunks before the model needs them. Removes CPU/input gaps; does not reduce encoder compute.
2. **Cache invariant label encodings** — entity/relation label representations, ontology descriptions, predicate-pack encodings once per process (Pri 2).
3. **Avoid unnecessary MPS synchronization** — one complete forward → one required sync → batched output transfer; not many sync points inside one inference.
4. **Relationship-likelihood routing** — full entity+relation vs entity-only (no relation labels / disable relation return) vs skip. Must achieve `accepted_relation_recall: 1.0` / `lost_gold_relations: 0` before activation. Encoder may still dominate; measure.
5. **Extraction windows** — largest plausible lever: fewer encoder calls, not simultaneous Relex on MPS.

## Only concurrency experiment worth running

Narrow sweep — concurrency **around** Relex supply, not assumed multi-forward:

```yaml
concurrency_sweep:
  active_Relex_requests: [1, 2, 3]
fixed:
  batch_size: 1
  same_chunks: true
  same_order: true
  same_model: true
  same_thresholds: true
  no_embeddings_or_reranker_running: true
metrics:
  - total_chunks_per_second
  - p50_chunk_latency
  - p95_chunk_latency
  - MPS_utilization
  - GPU_power
  - peak_unified_memory
  - swap
  - thermal_throttling
  - output_decision_parity
acceptance:
  minimum_throughput_gain: 1.15
  decision_regressions: 0
  OOM_events: 0
  swap_growth: negligible
```

Expected (measure, do not assume):
- concurrency 1 ≈ best or close
- concurrency 2 ≈ flat or slightly worse
- concurrency 3 ≈ worse + more memory

## What “maxing the GPU” means

Do **not** aim for 100% GPU utilization at any cost.

Aim for:
```text
maximum verified chunks per second
with zero extraction decision changes
and zero memory instability
```

Higher utilization with padding, request switching, label recomputation, duplicate work, or swap is not success.

## Final scheduling doctrine

> **Increase concurrency around Relex, not inside Relex.**
>
> Use parallel CPU preparation, spaCy, persistence and graph work to keep **one** Relex MPS consumer continuously supplied. Multiple simultaneous Relex requests on the same M1 Max are unlikely to materially improve throughput, and the failed batching qualification already shows that higher GPU occupancy does not automatically produce faster or equivalent extraction.

---

# Correct M1 ingestion design (owner-refined 2026-08-06)

## Pass 1 — become searchable quickly

```text
source
  → layout-aware cleanup
  → parent/child chunking
  → deterministic metadata and summaries
  → Qwen embeddings
  → Qdrant
  → Fast and basic Hybrid search available
```

## Pass 2 — graph enrichment

```text
neighboring children
  → bounded semantic extraction windows
  → spaCy parse once
  → Relex once
  → EntityRuler and syntax candidate union
  → entity and relation gate
  → Mongo assertion records
  → batched Neo4j projection
```

User-visible behavior: document searchable first; graph enrichment completes later.
Neo4j index/UNWIND/batch/cert work remains valid **downstream** — it will not solve an extraction stage that already consumes most wall time.

## What should NOT be the next step

| Technique | Verdict |
|---|---|
| TensorRT | NVIDIA/CUDA — not M1 path |
| FP16 | Threshold-sensitive; requires full closed-world rebench (entities, predicates, margins, lanes) |
| torch.compile | Measured constant-factor experiment **after** algorithmic work; may graph-break |
| ONNX | Reasonable bake-off **after** call count / windows / pair growth / scheduling fixed |
| Length bucketing with batch_size=1 | Little value |
| Post-inference type-pair allowlist for speed | Precision yes; released-Relex inference speed limited |

---

# LFM2 models — correct distinction (do not conflate)

| Model | Role |
|---|---|
| **LFM2.5-Encoder-350M** | Backbone only — requires task-specific extraction heads |
| **LFM2-1.2B-Extract** | Finished autoregressive extraction model — schema-conditioned JSON/XML/YAML via `generate()` OOTB |
| **LFM2.5-1.2B-Instruct** | Liquid’s current recommended structured-extraction successor (Extract checkpoint is **deprecated**) |

```yaml
LFM2_1_2B_Extract:
  works_out_of_the_box: true
  works_without_fine_tuning: true
  can_generate_entities_and_triples: true
  extraction_type: generative_structured_extraction
  drop_in_equivalent_to_GLiNER_Relex: false
  safe_to_write_directly_to_Neo4j: false
  production_graph_writer_without_gate: false
  current_status: deprecated
  supported_successor: LFM2.5_1.2B_Instruct
```

### Interface guarantee difference

**GLiNER-Relex natively returns:** exact entity spans, entity types, entity scores, ordered entity pairs, relation labels, relation scores, directional predictions.

**LFM2-1.2B-Extract generates serialized text.** You can ask for entities and triples, but the documented interface does **not** natively provide calibrated relation probabilities, direction margins, or guaranteed source offsets. Published evaluation measures JSON/XML/YAML validity, keyword faithfulness, and LLM-judge ratings — **not** exact entity-span F1 or directed-triple F1.

The distinction is not whether it extracts. It does. The distinction is **what kind of guarantees its extraction interface gives you.**

### How LFM should be tested (schema + gates)

Prompt for mentions + claims with exact source substrings (no model-generated offsets). Python then: schema/enum validation, exact substring alignment, real offset calculation, specificity checks, type-pair validation, same-sentence validation, polarity/modality, tautology/generic-edge checks, dedup.

Initial lanes:
```text
LFM + trusted syntax agreement → ACCEPT_LFM_CORROBORATED
LFM-only with exact evidence   → REVIEW_LFM_ONLY
unalignable entity or claim    → REJECT_UNGROUNDED_GENERATION
```

Do **not** treat model-generated confidence as calibrated relation probabilities. Constrained JSON guarantees structure, not factual correctness.

Us existing Extract checkpoint for historical comparison only; build any new path around **LFM2.5-1.2B-Instruct**.

---

# Bake-off that answers the replacement question

Run four systems on the **same frozen windows**:

| System | Stack |
|---|---|
| **A** | Current baseline: Relex + current spaCy/Python gate + current chunk strategy |
| **B** | Optimized Relex: adaptive windows + predicate packs + improved entity gate + one spaCy parse + batched writes |
| **C** | Liquid: LFM2.5-1.2B-Instruct (or Extract for history) + constrained claim schema + exact evidence alignment + spaCy/Python gate |
| **D** | Syntax ceiling: EntityRuler/gazetteer + DependencyMatcher/FrameExtractor + Python only |

**Measure:** exact entity-span P/R/F1 · entity-type F1 · generic-entity FP rate · directed canonical-triple P/R/F1 · unsupported generated-value rate · exact evidence-alignment rate · generic-edge rate · two-hop evidence-support rate · accepted triples per source token · model calls/doc · source tokens/s · p50/p95 window latency · total graph-ready time · peak unified memory.

- **D** = how much semantic recall is lost without a semantic relation model  
- **B** = whether the problem was Relex or how Relex was fed  
- **C** = whether a fast generative Liquid model can beat optimized encoder E2E  

```yaml
production_decision:
  current_frozen_path: keep_relex
  optimization_target: extraction_windows_and_stage_scheduling
  liquid_model_role: shadow_bakeoff
  replacement_condition:
    - match_or_exceed_directed_triple_precision
    - acceptable_recall
    - near_zero_unsupported_values_after_gate
    - material_end_to_end_speed_win
```

---

# WAY AHEAD — GLiNER2 entity census architecture (owner 2026-08-06)

**Implementation tracker:** `SPEED_BENCH_CLOSEOUT_AND_PERFORMANCE_PLAN_20260805.md` § OPEN work G0–G13.

## Verdict

```yaml
architecture_direction: correct
expected_throughput_improvement: likely
exact_speedup: unverified
execution_law: "Batch globally; adjudicate document-locally."
Relex: {production_candidate: removed, frozen_benchmark_baseline: retained}
```

Better than either: (a) every document end-to-end one-at-a-time, or (b) merge every identical surface corpus-wide immediately.

## Why faster (projected — measure on M1)

Hot path removes Relex’s largest cost: entity detection + ordered pair construction + predicate scoring over pairs.

GLiNER2 census does only: text encoding + span detection + entity-type scoring.  
Relations generate pairs only where grammar licenses (verb/frame → args), not E×(E−1)×predicates.

Additional gains: one warm model · corpus batching · length bucketing · no graph writes during inference · no concurrent embed · no spaCy during entity inference · spaCy only on relation-eligible windows · batched persistence.

MLX port ~50 ms/sentence is a **project-level** figure, not Polymath workload proof.

## Five controls still required before replacing frozen Relex

1. Gold-entity syntax ceiling rerun (pair/triple recall with current complete syntax stack)  
2. MPS vs MLX corpus benchmark (same `fastino/gliner2-base-v1` checkpoint)  
3. Document entity cluster evaluation  
4. Mention completion evaluation  
5. Closed-world threshold calibration + held-out directed-triple qualification  

Canonical graph writes remain disabled until these complete + owner GO.

## Document entity census record (example shape)

```json
{
  "document_entity_id": "doc:17:software:mongodb",
  "canonical_surface": "MongoDB",
  "candidate_type": "Software",
  "mention_count": 21,
  "sentence_count": 15,
  "section_count": 5,
  "type_votes": {"Software": 20, "Service": 1},
  "type_purity": 0.952,
  "maximum_confidence": 0.97,
  "mean_confidence": 0.89,
  "heading_mentions": 2,
  "definition_support": 1,
  "gazetteer_match": true,
  "genericity_score": 0.01,
  "context_coherence": 0.94
}
```

Recurrence ≠ sole gate (see SPEED_BENCH promote/suppress). Strong singletons allowed (people, events, metrics, rare datasets, one-shot titles, newly defined concepts).

## Mention completion (strongest addition)

After promote: compile doc-local matcher (MongoDB / mongodb / Mongo DB / …) → PhraseMatcher/EntityRuler scan whole document → denser mention inventory for relation compiler **without** re-asking the neural model.

Every occurrence remains a **separate mention** with offsets; relations assemble from local spans, not one abstract node.

## Relation eligibility (beyond ≥2 entities)

```yaml
relation_eligible:
  any:
    - accepted_entity_count >= 2
    - accepted_entity_count >= 1 and definition_cue
    - accepted_entity_count >= 1 and metric_or_quantity_cue
    - accepted_entity_count >= 1 and temporal_cue
    - accepted_entity_count >= 1 and attribution_cue
    - accepted_entity_count >= 1 and trusted_relation_verb
```

## Durable surface relation (integration gap — stack exists)

Reuse DependencyMatcher / FrameExtractor / SVO / voice / negation / modality / coordination / apposition / endpoint signatures. Persist:

```json
{
  "subject_mention_id": "doc:17:sent:8:0-7",
  "object_mention_id": "doc:17:sent:8:22-28",
  "surface_predicate": "was built on top of",
  "predicate_lemma": "build",
  "particle": null,
  "preposition": "on top of",
  "dependency_frame": "PASSIVE_VERB_PREPOSITION",
  "voice": "passive",
  "canonical_candidate": "depends_on",
  "mapping_rule_id": "verb_prep:build_on_top_of",
  "mapping_release": "predicate-map:2.0.0",
  "negated": false,
  "modality": "certain",
  "attribution": null
}
```

Ambiguous → `STORE_UNMAPPED_SURFACE_RELATION`. Forced mapping and default `related_to` prohibited.

## Primary architectural risk — relation recall

History: syntax-only was rejected because the frame system never proposed many gold pairs; Relex supplied semantic recall. Expanded syntax stack may now be enough — **must remeasure with gold entity spans** before production flip. Until then keep shadow semantic rescue (GLiNER2 Python relations on unresolved windows). MLX relation extract still unimplemented.

## What prior plan detail still feeds this design

| Prior detail | Role in GLiNER2 path |
|---|---|
| Vector-first Pass-1 / searchable SLO | Unchanged — enrichment after Qdrant |
| Entity-bounded windows + adaptive split | Still reduces call count / density explosions |
| Specificity ladder / promote-suppress | Document entity reducer rules |
| Surface relation + endpoint signature compile | Predicate compiler law |
| Syntax lane components | Relation generation authority |
| Layout dehyphenation + reversible offsets | Normalization before census |
| Type-pair allowlist ≠ Relex speedup | Confirmed; irrelevant once pair matrix gone |
| Decoys ablation-only | Still not production-first |
| LFM bake-off | Non-hot-path shadow |
| Neo4j batch/cert/index work | Downstream only |
| Lineage map “stack exists” | Wire + persist; do not rebuild |

## Coherent one-liner

> GLiNER2 discovers the document’s entity vocabulary. The document census decides which entities are credible and salient. A deterministic matcher completes their mentions. spaCy reconstructs propositions only where useful. Python preserves the surface meaning, maps only unambiguous predicates, and decides what becomes durable knowledge.

---

# Final judgment (owner 2026-08-06)

```yaml
way_ahead:
  architecture_direction: correct
  expected_throughput_improvement: likely
  exact_speedup: unverified
  hot_path:
    - GLiNER2_entity_census
    - document_entity_reducer
    - deterministic_mention_completion
    - selective_spacy_parse
    - existing_predicate_compiler
    - existing_quality_gate
  Relex:
    production_candidate: removed
    frozen_benchmark_baseline: retained
  missing_before_production:
    - gold_entity_relation_ceiling_rerun
    - MPS_vs_MLX_corpus_benchmark
    - document_entity_cluster_evaluation
    - mention_completion_evaluation
    - closed_world_threshold_calibration
    - held_out_directed_triple_qualification
    - graph_write_promotion

your_quality_plan:
  fixes_bad_graph_writes: mostly_yes_with_gates
  fixes_raw_model_predictions: partially
  preserves_recall_automatically: no
  requires_closed_world_calibration: yes
  syntax_relation_recall: requalify_with_gold_entities

your_speed_plan:
  gliner2_corpus_batch_entity_census: highest_model_lever
  deterministic_mention_completion: highest_recall_assist_without_renerve
  selective_spacy: required_and_schedulable
  adaptive_extraction_windows: still_high_value
  vector_first_ingestion: highest_user_visible_value
  neo4j_indexes_and_batches: useful_downstream
  type_pair_allowlist:
    precision: useful
    released_relex_inference_speed: limited
  decoy_labels: experiment_only

why_relex_leaves_the_hot_path:
  - many_tiny_inference_units
  - batch_one_quality_constraint
  - large_encoder
  - quadratic_entity_pair_growth
  - shared_accelerator_workload
```


