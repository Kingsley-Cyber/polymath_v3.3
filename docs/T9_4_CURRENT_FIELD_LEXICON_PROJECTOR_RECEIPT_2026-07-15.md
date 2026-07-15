# T9.4 Current-Field Lexicon Projector Receipt — 2026-07-15

Status: **VERIFIED GREEN for the pure current-field implementation only**.

This receipt does not claim live engine-output parity, a corpus-scale RunPod
gate, deployment, readiness wiring, production acceptance, or the existence of
future P2.1/P2.2 semantic-profile/DF/admission fields.

## Scope and outcome

- **VERIFIED:** `candidate_artifact_to_lexicon_row` converts one accepted
  `candidate_extraction_artifact.v1` into the existing lexicon projector's row
  shape. It does not implement a second vocabulary path.
- **VERIFIED:** `build_document_lexicon_sources` retains the existing durable
  `ghost_b_extractions` query when no explicit candidate artifacts are passed.
- **VERIFIED:** explicit artifacts are checked for corpus/document ownership,
  unique and present chunk identity, the exact shared contract hash, accepted
  status, and a current source-text SHA-256 before projection.
- **VERIFIED:** the projector contains no engine or engine-provenance branch.
  Engine/runtime/model identity is absent from the materialized
  query-translation contract.
- **VERIFIED:** equal strict semantic inputs labeled cloud, local,
  legacy-local/RTX, and RunPod produce field-identical document sources and
  materialized entries after excluding only nondeterministic `updated_at`.
- **VERIFIED:** the comparison exercises non-empty co-occurrence,
  parent-derived contextual usage, exact source-backed factual-relation
  evidence, retrieval gloss, and representation eligibility.
- **VERIFIED:** no provider call, live collection read/write, projection,
  readiness change, image rebuild, restart, endpoint mutation, or spend
  occurred.

## Exact implementation boundary

The adapter copies entity/relation/fact candidate values into the existing
projector input. Relation and fact `evidence_phrase` values are reconstructed
only from artifact evidence whose method is `exact_source_substring`. The
projector then performs the same deterministic identity, alias, definition,
structural-context, contextual-usage, relation, co-occurrence, gloss, and
representation-admission work regardless of the artifact's engine.

The test intentionally gives every engine label the same semantic extraction
payload. Therefore its conclusion is projector engine-blindness, not that
different models empirically extract identical candidates.

## Acceptance receipts

| Gate | Actual command boundary | Result | True exit |
|---|---|---:|---:|
| Host focused | `pytest` on the two candidate-projector tests | 2/2 passed | 0 |
| Backend canonical | Full `test_corpus_lexicon.py` + `test_extraction_artifact.py` from exact `/tmp` overlay | 30/30 passed; 7 existing warnings | 0 |
| Worker canonical | Same exact overlay suite in ingest worker | 30/30 passed; 7 existing warnings | 0 |
| Engine-blind audit | Static asserting audit of projector/adapter/test scope | PASS; 0 engine branches | 0 |
| Scoped formatting | Black checks limited to changed regions in three files | unchanged | 0 |
| Compile | Host plus backend and worker canonical compile gates | PASS | 0 |
| Diff | `git diff --check` | PASS | 0 |

Accepted final logs:

- `/tmp/t94_projector_host_focused_v3.log`
- `/tmp/t94_projector_backend_final.log`
- `/tmp/t94_projector_worker_final.log`
- `/tmp/t94_projector_engine_blind_audit.log`
- `/tmp/t94_projector_black_scope_corpus.log`
- `/tmp/t94_projector_black_scope_adapter.log`
- `/tmp/t94_projector_black_scope_test.log`
- `/tmp/t94_projector_host_compile_final.log`
- `/tmp/t94_projector_diff_check_final.log`

Final log SHA-256 values:

- backend final: `8d531b8bb2afb2ee2ef5a35b5a54b2075ea088d0eb37e374afafb0dc2688fb0b`
- worker final: `56c5faf4ede0407fe6109986e5996ee7ade14a4f5d5a3b815dc67b25f0c189c1`
- engine-blind audit: `8da2f2e6edc57686202f9045378fa927a03dfa58c424173d6d894ed00ea630cf`

## Failure-path evidence

- **VERIFIED:** wrong document ownership rejects.
- **VERIFIED:** duplicate artifact chunk identities reject.
- **VERIFIED:** an artifact for a chunk absent from the document rejects.
- **VERIFIED:** shared-contract hash drift rejects.
- **VERIFIED:** source-text checksum drift rejects.
- **VERIFIED:** failed artifacts reject.

Earlier non-green diagnostics remain evidence rather than accepted gates:

- The full host-v3.3 venv run was 27 passed / 3 failed because two existing
  tests require container settings and Pydantic 2.13.4 derives a different
  generated schema hash than the pinned deployed Pydantic 2.5.0 runtime.
- The first backend overlay invocation selected baked `/app` ahead of the
  overlay and failed collection. Running from `/tmp` with the overlay first in
  `PYTHONPATH` corrected invocation precedence without a code or image change.
- One strengthened test initially looked for source-row `relations` on the
  final materialized entry, whose canonical field is `factual_relations`; only
  that test assertion changed.

## Labels and remaining work

- **VERIFIED:** current-field engine-blind projector implementation is green.
- **INFERRED:** future projector fields will preserve provider neutrality only
  if they are added to this same shared projector, as the plan requires.
- **ASSUMED:** none for the green implementation result.
- **OPEN:** empirical legacy-local versus RunPod output comparison on identical
  real chunks.
- **OPEN:** P2.1/P2.2 usage-frame, semantic-profile, DF/specificity, and related
  admission extensions.
- **OPEN:** pinned RunPod artifact, canary/100/500/5,000 gates, retry safety,
  readiness wiring, burst execution, and production-ready acceptance.
