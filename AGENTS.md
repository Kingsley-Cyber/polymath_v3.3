# Polymath Engineering Invariants

## Retrieval

- Treat retrieval as a top-down system: selected corpus scope -> document
  profiles -> section/rollup summaries when the tier permits -> parent
  summaries -> child evidence. Document and tree summaries route discovery;
  final claims must descend to source evidence.
- Evaluate every retrieval change across all three product tiers. Focused
  retrieval is Qdrant-only, Hybrid adds Mongo lexical recall and hydration,
  and Graph Augmentation adds Neo4j facts and expansion. Do not violate those
  storage boundaries to make one test pass.
- Query planning must preserve the user's exact message and produce complete,
  independently meaningful answer obligations. Do not inject corpus-specific
  terms into a generic query; discover relevant domains through routing.
- Routing is a scope prior, not a weak score boost. Preserve selected corpus
  identities, reserve relevant routed documents, and keep a bounded global
  wildcard fallback for routing mistakes.
- Vocabulary resolution over multiple selected corpora must fan out within
  each corpus boundary, merge into one globally ranked result set, retain
  `corpus_id` plus global and corpus-local ranks, and reserve representation
  per selected corpus. Never satisfy a unified query with an unscoped global
  vocabulary search.
- Ranking must consider relevance, obligation coverage, document/corpus
  representation, metadata, schema, parent/child identity, and diversity.
  Never optimize MMR or final top-k independently of the initial candidate
  budget and required answer shape.
- When changing retrieval, add regression coverage for query planning,
  hierarchy descent, tier store boundaries, cross-corpus fairness, reranking,
  and final evidence coverage.

## Qdrant Engineering References

When executing the RAPTOR/RAG implementation checklist or changing Qdrant
storage, indexing, filtering, batching, hybrid search, monitoring, scaling, or
deployment, consult these upstream sources as best-practice references:

- [qdrant/skills](https://github.com/qdrant/skills): official optimization,
  scaling, monitoring, and search-quality decision guidance.
- [qdrant/vector-db-benchmark](https://github.com/qdrant/vector-db-benchmark):
  reproducible vector-database performance benchmarking patterns.
- [qdrant/workshop-ultimate-hybrid-search](https://github.com/qdrant/workshop-ultimate-hybrid-search):
  data-driven dense, sparse, Query API, and RRF evaluation patterns.
- [qdrant/qdrant-client](https://github.com/qdrant/qdrant-client): canonical
  Python client contracts for batching, querying, filtering, and collection
  management.
- [qdrant/qdrant](https://github.com/qdrant/qdrant): upstream engine source for
  implementation truth. Do not fork or modify the engine to compensate for
  repository-owned schema, lifecycle, or query-path defects.

Use these sources to inform decisions, not as permission to copy defaults
blindly. Measure the live Polymath workload first; record Qdrant server time,
end-to-end wall time, candidate counts, payload bytes, memory, write pressure,
and retrieval quality before and after each change. Preserve the three-tier
retrieval contract, corpus isolation, source provenance, and strict readiness.
Pilot migrations and quantization on a cloned small corpus with rollback and
quality evaluation before changing active large corpora.

## Goal Drift and Patch Overfitting

- Guard against AI goal drift that scope-collapses a global or root-cause
  objective into a local symptom fix. Do not optimize only for the visible
  bug, failing test, error message, example query, fixture, document, corpus,
  provider, model, or code path.
- Before editing code:
  1. Restate the user's global objective.
  2. Explain the difference between the specific symptom and the underlying
     bug class or violated shared invariant.
  3. Search the repository for all related patterns, implementations, callers,
     adapters, storage boundaries, and execution paths.
  4. Propose the smallest fix at the shared ownership layer that addresses the
     bug class without broad unrelated refactoring.
  5. Define regression coverage for the exact failing case, at least two
     adjacent cases, and one different module or call path.
- Reject a solution that handles only the supplied example unless repository
  evidence proves the defect is isolated. Treat a patch that merely satisfies
  the observed case while failing to generalize as patch overfitting.
- Tests must validate the shared invariant and externally observable behavior,
  not encode one query's wording, one expected ranking, one corpus-specific
  concept, or another implementation detail solely to make the test pass.
- Before declaring completion, re-check the original global objective against
  every affected path found during repository search and report any path that
  remains intentionally unchanged, including the reason.
