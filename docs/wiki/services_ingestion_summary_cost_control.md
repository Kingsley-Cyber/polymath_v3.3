# summary_cost_control

Source `backend/services/ingestion/summary_cost_control.py` (622 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Durable, fail-closed list-price ceilings for paid summary calls.

Synthesis: imported library module; first docstring sentence: “Durable, fail-closed list-price ceilings for paid summary calls.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `parse_authority_usd` | 63 | `value: Any` |
| `usd_to_nanos` | 86 | `value: Decimal, *, rounding: str=ROUND_CEILING` |
| `nanos_to_usd` | 90 | `value: Any` |
| `normalize_api_base` | 95 | `value: Any` |
| `normalize_model_id` | 102 | `value: Any` |
| `normalize_provider` | 106 | `value: Any` |
| `load_summary_price_card` | 124 | `*, provider: Any, model: Any, api_base: Any, registry_path: Path=REGISTRY_PATH` |
| `message_input_token_upper_bound` | 199 | `messages: list[dict[str, Any]]` |
| `list_price_nanos` | 211 | `card: SummaryPriceCard, *, input_tokens: int, output_tokens: int` |
| `summary_cost_snapshot` | 604 | `db: Any, run_id: Any` |
| `SummaryCallReservation.telemetry_fields` | 236 | `self` |
| `SummaryCostController.open` | 267 | `cls, db: Any, *, run_id: Any, corpus_id: Any, user_id: Any, authority_usd: Any` |
| `SummaryCostController.reserve` | 342 | `self, *, provider: Any, model: Any, api_base: Any, messages: list[dict[str, Any]], max_output_tok...` |
| `SummaryCostController.settle` | 454 | `self, reservation: SummaryCallReservation, *, usage: dict[str, Any] \| None, failure_class: str \...` |
| `SummaryCostController.snapshot` | 565 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `json`, `uuid`, `dataclasses`, `datetime`, `decimal`, `pathlib`, `typing`, `pymongo`, `services`
- **Imports OUT** (repo-wide): `backend/services/ghost_a.py`, `backend/services/ingestion/batches.py`, `backend/services/ingestion/document_summaries.py`, `backend/services/ingestion/summary_backfill.py`, `backend/services/ingestion/summary_tree.py`, `backend/services/ingestion/summary_tree_llm.py`, `backend/services/ingestion/worker.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_deterministic_summary_tree.py`, `backend/tests/test_summary_cost_control.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_deterministic_summary_tree.py`, `backend/tests/test_summary_cost_control.py`
- Size 622 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (622 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
