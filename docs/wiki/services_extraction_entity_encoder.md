# entity_encoder

Source `backend/services/extraction/entity_encoder.py` (465 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Provider-neutral entity-encoder boundary (owner-ratified 2026-08-08).

Synthesis: imported library module; first docstring sentence: “Provider-neutral entity-encoder boundary (owner-ratified 2026-08-08).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `get_entity_encoder_provider` | 365 | `()` |
| `GLiNERBiProvider.predict_entities` | 107 | `self, texts: Sequence[str], *, batch_size: int=4, threshold: float=0.5, adapters: tuple[str, ...]=()` |
| `RelexSidecarEntityProvider.predict_joint` | 184 | `self, texts: Sequence[str], *, adapters: tuple[str, ...]=()` |
| `RelexSidecarEntityProvider.predict_entities` | 258 | `self, texts: Sequence[str], *, batch_size: int=4, threshold: float=0.5, adapters: tuple[str, ...]=()` |
| `SidecarEntityProvider.predict_entities` | 327 | `self, texts: Sequence[str], *, batch_size: int=4, threshold: float=0.5, adapters: tuple[str, ...]=()` |
| `CompositeEntityProvider.predict_entities` | 423 | `self, texts: Sequence[str], *, batch_size: int=4, threshold: float=0.5, adapters: tuple[str, ...]=()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `os`, `threading`, `typing`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/entity_sidecar_server.py`, `backend/scripts/run_oracle_adapter_qual.py`, `backend/services/extraction/graphify_pipeline.py`
- **Tests**: `backend/tests/extraction/test_entity_encoder_batching.py`
- **Env vars** (name → default): `ENTITY_SIDECAR_URL`→``, `GLINER_BI_DEVICE`→`mps`, `GLINER_BI_THRESHOLD`→``, `GRAPHIFY_ENTITY_PROVIDER`→`gliner2`, `RELEX_INFER_BATCH`→`16`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_entity_encoder_batching.py`
- Size 465 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (465 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “0fbd2d0 Saturation closeout: Pareto pin confirmed, residual ledger (UNKNOWN=0), title miner”. Full list: `git log --all --oneline -- backend/services/extraction/entity_encoder.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
