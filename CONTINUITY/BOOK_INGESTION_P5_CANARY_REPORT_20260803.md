# P5 Canary Report — Relex-Only Book Ingestion Qualification

Date: 2026-08-03 (renamed from 20260727 per owner correction; execution
timestamps in container logs read 2026-08-03/04) · Scope: **canary only** (owner directive: no full-corpus
reingestion, legacy estate dry-run only, no generation switch, no historical
row deletion). Machine-readable matrix:
`data_eval/relex_canary_acceptance_matrix.json`.

## Milestone status

```yaml
relex_canary: passed
deterministic_summary_canary: passed
route_readiness_canary: passed
restart_canary: passed
rollback_canary: passed
full_corpus_reingestion: not_started
```

## Test corpus

4 fixtures in `backend/tests/fixtures/canary_book/`: Benesh 1975 article
(OCR-corrupted academic prose), TikTok Shop transcript (frontmatter +
relative times), mixed Python book (prose/code/output/table/caption),
Aldermoor DOCX (binary lane). Canary corpus
`b596c89e-ede6-4cd3-9896-0ecc04602992`: 111 chunks / 36 parents / 4 docs,
queryable in 45s.

## Results by stage

- **Extraction**: 111/111 rows stamped `relex_local`, zero gliner/glirel/
  fallback calls, no provider pool, 4 distinct per-doc contract hashes,
  sidecar model hash `7c5bd751…` verified. Evidence:
  `data_eval/relex_canary_extraction_lane.json`.
- **Summaries**: 36/36 jobs succeeded provider-free
  (`deterministic_summary.v1`, provider_calls 0). Code/output/caption
  parents keep verbatim text by design. Evidence:
  `data_eval/relex_canary_summary_materialization.json`.
- **Storage**: Mongo complete; Qdrant naive=143 / hrag=143 / graph=111 /
  schemas=864 points; Neo4j = structural projection only (111 chunks,
  4 docs, 2110 MENTIONS), **zero canonical fact writes**.
- **Readiness**: vector + hierarchical routes allowed, graph routes
  fail-closed by release registry (`release_pin_match` missing). Evidence:
  `data_eval/relex_canary_route_readiness_certificate.json`.
- **Retrieval**: all 5 matrix items pass (exact_document_ids,
  exact_chunk_ids, prose, temporal, mixed prose+code).
- **Restart**: deliberate `docker compose restart backend ingest-worker mcp`;
  post-restart battery: 6/6 identical doc sets. Evidence:
  `data_eval/relex_canary_restart_parity.json`.
- **Legacy gate (live)**: 6 retired engine values rejected 422 on create,
  3 on update; contract resolver fails fast on unknown persisted engines;
  runtime invariant suite 11/11. Evidence:
  `data_eval/relex_canary_legacy_gate.json`.
- **Estate dry-run**: 0 legacy engine values in the estate
  (cloud:1 / runpod_flash:16 / relex_local:12) → migrated_total 0.
  Evidence: `data_eval/estate_migration_dryrun.json`.
- **Rollback**: DELETE corpus → all 4 Qdrant collections dropped, Neo4j
  projection purged to zero, Mongo rows tombstoned (`status: deleted`,
  no hard delete), corpus hidden from API list, estate census byte-identical
  (29 corpora, zero count diffs). Tombstones survived a Docker Desktop
  crash. Re-ingest from the immutable fixtures → deterministic doc IDs
  identical across corpora; expected doc present in 6/6 queries (tail/
  source-count variance only). Evidence: `data_eval/relex_canary_rollback_*.json`.

## Caveats / flags for owner

1. **Answer generation is environment-blocked, not pipeline-blocked**:
   upstream providers are broken (DeepSeek 402 Insufficient Balance; Longcat
   401 invalid_api_key '无效的AppId: sk'). Retrieval verdicts unaffected;
   real answers appeared for 3/6 queries after stack recovery, confirming
   intermittency at the provider layer.
2. **Ingest-inline Neo4j structural write path** (worker.py ~4787) sits
   outside the `authorize_canonical_graph_write` seam. It writes only the
   MENTIONS projection (so `neo4j_canonical_writes: 0` holds), but the
   owner should decide whether inline structural writes should also be
   release-gated.
3. `extraction_engine` is a **MUTABLE** corpus field by design (enables
   Relex re-extraction); the frozen-field guard does not cover it. Legacy
   values are still rejected at the validation boundary.
4. Rollback re-ingest corpus `c6518e7b-1327-4694-85c8-08a81542e425`
   retained as proof artifact; keep or delete at owner's discretion.
5. Docker Desktop quit 4× during the session; every crash recovered
   cleanly (`open -a Docker` → staged `compose up -d` after Qdrant warmup).

## PAUSED — awaiting owner approval

Next actions require owner decision: (a) accept canary results;
(b) whether to run extraction+summary lanes on the v2 corpus; (c) whether to
gate inline structural Neo4j writes; (d) LLM provider billing/key repair;
(e) authorization and sequencing for any estate-wide migration or
full-corpus reingestion (both remain prohibited until then).

## Owner closeout (2026-08-03)

**P5 canary ACCEPTED.** Closeout state:

```yaml
p5_canary: passed
core_deterministic_book_pipeline: passed
full_corpus_reingestion: not_authorized
estate_migration: dry_run_only
canonical_graph_promotion: blocked_pending_qualification
answer_synthesis_environment: needs_repair
neo4j_structural_authority_separation: required_follow_up
qdrant_consolidation: pending_canary
```

Owner decisions on the four findings:

1. **Answer providers**: treat DeepSeek 402 / Longcat 401 as an
   answer-synthesis configuration failure, not an ingestion failure. The
   deterministic path (ingest → Relex → deterministic summaries → retrieval
   → evidence packet) must never require an external LLM. Chat must return
   a structured `answer_provider_unavailable` with `retrieval_succeeded:
   true` — never present a provider failure as "no evidence found".
