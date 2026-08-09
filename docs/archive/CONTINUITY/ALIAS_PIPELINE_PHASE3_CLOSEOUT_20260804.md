# Alias Pipeline Phase 3 Closeout — 2026-08-04

## Status: COMPLETE (code + tests; no prod mutation)

## What changed

1. **`backend/services/extraction/appos_enrichment.py`**
   - Typed classifier: `explicit_name_alias` | `descriptive_common_noun` | `role` | `location` | `ambiguous_name_like`
   - Length is **never** used as an alias decision
   - Dual source spans + evidence + rule_id on every observation
   - Adjacency gap filter kills spaCy false appos (e.g. “stores embeddings”)
   - Explicit `known as` / `a.k.a.` span path (spaCy often misses these as `dep=appos`)
   - Legacy `spacy_appos_enrichment` policy-corrected: only `explicit_name_alias` enters aliases dict; descriptive/role/location → defs only; ambiguous omitted

2. **`backend/services/ingestion/alias_candidates.py`**
   - `_wrap_appositions` → `AliasCandidateV1` / incomplete
   - `collect_alias_candidates(..., include_appositions=True, spacy_doc=None)`
   - Shared spaCy Doc reuse via `spacy_doc=`

## Mapping → candidate types

| Class | Candidate type | Legacy aliases dict |
|---|---|---|
| explicit_name_alias | proper_name_apposition | yes |
| ambiguous_name_like | proper_name_apposition (low conf / review) | no |
| descriptive_common_noun | descriptive_apposition | no |
| role | role_apposition | no |
| location | location_apposition | no |

## Receipts (MEASURED)

```text
cd backend && ../local_ghost_b/.venv/bin/python -m pytest \
  tests/test_alias_apposition_phase3.py \
  tests/test_alias_candidates_phase2.py \
  tests/test_alias_identity_contracts.py -q
→ 30 passed
```

## Explicitly NOT done

- Phase 4 alias gate
- Production corpus mutation / backfill
- Fast schema expansion activation
- Enabling appos as sole identity authority (gate still required)

## Next

Phase 4: deterministic `alias_gate` → ACCEPT_IDENTITY / ACCEPT_RETRIEVAL_ONLY / REVIEW / REJECT.
