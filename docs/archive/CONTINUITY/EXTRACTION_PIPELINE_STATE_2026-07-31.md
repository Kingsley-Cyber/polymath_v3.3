# EXTRACTION PIPELINE — STATE OF RECORD, 2026-07-31

Measured, not remembered. Every number below came from a live query against the
running stack on the date in the title.

---

## 1. THE INTENT (unchanged, `00_LOCKED_DECISIONS.md`)

> Ghost B (entity/relation/fact extraction) — **fully local, deterministic, no
> SLM, no LLM in the path.**
> Ghost A (summaries) — **stays cloud. Do not touch.**

Four organs. A language model may never write to the graph, because a
hallucinated edge is permanent.

| # | Organ | Engine | Emits |
|---|---|---|---|
| 1 | Entities | GLiNER pass-1 | 14 types |
| 2 | Facets (`object_kind`) | GLiNER pass-2 | refines Software → vector_database |
| 3 | Relations | **frame-licensed spaCy** (was GLiREL) | 30 predicates |
| 4 | Facts | `enrich.py` Stage D rules | 9 fact types |

---

## 2. WHAT IS ACTUALLY IN THE STORE (362,759 chunks)

| organ | total | per chunk | state |
|---|---|---|---|
| entities | 2,822,023 | 7.779 | healthy |
| **facets** (`object_kind`) | 12,639 chunks | **3.5% coverage** | **BROKEN — organ 2 barely runs** |
| relations | 17,194 | 0.0474 | repaired 2026-07-30 (was 0.0000 on 98.6%) |
| facts | 314,616 | 0.8673 | repaired 2026-07-31 (was 621 total) |
| claims | 3,071,361 | 8.467 | healthy |

---

## 3. THE ROOT CAUSE, AND HOW MANY ORGANS IT TOOK OUT

`runpod_local_extraction._compile_result` returned hardcoded empties. It is the
lane that extracted **six of seven corpora** — 362,142 of 362,759 chunks.

| organ | pod lane behavior | status |
|---|---|---|
| 1 entities | returned properly | never broken |
| 2 facets | **never sets `object_kind`** — GLiNER pass-2 is not in this lane at all | **STILL BROKEN** |
| 3 relations | `relations=[]` hardcoded | **FIXED** (forward + backfill + promoted) |
| 4 facts | `facts=[]` hardcoded | **FIXED** (forward + backfill) |

Three of four organs were degraded on the lane that did nearly all the work.
Two are repaired. **Facets remain broken and are the largest open gap.**

Evidence facets are lane-specific, not universal: the one locally-extracted
corpus (`cpcs_local`) reaches 8.4% facet coverage; the pod-extracted
`ecommerce_meta` reaches 1.5%.

---

## 4. RELATIONS — THE REBUILD

The lane was rebuilt this cycle, not merely repaired.

**Old model:** iterate all N² entity pairs, take the shortest dependency path,
ask a resolver to name it. Inside one sentence a path always exists, so
co-present entities became edges (`INSIDE instance_of Earth` from a chapter
heading). Hand-judged precision **0.10–0.15**, against a gate reporting 1.000.

**New model** (`services/extraction/frame_extractor.py`): find predicate frames
first (active transitive, copular, passive+agent, prepositional object,
possessive, appositive), then fill argument slots. Both slots or nothing.
Co-presence is not a frame, so those pairs never form.

**Gate:** `backend/evals/spacy_relation_gate_v2.json`, preregistered before
scoring, four rounds:

| round | precision | Wilson 95% |
|---|---|---|
| 1 | 0.6370 | [0.553, 0.713] |
| 2 | 0.7259 | [0.645, 0.794] |
| 3 | 0.7556 | [0.677, 0.820] |
| **4** | **0.8015** | **[0.725, 0.861] — PASS** |

asr 0.822 · book 0.778 · paper 0.805. n=135, BORDERLINE counted as WRONG.
**0.80 was reached with grammar and ontology only — the no-LLM lock held.**

Known limitation, recorded in the gate spec: the judge is the same agent that
wrote the extractor. All 131 per-item judgements persist with reasons in
`docs/baselines/GATE_V2_JUDGEMENTS_2026-07-30.jsonl` for independent re-judging.

---

## 5. THE GRAPH

