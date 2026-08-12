# FAILURE LEDGER

Symptom → root cause → guarding invariant. Match new symptoms against old shapes first.

| Symptom | Root cause | Guard now | Source |
|---|---|---|---|
| burst-then-stall, measured all night | claim lease defaulted to minutes while graphify execution of a large doc runs 10-25 min; mid-run expiry let another runner reclaim jobs to queued, lease-fenced _mark_jobs matched zero rows — hours of completed extractions persisted but jobs never flipped, next claimant re-ran the same chunks | [INVARIANTS.md](INVARIANTS.md): claim lease 7200s > worst-case book execution | `d399aff` |
| fenced-but-wasteful busy-steals at 180s adoption | heartbeats share the runner's event loop; book-scale sync phases stall beats 400s+ while ALIVE; 180s adoption threshold stole live work | INVARIANTS.md: DEFAULT_LANE_ADOPT_STALE_SECONDS = 420.0 | `474b076` |
| first start each tick went to a batch that immediately deferred, consuming the single INGEST_MAX_ACTIVE_BATCHES=1 slot | livelock guard counted every unexpired lease as owned (incl. dead-owner leases the adoption path exists to reclaim) and only deprioritized live-owned candidates | INVARIANTS.md: heartbeat-fresh leases only + skip live-owned outright | `88ebb704 → 88ebb74` |
| crash between artifact persistence and receipt write → re-inference duplicated work | durable hash-verified artifact with no passing receipt | INVARIANTS.md: _stage ADOPTS artifact + completes receipt, never re-infers | `82b37ff` |
| O2 crash battery: killed process pool child bricks ProcessPoolExecutor permanently (407 docs mass-failed, 10g container) | chunk-subprocess children imported full worker schema machinery | INVARIANTS.md: chunk_subprocess imports only tier_chunker + direct deps | `82b37ff / chunk_subprocess.py:1-5` |
| three ingest-fleet stalls + Ollama Cloud UI-path 400 | fleet stalls in batches.py/job_leases.py; UI-path 400 in llm.py | INVARIANTS.md: lease/adoption semantics; provider payload prefix gate | `7044f46` |
| vocabulary mismatch: battery gold 12 predicates vs decoder 31 vs ontology 20 — a predicate in one but not the other | three independent vocabularies with no mechanical MUST MATCH | [VOCABULARIES/](vocabularies/) pages + MUST MATCH lines; INVARIANTS.md three-lane structure | `vocab-drift` |
| 4-bucket lowercase entity types taught by json_object prompt while xgrammar enforces 15 Capitalized types + 31 snake_case predicates the prompt never shows | prompt teaches a contract the grammar forbids; small model guesses letter-by-letter under the token mask (company→Concept, place→Product), recall collapses | INVARIANTS.md: prompt vocabulary ≡ grammar vocabulary; SPECIMENS/ghost_b_prompt.md | `prompt-vs-grammar` |
| 'deployed' dead code with a restart; corpus lifecycle pointed at retired :8085 controller | no service→port→status→owner table; bake-vs-bind confusion | TRUTH_TABLES.md rows (backend baked-in image vs bind-mounted config; lane-manager :8085 ACTIVE) | `8085-dead-code` |
| book-scale completion failure (O5 finding #3) | graphify_completion book-scale defect | test coverage in test_graphify_completion; release-gate probes shadow_a/b_graph_release_gate_probe.py | `04026d4` |

## Pattern matching guide

- **'burst then stall'** → check the lease/claim section (d399aff) and adoption threshold (474b076) first.
- **'vocabulary rejection / letter-by-letter guessing'** → diff SPECIMENS/ghost_b_prompt.md vs VOCABULARIES/grammar-schema first.
- **'deployed but behavior unchanged'** → check TRUTH_TABLES deploy semantics (baked vs bind-mounted) before touching config.
- **'same concept, different names in different files'** → the vocab-drift class; diff the VOCABULARIES/ MUST MATCH lines.
## 11. GPU idle after deploy while work queued (2026-08-11)
symptom: executor+pump both log `lease_busy claimed=0` on every GPU corpus; nvidia-smi 1%; queue unchanged
root cause: `up -d --force-recreate` kills runners mid-batch; their `ingest_lane_leases` rows (per-corpus `extraction` lane mutex, ~30-min TTL, heartbeat-refreshed) survive with dead owners — all claimers block until TTL
guard: boot sweep in enrichment_executor deletes lane leases with heartbeat_at >10min stale (live holders refresh every few minutes); INVARIANT: deploy stall <= one boot, not one TTL

## 12. N executors, 1 lane of GPU work (2026-08-12)
symptom: 4 workers running, engine shows only 64 in-flight (one lane's worth) with 40s dead gaps; workers log `lease_busy claimed=0`
root cause: every executor sorted corpora by corpus_id and walked them in the SAME order, so all N raced the same corpus-lane mutex; one won, the rest starved — parallel workers produced serial GPU feed
guard: per-process rotation of the corpus walk (offset = hash(hostname)+cycle); INVARIANT: N concurrent executors must cover min(N, corpora) DISTINCT corpora per cycle
