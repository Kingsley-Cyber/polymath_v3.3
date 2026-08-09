# RTX ONNX Experiment — agent instructions (self-contained)

You are an AI agent operating the Windows RTX PRO 6000 box. Execute this document
top to bottom. It is self-contained — no prior conversation context is assumed.

GOAL: stand up an ONNX-backed extraction sidecar instance on port 8086, prove it
runs on the GPU (not silently on CPU), prove output equivalence to the torch lane,
measure speed, and deliver a report file back through git. The torch instance on
8084 stays as production. This SUPERSEDES any earlier pending instruction about
relaunching two torch instances on 8084+8085.

## Context

- Repo: `E:\polymath_v3.3` (github.com/Kingsley-Cyber/polymath_v3.3), production
  venv `.venv_sidecar` (torch cu130), GLiREL weights env
  `GLIREL_CKPT_DIR=E:\Polymath_Training\ghost_b_dataset\runs\glirel_ghost_b_v1\best`
- The repo has an env-gated ONNX lane: `GHOST_B_GLINER_ONNX=1` makes the pipeline
  load GLiNER via ONNX Runtime (both GLiNER passes — entity + facet — which are
  89% of extraction wall). gliner selects CUDAExecutionProvider when
  `GHOST_B_GLINER_ONNX_DEVICE=cuda`. GLiREL stays on torch — the venv still needs
  working torch cu130.
- Baseline to beat (measured on this box, torch, 256 real book chunks):
  ~328 ms/chunk, stage split ≈ gliner 49% / facets 40% / glirel 6%.
- KNOWN TRAP (the reason for every verification step below): on Blackwell sm_120,
  onnxruntime-gpu built for CUDA 12 can fail to init or silently fall back to CPU
  when only torch's cu13 libs are present (ragflow#14565, onnxruntime#26177).
  Never trust logs — trust `/health` `gliner.providers` and nvidia-smi utilization.
- The Mac-side equivalence run already PASSED (CPU EP, 16 real chunks, Jaccard 1.0,
  confidence delta 0) — the code path is proven; your job is the CUDA environment
  and the numbers.

## Phase 0 — consolidate

1. `git pull` in `E:\polymath_v3.3` — must reach commit `8729491` or later.
2. Stop ALL running sidecar instances on ports 8084–8091. Relaunch exactly ONE
   production torch instance on 8084 (existing launch pattern / Scheduled Task,
   your cmd-/c-redirect workaround) with envs:
   `GHOST_B_GLINER_BATCH=256 GHOST_B_FACET_BATCH=256 GHOST_B_GLIREL_BATCH=512`.
   Confirm `http://localhost:8084/health` shows `"gliner":{"backend":"torch",...}`
   and `warm:true`.

## Phase 1 — ONNX environment (isolated venv so production can't break)

3. Create `E:\polymath_v3.3\.venv_onnx` (python 3.11+). Install the same deps as
   `.venv_sidecar` (torch cu130 wheel included, fastapi/uvicorn/gliner/glirel etc.)
   PLUS `onnxruntime-gpu`. Resolution ladder — stop at the first rung where Phase 3
   verification passes:
   - a. Latest stable `onnxruntime-gpu` from PyPI. If ORT can't find CUDA-12
     runtime DLLs, `pip install nvidia-cuda-runtime-cu12 nvidia-cublas-cu12
     nvidia-cudnn-cu12` (ORT ≥1.19 auto-discovers pip-installed nvidia libs).
   - b. If sm_120/CUDA-13 is the blocker: check for a CUDA-13 ORT build
     (`pip index versions onnxruntime-gpu`; ORT docs list per-CUDA wheels/indexes)
     or ORT nightly.
   - c. Plan B only if CUDA EP is unachievable: `onnxruntime-directml` in a
     separate venv (DirectML EP; watch for einsum op failures — the equivalence
     gate will catch them).
4. Selective model download (do NOT let gliner snapshot the whole HF repo — it
   pulls every quantized variant, 3 GB+):
   ```
   hf download onnx-community/gliner_medium-v2.1 --include "*.json" "spm.model" "onnx/model.onnx" "onnx/model_fp16.onnx" --local-dir E:\Polymath_Training\gliner_onnx_medium_v2.1
   ```
   (If transformers prints a "fix_mistral_regex" tokenizer warning at load time,
   ignore it — tokenization equivalence is proven by the gate in Phase 3.)

## Phase 2 — launch ONNX instance on 8086

