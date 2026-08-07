# Acceptance criteria

The implementation is not complete until all structural, quality, performance, and reachability gates pass.

## Structural gates

- Canonical Graphify entrypoint preserved.
- Repository discovery map exists and validates.
- GLiNER2 CPU-only assertion passes.
- One warm GLiNER2 model instance in primary path.
- Raw mentions persisted before gating.
- Raw mention conservation equation passes.
- Entity reducer conservation equation passes.
- Mention completion preserves exact offsets.
- OpenIE outputs never write directly to the graph.
- Argument Adapter classifies every OpenIE argument.
- Proposition Reducer clusters and preserves OpenIE variants.
- Predicate compiler abstains when mapping is ambiguous.
- No forced `related_to` fallback.
- Qualified claims are not positive graph edges.
- Graph projection rebuilds from canonical artifacts.
- Second E2E run is idempotent.

## Entity quality gates

```yaml
entity_exact_span_f1: ">= 0.85"
entity_type_f1: ">= 0.85"
generic_noun_false_positive_rate: "<= 0.05"
accepted_pronoun_endpoints: 0
strict_evidence_alignment: 1.0
```

## OpenIE / relation gates

```yaml
openie_plus_precision_gold_pair_recall: ">= 0.65"
directed_canonical_triple_precision: ">= 0.90"
directed_canonical_triple_recall: ">= 0.60"
directed_canonical_triple_f1: ">= 0.72"
negation_fixture_accuracy: 1.0
modality_fixture_accuracy: 1.0
attribution_fixture_accuracy: 1.0
unsupported_graph_edges: 0
forced_related_to_fallback_count: 0
```

## Performance gate

```yaml
full_refactored_graphify_speed_ratio_vs_relex_baseline: ">= 1.5"
```

If speed fails, identify the dominant stage and optimize inside the CPU architecture only.

## Production boundary

Two-document E2E validates wiring, offsets, contracts, and approximate throughput. It does not authorize production graph writes. Production promotion still requires closed-world calibration and held-out qualification.
