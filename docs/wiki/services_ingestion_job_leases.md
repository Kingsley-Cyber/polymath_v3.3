# job_leases

Source `backend/services/ingestion/job_leases.py` (524 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Lease and exhaustion helpers for durable ingestion repair queues.

Synthesis: imported library module; first docstring sentence: “Lease and exhaustion helpers for durable ingestion repair queues.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `lanes_lost` | 47 | `corpus_id: str` |
| `normalize_failure_class` | 53 | `value: Any` |
| `acquire_lane_lease` | 64 | `db: Any, *, corpus_id: str, lane: str, owner: str, now: datetime \| None=None, lease_seconds: int=DEFAULT_LANE_LEASE_...` |
| `release_lane_lease` | 147 | `db: Any, *, corpus_id: str, lane: str, lease_id: str, now: datetime \| None=None` |
| `renew_lane_lease` | 175 | `db: Any, *, corpus_id: str, lane: str, lease_id: str, lease_seconds: int=DEFAULT_LANE_LEASE_SECONDS, now: datetime \|...` |
| `corpus_lane_lease` | 206 | `db: Any, *, corpus_id: str, lane: str, owner: str, lease_seconds: int=DEFAULT_LANE_LEASE_SECONDS, adopt_prefix: str \...` |
| `lease_deadline` | 277 | `now: datetime \| None=None, *, lease_seconds: int=DEFAULT_JOB_LEASE_SECONDS` |
| `reclaim_expired_running_jobs` | 285 | `db: Any, *, collection_name: str, corpus_id: str, user_id: str \| None=None, now: datetime \| None=None, lease_second...` |
| `claim_runnable_jobs` | 337 | `db: Any, *, collection_name: str, jobs: list[dict[str, Any]], runnable_statuses: set[str] \| tuple[str, ...] \| list[...` |
| `retire_superseded_jobs` | 445 | `db: Any, *, collection_name: str, jobs: list[dict[str, Any]], identity_fields: tuple[str, ...], supersedable_statuses...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `logging`, `re`, `contextlib`, `datetime`, `typing`, `uuid`, `pymongo`, `pymongo`
- **Imports OUT** (repo-wide): `backend/scripts/semantic_gateway_mark_atomic_b4.py`, `backend/scripts/semantic_gateway_mark_paid_pass.py`, `backend/scripts/semantic_gateway_mark_prose_phase2.py`, `backend/scripts/semantic_gateway_mark_sentence_hybrid_canary.py`, `backend/services/ingestion/batches.py`, `backend/services/ingestion/corpus_commander.py`, `backend/services/ingestion/document_pipeline_jobs.py`, `backend/services/ingestion/extraction_jobs.py`, `backend/services/ingestion/fleet_status.py`, `backend/services/ingestion/graph_promotion_jobs.py` …
- **Tests**: `backend/tests/test_job_leases.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_job_leases.py`
- Size 524 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (524 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “2. Batch/buffer size knobs”, “2. Job status state machine table”, “`backend/services/ingestion/extraction_jobs.py`”, “`backend/services/ingestion/job_leases.py`” — link into the map, do not duplicate it.
- Defect-fix commits: 5 — e.g. “474b076 fix: widen lane adoption threshold to 420s — heartbeats share the runner's event loop, so book-scale sync phases stall beats 400s+ while alive; 180s invited fenced-but-wasteful busy-steals”. Full list: `git log --all --oneline -- backend/services/ingestion/job_leases.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

## VERIFY

Drift check — grep the repo and confirm these still hold (run `docs/wiki/verify_claims.py`):

- `DEFAULT_LANE_ADOPT_STALE_SECONDS = 420.0` → backend/services/ingestion/job_leases.py:35
- `DEFAULT_JOB_LEASE_SECONDS = 15*60` → backend/services/ingestion/job_leases.py:23
- `DEFAULT_LANE_LEASE_SECONDS = 30*60` → backend/services/ingestion/job_leases.py:25
- `dead_letter at attempt_limit_exhausted` → backend/services/ingestion/job_leases.py:387-403