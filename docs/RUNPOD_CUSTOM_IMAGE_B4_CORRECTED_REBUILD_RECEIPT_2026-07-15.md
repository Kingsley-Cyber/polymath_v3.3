# RunPod B4 Corrected Rebuild and Private Publication Receipt — 2026-07-15

Status: **GREEN corrected image identity; live B4 remains open.**

## Outcome

Published corrective source commit
`8708f37428c06dbe40f0060fc2169f1e30124038` rebuilt the certified
linux/amd64 custom image with the offline runtime cache bound to the exact bake
root. The new image passed a real enforce-runtime spaCy + GLiNER task without
an injected environment override before it was pushed. Docker Hub visibility
was reverified private and remote immutable identities exactly match local.

No RunPod endpoint, quota, provider setting, corpus, graph, vector, or database
mutation occurred during this gate.

## Immutable identity

- Tag: `king2eze/polymath-local-extraction:8708f37`.
- OCI index:
  `sha256:c03416dcc6ae1fed6fd8851e09360711edea62ccd731c0f22d80653a51edbef1`.
- linux/amd64 child:
  `sha256:2bdb966e43be2145a1f0783c8221ffe3e2d7e223a1701e990d14a7e240b270c2`.
- Config:
  `sha256:074319618324c9b45bcc2e0f0ab80554fb94ba22e08ea2eeceb80bcbea3000cb`.
- Size: 5,864,789,730 bytes; user `10001:10001`; command `python app.py`.
- Source closure: `41a2c0db7aa35c7d0c30105f18df42687e0a459368d66f269f1a7248224c1415`.
- Runtime cache env and bake report:
  `/opt/polymath/hf-cache` exactly.
- Python 3.11.15; torch 2.12.0+cu130 / compiled CUDA 13.0; all 13 critical
  distributions, model assets, registries, and source hashes exact.

## True-exit gates

| Gate | Key result | Exit |
|---|---|---:|
| source closure | 13 files exact; zero missing/unexpected/secrets | 0 |
| Docker static check | no warnings | 0 |
| custom-image contract | 147 locked distributions; all checks green | 0 |
| linux/amd64 build | exact commit/closure labels; bake containment green | 0 |
| in-image metadata/asset attestation | all exact; non-root | 0 |
| no-override runtime probe | 1 result; 2 entities; 1 predicate; 0 relations | 0 |
| handler boundary | 4 malformed envelopes refused; torch import exact | 0 |
| Docker Hub private preflight | auth 200/private; unauth 404 | 0 |
| private push | tag digest `c03416dc…` | 0 |
| remote raw-OCI rehash | index and sole amd64 child equal local | 0 |

Logs:

- `/tmp/runpod_b4_fix_source_closure.log`
- `/tmp/runpod_b4_fix_docker_check.log`
- `/tmp/runpod_b4_fix_contract_reseal.log`
- `/tmp/runpod_b4_fix_image_build.log`
- `/tmp/runpod_b4_fix_image_identity_summary.log`
- `/tmp/runpod_b4_fix_image_attest.log`
- `/tmp/runpod_b4_fix_image_runtime_probe.log`
- `/tmp/runpod_b4_fix_image_handler_attest.log`
- `/tmp/runpod_b4_fix_registry_private_preflight.log`
- `/tmp/runpod_b4_fix_image_push.log`
- `/tmp/runpod_b4_fix_remote_digest_verify.log`

## Labels

VERIFIED: local and remote immutable identities, private visibility, exact
runtime/cache/model/source contract, actual offline CPU-emulated inference,
and zero external/durable writes.

INFERRED: none.

ASSUMED: none. GPU compatibility and live RunPod health are not inferred from
the local probe; they remain mandatory B4 conditions.

## Next gate

Recreate a primary-only green endpoint from the new immutable digest under the
already approved temporary embed max 2→1 remedy. Both extraction blue
endpoints and secondary remain read-only. The runner must persist provider job
IDs before polling and abort must restore embed max 2.
