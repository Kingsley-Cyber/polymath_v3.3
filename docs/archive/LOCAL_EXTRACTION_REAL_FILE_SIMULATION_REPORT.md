# Local Extraction Real File Simulation Report

Date: 2026-06-05

## BLUF

We simulated a real ingestion-style local extraction run using the flash drive route:

`/Volumes/Flash Drive/merged`

Test file:

`/Volumes/Flash Drive/merged/13_building_ai_mobile_apps_2025.md`

The GLiNER-family architecture is promising for speed and schema safety, but not ready as a full Ghost B replacement yet.

What works:

- The repo parser/chunker can turn the real markdown file into ingestion-like child chunks.
- Python validation keeps the final output compatible with `services.ghost_b_schemas.ExtractionResponse`.
- GLiNER2 is fast and safe as an entity/span helper.
- GLiREL can select relation candidates and produce accepted relations.
- Evidence gates pass because Python maps accepted items back to exact source text.

What does not work yet:

- Raw chunk routing is too weak. Medium navigation, byline, footer, and recirculation chunks reach extraction.
- Candidate spans are too noisy: examples include `tensorflow lite brings`, `face s model`, `s using`, `don`.
- GLiREL relation quality depends heavily on clean entity spans. It selected relations, but many were semantically ugly because the span candidates were ugly.
- GLiNER2 alone does not produce relations in the current harness.

The correct next implementation is not "wire this directly into production." The correct next step is:

`content router -> clean span candidates -> GLiNER2 entity assist -> GLiREL relation scorer -> Python compiler -> ExtractionResponse -> JSONL`

## 5W Summary

### Who

This test was for the Polymath local extraction pipeline, specifically the proposed GLiNER-family local Ghost B path.

The model roles tested:

- `fastino/gliner2-base-v1`: fast entity/span proposer.
- `jackboyla/glirel-large-v0`: relation scoring lane.
- Python: deterministic judge, evidence validator, ontology compiler, and JSONL writer.

The final contract stayed unchanged:

`services.ghost_b_schemas.ExtractionResponse`

Final object shape:

```json
{
  "entities": [],
  "relations": [],
  "facts": []
}
```

Then Python converts accepted objects to compact Ghost B JSONL.

### What

We simulated a real file ingestion by:

1. Reading a real markdown file from the flash drive.
2. Parsing it through the repo document parser.
3. Chunking it through the repo tier chunker.
4. Filtering out code chunks, matching Ghost B behavior.
5. Running local extraction harnesses on the resulting body chunks.
6. Running a second pass with an obvious Medium-junk content router.
7. Inspecting accepted entities and relations for schema safety and semantic quality.

Generated files:

- `/tmp/polymath_real_ingest_13_building_ai_chunks.jsonl`
- `/tmp/polymath_real_ingest_13_building_ai_body_chunks.jsonl`
- `/tmp/polymath_real_ingest_13_building_ai_content_chunks.jsonl`
- `/tmp/polymath_real_ingest_13_building_ai_meta.json`

Result reports:

- `/tmp/polymath_real_ingest_13_building_ai_union_pruned_ranked_report_v2.json`
- `/tmp/polymath_real_ingest_13_building_ai_glirel_raw_sequential_report_v3.json`
- `/tmp/polymath_real_ingest_13_building_ai_content_union_report.json`
- `/tmp/polymath_real_ingest_13_building_ai_content_glirel_report.json`

### When

The simulation was run on 2026-06-05.

The source markdown article itself says October 16, 2025 in its byline, but that is source content, not the test date.

### Where

Source route:

`/Volumes/Flash Drive/merged`

Source file:

`/Volumes/Flash Drive/merged/13_building_ai_mobile_apps_2025.md`

Repo used:

`/Users/king/polymath_v3.3`

The provided working directory `/Users/king/Downloads/polymath_v3.3-main` was not a git repo, so the active repo was `/Users/king/polymath_v3.3`.

### Why

The goal was to test whether the local GLiNER/GLiREL concept can behave like a real ingestion pipeline:

