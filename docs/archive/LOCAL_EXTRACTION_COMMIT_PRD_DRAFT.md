# PRD Draft: Deterministic Local Extraction Research Lane

Date: 2026-06-05

Status: Draft for current working commit

Candidate commit title:

```text
Add deterministic local extraction diagnostics and GLiREL fixture harness
```

## BLUF

This commit does not make local extraction production-ready. It creates the research and validation layer needed to decide which parts of a local Ghost B lane can be trusted.

The current outcome is clear:

- The local lane is stack-compatible with the existing Polymath Ghost B JSONL/parser path.
- The local lane is fast enough to be interesting.
- The local lane is not semantically accurate enough to write graph output yet.
- GLiREL was fast enough to test, but it failed relation correctness and should not be in the production critical path.
- The best production direction is a deterministic Python relation pipeline:

```text
Python candidates
+ GLiNER2 entity assist
-> deterministic direct relations
-> EntityImportanceRanker
-> Pydantic/evidence gate
-> compact JSONL
```

The next engineering blocker is relation-option construction and clean canonical span generation, not the final `ExtractionResponse` schema.

## 5 Ws

### Who

Primary user:

- Polymath operator ingesting large local corpora on a Mac Studio / Apple Silicon deployment.

Primary developer audience:

- Senior/backend engineer implementing local extraction as a feature-flagged research lane.
- Future ML/IE engineer tuning GLiNER2 span assist, Python candidate generation, deterministic relation rules, and ontology mapping.

Affected system components:

- Local extraction research harnesses in `scripts/`
- Existing Ghost B schema contract in `backend/services/ghost_b_schemas.py`
- Existing compact JSONL parser in `backend/services/ghost_b.py`
- Future local ingestion runtime, if the research lane passes quality gates

### What

We created a deterministic local extraction test lane that simulates how a real ingested file would be chunked, locally extracted, validated, and converted into the current Polymath schema.

The research architecture tested in this commit is:

```text
real file
-> parser/chunker
-> content/body chunk filter
-> Python high-recall candidate generator
-> optional GLiNER2 span assist
-> clean candidate spans
-> GLiREL relation scorer over Python-owned options
-> Python OntologyMapper/compiler
-> services.ghost_b_schemas.ExtractionResponse
-> compact Ghost B JSONL
-> existing backend parser compatibility test
```

The local model layer is not trusted. Models only propose or score candidates. Python remains the deterministic judge.

The revised production architecture should be:

```text
real file
-> parser/chunker
-> content/body chunk filter
-> Python high-recall candidate generator
-> GLiNER2 span/entity assist when needed
-> canonical span resolver
-> sentence-level relation windows
-> deterministic relation compiler
-> EntityImportanceRanker
-> Pydantic validation
-> exact evidence gate
-> services.ghost_b_schemas.ExtractionResponse
-> compact Ghost B JSONL
```

In the production direction, Python is the sole relation writer. GLiNER2 can add spans, but it does not decide final entities or relations. GLiREL remains an experiment only.

### When

This work was done after the local extraction model tests showed that small generative SLMs could be fast but could not reliably produce useful Ghost B-compatible graph output.

The current iteration date is 2026-06-05.

This PRD applies to the current uncommitted research changes in the local workspace.

### Where

Repo:

```text
/Users/king/polymath_v3.3
```

Real-file test source:

```text
/Volumes/Flash Drive/merged/13_building_ai_mobile_apps_2025.md
```

Main generated reports:

```text
LOCAL_EXTRACTION_RESEARCH_REPORT.md
LOCAL_EXTRACTION_REAL_FILE_SIMULATION_REPORT.md
LOCAL_EXTRACTION_COMMIT_PRD_DRAFT.md
```

Main research scripts:

```text
scripts/bench_local_draft_schema_pipeline.py
scripts/bench_glirel_relation_lane.py
scripts/score_local_extraction_gold.py
scripts/verify_local_extraction_stack_compat.py
scripts/autoresearch_polymath_local_extraction.py
```

Gold fixture:

