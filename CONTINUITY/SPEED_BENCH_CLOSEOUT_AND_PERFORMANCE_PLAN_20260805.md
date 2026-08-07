# Speed Bench Closeout + Performance Plan (2026-08-05)

**Status:** CLOSEOUT (Phases 0–7 Relex speed-first) + **WAY AHEAD (owner 2026-08-06): GLiNER2 entity-census → grammar relations**.
Read this first in any new session before touching ingestion, extraction sidecars,
Neo4j promotion, or summary algorithms.
**Living owner analysis:** `OWNER_INGESTION_THROUGHPUT_AND_QUALITY_ANALYSIS_20260806.md`
**Pairs with:** `INGESTION_CONTROL_PLANE_20260805.md`, `PIPELINED_INGESTION_CLOSEOUT_20260805.md`,
`MPS_BATCH_QUALIFICATION_20260805.md`, `KNOWLEDGE_PIPELINE_LINEAGE_MAP_20260804.md`, COORDINATION.md.
**Frozen path:** `relex_local` remains recoverable benchmark baseline. Canonical graph writes stay
disabled for the new path until closed-world qualification completes.

## BLUF

Ingestion is **correct and faster**: optimized same-five-file bench **109 min → 31 min
(3.5×)** after Neo4j overhead cuts + pass overlap + repair quiesce. Quality operationally
passes (deterministic summaries, 0 OOV vocabulary, verify ok on fresh docs).

**Remaining problems are two categories (owner 2026-08-06, refined same day):**
1. **Problem 1 — Extraction quality** — bad entities / low-information relations entering the graph.
2. **Problem 2 — Extraction throughput** — too many expensive semantic inference units on one M1 GPU.

**Central conclusion (superseded 2026-08-06 afternoon):** Relex quality gates help durable writes but cannot remove Relex’s pair-matrix cost. **Way ahead:** *Execute globally, decide locally* — corpus-batched **GLiNER2 entity census** → document entity reducer → deterministic mention completion → selective spaCy → **existing** predicate compiler/gate. Relex = frozen benchmark only (not production candidate). Exact speedup **unverified** until M1 corpus bench.

Post-opt Relex split (corpus `d9f87093`, historical): Relex ~15.4 min · Neo4j ~8.8 min · embed ~4.5 min.

**LFM:** generative bake-off remains shadow-only (`LFM2.5-1.2B-Instruct`). Not the hot-path entity census.
**GLiNER2 semantic rescue:** Python relation API on unresolved windows only; MLX port relation extract still unimplemented — MPS vs MLX entity bench required.

## What ran (clean-run measurements, 13:42:11→14:11:43 UTC)

| Document | Chunks | Extraction | Embed | Qdrant | Neo4j |
|---|---:|---:|---:|---:|---:|
| Fundamentals of Data Engineering | 1,695 | 490.0s | 138s | 22.0s | 340.0s |
| HashiCorp Terraform Associate | 859 | 225.1s | 66s | 10.0s | 204.5s |
| TRAIL_SIGNAL_BROWSER_ADMISSION_HANDOFF | 46 | 12.1s | 4.5s | 0.6s | 82.2s |
| TRAIL_SIGNAL_LOCAL_SCRAPER_SUCCESSOR | 42 | 10.0s | 3.6s | 0.5s | 78.3s |
| (Benesh — completed pre-quiesce) | 65 | — | — | — | — |

Extraction rate ≈ **3.5 chunks/s** (encode ≈0.19s/chunk batch-1 on MPS + spaCy/gate lane).
Neo4j total ≈ **704s ≈ 40% of wall**, including **78–82s on 42–46-chunk docs with 24
relations** — proof of a fixed per-doc pass. Terraform's promotion ran **15,103
domain-type lookups** (ontology resolution not cached/bulked).

**Queryable ≠ complete:** documents are searchable after Qdrant (Fundamentals at
+11.8 min, Terraform ~+5 min). Neo4j graph promotion continues afterward. Time to
*queryable* is already much better than time to *graph-complete*.

## Quality results (from `data_eval/speed_bench_test_20260805/quality_speed_assessment.json`)

- Summaries **PASS_CONTROL**: 1360/1360 `deterministic:v1`, cloud models = 0,
  33 low-overlap flags (2.4%) with clean samples.
