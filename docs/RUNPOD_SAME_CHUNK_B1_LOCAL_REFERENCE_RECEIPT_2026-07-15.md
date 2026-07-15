# RunPod same-chunk B1 pinned-local reference receipt — 2026-07-15

Status: **GREEN frozen local reference from published source.**

## Outcome

The 12 preregistered same-chunk tasks were evaluated twice by the certified
pinned-local extraction runtime. Both normalized output hashes are byte
identical. This artifact is the immutable comparison half for the future live
green RunPod endpoint; it is not itself evidence of endpoint parity.

## Frozen identities and counts

- Source commit: `08385fab906cd14268f29cbf75eff92b22f9747c`.
- Spec:
  `backend/evals/runpod_same_chunk_lockdown_v1.json`.
- Spec SHA:
  `a214bff374e5684e0f4b521eb042d6e253c4b28e07846cd7bfd935ce0cdde6f8`.
- Task count: 12; task-input SHA:
  `596d5e2af36bf196b33b1c8f06127f3cc38310104fe7dd37b4582d329dfd26e3`.
- Both normalized run hashes:
  `e84e2e3da1a1d698a3fdb6005a40d4e5ddba2ee3515797fac973b345e3517df6`.
- Output counts: 12 chunks, 126 entities, 56 predicates, 14 windows,
  0 relations.
- Runtime source closure:
  `41a2c0db7aa35c7d0c30105f18df42687e0a459368d66f269f1a7248224c1415`.
- Python 3.11.15 and all 11 extraction distributions are exact.
- Provider calls, database writes, graph writes, and vector writes: all zero.
- Reference file:
  `docs/baselines/RUNPOD_SAME_CHUNK_LOCAL_REFERENCE_2026-07-15.json`.
- Reference file SHA:
  `7615ad23cf7750cc3cb6691aa7c34dbeba8ba891da50141b09d520f85fb5d590`.

## True-exit receipt

```text
PYTHONPATH=runpod_flash_extractor POLYMATH_LOCAL_FILES_ONLY=1 \
  local_ghost_b/.venv/bin/python \
  scripts/run_runpod_same_chunk_reference.py \
  --spec backend/evals/runpod_same_chunk_lockdown_v1.json \
  --out docs/baselines/RUNPOD_SAME_CHUNK_LOCAL_REFERENCE_2026-07-15.json \
  --repeats 2
EXIT=0
byte_deterministic=true; task_count=12;
run_hashes=[e84e2e3d…, e84e2e3d…]
```

Log: `/tmp/runpod_b1_local_reference_reseal.log`.

## Labels

VERIFIED: the published pinned-local runtime is byte deterministic over the
full frozen task set and performed no provider or durable-store writes.

INFERRED: none.

ASSUMED: none. Live RunPod output equivalence remains a separate gate.
