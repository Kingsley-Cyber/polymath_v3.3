# INVARIANTS

One claim per line. Each is a comparison of concrete values, NOT a description —
an LLM reading code next to these lines flags a violation instantly.

Format: `INVARIANT: <comparison> — <citation> [bug ref]`. Citations are `file:line`

- INVARIANT: claim lease (7200s) > worst-case book execution — extraction_jobs.py:1240 `lease_seconds: int = 7200`, renewed every ~1/4 lease (1800s, :1470). Evidence: "graphify execution of a large doc runs 10-25 min" (commit d399aff); "even a worst-case book execution finishes inside one lease" (extraction_jobs.py:1244-1246). Any change dropping lease_seconds below book runtime reopens the lease-fence bug.  
  **Bug ref:** d399aff
- INVARIANT: DEFAULT_LANE_LEASE_SECONDS = 30*60 (job_leases.py:25) vs DEFAULT_LANE_ADOPT_STALE_SECONDS = 420.0 (job_leases.py:35) — adoption threshold must stay ABOVE the heartbeat cadence; book-scale sync phases stall heartbeats 400s+ while ALIVE (474b076), 180s invited fenced-but-wasteful busy-steals. Threshold 420s is load-bearing.  
  **Bug ref:** 474b076
- INVARIANT: extraction-job statuses on the runnable set must always be atomically CAS'd, never read-then-write — audit map §1 claim_runnable_jobs (job_leases.py:337); lease-fence _mark_jobs matches status=running (extraction_jobs.py:1178).  
  **Bug ref:** d399aff
- INVARIANT: INGEST_JOB_MAX_ATTEMPTS default 5 (config.py:1247-1250), dead-letter at attempt_limit_exhausted (job_leases.py:387-403) — a rerun of the same unchanged job must increment attempts or the dead-letter gate is unreachable.
- INVARIANT: ghost_b UNIVERSAL_RELATION_SCHEMA = 30 predicates incl. sentinel (ghost_b.py:225) ≤ SCHEMA_INLINE_LIMIT (30) — bumping above 30 flips into per-chunk Qdrant retrieval mode, which degrades fresh ingest where chunk vectors don't exist (ghost_b.py:226-229).
- INVARIANT: prompt vocabulary ≡ grammar vocabulary. ghost_b.py:147 _DEFAULT_ENTITY_TYPES = [person, org, concept, other] MUST NOT be shipped with json_schema lanes; grammar enforces 15 Capitalized types from ghost_b_schemas.py:32. The 4-bucket lowercase prompt vs 15-type grammar mismatch collapsed model recall (ghost_b.py:1787-1791).  
  **Bug ref:** prompt-vs-grammar
- INVARIANT: entity-type mapping is lossy by design: 34 Literal values (local_extraction.py:16-63) → ENTITY_TYPE_ALIASES (canonical.py:519) → 15 ontology values (config/ontology.yaml:1-16). The alias dict is the ONLY legal bridge; adding a type to one end without the alias entry strands mentions.
- INVARIANT: config/ontology.yaml entity_types (15) and ghost_b_schemas EntityType (15) MUST MATCH value-for-value — they are the same concept in two files; drift was a real bug class (battery gold 12 predicates vs decoder 31 vs ontology 20).  
  **Bug ref:** vocab-drift
- INVARIANT: three LEGAL predicate lanes: ghost_b LLM = 31 (ghost_b_schemas.py:53), spaCy dep-path = 31+9=40 (VALID_PREDICATES, spacy_relation_adapter.py:24-35 adds has_part/includes/example_of/evaluates/deploys/creates/trains/runs/quantizes), ontology promotion = 20 (config/ontology.yaml:18). The 9 ontology extensions have NO ghost_b grammar Literal and NO RELATION_ALIAS_MAP entry (verified: quantizes/evaluates/deploys/trains/runs unmapped in ghost_b.py:464-596) — they are legal ONLY in the spaCy lane. A predicate crossing lanes without its bridge entry is the bug.
  **Bug ref:** vocab-drift
