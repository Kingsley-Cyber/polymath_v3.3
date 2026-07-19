# Polymath v3.3 — agent & contributor guide

Read this before deploying or running the stack. It exists to stop specific,
costly mistakes from recurring on any device or by any AI agent.

## ⚖️ ROLE LAW (owner-engraved 2026-07-14 — supersedes all prior role behavior)

**Claude is NEVER an executor. Claude is ALWAYS and ONLY: supervisor, reviewer,
planner.** Claude never runs product code, scripts, tests, builds, deploys,
batches, or backfills. Claude NEVER spins up subagents of any kind. Claude's
only executor is CODEX — the sole entity that executes code and the only
"subagent" Claude may direct, via COORDINATION.md directives and the mission
file. Claude's permitted actions: reading/inspecting state, writing plans,
specs, rulings, and reviews into the coordination/ledger documents, and
committing THOSE document changes. If work needs executing and Codex is
unavailable, the work WAITS and Claude reports it — Claude does not fill the
seat. Violation of this law is a failure regardless of outcome quality.

## 🧭 North star: the implementation checklist — never execute from memory

`docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md` is the **item-level execution
ledger**, and `BUILDLINE.md` is the **temporal north-star** (checkpoint order,
NOW pointer). Item truth lives in the checklist; time truth lives in BUILDLINE;
every adopted design must hold a BUILDLINE slot the day it is adopted. It is a live file edited by
multiple agents in parallel — any in-head copy of it is stale by construction.

**The habit this section kills — "cached-plan" execution.** On 2026-07-13 the
agent worked from its session memory of the plan instead of the file: it
rebuilt lexicons + librarian cards for the PoC corpora while adopted capture
hooks for those SAME rows (temporal_class in the summary contract, bibliographic
author/date capture) were still unbuilt — forcing a full second rebuild — and
assumed latent-concept coverage was corpus-wide when only one small regen
window actually had it. Sequencing lives in the file, not in memory.

Binding rules:

1. **Read-before-act.** Re-read the relevant checklist section FROM DISK at the
   start of every work phase, before dispatching any subagent, and after any
   merge. Quote the item you are executing; if you can't point to it, you are
   not executing the plan.
2. **Adopt-then-execute.** New ideas and new owner directives are written INTO
   the checklist first — slotted into the dependency order with what they block
   and what blocks them — and only then implemented. Work with no checklist
   anchor is drift: stop and add the anchor.
3. **Rebuild freeze (dependency gate).** Never rebuild a derived artifact
   (lexicons, cards, summaries, embeddings, indexes) while an adopted-but-unbuilt
   capture hook targets the same rows. One contract change, one pass: batch all
   adopted capture fields into a single contract seam, then run ONE data pass
   that carries them all.
4. **Scratch-off with receipts.** `[x]` only after live verification per the
   item's completion rule; `[IN CODE @commit]` for merged-but-not-deployed;
   never mark from intent. Before starting an item, verify its upstream
   dependencies are actually `[x]` in the file — not just remembered as done.
5. Pair this with the Goal Drift rules in `AGENTS.md` — that section governs
   how to fix a bug; this one governs what order to do work in.

## 🚨 Deploying / recreating the backend — DO NOT get this wrong

The backend reaches its embedder + reranker via env vars. The correct values are
platform-specific:

- **Apple/Mac:** use `docker-compose.apple-mlx.yml` → host MLX sidecars
  `EMBEDDER_URL=http://host.docker.internal:8082`,
  `RERANKER_URL=http://host.docker.internal:8081`.
- **Linux/NVIDIA / Windows RTX:** use the default `docker-compose.yml` plus the
  `local-embed` / `local-rerank` profiles from `.env`, where
  `EMBEDDER_URL=http://embedder:80` and `RERANKER_URL=http://reranker:8080` are
  correct internal Docker network URLs.

**The trap:** `docker-compose.override.yml` is auto-merged by Docker Compose and
must stay local-only. It is gitignored. Do not commit machine-specific sidecar
IPs or host paths. Use `docker-compose.override.example.yml` as the template if
you need local experiments.

### ✅ The only correct ways to (re)deploy the backend

```bash
# Mac — canonical (matches scripts/setup_apple_mlx.sh):
docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build backend
# …or the full one-shot setup:
bash scripts/setup_apple_mlx.sh

# RTX/NVIDIA — canonical after bootstrap:
cd <repo> && docker compose up -d --build backend
```

### ❌ NEVER do this
```bash
git add docker-compose.override.yml   # commits private machine routing
```

### ✅ ALWAYS verify after any backend (re)deploy
```bash
bash scripts/verify_backend_runtime.sh
```
It fails LOUD (non-zero exit) if a live embed through the backend returns no
vector. The backend ALSO runs an embedder self-check at startup and logs
`CRITICAL` with the fix if embedding fails (see `backend/main.py` lifespan), so a
bad deploy can never be silent.

Quick manual check: `docker exec polymath_v33-backend-1 sh -c 'echo $EMBEDDER_URL'`
must show `host.docker.internal:8082` on Apple MLX, or `embedder:80` on RTX.

## Code & deploy mechanics
- Backend code is **baked into the image at build time** (COPY, not a volume
  mount). A code change requires `--build` to take effect; an env-only fix just
  needs a recreate. `tests/` is NOT baked into the image.
- The MLX embedder (`:8082`) and Qwen3 reranker (`:8081`) are **host sidecars**
  managed by `scripts/setup_apple_mlx.sh` / verified by `scripts/smoke_apple_mlx.sh`.

