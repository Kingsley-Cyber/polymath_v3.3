# Polymath Backend Wiki

Read-only module reference. Every page is generated from mechanical AST/grep data;
`file:line` citations are verbatim symbol locations, NOT re-derived from reading bodies.
Where behavior could not be mechanically established, pages say **NOT EXAMINED**.

- 299 module pages · 5 subsystems

Audit ground truth (execution chain, state machine, collection I/O):
[extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) — **never duplicated here, only linked**.

## Subsystem indexes

| Subsystem | Modules | Scope |
|---|---|---|
| [scripts a–l](subsystems/scripts-a-l.md) | 55 | one-shot gates/evals/backfills, a–l |
| [scripts m–z](subsystems/scripts-m-z.md) | 106 | one-shot gates/evals/backfills, m–z |
| [ingestion](subsystems/ingestion.md) | 86 | durable jobs, worker, leases, summaries, aliases, graph backfill |
| [extraction](subsystems/extraction.md) | 39 | Graphify engine: OpenIE, relation fast-path, reducer, encoders, relex sidecar |
| [control-plane](subsystems/control-plane.md) | 7 | artifact reconciler, ledger, certificates |
| [mcp](subsystems/mcp.md) | 6 | MCP stdio surface: fleet/GPU/worker tools |

## Claims layer (hold checkable claims, not descriptions)

| Page | What it holds | When an LLM should open it |
|---|---|---|
| [INVARIANTS.md](INVARIANTS.md) | one claim per line: concrete value comparisons + `file:line` + bug ref | before editing any lease/vocab/port logic |
| [VOCABULARIES/](vocabularies/) | one page per enum, values verbatim, AUTHORITY + CONSUMERS + MUST MATCH | any time two files name the same concept |
| [SPECIMENS/](specimens/) | real rendered prompt, real wire schema, request/response shape | before changing extraction prompts or schemas |
| [TRUTH_TABLES.md](TRUTH_TABLES.md) | service → port → status → owner → deploy command | before restarting/deploying anything |
| [FAILURE_LEDGER.md](FAILURE_LEDGER.md) | symptom → root cause → guarding invariant | first stop when a new symptom smells like an old one |
| [verify_claims.py](verify_claims.py) | runnable drift audit: grep pins + vocabulary parity | after any code/config edit; periodically via audit agent |

## Page template

1. Purpose · 2. Entry points · 3. Dependencies · 4. Linked scripts & configs · 5. Maintenance summary · 6. Refactor candidates · 7. Bug dependencies

## Linked configs

| Artifact | Path | Notes |
|---|---|---|
| Compose | `docker-compose.yml`, `.daily.yml`, `.heavy-ingest.yml`, `.offline-ingest.yml`, `.opskill.yml`, `.override.yml` | per-service env mapping NOT EXAMINED in this wiki |
| Env | `.env` (253 keys) | defaults per module in §3 of each page |
| Runbooks | `docs/OWNER_MANUAL_INGESTION_RUNBOOK_2026-07-17.md`, `docs/REBATCH_RUNBOOK_2026-07-14.md`, `RUNBOOK_E2E.md` | scripts referenced from docs listed in §4 |
| Skills | `~/.claude/skills/polymath-gpu-cluster/SKILL.md`, `~/.claude/skills/rtx-compute/SKILL.md` | RTX cluster + sidecar ops |
| Audit map | `docs/audit/extraction_jobs_execution_map.md` (1231 lines) | cited from §7 of audited pages |

## Repo facts (mechanical)

- Branch `graphify/remediation-freeze-v2` HEAD `b6173f5`; 1478 commits all branches.
- 299 pages: 161 scripts, 86 ingestion, 39 extraction, 7 control-plane, 6 mcp (package `__init__.py` omitted).
- 98 files have defect-fix commits in `git log --all` (counted per file in §7).
- 4 verbatim-duplicate public function bodies found by AST hash (listed in §6 of affected pages).
