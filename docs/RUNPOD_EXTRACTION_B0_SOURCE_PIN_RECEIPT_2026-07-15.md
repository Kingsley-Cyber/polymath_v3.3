# RunPod extraction lockdown — B0 source/pin receipt

Date: 2026-07-15

## Verdict

**VERIFIED GREEN.** The credential-free RunPod worker now contains the strict
`polymath.runpod_local_extraction.v1` boundary and the certified
LocalExtractionV1 stack. No image, endpoint, provider, database, graph,
vector, or corpus operation occurred in B0.

The active stack is Python 3.11.15, spaCy 3.8.14 with
`en_core_web_sm` 3.8.0, GLiNER 0.2.26 with
`urchade/gliner_medium-v2.1@40ec419335d09393f298636f471328b722c6da9e`,
and deterministic Python mention selection/predicate compilation.
`LocalExtractionV1.relations=[]` is enforced under the published T8.5
`without_wins` disposition.

## Source and asset identity

- Worker closure: 13/13 declared files, zero missing/unexpected.
- Closure SHA-256:
  `e0f25225e3c17f6e6661c7a97f56ebb93f628027a64d146097cfa7e405780b4c`.
- Vendored backend parity: 8/8 exact, zero mismatches.
- Extraction vocabulary SHA-256:
  `47ea44fee2341c3cc65ef2bb4f99795947aa0c1cc9e1d55314efc7647af89612`.
- Predicate normalization SHA-256:
  `0ba7cdc3d8dd6f643e7ccce74b46f4711940947fa73020adaf130f5efd727ce8`.
- GLiNER config SHA-256:
  `a8f3c2ecc57deb70077be6940962aa60e82d861a153a5cd2839b91795968ae7d`.
- GLiNER weights SHA-256:
  `922214c0c60f7835bb5c00f52ad1769d38518d5183f85de7bc03893a8403c023`.
- Secret scan: zero findings; provider-price/route files absent.

## Gate commands and true exits

### Backend contract focus

```bash
sh -c 'PYTHONPATH=backend local_ghost_b/.venv/bin/python -m pytest -q backend/tests/test_gliner_mentions.py backend/tests/test_local_extraction.py backend/tests/test_semantic_observations.py backend/tests/test_claim_compiler.py > /tmp/runpod_b0_backend_seal.log 2>&1; echo EXIT=$? >> /tmp/runpod_b0_backend_seal.log'
```

Output tail:

```text
..................................................                       [100%]
50 passed in 5.24s
EXIT=0
```

### Worker contract focus

```bash
sh -c 'cd runpod_flash_extractor && PYTHONPATH=. ../local_ghost_b/.venv/bin/python -m pytest -q test_app.py > /tmp/runpod_b0_worker_seal.log 2>&1; echo EXIT=$? >> /tmp/runpod_b0_worker_seal.log'
```

Output tail:

```text
......                                                                   [100%]
6 passed in 1.09s
EXIT=0
```

The worker tests prove strict request shape, legacy-wire refusal, asset-drift
refusal, stable IDs/output, controlled entity selection, predicate/negation
compilation, `relations=[]`, exact offsets, and the temporal phrases
`winter 1911` and `2018 drought summer`.

### Permanent closure verifier

```bash
sh -c 'python3 scripts/verify_runpod_extraction_runtime_closure.py --json-out /tmp/runpod_b0_closure_seal.json > /tmp/runpod_b0_closure_seal.log 2>&1; echo EXIT=$? >> /tmp/runpod_b0_closure_seal.log'
```

Key output:

```text
"all_green": true
"worker_file_count": 13
"vendored_mismatch_count": 0
"secret_finding_count": 0
"source_closure_sha256": "e0f25225e3c17f6e6661c7a97f56ebb93f628027a64d146097cfa7e405780b4c"
EXIT=0
```

### Compile, format, and diff

```bash
sh -c 'python3 -m py_compile runpod_flash_extractor/app.py runpod_flash_extractor/runtime.py backend/models/extraction_registry.py backend/services/ingestion/gliner_mentions.py backend/services/ingestion/semantic_observations.py scripts/verify_runpod_extraction_runtime_closure.py > /tmp/runpod_b0_compile_seal.log 2>&1; echo EXIT=$? >> /tmp/runpod_b0_compile_seal.log'
```

```text
EXIT=0
```

Black was run against Docker-copied source under a writable `/tmp` cache:

```text
All done! ✨ 🍰 ✨
6 files would be left unchanged.
EXIT=0
```

```bash
sh -c 'git diff --check > /tmp/runpod_b0_diffcheck_seal.log 2>&1; echo EXIT=$? >> /tmp/runpod_b0_diffcheck_seal.log'
```

```text
EXIT=0
```

## Red candidate runs retained

The first candidate runs remain in `/tmp/runpod_b0_backend_focused.log` and
`/tmp/runpod_b0_worker_focused.log` (true `EXIT=1`). They exposed a hand-made
test offset at 18 rather than the source's actual 19 and a synthetic phrase
whose pinned spaCy lemma was `low`, outside the conservative normalization
registry. Only the fixtures changed.

The next worker candidate (`/tmp/runpod_b0_worker_focused_rerun.log`, true
`EXIT=1`) exposed full-spaCy DATE entities suppressing the more specific
deterministic phrase `2018 drought summer`. The general detector precedence
was corrected to specific regex spans first, then non-overlapping spaCy spans.
No threshold, label, registry, expected result, or pass condition was weakened.

The first Black command was path-invalid against stale container source; the
second encountered a read-only cache; the third correctly reported three
unformatted new files. Those files were mechanically formatted and the final
check above is green. None of these commands touched a runtime container path
outside `/tmp`.

## Scope and next gate

VERIFIED: source/pins/contracts and zero-secret closure are sealed.

INFERRED: none.

ASSUMED: none.

B1/B2 may now run from the published commit: deterministic pinned-local
reference, then immutable Flash image build/identity. The standing blue
endpoints remain untouched.