- process real chunked file text,
- maintain Polymath schema safety,
- avoid graph pollution,
- run fast enough to matter locally,
- and produce useful entity/relation graph output without cloud extraction calls.

The important theory being tested:

Models should not be trusted to write final graph JSON directly. Models should propose spans or relation scores. Python should own the schema, evidence, ontology mapping, validation, pruning, and JSONL conversion.

## Chunking Result

The real file parsed and chunked successfully.

```json
{
  "file": "/Volumes/Flash Drive/merged/13_building_ai_mobile_apps_2025.md",
  "bytes": 70502,
  "mime": "text/markdown",
  "source_tier": "tier_a",
  "source_format": "local_markdown",
  "has_structure": true,
  "h1_count": 1,
  "h2_count": 37,
  "parent_count": 40,
  "child_count": 70,
  "chunk_kinds": {
    "code": 10,
    "body": 60
  },
  "token_total": 17013,
  "token_min": 4,
  "token_avg": 243.0,
  "token_p50": 220.0,
  "token_max": 570
}
```

Ghost B normally skips code chunks, so the first local extraction pass used the 60 body chunks.

Body-only stats:

```json
{
  "source_rows": 70,
  "body_rows": 60,
  "token_total": 14771,
  "token_max": 512
}
```

Then a simple Medium-junk router removed navigation, sign-in, footer, and recirculation chunks.

Clean article-body stats:

```json
{
  "chunks": 27,
  "token_total": 5143,
  "token_avg": 190.48,
  "token_max": 512
}
```

## Harness Fixes Made

Two research harness bugs were found and fixed.

### Fix 1: GLiNER2 Entity Harness Pruning

File:

`/Users/king/polymath_v3.3/scripts/bench_local_draft_schema_pipeline.py`

Issue:

When pruning to relation endpoints was enabled, chunks with zero relations still kept all candidate entities.

Impact:

Chunks with no real graph relation could produce bloated standalone entity output.

Fix:

Apply pruning whenever `--prune-entities-to-relation-endpoints` is enabled, not only when relations exist.

### Fix 2: GLiREL Empty NER Crash

File:

`/Users/king/polymath_v3.3/scripts/bench_glirel_relation_lane.py`

Issue:

GLiREL crashed when a chunk had fewer than two entity spans.

Impact:

A single empty/no-relation chunk could kill the whole real-file benchmark.

Fix:

Skip GLiREL calls for chunks with fewer than two NER spans and record empty predictions.

### Fix 3: GLiREL Entity Pruning

File:

`/Users/king/polymath_v3.3/scripts/bench_glirel_relation_lane.py`

Issue:

The GLiREL harness had the same pruning bug: chunks with no accepted relations kept all candidates.

Impact:

The first GLiREL run showed 2,384 accepted entities, which was not a real graph result.

Fix:

Always prune entities to accepted relation endpoints when endpoint pruning is enabled.

Corrected result:

79 entities and 57 relations on all 60 body chunks.

## Results

## Current Stack Compatibility Test

After the model/harness runs, we tested the local workflow output against the current backend Ghost B parser path:

```text
compact JSONL
-> backend.services.ghost_b._parse_jsonl_lines
-> backend.services.ghost_b._parse_jsonl_items
-> backend.services.ghost_b.ExtractionResult
```

Compatibility verifier:

`/Users/king/polymath_v3.3/scripts/verify_local_extraction_stack_compat.py`

### Routed Article GLiREL Output

Input report:

`/tmp/polymath_real_ingest_13_building_ai_content_glirel_report.json`

Compatibility output:

`/tmp/polymath_real_ingest_13_building_ai_content_glirel_stack_compat.json`

Result:

```json
{
  "samples_seen": 27,
  "clean_pydantic_pass": 27,
  "clean_pydantic_fail": 0,
  "jsonl_finished": 27,
  "jsonl_invalid": 0,
  "backend_parse_pass": 27,
  "backend_parse_fail": 0,
  "local_entities": 44,
  "local_relations": 33,
  "local_facts": 0,
  "backend_entities": 44,
  "backend_relations": 33,
  "backend_facts": 0,
  "backend_evidence_drops": 0,
  "backend_entity_schema_drops": 0,
  "backend_relation_schema_drops": 0,
  "backend_domain_range_remaps": 0
}
```

Interpretation:

The routed GLiREL workflow output is wire-compatible with the current stack. The backend parser kept exactly the same entity/relation counts and did not drop anything through evidence, schema, or domain/range gates.

### Routed Article GLiNER2 Entity Output

Input report:

`/tmp/polymath_real_ingest_13_building_ai_content_union_report.json`

Compatibility output:

`/tmp/polymath_real_ingest_13_building_ai_content_union_stack_compat.json`

Result:

```json
{
  "samples_seen": 27,
  "clean_pydantic_pass": 27,
  "jsonl_finished": 27,
  "backend_parse_pass": 27,
  "local_entities": 14,
  "local_relations": 0,
  "backend_entities": 14,
  "backend_relations": 0,
  "backend_evidence_drops": 0,
  "backend_entity_schema_drops": 0,
  "backend_relation_schema_drops": 0
}
```

Interpretation:

The GLiNER2 entity-helper output is also wire-compatible, but it is not a full extraction lane because it emits no relations in this harness.

### All Body GLiREL Output

Input report:

`/tmp/polymath_real_ingest_13_building_ai_glirel_raw_sequential_report_v3.json`

Compatibility output:

`/tmp/polymath_real_ingest_13_building_ai_body_glirel_stack_compat.json`

Result:

```text
60/60 clean Pydantic pass
60/60 JSONL finished
60/60 backend parse pass
79/57/0 local E/R/F -> 79/57/0 backend E/R/F
0 evidence/schema/domain drops
```

Interpretation:

Even the broader all-body run is stack-compatible. Its problem is content quality because it includes Medium navigation and footer chunks, not schema compatibility.

## Deterministic Quality Test

After stack compatibility was verified, we created a separate deterministic quality test with a fixed answer sheet.

This is the important distinction:

```text
Stack compatibility = does the output parse and survive Ghost B gates?
Quality test = did it extract the right graph?
```

Fixture files:

```text
/Users/king/polymath_v3.3/scripts/local_extraction_fixtures/13_building_ai_mobile_apps_10_chunks.jsonl
/Users/king/polymath_v3.3/scripts/local_extraction_fixtures/13_building_ai_mobile_apps_gold_v1.json
/Users/king/polymath_v3.3/scripts/local_extraction_fixtures/13_building_ai_mobile_apps_gold_criteria_v1.md
```

Scorer:

```text
/Users/king/polymath_v3.3/scripts/score_local_extraction_gold.py
```

Gold max score:

```text
Gold chunks:       10
Max entity TP:     165
Max relation TP:    75
Max total TP:      240
```

Grade bands:

```text
>= 95% graph F1: production_candidate
>= 90% graph F1: near_cloud_target
>= 80% graph F1: research_promising
>= 65% graph F1: prototype_only
<  65% graph F1: not_ready
```

Current routed GLiREL score:

```text
Entity precision:   34.3%
Entity recall:       7.3%
Entity F1:          12.0%

Relation precision:  7.1%
Relation recall:     2.7%
Relation F1:         3.9%

Graph F1:            7.9%
Grade:               not_ready

TP/FP/FN entities:   12 / 23 / 153
TP/FP/FN relations:   2 / 26 / 73
```

Score output:

`/tmp/polymath_real_ingest_13_building_ai_content_glirel_gold_score_v1.json`

Interpretation:

The workflow is wire-compatible with the current stack but fails the deterministic quality bar. This confirms the next engineering patch must improve content routing, span canonicalization, and relation candidate construction before production integration.

### GLiNER2 + Python Entity Lane On 60 Body Chunks

Model:

`fastino/gliner2-base-v1`

Settings:

- span source: `union`
- threshold: `0.15`
- label profile: `small`
- batch size: `8`
- max len: `256`
- standalone threshold: `0.75`

Result:

```json
{
  "samples": 60,
  "chunks_per_hour_wall": 10746.33,
  "schema_pass_rate": 1.0,
  "accepted_entities": 40,
  "accepted_relations": 0,
  "evidence_errors": 0,
  "gate_failures": ["accepted_relations_zero"]
}
```

Interpretation:

GLiNER2 is fast and safe as an entity/span helper. It is not a full local Ghost B extractor in this harness because it produced zero accepted relations.

### GLiREL Relation Lane On 60 Body Chunks

Model:

`jackboyla/glirel-large-v0`

Settings:

- prediction mode: `sequential`
- label profile: `raw`
- model threshold: `0.05`
- selected threshold: `0.05`
- top-k: `3`

Result after pruning fixes:

```json
{
  "samples": 60,
  "chunks_per_hour_wall": 6584.27,
  "inference_latency_p50_s": 0.453,
  "inference_latency_p95_s": 1.141,
  "schema_pass_rate": 1.0,
  "accepted_entities": 79,
  "accepted_relations": 57,
  "evidence_errors": 0,
  "gate_failures": []
}
```

Interpretation:

GLiREL can produce relation-backed graph output inside the Python cage. But all-body input includes Medium junk, which produces bad graph candidates.

### GLiNER2 + Python Entity Lane On 27 Routed Article Chunks

Result:

```json
{
  "samples": 27,
  "chunks_per_hour_wall": 14901.0,
  "schema_pass_rate": 1.0,
  "accepted_entities": 14,
  "accepted_relations": 0,
  "evidence_errors": 0,
  "gate_failures": ["accepted_relations_zero"]
}
```

Interpretation:

Entity lane is very fast, but too sparse after the standalone ranker. It needs better domain term boosting, cleaner span dedupe, and a stronger entity importance policy.

### GLiREL Relation Lane On 27 Routed Article Chunks

Result:

```json
{
  "samples": 27,
  "chunks_per_hour_wall": 9075.25,
  "inference_latency_p50_s": 0.403,
  "inference_latency_p95_s": 0.719,
  "schema_pass_rate": 1.0,
  "accepted_entities": 44,
  "accepted_relations": 33,
  "evidence_errors": 0,
  "gate_failures": []
}
```

Interpretation:

After routing out obvious junk, GLiREL still finds relations and gets faster. This supports GLiREL as a potential relation scorer, but not yet as production-quality output.

## Quality Inspection

Good sign:

The relation lane preserves schema and evidence safety. Every accepted relation maps back to exact evidence text.

Bad sign:

Many accepted relation endpoints are malformed because the candidate generator is too permissive.

Examples:

```text
assistant using llm uses llm s api
tensorflow lite brings supports gpu delegate
face s model supports model hub
s using uses app called suki
adaptation lora represents lora represents weight updates
```

These are not hallucinations in the usual sense. They are candidate-span hygiene failures. The model is selecting from bad options that Python gave it.

That means the next patch should focus on candidate quality before model quality.

## What This Proves

### Proved

- Real repo parsing/chunking works on the flash drive file.
- Python can keep the canonical Polymath schema unchanged.
- Evidence gates prevent unsupported text from entering JSONL.
- GLiNER2 is useful as a fast span/entity assist lane.
- GLiREL can produce relation selections at useful local speed.
- Content routing matters as much as model choice.

### Not Proved

- This is not yet near-cloud extraction quality.
- This is not ready to write Neo4j relations in production.
- The current candidate generator is not clean enough for trusted relation graph writes.
- There is no gold score on this real article yet, because no hand-labeled expected entities/relations were created for it.

## Senior Engineering Diagnosis

The local model architecture is directionally right, but the current weak point is upstream of GLiREL.

The failure chain:

1. The markdown parser includes Medium navigation, byline, footer, and recirculation content.
2. The chunker emits those as normal body chunks.
3. The candidate generator extracts malformed spans from noisy text and awkward noun phrases.
4. GLiREL scores relation options built from those malformed spans.
5. Python validates schema and evidence correctly, but evidence validity does not guarantee semantic quality.
6. Bad-but-evidence-backed relations can still pass if span quality is bad.

Atomic root cause:

Python is enforcing truth against the chunk text, but the candidate generator is not enforcing entity-span quality tightly enough before relation scoring.

## Recommended Implementation Architecture

Do not implement local extraction as one model call.

Implement this production line:

```text
file
-> parser
-> chunker
-> content router
-> code/nav/footer/table/noise classifier
-> clean span candidate generator
-> GLiNER2 entity assist
-> entity canonicalizer and deduper
-> relation evidence window builder
-> GLiREL relation scorer
-> Python ontology compiler
-> Pydantic ExtractionResponse validation
-> exact evidence gate
-> compact JSONL
-> existing Ghost B parse/write path
```

## Required Next Fixes

### 1. Content Router

Add a pre-extraction router that can skip:

- Medium nav/sidebar/footer chunks
- byline/login/share/listen chunks
- read-next/recirculation chunks
- URL-heavy chunks
- bookmark/sign-in action URLs
- image placeholder chunks
- reference-only junk

The test proved this matters immediately: 60 body chunks became 27 useful article chunks.

### 2. Candidate Span Hygiene

Reject or repair spans like:

- possessive fragments: `face s model`
- verb-contaminated noun phrases: `tensorflow lite brings`
- partial contractions: `don`, `s using`
- generic fragments: `model`, `code`, `users` unless relation-backed or strongly important
- URL/domain fragments unless the ontology type is explicitly URL/software/source

### 3. Canonical Span Dedupe

When candidates overlap, prefer the cleanest entity:

- prefer `TensorFlow Lite` over `TensorFlow Lite brings`
- prefer `GPU delegate` over `GPU delegate for hardware`
- prefer `Hugging Face model hub` over `face s model`
- prefer `Suki` over `app called Suki`

### 4. Relation Window Builder

Do not feed whole child chunks to GLiREL when the relation evidence is sentence-level.

Build sentence/evidence windows:

```text
candidate sentence
-> clean entities in same sentence
-> candidate relation options
-> GLiREL
```

This also avoids GLiREL's 512-token internal truncation warnings.

### 5. Gold Fixture For This File

Create a small gold set from this article:

- 10 chunks
- expected entities
- expected relations
- rejected junk chunks

Then report:

- entity precision
- entity recall
- relation precision
- relation recall
- graph F1
- chunks/hour

Without gold, we can measure schema safety and speed, but not true extraction quality.

## Production Readiness Call

Current state:

- GLiNER2 entity helper: promising, not enough alone.
- GLiREL relation scorer: promising, but blocked by span quality.
- Python schema/evidence cage: working.
- Full local Ghost B replacement: not ready.

Recommended flag state:

```env
LOCAL_GHOST_B_ENABLED=false
LOCAL_GHOST_B_ENTITY_HELPER=gliner2
LOCAL_GHOST_B_RELATION_SCORER=glirel
LOCAL_GHOST_B_REQUIRE_CONTENT_ROUTER=true
LOCAL_GHOST_B_REQUIRE_GOLD_PASS=true
LOCAL_GHOST_B_FACTS=false
```

The next implementation should be a disabled-by-default research/preview lane, not a production write path.

## Bottom Line

The architecture is correct:

`models propose, Python decides`

The schema cage works.

The speed is useful.

The current quality blocker is not the final Polymath schema. It is dirty chunk routing and dirty span candidates before GLiREL.

Fix the router and span canonicalizer first. Then rerun the same real-file simulation with a hand-labeled 10-chunk gold set. If relation precision clears the target, then integrate behind a disabled local extraction flag.

## 2026-06-05 Iteration: Gap Diagnostics And Candidate Hygiene

After the first answer-sheet run, we added a deterministic gap diagnostic to:

```text
/Users/king/polymath_v3.3/scripts/score_local_extraction_gold.py
```

The scorer now reports:

- missed entity gap category
- inferred entity type
- surface-form features
- context features
- dirty extra entity counts
- per-chunk missed/extra detail

This made the failure mode concrete:

```text
Original routed GLiREL answer-sheet score:
Entity P/R/F1:   34.3% / 7.3% / 12.0%
Relation P/R/F1: 7.1% / 2.7% / 3.9%
Graph F1:        7.9%
```

The diagnostic showed most misses were not pure model failures. Many gold entities were already in candidate space but were pruned or outranked by bad spans:

```text
candidate_not_selected_or_pruned: 119
missing_from_candidate_generator: 18
candidate_generator_can_find_but_report_did_not_include: 9
```

Examples of dirty accepted/proposed spans:

```text
assistant using llm
using llm s
tensorflow lite brings
face s model
gpu delegate for hardware
```

### Patch Applied

We patched the research candidate layer in:

```text
/Users/king/polymath_v3.3/scripts/autoresearch_polymath_local_extraction.py
/Users/king/polymath_v3.3/scripts/bench_glirel_relation_lane.py
```

Changes:

- canonical matching now removes possessive `s` noise
- phrase scoring rejects verb-contaminated fragments
- phrase scoring boosts mobile/AI technical terms
- GLiREL lane can keep standalone entities after freezing relation endpoints
- answer-sheet scorer can emit gap diagnostics

### Post-Patch Results

Pure Python candidate/ranker path:

```text
Output:
/tmp/polymath_python_ranker_cleaned_fixture_v1.json

Score:
/tmp/polymath_python_ranker_cleaned_fixture_score_v1.json

Entity P/R/F1:   19.2% / 44.8% / 26.9%
Relation P/R/F1: 0.0% / 0.0% / 0.0%
Graph F1:        13.5%
Speed:           ~13,760 chunks/hour
```

Cleaned GLiREL relation lane:

```text
Output:
/tmp/polymath_glirel_cleaned_ranker_fixture_v1.json

Score:
/tmp/polymath_glirel_cleaned_ranker_fixture_score_v1_best.json

Entity P/R/F1:   19.5% / 47.3% / 27.6%
Relation P/R/F1: 11.4% / 5.3% / 7.3%
Graph F1:        17.4%
Speed:           ~3,083 chunks/hour
```

### Interpretation

The patch improved the local lane, but not enough for production:

- entity recall improved from `7.3%` to `47.3%`
- missing-from-generator dropped from `18` to `10`
- relation F1 improved from `3.9%` to `7.3%`
- graph F1 improved from `7.9%` to `17.4%`

This proves candidate hygiene matters, but also proves the remaining blocker is deeper than threshold tuning.

The current local GLiNER/GLiREL path is still not near cloud extraction quality. It is stack-compatible and faster than generative extraction, but it is not semantically reliable enough to write graph output.

### Next Required Fix

The next patch should focus on relation option construction:

```text
clean sentence window
-> non-overlapping canonical spans
-> typed entity pairs
-> explicit predicate cue rules
-> GLiREL scores only those clean relation options
-> Python accepts only supported options
```

Do not integrate this lane until the deterministic answer sheet clears at least:

```text
Entity F1 >= 85%
Relation F1 >= 85%
Relation precision >= 90%
Graph F1 >= 90%
```

## 2026-06-05 Follow-Up: Python-Only Relation Compiler Test

After reviewing the GLiREL results, we tested the stronger hypothesis:

```text
If Python can build clean relation options, Python should compile relations directly.
GLiREL should not be in the critical path.
```

New script:

```text
/Users/king/polymath_v3.3/scripts/bench_python_deterministic_relation_compiler.py
```

### Current Direct Rules

Command output:

```text
Output:
/tmp/polymath_python_current_direct_mobile_fixture_v1.json

Entity P/R/F1:   24.2% / 56.4% / 33.8%
Relation P/R/F1: 0.0% / 0.0% / 0.0%
Graph F1:        16.9%
Speed:           ~16,254 chunks/hour
```

