# Local Extraction Quality Rubric

## BLUF

This rubric grades whether a local extraction workflow is safe and useful for Polymath graph writes.

It is intentionally stricter than plain F1. A model can be fast and schema-valid while still being bad at ontology extraction. The rubric separates those cases.

Executable grader:

```bash
/Users/king/PolymathRuntime/apple_ml_services/.venv/bin/python \
  /Users/king/polymath_v3.3/scripts/grade_extraction_quality_rubric.py \
  --report /tmp/model_report.json \
  --gold /Users/king/polymath_v3.3/scripts/local_extraction_fixtures/13_building_ai_mobile_apps_gold_v1.json \
  --samples /Users/king/polymath_v3.3/scripts/local_extraction_fixtures/13_building_ai_mobile_apps_10_chunks.jsonl \
  --out /tmp/model_rubric.json
```

## Score Shape

The script emits two top-level scores:

- `quality_score`: extraction correctness only.
- `deployment_score`: quality plus throughput readiness.

Labels:

- `90-100`: production_candidate
- `80-89`: near_cloud_candidate
- `65-79`: research_promising
- `45-64`: prototype_only
- `<45`: not_ready

## 100-Point Quality Score

### 1. Contract Safety - 20 points

This answers: can the current stack safely accept the output?

- Schema/Pydantic pass rate.
- No truncation.
- No reasoning trace leakage.
- Low dropped item rate.
- Low parser/evidence error rate.

Hard reject signals:

- Schema pass below 100%.
- Truncated JSON.
- Unsupported enum labels.
- Evidence strings not found in source text.

### 2. Entity Quality - 25 points

This answers: did the extractor find useful graph nodes without flooding the graph?

- Entity precision.
- Entity recall.
- Entity F1.
- Noise control for dirty surfaces, generic extras, metadata, sidebar fragments, and bad clean surfaces.

Common failure categories:

- `missed_entity`: gold entity not found.
- `extra_entity`: model invented or over-selected a node.
- `dirty_surface`: markup/sidebar/citation/junk entity.
- `generic_extra`: weak standalone term such as `api`, `ai`, `data`, `image`, or `source`.

### 3. Relation Quality - 40 points

This is the most important graph-write category.

- Relation precision.
- Relation recall.
- Relation F1.
- Endpoint fit: did the model at least pick the right pair?
- Predicate/direction fit: did it choose the right relation label and direction?

Relation failure categories:

- `wrong_predicate_right_direction`: endpoints are right, predicate is wrong.
- `wrong_direction_right_predicate`: predicate is right, edge direction is reversed.
- `wrong_direction_and_predicate`: both direction and predicate are wrong.
- `wrong_endpoint_with_right_predicate`: predicate is plausible, but subject/object are wrong.
- `unsupported_or_overbroad_endpoint`: one endpoint is related to the topic but not the actual relation endpoint.
- `unsupported_extra_relation`: relation has no gold support.
- `generic_related_to_extra`: model used `related_to` as a weak extra edge.

Missed relation categories:

- `missed_relation_both_endpoints_missing`
- `missed_relation_subject_missing`
- `missed_relation_object_missing`
- `missed_relation_wrong_predicate`
- `missed_relation_wrong_direction`
- `missed_relation_endpoints_present_no_edge`

### 4. Graph Usefulness - 15 points

This answers: is the accepted object worth writing to Mongo/Neo4j?

- Combined graph F1.
- Nonzero accepted relations.
- Predicate specificity, penalizing generic/unsupported `related_to` usage.

## Deployment Score

Deployment score is:

```text
quality_score * 0.90 + throughput_bonus
```

The throughput bonus is capped at 10 points and considers:

- chunks/hour
- median completion token/sec

This prevents a fast but wrong model from passing. Speed is a bonus only after quality exists.

## Calibration Results

### Fused Liquid LFM2-1.2B Extract Fine-Tune

Report:

`/tmp/polymath_lfm2_extract_ft_fused_10.json`

Rubric:

`/tmp/polymath_lfm2_extract_ft_fused_10_rubric.json`

Result:

```text
quality_score: 40.1/100
deployment_score: 43.91/100
label: not_ready

contract: 15.93/20
entity:   13.54/25
relation:  3.68/40
graph:     6.96/15
speed:     7.81/10

entity P/R/F1:   80.43% / 22.42% / 35.07%
relation P/R/F1:  8.33% /  2.67% /  4.04%
graph F1:        19.56%
```

Interpretation:

The fine-tune learned the JSON contract and runs at useful token speed, but it is not ready for graph writes. It misses too many relation endpoints and over-links broad/unsupported concepts.

Top failures:

- `unsupported_or_overbroad_endpoint`
- `wrong_endpoint_with_right_predicate`
- `wrong_predicate_right_direction`
- `missed_relation_both_endpoints_missing`
- `missed_relation_object_missing`

### Fixture-Seeded Deterministic Compiler

Report:

`/tmp/polymath_python_fixture_seeded_10.json`

Rubric:

`/tmp/polymath_python_fixture_seeded_10_rubric.json`

Result:

```text
quality_score: 87.09/100
deployment_score: 84.38/100
label: near_cloud_candidate

contract: 20.00/20
entity:   15.19/25
relation: 39.77/40
graph:    12.13/15
speed:     6.00/10

entity P/R/F1:   28.77% / 86.67% / 43.20%
relation P/R/F1: 100.0% / 98.67% / 99.33%
graph F1:        71.27%
```

Interpretation:

This is an upper-bound sanity test, not a production extractor, because the relation templates are seeded from the fixture. It proves that the deterministic compiler can produce near-perfect relation quality when given correct relation knowledge.

## Production Gate

A local extraction lane is not eligible for automatic Neo4j graph writes unless:

- `quality_score >= 80`
- `relation_precision >= 90%`
- `relation_recall >= 70%`
- schema pass is 100%
- truncation count is 0
- evidence errors are 0 after Python gating
- unsupported/generic extra relations are near zero

Anything below that can still be used as:

- entity helper
- relation proposal helper
- training data generator
- review queue candidate

But it should not write final graph edges unattended.
