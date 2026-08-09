# Polymath E2E Runbook — what to do, in order

One page. Each phase = one thing you do (or one prompt you paste to the agent).
Skip nothing, run nothing out of order. Written 2026-08-09.

---

## Phase 0 — NOW (do nothing, ~overnight)

The 39-doc soak is draining on the Mac's GPU. It self-heals; failures park
and retry. **Do not start any other ingest/battery/gauge while it runs**
(one GPU lane — everything contends).

Ask anytime: `status of the soak?`

## Phase 1 — RTX box online (your only manual work, ~15 min)

On the RTX 6000 Pro workstation (Linux or WSL2):

1. Copy the repo onto it (or just `backend/scripts/` + `config/` keeping layout).
2. Run: `bash backend/scripts/setup_relex_sidecar_cuda.sh`
   (installs pinned deps, verifies CUDA, downloads + sha-verifies the model, serves on :8737)
3. From the Mac, sanity check: `curl http://<rtx-ip>:8737/health` → should show `"device": "cuda"`.

Then paste to the agent:

> qualify the cuda sidecar at http://<rtx-ip>:8737

The agent will: route it in qualification mode → run the burned battery +
digest comparison against it → record it qualified if clean → report.
(~30–60 min, needs the soak finished or paused.)

## Phase 2 — Flip production to the RTX (one prompt)

After qualification passes, paste:

> flip production extraction to the rtx

Effect: ingestion runs 10–30× faster. Reversible the same way
(`flip production extraction back to mps`). The flip refuses to move to
anything unqualified or with different weights — you can't break it.

## Phase 3 — Load your real library (one prompt, ~1–2 hours on RTX)

> ingest my ecommerce corpus from ~/Documents/Research/Corpora into a new corpus

118 books, one time. Summaries and alias/lexicon build automatically.
When it finishes, the agent verifies every doc (COMPLETE badge + conservation).

## Phase 4 — Use it (daily)

- Add a book anytime: upload in the UI or ask the agent. Trust the badge:
  **COMPLETE** = all three stores verified. **PARTIAL / VERIFY_FAIL** = read
  the error text, or ask `verify corpus <name>`.
- Ask questions through chat/search/graph — query-time is fast on the Mac
  regardless of engine.
- If anything looks weird: `engine status` (which GPU, busy?, pins) and
  `verify ingestion of <doc>`.

## Standing rules

1. One heavy job at a time (the encoder is a single lane per engine).
2. Never trust "done" without the COMPLETE badge or `vector_conservation.holds: true`.
3. New encoder build/device = qualification battery BEFORE production. No exceptions — the agent enforces this.
4. Deletes are real (tombstoned) — the agent always asks before deleting.

## Still queued on the agent's side (no action from you)

soak drain → O6 measurements → battery re-run → release manifest tag
(`extraction-v1`) → coordinator promotion (+ GPU batching, digest-gated).
