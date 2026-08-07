# Frozen matching policy

Freeze ID: `technical-book-66-v1`

The legacy baseline scorer lowercased and tokenized endpoint names, removed a leading article, and then accepted either an exact normalized name or an undeclared prefix/suffix containment involving at least two tokens. It did not distinguish aliases, normalized variants, and subsumption.

That behavior is retained only to reproduce the original 35-of-66 result. It is prohibited for future acceptance scoring.

Future scorer versions must emit exactly one of these classes for each gold assertion:

1. `EXACT`
2. `DECLARED_ALIAS`
3. `NORMALIZED_VARIANT`
4. `DECLARED_SUBSUMPTION`
5. `NO_MATCH`

`DECLARED_ALIAS` and `DECLARED_SUBSUMPTION` require separate explicit rule registries. Embedding similarity and unconstrained fuzzy semantic matching are prohibited.
