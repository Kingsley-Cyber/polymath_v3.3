# RunPod custom image B3 primary green deploy receipt — 2026-07-15

Status: **GREEN primary blue-green control plane; no inference yet.**

## Outcome

A new primary RunPod endpoint was created beside the unchanged extraction
blue endpoint. It references the exact private OCI digest, has one bounded
maximum worker and zero minimum workers, and requires CUDA 13.0. Secondary is
intentionally untouched under the senior's narrowed quota ruling.

## Identities and configuration

- Primary extraction blue: `m2ric3stpsh11d`, unchanged.
- Primary green: `whs9pjd34h2hs2` (discovered from the create response).
- Green template: `zepw9ehfnj`.
- Image:
  `king2eze/polymath-local-extraction@sha256:d3620d8513c1d3b657740975eb08675065b3c1467cf8be2caa380415900b1a0f`.
- Template private-registry credential ID is present; no credential value was
  emitted.
- GPU policy: `ADA_24,AMPERE_24,-NVIDIA GeForce RTX 3090`, count 1.
- Workers: minimum 0, maximum 1; scaler `REQUEST_COUNT=1`.
- Idle timeout 60 seconds; execution timeout 1,800,000 ms.
- Minimum CUDA 13.0; FlashBoot enabled; container disk 64 GB.
- Secondary extraction blue `pitae1qruu59ne` and secondary embed
  `hlp9h3o4zd0v4d` are untouched.

## Quota reallocation receipt

RunPod's first green create rejected with true `EXIT=1` because each account's
10-worker quota was fully allocated at embed max 2 + extraction blue max 8.
The senior approved a primary-only temporary reallocation of Qwen3 embed
`k695blmk52oscm` from max 2 to max 1.

The first update-shape candidate omitted required `name` and was rejected
before mutation (`EXIT=1`). The second supplied ID/name/max but RunPod reset
unspecified defaults (`idleTimeout 60→10`, `scalerValue 1→4`); the helper
detected that drift and returned `EXIT=1`. A full-field corrective update then
returned true `EXIT=0` and proves:

- same embed ID `k695blmk52oscm`;
- same template `7p9r307t6u`;
- same min 0, idle 60, scaler `REQUEST_COUNT=1`, GPU/CUDA/FlashBoot policy;
- only maximum changed 2→1;
- extraction blue was unchanged throughout.

Binding restore rule: embed max returns to 2 at cutover or immediately on any
abort, whichever comes first, with its own before/after receipt.

## Commands and true exits

```text
docker exec ... python /tmp/runpod_green_deploy_operator.py \
  --action embed-capacity --account primary --embed-workers-max 1
EXIT=0
action=repairing-default-drift; blue_unchanged=true;
embed id/template/min/idle/scaler exact; max=1

docker exec ... python /tmp/runpod_green_deploy_operator.py \
  --action deploy --account primary
EXIT=0
green=whs9pjd34h2hs2; template=zepw9ehfnj; blue_unchanged=true;
secret_values_emitted=0
```

Logs: `/tmp/runpod_primary_embed_capacity_2_to_1_v3.log` and
`/tmp/runpod_green_primary_deploy_v2.log`. Rejected candidates remain in the
same log family with no hidden success claim.

## First-inference runner seal

The published runner `backend/scripts/run_runpod_green_lockdown.py` discovers
green by name, reads RunPod keys only inside the backend, constructs the exact
12 preregistered tasks from ID/text only, validates strict output/spans and
controlled labels, requires all invalid controls to return the named refusal,
and compares every semantic field with only the frozen `1e-5` confidence
tolerance. Focused tests are 4/4; compile, Black, and diff checks are true
`EXIT=0`. No inference was run by this receipt.

## Labels

VERIFIED: one exact primary green endpoint exists beside byte-unchanged blue;
the private image/template/capacity configuration matches the sealed contract;
secondary is untouched; no secret values were emitted.

INFERRED: none.

ASSUMED: none. B3 does not claim image pull success, GPU health, canary output,
or same-chunk equivalence; those begin at B4.
