# Alias Identity — Authoritative State (2026-08-04)

Owner-ratified correction. Supersedes any claim that Polymath already has a
fully qualified deterministic alias identity system.

## BLUF

Current system has **alias candidates and retrieval expansions**, not a fully
qualified deterministic alias identity system.

```yaml
alias_candidate_generation: implemented_partial
explicit_in_text_alias_mining: implemented
acronym_long_form_mining: implemented
curated_alias_map: implemented
surface_variant_collection: implemented
spacy_apposition_alias_path: wired_typed_candidates  # Phase 3; gate still required
description_vs_alias_policy: classified  # length never decides; descriptive≠alias
evidence_backed_alias_records: document_clustered  # Phase 5 DocumentEntityV1; no prod writers yet
ambiguity_gate: implemented_unit_tested  # Phase 4; no prod / Fast activation
parent_alias_aggregation: implemented_unit_tested  # Phase 5; co-occurrence≠identity
document_identity_clustering: implemented_unit_tested  # Phase 5
corpus_identity_clustering: implemented_unit_tested_shadow  # Phase 6; no prod writes / Fast off
shadow_schemas_projection: implemented_unit_tested  # Phase 7; trust classes separated
schema_assisted_retrieval: live_shadow_wired  # Phase 8; Fast/Hybrid/Graph; ranking off; global off
document_scoped_acronym_resolution: not_implemented
mention_to_document_to_corpus_identity: partial
alias_precision_recall: not_measured
canonical_cluster_quality: not_measured
fast_schema_alias_expansion:
  status: shadow_canary_only  # Phase 8; enabled_globally=false; ranking_enabled=false
  allowed_mode: shadow_or_bounded_fixture
```

## Three trust classes (do not collapse)

1. **Proven aliases** — explicit in-text identity equivalence with spans/rule/scope
   retained → may `ACCEPT_IDENTITY`.
2. **Surface-form variants** — casing/punctuation/extraction surfaces →
   `candidate_type: surface_variant`, `identity_merge_allowed: false`, bounded
   retrieval expansion only.
3. **Related descriptions / concepts** — common-noun appositions, semantic
   neighbors → description/type/related_term; **never** auto-alias.

## Field reinterpretation (binding until gate exists)

`query_aliases` must be treated as **`retrieval_surface_variants`** until an
alias gate accepts identity equivalence. Do not treat the field as one trust
class mixing explicit aliases, casing, abbreviations, descriptions, and loose
normalized forms.

Retrieval trust set today (`_TRUSTED_EXACT_ALIAS_METHODS` in
`services/retriever/vocabulary.py`):
`explicit_alias_pattern`, `schwartz_hearst_acronym`, `extraction_surface_form`.

## Target architecture

```text
Relex entity mentions
+ spaCy parse
+ deterministic text patterns
+ curated alias map
        ↓
AliasCandidate records (typed)
        ↓
Alias gate
├── ACCEPT_IDENTITY
├── ACCEPT_RETRIEVAL_ONLY
├── REVIEW
└── REJECT
        ↓
Mention-level identity
        ↓
Document-scoped entity clusters
        ↓
Corpus-scoped canonical entities
        ↓
Schemas vocabulary projection
  trusted_aliases | retrieval_surface_variants | related_terms | descriptions
```

## Apposition rule (binding)

Do **not** wire `appos_enrichment.py` unchanged. Length ≤ 60 is not a valid
alias decision. Must classify name-like vs descriptive vs role vs location
apposition before any ACCEPT_IDENTITY.

## Fast schema expansion until qualification

```yaml
explicit_proven_alias:
  may_expand_query: true
  may_pull_linked_child_anchors: true
surface_variant:
  may_expand_query: bounded
  may_change_final_ranking: limited
semantic_related_term:
  may_expand_query: shadow_only
  may_replace_original_query: false
schema_record_as_answer_evidence:
  allowed: false
```

Original-query evidence lane must always remain authoritative.

## Implementation order (next slices)

1. Inventory every producer feeding schema aliases.
2. Stop treating all `query_aliases` as one trust class.
3. Introduce typed AliasCandidate records.
4. Fix and wire apposition classification.
5. Add deterministic alias gate.
6. Add rule IDs, offsets, evidence, scope.
7. Document-scoped acronym clustering.
8. Project only accepted aliases into trusted schema fields.
9. Keep retrieval-only variants in a separate field.
10. Run q9 positive/negative fixtures.

## q9 acceptance (alias block)

```yaml
alias_pipeline:
  every_candidate_has_type: true
  every_accepted_alias_has_rule_id: true
  every_accepted_alias_has_source_offsets: true
  every_accepted_alias_has_evidence_text: true
  every_accepted_alias_has_scope: true
  deterministic_replay: true
negative_controls:
  common_noun_apposition_as_alias: 0
  ambiguous_acronym_cross_document_merge: 0
  semantic_similarity_auto_merge: 0
  surface_variant_auto_merge_without_rule: 0
retrieval:
  original_query_lane_preserved: true
  schema_expansion_traceable: true
  schema_records_used_as_answer_evidence: 0
```

## Final invariant

> GLiNER-Relex supplies entity mentions and may detect explicitly stated
> synonym relations. Deterministic miners generate evidence-backed alias
> candidates. A separate gate must decide identity, retrieval-only expansion,
> review, or rejection. **That gate is the missing piece.**
