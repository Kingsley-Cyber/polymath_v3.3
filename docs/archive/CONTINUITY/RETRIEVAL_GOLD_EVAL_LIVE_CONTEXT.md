# Retrieval Gold Eval Live Context

Last updated: 2026-06-21

This is a live continuity document for the three-tier retrieval evaluation work.
Update it whenever the retrieval eval harness, query set, labels, thresholds, or
baseline numbers change.

## Current Main Checkpoint

Latest pushed commit:

```text
6bde7bd Add three-tier retrieval eval harness
```

Branch policy for this work:

```text
Work directly on main.
Do not create a branch unless the user explicitly asks for one.
```

Important local note:

```text
CONTINUITY/GRAPH_SYNTHESIS_HANDOFF.md is currently untracked and pre-existing.
Do not add or modify it unless the user explicitly asks.
```

## What Exists Now

Three-tier live retrieval runner:

```text
scripts/retrieval_three_tier_eval.py
```

Deterministic validation module:

```text
backend/services/retriever/three_tier_eval.py
```

Default query set:

```text
scripts/retrieval_three_tier_queries.json
```

Docs:

```text
docs/RETRIEVAL_THREE_TIER_E2E.md
```

Unit tests:

```text
backend/tests/test_retrieval_three_tier_validation.py
```

## UI Routes Under Test

```text
Fast Search         -> qdrant_only
Hybrid Search       -> qdrant_mongo
Graph Augmentation  -> qdrant_mongo_graph
```

Use UI names in user-facing reports.

## Current Retrieval Budgets

The previous 35 second blanket standard was rejected as too loose. The current
retrieval/source-prework budgets are route-specific hard gates:

```text
Fast Search         <= 6s
Hybrid Search       <= 8s
Graph Augmentation  <= 8s
```

Total generation time and generation-after-sources are tracked separately.
Generation overages warn by default unless explicitly promoted with:

```text
--fail-total-budget
--fail-generation-budget
```

## Current Query Set

The current five default live queries are:

```text
1. what is python and is ai essentially python
2. natural language processing and data augmentation
3. why are ontologies so powerful
4. how do vector search, hybrid search, and graph augmentation work together in rag
5. why can dense retrieval miss exact tokens and how does bm25 help
```

These run across all three UI routes for 15 live route cases.

## Latest Live Retrieval-Only Baseline

Command:

```bash
set -a
source .env
set +a
scripts/retrieval_three_tier_eval.py \
  --stop-after-sources \
  --pretty \
  --assert \
  --output data_eval/retrieval_three_tier_retrieval_only.json
```

Result:

```text
status: pass
cases: 15
failures: 0
warnings: 0
```

Route timing baseline:

```text
Fast Search:
  avg source/prework: 2.780s
  max source/prework: 3.607s

Hybrid Search:
  avg source/prework: 2.998s
  max source/prework: 4.138s

Graph Augmentation:
  avg source/prework: 3.519s
  max source/prework: 5.250s
```

Quality summary from the retrieval-only run:

```text
Fast Search:
  avg source anchor coverage: 0.867
  avg docs retrieved: 2.6
  no graph facts or relations expected

Hybrid Search:
  avg source anchor coverage: 0.867
  avg docs retrieved: 3.4
  no graph facts or relations expected

Graph Augmentation:
  avg source anchor coverage: 0.933
  avg docs retrieved: 6.2
  graph facts/relations emitted when available
```

Graph evidence observed:

```text
python_ai_distinction:
  facts: 7
  relations: 23

nlp_data_augmentation:
  facts: 13
  relations: 3

ontology_power:
  facts: 5
  relations: 0

rag_tier_architecture:
  facts: 11
  relations: 1

dense_bm25_exact_tokens:
  facts: 10
  relations: 1
```

Current interpretation:

```text
Graph Augmentation is currently the strongest route by coverage, doc spread,
and structured graph evidence.

Hybrid Search is solid and faster than Graph on average for direct evidence.

Fast Search is acceptable for quick semantic recall but weaker for exact,
structured, or multi-evidence questions.
```

## What This Does Not Prove Yet

The current live E2E harness proves:

```text
sources are returned
anchors are covered
graph route emits facts/relations
doc diversity improves
retrieval stays fast
route-specific trace contracts hold
```

It does not yet prove true ranking quality with gold labels.

Missing:

```text
MRR@5
MAP@20
NDCG@8
ExactSourceRecall@8
answer sufficiency rate against labels
graded atom coverage against labels
```

## Gold-Label Eval Plan

Build this next.

### 1. Create Golden Query Set

Start with 25 to 50 queries.

Required categories:

```text
definitions
distinctions
associations
exact-token and BM25 cases
ontology and graph reasoning
multi-hop relationship questions
broad synthesis questions
negative or abstention cases
```

### 2. Export Candidate Evidence

For every golden query, run:

```text
Fast Search
Hybrid Search
Graph Augmentation
```

Capture:

```text
query_id
route UI name
backend tier
rank
chunk_id
doc_id
parent_chunk_id
source_tier
score fields if available
graph facts count
graph relations count
graph advantage trace
retrieval time
```

Keep full private chunk text out of committed artifacts by default.

### 3. Label Relevance

Use graded relevance:

```text
3 = directly answers the query
2 = strong supporting evidence
1 = weak mention or partial context
0 = irrelevant
```

Also label:

```text
exact_source: true/false
answer_atom: definition | classification | relation | example | caveat | procedure | contrast
graph_valid: true/false
notes: short human explanation
```

### 4. Compute Metrics

Use the existing offline metric script as the base:

```text
scripts/retrieval_eval_metrics.py
```

