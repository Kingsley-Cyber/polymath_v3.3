"""Control Plane V2 — durable ingestion brain.

This package owns intake ledgering, desired-vs-observed artifact
reconciliation, and query-ready certificates. It drives the existing stage
planners/executors (`services.ingestion.*`) and never re-implements them.

Design contract (2026-07-27):
- Desired state is compiled from the document/corpus ingestion contract.
- Observed state is exact artifact IDs joined across Mongo/Qdrant/Neo4j
  receipts — never counts, never mutable flags.
- A document is query-ready only when an immutable certificate has been
  issued for its current contract hashes with empty ID-set differences.
- Workers/executors are disposable hands: they return receipts, they never
  decide stage transitions or completion.
"""
