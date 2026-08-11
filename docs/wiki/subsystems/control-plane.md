# control-plane modules

Module pages — subsystem index. Back to [INDEX](../INDEX.md).

7 pages:

| Page | Source | Lines | Purpose (first docstring line) |
|---|---|---|---|
| [certificate](../services_control_plane_certificate.md) | `backend/services/control_plane/certificate.py` | 368 | Query-ready certificates and the proof contract. |
| [coverage_bridge](../services_control_plane_coverage_bridge.md) | `backend/services/control_plane/coverage_bridge.py` | 110 | Turn measured organ coverage into LANE-AWARE gaps the reconciler can act on. |
| [desired_state](../services_control_plane_desired_state.md) | `backend/services/control_plane/desired_state.py` | 455 | Desired-vs-observed artifact census. Exact ID joins, never counts. |
| [extraction_organs](../services_control_plane_extraction_organs.md) | `backend/services/control_plane/extraction_organs.py` | 144 | Accountable extraction-organ contract for the canonical Graphify lane. |
| [ledger](../services_control_plane_ledger.md) | `backend/services/control_plane/ledger.py` | 359 | Durable intake + workflow ledger (`ingestion_runs`, `stage_attempts`). |
| [reconciler](../services_control_plane_reconciler.md) | `backend/services/control_plane/reconciler.py` | 759 | Artifact-driven reconciler — replaces the queue-count scheduling gate. |
| [release_registry](../services_control_plane_release_registry.md) | `backend/services/control_plane/release_registry.py` | 253 | Fail-closed loader for the active release registry (release_pins.v1.json). |