# T5.6 Qwen3 universal query-instruction A/B — rejected

Date: 2026-07-14

Baseline: `baseline_live_v0` / `qwen3-retrieval-query-v1`

Candidate: `universal` / `embedding_instruction_registry.v1.universal`

Verdict: **REJECTED and reverted before reporting**

## Frozen promotion gates

- Same held-out IDs; zero runtime errors.
- Naïve and cross-corpus mean document recall strictly improve.
- No answer shape loses a document hit.
- No shape loses more than 5 points mean document recall.
- Negative controls remain 5/5 fail-closed.
- Mean latency remains within +20% of baseline.

The gates are implemented by
`backend/scripts/compare_embedding_instruction_ab.py`. Candidate artifacts use
`run_heldout_eval.py --out-suffix` so canonical baselines cannot be overwritten.

## Execution and decisive result

Candidate command:

```text
TOKEN=$(docker exec polymath_v33-backend-1 cat /tmp/probe_token) \
python3 backend/scripts/run_heldout_eval.py --tier qdrant_only \
  --out-suffix qwen3_universal_v1
```

The Fast run stopped immediately after a decisive gate failure at 32/58 rows
(intentional process exit 130; no full candidate artifact was emitted):

| Row | Shape | baseline hit / recall | universal hit / recall | Verdict |
|---|---|---:|---:|---|
| q032 | naive | true / .200 | false / .000 | FAIL — lost document hit |

Hybrid and Graph were not run because Fast is the required first gate.

## Same-ID partial census (32 Fast rows)

| Gate/metric | baseline | universal | Result |
|---|---:|---:|---|
| Runtime errors | 0/32 | 0/32 | pass |
| Naïve hits | 5/5 | 4/5 | **FAIL** |
| Naïve mean document recall | .740 | .800 | lift, but cannot excuse lost hit |
| Direct hits | 6/7 | 7/7 | improve |
| Direct mean document recall | .581 | .800 | improve |
| Cross-domain mean document recall | .250 | .500 | improve |
| Negative controls reached | 1/1 | 1/1 | partial only; full 5/5 not reached |
| Cross-corpus cohort reached | no | no | not evaluated after early failure |
| Mean latency, same IDs | 31.697s | 37.381s | +17.9%, inside +20% |

Other positive flips included q023 naïve recall .5→1.0 and q027 direct
miss→hit/1.0. The preregistered no-hit-loss rule is absolute; q032 rejects the
candidate despite aggregate gains.

## Revert receipt

Live rollback command used the canonical compose overlays with
`QWEN3_QUERY_INSTRUCTION_PROFILE=baseline_live_v0` and forced recreation of
backend + ingest-worker; exit 0. `scripts/verify_backend_runtime.sh` then
passed with live embed dimension 1024. A live profile probe reported
`baseline_live_v0` / `qwen3-retrieval-query-v1`, exit 0.

The repository/config/compose default was also restored to
`baseline_live_v0`, both images rebuilt, focused built-image tests passed
40/40, and the containers were recreated again **without** an environment
override. Final runtime verification and final profile probe both exited 0.

Rollback requires a container recreate only. It never requires document
re-embedding, vector writes, or a durable-data migration. Because the query
cache and batch group include `instruction_version`, baseline and universal
query vectors cannot cross-hit after a flip or revert.