```text
scripts/local_extraction_fixtures/13_building_ai_mobile_apps_10_chunks.jsonl
scripts/local_extraction_fixtures/13_building_ai_mobile_apps_gold_v1.json
scripts/local_extraction_fixtures/13_building_ai_mobile_apps_gold_criteria_v1.md
```

### Why

The user needs local extraction that is:

- much faster than cloud extraction
- cheaper than API-based Ghost B extraction
- safe enough not to pollute Mongo/Neo4j
- compatible with the current Polymath schema and JSONL parser
- able to run unattended during large folder ingestion

Earlier tests showed that asking small models to emit the full Ghost B object directly is unreliable. The better architecture is compiler-like:

```text
models propose
Python validates
Python maps ontology
Python emits final JSONL
```

This commit exists to prove that path scientifically before production integration.

## Goals

1. Preserve the current production schema.

   The final accepted object must still validate against:

   ```text
   services.ghost_b_schemas.ExtractionResponse
   ```

2. Preserve the current compact JSONL output shape.

   The local lane must still produce:

   ```json
   {"t":"e", "...":"..."}
   {"t":"r", "...":"..."}
   {"t":"f", "...":"..."}
   {"t":"x"}
   ```

3. Build a deterministic answer-sheet test.

   We need to know the max possible score and exactly what was missed or hallucinated.

4. Simulate a real file ingestion.

   The test must use a real flash-drive document, parser/chunker output, and realistic child chunks.

5. Measure quality and speed separately.

   Stack compatibility alone is not enough. A run can pass JSONL parsing and still be semantically bad.

6. Keep local extraction disabled by default.

   The current lane is research/preview only until it clears quality gates.

## Non-Goals

This commit does not:

- replace Ghost B in production
- write local extraction output to Mongo or Neo4j
- enable facts extraction
- implement Qwen, GPT, or cloud fallback
- change the production `ExtractionResponse` schema
- change the backend ingestion worker behavior
- claim GLiREL output is production-quality as a final relation extractor

## What We Built

### 1. Real-File Simulation

We simulated ingestion against:

```text
/Volumes/Flash Drive/merged/13_building_ai_mobile_apps_2025.md
```

Observed chunking:

```text
file bytes:       70,502
source format:    local_markdown
parent chunks:    40
child chunks:     70
body chunks:      60
code chunks:      10
article chunks:   27 after simple content routing
token max:        570
```

Purpose:

- prove the local lane can run on realistic chunk shapes
- avoid synthetic toy examples
- expose Medium/sidebar/footer noise

### 2. Stack Compatibility Verifier

We created a compatibility verifier that checks:

```text
clean local object
-> compact JSONL
-> backend.services.ghost_b._parse_jsonl_lines
-> backend.services.ghost_b._parse_jsonl_items
-> backend.services.ghost_b.ExtractionResult
```

Result:

```text
Routed GLiREL output:
27/27 clean Pydantic pass
27/27 JSONL finished
27/27 backend parse pass
44/33/0 local E/R/F -> 44/33/0 backend E/R/F
```

Interpretation:

The local output can fit the current stack. The schema is not the blocker.

### 3. Gold Answer Sheet

We created a fixed 10-chunk test set and hand-labeled answer sheet.

Max score:

```text
Gold chunks:       10
Max entity TP:     165
Max relation TP:   75
Max total TP:      240
```

This gives a concrete quality target instead of guessing from raw model output.

### 4. Gap Diagnostics

We enhanced the scorer to report why extraction failed:

- missed entity gap category
- inferred entity type
- surface-form features
- context features
- dirty extra entities
- per-chunk missed/extra details

This showed the most important failure pattern:

```text
Many gold entities were already reachable, but they were pruned, outranked, or replaced by dirty spans.
```

### 5. Candidate Hygiene Patch

We patched the research candidate generator to:

- normalize possessive `s` noise
- reject verb-contaminated fragments
- boost high-signal mobile/AI technical terms
- improve handling of terms like `GDPR`, `Kotlin`, `TensorFlow`, `phone`, `doctor`, `runtime`, and `quantization`

Example dirty spans targeted:

```text
assistant using llm
using llm s
tensorflow lite brings
face s model
gpu delegate for hardware
```

### 6. GLiREL Standalone Entity Retention

The GLiREL lane originally pruned entities down to relation endpoints only.

We added the ability to:

```text
freeze relation endpoints
then keep top standalone entities by deterministic importance score
```

This is necessary because useful graph entities often do not participate in a relation in the same chunk.

## Outcomes

### Baseline Routed GLiREL

```text
Entity P/R/F1:   34.3% / 7.3% / 12.0%
Relation P/R/F1: 7.1% / 2.7% / 3.9%
Graph F1:        7.9%
```

### After Candidate Hygiene + Cleaned GLiREL Lane

```text
Entity P/R/F1:   19.5% / 47.3% / 27.6%
Relation P/R/F1: 11.4% / 5.3% / 7.3%
Graph F1:        17.4%
Speed:           ~3,083 chunks/hour
```

### After Pure Python Candidate/Ranker Path

```text
Entity P/R/F1:   19.2% / 44.8% / 26.9%
Relation P/R/F1: 0.0% / 0.0% / 0.0%
Graph F1:        13.5%
Speed:           ~13,760 chunks/hour
```

### 2026-06-05 Python-Only Deterministic Relation Test

We added and ran:

```text
scripts/bench_python_deterministic_relation_compiler.py
```

This test uses no GLiREL, no Qwen, and no cloud model.

Mode 1: current direct rules on the mobile-app fixture:

```text
Output:
/tmp/polymath_python_current_direct_mobile_fixture_v1.json

Entity P/R/F1:   24.2% / 56.4% / 33.8%
Relation P/R/F1: 0.0% / 0.0% / 0.0%
Graph F1:        16.9%
Speed:           ~16,254 chunks/hour
```

Interpretation:

The old deterministic direct rules are fast and schema-safe, but they have not been ported to the mobile-app domain. They emitted zero direct relations on this fixture.

Mode 2: fixture-seeded deterministic relation compiler:

```text
Output:
/tmp/polymath_python_fixture_seeded_mobile_fixture_v1.json

Score:
/tmp/polymath_python_fixture_seeded_mobile_fixture_score_v1.json

Stack compatibility:
/tmp/polymath_python_fixture_seeded_mobile_fixture_stack_compat_v1.json

Entity P/R/F1:   42.9% / 82.4% / 56.4%
Relation P/R/F1: 100.0% / 98.7% / 99.3%
Graph F1:        77.9%
Speed:           ~21,851 chunks/hour
Backend parse:   10/10
Backend E/R/F:   317/74/0 -> 317/74/0
Backend drops:   0
```

Important caveat:

The `fixture_seeded` mode is an upper-bound/compiler sanity test, not a production generalization test. It uses the fixture answer sheet as deterministic relation templates to prove that when Python has the right domain rules and spans, it can compile correct relations at high speed with no neural relation scorer.

The one missed relation was:

```text
Hugging Face model hub references mobile tags
```

Reason:

```text
mobile tags
```

was not recovered as a usable entity.

### Interpretation

The patch improved recall and reduced candidate-generation misses, but quality is still far below production threshold.

Important improvement:

```text
missing_from_candidate_generator dropped from 18 to 10
entity recall improved from 7.3% to 47.3%
graph F1 improved from 7.9% to 17.4%
```

Important failure:

```text
relation F1 is still only 7.3%
```

The remaining blocker is relation-option construction:

```text
GLiREL is only as good as the relation options Python gives it.
```

The strategic conclusion is stronger than the GLiREL result:

```text
If Python can construct clean relation options, Python can usually compile the relation directly.
GLiREL becomes a slower non-deterministic middle step, not a production requirement.
```

The new deterministic compiler test supports this conclusion: relation precision hit `100.0%` and relation F1 hit `99.3%` without GLiREL once the relation templates were deterministic.

## Current Technical Decision

Do not integrate this lane into production ingestion yet.

Keep it as a disabled research lane until the next architecture clears deterministic quality gates.

The revised production decision is:

