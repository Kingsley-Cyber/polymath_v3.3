# Local Extraction Research Report

Date: June 5, 2026

Repo: `/Users/king/polymath_v3.3`

## Executive BLUF

We created and tested a local extraction architecture that keeps the existing Polymath Ghost B schema intact while letting local models operate through a simpler draft schema.

Best current result:

```text
Entity F1:   82.4%
Relation F1: 100.0%
Graph F1:    91.2%
Speed:       ~15,183 chunks/hour on the 5-chunk fixture
```

The strongest design is:

```text
Python high-recall candidates
+ deterministic relation endpoint extraction
+ EntityImportanceRanker
-> ExtractionResponse
-> existing JSONL / Mongo / Neo4j pipeline
```

GLiNER2 is useful as an optional fast span helper, but it should not replace Python candidate generation. GLiREL is useful only as a relation proposal/verifier lane; it does not write final graph output.

## The 5W

## Who

This work is for the Polymath local ingestion and extraction stack.

The target operator is you, running a Mac Studio local deployment where extraction cost, speed, reliability, and overnight unattended ingestion matter.

The engineering role of the system is:

- local models propose candidates
- Python owns deterministic truth, schema, evidence validation, and graph safety
- no generative or cloud model is part of the extraction path

## What

We created a research and benchmark path for local Ghost B extraction.

The core idea was to avoid forcing small local models to output the production Ghost B schema directly. Instead, we tested this architecture:

```text
LocalExtractionDraft
-> OntologyMapper
-> services.ghost_b_schemas.ExtractionResponse
-> compact JSONL
-> existing Mongo / Neo4j pipeline
```

The canonical schema remains unchanged:

```text
entities:  list[LLMEntity]
relations: list[LLMRelation]
facts:     list[LLMFact]
```

We tested local extraction as a compiler problem:

- spans are proposed by Python and optionally GLiNER2
- relations are generated as legal options by Python
- relation endpoints are kept unconditionally
- standalone entities are admitted only if they score as important
- final objects validate through `ExtractionResponse`

## When

This research was performed during the June 5, 2026 local extraction session.

The work happened after the ingestion reliability discussions, model-adherence tests, and local model speed investigations.

## Where

Main repo:

```text
/Users/king/polymath_v3.3
```

Main canonical schema:

```text
backend/services/ghost_b_schemas.py
```

Research scripts created or extended:

```text
scripts/autoresearch_polymath_local_extraction.py
scripts/linear_polymath_model_pipeline.py
scripts/bench_glirel_relation_lane.py
scripts/bench_local_draft_schema_pipeline.py
```

Important benchmark outputs were written under `/tmp`, including:

```text
/tmp/polymath_local_draft_python_keep_100.json
/tmp/polymath_local_draft_union_batch8_small_256_v2.json
/tmp/polymath_local_draft_ranker_th_0.75_cap_20.json
/tmp/polymath_glirel_relation_lane_natural_batch_no_direct.json
/tmp/polymath_glirel_relation_lane_raw_batch_no_direct.json
```

## Why

The original problem was that full local extraction with small fast models was not reliable enough when those models were asked to directly produce rich graph output.

Fast small models were good at:

- span detection
- entity-like phrase detection
- simple classification
- exact evidence proposal
- short structured outputs

They were weak at:

- choosing the correct Polymath predicate
- relation direction
- avoiding weak `related_to`
- handling abstract textbook relations
- distinguishing meaningful ontology edges from co-occurrence

Example failure:

```text
Expected:
database design uses normalization tools

Bad local relation output:
even though data causes data normalization
```

That output can be schema-valid and evidence-backed, but semantically wrong.

The reason for the new architecture is graph safety:

```text
small model = proposer
Python = judge and compiler
existing schema = final contract
```

## How

## 1. We Froze The Final Schema

We kept `ExtractionResponse` as the canonical final contract.

We decided not to add metadata or local-only fields to the final response because `ghost_b_schemas.py` explicitly warns that adding fields requires coordinated parser and graph-writer changes.

Instead, any local-only metadata stays in benchmark reports or draft objects.

## 2. We Created A Local Draft Schema

In `scripts/bench_local_draft_schema_pipeline.py`, we added harness-only Pydantic models:

```python
LocalExtractionDraft
DraftSpan
DraftLink
DraftQualifier
```

The draft schema represents what local models are good at:

```text
spans
links
qualifiers
evidence
metadata
```