Metrics by route:

```text
Fast Search:
  MRR@5
  Recall@20
  retrieval p95

Hybrid Search:
  MAP@20
  NDCG@8
  ExactSourceRecall@8
  unique_doc_count
  near_duplicate_rate
  retrieval p95

Graph Augmentation:
  NDCG@8
  answer_sufficiency_rate
  atom_coverage
  graph_advantage_score
  facts_used
  relations_used
  multi_doc_evidence_rate
  near_duplicate_rate
  retrieval p95
```

### 5. Initial Gates

Do not treat these as permanent. Tune after the first labeled set.

```text
Fast Search:
  MRR@5 >= 0.75
  retrieval p95 <= 6s

Hybrid Search:
  MAP@20 >= 0.70
  NDCG@8 >= 0.75
  retrieval p95 <= 8s

Graph Augmentation:
  NDCG@8 >= 0.82
  answer_sufficiency >= 0.85
  atom_coverage >= 0.80
  retrieval p95 <= 8s
```

### 6. Regression Runner Target

Build a script that does:

```text
load golden queries
run all three UI routes
join route outputs with gold labels
calculate MRR/MAP/NDCG/answer sufficiency
compare retrieval latency
fail on quality regression or retrieval budget breach
write ignored JSON report under data_eval/
```

Suggested file:

```text
scripts/retrieval_gold_eval.py
```

Suggested fixtures:

```text
data_eval/gold/retrieval_gold_queries.example.json
data_eval/gold/retrieval_gold_labels.example.json
```

If committed examples are needed, place sanitized examples outside ignored
`data_eval/`, or force-add intentionally with no private text.

## GitHub/Public Stack References

Use these as design references, not mandatory dependencies:

```text
Ragas:
  https://github.com/explodinggradients/ragas

DeepEval:
  https://github.com/confident-ai/deepeval

Arize Phoenix:
  https://github.com/Arize-ai/phoenix

Qdrant RAG Eval:
  https://github.com/qdrant/qdrant-rag-eval

Qdrant Improving R in RAG Workshop:
  https://github.com/qdrant/workshop-improving-r-in-rag
```

## Next Agent Instructions

If context compacts, continue from here.

Immediate next task:

```text
Implement the gold-label retrieval eval harness.
```

Do not redo the three-tier harness unless tests show it is broken.

Use the existing live runner for route execution, then add the labeled metrics
join and thresholds.

Before committing future changes:

```bash
python3 -m py_compile \
  backend/services/retriever/three_tier_eval.py \
  scripts/retrieval_three_tier_eval.py

docker compose run --rm -T \
  -v /Users/king/polymath_v3.3/backend:/app \
  backend python -m pytest \
  tests/test_retrieval_three_tier_validation.py \
  tests/test_retrieval_eval_metrics.py -q
```

For live retrieval validation:

```bash
set -a
source .env
set +a
scripts/retrieval_three_tier_eval.py \
  --stop-after-sources \
  --pretty \
  --assert \
  --output data_eval/retrieval_three_tier_retrieval_only.json
```

The live retrieval standard is source/prework latency, not total model
generation latency.

## Fresh Install And Ingest Readiness

Current status on 2026-06-21:

```text
Fresh install contract:
  ./scripts/check-install.sh
  Result: 0 failures, 2 model-directory warnings

Static runtime contracts:
  python3 scripts/verify_runtime_contracts.py --json
  Result: 40 passed, 0 failed

Focused ingest/readiness tests:
  docker compose run --rm -T \
    -v /Users/king/polymath_v3.3/backend:/app \
    backend python -m pytest \
    tests/test_retrieval_readiness.py \
    tests/test_ingest_verify_graph_indexes.py \
    tests/test_worker_phases.py -q
  Result: 22 passed, 1 skipped

Batch path coverage:
  docker compose run --rm -T \
    -v /Users/king/polymath_v3.3/backend:/app \
    backend python -m pytest \
    tests/test_ingest_batches.py \
    tests/test_retrieval_readiness.py \
    tests/test_ingest_verify_graph_indexes.py \
    tests/test_worker_phases.py -q
  Result: 35 passed, 1 skipped
```

Pinned guarantees:

```text
Startup:
  ingestion_service.connect() calls repair_retrieval_readiness_for_all_corpora()
  so existing corpora get Qdrant route collections and Neo4j retrieval schema.

Corpus creation:
  create_corpus() calls ensure_corpus_retrieval_ready() before returning.

Worker setup:
  run_ingest_job calls ensure_corpus_retrieval_ready() before chunk/vector/graph
  writes. A setup failure becomes stage="setup_failed" instead of a partial
  ingest.

Qdrant:
  new corpora get all route collections: naive, hrag, graph, schemas.
  new collections use named dense vector "dense" and sparse vector "sparse"
  with IDF so exact-token lanes remain available.

Neo4j:
  graph-enabled corpora initialize graph constraints, RELATES_TO property
  indexes, and full-text indexes entity_name_ft and fact_text_ft.

Post-ingest verification:
  verify_ingest checks Mongo chunk rows, Qdrant child counts, summary counts,
  payload text contract, probe scroll, Neo4j HAS_CHUNK count, and Neo4j
  retrieval-index readiness. A document only reaches final done when
  write_state.verified is true.

Route validation:
  scripts/retrieval_three_tier_eval.py remains the live route validator for
  Fast Search, Hybrid Search, and Graph Augmentation source/prework budgets.
```

The Downloads checkout at `/Users/king/Downloads/polymath_v3.3-main` is not a
git repository. Use `/Users/king/polymath_v3.3` for commits and validation.