- Extraction: 2704/2704 chunks, 0 failures, 0 empty rows, engine `relex_local` on all docs.
  57,499 entities; 413 accepted relations (372 `accept_high` + 41 `accept_corroborated`).
- Ontology **PASS_SOFT**: OOV entity-type rate 0.0, OOV predicate rate 0.0.
- Assessment script: `backend/scripts/assess_speed_bench_test.py` (runs in backend
  container: `docker cp` + `docker exec -w /app ... python _assess_speed.py`).

## Owner review (2026-08-05): operational PASS ≠ semantic PASS

Owner-supplied closeout review of `quality_speed_assessment.json`. Verdict:
`pipeline_functionality: PASSED`, `corpus_semantic_quality: NOT_YET_FULLY_PASSED`.
The run proves reliability; the following bounded, reviewable gaps remain open and
must be closed by a **narrow quality-closeout pass — NOT a full reingest**.

### Open gaps (all bounded, none blocking use of the corpus)

1. **24 parents without nonempty summaries** (`total_parents=1384` vs
   `nonempty=1360`). Unclassified: intentional skips (noise/header/code-only/sparse)
   vs missing outputs. Required final shape:
   `parents_summary_required / intentionally_skipped / summarized / missing: 0`.
2. **32 child chunks without extraction rows** (`child_chunks=2736` vs
   `extraction_rows=2704`; per-doc extracted sums also 2,704). Probably classifier
   skips (noise/navigation/sparse) but NOT proven. Required:
   `children_extraction_eligible / intentionally_skipped / missing_unexpectedly: 0`.
3. **Row-level engine metadata unstamped** — doc-level says `engine: relex_local`,
   aggregate rows say `"<unset>": 2704`. Every extraction row must carry
   `engine / extractor_release / model_hash / acceptance_policy_release /
   ontology_release / contract_hash` for replay validation, release comparison,
   stale-record detection, migration.
4. **Visibly questionable accepted relations in samples** (of 413 total):
   - Malformed span: `operatorauthorized account` (punctuation lost; likely
     `operator-authorized account`).
   - Duplicate surface forms unconsolidated: `allowed network route` / `network route`;
     `explicit 20260730 agreement` / `20260730 agreement`; `this directive` / `directive`.
   - Possessive mis-predicate: `repository owner —owns→ explicit 20260730 agreement`
     (source: "repository owner's explicit 2026-07-30 agreement"; likely `approved` /
     `attributed_to` semantics).
   - `human users —uses→ mcp service` confidence 0.336 labeled `accept_high` — needs
     the syntax-evidence lane exposed to explain why raw score was overridden.
   - **Ontology PASS_SOFT means vocabulary compatibility only** — it does NOT prove
     span correctness, predicate accuracy, direction, consolidation, or usefulness.
     `owns` is a legal predicate; that does not make `owner owns agreement` true.
5. **33 low-overlap summaries need disposition** — one sample `ok:false`,
   `overlap_ratio: 0.3` (sparse heading). Policy to ratify:
   `sparse_or_low_overlap: eligible_for_vector_routing=conditional,
   ranking_penalty=true, linked_children_still_available=true`.
6. **Ladder counters ambiguous** — docs report `fully_enriched:5` AND
   `graph_pending:5` / `summary_pending:5` simultaneously (cumulative milestone
   membership, not current state). Final readiness report must separate
   `current_state_counts` (pending: 0) from `historical_milestone_counts`.

### Quality-closeout sequence (next action — do not reingest)

1. Classify the 24 parents (skip-reason attribution).
2. Classify the 32 child chunks (skip-reason attribution).
3. Stamp engine/release metadata on all 2704 extraction rows.
4. **Audit ALL 413 accepted relations against source evidence** (small enough for a
   complete source-backed audit; the right move now).
5. Consolidate duplicate entities/relation endpoints; repair malformed spans
   (`operatorauthorized account` class).
6. Disposition all 33 low-overlap summaries per the routing policy above.
7. Fix summary/graph pending counter semantics (current vs milestone).
8. Reproject ONLY affected summaries/vocabulary/graph assertions.
9. Rerun retrieval-quality tests after the repair pass.

