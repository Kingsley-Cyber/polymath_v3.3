# RunPod custom image B2 remote publication receipt — 2026-07-15

Status: **GREEN private publication and immutable remote identity; B3 endpoint
deployment blocked by worker quota.**

## Outcome

The locally attested custom worker is published to an explicitly private
Docker Hub repository. Independent registry inspection reproduces both the
local OCI index digest and its linux/amd64 image manifest. No endpoint
referenced the image before those checks passed.

The subsequent B3 endpoint attempt was rejected by RunPod's worker-quota gate.
No green endpoint exists and both blue extraction endpoints are unchanged. A
single exact primary template remains as partial, inert deployment state.

## Private publication gate

- Repository: `king2eze/polymath-local-extraction`.
- Create response: HTTP 201; authenticated metadata: HTTP 200 with
  `is_private=true`; unauthenticated metadata: HTTP 404.
- Tag pushed: `08385fa`.
- Remote OCI index:
  `sha256:d3620d8513c1d3b657740975eb08675065b3c1467cf8be2caa380415900b1a0f`.
- Sole linux/amd64 child:
  `sha256:ef7a286dd2365b19d5a71d98d6e44ed647cf4774aba4f978a98b3e6d54e878a9`.
- Both identities exactly equal the local B2 attestation.
- Image config/history secret scan: zero credential assignments, bearer
  tokens, Mongo URIs, or private-key findings.
- No secret is baked. The image contains no provider, database, graph, or
  vector credential. RunPod API keys remain encrypted in Mongo and are
  decrypted only inside the backend credential boundary. Private-registry
  credentials are stored in each RunPod account's secret store and are not
  endpoint environment values emitted by receipts.

## Commands and true exits

```text
python3 -c '<Docker Hub create-private + authenticated/unauthenticated verify>'
EXIT=0
create_status=201; authenticated_status=200; is_private=true;
unauthenticated_status=404

python3 -c '<image config/history secret scan>'
EXIT=0
secret_findings=[]

docker push king2eze/polymath-local-extraction:08385fa
EXIT=0
08385fa: digest: sha256:d3620d8513c1d3b657740975eb08675065b3c1467cf8be2caa380415900b1a0f

python3 -c '<docker buildx imagetools inspect --raw + SHA/child compare>'
EXIT=0
index_match=true; amd64_match=true; manifest_count=2

python3 -c '<credential-helper stdout passed only to backend-container stdin>'
  -> python /tmp/runpod_green_deploy_operator.py --action registry-auth
EXIT=0
primary+secondary created; secret_values_emitted=0
```

Logs: `/tmp/runpod_registry_private_create_verify.log`,
`/tmp/runpod_custom_image_secret_config_scan.log`,
`/tmp/runpod_custom_image_push.log`,
`/tmp/runpod_custom_image_remote_digest_verify.log`, and
`/tmp/runpod_green_registry_auth_create.log`.

The untracked, never-staged operator helper was Docker-copied into `/tmp` per
the deployment protocol. Its final diagnostic SHA is
`e93c644b883368d20d952c498535ff6853897ded9bd8dedeaff11983b1820bef`.
It contains identities/configuration only and reads credentials exclusively
from encrypted settings or stdin.

## B3 failed attempt and unchanged-blue proof

```text
docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 \
  python /tmp/runpod_green_deploy_operator.py \
  --action deploy --account primary
EXIT=1
RunPod: Max workers across all endpoints must not exceed quota (10);
new endpoint allowed at most 0 workers.

docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 \
  python /tmp/runpod_green_deploy_operator.py --action census
EXIT=0
green endpoints: 0+0; blue IDs/config unchanged;
allocations per account: Qwen3 embed max=2 + blue extraction max=8.
```

- Primary blue extraction: `m2ric3stpsh11d`, unchanged.
- Secondary blue extraction: `pitae1qruu59ne`, unchanged.
- Primary inert template: `zepw9ehfnj`, exact private digest, 64 GB disk,
  exact registry-auth ID, only nonsecret runtime-control environment names.
- Secondary green template: none.
- Green endpoints: none.

## Labels

VERIFIED: repository visibility is private, remote and local image identities
are exact, secret findings are zero, both account registry credentials exist,
the B3 endpoint mutation failed before endpoint creation, and blue remains
unchanged.

INFERRED: none.

ASSUMED: none. The inert template is not described as deployed or healthy.

## Blocker

Both accounts exhaust their 10-worker quota through max allocations 2+8 even
though all endpoints have minimum zero. B3 cannot proceed with a valid green
maximum of one unless RunPod raises quota, another account is introduced, or
an authorized temporary capacity reallocation frees one slot. Canary, parity,
and fresh-corpus work remain stopped behind B3.
