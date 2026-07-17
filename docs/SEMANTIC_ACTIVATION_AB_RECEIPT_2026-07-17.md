# Semantic activation A/B receipt — 2026-07-17

## Decision

**REJECT activation for both families. Keep both flags default OFF.**

- **VERIFIED — Semantic digests:** 249 new provenance-closed Tier-0 points
  were projected idempotently, but the first valid exposed Fast arm regressed
  Mark doc-hit from `0.889` to `0.778` and direct hits from `2/2` to `1/2`.
  Digest Graph and Frozen ON arms were stopped and never launched.
- **VERIFIED — Atomic claims:** the preregistered q021 Graph probe found four
  valid current compilation rows but attached and rendered zero anchors.
  The full Mark and Frozen ON arms were stopped and never launched.
- **VERIFIED — runtime:** the shared backend was restored to canonical commit
  `3157ec9`, five overlays, image `sha256:1dd3702c…697d5`, healthy. Neither
  activation flag nor activation module exists in that deployed image.
- **VERIFIED — durable state:** the 249 new digest points, one manifest, and
  249 applied outbox rows remain dark. No existing Qdrant point changed.

This is an experiment receipt, not a production promotion.

## Scope and invariants

- New digest points use deterministic IDs, the corpus-frozen embedding
  profile, `corpus_id`, source provenance, a strict projection manifest, and
  an idempotent outbox/application receipt.
- Tier-0 excludes `chunk_type=semantic_digest` when the digest flag is OFF.
- Claim anchors attach only after final chunk selection, are additive to
  chunk evidence, and never alter retrieval scores, source ordering, or
  answerability counts.
- The claims path uses one bounded Mongo aggregation, validates current
  document/chunk ownership and source-version/hash closure, and accepts only
  exact sentence spans with positive query-term overlap.
- No corpus, graph, chunk, document, summary, claim, or existing vector point
  was mutated by the A/B runs.

## Projection receipt

| Check | Result |
|---|---:|
| Eligible current digests | 249 |
| Source documents represented | 38 |
| Terminal source exclusion | 1 faithfulness-rejected row |
| New Qdrant points applied | 249 |
| Projection manifests | 1 |
| Outbox rows | 249 applied |
| Idempotent second enqueue/drain | 249 remained applied; 0 reclaimed |
| Exact authorization probe after BSON fix | 96 seen / 96 authorized |
| Contract rejection counters | all 0 |

The first worker claim exposed a real lease-attempt binding defect. Recovery
used the natural lease reclaim path; every final row was applied on attempt
2. A second enqueue and drain were no-ops.

Mongo's shared Motor client also returned BSON UTC datetimes without
`tzinfo`. Strict manifest/outbox and compilation contracts correctly rejected
them. The implementation now uses one narrow Mongo read-boundary helper that
restores UTC awareness immediately before strict parsing; it does not
reinterpret arbitrary timestamps.

### Existing-point immutability

| Measurement | Before | After |
|---|---:|---:|
| Non-target point count | 692 | 692 |
| New-ID intersection | 0 | 249 |
| Non-target fingerprint | `sha256:8bb06198…daf3` | `sha256:8bb06198…daf3` |

**VERIFIED:** non-target fingerprint equality proves existing Qdrant points
were unchanged.

## Digest A/B

The OFF arm used the frozen MiniMax/OpenCode contract:

- model: `anthropic/minimax-m2.7`
- provider: `opencode-go-anthropic`
- base: `https://opencode.ai/zen/go`
- safe pool hash: `91bf6ceb…edadfe`
- three identical 1024-dimensional Qwen3 embedding probes passed before each
  exposed arm.

An early digest-ON run is excluded from quality interpretation: a Motor
timezone mismatch caused all 96 fetched digest candidates to fail application
receipt validation, so it had no digest exposure. The valid arm began only
after a connected probe showed `96/96` authorized and every rejection counter
at zero.

### Valid Fast comparison

| Metric | OFF | Digest ON | Delta |
|---|---:|---:|---:|
| Doc-hit | 0.889 | 0.778 | -0.111 |
| Mean doc recall | 0.796 | 0.648 | -0.148 |
| Mean concept recall | 0.630 | 0.648 | +0.018 |
| Answerability correctness | 0.889 | 0.889 | 0 |
| Direct hits | 2/2 | 1/2 | -1 |
| Lay/naive hits | 1/1 | 1/1 | 0 |
| Mean latency | 29.059 s | 16.571 s | -12.488 s |

