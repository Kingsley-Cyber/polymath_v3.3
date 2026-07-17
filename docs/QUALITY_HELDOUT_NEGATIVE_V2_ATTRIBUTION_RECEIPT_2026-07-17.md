# Held-out Negative v2 Attribution Receipt — 2026-07-17

Status: **measurement complete; generalization gap confirmed**

## Bound inputs and runtime

- Frozen held-out-v2 SHA-256:
  `3b35c14c165f6be89202b809ea01a1cd6ad0f5c0217e4167b86e4b5dc0b09960`
- Prior partial RED-log SHA-256:
  `6345380ccb36331776515348c1e62adbee902a069910c1741516cdb5f2043832`
- Frozen selection SHA-256:
  `da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00`
- Relationship evidence allocation: ON
- Corpus-scope v2 answerability: ON
- Temporal routing: OFF
- Four-lane router: OFF
- Tier: `qdrant_mongo_graph`
- Model route: `anthropic/minimax-m2.7`

The runner excluded the nine probes already refused in the first partial
pass, then measured the three previously answered plus the 16 unexecuted
probes. It rejected input drift unless the split was exactly 9 passed,
3 answered, and 19 pending.

## Result

- Executions: 19/19
- Technical success: 19/19
- Refused: 10
- Answered: 9
- Wrapper: `EXIT=0`
- Two-attempt envelope: `$0.9719982` (senior cap: `$1.10`)
- No tuning, threshold, prompt, scorer, or corpus change occurred.

| Probe | Family | Outcome | Guard coverage |
|---|---|---:|---:|
| `negv2_f2_oscar_2026` | F2 | ANSWER | 1.0000 |
| `negv2_f3_deakins` | F3 | ANSWER | 0.7500 |
| `negv2_f3_visual_story` | F3 | ANSWER | 0.7143 |
| `negv2_f4_mark_facebook_ads` | F4 | REFUSE | 0.2500 |
| `negv2_f4_mark_quiz_funnels` | F4 | REFUSE | 0.3750 |
| `negv2_f4_mark_marketing_advice` | F4 | ANSWER | 0.4000 |
| `negv2_f4_mark_vsl` | F4 | ANSWER | 0.7500 |
| `negv2_f5_roi_table` | F5 | REFUSE | 0.0000 |
| `negv2_f5_figure_9_4` | F5 | ANSWER | 0.6667 |
| `negv2_f5_vfx_interview` | F5 | REFUSE | 0.5000 |
| `negv2_f5_combat_checklist` | F5 | REFUSE | 0.5000 |
| `negv2_f6_llc_guess` | F6 | REFUSE | 0.0000 |
| `negv2_f6_resolve` | F6 | REFUSE | 1.0000 |
| `negv2_f6_export` | F6 | ANSWER | 0.8000 |
| `negv2_f6_kubrick_camera` | F6 | ANSWER | 1.0000 |
| `negv2_lay_deakins` | F3 paraphrase | ANSWER | 1.0000 |
| `negv2_lay_shopify` | F3 paraphrase | REFUSE | 0.0000 |
| `negv2_lay_roi` | F5 paraphrase | REFUSE | 0.0000 |
| `negv2_lay_llc` | F6 paraphrase | REFUSE | 0.0000 |

The three failures first observed with temporal ON all reproduce with
temporal OFF. Therefore temporal routing was not their shared cause. The
sealed journal records each probe's matched and missing terms, coverage,
raw-answerable state, guard decision, source identities, and answer hash.

## Sealed journal

`docs/baselines/QUALITY_HELDOUT_NEGATIVE_V2_ATTRIBUTION_2026-07-17.json`

SHA-256:
`0ca922a89069d193adca3ae3eab2c9c4db5625c61cc77e4e6f1290998fb7c593`

## Concurrent R observation

R had already entered its admitted live window when the 18:34Z ceiling
ruling arrived. Its six fixed bridge probes completed technically, but only
4/6 passed; the forbidden cinematography/lens document ranked first on the
camera-motion probe. The T9.1 materializer inserted 15 additive profiles,
then proved idempotency with `0 inserted / 15 reused`. R was restored OFF.

R artifact:
`docs/baselines/QUALITY_ROUTER_R_T91_BRIDGE_ON_2026-07-17.json`
(`7dd1a2c504ea2fd9cbe58abf926d9face37d5154e5bda0557ed91f053495908e`).
Its two-attempt envelope was `$0.3069468`.

Conservative cumulative wave exposure after R and attribution is
`$9.1572462`. The later 18:57Z OWNER ruling retires this ceiling as a
dispatch gate while retaining usage accounting.
