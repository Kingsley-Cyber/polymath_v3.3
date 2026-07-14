# Plan Critique & Finalized Sequenced Plan — 2026-07-13

Auditor: planning agent (original read-only audit; later semantic addenda are
documentation-only updates).
Evidence labels: **V** = VERIFIED (auditor ran the probe today), **L** = LEDGER (from RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md text).
Scope ground rules honored throughout: `polymath_v2` frozen for heavy ops; PoC pair = `markbuildsbrands_transcripts` + `ecommerce_AI_FILM_SCHOOL`; `UGO_CORPUS` = canary. (L)

## 0. Semantic-Relational Decision Addendum — 2026-07-13

Decision of record:
`docs/SEMANTIC_RELATIONAL_ARCHITECTURE_DECISION_2026-07-13.md`, with the final
logical schema, metadata, identity, hash, ownership, and projection contract in
`docs/FINAL_SCHEMA_METADATA_ARCHITECTURE_2026-07-13.md`.

The attachment review does **not** replace this plan's S0–S14 dependency spine.
It preserves S0–S10 and selectively supersedes the future semantic work in
S11–S13: one controlled domain registry, one source-backed claim/assertion
ledger, typed mechanism frames and roles, deterministic motif candidates, and
bounded validated analogy cards now replace broad theme/string/topology
inference as the target bridge method.
Soft themes remain a baseline; existing hierarchy, lexicon, ERE, raw child and
separate summary vectors, protected original-query lane, and all three
retrieval tiers remain intact.

5Ws: deterministic ingestion/validation owns evidence, identity, registries,
and persistence while extraction/graph/librarian/evaluation consume bounded
artifacts (**Who**); add one shared envelope and versioned SourceVersion →
Observation → ClaimAssertion → Sense/Domain/Frame/Role → Motif → Digest/Analogy
layer (**What**) after labeled
fixtures and trustworthy spans but before the PoC mass re-extraction and P3
materialization (**When**), with Mongo authoritative, Neo4j structural, Qdrant
candidate-only, and existing lexicon/tree ownership preserved (**Where**),
because current identities and hashes are fragmented and current triples and
overlap signals cannot defend exact evidence, assertion scope, local sense,
causal roles, invariants, or break conditions (**Why**).

The logical implementation schema and metadata contract are no longer
deferred: P2.5b freezes them before capture. Concrete Pydantic modules, prompts,
Cypher, physical storage names, indexes, adapters, and rollout commands remain
the owner-approved `HOW` phase. This addendum does not rewrite unrelated
historical receipts in this audit document.

## 1. Checklist Status Table (all counts L; box = work + milestone + acceptance)

