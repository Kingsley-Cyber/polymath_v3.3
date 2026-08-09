# Ingestion Control Plane — Owner Ruling (2026-08-05)

**Status:** BINDING for all q9 final E2E and forward ingest work.  
**Supersedes:** any launch that treated DeepSeek / cloud Ghost A as the required summary pathway.

## Control table (moving forward)

| Lane | Authority | Default |
|---|---|---|
| Extraction | `relex_local` only (`CANONICAL_ENGINE` in `extraction_contract.py`; host sidecar `:8086`) | ON |
| Required summaries | `deterministic_summary.v1` (`services/ingestion/deterministic_summary.py`) | ON when `chunk_summarization=true` |
| Cloud / LLM summaries | `llm_summary_enrichment.v1` (Ghost A / `summarize_parents`) | **DEPRECATED** — not removed; only when explicit `summary_cost_run_id` + cost authority is open |
| Embedding | Local MLX only (`embed_mode: local`) | ON |
| Graph materialization | Neo4j when `use_neo4j=true` | per config |

## Why prior clean-gen must be redone

Corpus `q9_final_e2e_20260805` / `7d801816-17d9-4209-b09e-b511339e3d8e` was launched with DeepSeek `summary_models` + `summary_cost_authority_usd`. That opened the deprecated enrichment lane as if it were the required baseline. Acceptance truth for q9 final E2E is **invalid** until a clean generation runs under this control plane with zero provider summary calls.

Rollback corpus `6a766597-29f3-4a3e-8918-5de10f0053b3` stays available; do not treat the contaminated sibling as activation truth.

## Worker seam (code)

`backend/services/ingestion/worker.py` → `_run_ghosts_parallel`:

1. `need_summaries` = `chunk_summarization and not defer_summaries`
2. `need_deterministic_summary` = summaries needed **and** `summary_cost_controller is None`
3. `need_llm_summary_enrichment` / `need_ghost_a` = summaries needed **and** cost controller open
4. Cost controller opens only when `summary_cost_run_id` is present (`process_document` startup)

## Launch contract for redo

- Profile: `mac_safe` (or equivalent local-only)
- Extraction: `relex_local` / engine lock
- Summaries: `chunk_summarization=true`, **no** `summary_cost_run_id`, **no** `summary_cost_authority_usd`
- Do not put DeepSeek (or any cloud model) in the required summary path
- Receipts must show `summary_model=deterministic:v1` / schema `deterministic_summary.v1` and `provider_calls=0` for the baseline lane

## API / batch seam (fixed 2026-08-05)

Prior bug: `create_local_batch` always stamped `summary_cost_run_id=batch_id`, and
the router required `summary_cost_authority_usd` whenever summarization was on —
forcing the deprecated cloud enrichment lane.

Correct contract now:

- No authority → `summary_cost_run_id=null`, worker uses `deterministic_summary.v1`
- Authority present → `summary_cost_run_id=batch_id`, worker may open Ghost A enrichment
- Worker opens `SummaryCostController` only when **both** run id and authority exist
- `ingestion_service.ingest` must not raise when summarization is on without a cost run
- Summary provider preflight canary runs **only** when batch has cost authority
  (otherwise it falsely deferred summaries / forced cloud)

## Active clean generation (MEASURED 2026-08-05)

| Field | Value |
|---|---|
| corpus | `q9_final_e2e_det_20260805` / `d153de2a-14c9-4ab3-9063-565d872f3773` |
| batch | `740be753-d988-4b8b-9d98-5c8305398075` |
| profile | `mac_safe` |
| extraction | `relex_local` |
| cost fields | both `null` |
| rollback | `6a766597-29f3-4a3e-8918-5de10f0053b3` |
| contaminated sibling (void) | `7d801816-17d9-4209-b09e-b511339e3d8e` |

## Regression gate

`backend/tests/test_worker_summary_control_plane.py` — without cost authority, `summarize_parents` must not run; deterministic artifacts must be produced.

## Next action after this ruling is on disk

1. Bring Docker back up  
2. Rebuild backend + ingest-worker images (bake worker control seam)  
3. Launch a **new** sibling corpus under this contract (do not resume contaminated batch as acceptance)  
4. Continue XML phases 2–12 on that generation only  
