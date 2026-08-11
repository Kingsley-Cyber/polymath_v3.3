# relex_sidecar_client

Source `backend/services/extraction/relex_sidecar_client.py` (249 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Client for the host-MPS GLiNER-Relex sidecar (contract relex-infer-v1).

Synthesis: imported library module; first docstring sentence: “Client for the host-MPS GLiNER-Relex sidecar (contract relex-infer-v1).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `sidecar_url` | 24 | `()` |
| `sidecar_pool` | 66 | `()` |
| `health` | 103 | `timeout: float=5.0` |
| `infer` | 124 | `texts: Sequence[str], *, entity_labels: Sequence[str] \| None=None, relation_labels: Sequence[str] \| None=None, enti...` |
| `infer_sharded` | 179 | `texts: Sequence[str], *, batch_size: int, entity_labels: Sequence[str] \| None=None, relation_labels: Sequence[str] \...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `json`, `os`, `urllib`, `urllib`, `dataclasses`, `typing`
- **Imports OUT** (repo-wide): `backend/services/extraction/entity_encoder.py`, `backend/services/extraction/graphify_relations.py`
- **Tests**: `backend/tests/extraction/test_entity_encoder_batching.py`, `backend/tests/extraction/test_graphify_relations.py`
- **Env vars** (name → default): `RELEX_EXPECT_RELEASE`→``, `RELEX_INFER_TIMEOUT_SECONDS`→``, `RELEX_SIDECAR_POOL`→``, `RELEX_SIDECAR_URL`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_entity_encoder_batching.py`, `backend/tests/extraction/test_graphify_relations.py`
- Size 249 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “3297fc7 CUDA sidecar deployment kit: pinned config, fail-closed guard, release-pin client check”. Full list: `git log --all --oneline -- backend/services/extraction/relex_sidecar_client.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

## VERIFY

Drift check — grep the repo and confirm these still hold (run `docs/wiki/verify_claims.py`):

- `RELEX_SIDECAR_URL default host.docker.internal:8737` → backend/services/extraction/relex_sidecar_client.py:7,36-41