# RAPTOR Critique Acknowledgement — 2026-07-12

This document is the acknowledgement referenced by
[`RAPTOR_CRITIQUE_RESPONSE_2026-07-12.md`](RAPTOR_CRITIQUE_RESPONSE_2026-07-12.md).
It records, without qualification, the audit findings the maintainer response
understated, and where each is tracked.

## Accepted findings

1. **Corpus-floor relevance gating was incomplete (P0.3).** Bounded relevance
   was added to late planned-fusion corpus reservations, but an
   already-selected corpus candidate could still be protected as a
   reservation without passing the same gate, and the `ranking_policy`
   corpus-floor path used only normalized MMR relevance (plus an
   unconditional +0.10 reserve bonus). One path could seat what the other
   rejected. Tracked as checklist P0.3; both deciders now share one
   calibrated reservation bound (`services/retriever/reservation_policy.py`).

2. **Cleanup leases relied on startup-only recovery (P0.6).** Orphaned or
   expired cleanup leases were reclaimed only when the service restarted;
   long purges could outlive their lease with no heartbeat, and partial
   cleanups required an operator restart to retry. Tracked as checklist P0.6.

3. **A referenced follow-up document did not exist (P0.7).** The maintainer
   response linked to this acknowledgement before it was written, and no
   automated check validates relative links in tracked Markdown. This
   document closes the link; `backend/scripts/check_markdown_links.py` adds
   the check. Tracked as checklist P0.7.

4. **Embedder "readiness" conflated model load with inference warmup
   (P1.8).** The embedder health endpoint verified model load and stall
   state only; the first interactive embedding after startup still paid
   compile/warmup cost, and deployment gates could not tell liveness,
   model-loaded, and inference-ready apart. Tracked as checklist P1.8.

## Standing correction on claim language

Completion reports in this repository must distinguish deployed code,
migrated legacy data, and future-only behavior, and health/readiness claims
must state whether they test process liveness, model load, or real
inference. The implementation-log entries in
[`RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md`](RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md)
follow that convention, including explicit "deploy pending" status for code
that has not been rebuilt into the running image.
