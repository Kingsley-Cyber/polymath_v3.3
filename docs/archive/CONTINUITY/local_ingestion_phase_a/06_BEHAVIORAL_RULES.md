# Behavioral Rules — agent protocol for Phase A

These are user-set rules from prior sessions. Read carefully. They shape HOW to work, not just WHAT to build.

## Rule 1: Think before executing (load-bearing)

**Source**: user direct instruction, 2026-06-08, stored in `~/.claude/projects/-Applications/memory/feedback_think_before_executing.md`.

Before executing a request — especially anything involving:
- Model / dependency swaps
- Multi-step pipelines that take real wall time (>30 s)
- Destructive ops (deleting weights, wiping caches, killing long-running processes)
- Requests grounded in a stated hypothesis ("X will fix Y because Z")

**Predict the outcome first. State the prediction in one or two sentences with the reasoning. Then ask whether to proceed or recommend an alternative.**

Default: act on simple, reversible, single-step commands without ceremony. Reserve the prediction step for requests where misjudgment costs real time or trust.

For multi-step pipelines that take >30 s of real work: give a one-line forecast: expected outcome + recovery path if the prediction is wrong.

## Rule 2: Honor locked decisions

Every entry in `00_LOCKED_DECISIONS.md` was explicitly confirmed by the user. Do not reopen them. If something looks wrong, flag it and ask before changing.

## Rule 3: Don't re-run settled experiments

`04_PRIOR_EXPERIMENTS.md` lists what was tried and rejected. Notable: SLM models were all dropped; don't re-test them. GLiREL v2 regressed; v1 is the keeper.

## Rule 4: Commit author identity

Use `git -c user.name="Kingsley" -c user.email="ezeokonkwokingsley@gmail.com"` on every commit. The system default ("King <king@Kings-Mac-Studio.local>") is wrong.

Co-author trailer required: `Co-Authored-By: Claude Opus <version> <noreply@anthropic.com>`.

## Rule 5: Don't push without user OK

`git push origin main` is shared-state. Confirm with user before pushing each commit batch.

## Rule 6: Additive over destructive

When in doubt, leave deprecated code in place rather than deleting. The user has reverted entire experimental sessions before; preserving optionality matters.

Specifically: leave `slm_enrich_mlx`, `slm_enrich.py` adapter, and cloud `ghost_b.py` in place. They're orphaned in the new design but not deleted.

## Rule 7: Predict scope honestly

If a request is bigger than one turn's worth of work, say so. Don't dive in and hope to finish. Lay out phases.

Phase A is 7 tasks ≈ 10 hours. That's multi-turn. Pace accordingly.

## Rule 8: Predict outcome before model/library swaps

Specifically called out by user. The user noticed when an agent swapped Qwen for LFM2 without predicting "the prompt-anchoring failure mode applies to both" — and got annoyed. ALWAYS predict whether a swap will help based on prior evidence.

## Rule 9: Polymath project conventions

- Commit message style: short imperative title (~60 chars), no period. Match repo log examples: "Tune embedding batches for MLX", "Add fine-tuned GLiREL relation classifier with env switch"
- File paths: absolute when possible
- Bash tool: prefer dedicated tools (Read, Edit, Write) over shelling out to `cat`, `head`, `sed`, `awk`, `echo`
- Don't use `git add -A` or `git add .` — stage explicit files

## Rule 10: Pipeline config is single source of truth

`local_ghost_b/pipeline_config.py` is canonical. Bump `PIPELINE_VERSION` when changing locked values. Don't add new env vars when a constant in pipeline_config will do.

## Rule 11: Auto-pilot mode

User has said this Phase A should be runnable on auto-pilot ("switch model to opus 4.8 to run auto"). This means:
- The continuity folder must be self-sufficient
- Tasks must have explicit acceptance criteria
- Don't ask "should I start?" — just predict + state + proceed unless there's a real branch point
- If you hit a branch point, write it to a `BLOCKED.md` in this folder + stop

## Rule 12: Memory files (~/.claude/projects/-Applications/memory/)

The user's persistent memory across sessions. Read at session start:
- `feedback_think_before_executing.md` — Rule 1 above
- `project_polymath_ghost_b_predicate_floor.md` — predicate floor knowledge
- `reference_glirel_python311_mac.md` — Python 3.11 required for glirel
- `feedback_hermes_config_sync.md` — Hermes-specific, mostly irrelevant for Phase A
- `project_lfm2_finetune.md` — LFM2 fine-tune project context (LFM2 dropped from local lane per user but the project exists)
- `reference_unsloth_lfm2_mac.md` — Unsloth-LFM2 setup note (also mostly out of scope)

Don't fabricate memory entries. If something deserves persistence across sessions, write a new file and link from `MEMORY.md`.

## Rule 13: User communication style

- Be honest, not deferential. The user has explicitly pushed back on hedging.
- Lead with the bottom line. BLUF format works.
- Show concrete numbers when claiming throughput or quality.
- When wrong, acknowledge plainly and recalibrate. Don't grovel.
- Use tables for multi-dimensional comparisons.
