# STEP1 Temporal Combined Verification Receipt — 2026-07-17

Status: **RED — temporal disabled**

## Runtime under test

- Corpus: `2c894530-8d57-4432-a6d4-bc14505a698b`
- Model route: `anthropic/minimax-m2.7`
- Relationship evidence allocation: ON
- Corpus-scope v2 answerability: ON
- Temporal query routing: ON during verification; OFF immediately after the RED held-out gate
- Frozen selection SHA-256: `da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00`
- Frozen preregistration SHA-256: `8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110`
- Held-out negative-v2 SHA-256: `3b35c14c165f6be89202b809ea01a1cd6ad0f5c0217e4167b86e4b5dc0b09960`

## Results

| Gate | Result | Target | Verdict |
|---|---:|---:|---|
| Frozen direct doc-hit | 94.44% | ≥85% | GREEN |
| Frozen lay-language doc-hit | 100% | ≥75% | GREEN |
| Frozen relationship minimum-distinct | 100% | ≥75% | GREEN |
| Frozen negative refusals | 9/9 | 9/9 | GREEN |
| Frozen corpus boundary | 100% | 100% | GREEN |
| Frozen citation membership | 100% | 100% | GREEN |
| Temporal doc-hit | 100% | ≥90% | GREEN |
| Temporal full-anchor coverage | 75% | ≥70% | GREEN |
| Held-out negative v2 | 9/12 completed refused; 3 answered | 100% | **RED** |

The held-out run was stopped after the third answered probe because the 100%
gate was irrecoverably failed. The answered probes observed before
termination were `negv2_f2_oscar_2026`, `negv2_f3_deakins`, and
`negv2_f3_visual_story`. The wrapper exited `143` after the deliberate
termination; this is not presented as a completed 28-probe pass.

## Temporal tier split

| Tier | Doc-hit | Full-anchor |
|---|---:|---:|
| Qdrant only | 100% | 62.5% |
| Qdrant + Mongo | 100% | 87.5% |
| Qdrant + Mongo + graph | 100% | 75% |

## Cost and durability

- STEP1 frozen envelopes: `$1.790523 + $0.8185248`
- STEP1 temporal envelope: `$1.2277872`
- Held-out negative-v2 envelope: `$1.4324184` (conservatively counted in full)
- Cumulative wave envelope including STEP0: `$7.8783012 / $10.00`
- Combined frozen/temporal journal:
  `docs/baselines/QUALITY_STEP1_TEMPORAL_COMBINED_2026-07-17.json`
  (`112c090b964fb0a1bc379ebe1b004831581810d07a0d97b9c1b9d8474f58dff9`)
- Held-out RED log:
  `docs/baselines/QUALITY_STEP1_HELDOUT_NEGATIVE_V2_RED_2026-07-17.log`
  (`6345380ccb36331776515348c1e62adbee902a069910c1741516cdb5f2043832`)

## Fail-safe receipt

The canonical five-overlay stack was recreated with
`TEMPORAL_QUERY_ROUTING_ENABLED=false`. After health returned, the live
backend environment verified:

```text
ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED=true
RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED=true
TEMPORAL_QUERY_ROUTING_ENABLED=false
EXIT=0
```

No corpus data was written.