| Section | [x] | IN CODE / PARTIAL | Open | Open items (short form) |
|---|---|---|---|---|
| Non-negotiable invariants | 0 | — | 15 | Standing continuous gates, not one-time work |
| P0.8 schema enforcement | 3 | — | 1 | Typed-model acceptance at Mongo writer boundary (B0 gap) |
| Temporal hooks/T-MAIN | 1 | — | 3 | T-HOOK-2 summary temporal_class; T-HOOK-3 date de-conflation; T-MAIN phases 2–7 |
| Librarian Phase 0 | 0 | — | 7 | P0.1/P0.6 completion refs; v2 concept/mechanism/readiness reconcile; readiness publication gate; 3 milestones |
| Librarian Phase 1 (card.v0) | 5 | — | 3 | Milestones: schema universal across corpora; cards usable LLM-off; missing-card degrade path |
| Librarian Phase 2 (seat policy) | 2 | — | 8 | Query→ID resolution; Tier-0 candidate gen wiring; **shelf_reserve**; tree→parent→child descent; bridge-seat evidence req; 3 milestones |
| Librarian Phase 3 | 0 | — | 6 | Aliases→versioned data; P1.7; P2.1 materialization; transfer_edge; seat diagnostics; measure before LLM |
| Librarian Phase 4 (LLM amp) | 0 | — | 8 | Field enrichment; planner-when-thin; explain-not-select; P2.2 generated methods; 4 milestones |
| P0.1 summary integrity | 8 | — | 4 | Verify summary fields (child IDs/boundaries/evidence); verify rollups; Funnel-A no byte-identical; 3-tier no-regress. NOTE: 2 boxes still tagged IN PROGRESS are stale — verify PASSES all 4 corpora (V, today: 0 empty-model, 86,880+9,453+1,009+203 all attributed) |
| P0.2 hierarchy repair | 4 | — | 7 | Singleton omit-vs-alias decision; storage/round-trip measure; 5-format fixture validation; 4 acceptance |
| P0.3 corpus floor | 10 | — | 1 | Acceptance: cross-corpus retains evidence from each necessary corpus |
| P0.4 honest answerability | 7 | 1 | 1 | Contradiction checks (PARTIAL @f2fb6e2) |
| P0.5 chunk/metadata hygiene | 3 | — | 7 | **OCR-corrupt heading audit**; doc_name/source-identity backfill; curated aliases→versioned lexicon; facet diagnostics; 3 acceptance |
| P0.6 deletion/orphans | 11 | 1 | 6 | Lease test matrix (PARTIAL: restart-before-expiry, purge-beyond-lease); scheduled reconciliation; 4 acceptance |
| P0.7 doc/claim integrity | 8 | — | 0 | COMPLETE |
| P1.1 held-out eval | 5 | — | 3 | Librarian rubric; automated contamination eval-run check; standing before/after gate |
| P1.2 grounded planner | 2 | — | 9 | Activation block until card.v0+seats+baseline; provider/model+budget; weak-confidence trigger; lane preservation; lexicon-ID rejection; call logging; 3 acceptance |
| P1.3 conversation anchoring | 0 | — | 8 | All (session-vs-profile, inspectable prefs, anchors as priors, follow-up tests; 2 acceptance) |
| P1.4 shelf routing | 0 | — | 8 | All (corpus profile cards, kind-indexed, route-before-books, fan-out fallback; 2 acceptance) |
| P1.5 selection roles | 7 | — | 6 | Universal-capability derivation; gated role-diversity reserve; reading path; 3 acceptance |
| P1.6 answer-shape routing | 0 | — | 7 | All (shape policies, summary scaffolds, hydrate-before-cite, sibling expansion, dynamic K; 2 acceptance) |
| P1.7 vocab batch/cache | 0 | 2 | 8 | Cache + epoch invalidation IN CODE awaiting live verify; gather-before-fanout; vector reuse; per-corpus batching; exact-lookup separation; lexical-only baseline; P2.2 prereq gate; 2 acceptance |
| P1.8 sidecar isolation | 3 | 3 | 0 | Embedder warmup / status split / readiness diag all IN CODE (wave1/warm, pending merge) |
| P1.9 Qdrant hot-path audit | 0 | — | 17 | Per-stage timing; payload selectors; batching; filter-index audit; 29-index audit; profiles; prevent_unoptimized; topology; image pin; priority classes; reranker admission; keepalive; runpod-embed A/B (deployed, promotion-gated L); 4 acceptance |
| P1.9(dup id) adaptive rerank | 0 | — | 7 | All (latency-by-shape, excerpts, budget-by-cliff, cascade eval; 2 acceptance) |
| P2.1 concept contract | 0 | — | 22 | All (contract fields, provenance classes, salience, co-occurrence, usage_frames, semantic_profile, DF/specificity, scoped senses/mappings, separated semantic facets, controlled domain registry/resolver and evidence-bearing hierarchy profiles, card.v0 seed contracts, hub penalty, slim payloads) |
| P2.2 multi-point reps | 0 | — | 29 | All (4 prereqs incl. P1.7; 6 projection; 6 attested-loop; 4 generated; 2 downstream; 7 acceptance) |
| P2.3 query-only instructions | 0 | — | 6 | All |
| P2.4 negation | 0 | — | 5 | All |
| P2.5 typed signatures | 0 | — | 4 | All |
| P2.5a unified claim/frame | 0 | — | 18 | One claim/assertion artifact; exact spans; senses; versioned frame registry; inference status; raw/idempotent provenance; deterministic GLiNER-Relex+spaCy candidate baseline vs provider LLM; bounded parent-digest recipe; annotate-only capture-before-rebuild; 5 acceptance gates |
| P2.5b artifact envelope/identity | 0 | — | 20 | Shared envelope; canonical JSON/hash namespaces; logical doc/source version; hierarchy/observation/summary adapters; immutable revisions; Mongo outbox; projection manifests; 6 acceptance gates |
| P2.6 engine parity | 0 | — | 7 | All |
| P2.7 RunPod validation | 1 | — | 6 | Pinned artifact; canary/100/500/**5,000** gates; parity compare; retry safety; readiness wiring; acceptance |
| P2.7b burst orchestration | 0 | — | 5 | Disposition matrix; chunk-complete barrier; saturating dispatch; full-stack-in-worker; T-HOOK-1-first sequencing (hook landed L) |
| P2.7c multi-account routing | 3 | — | 0 | COMPLETE |
| P2.8 concept→doc grounding | 1 | — | 4 | Core-lane provenance routing; anchor protection; profile+source gates; polluted-card detection |
| P3.1 claim/frame routing pilot | 0 | — | 14 | Labeled cross-domain slice; UGO→PoC; frame/role evaluation; soft themes baseline; protected existing lanes; diagnostics; 4 acceptance gates |
| P3.2 motif/analogy bridge cards | 0 | — | 15 | Versioned fingerprints; bounded candidate compare; role/direction/evidence/invariant/break gates; calibrated thresholds; epistemic separation; capped materialization; 5 acceptance gates |
| P3.3 collection consolidation | 0 | — | 9 | All (migration) |
| P3.4 quantization | 1 | — | 5 | Experiment (clone-first) |
| P3.5 reranker serving | 0 | — | 4 | Experiment |
| Quick upload contract | 6 | — | 2 | Watch-mode decision; inbox/dead-letter states if adopted |
| 3-tier regression matrix | 0 | — | 24 | 16 query rows x record contract (8) — final gate |
| Strict-ready definition | 0 | — | 10 | Per-corpus gates, consumed by readiness split (S6) |

## 2. Verified Live Data State (all V, probed today via backend container, read-only)

| Probe | polymath_v2 | ecommerce | markbuilds | UGO |
|---|---|---|---|---|
| Parents | 130,503 | 10,222 | 1,015 | 203 |
| …with latent_concepts | **639 (0.5%)** | **0** | **0** | **0** |
| Documents | 498 | 79 | 103 | 1 |
| …author / language / doc_date / published_at | 1/0/0/0 | 0/0/1/0 | 0/0/0/0 | 1/0/0/0 |
| heading_path non-empty | 112,175 (86%) | 100% | 100% | 100% |
| page_start present (any value) | **0** | **0** | **0** | **0** |
| Parents w/ facet in {emotional_patterns, agency_preservation} | 92,175 (71%) | 8,281 (81%) | 739 (73%) | 203 (100%) |

- temporal_class on parent_chunks: **0**; on summary_tree: **0**; time_expressions: **0**. T-HOOK-2 unbuilt — code grep confirms the fields exist only in `runpod_flash_extraction.py` (T-HOOK-1 wire capture), never in the Ghost A summary contract. (V)
- latent_concepts[].aliases consumer: **none**. Only `services/librarian/card_builder.py` reads latent_concepts, and it uses concept/evidence_basis/confidence only — aliases are captured (`summary_semantics.py`, clamped to 3) and then read by nothing. (V)
- Legacy broad-alias facet family globally: agency_preservation on **89,088** parents, emotional_patterns on **38,492** — the family the P0.5 decontamination deliberately left stamps 71–100% of every corpus, i.e. DF-worthless as a discriminator. (V)
- P0.1 verify: PASS on all four corpora, exit 0 (`p0_1_summary_integrity.py verify`, host run today). The checklist's two IN PROGRESS tags on P0.1 are stale. (V)
- `shelf_reserve` as a symbol does not exist in backend code (V). *(RESTAMP 2026-07-13, per registry audit: stale — shelf_reserve merged DARK to main @f049041, SHELF_RESERVE_ENABLED=False; A/B still pending at S8.)* Its calibrated substrate (`services/retriever/reservation_policy.py`, P0.3) is merged and live-probed (L).

## 3. Finalized Sequenced Plan (dependency order; each item: what / why-now / riding data pass / anchor)

Deploy-train rule: every step below that changes backend code ships in the next image rebuild + restart; do not hold merged code dark across steps.

| # | Item | What | Why now (unblocks) | Data pass riding it | Anchor |
|---|---|---|---|---|---|
| S0 | Land the dark code | Merge wave1/warm (embedder warmup/status split); live-verify P1.7 vocabulary cache + epoch invalidation; close P0.4 contradiction-check partial or re-scope it | 6 IN CODE boxes are finished work earning nothing; every later latency/eval number is cleaner with warmup + cache real | None (code only) + refreshed latency probe | P1.8, P1.7 cache boxes, P0.4 partial |
| S1 | Summary-contract capture seam — ONE change | Add temporal_class + time_expressions to the Ghost A summary contract (same seam as latent_concepts); confirm latent+aliases persist end-to-end; extend P0.8 validators; typed-model writer boundary rides this same touch | Every summary generation after this carries latent+temporal+aliases for free; it was declared "immediate" and skipped once already (see critique C1) | None yet — contract only; UGO canary generation proves fields land | T-HOOK-2, P0.8 writer boundary |
| S2 | T-HOOK-3 bibliographic/date de-conflation | Docling publication-vs-file-date de-conflation; source_published_at; author/title/language front-matter capture; deterministic doc-level backfill (pair + UGO full; v2 documents-only — 498 rows, no parent rewrites, stays inside the freeze) | MUST precede the next lexicon/card rebuild (constraint); temporal eligibility and librarian metadata-quality readiness need these fields; verified state is 0–1 docs populated anywhere | Deterministic doc-metadata backfill (cheap, no LLM) | T-HOOK-3 = P2.1 bibliographic item |
| S3 | Heading/OCR quality audit → disposition matrix sign-off | Audit heading_path TEXT quality on ecom (presence is 100% — corruption, not absence, is the question); define source-safe repair rule; record that page_start is absent everywhere and only a reingest lane can ever add it; owner signs the per-corpus reingest / re-extract-only / projection-only matrix | An ecom reingest rebuilds chunk IDs — summaries/vectors/extractions follow. The decision must precede S4's paid pass or that pass is destroyed and re-bought | Read-only sampling; produces the P2.7b disposition matrix artifact | P0.5 OCR audit, P2.7b matrix |
| S4 | PoC-pair capture backfill — ONE paid pass | Single summary regen/backfill over mark (1,015) + ecom (10,222 — or riding its reingest if S3 flags it) writing latent_concepts+aliases+temporal_class+time_expressions together; UGO first; v2 gets ONLY the deterministic temporal classifier backfill (T-MAIN Phase 3), never paid regen | Pair currently has ZERO latent coverage (V); rollup, cards, shelf A/B, and P2.2 admission all starve without it | THE consolidated pass — nothing summary-shaped reruns on the pair after this | T-HOOK-2 backfill clause, P2.2 gains (LATENT_CONCEPT_PROMPT), P0.1 provenance discipline |
| S5 | Alias/latent rollup (deterministic Python) | parent latent_concepts → doc profile concepts → corpus_lexicon/vocabulary join → versioned alias registry → gated query-expansion consumption (original-query lane protected); fold P0.5 curated-aliases-out-of-Python into the same registry; fix the positional 6-match planner cap with obligation-aware selection | Converts S4's capture into retrieval value; kills the aliases-read-by-nobody dead end (V); feeds P2.1 salience/DF fields | Registry build + lexicon rematerialization (epoch bump exercises S0's cache invalidation) | P2.1, P0.5 alias migration, P1.2/P1.5 cap-policy gain |
| S6 | Readiness split — own item | Separate operational readiness (artifacts/projections reconcile) vs metadata-quality (bibliographic, facet precision, card coverage) vs temporal capability (temporal_unavailable/partial/strict_ready); `fully_enriched` stops implying librarian-grade metadata | After S2/S4 there is real metadata to grade; gates honest interpretation of S8's A/B and T-MAIN enforcement | Readiness recompute per corpus | P2.1 readiness gain, T-MAIN capability seam, strict-ready defn |
| S7 | DF-facet rule for the legacy broad-alias family — OWNER DECISION | Propose: any facet with per-corpus parent-DF above threshold loses retrieval/expansion rights (evidence retained); apply to {agency_preservation 89,088; emotional_patterns 38,492} (V); cleanup backfill batched with S5's reindex so payloads rewrite once | Family stamps 71–100% of every corpus — it can only add noise to facet-aware ranking; decision is pending and blocks calling P0.5 acceptance honest | Facet payload cleanup riding the S5 rematerialization | P0.5 adopted gain (left family), P2.1 specificity gates |
| S8 | Pair card rebuild FINAL + shelf_reserve A/B | Rebuild librarian cards for the pair (now with latent+bibliographic+clean facets); wire shelf_reserve through P0.3's calibrated reservation_policy (never the old unconditional floor); A/B on the held-out 56 vs the 2026-07-13 baseline | Cards rebuilt exactly once after ALL capture (constraint); A/B on pre-capture cards would measure the wrong artifact | Card rebuild (Mongo + slim Tier-0 projection this time) | Phase 2 shelf_reserve, P1.5 remainder, P1.1 gate |
| S9 | P1.3 anchoring + P1.6 answer-shape routing | Conversation/open-book anchors as priors; shape-keyed breadth/K and sibling expansion | Consume the substrate; both are held-out-gated behaviors, cheapest after S8's eval harness is warm | None | P1.3, P1.6 |
| S10 | P2.2 gated multi-point consumption | Prereqs now real: P1.1 baseline [x], P1.7 batching+lexical-only baseline (S0/S5), admission fields (S5); pilot on mark with per-concept point caps; contamination eval-run check lands here with the first representation store | The lay-language recall payoff; illegal earlier by its own prerequisites | Representation-point projection (pilot corpus only) | P2.2, P1.1 contamination box |
| S11 | Semantic contract freeze + extraction/domain/claim/frame capture: P2.1, P2.4→P2.6 (including P2.5a/P2.5b), quality-gated P2.7 scale + P2.7b burst, P2.8 | First freeze the shared artifact envelope, canonical hash namespaces, source-version and hierarchy identity, provider-neutral observation bundle, registry snapshots, legacy adapters, Mongo outbox, and projection manifests. Then keep negation and typed signatures annotate-only; add the controlled domain resolver, unified claim/assertion capture contract, exact-span verifier, scoped-sense refs, frame candidates, inference status, raw/idempotent provenance, and one shared validator. The 2026-07-13 feasibility result makes a pinned trained spaCy parser the scoped claim/qualifier compiler, permits GLiNER as optional span candidates, and keeps current GLiREL/joint Relex relations observation-only; DeepSeek/LongCat remain identical-label baselines or bounded refiners. Test the parent semantic digest only after accepted child artifacts and as a separate recipe from Ghost A. Resume RunPod 100/500/5,000 scaling only after an expanded unique quality fixture clears; P2.8 grounding follows | The identity/schema front gate prevents the one paid re-extraction from producing semantically useful but mutually incompatible artifacts. Bursting first would make the pair back-level. The current joint RunPod model failed the 9-sample relation gate at both 0.75 and 0.40 thresholds, current RunPod defaults to blank spaCy, and Ghost A does not consume accepted child claims. Scaling or treating the draft as implemented now would repeat C1/C3 | The mass re-extraction pass (pair only; v2 frozen), after P2.5b, expanded semantic-quality gates, and UGO capture/parity/parent-recipe canaries | P2.1, P2.4–P2.8, especially P2.5a/P2.5b; P2.7b; `SEMANTIC_EXTRACTION_PRODUCTION_READINESS_2026-07-13.md` |
| S12 | Unified T-MAIN claim/assertion phases 2–7 | Source versions/evidence episodes plus ONE authoritative temporal+general `ClaimAssertion` ledger; projection via the P2.5b outbox/manifests without blind re-embed; asserted structure projected separately from synthesized/analogy artifacts; legacy/versioned edge migration co-scheduled with P2.5/P2.5a/P2.5b; temporal query modes, one eligibility service, shadow-then-enforce | S11 supplies exact, versioned semantic capture and S1/S2/S4 supply temporal metadata; unifying here prevents two claim stores and keeps one migration per seam | Neo4j assertion-keyed compatibility projection + Qdrant payload/index shadow; full artifact stays Mongo-authoritative | T-MAIN, P2.5, P2.5a, P2.5b |
| S13 | Semantic-relational P3 program | P3.1 claim/mechanism-frame routing pilot (UGO→mark/ecommerce), P3.2 deterministic motif/analogy cards; compare soft themes and existing bridges as baselines; P3.3 consolidation, P3.4 quantization, and any contextual-vector recipe remain separate clone-only experiments | Frame/motif promotion depends on labeled fixtures, exact spans, unified assertions, and S11/S12 parity; experiment isolation prevents Qwen/context/quantization changes from confounding semantic conclusions | Canary/pilot corpora and cloned collections only; no production-wide reindex | P3.1–P3.5 |
| S14 | Full regression matrix + semantic quality gates + image rebuild + final report | All 16 rows x 3 tiers with full record contract plus claim precision/span recall, frame/role F1 and abstention, analogy false-positive/harmful-analogy controls, canonical-hash replay, Mongo/projection reconciliation, cost/write amplification; rebuild+deploy only promoted slices; baselines re-captured; report deployed/migrated/future-only | The completion gate must prove better cross-domain evidence without direct-query, corpus-isolation, latency, provenance, identity, or replay regression | Final probes and scorer-versioned semantic eval artifact | Regression matrix, strict-ready, P2.5a/P2.5b/P3 acceptance |

Parallelizable: S2+S3 (independent of S1); S6 alongside S5; P0.2/P0.6 test-matrix remainders can fill any gap — they touch nothing above.

## 4. Critique — where sequencing broke, what it cost, what rule prevents it

**C1. Rebuild-before-capture on the very rows the plan cares about (the core violation).**
T-HOOK-2 was adopted 2026-07-13 and labeled "immediate" (L). The same day, the P0.1 program regenerated 2,633 quarantined v2 summaries through the paid Ghost A path and drove the pair to 100% verified coverage (L, V). Verified outcome: only 639 v2 parents carry latent_concepts (≤24% of even the regenerated rows), the pair carries **zero**, and **zero** rows anywhere carry temporal_class. The capture seam was hot — the exact contract file was being exercised at scale — and the hook wasn't built first. Cost: the pair now needs a dedicated paid regen pass (S4) that could have ridden P0.1, and ~2,000 paid v2 regenerations produced rows that are already back-level against the adopted contract.

**C2. Card build before its feed existed.**
673 librarian cards were built 2026-07-13 (L). `card_builder.py` consumes `latent_concepts` as a card seed (V) — and its input was empty on 100% of pair parents (V). The pair's cards are structurally missing a field family they were designed to carry, plus all bibliographic fields (V: author/language/date ≈ 0). Cost: full pair card rebuild (S8) — tolerable only because cards are cheap deterministic projections; the pattern is not tolerable for paid artifacts.

**C3. Capture without consumers; code without deployment.**
latent aliases are parsed, clamped, validated — and read by nothing (V). The planner's positional 6-match cap that demonstrably dropped a 0.909 concept is still open (L). *(RESTAMP 2026-07-13, per registry audit: the cap fix is IN CODE under other names — strength override vocabulary.py:1705 + obligation-aware selection grounded_planner.py:375 — S5 verifies rather than builds it.)* Meanwhile 6 boxes sit IN CODE unmerged (P1.7 cache, P1.8 warm) while newer feature work landed. The system is accumulating dark data and dark code at both ends of the pipeline; each is a silent invalidation risk for every measurement taken in between.

**C4. Ledger hygiene drift.**
P0.1's two IN PROGRESS tags contradict its own verified acceptance lines and today's green verify (V). Section id "P1.9" is used twice. The P0.5 facet decontamination is recorded as merged/complete-shaped while leaving a facet family stamped on 71–100% of every corpus (V) with no recorded owner decision. None fatal; all erode the header's contract that a checkbox means deployed + reconciled + verified.

**Standing rules (proposed for adoption at the top of the checklist):**
1. **Capture-before-rebuild:** no summary/lexicon/card rebuild may run while an ADOPTED capture hook targeting the same rows is unbuilt. A rebuild must list open capture hooks for its seam and obtain an explicit owner waiver, else the hook lands first.
2. **No dark fields:** every captured field ships with its first consumer, or a named consumer + checklist anchor in the same phase; otherwise the ledger marks it dark data.
3. **Receipts refresh tags:** any status tag (IN PROGRESS/PARTIAL) contradicting a later verification is stale by definition; ledger edits re-run the relevant verify and restamp the date.
4. **One migration per seam:** co-schedule migrations that touch the same artifact (P2.5 + T-MAIN edges; S5 + S7 payloads) — already the plan's shape; make it a rule.