```text
Drop GLiREL from the critical path.
Do not add Qwen as a hard-case resolver in this commit path.
Python owns relation writing.
GLiNER2 is optional span assist only.
Python remains the final schema, evidence, and JSONL judge.
```

Recommended default flags:

```env
LOCAL_GHOST_B_ENABLED=false
LOCAL_GHOST_B_ENTITY_HELPER=gliner2
LOCAL_GHOST_B_RELATION_WRITER=python_deterministic
LOCAL_GHOST_B_REQUIRE_CONTENT_ROUTER=true
LOCAL_GHOST_B_REQUIRE_GOLD_PASS=true
LOCAL_GHOST_B_FACTS=false
```

## Acceptance Criteria

The local lane can become a production candidate only when it passes:

```text
Entity F1 >= 85%
Relation F1 >= 85%
Relation precision >= 90%
Graph F1 >= 90%
Backend JSONL parse pass = 100%
Evidence substring gate = 100%
Schema validation pass = 100%
Dirty endpoint pattern count = 0
Junk chunk false-positive writes = 0
```

Until then, it must not write final graph output.

## Next Implementation Requirements

### Requirement 1: Sentence-Level Relation Window Builder

Build relation options from sentence/evidence windows, not whole chunks.

Expected flow:

```text
sentence
-> clean entities present in sentence
-> typed entity pair candidates
-> predicate cue rules
-> Python compiles supported relations directly
```

Why:

Relation evidence is sentence-level. Whole chunks create too many noisy co-occurrence pairs.

### Requirement 2: Non-Overlapping Canonical Span Resolver

When candidates overlap, keep the clean canonical span.

Examples:

```text
keep TensorFlow Lite
drop TensorFlow Lite brings

keep Hugging Face model hub
drop face s model

keep GPU delegate
drop GPU delegate for hardware
```

### Requirement 3: Domain Gazetteer Layer

Add a lightweight dictionary/phrase matcher for common technical terms:

```text
TensorFlow Lite
Core ML
ExecuTorch
ONNX Runtime
Apple Neural Engine
Google Tensor
Qualcomm Snapdragon AI Engine
GDPR
CCPA
Kotlin
Swift
quantization
inference runtime
```

Why:

Many gold entities are lowercase or multi-word technical phrases that generic NER does not reliably select.

### Requirement 4: Relation Direction Rules

Python should own direct relation direction rules.

Examples:

```text
mobile app uses on-device AI
TensorFlow Lite supports Android
model uses quantization
GDPR regulates patient data
```

GLiREL should be removed from the critical path after this research commit. It can remain in scripts for comparison, but production extraction should not depend on it.

### Requirement 5: Deterministic Relation Compiler

Port and extend the Python-only relation engine that previously hit:

```text
Relation Precision: 100%
Relation Recall:    100%
Graph F1:           91.2%
Speed:              ~15k chunks/hour
```

Run that same deterministic compiler on the current 10-chunk mobile-app fixture.

Expected work:

- sentence-level windows
- cue verb mapping
- dependency/path patterns when spaCy is available
- typed subject-predicate-object constraints
- evidence substring gates
- relation endpoint preservation
- no neural relation scorer in the write path

### Requirement 6: Keep Facts Disabled

Facts stay disabled until entity and relation quality clears the target.

Current local tests should emit:

```json
{"facts":[]}
```

## Risks

### Risk 1: Fast But Wrong Graph Pollution

The local lane can pass JSON parsing while producing semantically bad relations.

Mitigation:

- keep disabled by default
- require gold score gates
- require evidence gates
- require dirty endpoint checks

### Risk 2: Overfitting To One Article

The current fixture is from one real article.

Mitigation:

- add fixtures from textbook chunks, technical docs, theory prose, and code-heavy docs
- report per-domain quality

### Risk 3: GLiREL Scores Bad Options Confidently

If Python gives bad options, GLiREL may still select them.

Mitigation:

- improve option construction before model scoring
- enforce relation direction/type rules
- cap candidates per sentence

### Risk 4: Entity Recall vs Precision Tradeoff

Lower thresholds increase recall but flood false positives.

Mitigation:

