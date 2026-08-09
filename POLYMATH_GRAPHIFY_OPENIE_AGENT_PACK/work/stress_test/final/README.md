# Final Graphify verification bundle

This directory is the analysis handoff for the frozen technical-book answer-key run and the two-document Graphify E2E.

## Result

- Frozen gold assertions: 66 positive and 5 qualified.
- Correct canonical assertions promoted: 61 of 66.
- Neo4j triples: 68 across all predicates, 66 inside the gold ontology, 2 `stores` triples outside it.
- Precision, recall, and F1: 0.924242 each.
- Match classes: 0 EXACT, 0 DECLARED_ALIAS, 61 NORMALIZED_VARIANT, 0 DECLARED_SUBSUMPTION, 5 NO_MATCH.
- Recall checkpoints: 62 endpoint pairs, 60 correct surface propositions, 61 correct canonical promotions.
- Failure taxonomy: 2 MISSING_PAIR, 2 ENTITY_TYPE, 1 GATE_POLICY, all other declared classes 0.
- Qualified gold leaked into positive Neo4j edges: 0.
- Prohibited embedding or fuzzy matchers used: 0.
- Frozen scorer: `stress-answer-key-scorer-v4-qualified-taxonomy`.

The isolated two-document E2E promoted 15 distinct Neo4j relation triples after conservative semantic filtering. The separate technical-book graph promoted 68 triples because it uses its own frozen namespace and fixture.

## Frozen inputs

- Fixture SHA-256: `2c0499505455b782cd6a4a2a9bf888141a820e89919eb0866bf9ba4270bd2391`.
- Answer-key SHA-256: `8e402f5e2f16ca8011bd23facf3f70a52c846b7618a58a8659105d1801d7d7a2`.
- Matching-policy SHA-256: `9de099dbec4053be29fd0113b55759c4d186b68783b9cc68098b4ea04298b393`.
- Namespace: `technical_book_stress_v1`.
- Mongo database: `graphify_e2e_technical_book_stress_v1_bc4ad2c7`.
- Corpus ID: `0a858214-cc58-5615-9b4b-ba177ffd3ca3`.
- Document ID: `9e3e45389e2e73da5cc6838d98f99f7d59b3dccbf1cb08035f8ad496628365c7`.

## Test Markdown files

- `fixtures/reliable_event_processing_chapter.md`: frozen 66-assertion stress document.
- `fixtures/graphify_quality_fixture.md`: two-document E2E quality document.
- `fixtures/graphify_throughput_fixture.md`: two-document E2E throughput document.

## Files to analyze first

1. `scoring/score.json`: summary metrics and five match classes.
2. `scoring/answer_key_comparison.jsonl`: one row per gold assertion with its match class and checkpoint states.
3. `scoring/failure_taxonomy.json`: every unmatched gold assertion and loss category.
4. `scoring/neo4j_promoted_triples.json`: triples actually promoted to the isolated Neo4j graph.
5. `scoring/qualified_assertions.json`: retained qualified candidates.
6. `quality_preview_random_5.json` and `throughput_preview_random_5.json`: deterministic five-record samples.
7. `e2e/`: graph rebuild, idempotency, quality, speed, and reachability proof.
8. `verification/final_status.json`: final controller result and release boundary.
9. `scoring/semantic_safety.json`: frozen hashes, ≥90% semantic gates, and wildcard/qualification safety checks.

Production graph-write promotion remains pending. These isolated namespaces do not authorize production writes.
