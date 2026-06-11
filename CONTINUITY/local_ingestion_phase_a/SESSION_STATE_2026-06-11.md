# SESSION STATE — 2026-06-11 (supersedes prior docs in this folder for "where are we")

Read this FIRST after compaction. Older docs in this folder are background:
A1_FINDINGS (extraction contract), PILOT_REPORT (quality evidence),
NOISE_PROOFING, TABLE_FACTS, CHUNK_SIZE_AB, RTX_SIDECAR_RUNBOOK (setup),
00_LOCKED_DECISIONS / 06_BEHAVIORAL_RULES (still binding).

## Mission status: BUILD COMPLETE — backfill NOT yet launched

Fully local, deterministic Ghost B extraction (GLiNER ×2 + GLiREL + Python
rules) replaced cloud extraction end-to-end. Ghost A summaries remain cloud
(reconfirmed twice by user — do not touch). Quality proven (15/15 pilot,
typed-share 46–97%, no junk signature, 4/4+4/4 retrieval probes, 100%
fact-entity linkage). Everything is committed+pushed through `e918ad6`
(repo github.com/Kingsley-Cyber/polymath_v3.3, branch main; verify with
`git rev-list --count origin/main..HEAD` == 0).

THE ONE BIG PENDING ACTION: the 498-file backfill —
`scripts/run_backfill.sh` (caffeinated, resumable, monitored; preflight now
requires the ONNX sidecar healthy+CUDA on :8086). User says "launch" when
ready. Target: `/Volumes/Flash Drive/authentic_files` (498 .md, 340 MB).
Expected wall: ~half a day — extraction is 19 ms/chunk on RTX ONNX; the Mac
MLX embedder is the long pole now.

E2E VALIDATED 2026-06-11 (corpus onnx_e2e_proof, 2657e9b0, 2 files): lister
dotfile guard → docling → 94 children → extraction routed via Settings to
RTX :8086 at 24–26 ms/chunk → 347 entities / 470 relations (87.7% typed) /
153 facts → 88 vectors exact → verify ok=true ×2 → dense retrieval routed
4/4 to the right doc per query + Qwen3 rerank discriminating (0.97–0.43
spread). PRODUCTION EXTRACTION CONFIG NOW LIVES IN SETTINGS (Mongo
`extraction.endpoints`; UI Settings → Ingestion): "RTX ONNX (GPU)" :8086
enabled; ":8084 control" + "Local sidecar (Mac)" rows present but DISABLED —
round-robin splits slices evenly across live enabled endpoints, so a slow
enabled endpoint drags everything. Compose env LOCAL_GHOST_B_EXTRACT_URL is
only the seed/fallback now.

KNOWN FRAGILITY: RTX :8086 was launched manually — NOT boot-persistent until
the user's RTX agent executes bus instruction 001 (CONTINUITY/agent_bus/;
needs the user to type the one bootstrap line). If the RTX box reboots
before that, relaunch 8086 per the envs in 001.

## Topology (the user's final, simplified operating model)

| where | what | how managed |
|---|---|---|
| Mac Studio 32 GB (this machine) | Docker stack (backend/frontend/Mongo/Neo4j/Qdrant/litellm/redis/cloudflared…, 11 containers), native embedder :8082 (MLX, EMBED_BATCH_SIZE=128), native reranker :8081 (Qwen3-Reranker-0.6B cross-encoder via brew llama-server on Metal), native extraction sidecar :8084 | LaunchAgents: com.polymath.apple-ml (embedder; START_RERANKER=false now), com.polymath.reranker-qwen3, com.polymath.ghostb-extract |
| RTX PRO 6000 Blackwell 96 GB, Windows, 28 cores, LAN 192.168.1.83 | extraction sidecar(s) :8084 (+:8085) — repo clone at E:\polymath_v3.3, venv .venv_sidecar, torch cu130, weights via GLIREL_CKPT_DIR=E:\Polymath_Training\ghost_b_dataset\runs\glirel_ghost_b_v1\best | Windows Scheduled Task PolymathGhostBSidecar + run_sidecar_windows.ps1 (in THEIR clone, possibly uncommitted there). Firewall LAN-only 8084-8091. User drives this box via their own AI agent — communicate by giving the USER paste-ready agent prompts. |

**Extraction endpoint selection is now an APP FEATURE** (user's explicit
product requirement): Settings → Ingestion → "Extraction Engines" card —
toggleable {label,url,enabled} list, Mongo-backed (`extraction` section of
GlobalSettings), seeded from LOCAL_GHOST_B_EXTRACT_URL env, read per-ingest by
the worker via `settings_service.get_system_extraction()` →
`ghost_b_local.RUNTIME_ENDPOINT_URLS`. The client liveness-probes enabled
endpoints per doc and round-robins slices across live ones. RTX off → Mac
sidecar absorbs small batches automatically. Verified e2e both directions.
Env override file: docker-compose.override.yml (EMBEDDER_URL, RERANKER_URL
+scale, LOCAL_GHOST_B_EXTRACT_URL=rtx:8084,rtx:8085,host.docker.internal:8084,
EXTRACTION_MAX_ACTIVE_DOCS=4).