- INVARIANT: SCHEMA_INLINE_LIMIT=30 + sentinel rule (ghost_b.py:222-223 'MUST stay last') — order of UNIVERSAL_RELATION_SCHEMA is contractual for the [FALLBACK] tag.
- INVARIANT: RTX lane-manager :8085 is the ACTIVE control plane (tools.py:3582 fallback http://192.168.1.83:8085); the vLLM lane serves OpenAI-compatible :8000/v1 when up (rtx-compute/SKILL.md:21). Port 8085 was absent in a past fail-safe verification (COORDINATION.md:13660) — it is not a retired controller.  
  **Bug ref:** 8085-dead-code
- INVARIANT: batch lane adoption is heartbeat-fresh-lease based (batches.py:1825-1829 reuses DEFAULT_LANE_ADOPT_STALE_SECONDS); the livelock guard must match lease-adoption semantics (88ebb74) — dead-owner leases are adoptable, live-owned candidates must be skipped so INGEST_MAX_ACTIVE_BATCHES=1 slot goes to a startable batch.  
  **Bug ref:** 88ebb74
- INVARIANT: crash between artifact persistence and receipt write leaves a durable hash-verified artifact with no passing receipt — _stage ADOPTS the artifact and completes the receipt (82b37ff), never re-infers. kill_seam.py is inert unless GRAPHIFY_OPS_KILL names a point.  
  **Bug ref:** 82b37ff

## Bug-ref key

| ref | commit / doc |
|---|---|
| d399aff | fix: extraction-job claims lease 7200s — mid-run expiry was discarding completed work |
| 474b076 | fix: widen lane adoption threshold to 420s |
| 88ebb74 | fix: livelock guard now matches lease-adoption semantics |
| 82b37ff | O2 crash battery closed + O4 vector-omission conservation + honest e2e gates |
| 7044f46 | fix: three ingest-fleet stalls + Ollama Cloud UI-path 400 |
| 04026d4 | Book-scale completion fix (O5 finding #3) + fixture rehome |
| vocab-drift | battery gold 12 vs decoder 31 vs ontology 20 mismatch class |
| prompt-vs-grammar | ghost_b.py:1787-1791 — 4-bucket prompt vs 15-type/31-predicate grammar |
| 8085-dead-code | 'deployed' dead code / retired-controller misrouting class |

VERIFY:
- `grep -n 'lease_seconds' backend/services/ingestion/extraction_jobs.py` → 7200 at :1240, :1342
- `grep -n 'DEFAULT_LANE_ADOPT_STALE_SECONDS' backend/services/ingestion/job_leases.py` → 420.0 at :35
- `grep -n '_DEFAULT_ENTITY_TYPES' backend/services/ghost_b.py` → :147 with [person, org, concept, other]
- `sed -n '32,63p' backend/models/local_extraction.py` → Literal size 34

- extraction artifacts stay CHILD-keyed: one ghost_b_extractions row per child chunk id — desired_state.py:368-400 counts required extractions as len(chunk_ids), and neo4j_writer.py:2606-2622 DROPS any result whose chunk_id is not a live child. Batching may change the PROMPT grain, never the ARTIFACT grain.
- sibling group tokens <= EXTRACTION_MAX_INPUT_TOKENS (config.py:2066, le=4096): _bounded_extraction_text (ghost_b.py:979-989) truncates prompt text SILENTLY, so an oversized group emits empty artifacts for its tail children while their jobs still flip succeeded. Clamped in sibling_batching._limits.
- entity attribution cannot use evidence_phrase: EntityItem has no such field (ghost_b.py:2167-2186); surface_form/canonical_name are the only spans available.
- sibling group content tokens x ~6.7 (measured output:content ratio) + 1,590 instruction tokens must fit max_model_len (8,192): a 1,600-token group finished 5.5% of calls on finished_reason=length (truncated JSON + rescue retry). Cap is 600.
