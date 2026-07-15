# RunPod Custom Image B4 First-Canary Failure Receipt — 2026-07-15

Status: **RED, diagnosed, rolled back; corrected rebuild not yet published or
deployed.** This receipt does not claim B4, B5, B6, production readiness, or
corpus-scale parity.

## Scope and authority

- Owner/senior scope: the owner-authorized RunPod finish line, with the
  primary-only quota rider and abort restoration law in `COORDINATION.md`.
- Tested surface: primary green endpoint `whs9pjd34h2hs2`, exact private image
  index `sha256:d3620d8513c1d3b657740975eb08675065b3c1467cf8be2caa380415900b1a0f`.
- Protected surfaces: extraction blue `m2ric3stpsh11d`, secondary extraction
  blue `pitae1qruu59ne`, both corpora and all canonical stores.

## Gate result

The real first valid 12-task request was submitted through the credential-
blind backend runner. The endpoint moved from queued to initializing and the
job terminated `FAILED` after approximately 19 minutes without an output
envelope. The dependent chain stopped: no invalid-control jobs, B5/B6,
provider-setting change, fresh corpus, graph write, vector write, or database
write followed.

```text
Command:
docker exec -e PYTHONPATH=/tmp/runpod-green-lockdown/backend:/tmp/runpod-green-lockdown:/app \
  -w /tmp/runpod-green-lockdown polymath_v33-backend-1 python \
  /tmp/runpod-green-lockdown/scripts/run_runpod_green_lockdown.py \
  --mode canary \
  --baseline /tmp/runpod-green-lockdown/baseline.json \
  --out /tmp/runpod-green-lockdown/b4-canary.json \
  --timeout-seconds 1800

Output tail:
RuntimeError: RunPod job terminated status=FAILED
EXIT=1

Receipt: /tmp/runpod_green_b4_canary_v3.log
```

The original runner failed evidence retention: it held the provider job ID in
memory and did not print or persist it before raising. The endpoint was then
deleted under the mandatory abort path. RunPod endpoint deletion removes the
endpoint's logs and job history, so the provider-side job ID and log stream
cannot be reconstructed. This limitation is disclosed, not converted into a
pass.

## Root-cause evidence

The failure class is **VERIFIED** as a deterministic baked-model cache-root
mismatch before inference:

- The immutable image bake report locates the locked snapshot at
  `/opt/polymath/hf-cache/models--urchade--gliner_medium-v2.1/snapshots/40ec…`.
- The image sets `HF_HOME=/opt/polymath/hf-cache`, but production
  `_model_cache_root()` ignores `HF_HOME`. Without its explicit override it
  chooses `/runpod-volume/huggingface-cache/hub` or the non-root home cache.
- A one-task preregistered probe in the exact linux/amd64 image reproduces
  `huggingface_hub.errors.LocalEntryNotFoundError` before inference with true
  `EXIT=1`: `/tmp/runpod_green_exact_image_cpu_probe_v2.log`.
- Image imports are green with true `EXIT=0`:
  `/tmp/runpod_green_local_import.log`.
- The exact same immutable image plus only
  `POLYMATH_HF_CACHE_ROOT=/opt/polymath/hf-cache` executes the real offline
  spaCy + GLiNER path successfully: one result, two entities, one predicate,
  zero relations, exact locked model/registry/source identities, provider
  calls zero, durable writes zero, true `EXIT=0`:
  `/tmp/runpod_green_exact_image_cpu_probe_fixed_env.log`.

The cache defect causing an image-local failure is **VERIFIED**. Its mapping
to the deleted cloud job's `FAILED` state is **INFERRED**, because the provider
job ID/logs were not retained. No other failure class is claimed excluded by
provider logs.

## Abort rollback

```text
Command: backend-boundary operator --action abort-green --account primary
Output: deleted_green_id=whs9pjd34h2hs2; green_remaining=0; blue_unchanged=true
EXIT=0
Receipt: /tmp/runpod_green_b4_abort_delete.log

Command: backend-boundary operator --action embed-capacity --account primary \
  --embed-workers-max 2
Output: k695blmk52oscm workersMax 1 -> 2; all other sealed fields unchanged
EXIT=0
Receipt: /tmp/runpod_primary_embed_capacity_restore_1_to_2.log

Command: backend-boundary operator --action census
Output: green=[]; primary embed max=2; both blue endpoints unchanged;
        secondary untouched; secret_values_emitted=0
EXIT=0
Receipt: /tmp/runpod_green_b4_abort_final_census.log
```

The exact-digest inert template and private registry-auth records remain; they
allocate no workers. No secret value appears in any receipt.

## Corrective seal before rebuild

- `Dockerfile.locked` now binds the runtime's existing override to the exact
  bake root: `POLYMATH_HF_CACHE_ROOT=/opt/polymath/hf-cache`.
- The bake script fails if the downloaded snapshot is outside the production
  runtime cache root.
- The custom-image contract verifier requires the cache binding.
- `scripts/probe_runpod_custom_image_runtime.py` executes one preregistered
  task through the actual enforce-runtime image path and validates identities,
  source-round-trip spans, and zero relations/writes.
- The B4 runner appends and `fsync`s the endpoint ID/provider job ID before
  polling, then records terminal status without input/output or secrets.

Pre-build gates:

| Gate | Key result | Exit |
|---|---:|---:|
| exact old image + corrected env runtime probe | 1 result; 2 entities; 1 predicate; 0 relations | 0 |
| focused lockdown runner tests | 5 passed | 0 |
| custom-image contract verifier | all green; 147 locked distributions; 0 secret findings | 0 |
| Black | 3 files unchanged | 0 |
| compile | 5 files | 0 |
| diff check | clean | 0 |

The next authorized step is to publish this corrective source, rebuild a new
immutable image from that commit, run the enforce-runtime image probe without
an injected environment override, re-attest/publish it privately, and only
then recreate green under the same primary-only quota remedy. B4 remains open.