The draft schema is not the production schema. It compiles into the production schema.

## 3. We Created An OntologyMapper Compiler

The harness added an `OntologyMapper` that converts draft spans and relation options into the existing Ghost B shape.

It handles:

- span to entity mapping
- entity type mapping
- evidence candidates
- legal relation options
- direct relation acceptance
- endpoint pruning
- standalone entity importance scoring
- `ExtractionResponse` validation
- JSONL conversion

The mapper keeps model output from directly polluting the graph.

## 4. We Tested GLiREL

We installed and loaded GLiREL in a temporary venv:

```text
/tmp/polymath_relation_models_py310_venv
```

Model tested:

```text
jackboyla/glirel-large-v0
```

Findings:

- GLiREL uses a DeBERTa-style 512-token encoder context.
- It required loader workarounds because the package had a `huggingface_hub` wrapper mismatch.
- It also required tokenizer dependencies such as `sentencepiece`, `protobuf`, and `tiktoken`.
- It was fast enough to test, but relation quality failed.

GLiREL-alone results:

```text
Natural labels:
Relation F1: 0.0%
TP/FP/FN:    0/19/21

Raw predicates:
Relation F1: 4.2%
TP/FP/FN:    1/26/20
```

Decision:

```text
Do not use GLiREL as a final relation writer.
```

Possible future role:

```text
cheap relation verifier on short sentence windows only
```

## 5. We Tested GLiNER2

Model tested:

```text
fastino/gliner2-base-v1
```

Role tested:

```text
fast entity/span proposer
```

Context:

```text
512 encoder tokens
```

Important speed results:

```text
GLiNER2 single call:
~0.27 to 0.32 seconds per 420-token fixture chunk

GLiNER2 optimized native batch:
small labels, batch_size=8, max_len=256
~36,733 chunks/hour for raw model calls
```

But full pipeline speed was lower because Python candidate generation and relation option compilation became the dominant work.

GLiNER2-only result:

```text
Schema:       5/5
Relation F1: 66.7%
Graph F1:    56.0%
```

Decision:

```text
GLiNER2 should assist span discovery.
GLiNER2 should not replace Python candidate generation.
```

## 6. We Tested Python Direct Extraction

Python direct extraction with endpoint pruning gave:

```text
Entity F1:   59.5%
Relation F1: 100.0%
Graph F1:    79.8%
Speed:       ~34,291 chunks/hour
```

This proved Python could produce perfect relation endpoints on the fixture, but it missed standalone entities.

## 7. We Diagnosed Entity Misses

Missed standalone entities included:

```text
hallucinations
RAG
agent
feedback loop
Airbnb
lookup table
object detection
speech recognition
documentation
declaration
Mike Hernandez
```

The key finding:

```text
The candidate generator was not actually missing the entities.
```

At candidate breadth `160`, raw candidate recall was:

```text
Entity candidate recall: 100%
TP/FN: 85/0
```

The low final Entity F1 was caused by output policy:

```text
endpoint-only pruning kept the graph clean but dropped standalone concepts
```

No-prune was too noisy:

```text
keep 120 no prune:
Entity F1: 24.2%
Entity FP: 517
Relation F1: 100%
```

So the solution was not "more spans." The solution was importance ranking.

## 8. We Created EntityImportanceRanker

The ranker scores standalone candidates that are not relation endpoints.

Features include:

- repetition count
- acronym detection
- definition pattern detection
- heading presence
- domain term boost
- concept marker hits
- candidate score
- multi-word phrase boost
- capitalization boost
- short/generic penalties

Best tested setting:

```text
standalone_importance_threshold = 0.75
keep_standalone_entities = 20
```

Result:

```text
Entity Precision: 82.4%
Entity Recall:    82.4%
Entity F1:        82.4%

Relation Precision: 100.0%
Relation Recall:    100.0%
Relation F1:        100.0%

Graph F1: 91.2%
Speed:    ~15,183 chunks/hour
```

This was the best overall ontology result from the tested configurations.

## Benchmark Summary

| Pipeline | Entity F1 | Relation F1 | Graph F1 | Speed |
|---|---:|---:|---:|---:|
| Python endpoint-only | 59.5% | 100.0% | 79.8% | ~34,291 chunks/hr |
| GLiNER2 only | 45.4% | 66.7% | 56.0% | ~9,195 chunks/hr |
| Python + GLiNER2 union endpoint-only | 59.5% | 100.0% | 79.8% | ~14,000 to 15,000 chunks/hr |
| Python no-prune keep120 | 24.2% | 100.0% | 62.1% | ~28,253 chunks/hr |
| Python + EntityImportanceRanker | 82.4% | 100.0% | 91.2% | ~15,183 chunks/hr |

