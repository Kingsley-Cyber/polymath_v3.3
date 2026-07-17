# Random Cascade Quality Test

## BLUF

This is a seeded random stress test against raw stored Ghost B relation labels. It is intentionally harder than the clean held-out eval because it samples noisy `part_of`, `uses`, and `related_to` rows from the full extraction corpus.

## Setup

- Seed: `20260606`
- Sample size: `240`
- Raw relation candidates scanned: `736687`
- Device: `mps`
- Pairs/sec after load: `36.62`

## Metrics

- `exact_prediction_count`: `81`
- `exact_prediction_rate`: `0.3375`
- `exact_precision_vs_ghost_b`: `0.7778`
- `exact_coverage_vs_ghost_b`: `0.2625`
- `family_accuracy`: `0.375`
- `related_to_fallback_rate`: `0.3167`
- `drop_rate`: `0.3458`
- `safe_or_correct_rate`: `0.5792`
- `typed_precision_excluding_related_to`: `0.6842`
- `typed_coverage_excluding_related_to`: `0.1625`

## Top Gold Labels

- `part_of`: `63`
- `related_to`: `47`
- `uses`: `31`
- `created_by`: `20`
- `references`: `16`
- `instance_of`: `15`
- `located_in`: `7`
- `produces`: `6`
- `works_for`: `6`
- `supports`: `4`
- `depends_on`: `4`
- `detects`: `3`

## Top Confusions

- `part_of` -> `member_of`: `5`
- `related_to` -> `member_of`: `1`
- `works_for` -> `member_of`: `1`
- `instance_of` -> `uses`: `1`
- `related_to` -> `example_of`: `1`
- `part_of` -> `supports`: `1`
- `related_to` -> `supports`: `1`
- `uses` -> `stores`: `1`
- `supports` -> `example_of`: `1`
- `part_of` -> `detects`: `1`

## Interpretation

- The cascade loads and runs correctly on Mac MPS from the flash-drive model bundle.
- Typed exact precision on a fully random raw Ghost B sample is lower than the clean held-out eval because the sample is dominated by ambiguous `part_of`, `uses`, and `related_to`.
- The safety behavior is working: uncertain rows mostly route to `related_to` or `drop` instead of forcing exact predicates.
- For production, keep the candidate-pair gate and caps enabled; do not feed all random relation candidates blindly.
