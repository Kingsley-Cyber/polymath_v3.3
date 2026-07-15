# RunPod custom image B2 local attestation receipt — 2026-07-15

Status: **GREEN local linux/amd64 image; registry publication and B3 deploy not
yet executed.**

## Outcome

The exact custom-image source published at commit
`08385fab906cd14268f29cbf75eff92b22f9747c` built successfully for
linux/amd64. Inspection from inside that image verifies the certified Python,
package, model, registry, and source identities. The image runs as a non-root
user and the standalone queue handler rejects malformed envelopes without
starting inference.

This closes the local portion of B2. It does **not** claim a deployable remote
identity: the local manifest-list identifier is not a registry digest. A
registry push, remote digest inspection, blue-green endpoint deployment, and
GPU canary remain downstream gates.

## Build identity

- Source commit:
  `08385fab906cd14268f29cbf75eff92b22f9747c`.
- Source closure:
  `41a2c0db7aa35c7d0c30105f18df42687e0a459368d66f269f1a7248224c1415`.
- Local tag: `king2eze/polymath-local-extraction:08385fa`.
- Local manifest-list identifier:
  `sha256:d3620d8513c1d3b657740975eb08675065b3c1467cf8be2caa380415900b1a0f`.
- linux/amd64 image manifest emitted by BuildKit:
  `sha256:ef7a286dd2365b19d5a71d98d6e44ed647cf4774aba4f978a98b3e6d54e878a9`.
- Image config:
  `sha256:6fa4eeb5fdd5409438d893ae71bed833d2d0852a9c6e389bb54bf3d613687751`.
- Platform: `linux/amd64`; size: 5,864,789,051 bytes.
- Runtime user: `10001:10001`; command: `python app.py`.
- Base linux/amd64 child label:
  `sha256:28255a3ace7eb4c48bc1b57b90af29e1bc82b4fd6c60614a8e3dce61b87ff941`.
- Contract label: `polymath.runpod_local_extraction.v1`.

## Runtime and asset attestation

Inside the image:

- Python is exactly `3.11.15`, machine is `x86_64`, and UID is `10001`.
- Distribution metadata is exact for all 13 critical/runtime pins, including
  `torch==2.12.0`, `transformers==4.57.6`, `gliner==0.2.26`,
  `spacy==3.8.14`, `en-core-web-sm==3.8.0`, `runpod==1.10.1`, and
  `runpod-flash==1.18.0`.
- Import-time torch identity is `2.12.0+cu130`; its compiled CUDA runtime is
  13.0. `torch.cuda.is_available()` is false in the local Docker Desktop
  emulator, as expected for this non-GPU gate. GPU compatibility remains a
  mandatory live canary condition.
- GLiNER revision is
  `40ec419335d09393f298636f471328b722c6da9e`; config SHA is
  `a8f3c2ecc57deb70077be6940962aa60e82d861a153a5cd2839b91795968ae7d`
  and weights SHA is
  `922214c0c60f7835bb5c00f52ad1769d38518d5183f85de7bc03893a8403c023`.
- Both extraction registry hashes and all 13 source-file hashes reproduce the
  published source closure.

## True-exit receipts

```text
docker buildx build --progress plain --platform linux/amd64 --load \
  --build-arg POLYMATH_SOURCE_COMMIT=08385fab906cd14268f29cbf75eff92b22f9747c \
  --build-arg POLYMATH_SOURCE_CLOSURE=41a2c0db7aa35c7d0c30105f18df42687e0a459368d66f269f1a7248224c1415 \
  -t king2eze/polymath-local-extraction:08385fa \
  -f runpod_flash_extractor/Dockerfile.locked .
EXIT=0
BuildKit bake: Python 3.11.15; source closure exact; model hashes exact.

docker image inspect --format='<selected identity/config JSON>' \
  king2eze/polymath-local-extraction:08385fa
EXIT=0
architecture=amd64; os=linux; user=10001:10001; labels exact.

docker run --rm --platform linux/amd64 --entrypoint python \
  king2eze/polymath-local-extraction:08385fa -c '<runtime metadata + bake report>'
EXIT=0
Python/package/model/registry/source identities exact.

docker run --rm --platform linux/amd64 --entrypoint python \
  king2eze/polymath-local-extraction:08385fa -c '<torch + handler boundary checks>'
EXIT=0
torch=2.12.0+cu130; compiled CUDA=13.0; three malformed envelopes rejected.
```

Logs: `/tmp/runpod_custom_image_build.log`,
`/tmp/runpod_custom_image_inspect.log`,
`/tmp/runpod_custom_image_attest.log`, and
`/tmp/runpod_custom_image_handler_attest.log`.

## Labels

VERIFIED: the local linux/amd64 image reproduces the published exact-runtime
contract and all bake identities with true `EXIT=0`.

INFERRED: none.

ASSUMED: none. In particular, no remote image digest, GPU compatibility, or
RunPod deployment state is inferred from the local build.

## Next gate

Publish the already-verified image only after the external registry boundary
is authorized, inspect the registry's immutable digest, and deploy a new green
RunPod endpoint by that digest. Existing blue endpoints remain untouched.
