# Extraction Core v1 — Engineering Freeze Manifest

```yaml
phase: "Extraction Core v1 — Engineering Freeze"
phase_status: CLOSED
created_at: 2026-08-02T15:10:00-06:00
historical_label: EXTRACTION_FREEZE_MANIFEST_20260727.md
freeze_reason: D/E adjudication complete, 100% candidate precision confirmed
freeze_classification: PATCH-FROZEN
clean_working_tree: false
next_phase: "Extraction Core v1 — Calibration and Production Qualification"
```

## Phase Closeout

**Completed in this phase:**
- Single-encoder Relex architecture
- MPS FP32 execution path
- Entity-type provenance
- Exact-offset evaluation
- Deterministic evidence joining
- Endpoint and scope gates
- Self-loop rejection
- Syntax-only and corroborated acceptance separation
- Open-relation containment
- Credit-pattern extraction
- Verb-preposition extraction
- Temporal-ready evidence records
- A–F causal ablation system
- Focused fixture validation
- D/E adjudication
- Freeze manifest and patch artifact

**Verified state:**
```
Candidate pair coverage:          17/18 = 94.4%
Candidate triple coverage:        10/18 = 55.6%
D/E candidate precision:          21/21 = 100%
Positive fixture recall:          18/18 = 100%
Negative containment:             14/14 = 100%
Extraction tests:                 451 passed
MPS/CPU decision agreement:       100%
Known expected failures:          0
```

**Not part of this phase (intentionally open):**
- Closed-world corpus annotation
- Real accepted precision and recall
- Final P3 predicate corrections
- P5 threshold and margin calibration
- Held-out multi-domain evaluation
- Production Neo4j graph writes

## Effective Freeze Boundary

The following are frozen together as one unit. Any change to any item
requires the full change-control procedure:

```
Frame types
Frame resolver mappings
Predicate mappings
Candidate construction
Entity-quality behavior
Scope and direction rules
Acceptance decision classes
Feature-profile and ablation gating
Ontology snapshot
Acceptance-policy snapshot
```

This includes the complete `p2_verb_prep` feature boundary: passive
`prep_object` frames with an `nsubjpass` signature and `copular_prep` frames
must remain gated together with their resolver mappings. Otherwise, later
ablations would no longer reproduce the frozen D/E comparison.

## Git State

```
HEAD SHA:                   86149227db703c097aad586e4a18018185e9f742
Uncommitted files:          91
Uncommitted patch:          CONTINUITY/EXTRACTION_FREEZE_UNCOMMITTED.patch
Patch sha256:               eb838080dc3887bc19af37bb18aaa5c432e736c1763c9ef34cd674f61aade3cf
```

**NOTE:** The freeze is NOT fully reproducible from HEAD alone. Apply the patch
to reproduce the tested implementation state. Prefer committing the frozen state
and replacing this manifest's SHA with that commit.

## Configuration Hashes (sha256)

```
ontology.yaml:              f484cb6acf4062fe08d2f473a80e659cb3d3239db8567d25d63efaf9d45ee956
predicate_synonyms.yaml:    5c32eb5588fe6b51ebf9f4df922d50695154fa05f19f5e3474dec467934d2a4c
relation_acceptance.yaml:   ebe3a82d13515d5017f7b3a8e86d5da313e925420fdc02b9de2b8cca6b7b3e8b
```

## Extraction Implementation Hashes (sha256)

```
frame_extractor.py:         d4655f939b29338264f4032fc5245ed2790eccaf5bce66be8a2224e643393c98
dep_path_extractor.py:      63f5bfc9abb16fe1b263e7ab65395b92bd1570d4216d59434923a1739ccb2493
credit_patterns.py:         3f3132772b23885cbdf8e41dc88bc27367b50386f26aadaddaad5a8028dd6f1d
canonical.py:               c1a57ee0af33691b1eac97a6bcea83c5fec73dc6ba82e1eb71c74d2ca6a7b96f
```

## Fixture Corpus Hashes (sha256)

```
credit_pattern_adjudicated.yaml:    db95bd853c4824a2c901c994e6a7c398d6223b0ad81387ad82ab9621bddc439a
verb_prep_adjudicated.yaml:         40ca0ef9b16697479ef326f63a4fe4be4f09cc9deb046b0dbd60feae2e34cdf6
open_relation_adjudicated.yaml:     3a152f5a8ad6e0be460bae186d18e5038848d5b2961be08c94bbb956b17f488e
```

## Gold and Adjudication Artifact Hashes (sha256)