## Tests
- **Portable invariants** (`backend/tests/test_retrieval_quality_invariants.py`,
  `test_retrieval_ranking_policy.py`): pure logic, NO live stack. Run anywhere:
  `cd backend && pytest tests/test_retrieval_quality_invariants.py -q`. CI runs
  these on every push (`.github/workflows/retrieval-quality.yml`).
- **Live e2e** (`backend/tests/test_*_e2e.py`): import-safe scripts that need the
  running stack + seeded data. Run via `docker cp test.py polymath_v33-backend-1:/app/_x.py
  && docker exec -w /app polymath_v33-backend-1 python _x.py`. They are NOT pytest
  tests (no module-level execution at collection).
- **Prove before commit:** non-trivial backend changes get an automated asserting
  test (non-zero exit on fail) run GREEN before committing — log-greps are not proof.

## Rebuild stipulations (changes are NOT live until the right rebuild)
Nothing in this repo hot-reloads. For every change, the matching rebuild is
REQUIRED before claiming it works — and verify, don't assume:

| You changed | To make it live | Verify |
|---|---|---|
| `backend/**` | `docker compose up -d --build backend` (NEVER `-f docker-compose.yml` alone — drops the override → dead sidecars, silent vector=0) | health green + the embedder self-check line in logs |
| `frontend/src/**` | `docker compose up -d --build frontend` (container serves BUILD-TIME dist) | `docker exec polymath_v33-frontend-1 grep <marker> /usr/share/nginx/html/assets/*.js` — then HARD-refresh the browser (`Cmd+Shift+R`); the public origin is `rag.kingsleylab.xyz` and can also cache at the Cloudflare edge |
| `docling_svc/**` | rebuild the docling container (it's on-demand/profile-gated) | — |
| `scripts/apple_ml_services/**` | `bash scripts/install_apple_mlx_runtime.sh` (rsyncs to `~/PolymathRuntime` — the LaunchAgent runs the COPY, not the repo) then `launchctl kickstart -k gui/501/com.polymath.apple-ml` | ports 8082/8081 up |
| Ghost B extraction sidecar | runs from `local_ghost_b/.venv` (glirel-compatible pins — the shared apple_ml_services venv CANNOT run it); ad-hoc launch: `PYTHONPATH=<repo>/backend local_ghost_b/.venv/bin/python -m uvicorn ghost_b_extract_svc.main:app --port 8084` from `~/PolymathRuntime/apple_ml_services` | `curl :8084/health` |

Fast iteration WITHOUT a rebuild: `docker cp` the edited file into the
container (+ `docker restart` for import-time code) — but a container
RECREATE reverts to the image, so always finish with the real rebuild.

## RunPod extraction architecture (read before ANY runpod-lane ingestion work)

- **Wire contract**: `runpod_wire_contract: local_extraction_v1` in the corpus
  config routes chunk-extraction to serverless endpoints named in
  `runpod_local_extraction_routes[]` `{account_name, endpoint_id}`. The
  deterministic image is digest-pinned
  (`king2eze/polymath-local-extraction@sha256:4cb08457…`, GLiNER medium) —
  never retag; deploy new endpoints from the same digest.
- **Accounts & keys**: multi-account rows live in Mongo
  `settings.ingestion.runpod_flash.accounts[]`; the matching keys are
  ENCRYPTED under `settings.api_keys.runpod_accounts.<name>`
  (settings-service `get_system_runpod_flash_accounts()` resolves both —
  env vars are NOT the truth). Fleet enable/disable truth =
  `accounts[].enabled` / endpoint rows, not env. Keys can be
  restricted-scope: a key that 401s other endpoints' `/health` or 403s the
  GraphQL management API is invoke-only — do not assume it can deploy.
- **Worker quota**: RunPod caps ~10 serverless workers per account across
  ALL endpoints. `workersMax` is reallocatable live via GraphQL
  `updateEndpointWorkersMax` — check that the endpoints your contract
  actually routes to hold the quota (2026-07-19: the deterministic
  endpoints ran at workersMax=1 while unused default endpoints hogged 15
  slots; reallocated to 5+4).
- **Economics**: serverless bills active seconds only (idleTimeout 60s,
  scale-to-zero verified: `currentSpendPerHr: 0` when idle). Sequential
  batches waste no money — only cold-start latency. Measured: 117 books'
  extraction ≈ $0.10–0.15 total; transcripts are ~10× cheaper per file.
  The cost center of ingestion is the SUMMARY provider, not RunPod.
- **Extract-first profile** (`runpod_extract_first`): pass 1 sweeps
  parse→chunk→extract for the whole batch (saturated pod burst, durable
  staged artifacts at stage `extracted`, `defer_summaries: true`), pass 2
  finishes embed/index/graph locally at zero pod cost. Use it for large
  corpora; `runpod_burst` (single pass, in-batch summaries) for small ones.
- **Job journal**: completed runpod jobs are journaled per-corpus at
  `/data/ingest-files/runpod-job-journals/corpus-<sha256(corpus_id)>.jsonl`
  for reuse. "reusable completed-job closure is partial" = the journal has
  some-but-not-all slices for an item (interrupted run); archive the
  journal file to force clean re-extraction of retrying items.
- **File-concurrency governors**: runner concurrency =
  min(batch.options.concurrency, INGEST_GLOBAL_MAX_DOCS,
  INGEST_MAX_ACTIVE_JOBS) — the offline overlay maps `OFFLINE_INGEST_*`
  env values into the worker; all three default to 1. Local pass-2
  pressure points: qdrant `mem_limit` (daily overlay honors
  `QDRANT_MEM_LIMIT`, ≥8g for book-scale) and the Metal embedder.
- **Law**: 1-file canary with full receipts (extraction provider =
  runpod_local_extraction, summary calls settled, vectors verified,
  balance delta measured) before ANY multi-hundred-file batch.
