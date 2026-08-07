# Control-Pack Self-Test

The pack was validated before delivery with the following checks:

- All Python files compile with `py_compile`.
- The workflow controller initializes, starts, completes, branches, and skips the semantic-rescue stage when the gold-pair recall condition is false.
- The fixture validator confirms checksums, exact entity/relation offsets, evidence containment, canonical type/predicate coverage, structural counts, and throughput-fixture size.
- The generic E2E runner was executed against a temporary mock repository; it created reports, compared baseline/candidate wall time, and verified identity/count idempotency across repeated candidate runs.
- The CPU-only policy validator was executed against a temporary candidate provider scope.
- The spaCy parse-once validator was executed against a temporary centralized parse owner and Doc-only relation consumer.
- The acceptance-gate evaluator passed a complete synthetic result satisfying all required gates.
- Immutable control-pack files are checksum-protected by `MANIFEST.json`.

These tests validate the control pack itself. They do not claim that the target repository refactor has been implemented; that proof must be produced by the staged workflow inside the target repository.
