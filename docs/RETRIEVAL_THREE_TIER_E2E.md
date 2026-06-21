# Three-Tier Retrieval E2E

This repo now has a repeatable live retrieval test for the three UI routes:

| UI route | Backend tier | What the test expects |
| --- | --- | --- |
| Fast Search | `qdrant_only` | Fast semantic recall, sources returned, no graph trace leakage |
| Hybrid Search | `qdrant_mongo` | Hydrated evidence chunks, anchor coverage, no graph trace leakage |
| Graph Augmentation | `qdrant_mongo_graph` | Hybrid evidence plus Graph Advantage trace with Neo4j facts, relations, or expanded chunks |

## Why This Stack

The local runner follows the same evaluation shape used by public RAG eval projects:

- [Ragas](https://github.com/vibrantlabsai/ragas): RAG evaluation loops, test data generation, and app-level metrics.
- [DeepEval](https://github.com/confident-ai/deepeval): black-box E2E eval style for LLM/RAG applications.
- [Arize Phoenix](https://github.com/Arize-ai/phoenix): trace-first debugging, retrieval evals, and experiment comparison.
- [Qdrant RAG Eval](https://github.com/qdrant/qdrant-rag-eval): route comparison across naive, hybrid, and evaluated RAG pipelines.
- [Qdrant Improving R in RAG](https://github.com/qdrant/workshop-improving-r-in-rag): hybrid retrieval, sparse/dense representations, reranking, MMR, and diversity patterns.

We do not add those packages as runtime dependencies. The live test is deterministic and local: it calls this app's real `/api/chat` endpoint and validates the route contracts from the SSE trace.

## Query Set

The default query set lives in:

```text
scripts/retrieval_three_tier_queries.json
```

It covers:

- `python_ai_distinction`: classification and entity distinction.
- `nlp_data_augmentation`: exact concept association.
- `ontology_power`: ontology/graph reasoning.
- `rag_tier_architecture`: the app's own retrieval architecture.
- `dense_bm25_exact_tokens`: dense-vs-sparse exact-token retrieval.

Each case documents its intent, why it exists, expected best route, anchor groups, and expected answer atoms.

## Running It

Start the backend, then run:

```bash
set -a
source .env
set +a
python3 scripts/retrieval_three_tier_eval.py \
  --pretty \
  --assert \
  --output data_eval/retrieval_three_tier_latest.json
```

If `PROBE_TOKEN` is not set, the script logs in with `DEFAULT_ADMIN_USERNAME` and `DEFAULT_ADMIN_PASSWORD`.

Useful focused runs:

```bash
python3 scripts/retrieval_three_tier_eval.py --query-id ontology_power --pretty
python3 scripts/retrieval_three_tier_eval.py --route "Graph Augmentation" --max-queries 2 --pretty
python3 scripts/retrieval_three_tier_eval.py --stop-after-sources --pretty
```

The default retrieval/source budget is strict and route-specific:

```text
Fast Search         source/prework <= 6s
Hybrid Search       source/prework <= 8s
Graph Augmentation  source/prework <= 8s
```

Total answer time is tracked separately because model TTFT/generation can be
slow even when retrieval is healthy. Default total/generation overages are
warnings, not retrieval failures:

```text
Fast Search         total warn > 20s, generation-after-sources warn > 14s
Hybrid Search       total warn > 20s, generation-after-sources warn > 14s
Graph Augmentation  total warn > 25s, generation-after-sources warn > 16s
```

Use `--fail-total-budget` or `--fail-generation-budget` only when the test is
about model latency, not retrieval quality.

## What The Report Captures

Per query and route:

- route UI name and backend tier
- retrieval/source timing
- total generation timing
- generation-after-sources timing
- source count, document spread, parent duplication, source tier counts
- anchor coverage in retrieved sources and final answer
- trace titles and effective tier
- Graph Advantage details for Graph Augmentation
- route-specific failures and warnings

The report intentionally excludes full source text by default. Use `--include-source-text` only for local debugging.

## Metrics Positioning

Live route tests check behavior and latency. Offline relevance metrics are separate:

- `MRR@5`: first good hit, useful for Fast Search.
- `MAP@20`: did the candidate pool retrieve many relevant chunks early.
- `NDCG@8`: graded final evidence-pack quality, most important for Graph Augmentation.
- answer sufficiency: whether selected evidence can actually answer the question.

Use `scripts/retrieval_eval_metrics.py` for labeled offline runs. Do not compute MRR/MAP/NDCG live because they require ground-truth relevance labels.