| Query | OFF hit / recall | Digest ON hit / recall | Outcome |
|---|---:|---:|---|
| q021 | true / 1.000 | true / 1.000 | parity |
| q022 | true / 1.000 | true / 1.000 | parity |
| q023 | true / 1.000 | true / 1.000 | parity |
| q024 | true / 0.500 | true / 0.500 | parity |
| q025 | true / 1.000 | true / 1.000 | parity |
| q026 | false / 0.000 | false / 0.000 | parity |
| q027 | true / 1.000 | **false / 0.000** | regression |
| q028 | true / 1.000 | true / 1.000 | parity |
| q029 | true / 0.667 | true / 0.333 | recall regression |

**Gate verdict: RED.** The required no-regression and direct/lay improvement
criteria were not met. A partial Hybrid run was stopped after q026; Graph and
Frozen ON were not run. No quality retry or gate weakening followed.

The completed shared OFF Frozen baseline remains:

| Frozen metric | OFF |
|---|---:|
| Technical success | 1.000 |
| Direct doc-hit | 1.000 |
| Lay-language doc-hit | 1.000 |
| Relationship minimum-distinct | 0.750 |
| Negative refusal | 0.556 (existing unrelated RED) |

## Atomic-claim gate

The first q021 probe found four rows but rejected all four because naive BSON
datetimes could not enter strict canonical hashing. After the shared UTC
read-boundary fix:

| q021 live Graph check | Result |
|---|---:|
| Selected source keys | 4 |
| Compilation rows seen | 4 |
| Rows valid / rejected | 4 / 0 |
| Anchors attached | 0 |
| Sources anchored | 0 |
| Anchors rendered after prompt compaction | 0 |
| Probe exit | 1 |

**Gate verdict: RED / unverified.** q021 did not satisfy the existing
query-overlap eligibility guard. The guard was not weakened and a friendlier
query was not substituted. Full Mark/Frozen claims A/B did not run, so no
answer-quality or semantic citation-relevance improvement is claimed.

### Deterministic diagnosis only

A no-API, no-model, read-only replay used the completed OFF Graph source
packets for q021–q029:

| Diagnostic | Result |
|---|---:|
| Questions replayed | 9 |
| Questions with eligible anchors | 6 |
| Positive IDs | q022, q024, q025, q027, q028, q029 |
| Eligible anchors | 27 |
| Exact ownership/span/provenance valid | 27/27 |
| Structural sentence-anchor precision | 1.000 |
| q021 isolated zero | true |

This establishes that q021 was isolated relative to six other questions, but
it cannot promote the family because the preregistered live gate failed.
“Structural sentence-anchor precision” means exact source ownership, exact
span equality, and provenance closure; it is not semantic relevance gold.

## Verification and restoration

- Activation-focused canonical container suite: **90 passed**, `EXIT=0`.
- Digest authorization regression: **14 passed**, `EXIT=0`.
- Claims/Tier-0 UTC regression subset: **25 passed**, `EXIT=0`.
- Final backend: canonical `3157ec9`, image `sha256:1dd3702c…697d5`,
  healthy, exact five-overlay deployment.
- Final runtime inspection: activation flags absent; activation modules
  absent; host and container eval locks absent.

## Durable artifacts

- `docs/baselines/SEMANTIC_ACTIVATION_DIGEST_PROJECTION_RECEIPT_2026-07-17.json`
- `docs/baselines/SEMANTIC_ACTIVATION_QDRANT_FINGERPRINT_2026-07-17.json`
- `docs/baselines/EVAL_2026-07-17_qdrant_only_semantic_activation_off.json`
- `docs/baselines/EVAL_2026-07-17_qdrant_only_semantic_digest_on_authorized_rerun.json`
- `docs/baselines/EVAL_2026-07-17_frozen51_semantic_activation_off.json`
- `docs/baselines/SEMANTIC_ACTIVATION_CLAIMS_OFFLINE_REPLAY_2026-07-17.json`

## Review recommendation

Keep the implementation on the review branch for inspection, with both flags
default OFF. Do not activate digest routing without a new, preregistered
ranking design that removes the q027/q029 regressions. Do not activate claim
anchors until a live gate chosen independently of these results passes and a
full Frozen A/B demonstrates no regression plus a defined citation-quality
gain.
