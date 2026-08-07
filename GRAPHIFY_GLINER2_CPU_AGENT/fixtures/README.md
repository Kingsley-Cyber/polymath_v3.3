# Fixed E2E Fixtures

## `graphify_quality_fixture.md`

A compact adversarial quality document with exact-offset gold for all canonical entity types and predicates, passive direction, coordination, definitions, aliases, lowercase valid names, ambiguous surface forms, generic-noun traps, pronoun traps, qualified claims, and open surface relations.

## `graphify_throughput_fixture.md`

A deterministic 100–250 KB throughput/tail document containing 160 modules, repeated furniture, tables, code blocks, reference sections, a global bibliography, recurring technical entities, and pathological coordination. Its sidecar contains structural expectations and stratified exact-offset gold.

The fixtures prove implementation connectivity and provide stable comparative scores. They do not replace the repository's document-separated closed-world held-out qualification.

Do not edit the Markdown or sidecars during the refactor. Regeneration is deterministic through `scripts/regenerate_fixtures.py`; changing the generator is a benchmark change that requires a new fixture version.
