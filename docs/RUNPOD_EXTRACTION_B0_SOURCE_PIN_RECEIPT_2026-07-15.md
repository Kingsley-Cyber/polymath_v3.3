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
  `fc33a9347aa792629309a1aa9e7bb3e961b1040174abc2e9f1b45013068b6378`.
- Vendored backend parity: 8/8 exact, zero mismatches.
- Flash dependency literal: 11/11 exact pins, including the hashed spaCy
  model wheel; ranges and module-variable indirection are rejected.
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
sh -c 'PYTHONPATH=backend local_ghost_b/.venv/bin/python -m pytest -q backend/tests/test_gliner_mentions.py backend/tests/test_local_extraction.py backend/tests/test_semantic_observations.py backend/tests/test_claim_compiler.py > /tmp/runpod_b0_backend_dependency_reseal.log 2>&1; echo EXIT=$? >> /tmp/runpod_b0_backend_dependency_reseal.log'
```

Output tail:

```text
..................................................                       [100%]
50 passed in 5.38s
EXIT=0
```

### Worker contract focus

```bash
sh -c 'cd runpod_flash_extractor && PYTHONPATH=. ../local_ghost_b/.venv/bin/python -m pytest -q test_app.py > /tmp/runpod_b0_worker_dependency_reseal.log 2>&1; echo EXIT=$? >> /tmp/runpod_b0_worker_dependency_reseal.log'
```

Output tail:

```text
......                                                                   [100%]
6 passed in 1.10s
EXIT=0
```

The worker tests prove strict request shape, legacy-wire refusal, asset-drift
refusal, stable IDs/output, controlled entity selection, predicate/negation
compilation, `relations=[]`, exact offsets, and the temporal phrases
`winter 1911` and `2018 drought summer`.

### Permanent closure verifier

```bash
sh -c 'python3 scripts/verify_runpod_extraction_runtime_closure.py --json-out /tmp/runpod_b0_closure_dependency_reseal.json > /tmp/runpod_b0_closure_dependency_reseal.log 2>&1; echo EXIT=$? >> /tmp/runpod_b0_closure_dependency_reseal.log'
```

Key output:

```text
"all_green": true
"worker_file_count": 13
"vendored_mismatch_count": 0
"secret_finding_count": 0
"dependency_pin_count": 11
"dependency_pins_exact": true
"source_closure_sha256": "fc33a9347aa792629309a1aa9e7bb3e961b1040174abc2e9f1b45013068b6378"
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

The first B2 build returned `EXIT=0` but its manifest truthfully reported zero
dependencies because Flash 1.18 does not resolve a module constant used in the
decorator. That artifact (SHA `4782b420…`) was rejected before deployment.
The same 11 exact pins were moved inline, the permanent closure verifier gained
an AST assertion for their exact ordered literal, and B0 reran from scratch:
backend 50/50, worker 6/6, closure `EXIT=0`. No dependency changed.

## Scope and next gate

VERIFIED: source/pins/contracts and zero-secret closure are sealed.

INFERRED: none.

ASSUMED: none.

B1/B2 may now run from the published commit: deterministic pinned-local
reference, then immutable Flash image build/identity. The standing blue
endpoints remain untouched.

## 2026-07-15 custom-image source reseal after Flash artifact rejection

The Flash artifact was not deployable: Flash 1.18 excludes torch and its
immutable base is Python 3.12 over RunPod torch 2.9.1. The senior-approved
preferred route is now a standalone custom image. Adding only the exact
RunPod queue envelope to `app.py` changed the source closure, so B0 reran from
scratch before any image build.

The new closure is
`41a2c0db7aa35c7d0c30105f18df42687e0a459368d66f269f1a7248224c1415`:
13/13 files, 8/8 vendored bytes exact, 11/11 endpoint pins exact, zero secret
findings, backend 50/50, worker 6/6, and standalone handler 3/3. The custom
image contract verifier binds a hashed 147-distribution lock, official Python
3.11.15 index + amd64 child digests, offline model bake, non-root runtime, and
source identity labels. BuildKit's final static check is warning-free.

Permanent feasibility receipt:
`docs/RUNPOD_CUSTOM_IMAGE_B0_FEASIBILITY_RECEIPT_2026-07-15.md`. No image,
registry, endpoint, provider, or corpus operation occurred in this reseal.
