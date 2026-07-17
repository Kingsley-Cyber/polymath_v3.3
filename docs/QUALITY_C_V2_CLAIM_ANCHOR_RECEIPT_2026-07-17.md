# C v2 Claim-anchor Re-window Receipt — 2026-07-17

Status: **GREEN; enablement-eligible, still OFF pending owner approval**

The 19:22Z senior ruling preregistered gate v2 before this window. The new
gate preserves every quality invariant from v1, removes only the
mis-specified exact output count, requires at least 18 anchors, and requires
100% of all emitted anchors to validate and render.

The sealed v1 OFF packet was reused only after its SHA-256 reverified as
`fd02ed0abb93f4017c4adbaefaa7ad557a3454d916173f07bfd039cbbf0424e0`
and runtime flags verified relationship ON, corpus-scope v2 ON, temporal
OFF.

Results:

- 26 emitted anchors (minimum 18);
- 26/26 structurally valid;
- 26/26 rendered;
- q021: 2 rendered anchors;
- source identities byte-identical for 6/6;
- non-anchor evidence byte-identical for 6/6;
- raw claim text unchanged for 6/6;
- corpus fingerprint unchanged;
- no provider call and no store write;
- true `EXIT=0`.

Artifact:
`docs/baselines/QUALITY_C_V2_CLAIM_ANCHOR_ON_REPLAY_2026-07-17.json`

SHA-256:
`67c8fee32f28acb066e42b5e1b2850ec01d95f7ada6ddc25ee29c7908d1a4f32`

The flag was restored OFF because the senior ruling requires separate owner
approval for enablement.
