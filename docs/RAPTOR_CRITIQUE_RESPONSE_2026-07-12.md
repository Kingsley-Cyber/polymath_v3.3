# RAPTOR Critique Maintainer Response

Date: 2026-07-12

Source review: `Polymath v3.3 - RAPTOR RAG: Heavy Critique, Grounded
Roadmap, and GLiNER Plan Review`.

## Bottom line

The report is directionally strong. Polymath currently implements deterministic
top-down document routing and small-to-big evidence hydration, not the RAPTOR
paper's recursive semantic clustering. That is an honest naming and capability
gap, but it does not make the current hierarchy invalid. The immediate defects
were hollow summary projections, duplicated one-child descent, structural-noise
evidence, late cross-corpus quota pollution, and incomplete lifecycle cleanup.

## Live validation

- `polymath_v2`: 84,987 summary points per retrieval collection; 67,953 had an
  explicit empty `summary_model` and were therefore unproven placeholders.
- `ecommerce_AI_FILM_SCHOOL`: 9,375 summary points; 5,796 placeholders.
- `polymath_v2`: 20,014 of 20,427 section nodes had one rollup child.
- `ecommerce_AI_FILM_SCHOOL`: 668 of 739 section nodes had one rollup child.
- Qdrant contained collection families with no active corpus owner. They were
  not deleted automatically because an orphan sweep is destructive and needs a
  reviewed allow-list.
- A fresh Hybrid negative-control probe, `What is the boiling point of
  tungsten?`, returned required-lane coverage `0.0` and `answerable: false`.
  The report's earlier `answerable: true` result is stale relative to current
  `main`; the chat answerability gate and required-lane sufficiency have already
  evolved.
- The sidecar cold-start claim is partly stale. The deployed Jina reranker
  reports `warmup_complete=true`; both current sidecar health paths execute real
  inference rather than checking process liveness only.

## Implemented repair tranche

1. Explicit empty-model summary placeholders are excluded from Funnel A and
   refused at the Qdrant write boundary. Legacy rows with a missing field still
   pass; valid imports must carry explicit provenance such as `legacy_unknown`.
2. Separator-only markdown/table chunks are dropped during child creation and
   again before reranking. The guard is intentionally narrow and preserves
   code, identifiers, and equations.
3. Parser-emitted `Page N` headings inherit the previous semantic heading, so
   future trees do not fragment at every PDF page.
4. New one-child section points carry deterministic passthrough parent and
   lexicon IDs. The navigator uses them directly and skips duplicate rollup
   vector search.
5. Final cross-corpus reservation now requires bounded relevance. A selected
   corpus with no candidate above `max(0.25, 0.30 * top_score)` is diagnosed as
   skipped instead of receiving a forced low-score seat.
6. Corpus deletion now removes shared Tier-0 document cards, summary-tree rows,
   and supersedes all durable source, document, extraction, summary, and graph
   jobs in addition to dropping per-corpus Qdrant collections.
7. Quick Upload and stored local batches use the durable Mongo `stored_bytes`
   ledger for quota checks. Full filesystem traversal is fallback-only. Quick
   Upload files are authoritative under the host-mounted per-corpus drop-off,
   keep their original names, and remain directly auditable outside Docker.
8. Broad one-word aliases no longer trigger hand-authored semantic facets.
   Deterministic alias matching now requires a contextual phrase, canonical
   facet name, or protected technical acronym; corpus phrase mining remains.
9. Corpus bulk deletion is now a tracked, Mongo-leased operation. Interrupted
   and partial purges are recoverable after restart instead of depending on an
   unreferenced `asyncio.create_task` surviving the request process.

## Report claims not applied literally

- Query instruction prefixes were not added inside `embed_queries`. Despite its
  name, that function is also used to embed lexicon and summary-tree index
  documents. A global prefix there would silently mix query and document
  semantics. This needs a separate query-only API before activation.
- The internal ranking sufficiency field remains a repair driver. User-facing
  answerability is separately enforced in `chat_orchestrator`; renaming one
  field would not improve correctness.
- Existing sidecar warmup was retained. Adding another periodic inference loop
  would create contention with the same single MPS slot it is meant to protect.
- Existing orphan collections were not dropped automatically. First produce a
  dry-run ownership manifest and explicitly approve the deletion set.
- Collection triplication, clustered thematic nodes, and named-vector
  migrations are architecture programs, not safe quick fixes. They require
  recall, latency, memory, rollback, and migration evaluations.

## Next evidence-gated work

1. Finish real summary backfill from existing parent/child artifacts. No source
   re-ingestion is required.
2. Backfill one-child passthrough payloads in Qdrant from Mongo tree rows. This
   is a deterministic payload migration, not re-embedding or re-ingestion.
3. Batch corpus-lexicon lane queries per corpus and cache resolver output by
   corpus artifact epoch. Measure p50/p95 before and after.
4. Add salient-only user-language concept representations as additional points,
   then evaluate naive-query recall before scaling beyond one corpus.
5. Prototype semantic theme nodes on the small transcript corpus. Promote the
   layer only if it improves broad/cross-document recall without weakening
   focused evidence precision.
6. Add field-level extraction provenance and typed relation validation only
   after the domain/range table is reviewed against real accepted edges. Do not
   hard-drop relations from an unvalidated signature map.

## Verification contract

Every retrieval change remains subject to all three product tiers: Qdrant-only,
Hybrid, and Graph Augmentation. It must preserve selected corpus boundaries,
top-down document/tree routing, parent/child identity, source hydration,
required answer obligations, and final evidence relevance.
