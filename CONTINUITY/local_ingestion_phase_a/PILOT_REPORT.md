# Cross-domain pilot report — 15 files, full local stack (2026-06-10)

**Corpus** `137550d5` (pilot_cross_domain_15), 128-tok children, sidecar extraction.
**Files**: 3.84 MB across scraped tech docs, software/engineering books, UX, business,
psychology, game theory, and a 932 KB alchemy book as the out-of-distribution canary.
**Outcome: 15/15 ingested + verified** (one initial failure → two real bugs found+fixed;
see Resilience). **Verdict: GO for backfill** once the speed option is chosen.

## Quality

### Per-doc typed relation share (floor check: ~50% = pure-BERT floor, 100% = junk)
| share | docs |
|---|---|
| 88–97% | Stable Diffusion (88), Serious Games (93), executorch (97), flutter_ai (100, n=1) |
| 73–82% | Emerald Tablet/alchemy (75 — the OOD canary held), TDD (75), Kent Beck, Microcopy (77), Postgres (82), Designing Interfaces (73), flame (75) |
| 46–60% | Mom Test (46), Elegant Puzzle (52), Hidden Games (55), Projective Techniques (60) |

No junk signature (no doc near 100% with entity spikes). The weak tail is
conversational business prose — legitimately `related_to`-heavy narration, not a
defect. Someday-fix: GLiREL v3 fine-tune with business-prose examples (RTX recipe).

### Facts: 8,826 staged → 5,289 graph nodes (deduped), **100% entity-linked**
rule_condition 2366, property 1700 (**1691 = table-sourced — 19% of all facts**),
rule_action 1398, timestamp 403, status 371, category 290, threshold 221, quantity 77.

### Entities: 31,737 occurrences; 13% faceted, 8.7% with definitional_phrase
(Books are people/concept-heavy; the facet-eligibility filter correctly skips most.)
**Junk finding**: pronouns were the top "entities" (`you` ×715, `we`, `they`, `he`) —
fixed post-pilot with a pronoun blocklist (pipeline_config). Pilot numbers above are
PRE-fix; backfill numbers will be cleaner.

### Retrieval probes: 4/4 direct hits at #1
alchemy → Emerald Tablet intro; TDD → "write your tests before the code"; game theory
→ costly signaling passage; Postgres → table/psql chunk.

## Throughput (the 2-day question)

| config | rate | 338 MB backfill |
|---|---|---|
| pilot as-run (per-chunk calls) | ~14 min/MB blended; 557 ms/chunk on books | ~4–5 days |
| + batched inference (all 3 stages) | books barely moved — GLiREL is compute-bound | ~4 days |
| + facet eligibility + 400-char contexts + doc-sized slices | **420 ms/chunk books (654 → 420)** | **~3 days** |
| fp16 GLiREL | **rejected: 0.99×** on MPS (partial CPU fallback in DeBERTa attention; CPU-vs-MPS probe showed only 2.1× GPU benefit) | — |

Mac-only floor ≈ 3 days. Options to go under 2: **(D) RTX sidecar** — point
`LOCAL_GHOST_B_EXTRACT_URL` at the CUDA box running the same sidecar (<1 day,
zero quality trade, ~1–2 h setup; weights+CUDA already there from fine-tuning) —
recommended; (A) tiered backfill, docs-first usable in ~1 day; (B) cue-gated GLiREL
on books only (~1.8 days, thinner book graphs); (C) flat ~3 days.

## Resilience findings (must-fix before unattended multi-day run)

1. **FIXED — whole-doc sidecar requests**: a 932 KB book (~1800 chunks) blew the 600 s
   request ceiling → doc-sized slices (2048) + 1800 s/slice.
2. **OPEN — embed partial-failures are swallowed**: the embedder intermittently 400s/
   stalls (also seen by backend health checks); one chunk of 1726 silently missed all
   three Qdrant collections + Neo4j. Verify catches it (the doc fails) but resume
   trusts staging and never re-embeds the hole. Needed: retry-on-failure in the embed
   stage + a reconcile that diffs staged chunk_ids vs the collection and back-fills.
   (The pilot hole was hand-repaired: embed + PUT upsert ×3 + graph MERGE; the
   verifier's text-contract check — sha1 + text_len + is_truncated — validated it.)
3. **Noted**: GPU contention while extraction holds Metal can starve the embedder;
   shorter extraction (optimizations above) and the RTX option both reduce exposure.

## Memory (32 GB Mac Studio, hosting-first)

Measured under load: sidecar 2.9 GB + embedder ~1.5 GB + Docker stack ~8.6 GB + macOS
~3.5 GB ≈ 17 GB; peak with doubled batches ≈ 20–22 GB. The dedicated 25–27 GB envelope
(via `scripts/ingest_reclaim_memory.sh --apply --require-gb 22`) covers it with margin.
Doubled batch envs: `GHOST_B_GLINER_BATCH=64 GHOST_B_GLIREL_BATCH=128 GHOST_B_FACET_BATCH=64`.