Interpretation:

The earlier Python direct relation engine has not been ported to this mobile-app fixture yet. It is fast and schema-safe, but it emitted zero relations here.

### Fixture-Seeded Deterministic Compiler

Command output:

```text
Output:
/tmp/polymath_python_fixture_seeded_mobile_fixture_v1.json

Score:
/tmp/polymath_python_fixture_seeded_mobile_fixture_score_v1.json

Compatibility:
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

This was a compiler sanity test, not a generalization test. The fixture-seeded mode uses the fixture answer sheet as deterministic relation templates. It proves Python can compile the current schema and relations correctly when domain rules/spans exist. It does not prove the general mobile-domain rule set is complete yet.

The one missed relation was:

```text
Hugging Face model hub references mobile tags
```

because `mobile tags` was not recovered as a usable entity.

### Updated Decision

Drop GLiREL from the production critical path.

Recommended production direction:

```text
Python high-recall span generator
-> optional GLiNER2 span assist
-> canonical span resolver
-> sentence-level relation windows
-> deterministic Python relation compiler
-> EntityImportanceRanker
-> ExtractionResponse
-> compact JSONL
```

## 2026-06-05 Follow-Up: spaCy Relation Compiler Simulation

We then tested whether spaCy dependency parsing could replace GLiREL as the
relation layer on the same 10 real chunks and answer sheet.

### spaCy Hybrid Compiler

Output:

```text
/tmp/polymath_python_spacy_rules_mobile_fixture_v3.json
```

Compatibility:

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

### Strict spaCy SVO Compiler

Output:

```text
/tmp/polymath_python_spacy_svo_mobile_fixture_v2.json
```

Compatibility:

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

### What This Proves

The output works with the current stack. Pydantic validation, compact JSONL
conversion, and backend Ghost B parsing all passed.

The quality still does not clear the production gate. Direct SVO parsing reduced
some false positives, but the mobile-app gold sheet contains many technical
relations that are not simple grammatical SVO edges. The missing layer is a
portable technical relation pattern library:

```text
canonical span resolver
-> spaCy dependency parser
-> technical pattern handlers
-> typed predicate constraints
-> evidence gate
-> ExtractionResponse / JSONL
```

That is the next implementation target.

## 2026-06-05 Follow-Up: MLX SLM Relation Proposer Simulation

We tested whether a small off-the-shelf MLX instruction model could act as a
relation proposer between Python-owned entity pairs.

New script:

```text
/Users/king/polymath_v3.3/scripts/bench_mlx_slm_relation_proposer.py
```

The model was sandboxed. It only proposed:

```text
1 <predicate> 2
2 <predicate> 1
none
```

Python still produced the final `ExtractionResponse` and compact JSONL.

### Qwen2.5-1.5B Few-Shot Full Fixture

Output:

```text
/tmp/polymath_mlx_slm_qwen25_15b_fewshot_v1.json
```

Compatibility:

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

The model obeyed the command format, but proposed relations for nearly every
candidate pair. This caused graph pollution even though the stack accepted the
output.

### Short Model Probes

Two-chunk zero-shot probes:

```text
Qwen2.5-1.5B:
Relation F1: 0.0%
Relation TP/FP/FN: 0/50/8

Qwen3-1.7B:
Relation F1: 4.9%
Relation TP/FP/FN: 1/32/7
Speed: ~193 chunks/hour

Llama-3.2-1B:
Relation F1: 0.0%
Relation TP/FP/FN: 0/13/8
Speed: ~295 chunks/hour
```

### Decision

The SLM proposer architecture is technically compatible with the current stack,
but it is not quality-compatible yet.

Do not use a raw off-the-shelf SLM pair proposer as a production relation
writer. It needs cue-window prefiltering, spaCy/rule agreement, calibrated
no-relation thresholds, or fine-tuning before it can help.
