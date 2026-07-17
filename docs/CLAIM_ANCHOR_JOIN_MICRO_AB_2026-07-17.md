# Claim-Anchor Parent Join — Six-Query Micro A/B

Date: 2026-07-17

Branch: `codex/claim-anchor-join-20260717`

Implementation commit: `32d30b3378d677e23fc5c485883a0a434dcaef03`

Durable summary: `docs/baselines/CLAIM_ANCHOR_JOIN_MICRO_AB_2026-07-17.json`

## Outcome

**RED for promotion.** The sentence-to-parent join itself works and q021 now
attaches and renders two provenance-closed exact-sentence anchors. Both OFF and
ON arms independently completed without technical errors or corpus-store
writes. However, the preregistered cross-arm invariant failed: raw selected
source identities differed on q021, q022, and q023. No retry was run and the
gate was not redefined. The flag remains default OFF.

Labels:

- **VERIFIED:** q021's selected
  `778c…_parent_0002_summary` is exactly the durable parent summary for
  `778c…_parent_0002`; the single active parent row maps the claim-bearing
  child `778c…_0002`.
- **VERIFIED:** the ON trace saw eight valid compilation rows, three valid
  mapped rows, zero rejected rows, zero ambiguous mappings, two attached
  anchors, and two rendered anchors for q021.
- **VERIFIED:** all 18 anchors emitted across the six ON queries closed exact
  sentence spans, claim/evidence identities, source versions, compilation
  revisions, and selected-source ownership.
- **VERIFIED:** the OFF and ON raw SSE source lists and their persisted message
  previews agree. Persisted-preview compaction did not cause the cross-arm
  source drift.
- **INFERRED:** because anchor attachment runs only after final source
  selection and unit tests prove list/order/text/score preservation, the
  source drift is upstream run-to-run retrieval variation, not evidence that
  the parent join mutates retrieval. The preregistered live gate still fails;
  this inference does not convert RED to green.
- **ASSUMED:** none.

## Per-query receipt

| Query | Shape | OFF sources | ON sources | Same selected IDs | OFF anchors | ON valid / attached | ON rendered | Verdict |
|---|---|---:|---:|---|---:|---:|---:|---|
| q021 | direct / known join failure | 6 | 6 | No | 0 | 2 / 2 | 2 | Join fixed; cross-arm RED |
| q022 | procedural | 5 | 4 | No | 0 | 6 / 6 | 6 | Cross-arm RED |
| q023 | naive | 2 | 4 | No | 0 | 0 / 0 | 0 | Cross-arm RED |
| q024 | list | 3 | 3 | Yes | 0 | 2 / 2 | 2 | Green |
| q025 | single fact | 2 | 2 | Yes | 0 | 2 / 2 | 0 | Join valid; prompt render absent |
| q029 | follow-up | 5 | 5 | Yes | 0 | 6 / 6 | 6 | IDs green; non-anchor bytes drifted |

Selected-source identity passed 3/6 queries. Full selected-evidence bytes after
removing only `atomic_claim_anchors` passed 2/6; q029 kept the same identities
but its remaining serialized source bytes differed.

For q021, the anchor-bearing parent summary itself was present in both arms.
The identity failure came from another selected seat in document `a2d5…`:
child `_0014` OFF versus `_0002` ON. q022 had two OFF-only identities and one
ON-only identity; q023 had two OFF-only and four ON-only identities.

## Structural precision is not semantic quality

The 18/18 figure is **structural citation precision**, not semantic claim
quality. q021's exact sentences are genuinely about dry testing and their
offset/provenance closure is valid, but the stored compiler propositions
attached as `claim_text` are malformed/untyped outputs such as
`you POSITIVE ASSERTED UNTYPED[test]` and
`you POSITIVE POSSIBLE UNTYPED[use] dry * | *`. This branch intentionally
fixes only the join. It does not certify the claim compiler, claim relevance,
or answer quality, and it does not authorize claim-family promotion.

q025 also attached two structurally valid anchors but rendered zero after
prompt assembly. That observation is recorded without changing prompt logic,
which was outside this job.

## Commands and true exits

Canonical-container focused tests:

```text
docker exec -e PYTHONPATH=/app polymath_v33-backend-1 \
  python -m pytest -q \
  /tmp/claim_join_tests/test_atomic_claim_anchors.py \
  /tmp/claim_join_tests/test_claim_anchor_micro_ab.py
22 passed
EXIT=0
```

Each arm ran only after the Step-0 preflight:

```text
docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 \
  python /app/scripts/run_eval_with_embedder_preflight.py -- \
  python /app/scripts/run_claim_anchor_micro_ab.py \
  --expected-flag off \
  --output /tmp/claim_anchor_micro_off.json
EXIT=0

docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 \
  python /app/scripts/run_eval_with_embedder_preflight.py -- \
  python /app/scripts/run_claim_anchor_micro_ab.py \
  --expected-flag on \
  --output /tmp/claim_anchor_micro_on.json
EXIT=0
```

Both preflights returned `status=ready`, Qwen3 MLX dimension 1024,
30-second query timeout, one timeout retry, queue depth zero, and warmup
59.606 ms OFF / 59.642 ms ON. Both arms used
`anthropic/minimax-m2.7`.

The exact cross-arm comparator returned:

```text
cross_arm_passed=false
failed selected-source identity: q021, q022, q023
failed non-anchor evidence bytes: q021, q022, q023, q029
EXIT=1
```

Two preliminary runtime-introspection commands referenced nonexistent setting
attribute names and exited 1. They made no eval/model call and changed no
state. The corrected probe verified `FLAG=False/True` per arm,
`QUERY_TIMEOUT=30.0`, `LOCAL_TIMEOUT_RETRIES=1`, and live embed dimension
1024 with `EXIT=0`.

## No-write and restoration receipts

The following corpus-bearing Mongo collections were BSON-fingerprinted before
and after each arm: `documents`, `chunks`, `parent_chunks`, `summary_tree`,
`corpus_lexicon`, `ghost_b_extractions`, and
`semantic_digest_claim_compilations`. Counts and hashes were unchanged in both
arms; combined hash:
`2844e1b57f7e114eaa5c99e4bdd122a10465bd8a5e653c7a9b716d5d2ffc51af`.
Chat conversation/message rows are expected eval telemetry and are not corpus
data.

The shared runtime was restored before lock release:

```text
image=sha256:a2b8aeb5e891d0247b4e1e6c73836295cb447fb9a83740a20fce174a059b365a
config_hash=3954e01c8643171ab1c92639c57dfb14e02479718498b3eb51bb1c408a4730e2
compose_files=exact five canonical files
ATOMIC_CLAIM_ANCHORS_ENABLED=<unset>  # config default OFF
live_embed_dim=1024
restore EXIT=0
lock release EXIT=0
```

## Recommendation

Keep the join implementation on its review branch and keep the feature OFF.
The q021 ownership bug is corrected, but promotion requires a future
predeclared evaluation design that controls upstream selected-source
nondeterminism without retrying or rewriting this failed result. Separately,
claim compiler quality needs its own semantic-quality gate before these
structurally precise anchors can be treated as useful answer evidence.
