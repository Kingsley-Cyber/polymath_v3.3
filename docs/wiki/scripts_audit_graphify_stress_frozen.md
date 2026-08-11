# audit_graphify_stress_frozen

Source `backend/scripts/audit_graphify_stress_frozen.py` (531 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Audit a frozen Graphify stress namespace without rerunning extraction.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/audit_graphify_stress_frozen.py`), not a runtime service; first docstring sentence: “Audit a frozen Graphify stress namespace without rerunning extraction.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--freeze-dir`, `--policy-sha256`, `--output-dir`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 521 | `()` |
| `MatchingPolicy.name_class` | 95 | `self, gold: str, predicted: str` |
| `MatchingPolicy.triple_class` | 111 | `self, gold: dict[str, Any], predicted: dict[str, Any]` |
| `MatchingPolicy.pair_matches` | 130 | `self, gold: dict[str, Any], subject: str, obj: str` |
| `MatchingPolicy.surface_matches` | 136 | `self, predicate: str, surface: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `hashlib`, `json`, `re`, `collections`, `pathlib`, `typing`, `run_graphify_fixture_e2e`, `models`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `README.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 531 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (531 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
