# Local Ingestion Pipeline — Phase A Continuity Folder

**Purpose**: this folder is a complete handoff packet for any future agent picking up
the Polymath v3.3 fully-local ingestion build. Self-contained — no chat history needed.

## Read in this order

1. [`00_LOCKED_DECISIONS.md`](00_LOCKED_DECISIONS.md) — every architectural decision the user has made and the reasoning behind each. **Treat these as locked; do not re-litigate.**
2. [`01_ARCHITECTURE.md`](01_ARCHITECTURE.md) — final architecture diagram + pipeline order.
3. [`02_PHASE_A_TASKS.md`](02_PHASE_A_TASKS.md) — the 7 sub-tasks of Phase A with exact deliverables and acceptance criteria.
4. [`03_FILE_MAP.md`](03_FILE_MAP.md) — what exists in the repo today, what's new in Phase A, what's deprecated.
5. [`04_PRIOR_EXPERIMENTS.md`](04_PRIOR_EXPERIMENTS.md) — what was tried in prior sessions and the final findings. **Do not redo these experiments.**
6. [`05_QUICK_START.md`](05_QUICK_START.md) — exact commands to resume Phase A.1.
7. [`06_BEHAVIORAL_RULES.md`](06_BEHAVIORAL_RULES.md) — user-set agent rules (think-before-executing, etc.). **Critical to behavior.**

## TL;DR

- The goal: replace cloud Ghost B (LLM extraction) in `worker.py` with a fully deterministic local extractor.
- Ghost A (summaries) stays cloud — DO NOT touch it.
- Local Ghost B stack: GLiNER ×2 + GLiREL + Python rules. No SLM, no Qwen sidecar.
- 7 tasks ≈ 10 hr total work.
- HEAD currently: `53b04ca` on origin/main (Qwen GGUF sidecar is committed but DEPRECATED in the new design; leave the code, just don't call it).
