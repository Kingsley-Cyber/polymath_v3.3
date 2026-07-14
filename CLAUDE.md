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
