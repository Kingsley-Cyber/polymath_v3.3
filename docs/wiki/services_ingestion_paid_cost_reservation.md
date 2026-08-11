# paid_cost_reservation

Source `backend/services/ingestion/paid_cost_reservation.py` (108 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Fail-closed pre-claim cost reservation for every paid provider lane.

Synthesis: imported library module; first docstring sentence: “Fail-closed pre-claim cost reservation for every paid provider lane.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `worst_case_next_call_cost_usd` | 23 | `*, packet_input_token_upper_bound: int, max_output_tokens: int, uncached_input_usd: int \| float \| Decimal, output_u...` |
| `worst_case_authority_usd` | 53 | `*, packet_input_token_upper_bounds: Iterable[int], max_output_tokens: int, uncached_input_usd: int \| float \| Decima...` |
| `cost_reservation_allows_claim` | 89 | `*, current_ceiling_basis_usd: int \| float \| Decimal, max_call_cost_usd: int \| float \| Decimal, authorized_ceiling...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `decimal`
- **Imports OUT** (repo-wide): `backend/scripts/materialize_semantic_digest_claim_inputs.py`, `backend/scripts/semantic_gateway_mark_atomic_b4.py`, `backend/scripts/semantic_gateway_mark_atomic_preflight.py`, `backend/scripts/semantic_gateway_mark_paid_pass.py`, `backend/scripts/semantic_gateway_mark_prose_phase2.py`, `backend/scripts/semantic_gateway_mark_sentence_hybrid_preflight.py`, `backend/services/ingestion/summary_cost_control.py`
- **Tests**: `backend/tests/test_paid_cost_reservation.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_paid_cost_reservation.py`
- Size 108 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