## Root causes found + FIXED today (all BAKED into images — docker-cp era ended)

1. **Transport bug** — worker sent up to 512 chunks in one mute POST; Docker Desktop's
   host proxy severed it mid-request (`RemoteProtocolError: Server disconnected without
   sending a response`) while the sidecar stayed healthy. Fix in
   `backend/services/ingestion/relex_local.py`: **32-chunk slices** (`RELEX_SIDECAR_TASK_MAX`),
   one retry on a fresh connection, `max_keepalive_connections=0` (uvicorn drops idle
   keep-alives ~5s).
2. **Image drift after power cut** — hot-patched fixes (`join_trust`, control-plane seams,
   timeouts) were silently reverted when unplug deaths triggered container recreates.
   Two real image bakes done 2026-08-05; verify `join_trust` + slice constant exist in
   images before trusting any fix. **Never claim a fix is live from docker cp alone.**
3. **Repair-lane starvation** — auto-repair had **14,730 queued extraction_jobs** for
   paused q9-det (`d153de2a`) + void corpus (`7d801816`) and fed them to the single MPS
   encoder, starving the bench. Quiesced: `.env` `INGEST_AUTO_REPAIR_RUN_EXTRACTION=false`
   / `INGEST_AUTO_REPAIR_RUN_SUMMARIES=false`; `docker-compose.override.yml`
   `CONTROL_PLANE_V2_RUN_ALL_LANES=false` on backend + ingest-worker. Jobs stay queued
   (not lost); batch runner is now the ONLY extraction authority, per doctrine.
4. **spaCy/gate serialization** — CPU lane (~35% of extraction wall) ran after all encode
   waits. Now **slice-pipelined** (`_extract_prediction_slices` generator + single
   consumer thread; spaCy pipeline is not thread-safe — exactly ONE consumer). Results
   stay in task order; deterministic identical output. Tests:
   `backend/tests/test_relex_local_microbatch.py` 3/3 green.

## DONE since original closeout (Phases 0–7)

- [x] Pass-1/pass-2 live trace receipt
- [x] Relex batching qualified → **retain batch_1** (non-deterministic; 0 long-chunk speedup)
- [x] Neo4j fixed overhead reduced (defer cert, drop double clear, scope orphan, cache ontology)
- [x] Pass overlap (disjoint claims; MPS exclusivity preserved)
- [x] Embed batch sweep → keep 64
- [x] Readiness `stage_counts` vs cumulative `ladder`; void corpus isolation confirmed
- [x] Bake + same-five benchmark (3.5×) + q9 batch resume from checkpoint

## OPEN work — WAY AHEAD (owner 2026-08-06) — IMPLEMENTATION TRACKER

**Verdict:** direction correct; materially quicker is likely; exact speedup unverified.
**Execution law:** *Batch globally; adjudicate document-locally.*
Do **not** rebuild parsing/chunking/deterministic summary runtime/Qdrant/control-plane/predicate stack.
Reuse: DependencyMatcher · FrameExtractor · SVO · dep-path · appos/coord · endpoint signatures · surface-relation retention · assertion gate · vector-first Pass-1 · specificity ladder · layout dehyphenation (from prior Pri 0–6 / 5a–5e).

### Architecture (production candidate — shadow until qualified)

```text
SOURCE CORPUS
  → Structure-preserving normalization
       (furniture out · dehyphenate · headings/tables/code · reversible offsets)
  → Retrieval children + larger bounded extraction windows
  → Length-bucket every extraction window
  → PHASE A  GLiNER2 ENTITY CENSUS
       (one warm model · versioned entity descriptions · batched entity-only ·
        exact spans · confidence · raw mention persistence)
  → PHASE B  DOCUMENT ENTITY REDUCER
       (cluster · type votes · recurrence/purity · reject pronouns/noise ·
        specificity · strong singletons · promote document entities)
  → PHASE C  DETERMINISTIC MENTION COMPLETION   ← critical addition
       (doc-local PhraseMatcher/EntityRuler · scan whole doc ·
        every occurrence = separate mention offset · no merge-by-surface-alone)
  → PHASE D  RELATION-ELIGIBILITY FILTER
       (≥2 entities OR 1+ definition/metric/date/attribution/trusted verb)
  → PHASE E  spaCy PARSE (eligible windows only)
       (nlp.pipe · exclude NER · n_process bench 1/2/4 · one Doc reused)
  → DependencyMatcher ∪ FrameExtractor ∪ SVO ∪ dep-path ∪ appos/coord/rel-clause
  → SURFACE RELATION RECORD (durable) → PREDICATE COMPILER → ASSERTION GATE
  → Mongo authoritative → batched Neo4j projection
```