```
spacy_relation_asserted_gold_v1.json:   8d2912e28a738109076e8c64685db569d30e4f7e9c61179b1a9bc9cb033f3127
spacy_relation_gate_v1.json:            1b92245b6b6dc90bee06888fccd674d977b4ce39ac14143b026e26350a69fb20
spacy_relation_gate_v2.json:            818b55560a94487df05174fe824a570597c87e586da0cc215cd7db153835b065
adjudicate_d_e_candidates.py:           491faa0cd677eb3854af24e4446e447b2e3e02c97ca2924d8427b658bfa45249
test_ablation_profiles.py:              6404a4d5a7e436cde4acf3b6a8eaf8e04033c20d603d19af1dfe11aa59d1913f
```

## GLiNER-Relex Model

```
Repository:                 local_ghost_b/models/gliner_onnx_medium_v2.1/
GLiNER package version:     0.2.28
model.onnx sha256:          dc077a7f2a6a86c29cf6fcbae783e6a1fceb82e50bbffbcab4e39eaba6f487ad
model_fp16.onnx sha256:     97089adc9af3c2d697a01d13b1bbc97f0105deebce9d54ff0263e383f9e59998
```

## Runtime Environment

```
Python:                     3.11.15
spaCy:                      3.8.14
spaCy model:                en_core_web_sm v3.8.0
PyTorch:                    2.13.0
MPS available:              True
MPS built:                  True
pip-freeze sha256:          006aff943ae4c480ef35b1e851dc46a682b7cddf9784a8357c408ab32a87a941
```

## Adjudication Results

```json
{
  "introduced_candidates": 21,
  "correct_introduced_candidates": 21,
  "candidate_precision": 1.0,

  "positive_fixtures": 18,
  "positive_fixtures_recovered": 18,
  "positive_fixture_recall": 1.0,

  "negative_fixtures": 14,
  "negative_fixtures_contained": 14,
  "negative_containment_rate": 1.0,

  "fixture_decision_accuracy": 1.0,

  "known_coverage_gaps": [
    "produced_by not in credit markers (KNOWN_COVERAGE_GAP)"
  ]
}
```

## Test Result Manifest

```
Extraction suite:           451 passed, 0 xfailed, 0 failed
  - Open-relation fixtures: 232 tests (4/4 valid STORE, 8/8 invalid contained)
  - Credit-pattern fixtures: 56 tests (all positives recovered, 0 prose leaks)
  - Verb-prep fixtures:     181 tests (47 positive, 20 negative, all pass)
  - Ablation profiles:      13 tests (D/E causal isolation verified)
```

## Feature Gate Summary

```
p2_verb_prep gating covers:
  1. Structural frame generation
     - copular_prep frames (section 2b)
     - passive prep_object with nsubjpass signature (section 4b)
  2. Predicate resolution
     - all T1/T2 rules tagged feature_group=p2_verb_prep
     - works_for (work+for, obj_type=Organization)
     - member_of (serve+in, member+of)
     - part_of (part+of)

Credit pattern gating:
  - document-structure qualification (_is_metadata_context)
  - prose subject+verb rejection
  - quotation guard
  - verb continuation rejection
```

## Known Coverage Gaps (not precision issues)

```
- "produced by" not in credit markers (KNOWN_COVERAGE_GAP)
- located_in via "based in" resolves through T3 synonym, not T2 rule
- _ARTIFACT_SURFACES blocks valid part_of subjects that are document nouns
```

## Change-Control Rule

From this point, no new extractor feature, frame, mapping, ontology predicate,
or acceptance behavior may enter the frozen core without:

1. A documented failure case
2. A positive fixture
3. Adversarial negative fixtures
4. An ablation delta
5. Candidate adjudication
6. A benchmark impact report

This prevents the closed-world benchmark from becoming a moving target.

## GIT-FROZEN Upgrade Path

To upgrade from PATCH-FROZEN to GIT-FROZEN:

```bash
git add backend/services/extraction \
        backend/scripts \
        backend/tests/extraction \
        config \
        data/closed_world_v1 \
        CONTINUITY

git commit -m "freeze: extraction core v1 before closed-world calibration"

git tag -a extraction-core-v1.0.0 \
  -m "Deterministic extraction architecture freeze before P3/P5"
```

Then update this manifest with:
```yaml
freeze_commit_sha: <new_sha>
tag: extraction-core-v1.0.0
clean_working_tree: true
freeze_classification: GIT-FROZEN
```

**Do not mix closed-world calibration changes into that commit.**

## Final Status

Formal release state (categorical — the only normative form):

```yaml
engineering_freeze: passed
recoverability: passed
git_reproducibility: pending
closed_world_annotation: pending
calibration: pending
held_out_qualification: pending
graph_write_promotion: pending
```

```text
Extraction engineering:          CLOSED
Architecture hardening:          CLOSED
Feature-level validation:        CLOSED
Implementation freeze:           PATCH-FROZEN
Production qualification:        NOT YET CLOSED
Canonical graph writes:          DISABLED
```

Note: percentage-style readiness estimates (e.g., "99% engineering",
"88–90% graph-write readiness") are informal planning figures only and are
NOT normative release metrics.
