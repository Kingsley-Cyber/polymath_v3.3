# Alias Pipeline — Phase 0 Baseline Inventory

**Date:** 2026-08-04T17:55Z  
**Directive:** `polymath_alias_pipeline_directive` v1.0  
**Authority:** owner_approved · `production_mutation_authorized=false`  
**Companion:** `CONTINUITY/ALIAS_IDENTITY_AUTHORITATIVE_STATE_20260804.md`  
**Machine artifact:** `data_eval/alias_pipeline/baseline_inventory.json`

---

## BLUF

Live system produces **best-effort alias strings + method-tagged evidence** into
Mongo `corpus_lexicon` and Qdrant schemas. There is **no** AliasCandidateV1 /
AliasDecisionV1 / alias gate / document↔corpus identity machine. Fast schema
expansion remains **unqualified**.

---

## 1. Live producers (call-path confirmed)

| # | Producer | Path | What it emits | Live? | Identity authority today |
|---|---|---|---|---|---|
| P1 | `extract_aliases` | `backend/services/ingestion/enrich.py` | `dict[canon → list[str]]` via Schwartz-Hearst + casing variants | **Yes** — called from `extraction_artifacts.py`, `runpod_flash_extraction.py`, `enrich` helper | Strings only; no dual spans in output |
| P2 | `mine_entity_text_evidence` | `backend/services/ingestion/corpus_lexicon.py` | `{alias, method, evidence}` for SH + explicit patterns | **Yes** — during lexicon materialization | Method-tagged; evidence string; chunk_id attached later |
| P3 | Curated map | `config/canonical/entity_aliases.json` (33 keys); fallback `backend/services/graph/entity_aliases.json` | Exact alias → canonical via `resolve_entity_alias` / `canonicalize_entity_name` | **Yes** at extraction/graph boundary | Versioned file; not projected as typed candidates |
| P4 | `relex_local._consolidate_entities` | `backend/services/ingestion/relex_local.py` | Alternate surfaces → `query_aliases[]` under same `canonicalize_entity_name` key | **Yes** on Relex path | Retrieval candidate only (directive) |
| P5 | Lexicon absorption of `query_aliases` | `corpus_lexicon.py` `method="extraction_query_alias"` | Identity-alias add via `add_identity_alias` | **Yes** | **Not** in `_TRUSTED_EXACT_ALIAS_METHODS` |
| P6 | Lexicon absorption of surfaces | `method="extraction_surface_form"` | Surface when abbreviation/substring rules pass | **Yes** | Trusted for exact identity if evidence method matches |
| P7 | Ghost B / LLM prompt `qa` | `backend/services/ghost_b.py` | Model-suggested query_aliases | Path exists | Candidate artifact path **recomputes** aliases via P1 and drops provider-supplied (`test_extraction_artifact`) |
| P8 | `synonym_of` relation | Relex + `ghost_b` schema; folded in `graph/orchestrator.py` | Synonym clusters for **prompt packing**, not alias gate | **Yes** (graph context) | Corroborating only; not AliasDecision |
| P9 | `promote.py` | Uses `query_aliases` for term norms | Downstream promote | Yes | Not a miner |

### Confirmed dead

| Component | Path | Status |
|---|---|---|
| `spacy_appos_enrichment` / `extract_apposition_observations` | `backend/services/extraction/appos_enrichment.py` | **Phase 3:** typed classifier + wired via `alias_candidates._wrap_appositions` (legacy adapter policy-corrected; length never decides) |
| `get_shared_nlp` | same module | Live for FrameExtractor / dep_path only — **not** alias path |
| `dedupe_entities` | `local_ghost_b/tools/chunk_with_gliner.py` | Tooling only; keys `surface.lower()` — weak |

---

## 2. Storage / projection chain

```text
Extraction entity.query_aliases (string list)
        + mine_entity_text_evidence(method, evidence)
        ↓
Mongo corpus_lexicon
  aliases[]
  aliases_normalized[]
  alias_evidence[{alias, alias_key, method, chunk_id, parent_id, confidence, evidence}]
        ↓
qdrant_writer.upsert_lexicon_entries / _lexicon_payload
  schemas collection kind=entity_lexicon
  payload.aliases / aliases_normalized (≤32)
        ↓
retriever/vocabulary.py
  mongo exact alias lookup
  _trusted_exact_identity_match requires method ∈
    {explicit_alias_pattern, schwartz_hearst_acronym, extraction_surface_form}
```

**No separate fields** for `trusted_aliases` / `retrieval_surface_variants` /
`related_terms` / `descriptions`. Everything collapses into `aliases[]` +
`alias_evidence[].method`.

