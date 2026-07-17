# Agent T — Two-Lane Anchoring Build Receipt

Date: 2026-07-17

Branch: `codex/two-lane-anchoring-20260717`

Base: `3d5344e` (`eval: freeze held-out negative v2 gate`)

Live deployment/evaluation: **NOT RUN — queued after Agent W**

## Outcome

- **VERIFIED:** `TWO_LANE_ANCHORING_ENABLED` is settings-driven and defaults
  `false` in code, `.env.example`, and Compose passthroughs for the backend and
  MCP retrieval consumer.
- **VERIFIED:** final ranked evidence candidates are deterministically classified
  as `ANCHOR` only by case-normalized, token-bounded metadata matches from title,
  author, `heading_path`, entity, or bibliographic fields. Candidate body text,
  embeddings, and LLM calls are not classification inputs.
- **VERIFIED:** every existing relationship-side seat count is fixed before
  applying the anchor/expansion quota within that side. The allocator uses
  `ceil(ANCHOR_LANE_RATIO * side_K)` anchor seats, gives the remainder to
  expansion, and spills above-threshold vacancies in both directions.
- **VERIFIED:** corpus-floor, document-anchor, graph-reserve, and sufficiency
  seats are protected. A post-allocation sufficiency check rolls back membership
  if a previously answerable packet would become unanswerable.
- **VERIFIED:** OFF bypasses allocation and trace annotation entirely. The unit
  fingerprint test proves the explicit-OFF result and diagnostics equal the
  pre-feature call byte-for-byte.
- **VERIFIED:** ON adds per-seat `side`, `lane`, exact `matched_fields`,
  `matched_terms`, score, and protection state to retrieval diagnostics. This
  supplies the preregistered T2 anchor-coverage numerator and denominator.
- **VERIFIED:** this package creates no vectors, embeds no parent summaries,
  writes no corpus/store data, and changes no prompt, scorer, or frozen spec.

## Build verification

| Gate | Command | Result | True exit |
|---|---|---:|---:|
| Compile | `PYTHONPATH=backend python3 -m py_compile ...` | selected changed Python files compiled | `0` |
| Canonical isolated suite | `docker exec -e PYTHONPATH=/tmp/agent_t_backend:/app polymath_v33-backend-1 pytest -q ...` | `125 passed` | `0` |
| Black | `uvx black --check evidence_allocation.py run_two_lane_anchoring_ab.py test_two_lane_anchoring.py` | 3 files unchanged | `0` |
| Ruff | `uvx ruff check` over all changed Python files | all checks passed | `0` |
| Compose | `docker compose config -q` | configuration valid; missing-secret warnings only because the isolated worktree has no `.env` | `0` |

Receipt log SHA-256 values:

- compile: `418a5c17f33c70e99b0cc0a07fce69191489cfedc94164bfa903785777c5bd4b`
- canonical tests: `d109b5e4538363d1123b963755304a1aa5520d9b3688caae95b0de0947c3da9d`
- Black: `8eff10171ff2ee31b9af001fffae4b117e9aed1ba9388a4e9e1edb8573ed0619`
- Ruff: `7e8e995beb6b942867c80817910cf3a80d4b9cb10974bc60ce55e6e21f00e688`
- Compose: `25f4d4051641c1a5ae52692df08f1e126871f37d4b3a1848d937a92ea827f26f`

Focused coverage includes:

- exact case-normalized title classification;
- author, heading, entity, and bibliographic classification;
- generic/partial-token rejection;
- `ceil(0.6K)` quota behavior;
- anchor-to-expansion and expansion-to-anchor spillover;
- admission thresholds and protected seats;
- relationship-side precedence and within-side quotas;
- settings default OFF;
- OFF fingerprint identity;
- repeated ON seat/trace determinism.

## Prepared live harness

`backend/scripts/run_two_lane_anchoring_ab.py` is a deployment-neutral,
single-arm harness. It:

1. acquires `/tmp/polymath-eval.lock`;
2. verifies immutable hashes for the 17-query/51-execution preregistration,
   15-document selection, and frozen 28-probe negative-v2 set;
3. aborts before scoring unless the MLX embedder batch preflight is ready;
4. drives the real `/api/chat` SSE path;
5. records per-query allocation fingerprints, eligible anchor candidate IDs,
   ON anchor coverage, and the OFF baseline over the identical ON-eligible
   pool;
6. checks direct, lay, relationship, original-negative, 28-probe,
   corpus/citation, runtime-flag-shape, and determinism gates.

The OFF arm requires one pass. The ON arm refuses to start without
`--repeat 2`, which makes T4 determinism an executable gate rather than a
manual assertion. Output paths must be new, so prior artifacts cannot be
overwritten.

## Deferred evidence

- **ASSUMED:** the live deployment will contain the same immutable frozen input
  hashes. The harness fails before scoring if this assumption is false.
- **INFERRED:** title/author/entity payload availability will vary by tier and
  corpus. The T2 live diagnostic measures the actual eligible pool instead of
  assuming metadata coverage.
- **UNVERIFIED:** frozen OFF/ON scores, 28-probe refusal parity, live anchor
  coverage, live latency, and spend. No deploy, provider call, or live eval was
  authorized for this build slot.