| | before | after |
|---|---|---|
| RELATES_TO total | 898,096 | **14,058** |
| edges belonging to a LIVE corpus | ~1,585 | **~14,050** |
| dead-corpus pollution reachable from live entities | **94.9%** | **0.1%** |

Two findings drove this:

1. **Corpus deletion never cleaned up Neo4j.** 884,023 edges pointed at corpora
   with zero content in every Mongo collection. Purged, with a full
   884,023-row export written first
   (`/data/ingest-files/graph-purge-receipts/orphan_edges_2026-07-31.jsonl.gz`,
   86 MB gz) because Neo4j deletes are irreversible.
2. **Two hot retrieval paths do not filter edges by corpus** —
   `graph_decoration.py:508` and `graph_rerank.py:84`. Entity nodes are global,
   so dead-corpus edges were decorating live winners and inflating rerank
   degree. That made the purge a correctness fix, not housekeeping.

**End-to-end verified:** the production `graph_decoration` Cypher, run over 25
real winner chunks, returned 25 decorations — **25/25 from this promotion**.

---

## 6. OPEN GAPS, RANKED

1. **Facets (organ 2) — 3.5% coverage.** GLiNER pass-2 never runs on the pod
   lane. Largest remaining hole. Same fix shape as relations/facts: the lane
   already has the entities; the facet pass is a second GLiNER call, so unlike
   relations and facts it is NOT free — it needs a model pass.
2. **Cross-corpus leak, 35.1%.** Post-purge, edges reachable from one corpus's
   entities still include other LIVE corpora, because neither hot path filters
   on `r.corpus_ids`. May be INTENDED (cross-corpus bridges are an explicit
   feature elsewhere) — needs an owner ruling. The clearer defect is
   `graph_rerank` counting degree across corpora, which inflates scores.
3. **Facts are ungated.** No precision fixture; eyeball quality ~60–65% on the
   canary, below the 0.80 relations bar. A `fact_gate_v1` is the obvious
   next instrument.
4. **Table facts never run on the pod lane** — `extract_table_facts` needs
   `chunk_kind == "table"` plus the column list, which the lane does not carry.
5. **801 relations (4.9%) could not be promoted** — `_upsert_relation` MATCHes
   rather than MERGEs Entity nodes, so an edge needs both endpoints to exist.
6. **22 RELATES_TO edges carry no `corpus_ids`** — deliberately not purged,
   since orphan status is unprovable for them.
7. **Recall rungs R1–R7 remain unbuilt** (see §7).

---

## 7. THE RECALL LADDER — DEFERRED, AND NOW SAFE TO ATTEMPT

R1 (T4 lemma mining), R2 (POS overrides), R3 (coordination distribution),
R4/R5 (nominal lane, rank-then-cap), R6 (cross-sentence carry-over),
R7 (ASR re-segmentation) were all **deliberately deferred** when precision
measured 0.10–0.15: every one adds yield, and yield × bad precision makes the
graph worse.

**That blocker is now gone.** A working gate exists, so each rung can be
attempted and measured rather than guessed at. Current yield is 0.0474
relations/chunk — deliberately low. R3 in particular has a MEASURED ceiling:
`suppressed_conjunct_crossing` fires 1.49 times per chunk.

**R9 is withdrawn permanently:** the multi-clause guard it targeted is obsolete
under the frame model, because a frame is one clause by construction.

---

## 8. WHAT CHANGED IN THE CONTRACT

- `ExtractionResult.extraction_counters` — full per-chunk suppression map now
  reaches Mongo. Previously 4 of 14+ counters were published and the rest were
  garbage-collected at the emit boundary, so nobody could measure what the
  guards cost.
- `GHOST_B_PAIRING_MODEL` — `frame` (default) | `deppath` (legacy, A/B only).
- `GHOST_B_RELATION_ENGINE` — now raises loudly instead of silently ignoring a
  non-spacy value. There is no runtime rollback to GLiREL; reverting means
  reverting the commit.
- `./config:/app/config:ro` mounted into backend + ingest-worker. The build
  context is `./backend`, so repo-root `config/` was never in the image and the
  deterministic lane could not run in-container at all.
- Provenance stamps: `relation_backfill.version`, `fact_backfill.version`,
  `r.promote_version` — every written row is attributable and reversible.