Optional shadow: **GLiNER2 Python relations** on unresolved windows only (semantic rescue).
Relex: `production_candidate: removed` · `frozen_benchmark_baseline: retained`.

### φ (binding)

```yaml
phi:
  execution_law: "Batch globally; adjudicate document-locally."
  entity_model:
    provider: GLiNER2
    checkpoint_bench: fastino/gliner2-base-v1
    role: candidate_mention_generator
    authority: none
  document_entity_census:
    recurrence_is_evidence: true
    recurrence_is_truth: false
    strong_singletons_allowed: true
    mention_count_lt_2_global_reject: prohibited
  mention_completion:
    method: [PhraseMatcher, EntityRuler]
    preserve_every_mention_offset: true
    merge_by_surface_alone: prohibited
  relation_generation:
    authority: grammar
    all_pairs_generation: prohibited
    default_scope: same_sentence
  predicate_mapping:
    ambiguous_action: STORE_UNMAPPED_SURFACE_RELATION
    forced_mapping: prohibited
    related_to_as_default: prohibited
  semantic_rescue:
    mode: shadow_or_selective
    provider_candidate: GLiNER2_Python_relations
    required_until: gold_entity_syntax_recall_qualified
  graph_write:
    model_output_direct_write: prohibited
  scheduling_option_A:
    phase_A: GLiNER2_alone_max_throughput
    phase_B_plus: spaCy_Python_after_census
```

### Recurrence ≠ sole gate (carry forward from specificity ladder)

```yaml
promote_entity:
  any: [curated_gazetteer_match, explicit_definition, explicit_alias_or_acronym,
        one_very_high_confidence_specific_mention, repeated_context_consistent_mentions,
        stable_type_across_multiple_sections, participation_in_a_trusted_relation]
suppress_entity:
  require_all: [generic_surface, no_gazetteer_match, no_definition, no_alias_evidence,
                no_trusted_relation, low_context_specificity, unstable_or_low_model_confidence]
```

`wordfreq` → one input to `genericity_score`, never independent deletion.
Recurrence means three different things: mention frequency · section spread · corpus document frequency.

### Implementation tracker (GLiNER2 path)

| ✓ | Pri | Item | Notes / gate |
|---|---:|---|---|
| [ ] | **G0** | **Gold-entity syntax ceiling rerun** | Gold spans → current complete syntax stack → pair/triple recall. Primary risk: syntax-only was previously too silent. **Block production flip until acceptable.** |
| [ ] | **G1** | **Structure-preserving normalization** | Furniture out; dehyphenation; reversible offsets; heading/table/code boundaries. Feeds both census and spaCy. |
| [ ] | **G2** | **Retrieval children + entity-bounded windows + length buckets** | Keep citation children; windows for extract; soft≤20 / hard≤30 ents still apply as density split. |
| [ ] | **G3** | **PHASE A — GLiNER2 entity census sidecar** | One warm model; fixed versioned entity descriptions; batched entity-only; persist raw mentions. No relations in hot path. |
| [ ] | **G4** | **MPS vs MLX corpus bench** | Same checkpoint `fastino/gliner2-base-v1`, same windows/descriptions/thresholds. Arms A–D MPS batch 1/4/8/16 · E MLX serial · F MLX microbatch if exists. Metrics below. |
| [ ] | **G5** | **PHASE B — Document entity reducer** | Doc-local cluster; type votes/purity; specificity; strong singletons; census record shape (see owner analysis). |
| [ ] | **G6** | **PHASE C — Mention completion** | Doc-local PhraseMatcher/EntityRuler from promoted entities; every occurrence separate mention; `doc.spans["polymath_entities"]`; `char_span(..., strict)` only for graph acceptance. |
| [ ] | **G7** | **PHASE D — Relation eligibility** | ≥2 ents **or** 1+ definition/metric/temporal/attribution/trusted verb. |
| [ ] | **G8** | **PHASE E — Selective spaCy + syntax stack** | `exclude=["ner","textcat"]`; `nlp.pipe` batch_size≈128; n_process 1/2/4 bench (do not assume 4 wins on M1). Reuse existing FrameExtractor/DependencyMatcher/SVO. |
| [ ] | **G9** | **Durable surface relation + predicate compiler** | Persist surface/lemma/particle/prep/frame/voice/negation/modality/attribution + `mapping_rule_id` / `mapping_release`. Ambiguous → STORE_UNMAPPED_SURFACE_RELATION. |
| [ ] | **G10** | **Compile endpoint signatures at startup** | Version-stamp for whole corpus generation; lookup-only at runtime. |
| [ ] | **G11** | **Assertion gate → Mongo → batched Neo4j** | No direct model→Neo4j. Graph promotion only after held-out directed-triple qualification. |
| [ ] | **G12** | **Semantic-rescue shadow lane** | GLiNER2 Python relations on unresolved windows; keep until G0 qualifies. Relex retained as frozen baseline comparator only. |
| [ ] | **G13** | **Closed-world calibration + held-out qualification** | Thresholds, entity cluster eval, mention-completion eval, directed-triple P/R. Then owner GO for production flip. |
| [ ] | **0** | **Vector-first Pass-1 (still required)** | Searchable Qdrant before enrichment. Independent of GLiNER2 vs Relex. |
| [ ] | **6** | **Deterministic summary v2** | Quality track; not on extraction critical path. |

