# Alias Pipeline Phase 8 Closeout — 2026-08-04

## Status: COMPLETE (shadow/canary wiring) — HARD PAUSE

Live Fast / Hybrid / Graph wiring behind explicit feature controls.
**Production activation is NOT authorized.**

```text
STOP.
```

Do not:

* enable schema expansion globally
* mutate active production schema points
* backfill historical aliases
* switch Fast ranking in production
* create canonical Neo4j identity edges
* begin full-corpus migration

Production activation only after the live end-to-end alias test **and** the
broader q9 10-file retrieval test both pass (owner gate).

---

## Controls (live defaults)

```yaml
alias_retrieval:
  enabled_globally: false
  shadow_enabled: true
  ranking_enabled: false
  fixture_corpus_allowlist:
    - isolated_alias_fixture
  production_schema_writes: false
  production_backfill: false
```

Settings fields: `ALIAS_RETRIEVAL_*` in `backend/config.py`.

---

## Exact files changed

| Path | Role |
|---|---|
| `backend/config.py` | Feature flags (shadow on, ranking/global/writes off) |
| `backend/services/ingestion/alias_retrieval_shadow.py` | Shadow orchestrator + fixture registry + fail-closed lane |
| `backend/services/retriever/__init__.py` | Wire into `retrieve_planned` + legacy `retrieve()` attach |
| `backend/tests/test_alias_retrieval_shadow_phase8.py` | Fixture acceptance tests (15) |
| `scripts/alias_pipeline/generate_phase8_artifacts.py` | Artifacts generator |
| `scripts/alias_pipeline/live_phase8_probe.py` | Container live probe |
| `data_eval/alias_pipeline/phase8_*` | Acceptance + query reports |
| `CONTINUITY/ALIAS_PIPELINE_PHASE8_CLOSEOUT_20260804.md` | this closeout |

---

## Required behavior (verified)

```text
Original query lane  → always executes; ranking unchanged when ranking_enabled=false
Schema lane          → non-blocking (deadline); records trust class / expansions / anchors
Schema failure       → status=schema_lane_failure_fallback; direct results preserved
Final answer         → schema_records_as_citations=0; document evidence only
```

### Trust-class behavior

| Class | Production shadow | Fixture ranking | Notes |
|---|---|---|---|
| trusted_alias | yes | allowed if ranking_enabled | identity authority |
| retrieval_surface_variant | yes | bounded if ranking_enabled | no identity authority |
| ambiguous_alias | scoped parent/doc only | n/a | cross-expansion = 0 |
| related_term | trace only | never | ranking_changes = 0 |
| description | no expansion | never | — |

### Route wiring

| Tier | Schema assist |
|---|---|
| Fast | original children + ≤1 bounded canonical child search; linked child anchors; no parent/section summaries |
| Hybrid | original vector+lexical + parent/section anchors + canonical terms + child hydration |
| Graph | original seeds + entity/node anchors + authority filtering + child hydration |

---

## Receipts (MEASURED)

### Unit / fixture suite

```text
host: Mac
command: PYTHONPATH=backend local_ghost_b/.venv/bin/pytest
  tests/test_alias_*phase*.py tests/test_alias_identity_contracts.py
  tests/test_alias_candidates_phase2.py -q
result: 102 passed (phases 1–8)
phase8 alone: 15 passed
```

### Artifacts

```text
data_eval/alias_pipeline/phase8_acceptance.json
data_eval/alias_pipeline/phase8_query_reports.jsonl
data_eval/alias_pipeline/phase8_controls.json
```

Acceptance (MEASURED from generator):

```yaml
retrieval:
  direct_lane_without_schema_hit: passed
  schema_lane_failure_fallback: passed
  trusted_alias_improves_expected_recall: passed
  ambiguous_IR_cross_expansion: 0
  related_term_ranking_changes: 0
  schema_records_as_citations: 0
  final_evidence_hydration: passed
runtime:
  Fast_latency_overhead_measured: true   # ~0.0001–0.0002s shadow lane
  Hybrid_latency_overhead_measured: true
  Graph_latency_overhead_measured: true
  restart_replay_identical: true
  production_queries_unchanged: true
```

### Live container probe (MEASURED)

```text
host: polymath_v33-backend-1 (docker cp + restart — not image-baked)
command: python /app/_phase8_probe.py
result: LIVE_PROBE_OK
  Fast/Hybrid/Graph status=ok; traces=1 on fixture; ranking unchanged
  production corpus path: records=0; chunk order unchanged
verify_backend_runtime.sh: OK embed dim=1024
```

Note: `isolated_alias_fixture` is **not** an ingested Mongo corpus yet — probes use
the in-process shadow registry allowlisted for that id. No production schema
writes. Image recreate will drop docker-cp'd files until a durable `--build`.

---

## Deploy durability — PASSED 2026-08-04T20:58Z

| State | Meaning |
|---|---|
| `[IN CODE @ working tree]` | Phase 8 source on disk |
| `[BAKED @ image]` | backend `680e48dac2e5…`, mcp `944b5508a796…`, ingest-worker `c02e57fd8e80…` |
| Force-recreate proof | Second recreate without rebuild kept hooks; `docker_cp_dependency=0` |
| Artifact | `data_eval/alias_pipeline/phase8_durability_acceptance.json` |

```yaml
durability:
  docker_cp_dependency: 0
  hooks_present_after_force_recreate: true
  feature_flags_preserved: true
  direct_ranking_unchanged: true
  replay_identical: true
production_ready: false
```

---

## NOT done (explicit)

* `enabled_globally=true`
* `ranking_enabled=true` in production
* production schema collection writes
* historical alias backfill
* Neo4j canonical identity edges
* full-corpus migration
* q9 10-file retrieval test
* live end-to-end alias ingest of `isolated_alias_fixture`

---

## Next (requires owner authorization)

1. Durable backend rebuild (bake Phase 7–8 modules)
2. Isolated fixture corpus ingest + live e2e alias test
3. Broader q9 10-file retrieval test
4. Only then consider production activation
