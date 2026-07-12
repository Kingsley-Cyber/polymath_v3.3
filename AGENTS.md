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