### Prior Relex-era items — mapped, not discarded

| Old Pri | Fate under way-ahead |
|---|---|
| 1–3 Relex profile / cache / entity-only routing | **Baseline maintenance only** while Relex is frozen comparator; not hot-path optimization target |
| 4 extraction windows | **Absorbed into G2** (still highest call-count lever) |
| 5a label defs + per-type thresholds | **Absorbed into G3** as versioned GLiNER2 entity descriptions + calibrated thresholds |
| 5b specificity ladder | **Absorbed into G5** promote/suppress rules (recurrence ≠ sole gate) |
| 5 consolidation | **Absorbed into G5→corpus canonical consolidation** after doc-local census (never immediate corpus surface-merge) |
| 5c EntityRuler | **Absorbed into G6** mention completion (+ gazetteer assist) |
| 5d / 5d.1 / 5d.2 syntax + surface + signatures | **Absorbed into G8–G10** |
| 5e decoy labels | Still **ablation-only** if ever tested on GLiNER2 descriptions |
| Bake-off A/B/C/D | **Revised:** A=frozen Relex · B=GLiNER2+syntax path · C=LFM shadow · D=syntax-ceiling (gold entities). Production flip needs B ≥ gates vs A on quality + material speed |
| Type-pair allowlist “3–10× Relex speedup” | **Rejected as model-speed claim**; precision-only on released Relex. Irrelevant on GLiNER2 hot path (no pair matrix) |

### Missing before production flip (five controls + quals)

1. Gold-entity relation ceiling rerun (G0) — syntax recall must be requalified  
2. MPS vs MLX corpus benchmark (G4)  
3. Document entity cluster evaluation (G5)  
4. Mention completion evaluation (G6)  
5. Closed-world threshold calibration + held-out directed-triple qualification (G13)  
Plus: graph_write_promotion owner GO only after the above.

### Bench metrics (G4 / G13)

```yaml
entity_stage: [total_corpus_wall_time, source_tokens_per_second, windows_per_second,
               p50_window_latency, p95_window_latency, peak_unified_memory,
               exact_span_f1, entity_type_f1, generic_noun_false_positive_rate,
               five_run_decision_agreement]
downstream: [promoted_entity_rate, relation_eligible_window_rate, spacy_cpu_seconds,
             pair_recall, directed_triple_precision, directed_triple_recall,
             accepted_triples_per_second]
```

### Alignment invariant (G6 ↔ G8)

```text
GLiNER2 text == spaCy Doc text
span = doc.char_span(start, end, alignment_mode="strict")
if span is None: record_alignment_failure; expand/contract = shadow diagnostic only
doc.spans["polymath_entities"] = accepted_spans   # overlapping spans OK; not only doc.ents
```