5. From `.venv_onnx`, launch a second sidecar instance on port 8086 (same uvicorn
   command as 8084, different port) with envs:
   ```
   GHOST_B_GLINER_ONNX=1
   GHOST_B_GLINER_ONNX_REPO=E:\Polymath_Training\gliner_onnx_medium_v2.1
   GHOST_B_GLINER_ONNX_DEVICE=cuda
   GLIREL_CKPT_DIR=<same as production>
   GHOST_B_GLINER_BATCH=256 GHOST_B_FACET_BATCH=256 GHOST_B_GLIREL_BATCH=512
   ```
   Port 8086 is already inside the existing LAN-only firewall rule (8084–8091).
   Reminder: port 8090 gets squatted by Wondershare WsToastNotification.exe —
   irrelevant unless you change ports.

## Phase 3 — verification (ALL THREE must pass before any speed number counts)

6. `curl http://localhost:8086/health` — require: `gliner.backend == "onnx"`,
   `gliner.loaded == true` (wait for warmup), and `gliner.providers` contains
   `"CUDAExecutionProvider"`. If providers is `["CPUExecutionProvider"]` only,
   CUDA EP did not init — go back to the Phase 1 ladder.
7. Equivalence gate, run from `E:\polymath_v3.3\local_ghost_b` with `.venv_onnx`
   python (PowerShell syntax; adapt if using cmd):
   ```powershell
   $env:GHOST_B_GLINER_ONNX=""; python onnx_equivalence_check.py dump --out torch_rtx.json
   $env:GHOST_B_GLINER_ONNX="1"; $env:GHOST_B_GLINER_ONNX_REPO="E:\Polymath_Training\gliner_onnx_medium_v2.1"; python onnx_equivalence_check.py dump --out onnx_rtx.json
   python onnx_equivalence_check.py compare torch_rtx.json onnx_rtx.json
   ```
   Required: `RESULT: PASS`.
8. Warm speed probe with nvidia-smi watching (run twice, report the SECOND/warm
   run; do the identical POST against torch 8084 for an apples-to-apples pair):
   ```
   python -c "import json,urllib.request,time; import onnx_equivalence_check as q; tasks=[{'chunk_id':'b%d'%i,'doc_id':'bench','corpus_id':'bench','text':t,'chunk_kind':'body','columns':[]} for i,t in enumerate(q._FALLBACK_TEXTS*32)]; t0=time.time(); r=urllib.request.urlopen(urllib.request.Request('http://localhost:8086/extract',json.dumps({'tasks':tasks,'enable_facts':True}).encode(),{'Content-Type':'application/json'}),timeout=1800); d=json.loads(r.read()); el=time.time()-t0; print('wall_s',round(el,1),'ms/chunk',round(1000*el/256,1)); print('timings',d['timings'])"
   ```
   Record: wall seconds, ms/chunk, the response `timings` dict (per-stage split),
   peak GPU util % and VRAM from nvidia-smi during the run.

## Phase 4 (optional, only if Phase 3 fully passed) — quick wins

9. fp16: restart 8086 with `GHOST_B_GLINER_ONNX_FILE=onnx/model_fp16.onnx`, rerun
   step 7's onnx dump + compare (small confidence drift is expected and reported;
   gates must still PASS), rerun step 8. Report alongside fp32.
10. Forward-batch sweep on whichever file won: repeat step 8 with
    `GHOST_B_GLINER_FORWARD` = 8 (default), 16, 32 — ONNX changes the
    launch-overhead/padding tradeoff, the torch optimum of 8 may not hold.

## Report delivery (cross-device clipboard is broken — deliver through git)

Write the report to
`CONTINUITY/local_ingestion_phase_a/RTX_ONNX_REPORT.md` containing:

- pip versions: onnxruntime-gpu (and which ladder rung worked), torch
- `/health` JSON from 8086
- the full compare printout for fp32 (and fp16 if run)
- step 8 numbers for 8086 vs 8084: wall seconds, ms/chunk, timings dict,
  peak GPU util/VRAM
- forward-sweep table if run
- any deviations from these instructions

Then `git add` that one file, commit (message: "RTX ONNX experiment report"),
and `git push`. If push is rejected (no credentials on this box), fall back to
serving it over LAN instead — from the folder containing the report run
`python -m http.server 8091` and leave it running (8091 is inside the firewall
rule; the Mac will fetch http://192.168.1.83:8091/RTX_ONNX_REPORT.md). State
clearly on screen which delivery path you used. Leave both sidecar instances
(8084 torch, 8086 onnx) RUNNING — the Mac side runs its own canonical bench
against 8086 next.

Do NOT add 8086 to anything user-facing — the Mac side decides promotion after
its own 256-real-chunk bench against your instance.
