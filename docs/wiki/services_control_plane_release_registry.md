# release_registry

Source `backend/services/control_plane/release_registry.py` (253 lines) · subsystem [control-plane](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Fail-closed loader for the active release registry (release_pins.v1.json).

Synthesis: imported library module; first docstring sentence: “Fail-closed loader for the active release registry (release_pins.v1.json).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `default_registry_path` | 125 | `()` |
| `resolve_registry_path` | 131 | `explicit: str \| Path \| None=None` |
| `canonical_entry_bytes` | 147 | `entry: dict[str, Any]` |
| `compute_entry_hash` | 154 | `entry: dict[str, Any]` |
| `compute_registry_hash` | 158 | `raw: bytes` |
| `load_release_registry` | 170 | `path: str \| Path \| None=None` |
| `begin_registry_run` | 248 | `path: str \| Path \| None=None` |
| `ReleaseRegistryResolution.trace_identity` | 80 | `self` |
| `RegistryRunHandle.verify_unchanged` | 104 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `datetime`, `pathlib`, `typing`, `pydantic`, `models`
- **Imports OUT** (repo-wide): `backend/scripts/canary_graph_release_gate_probe.py`, `backend/scripts/shadow_b_graph_release_gate_probe.py`, `backend/services/ingestion/graph_promotion_jobs.py`, `backend/services/ingestion/route_readiness.py`
- **Tests**: `backend/tests/test_release_registry_loader.py`, `backend/tests/test_route_readiness_certificate.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_release_registry_loader.py`, `backend/tests/test_route_readiness_certificate.py`
- Size 253 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