### Quality + speed law (updated)

```yaml
architecture_direction: correct
expected_throughput_improvement: likely
exact_speedup: unverified
hot_path: [GLiNER2_entity_census, document_entity_reducer, deterministic_mention_completion,
           selective_spacy_parse, existing_predicate_compiler, existing_quality_gate]
Relex: {production_candidate: removed, frozen_benchmark_baseline: retained}
principle: every_graph_write_must_be_justified
execution_law: batch_globally_decide_document_locally
all_pairs_generation: prohibited
preserve_surface_relation: required
compile_endpoint_signatures: before_runtime
speed_highest_levers: [gliner2_corpus_batch_entity_census, selective_spacy, vector_first_pass1, adaptive_windows]
not_next: [TensorRT, FP16_without_full_gold, torch.compile_first, corpus_wide_immediate_surface_merge]
```

Hard constraints carried forward:
- **Do not process every document end-to-end one-at-a-time** as the only mode; census batches globally.
- **Do not merge every identical surface corpus-wide immediately**; doc census first, then corpus consolidation.
- **Frozen Relex batch_1 / one-MPS-forward** still apply when running the Relex baseline comparator.
- **Zero OOV ≠ semantic correctness.**
- **No model→Neo4j direct write.**
- **Summaries are not a speed problem** (Pri 6 remains quality-parallel).
- **Lineage map:** syntax stack exists — wire + durable persist; do not rebuild FrameExtractor/DependencyMatcher/SVO.

## Owner SLOs (same five files, 32 GB M1 Max) — committed targets

```yaml
target:
  searchable: <= 5_minutes          # Qdrant queryable; Relex need not finish first
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

Most reasonable success result: **~5 min searchable · ~10–12 min fully enriched**.
Recommended architecture path (extraction windows + overlap): **9–13 min fully enriched**.
Stretch (parity-gated): **7–9 min fully enriched**. Sub-5 fully enriched = different device or different extraction contract.

Historical Relex optimization levels (superseded as production aim; kept for baseline compare):
1. Low-risk Relex only → **20–24 min** full
2. Extraction windows + Neo4j overlap → **9–13 min** full / **4–6 min** searchable
3. Stretch → **7–9 min** full

**New production aim:** GLiNER2 corpus-batched entity census + selective spaCy (speedup likely, unverified). Scheduling Option A: GLiNER2 alone at max throughput, then spaCy/Python — no concurrent embed/Relex during census. See owner analysis.

## OPEN owner decisions

- **A.** Void corpus `7d801816` disposition (owner boundary — still open).
- **B.** Production flip to GLiNER2 path only after G0+G4+G5+G6+G13 green + owner GO (graph writes stay off until then).
- **C.** Semantic-rescue provider: default GLiNER2 Python relations (selective); Relex baseline retained for A/B compare only.
- **D.** Summary v2 adoption = new algorithm stamp + one regeneration pass (Pri 6) — parallel track.
- **E.** spaCy `n_process` — measure 1/2/4; do not assume 4 on M1.
- **F.** LFM bake-off remains shadow / non-hot-path.
- **G.** Decoy labels stay ablation-only.
- **H.** Vector-first Pass-1 (Pri 0) remains mandatory for searchable SLO regardless of extractor.

## Session-restart checklist (mechanical)

- [ ] Read this file § **WAY AHEAD** + owner analysis GLiNER2 architecture section.
- [ ] Read `KNOWLEDGE_PIPELINE_LINEAGE_MAP_20260804.md` (reuse syntax stack; do not rebuild).
- [ ] Read `INGESTION_CONTROL_PLANE_20260805.md` + latest COORDINATION receipt.
- [ ] Stack healthy; repair lanes still quiesced.
- [ ] Next work starts at **G0** (gold-entity syntax ceiling) in parallel with **G4** design for MPS/MLX bench — unless owner reorders.
- [ ] Relex sidecar kept alive for frozen baseline only; do not optimize Relex as the production target.
- [ ] Success bar (five-file SLO) still: searchable ≤5 · graph_ready ≤12 · hard ceiling 15 — re-measure under GLiNER2 path.
