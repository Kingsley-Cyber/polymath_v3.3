# relex_gate

Source `backend/services/extraction/relex_gate.py` (329 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Precision gate for GLiNER-Relex output.

Synthesis: imported library module; first docstring sentence: “Precision gate for GLiNER-Relex output.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `new_gate_counters` | 131 | `()` |
| `gate_relations` | 187 | `raw: list[dict], *, text: str='', doc=None, chunk_id: str='', doc_id: str='', counters: dict[str, int] \| None=None` |
| `kept_only` | 327 | `gated: list[GatedRelation]` |
| `GatedRelation.kept` | 151 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `dataclasses`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/relex_gate_run.py`, `backend/scripts/relex_pr_curve.py`
- **Tests**: `backend/tests/test_relex_gate.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_relex_gate.py`
- Size 329 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