2. **Engine mutability**: lock `extraction_engine` on any non-empty corpus;
   API rejects ordinary updates with `extraction_engine_locked` /
   `required_action: create_reextraction_generation`. Controlled
   re-extraction only via new generation or audited, rollback-capable
   migration.
3. **Structural graph authority**: structural/shadow graph (Chunk nodes,
   document structure, EXPLAINS, candidate MENTIONS, code/table relations)
   is allowed pre-qualification ONLY if every node/edge carries corpus_id,
   document_id, extractor_release, projection_kind=structural_shadow,
   authority=noncanonical, release_stamp, evidence chunk id — or is moved
   to a distinct `CANDIDATE_MENTIONS` relationship type. Canonical factual
   routes must never be satisfied by structural shadow. Fix before closing
   graph-control integration.
4. **Fixture hygiene**: keep ONE permanent 4-document canary corpus
   (`purpose: permanent_integration_fixture`, `production_visible: false`,
   `excluded_from_user_search: true`) plus immutable fixtures, artifact
   manifest, and retrieval gold set; delete temporary rollback copies but
   preserve JSON reports.

Next bounded sequence (owner-ordered):

```text
1. Lock extraction_engine on non-empty corpora.
2. Separate structural-shadow Neo4j projections from canonical evidence.
3. Repair answer-provider configuration.
4. Permanent fixture + cleanup of temp copies.
5. Qdrant single-evidence-collection canary (one point per child with
   record_kind + route-eligibility flags; pilot only, no global switch).
6. 10-file pressure test before 100/1,000-file rollout.
```

Slice ordering rule: the next engineering slice is the Qdrant
one-point-per-child canary UNLESS current MENTIONS edges lack noncanonical
labeling — in that case graph-authority separation comes first.
## Execution record — bounded items 1–4 closed (2026-08-04)

Slice-ordering rule applied: DebugAgent diagnosis proved the estate's
structural graph artifacts were INDISTINGUISHABLE from canonical evidence
(3,663,758 MENTIONS / 309,794 ghost_b Facts / 3,135 shadow RELATES_TO
unlabeled; fact-seed read `coalesce(knowledge_status,'accepted')` made
shadow facts OUTRANK release-gated claim candidates). Graph-authority
separation therefore went first.

### 2. Structural-shadow authority separation — DONE
- Write side: `neo4j_writer.py` stamps `projection_kind=structural_shadow`,
  `authority=noncanonical` on MENTIONS (batch + legacy), EXPLAINS, CALLS;
  RELATES_TO/Fact stamps are coalesce-guarded so claim-promoted edges are
  never downgraded. `promote.py` sets `claim_promoted`/`canonical_candidate`
  unconditionally (upgrade wins).
- Read side: `fact_retrieval.py` fails closed
  (`coalesce(knowledge_status,'candidate')` — 4 sites);
  `graph_decoration.py` RELATES_TO decoration requires claim_ids or
  non-shadow authority.
- Backfill: `backend/scripts/label_structural_shadow_projections.py`
  applied to the live estate; verify dry-run shows 0 unlabeled artifacts in
  every class (792 claim edges uplifted to canonical_candidate).
- Tests: `tests/test_graph_authority_separation.py` (11).

### 1. extraction_engine lock — DONE
- `ExtractionEngineLockedError` + `update_corpus` guard (non-empty corpora
  only; same-value no-op stays 200); router returns 409 with exactly
  `{"error": "extraction_engine_locked",
    "required_action": "create_reextraction_generation", ...}`.
- Live-verified against the fixture corpus (409 on switch, 200 on no-op).

### 3. Answer-provider structured failure — DONE
- `ChatChunk.answer_status` field + `_answer_provider_unavailable_chunk`
  helper; all three terminal post-retrieval failure paths in
  `chat_orchestrator.py` now emit
  `{"status": "answer_provider_unavailable", "retrieval_succeeded": true,
    "sources": [...]}`. A provider failure can no longer read as missing
  evidence.
- Live-verified: real provider failure on the fixture corpus returned the
  structured verdict with all 5 retrieved sources
  (`data_eval/q6_answer_provider_unavailable_live_sse.txt`). Provider
  credentials themselves still need repair (DeepSeek 402 / Longcat 401).
- Tests: `tests/test_answer_provider_failure_contract.py` (6).

### 4. Permanent fixture + temp cleanup — DONE
- `c6518e7b` marked via `backend/scripts/mark_permanent_integration_fixture.py`:
  `fixture.purpose=permanent_integration_fixture`,
  `production_visible=false`, `excluded_from_user_search=true`.
- `IngestionService.list_corpora` consumes the flag (fail-open); live-verified
  fixture absent from user list but GET-by-id returns 200.
- Deleted straggler `_scratch_canary_p4` (acf8265e): Mongo tombstoned,
  4 Qdrant collections dropped, Neo4j 0. Original canary b596c89e remains
  tombstoned as the deletion ledger. JSON reports preserved in `data_eval/`.
- Outstanding owner decision: three duplicate "Mixed-Content Book Smoke
  (Slice 1)" corpora (48de0f90, 7563dbd7, 3ab29d69) and duplicate Temporal
  Smoke corpora predate this program — not deleted without explicit sign-off.
- Tests: `tests/test_permanent_integration_fixture.py` (5).

### Remaining
```yaml
q8_qdrant_one_evidence_collection_canary: pending   # next slice per owner
q9_10_file_pressure_test: pending
answer_provider_credentials: needs_repair           # environment, not code
full_corpus_reingestion: not_authorized
```
