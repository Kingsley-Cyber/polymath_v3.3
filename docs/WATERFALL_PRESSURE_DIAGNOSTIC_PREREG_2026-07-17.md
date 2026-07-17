# Waterfall high-pressure diagnostic — preregistration receipt

Date: 2026-07-17

Branch: `codex/waterfall-pressure-20260717`

Base: `claude-continuation-20260713 @ 7af4f16`

## Scope

This package ports the existing deterministic hydration waterfall and adds
the missing live-pressure harness. It does not activate the four-lane router,
change retrieval ranking, modify prompts or scoring, call a synthesis model,
or write corpus data. `WATERFALL_ASSEMBLY` remains default OFF.

## Frozen binding

- Query set: the immutable six-query Tier-0 bridge diagnostic.
- Bridge SHA-256:
  `6c348cbf852a26e483ee810f6d3776ce1425955acc53ec4aede880f76dedc4b8`.
- E2E selection SHA-256:
  `da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00`.
- Corpus: discover the unique active `runpod_e2e_15doc_20260715` row, then
  require its 15 active filenames to equal the immutable selection.
- Retrieval tier: `qdrant_mongo_graph`; final top K: 10.

## Pressure and fallback law

The primary ceiling is exactly 1,500 tokens per query. The runner accepts no
other primary value. A single 750-token fallback is possible only when the
saved 1,500-token artifact proves every ranked-parent decision was `full`,
with zero `summary` and zero `skip` decisions. If 1,500 exercises only one
lower tier, the gate is RED and 750 is not authorized. There is no further
post-hoc budget change.

## Acceptance

- At least one live `summary` and one live `skip` decision.
- Every ranked parent has a recorded `hydration_level`.
- Each ON packet hash is byte-stable across a second retrieval.
- OFF, ON-first, and ON-repeat evidence-selection signatures are identical.
- Every bridge result is held at its OFF-arm result; the immutable title
  scorer and forbidden-rank-one rule are reported per query.
- The final accepted wrapper exits 0.

## State

Static implementation only. Live deployment and evaluation remain blocked
behind the shared serialized order `STEP0 -> STEP1 -> R -> C -> W`.

Static receipt:

- Frozen bridge and selection hashes: exact, wrapper `EXIT=0`.
- Python compilation: `EXIT=0`.
- `git diff --check`: `EXIT=0`.
- Black check across the allocator, assembly, runner, and tests: `EXIT=0`.
- Isolated dependency-image suite: waterfall, assembly, pressure runner,
  hydration mode, rerank hydration, identity hydration, and document-artifact
  adjacency — 50 passed, wrapper `EXIT=0`.
- No backend deploy, live eval, provider call, eval-lock acquisition, or corpus
  mutation occurred.
