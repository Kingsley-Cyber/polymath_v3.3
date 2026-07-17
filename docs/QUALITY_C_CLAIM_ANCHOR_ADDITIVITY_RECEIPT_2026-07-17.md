# C Claim-anchor Additivity Receipt — 2026-07-17

Status: **RED — feature remains OFF**

The OFF arm sealed the exact selected evidence for six frozen Mark queries
(plus q029's required history turn). It passed with zero anchors exposed,
the exact `anthropic/minimax-m2.7` route, unchanged corpus fingerprints, and
true `EXIT=0`.

The ON arm replayed claim attachment after final selection without another
retrieval or model call. Every invariant held:

- source identity unchanged for 6/6 queries;
- non-anchor evidence bytes unchanged for 6/6;
- raw claim text preserved for 6/6;
- corpus fingerprint unchanged;
- q021 rendered 2/2 valid anchors;
- all 26 emitted anchors were structurally valid and rendered;
- structural citation precision was 100%.

The gate nevertheless failed because the preregistered suite required
exactly 18 structural and 18 valid anchors; the replay produced 26. The
exact-count gate was not weakened or redefined.

Artifacts:

- OFF:
  `docs/baselines/QUALITY_C_CLAIM_ANCHOR_OFF_2026-07-17.json`
  (`fd02ed0abb93f4017c4adbaefaa7ad557a3454d916173f07bfd039cbbf0424e0`)
- ON replay:
  `docs/baselines/QUALITY_C_CLAIM_ANCHOR_ON_REPLAY_2026-07-17.json`
  (`979971234292c509f25de9d9c094e3e8ea8b5af96c4b737e31abe75918c81351`)

The OFF capture's seven-call two-attempt envelope was `$0.3581046`; the ON
replay made zero provider calls. The canonical runtime was restored with
claim anchors OFF, temporal OFF, relationship allocation ON, and
corpus-scope v2 ON. No corpus-bearing data changed.