## Current Best Architecture

Recommended local extraction lane:

```text
1. Python high-recall candidate generation
2. Python direct relation endpoint generation
3. Keep relation endpoint entities unconditionally
4. Run EntityImportanceRanker on standalone candidates
5. Compile to ExtractionResponse
6. Validate Pydantic + exact evidence
7. Convert to existing compact JSONL
8. Send through existing Mongo / Neo4j pipeline
```

Optional model use:

```text
Use GLiNER2 only when Python coverage is weak or no direct relation options exist.
Use GLiREL only as a relation proposal/verifier over Python-owned options.
Discard low-confidence or invalid GLiREL proposals outright; do not rerank them with another model.
Do not let any model write final graph JSON directly.
```

## Model Decisions

## GLiNER2

Chosen role:

```text
optional fast span assist
```

Not chosen as:

```text
primary final extractor
```

Why:

```text
Good speed, useful spans, but weaker final relation coverage when used alone.
```

## GLiREL

Chosen role:

```text
optional relation proposal/verifier lane
```

Why:

```text
It can score relation options, but relation quality depends on clean Python-owned entity spans and evidence windows.
It must not write final graph output.
```

## Python

Chosen role:

```text
primary deterministic compiler and graph-safety layer
```

Why:

```text
It produced perfect relation F1 on the fixture and can enforce evidence, schema, and direction rules.
```

## Context Window Notes

GLiNER2:

```text
512 encoder tokens
```

GLiREL:

```text
512 encoder tokens
```

## What We Did Not Change

We did not change the canonical backend schema.

We did not push a commit to GitHub.

We did not integrate this into production ingestion yet.

We did not change Mongo or Neo4j writer contracts.

This work remains research harness code until explicitly promoted.

## GitHub Access Test

We tested GitHub access.

Result:

```text
Repo: Kingsley-Cyber/polymath_v3.3
Permission: ADMIN
Push dry-run: succeeded
```

No branch or commit was created by the access test.

## Remaining Issues

Remaining misses from the best ranker run:

```text
Mike Hernandez
hallucinations
agent
finetuning
parameter-efficient finetuning
developers
documentation
declaration
```

Remaining extras:

```text
ml
dmls
aie
sidebar reading ai engineering
simple each zip code
block cn
```

Next cleanup should add:

```text
1. markup/sidebar rejection rules
2. conditional acronym filtering
3. person-name boost
4. domain-term boosts for hallucination, agent, finetuning, documentation, declaration
```

## Recommended Next Patch

Implement production-ready local extraction as a feature-flagged adapter:

```text
LOCAL_DRAFT_EXTRACTION_ENABLED=false
LOCAL_DRAFT_SPAN_SOURCE=python
LOCAL_DRAFT_USE_GLINER2_ASSIST=false
LOCAL_DRAFT_USE_GLIREL_RELATION_SCORER=false
LOCAL_DRAFT_IMPORTANCE_THRESHOLD=0.75
LOCAL_DRAFT_MAX_STANDALONE_ENTITIES=20
```

Add tests for:

```text
1. LocalExtractionDraft validation
2. OntologyMapper compilation
3. relation endpoint preservation
4. standalone entity ranker thresholding
5. exact evidence gate
6. JSONL conversion compatibility
7. no change to canonical ExtractionResponse path
```

Acceptance criteria:

```text
Final ExtractionResponse remains unchanged.
Relation F1 does not regress.
Bad local model output cannot write directly to graph.
Standalone entity count is capped.
Every accepted entity and relation has exact source evidence.
```

## Final Recommendation

Build the production patch around this:

```text
LocalExtractionDraft -> OntologyMapper -> ExtractionResponse
```

Use:

```text
Python direct relation endpoint extraction
+ EntityImportanceRanker threshold 0.75
+ max 20 standalone entities per chunk
```

Keep GLiNER2 as optional assist, not always-on.

Keep GLiREL as an optional proposal/verifier lane only; Python remains the final compiler and judge.

This gives the best balance so far:

```text
fast
deterministic
schema-safe
ontology-aligned
high graph quality on the fixture
```
