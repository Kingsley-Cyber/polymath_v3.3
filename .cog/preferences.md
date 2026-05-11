# User preferences (Polymath v3.3 owner)

Observed over the 2026-05-10 session. Update as new ones surface.

## Workflow

- **Vet before push by default.** When asked to commit, commit locally.
  Don't push to GitHub unless the user explicitly says "push" or
  "deploy" or similar. They sometimes say "ensure it's in the repo"
  meaning "commit it" not "push it".
- **One push gesture per intent.** Once user says "push", that
  authorisation is for the current set of pending commits, not a
  standing license to auto-push future work.
- **Auto mode is real.** When auto mode is active, take low-risk
  actions immediately without confirming. Use the briefing to
  identify the next step from context.
- **Status boards are good.** User repeatedly asked for tabular
  "done vs open" summaries. Default to tables when listing the
  state of multiple work items.

## Technical preferences

- **No DeepSeek API direct.** Goes against the global CLAUDE.md
  policy ("China-hosted, prompts logged, no opt-out"). All DeepSeek
  calls must go through LiteLLM. System will block direct
  `api.deepseek.com` requests.
- **Don't suggest re-extraction casually.** It's expensive (provider
  tokens) and slow. Always flag the cost if it's required.
- **Honour `EMBEDDING_DIMENSION` lock.** Never suggest changing the
  embedding model without a full re-index plan.
- **Backward compat matters.** Adding new endpoints alongside legacy
  ones is preferred over modifying existing signatures.

## Communication style

- **Direct, senior-engineer level.** No filler, no "great question",
  no preamble. Get to the answer.
- **Numbers and code paths.** "475 chunks at 97.4% success" beats
  "extraction mostly worked". Reference file paths and line numbers.
- **Trade-off framing.** When proposing changes, give 2-3 options
  with effort + risk + impact tiers. User picks.

## Operational facts

- **Windows host** is the primary development machine
  (`C:/Users/Sammb/Downloads/Polymath_v3.3`)
- **Mac mini** runs an active ingestion (separate Claude session)
- **Mac Studio** is the target for MLX hybrid deployment
- **GitHub**: `Kingsley-Cyber/polymath_v3.3` — origin/main
- User has direct push permission to main (force pushes blocked for
  safety; user can override)

## What user has explicitly disliked

- Speculative architectural takes (e.g. "schema lens contamination")
  when the actual issue is something simpler. User called this out:
  "WTF this is HRAG". Stick to facts grounded in code.
- Pushing without explicit go.
- The frontend-design skill — flagged in global CLAUDE.md as
  "considered poorly made".

## What user has explicitly liked

- Replay rigs that test changes before committing
  (`scripts/replay_ghost_b_chunks.py` was useful)
- Production-readable scaffolds (the MLX sidecars have wire shapes
  even though bodies are placeholders — user can drop verified code
  in without restructuring)
- Multi-tier endpoint design (`overview / drill / full` modes on
  `/api/graph/by-document`)
