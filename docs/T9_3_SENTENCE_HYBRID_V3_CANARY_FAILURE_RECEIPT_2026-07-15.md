# T9.3 sentence-hybrid v3 canary failure receipt — 2026-07-15

Status: **RED under the preregistered acceptance gate.** Phase 2 is sealed and
parked. This is an external provider-structure limit, not authorization to
weaken validation, retry, change retrieval logic, or start another paid lane.

## Frozen execution boundary

- Authorization: `COORDINATION.md:2026-07-15T03:39:13Z:v3-CANARY-GO`
- Sealed runner commit: `63c6f3d7268582914a277c357264bdb6b143270e`
- Packet set: `sha256:89ace7ede4eab1d00f7f8d062b92d756cc5f7243fe4d0c3d0c7e0fec131b2d43`
- Packet schema: `sha256:5c600d3047807541a09be38d01933b6e048f5a3f730de1b5e2cf6c48991f2e40`
- Selection: `sha256:6aed7b1a967c1ad8889a0f058091e7f47691053d25185ff03cac797b3875f595`
- Prompt / repair / digest schema:
  `sha256:ee523bbf674d26a3974488e48fdfae6f0f4a4238e1df94ce39067dc9d35c10eb` /
  `sha256:0d4d7d5f50c0a98312cf4052510aa4225d1cc235b319df84c5eacf1c5801d145` /
  `sha256:ce106660a46ff7799e79399816dd634645e1b906f80905db3460f70787f97c99`
- Provider: LongCat Tier 3, `openai/LongCat-2.0`, maximum 8,192 output
  tokens, temperature 0, thinking disabled, serial execution.
- Hard selected-ten authority: `$0.78260930`; acceptance: at least 9/10 plus
  strict faithfulness of every accepted digest.

## Terminal execution receipt

| Check | Result |
|---|---|
| Terminal closure | 10/10 |
| Accepted | **2/10 — FAIL** |
| Structural dead letters | 8/10 |
| Provider calls | 19 |
| ReadTimeouts | 0 |
| Known actual / ceiling basis | `$0.55765220` |
| Selected-ten authority | `$0.78260930` — held |
| Budget accounting | Complete; 0 unpriced exposure |
| Cumulative umbrella before / after | `$2.19883750` / `$2.75648970` |
| Owner cumulative umbrella | `$49.45` — held |
| Protected canonical census | Exactly unchanged |
| Ambient census | Exactly unchanged |
| Canonical writes | 0 |
| Runner process | expected red `EXIT=1` |

The exact execution receipt is
`/tmp/t9_3_v3_canary_execution.json`, SHA-256
`5bdf6f74bb016dbdaf8be05c365912a75beebdf59c407ae2b8afb52fbd9fe70d`;
the true-exit command log is `/tmp/t9_3_v3_canary_execution.log`.

## Structural failure diagnosis

All eight DLQ jobs used both permitted attempts. Of the 16 failed attempt
bodies:

- 14/16 are zero-byte `empty_tool_arguments`;
- 2/16 are structurally valid, nonempty JSON that fail semantic citation
  validation;
- 6/8 jobs are empty on both attempts;
- ordinal 479 is empty then nonempty JSON with 4 unknown claim-ID references;
- ordinal 576 is nonempty JSON with 75 unknown claim-ID references then empty;
- no transport failure, unpriced exposure, refusal prose, malformed JSON
  prefix, or canonical mutation was observed.

The count-only/no-raw-output classifier is true `EXIT=0` at
`/tmp/t9_3_v3_dlq_shape_classification.log`. The context-reconstructed
postflight is true `EXIT=0` at `/tmp/t9_3_v3_postflight_extract_v3.json`,
SHA-256
`598ecbb7ae1e18ab82e57d0a9032fc1fb43de59ee08b0cc053821bd0721ae12b`.
It reconstructed packets from the ten durable parent identities and frozen
source revisions; rerunning the fresh selector after purchase would correctly
advance to a new population and is not historical replay.

## Accepted-output faithfulness

Strict verdict: **2/2 PASS**.

| Ordinal | Evidence closure | Summary/thesis verdict | Qualification handling |
|---|---|---|---|
| 218 | 28 citable + 7 context-only units; 40 proposal references / 18 unique; 0 unknown or context-only citations | PASS — six-step VSSL process, Operator/ChatGPT research, brief/avatar preparation, and preparation-dependent writing simplicity are directly supported | Conditions and free-resource uncertainty are surfaced |
| 287 | 49 citable + 7 context-only units; 61 proposal references / 26 unique; 0 unknown or context-only citations | PASS — safety-net removal, age-19 near-eviction, necessity over motivation, consequence pressure, high repetitions, and analysis paralysis are directly supported | Pressure-personality condition/exception is retained; advice is not presented as unqualified |

No unsupported synthesis was found in either accepted digest. All emitted
proposal, condition, and exception citations resolve only to citable sentence
units. The eight DLQ rows have no accepted digest to review, so corpus-canary
faithfulness cannot substitute for the failed 9/10 structural gate.

## Cross-shape owner decision table

| Provider input shape | Preregistered comparable acceptance | Dominant failure evidence |
|---|---:|---|
| Interim prose | **10/10** | none in the canary |
| Claims-only structured | **3/8 within authority** | zero-byte tool arguments dominated |
| Ordered-unit structured v3 | **2/10** | 14/16 failed bodies empty; 2 semantically invalid citation sets |

VERIFIED inference: accepted ordered-unit outputs are evidence-faithful, but
LongCat's Tier-3 tool path is not production-reliable for either structured
packet family. Prose is the only empirically reliable input shape in these
canaries. This evidence does not itself authorize a new prose contract or
another provider spend.

The green-only three-digest owner sample was not produced: the canary is red
and only two accepted digests exist. Phase 2 remains blocked pending the
senior/owner decision.
