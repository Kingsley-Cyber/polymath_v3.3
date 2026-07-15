# RunPod custom image B0 feasibility receipt — 2026-07-15

Status: **GREEN source contract; no image built, pushed, or deployed.**

## Outcome

The first Flash 1.18 bundle is rejected: the tool always excludes torch, its
immutable GPU base is Python 3.12 over RunPod torch 2.9.1, and the generated
artifact does not attest the certified Python 3.11.15 / torch 2.12.0 runtime.
The preferred senior-approved remediation is feasible as a standalone RunPod
queue worker in a user-controlled image.

The custom image retains the exact `polymath.runpod_local_extraction.v1`
payload and result contract. It adds no database, graph, vector, summary, or
provider client. GLiREL remains inactive and `relations=[]` remains the locked
disposition.

## Frozen image inputs

- Base: official Python `3.11.15-slim-bookworm` immutable multi-arch index
  `sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba`.
- Required linux/amd64 child:
  `sha256:28255a3ace7eb4c48bc1b57b90af29e1bc82b4fd6c60614a8e3dce61b87ff941`.
- Critical packages: the original 11 exact extraction pins plus
  `runpod==1.10.1` and `runpod-flash==1.18.0`.
- Fully resolved image lock: 147 distributions, every wheel/source protected
  by `--require-hashes`; lock SHA
  `e48c019f0530bf318a319c74478bcc89684740537220b3ea5c2fe75d3c866067`.
- Dockerfile SHA:
  `def38811c628e8306691ce4c7fd466ff0f68ad5473eb54203539f010455e6c3b`.
- GLiNER revision/config/weights and both extraction registries retain their
  published exact hashes. The build downloads the exact revision, verifies
  both model files, writes an in-image bake report, and runs offline thereafter.
- Runtime runs as UID/GID 10001 with no embedded credentials.

The PyPI torch 2.12 Linux closure carries its declared CUDA 13 runtime
libraries as locked dependencies. GPU compatibility remains a live synthetic
canary gate; no inference claim is made by this source receipt.

## Source and test receipt

```text
python3 scripts/verify_runpod_custom_image_contract.py --json-out /tmp/runpod_custom_contract_v3.json
EXIT=0
critical_mismatches={}; docker checks all true; secret_findings=[]

docker buildx build --check --platform linux/amd64 -f runpod_flash_extractor/Dockerfile.locked .
EXIT=0
Check complete, no warnings found.

PYTHONPATH=runpod_flash_extractor runpod_flash_extractor/.venv/bin/python -m pytest runpod_flash_extractor/test_custom_image_handler.py -q
EXIT=0; 3 passed

PYTHONPATH=runpod_flash_extractor POLYMATH_LOCAL_FILES_ONLY=1 local_ghost_b/.venv/bin/python -m pytest runpod_flash_extractor/test_app.py -q
EXIT=0; 6 passed

PYTHONPATH=backend local_ghost_b/.venv/bin/python -m pytest -q backend/tests/test_gliner_mentions.py backend/tests/test_local_extraction.py backend/tests/test_semantic_observations.py backend/tests/test_claim_compiler.py
EXIT=0; 50 passed

python3 scripts/verify_runpod_extraction_runtime_closure.py
EXIT=0; 13 files; 8/8 vendored exact; 11/11 endpoint pins exact; zero secrets;
closure=41a2c0db7aa35c7d0c30105f18df42687e0a459368d66f269f1a7248224c1415
```

## Rejected static candidates retained

- Direct amd64 child digest without a Dockerfile platform binding: BuildKit
  checker RED, true `EXIT=1`, `/tmp/runpod_custom_docker_check.log`.
- Explicit `$TARGETPLATFORM` binding: contradictory redundant/host-platform
  warnings, true `EXIT=1`, `/tmp/runpod_custom_docker_check_v2.log`.
- Immutable multi-arch index plus labeled amd64 child: warning-free GREEN,
  `/tmp/runpod_custom_docker_check_v3.log`.

## Boundary and next gate

VERIFIED: a reproducible custom linux/amd64 image source contract is ready to
build from a published commit.

INFERRED: none.

ASSUMED: the authenticated Docker Hub namespace can accept an image only after
the necessary repository/publication authority is confirmed.

Next: commit/push the source, build locally, attest image config/packages/model
hashes and local same-chunk behavior. External registry push remains a
publication boundary and is not executed by this receipt.
