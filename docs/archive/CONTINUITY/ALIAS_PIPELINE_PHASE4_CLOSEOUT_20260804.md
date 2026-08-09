# Alias Pipeline Phase 4 Closeout — 2026-08-04

## Status: COMPLETE — HARD PAUSE

Gate implemented and unit-tested. **No production mutation. No Fast schema-expansion activation. No Phase 5 started.**

## What shipped

**`backend/services/ingestion/alias_gate.py`**
- `run_alias_gate(candidates, incomplete=...)` → `AliasGateBatch`
- Versioned `gate_release = alias_gate.v1`
- Deterministic reason codes + `decision_hash` via `AliasDecisionV1.create`
- Pure function: sort-stable replay, no I/O, no corpus writes

### Decision policy (enforced)

| Candidate type | Decision |
|---|---|
| explicit_alternate_name / explicit_alias_pattern / curated / casing / punct | `ACCEPT_IDENTITY` |
| acronym_long_form / explicit_abbreviation | `ACCEPT_IDENTITY` (scope=`document`; ambiguity check) |
| former_name | `ACCEPT_TEMPORAL_IDENTITY` |
| proper_name_apposition + known-as rule | `ACCEPT_IDENTITY` |
| proper_name_apposition (no explicit signal) | `REVIEW` |
| extraction_surface_variant / morphological | `ACCEPT_RETRIEVAL_ONLY` |
| relex_synonym_relation (uncorroborated) | `ACCEPT_RETRIEVAL_ONLY` |
| relex_synonym_relation + deterministic corroboration | `ACCEPT_IDENTITY` |
| semantic_related_term | `REVIEW` (shadow-only; no retrieval expansion) |
| descriptive / role / location apposition | `REJECT` |

### Additional enforcement

- Source-span, evidence-text, rule-ID, scope validation (fail-closed → `REJECT`)
- Entity-type conflict → `REVIEW` / `type_conflict`
- Document-scoped ambiguous acronyms → both `REVIEW` / `ambiguous_acronym`
- Same acronym in different documents: each may `ACCEPT_IDENTITY` at **document** scope (no cross-doc merge at gate)
- Incomplete candidates never receive `ACCEPT_*`

## Receipts (MEASURED)

```text
cd backend && ../local_ghost_b/.venv/bin/python -m pytest \
  tests/test_alias_gate_phase4.py \
  tests/test_alias_identity_contracts.py \
  tests/test_alias_candidates_phase2.py \
  tests/test_alias_apposition_phase3.py -q
→ 48 passed
```

## Explicitly NOT done (pause boundary)

- Phase 5 document/corpus identity clustering
- Schemas vocabulary projection / trust-class field split
- Retrieval Fast schema-expansion activation
- Production corpus mutation / backfill / reingest
- Isolated e2e fixture corpus ingest

## Resume pointer

Next authorized slice (when owner says go): **Phase 5 — document identity clustering**, still under `production_mutation_authorized=false`.