**No collections** named `alias_candidates` / `alias_decisions` /
`document_entities` / `corpus_entities` (logical targets for later phases;
adapt or add — do not duplicate authorities).

---

## 3. Live Mongo evidence mix (MEASURED 2026-08-04)

`corpus_lexicon` alias_evidence method histogram (all corpora on this host):

| method | count |
|---|---:|
| `schwartz_hearst_acronym` | 4520 |
| `extraction_surface_form` | 196 |
| `explicit_alias_pattern` | 98 |
| `extraction_query_alias` | 2 |

Also: `with_aliases` ≈ 1769 docs; `with_alias_evidence` ≈ 1834.

Example proven shape (good seed for AliasCandidateV1 wrap):
```json
{
  "canonical_name": "crud",
  "aliases": ["create, read, update, delete"],
  "alias_evidence": [{
    "method": "schwartz_hearst_acronym",
    "chunk_id": "..._0312",
    "evidence": "CRUD (create, read, update, delete)",
    "confidence": 1
  }]
}
```

**Gaps vs AliasCandidateV1:** no `rule_id` / `rule_release` / `sentence_id` /
dual integer spans / `alias_candidate_id` / `decision` / `scope` /
`contract_hash`.

---

## 4. Retrieval trust (current)

```python
_TRUSTED_EXACT_ALIAS_METHODS = {
    "explicit_alias_pattern",
    "schwartz_hearst_acronym",
    "extraction_surface_form",
}
```

- `extraction_query_alias` → **not trusted** for exact identity (good).
- Untrusted aliases can still appear in `aliases[]` and affect non-exact paths.
- Schema records must not be answer citations (already product rule; keep).

---

## 5. Existing tests (alias-touching)

| File | Coverage |
|---|---|
| `tests/test_corpus_lexicon.py` | `clean_alias`, mining, structure≠identity, document projection merges |
| `tests/test_extraction_artifact.py` | Provider `query_aliases` ignored; P1 recomputed |
| `tests/extraction/test_canonical_contract.py` | Curated map validation / idempotence |
| `tests/test_vocabulary_resolver.py` | One-off model alias needs confirmation |
| `tests/test_runpod_flash_extraction.py` | Flash path alias merge |
| `tests/test_promote.py` | Promote consumes aliases |

**Missing:** AliasCandidate/Decision contracts, apposition classifier, ambiguity
gate, document-scoped acronym non-merge, deterministic replay of decisions,
schema field separation, Fast expansion trace fixtures.

---

## 6. Apposition before/after (policy)

| | Before (dead code) | Required after fix |
|---|---|---|
| Decision rule | `len(appos_text) ≤ 60` → alias | Typed class: name / descriptive / role / location / ambiguous |
| `Microsoft, a software company` | Would become alias if wired | Description; REJECT as alias |
| `Abel Tesfaye, known as The Weeknd` | Ambiguous under length rule | explicit_name_alias / REVIEW unless explicit signal |
| spaCy Doc | Would re-parse if called alone | Must reuse `get_shared_nlp()` Doc |

---

## 7. Recommended adaptation seams (prefer over parallel authority)

1. **Candidate emission:** wrap P1+P2 outputs in new module beside
   `enrich.py` / `corpus_lexicon.py` (suggested `alias_candidates.py`).
2. **Gate:** new `alias_gate.py` (or package) — no existing equivalent.
3. **Clustering:** extend lexicon document projection + new document/corpus
   entity records; do not invent a second lexicon.
4. **Schemas projection:** extend `_lexicon_payload` / materialize_entries to
   emit separated fields; keep reading legacy `aliases` / `query_aliases` as
   `legacy_unqualified`.
5. **Retrieval:** extend `_TRUSTED_EXACT_ALIAS_METHODS` only for
   `ACCEPT_IDENTITY` / `ACCEPT_TEMPORAL_IDENTITY` with provenance; shadow
   mode for Fast expansion until fixture qualification.

---

## 8. Hard boundaries (re-affirmed)

- No production corpus mutation / backfill / reingest
- No global Fast schema authority switch
- No enabling `appos_enrichment` unchanged
- No embedding-only or Relex-score-only identity
- No LLM/SLM in deterministic alias extraction
- Original-query evidence lane preserved

---

## 9. Phase 0 → Phase 1 handoff

**Next:** implement validation for AliasCandidateV1 / AliasDecisionV1 /
DocumentEntityV1 / CorpusEntityV1 + deterministic IDs/hashes (directive phase 1),
without wiring production writers yet.

**Do not** start fixture corpus ingest until phases 1–4 exist with unit tests.
