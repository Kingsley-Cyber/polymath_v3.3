# Metal GPU Priority Arbiter — Build Receipt

Date: 2026-07-17

Branch: `codex/gpu-priority-arbiter-20260717`

Base: `7af4f16`

Scope: build and mocked/local validation only; deployment remains gated.

## Outcome

VERIFIED: one host-local lease service now coordinates the Apple embedder and
reranker sidecars when `ARBITER_ENABLED=true`. The committed default is
`false`, which performs no arbiter HTTP call and preserves the direct path.

VERIFIED:

- EMBED is the high-priority class.
- RERANK is low priority and releases at every pre-existing model batch/block
  checkpoint.
- The starvation guard admits a waiting rerank after at most one embed lease
  by default, or after 500 ms of wait.
- Both clients fail open to the unchanged direct computation and emit the
  exact named alert `gpu_arbiter_unavailable` when acquisition/release cannot
  reach the arbiter.
- The Jina torch path wraps its existing `_compute_single_batch` calls. It
  does not split/reorder documents or alter truncation, model, precision,
  calibration, score assembly, or backend.
- Lease health exposes queue/grant counts, wait/hold p95, over-target holds,
  and stale-lease recovery. The rerank hold target is 500 ms.
- The existing `RERANK_EVIDENCE_SUPPORT` default-OFF law is unchanged.
- The runtime installer stages source, renders the LaunchAgent
  deterministically, validates it with `plutil`, atomically installs it,
  compares it byte-for-byte, and then uses the existing bootstrap/kickstart
  path. `scripts/check_apple_mlx_plist_drift.sh` is the shared read-only drift
  check.

## Validation receipts

### Focused canonical-image tests

Command:

```bash
docker run --rm \
  -e LITELLM_MASTER_KEY=test \
  -e AUTH_SECRET_KEY=test \
  -e DEFAULT_ADMIN_PASSWORD=test \
  -e SIDECAR_PATH=/repo/scripts/apple_ml_services \
  -v /Users/king/polymath-wt/gpu-arbiter:/repo \
  -w /repo/backend \
  --entrypoint python polymath_v33-backend \
  -m pytest -q \
  tests/test_gpu_priority_arbiter.py \
  tests/test_embedder_priority_gate.py \
  tests/test_embedder_warmup.py \
  tests/test_answerability_gate_loosening.py::test_rerank_evidence_support_defaults_off_and_flips_on
```

Actual output tail:

```text
......................                                                   [100%]
22 passed, 26 warnings in 3.44s
EXIT=0
```

### Mocked mixed-load and identity metrics

Command:

```bash
docker run --rm \
  -e LITELLM_MASTER_KEY=test \
  -e AUTH_SECRET_KEY=test \
  -e DEFAULT_ADMIN_PASSWORD=test \
  -e SIDECAR_PATH=/repo/scripts/apple_ml_services \
  -v /Users/king/polymath-wt/gpu-arbiter:/repo \
  -w /repo/backend \
  --entrypoint python polymath_v33-backend \
  -m pytest -q -s tests/test_gpu_priority_arbiter.py
```

Actual output:

```text
MOCKED_MIXED_SOAK embeds=100 embed_p95_s=0.236637 reranks=25 rerank_compute_p95_s=0.002231
MOCKED_EMBED_IDENTITY vectors=100 dimensions=1024 max_abs_diff=0.0
MOCKED_RERANK_IDENTITY scores=4 checkpoints=2 max_abs_diff=0.0
11 passed, 16 warnings in 3.84s
EXIT=0
```

VERIFIED: the local scheduling soak completed 100/100 mocked embeds with no
timeout or deadlock. The 100-vector × 1024-dimension sample was byte-exact
between OFF and ON scheduling wrappers. Mocked rerank scores were exact and
the two pre-existing block calls each acquired a separate lease.

### Static/build gates

Commands:

```bash
python3 -m py_compile \
  scripts/apple_ml_services/gpu_arbiter/client.py \
  scripts/apple_ml_services/gpu_arbiter/main.py \
  scripts/apple_ml_services/embedder_mlx/main.py \
  scripts/apple_ml_services/reranker_mlx/main.py \
  scripts/render_apple_mlx_launch_agent.py \
  backend/tests/test_gpu_priority_arbiter.py

bash -n \
  scripts/apple_ml_services/start.sh \
  scripts/install_apple_mlx_runtime.sh \
  scripts/check_apple_mlx_plist_drift.sh

docker run --rm \
  -v /Users/king/polymath-wt/gpu-arbiter:/repo \
  -w /repo/backend \
  --entrypoint python polymath_v33-backend \
  -m black --check \
  tests/test_gpu_priority_arbiter.py \
  ../scripts/apple_ml_services/gpu_arbiter/client.py \
  ../scripts/apple_ml_services/gpu_arbiter/main.py \
  ../scripts/apple_ml_services/embedder_mlx/main.py \
  ../scripts/apple_ml_services/reranker_mlx/main.py \
  ../scripts/render_apple_mlx_launch_agent.py
```

Actual output tails:

```text
EXIT=0
EXIT=0
All done! ✨ 🍰 ✨
6 files would be left unchanged.
EXIT=0
```

### Isolated plist-drift fixture

Command: render a default-OFF plist beneath a fresh `/tmp` HOME, then run
`HOME=<fixture> POLYMATH_DOCKER_DATA_ROOT=<fixture>/runtime
bash scripts/check_apple_mlx_plist_drift.sh`.

Actual output:

```text
[apple-mlx] plist drift check: clean (.../Library/LaunchAgents/com.polymath.apple-ml.plist)
EXIT=0
```

VERIFIED: this fixture touched only a newly created `/tmp` tree.

## Preregistered live gates

| Gate | Build-stage status |
|---|---|
| Q1 bit identity | Mocked 100-vector and rerank-score wrappers green; real-model ≥100-vector and score samples remain deploy-gated. |
| Q2 contention | Mocked 100/100 green; real continuous-rerank load and embed p95 `<2.0s` remain deploy-gated. |
| Q3 starvation | Unit guard and mocked compute completion green; real rerank p95 ratio remains deploy-gated. |
| Q4 robustness | Named fail-open, stale recovery, and deadlock-free mocked soak green; 10-minute soak and mid-soak arbiter kill remain deploy-gated. |
| Q5 canonical | Focused build suites green; frozen live spot-check remains deploy-gated. |

## Deploy-gate note

INFERRED: the only zero-computation-change checkpoint exposed by the deployed
Jina model is its existing `_compute_single_batch` boundary. The arbiter
records every hold over the 500 ms target, but it deliberately does not expire
an active lease at 500 ms: doing so would permit concurrent Metal work while
the reranker kernel is still active. It also does not split a model block,
because that would change Jina's query-embedding/block-weight computation and
violate Q1. Therefore Q2/Q3 must empirically prove the existing block duration
is sufficient. A miss is a RED deploy verdict, not permission to change math.

VERIFIED: no command in this build invoked the Apple ML installer, `launchctl`,
the live sidecar ports, or wrote beneath `~/PolymathRuntime`. The eval lock was
not needed because no live eval/deploy occurred.
