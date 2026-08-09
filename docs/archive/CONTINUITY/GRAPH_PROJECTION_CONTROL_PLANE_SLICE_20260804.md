# Graph Projection Control-Plane Closure — ADOPTED 2026-08-04 → CLOSED

**status:** CLOSED with receipts — see `CONTINUITY/GRAPH_PROJECTION_CONTROL_PLANE_CLOSEOUT_20260804.md`  
**next_slice:** schema → corpus entity join  
**deferred:** ontology release control plane; production migration  
**prohibited:** blind full re-extraction of stuck orphans

## Lifecycle

```
PLANNED → INPUTS_VALIDATED → ONTOLOGY_RESOLVED → AUTHORIZED
→ APPLYING → APPLIED → VERIFIED → CERTIFIED
```

Terminal non-success: `NOOP_NO_ELIGIBLE_ARTIFACTS`, `ABANDONED_*`, `BLOCKED_*`, `VERIFY_FAILED`, `DEAD_LETTER`  
None of these may advertise graph capability.

## Orphan classes

`recoverable_projection` | `missing_extraction` | `superseded_generation` | `deleted_document` | `legacy_engine_or_contract` | `corrupt_or_ambiguous`

## q9 MEASURED close

- 2583 → `missing_extraction` / `REVIEW_MISSING_EXTRACTION` (no blind re-extract)
- projection jobs: 20 CERTIFIED / 18 NOOP / 2 BLOCKED_INPUT_MISSING
- advertised_mode: `graph_assertion`; restart replay identical; Fact=0 accurate
