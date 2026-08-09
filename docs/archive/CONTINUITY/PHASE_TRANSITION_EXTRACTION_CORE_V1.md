# Phase Transition: Extraction Core v1

## Transition Record

```yaml
from_phase: "Extraction Core v1 — Engineering Freeze"
to_phase: "Extraction Core v1 — Calibration and Production Qualification"
transition_date: 2026-08-02
freeze_classification: PATCH-FROZEN
freeze_manifest: CONTINUITY/EXTRACTION_FREEZE_MANIFEST_20260802.md
patch_artifact: CONTINUITY/EXTRACTION_FREEZE_UNCOMMITTED.patch
```

## Phase 1 Closeout Summary

The engineering phase is **CLOSED**. All architectural work, deterministic
hardening, and feature-level validation are complete.

### Deliverables

| Component | Status |
|-----------|--------|
| Single-encoder Relex architecture | ✓ |
| MPS FP32 execution path | ✓ |
| Entity-type provenance | ✓ |
| Exact-offset evaluation | ✓ |
| Deterministic evidence joining | ✓ |
| Endpoint and scope gates | ✓ |
| Self-loop rejection | ✓ |
| Syntax-only / corroborated acceptance separation | ✓ |
| Open-relation containment | ✓ |
| Credit-pattern extraction | ✓ |
| Verb-preposition extraction | ✓ |
| Temporal-ready evidence records | ✓ |
| A–F causal ablation system | ✓ |
| Focused fixture validation | ✓ |
| D/E adjudication | ✓ |
| Freeze manifest and patch artifact | ✓ |

### Verified Metrics

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

## Phase 2: Calibration and Production Qualification

### Entry Condition

Begin by filling the closed-world annotation files:

```
data/closed_world_v1/source_chunks.jsonl
data/closed_world_v1/mentions.jsonl
data/closed_world_v1/pair_annotations.jsonl
data/closed_world_v1/relation_instances.jsonl
data/closed_world_v1/open_relations.jsonl
```

Then freeze the document-level 60/20/20 splits **before** using any labels
for threshold tuning.

### Phase 2 Scope

1. **Closed-world corpus annotation**
   - Exhaustive entity mention annotation
   - Exhaustive pair annotation (RELATION / NO_RELATION / OPEN_RELATION)
   - All 13 negative classes represented
   - Dual annotation for 20–25% with adjudication

2. **P3 predicate corrections**
   - Run corrected confusion analysis on closed-world data
   - Apply only evidence-supported corrections
   - No new features without change-control approval

3. **P5 threshold and margin calibration**
   - Calibrate acceptance thresholds per gate
   - Optimize corroboration margins
   - Use development partition only

4. **Held-out evaluation**
   - Single evaluation on held-out partition
   - Report all metrics from manifest targets
   - Determine promotion gate pass/fail

5. **Production qualification**
   - Enable Neo4j graph writes only if gates pass
   - Monitor latency, memory, candidate explosion
   - Establish operational runbook

### Controlled Sequence (normative order)

```
1.  Populate source_chunks.jsonl
2.  Annotate mentions.jsonl
3.  Annotate pair_annotations.jsonl
4.  Annotate relation_instances.jsonl
5.  Annotate open_relations.jsonl
6.  Complete adjudication records
7.  Freeze document-grouped 60/20/20 splits
8.  Hash the corpus and split assignments
9.  Run P3 confusion analysis on development labels
10. Apply only change-control-approved corrections
11. Calibrate P5 thresholds on development data
12. Evaluate exactly once on the held-out partition
13. Decide graph-write promotion
```

The document grouping (step 7) must occur before threshold tuning (step 11)
so that related chunks from the same source cannot cross development and
held-out boundaries.

### Promotion Gates

Before canonical graph writes:

```
overall_accepted_canonical_precision >= 0.90
syntax_high_precision >= 0.95
no_known_invalid_relation_accepted = true
stable_per_predicate_results = true
cross_sentence_pairs_non_production = true
exact_evidence_offsets_retained = true
acceptable_latency_and_memory = true
```

### Lane-Specific Promotion Reporting

Promotion must NOT rely on one aggregate precision value. Qualification
reports each output lane separately:

```
ACCEPT_CORROBORATED precision/recall
ACCEPT_SYNTAX_HIGH precision/recall
ACCEPT_RELEX_HIGH precision/recall, if enabled
STORE_UNMAPPED_SURFACE_RELATION precision
Qualified negative/modal assertion accuracy
Typed endpoint coverage
False positives per chunk
Performance by predicate
Performance by domain
Performance by entity-density bucket
```

Real precision for these lanes remains unknown until the closed-world corpus
is populated and evaluated.

## Change-Control Rule (Effective Immediately)

No new extractor feature, frame, mapping, ontology predicate, or acceptance
behavior may enter the frozen core without:

1. A documented failure case
2. A positive fixture
3. Adversarial negative fixtures
4. An ablation delta
5. Candidate adjudication
6. A benchmark impact report

**Rationale:** The closed-world benchmark must measure a fixed implementation.
Any change invalidates calibration data and requires re-annotation.

## GIT-FROZEN Upgrade

When ready to commit the frozen state:

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

Then update `EXTRACTION_FREEZE_MANIFEST_20260802.md` with:
- `freeze_commit_sha`
- `tag: extraction-core-v1.0.0`
- `clean_working_tree: true`
- `freeze_classification: GIT-FROZEN`

## Status Summary

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

Percentage-style readiness estimates are informal planning figures only and
are not normative release metrics.

The architecture is no longer the variable under investigation. The next
phase measures whether the frozen architecture produces sufficiently accurate
knowledge under closed-world, document-separated evaluation.
