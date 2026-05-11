# Session summary — 2026-05-10 Ghost B stabilisation + MLX + multi-corpus groundwork

This file is the L0 entry-point for future Claude sessions. Read this first,
then load the relevant L1 topic file from `.cog/L1/` based on the task.

## What got fixed today (12 commits, all on `origin/main` HEAD = `6077dc4`)

| commit | one-line |
|---|---|
| `6077dc4` | start.sh: bash-3.2 polling loop (was `wait -n`) |
| `4708a42` | /health alias + export-runtime cross-OS hardening (--smoke + git-bash tar) |
| `8a66b85` | installer protects verified host MLX sidecars from rsync clobber |
| `a579634` | audit failure cap 25→200, entity cap 14→18 |
| `44f308e` | section_classifier catches mid-section index pages |
| `e0d671a` | tier_chunker CPU-stall fix + per-doc timeout |
| `de6abd0` | docker-compose.yml extraction defaults synced to config.py |
| `70ccee2` | config.py defaults baked from working .env |
| `1a73846` | Apple Silicon MLX hybrid profile + installer + agent prompt |
| `77b7f1b` | **DeepSeek thinking-mode disabled for Ghost B (real root-cause fix)** |
| `d24e15c` | Frontend books-as-clusters mode (Phase-1 cards/drill) |
| `55f759b` | Backend `POST /api/graph/by-document` (overview/drill/full) |

## Net production impact

**Ghost B extraction success rate: 0.2% → 99.7%** on representative books
(Denial of Death: 846/849 chunks, 7,295 entities, 3,574 relations).

## The single most important architectural finding

`deepseek/deepseek-v4-flash` defaults to **thinking-mode ON**. Reasoning tokens
consume the entire `max_tokens` budget BEFORE any JSONL content emits, so
`message.content` stays empty. Bumping max_tokens just gives the chain-of-
thought more rope. The fix is `payload["thinking"] = {"type": "disabled"}`
at the LiteLLM call site for any `deepseek/*` model — wired in
`backend/services/ghost_b.py` lines ~2666-2674. If you ever swap to another
reasoning model (Claude extended thinking, o-series, QwQ), the disable knob
is provider-specific — wire the equivalent or budget 3-5× max_tokens.

## What's still open (next-session work)

Items #4, #5, #6 from the risk audit. The user has an implementation plan
from a reasoning LLM. Background context for each is in:
- `.cog/L1/multi-corpus-roadmap.md`

## Current `EXTRACTION_*` defaults (all baked into repo)

```
EXTRACTION_MAX_TOKENS              = 6144
EXTRACTION_RESCUE_MAX_TOKENS       = 4096
EXTRACTION_MAX_TOTAL_LINES         = 55
EXTRACTION_RESCUE_MAX_TOTAL_LINES  = 30
EXTRACTION_MAX_ENTITIES_PER_CHUNK  = 18
EXTRACTION_MAX_RELATIONS_PER_CHUNK = 20
EXTRACTION_MAX_FACTS_PER_CHUNK     = 5
EXTRACTION_FAILURE_PAUSE_PERCENT   = 25.0
EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS = 20
EXTRACTION_ERROR_AUDIT_MAX_FAILED_ATTEMPTS_PER_DOC = 200
TIER_CHUNKER_DOC_TIMEOUT_SECONDS   = 600
```

## Running stack as of session end

| service | status | notes |
|---|---|---|
| backend | healthy, image rebuilt today | thinking-disable + new defaults baked in |
| frontend | healthy, image rebuilt today | books mode live |
| mcp | healthy, image rebuilt today | latest tools.py |
| litellm | healthy | |
| mongodb / qdrant / neo4j | healthy | |

## User's Mac mini ingestion is in progress

A separate session has been ingesting on the user's Mac mini using the
post-fix code. Manager's Path showed 1 final failure of ~700+ chunks
post-fix — healthy steady state.

User plans to export the Windows-ingested corpus to Mac Studio next.
`scripts/export-runtime.sh --smoke` is the preflight; the script auto-
selects git-bash GNU tar on Windows for cross-OS portability.