## RTX-side state (LAST KNOWN — confirm before relying)

2026-06-11: the user's agent executed RTX_ONNX_AGENT_PROMPT.md — 8084 torch
(GLiNER on CPU, see truth table) + 8086 ONNX-CUDA both up at envs 256/256/512,
verified from the Mac via /health gliner blocks. PENDING (user types one line
to their agent): pull ≥322cc4f and ROLE-SWAP — 8084 relaunches with the ONNX
envs (production; app engine list needs NO change), 8086 relaunches plain
torch (becomes the torch-CUDA control to complete the truth table). The
agent's report file (git push, http :8091 fallback) never arrived — fully
superseded by direct Mac-side /health probes + benches. Port 8090 note:
Wondershare WsToastNotification.exe squats it if their main app runs.

## Performance: measured truth table (do NOT re-test these)

| hypothesis | result |
|---|---|
| GLiNER internal batching (default 8/forward) is the bottleneck | Pin to slice made it WORSE: forward 8=328 ms/chunk, 32=654, 256=842 (padding on length-varied texts). GHOST_B_GLINER_FORWARD env, default 8. |
| fp16 GLiREL on MPS | 0.99× — null (Mac is unified-memory-bandwidth-bound for DeBERTa-large; ~420 ms/chunk is the Mac's ceiling) |
| Dual-lane GLiREL (CPU+GPU threads on Mac) | null (same bandwidth wall). Code kept env-gated GHOST_B_GLIREL_CPU_LANE, default off |
| Multi-process fleet on Windows | FULLY SERIALIZES — 2 procs = exactly 2× each (WDDM; CUDA MPS is Linux-only). Fleet abandoned; multi-URL client kept for CROSS-machine fan-out |
| RTX single-process real-content rate (torch) | ~280–330 ms/chunk — ROOT CAUSE FOUND 2026-06-11: get_gliner() was Mac-written (MPS-else-CPU) and silently ran GLiNER+facets (89% of wall) on the 28-core CPU; only GLiREL was on GPU. "Kernel-launch overhead, GPU mostly idle" was a WRONG theory — the GPU was idle because the work never reached it. Fixed in 322cc4f (CUDA > MPS > CPU; /health gliner.device is the tell). |
| ONNX CUDA lane on RTX (same 256-chunk payload, Mac-side bench) | 19 ms/chunk total (gliner 2.2s + facets 0.9s + glirel 1.5s for 256) vs deployed torch-CPU 253 ms/chunk = 13.3×; both rounds identical. CUDAExecutionProvider verified active via /health; 9 ms/chunk pass-1 is physically impossible on CPU. Quality: 536 vs 535 entities — the single diff is ('multithreading', Concept) at conf 0.45019 vs threshold 0.45, pure fp boundary; zero conf drifts >0.01 elsewhere. torch-CUDA control number pending (8086 role-swap). |
| E2E measured | 1 MB book = 8m13s (ghosts 342s, embed 106s, qdrant 6s) → 498 files ≈ 1.3–1.7 days with concurrency-3 pipelining |

**ONNX Runtime for GLiNER — VALIDATED ON RTX 2026-06-11: 19 ms/chunk, 13.3×
vs deployed torch (see truth table). PROMOTED: 8084 relaunching as ONNX.
Backfill ETA collapses to well under a day; the Mac MLX embedder is now the
expected long pole.** Env gate: `GHOST_B_GLINER_ONNX=1` swaps
BOTH GLiNER passes (entity + facet — the one shared instance in
facet_tagger.get_gliner) onto ORT; companions GHOST_B_GLINER_ONNX_REPO /
_FILE / _DEVICE (defaults: onnx-community/gliner_medium-v2.1,
onnx/model.onnx, auto). NOTE: gliner 0.2.26 has NO providers kwarg —
map_location "cuda" requests CUDAExecutionProvider (exclusive), else CPU EP.
Sidecar /health now reports the ACTIVE session providers under "gliner".
Quality gate: `local_ghost_b/onnx_equivalence_check.py` (dump per lane via
env, then compare) — full-pipeline diff, gates ent-jaccard≥0.95 /
rel≥0.90 / facet-agree≥0.95. Mac CPU-EP run on 16 real chunks: PASS,
jaccard 1.0 everywhere, conf delta 0. Pre-download trick (gliner
snapshot_downloads the WHOLE repo ~3GB+ otherwise): hf download with
--include "*.json" "spm.model" "onnx/model.onnx", point _REPO env at the
local dir (Mac copy: local_ghost_b/models/gliner_onnx_medium_v2.1, now
gitignored). Remaining risks RTX-side: (a) DOCUMENTED silent-CPU-fallback
on Blackwell sm_120 with ORT-cu12 vs torch-cu13 (ragflow#14565,
ORT#26177/#27875) — verify /health providers AND nvidia-smi utilization
during bench, never logs alone; (b) einsum ops choke some ORT backends
(DirectML EP = Windows plan B). Expected 3–5× on the 89% (gliner+facets)
→ backfill ~0.7–1.1 days. Bench design: agent stands up ONNX instance on
port 8086 (separate venv OK); bench from Mac with the 256-chunk payload
(re-export: mongo chunks doc_id^4ceee45bfa14 text limit 256 → POST
/extract, read response `timings`); add to engine list only if it beats
328 ms/chunk with verified GPU use. fp16 (onnx/model_fp16.onnx) allowed
only after passing the equivalence gate. Can run DURING the backfill.

## Resilience features in place (all live-verified)

- Embed self-healing: transient-failure retries (intermittent embedder 400s
  are real), vector-count contract (silent partial responses), alignment
  guard in `_embed_batch_for_doc`, reconcile-on-resume (Qdrant exact count vs
  vector-eligible children; mismatch reruns embed — verified by deleting a
  point and watching it heal in 2m17s).
- Verify stage catches everything (it caught my own md5-vs-sha1 hand-repair;
  text contract = sha1+len+is_truncated; counts corpus-scoped — I fixed the
  cross-corpus HAS_CHUNK false-fail).
- run_backfill.sh: caffeinate -ims (CRITICAL: Mac system sleep is set to
  1 MINUTE; sleep killed Docker Desktop's VM twice on 06-10), preflights
  (docker/backend/RTX/embedder/drive/disk), durable manifest batch
  (ingest-batches/local, concurrency 3), .backfill_state → rerun = resume.
- scripts/ingest_reclaim_memory.sh --apply --require-gb 22 before launch
  (dry-run default; keep-list protects Docker/terminals/Claude).

## Session gotchas (will bite you again)

- DEPLOY DISCIPLINE: backend code changes need `docker compose build backend`
  + `up -d` (source is BAKED, no volume mount). Env-only changes need only
  `up -d`. I shipped a broken deploy once by forgetting this.
- Mac sidecar/code changes: restart the LaunchAgent process (it caches
  modules). launchctl kickstart does NOT reload plist env — use
  `launchctl unload && load`.
- /tmp gets cleaned: /tmp/pm_token + bench files vanish. Re-auth:
  PW=$(grep '^DEFAULT_ADMIN_PASSWORD=' .env | cut -d= -f2-) → POST
  /api/auth/login {username: admin} → access_token.
- Mongo/Neo4j creds: from .env (MONGO_PASSWORD, NEO4J_PASSWORD); mongosh via
  docker exec polymath_v33-mongodb-1, cypher-shell via polymath_v33-neo4j-1.
- Qdrant: named vector "dense"; upsert = PUT /points; filtered scroll may 400
  (no payload index) — page client-side. Point id = md5(chunk_id) as UUID.
- Ghost A skips on test corpora (empty summary_models pool) — chat-level QA
  needs a corpus with a real model pool. litellm 400 on schema_lens → falls
  back deterministic (fine).
- Batch /resume does NOT re-run hard-failed items — re-upload instead.
- git: ALWAYS `-c user.name="Kingsley" -c user.email="ezeokonkwokingsley@gmail.com"`,
  explicit file staging (never -A), push without asking is OK for this
  initiative (user authorized). 9 pre-existing modified files + untracked
  research reports are NOT ours — leave uncommitted.
- Disposable test corpora (deletable anytime): flame_smoke*, table_facts_smoke,
  ab_control_500, ab_treatment_128, rtx_smoke*, dual_machine_smoke*,
  toggle_proof, pilot_cross_domain_15 (137550d5 — keep until backfill done,
  it's the quality reference), rtx_after_benchmark (4cb2421e),
  onnx_e2e_proof (2657e9b0 — ONNX-production-path e2e proof; keep until
  backfill done).

## After the backfill completes (agreed follow-ups, in rough order)

1. Post-backfill QA eval: ~20 cross-domain questions through the real chat
   path (needs model pool on the corpus) — this finally measures the
   "unified KB" score (~70% → target ~78+).
2. Entity dedup/resolution pass (flame vs flame-engine fragmentation) — the
   single biggest "unified KB" win (~1–2 days, deterministic embedding-merge).
3. ONNX GLiNER experiment (above) for future re-ingests.
4. GLiREL v3 fine-tune with business-prose examples (Mom Test 46% / Elegant
   Puzzle 52% / Hidden Games 55% typed — legitimate weak domains) — RTX
   recipe, NOT v2's literal-recovery recipe (it regressed).
5. Optional: have the RTX agent commit run_sidecar_windows.ps1 + firewall
   script from their clone.

## User interaction protocol

User is an "agentic vibe coder": for anything on the RTX box, give a
PASTE-READY prompt for their agent (self-contained, no conversation context
assumed). BLUF format, concrete numbers, honest about failures (user has
explicitly rewarded admitting wrong theories — I was wrong about GIL and the
batch pin; say so plainly when it happens). Predict outcomes before
multi-step/destructive actions (06_BEHAVIORAL_RULES Rule 1). Simplicity is
now an explicit product value — prefer toggles/config over new moving parts.
