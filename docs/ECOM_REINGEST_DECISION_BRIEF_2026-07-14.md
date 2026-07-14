# Ecom Reingest — Owner Decision Brief (2026-07-14, corrected evidence)

One-page §8 decision package. Everything below is receipt-backed; no estimates
without a basis stated.

## What changed since the original S3 matrix

The S3 evidence said ecom's heading corruption was "source-baked — reingest
buys nothing." CP1 disproved half of that: the corruption was substantially a
PIPELINE artifact — digital PDFs bypassed structure parsing entirely
(`_parse_pdf_fast_text`), and the chunker merged small sections across heading
paths. Both are now fixed and CP1-certified on real PDFs through the deployed
stack (all g1–g10 green):

| Capability (proven at CP1) | Old lane | Fixed lane |
|---|---|---|
| PDF heading_path structure | flat body parents, 0 headings | tier_a parents, ALL headings, coalesce within-path only |
| Title-page bibliography (Author/Published) | lost | captured w/ typed provenance (`text_head_published`) |
| Qualified temporal phrases | bare years | complete surfaces w/ offsets |
| Honest nulls / provenance | partial | reason-coded, per-lane parity |

## What a reingest now recovers for ecom (79 docs, 10,222 parents, 56,996 children)

- Real section heading_paths replacing the 45.2% page-slug headings on the
  61 CLEAN + 6 SUSPECT docs (S3 doc lists) — the parent routing surface the
  whole librarian design depends on.
- Text-head bibliographic candidates (authors/dates) where title pages carry
  them — ecom currently has 17 authors / 27 dates of 79 docs.
- Finer, heading-faithful parents (coalesce fix) → better summary packets.
- Qualified temporal captures at extraction (rides the same one paid pass).

## What a reingest does NOT fix (unchanged from S3)

- 3 empty/under-ingested books (Group A) — need upstream re-OCR/re-conversion
  of sources first; excluded from this decision.
- 4 scanned page-slug books (Group B) — only better sources help; excluded.
- 3 pipeline-report junk docs (Group D) — deletion is a separate owner line.

## Hard sequencing constraints (already in BUILDLINE)

1. **CP6's batched Neo4j purge MUST land first** — deleting ecom's old graph
   for reingest would OOM today (purge died at 716MiB on a 76-chunk corpus;
   ecom has ~913× that entity volume). This is the CP9 hard prereq.
2. Reingest = new chunk IDs → summaries/extractions/lexicon/cards all rebuild
   — which is WHY ecom was pulled from CP2: pay for enrichment ONCE, after
   reingest, never twice.

## Cost of the one-pass path (basis stated)

- Parse+chunk: local CPU, minutes-scale (docling/pypdf lanes, no OCR).
- Embed ~57k children + summary parents: local GPU at ~37 chunks/s ≈ ~30 min.
- Summaries ~10k parents: deepseek-flash, durable done-means-done jobs,
  bounded batches with drop counters (same machinery CP2 proves on mark).
- Extraction ~57k children: upgraded RunPod fleet, ~25–40 min burst at gate
  throughput; scale-to-zero after.

## The decision lines (reply with any subset)

1. **"ecom reingest approved"** — the 67 clean+suspect docs go through the
   fixed lane after CP6's purge fix, then ONE enrichment pass (summaries +
   extraction + lexicon + cards). Scheduled at CP9.
2. **"junk deletion approved"** — the 3 Group-D report docs are deleted
   (backup-first) during the same window. OWNER-ONLY, never glides.
3. Group A/B source re-acquisition — no action needed now; parked until you
   supply re-OCR'd sources.

Recommendation: approve 1 and 2 together; leave 3 parked. Nothing here blocks
CP2–CP8; this decision gates only the CP9 ecom lane.