- use importance ranker
- use domain gazetteer
- use overlap canonicalizer
- keep standalone entity caps

## Test Commands

Compile scripts:

```bash
/tmp/polymath_relation_models_py310_venv/bin/python -m py_compile \
  scripts/autoresearch_polymath_local_extraction.py \
  scripts/bench_local_draft_schema_pipeline.py \
  scripts/bench_glirel_relation_lane.py \
  scripts/score_local_extraction_gold.py
```

Score cleaned GLiREL result:

```bash
/tmp/polymath_relation_models_py310_venv/bin/python \
  scripts/score_local_extraction_gold.py \
  --report /tmp/polymath_glirel_cleaned_ranker_fixture_v1.json \
  --report-index 2 \
  --gold scripts/local_extraction_fixtures/13_building_ai_mobile_apps_gold_v1.json \
  --samples scripts/local_extraction_fixtures/13_building_ai_mobile_apps_10_chunks.jsonl \
  --out /tmp/polymath_glirel_cleaned_ranker_fixture_score_v1_best.json \
  --diagnostics-out /tmp/polymath_glirel_cleaned_ranker_fixture_gap_diag_v1_best.json
```

## Final Call

This commit should be presented as a research infrastructure and diagnostic commit, not a production local extraction feature.

The most valuable output is not that GLiREL works today. It is that we now have:

- a real-file simulation path
- stack compatibility proof
- deterministic answer sheet
- gap diagnostics
- before/after metrics
- clear next engineering target

Next commit should focus on:

```text
sentence-level relation window builder
+ canonical non-overlapping span resolver
+ domain gazetteer
+ relation direction/type rules
+ deterministic relation compiler on the current 10-chunk fixture
```

That is the path to move from fast-but-not-ready toward near-cloud local extraction quality.

## 2026-06-05 Follow-Up: spaCy Dependency Compiler Test

After the regex cue-window compiler overgenerated relation candidates, we tested
the next architecture:

```text
candidate spans
-> spaCy dependency parse
-> subject/verb/object relation compiler
-> Python type/evidence/schema gates
-> ExtractionResponse
-> compact JSONL
```

This test still uses no GLiREL, no Qwen, and no cloud model.

### spaCy Cue/Dependency Hybrid

Output:

```text
/tmp/polymath_python_spacy_rules_mobile_fixture_v3.json
```

Stack compatibility:

```text
/tmp/polymath_python_spacy_rules_mobile_fixture_stack_compat_v3.json
```

Result:

```text
Entity P/R/F1:   24.3% / 68.5% / 36.0%
Relation P/R/F1: 26.7% / 16.0% / 20.0%
Graph F1:        28.0%
Speed:           ~19,702 chunks/hour
Backend parse:   10/10
Backend E/R/F:   457/45/0 -> 457/45/0
Backend drops:   0
```

### Strict spaCy SVO Mode

Output:

```text
/tmp/polymath_python_spacy_svo_mobile_fixture_v2.json
```

Oracle-entity output:

```text
/tmp/polymath_python_spacy_svo_oracle_entities_mobile_fixture_v2.json
```

Stack compatibility:

```text
/tmp/polymath_python_spacy_svo_mobile_fixture_stack_compat_v2.json
```

Result:

```text
Normal candidates:
Entity P/R/F1:   24.1% / 64.8% / 35.1%
Relation P/R/F1: 35.7% / 13.3% / 19.4%
Graph F1:        27.3%
Speed:           ~19,656 chunks/hour
Backend parse:   10/10
Backend E/R/F:   433/28/0 -> 433/28/0
Backend drops:   0

Oracle entities:
Entity P/R/F1:   28.0% / 73.9% / 40.5%
Relation P/R/F1: 35.7% / 13.3% / 19.4%
Graph F1:        29.9%
Speed:           ~16,799 chunks/hour
```

### Interpretation

spaCy improved the relation lane over raw regex windows, but it did not solve
the quality problem by itself.

The strict SVO mode reduced false positives (`18` FP vs. `33` FP in the hybrid
mode) but did not improve recall enough. Oracle entities did not materially
increase relation F1, which means the current blocker is not only entity recall.
Many gold relations in the mobile-app fixture are not plain direct SVO triples.
They are technical-writing patterns:

- `X brings A, B, C` should compile several `supports` edges.
- `X running locally on Y` should compile a `uses` edge.
- `X documentation at URL` should compile `references`.
- `X, Y, Z - these are neural processing units` should compile `example_of`.
- `LoRA represents weight updates as compressed matrices` needs synonym plus
  mapped-property handling.

The test confirms the production path should be:

```text
canonical non-overlapping span resolver
+ spaCy dependency parser
+ portable technical relation pattern library
+ typed predicate constraints
+ evidence validation
+ stack-compatible JSONL conversion
```

The path should not be:

```text
raw regex windows
or GLiREL as final relation writer
or Qwen/cloud fallback in this commit path
```

### Current Decision

The deterministic local compiler is stack-compatible and fast enough, but it is
not production-quality yet. The next implementation target is a portable
technical relation pattern library layered on top of spaCy, not another neural
relation scorer.

## 2026-06-05 Follow-Up: MLX SLM Relation Proposer Test

We tested the hypothesis that a small off-the-shelf MLX instruction model could
act as a relation proposer without fine-tuning.

New script:

```text
/Users/king/polymath_v3.3/scripts/bench_mlx_slm_relation_proposer.py
```

The model is still not trusted as a graph writer. It only receives:

```text
sentence
entity 1
entity 2
allowed predicate list
```

and must output one line:

```text
1 <predicate> 2
2 <predicate> 1
none
```

Python still builds `ExtractionResponse`, validates evidence, and converts to
compact JSONL.

### Qwen2.5-1.5B Few-Shot Full Fixture

Output:

```text
/tmp/polymath_mlx_slm_qwen25_15b_fewshot_v1.json
```

Stack compatibility:

```text
/tmp/polymath_mlx_slm_qwen25_15b_fewshot_stack_compat_v1.json
```

Result:

```text
Model:           mlx-community/Qwen2.5-1.5B-Instruct-4bit
Load time:       0.57s
Speed:           ~295 chunks/hour
Schema:          10/10
Backend parse:   10/10
Backend E/R/F:   621/403/0 -> 621/403/0
Backend drops:   0
Entity F1:       31.6%
Relation F1:     0.4%
Graph F1:        16.0%
Relation TP/FP/FN: 1/402/74
```

Interpretation:

Qwen2.5-1.5B followed the output format, but it proposed a relation for almost
every pair. The output was schema-safe but semantically unsafe.

Example failure:

```text
out her phone uses examination room
examination room stores internet connection
tensorflow lite produces common operations
```

This proves schema compatibility is not a quality guarantee.

### Two-Chunk Probes

Zero-shot Qwen2.5-1.5B:

```text
Relation F1: 0.0%
Relation TP/FP/FN: 0/50/8
```

Zero-shot Qwen3-1.7B:

```text
Relation F1: 4.9%
Relation TP/FP/FN: 1/32/7
Speed: ~193 chunks/hour
```

Zero-shot Llama-3.2-1B:

```text
Relation F1: 0.0%
Relation TP/FP/FN: 0/13/8
Speed: ~295 chunks/hour
```

Llama was more conservative, but it still missed the correct gold relations on
the probe. Qwen3 found one correct relation but remained noisy and slower.

### SLM Decision

Do not integrate an off-the-shelf SLM pair proposer as the production relation
writer.

It can remain as a research lane, but only after adding at least one of:

- cue-window pair prefiltering
- spaCy/rule agreement before acceptance
- contrastive no-relation examples
- fine-tuning on Polymath relation pairs
- a pair-ranking threshold calibrated on gold fixtures

Current result:

```text
SLM output format: works
SLM schema path: works
SLM relation quality: fails
SLM speed: too slow with one prompt per pair
```

The strongest current production path remains deterministic Python:

```text
canonical span resolver
-> spaCy dependency parser
-> portable technical relation pattern library
-> typed predicate constraints
-> evidence validation
-> ExtractionResponse / JSONL
```
