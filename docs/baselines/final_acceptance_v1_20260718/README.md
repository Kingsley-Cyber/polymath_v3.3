# Final Acceptance v1 — durable run artifacts

This directory preserves the initial 23-query acceptance attempt and the
sole senior-authorized repaired rerun executed on 2026-07-18.

## State of record — repaired rerun

- Result: **RED / technically unsealable**
- Runner exit: `EXIT=2`
- Executions / retrieval repeats: `23/23`, `5/5`
- Journal-complete / technical executions: `21/23`
- Negative refusal states: `5/5`; named-guard proofs: `4/5`
- Deep / fast p50: `49.066s / 13.386s`
- Known cost subtotal: `$0.00623476`; total **UNKNOWN** because two timed-out
  helper calls have no usage receipts
- `sealed=false`, `seal=null`
- Raw journal SHA-256:
  `135d7b0586a978d27aaea574622cc9f1ca751bc61a063cb9c01e99148c67d79d`
- Raw run-log SHA-256:
  `d37df7ecacd52f125b73d3dd31907d164e125d9aa3586306826816ff2f446ee5`

The complete-stack RED was rolled back atomically. The safe live-proven flags
remain ON; all final-review flags are OFF; health is green; eval mounts are
absent; the host lock is released.

### Rerun and materialization artifacts

All gzip files were created deterministically with `gzip -n -9`.

| Artifact | SHA-256 | Purpose |
|---|---|---|
| `final_acceptance_v1_rerun.json.gz` | `b9562c9cb1b6727709475305cd2fbee7443c9d4a09f40b75d1a92fbcdb0a421d` | Complete repaired-rerun journal |
| `final_acceptance_v1_rerun.log.gz` | `cd5522e6e6e6078b370f83e8a9d73c15c3697e0e8bc286fb6252c424ad024e02` | Rerun stdout/stderr and true exit |
| `final_acceptance_rerun_preflight.log.gz` | `ebe8fff135e985043d558f8954ab4229f9d987a7b0965a8248fcfa135c5daf31` | Exact runtime/lock/spec preflight |
| `final_acceptance_rerun_deploy.log.gz` | `c15803edab3b1f8a892cc3a27891c76f08a348c903582c2e0578e52e03eab706` | Complete-stack deployment |
| `final_acceptance_rerun_rollback.log.gz` | `0daee5623cf746bbee35b9ee953a479498734fab8e31528357adee59d5c4c9f0` | Atomic RED rollback |
| `final_acceptance_rerun_rollback_attestation.log.gz` | `9864cda413014f8332289c53198677ef2b507e460b4f4c7e2cbd109a504541b9` | Final health/flag/mount attestation |
| `final_acceptance_rerun_report_verification.log.gz` | `9f232f149a5e676e519895f0f43c41388a331d24efc9232bb1ba4b9846a8c81a` | Independent report-fact and secret-prefix checks |
| `claim_materialization_import.log.gz` | `6c26a23e1e017e457c9383a019da12481b3af4008c79dba5b101632163fef8f9` | Zero-before import and protected-census receipt |
| `claim_materialization_double_export_compare.log.gz` | `9ee88a0acdeb3f1f4284b2c56eae2d99d09c6e65603af20cd25b17a314ea828f` | Byte-equality proof |
| `claim_materialization_q7_microproof.log.gz` | `3bb15e608d04c9c5a9653e1b32d3de6729fb8414151dc94fe14da0cba13dfab5` | Provider-free additive anchor proof |
| `claim_materialization_post_census.log.gz` | `95421864262b87f6dba5a8fedc45120047e47ebc4e8e09accf2ccec234d2b39a` | Global/E2E/Mark count closure |
| `claim_materialization_before_census.json.gz` | `e6ec4e648b6a584a01fd51a059addbec606833e4d72bbd8c81a9dfdeaa5cfe52` | Pre-write protected census |
| `claim_materialization_write_manifest.jsonl.gz` | `5397f6841c43ab9cf759b158593092a589d0dcdbeea35dd6246d13af280f977d` | Exact planned-ID rollback set |
| `claim_materialization_source_lineage.jsonl.gz` | `3524be0429150cd0bbea6f47671a2e5f045a5085848853bfa1a462b1de326d83` | Pinned source-lineage manifest |
| `claim_materialization_empty_backup.jsonl.gz` | `f61f27bd17de546264aa58f40f3aafaac7021e0ef69c17f6b1b4cd7664a037ec` | Zero-row target backup |

Rerun selection identity:

- query manifest:
  `a130175f341596baeca8b53a288fde4890f1e1e31c5f83e43f8c4d20a3d6807b`;
- exact query surface:
  `2a746275f9a224a7b916dbee34dfd44d424c23fb2e69230f82870d1c3861debc`;
- parent acceptance spec:
  `99f2c37bbc22ded15135afa0f113f41e1faa0dc4346f77f74c752cb4d6905c4e`.

## Historical first-attempt verdict

- Result: **RED / technically unsealable**
- Runner exit: `EXIT=2`
- Executions recorded: `23/23`
- Retrieval-only repeats recorded: `5/5`
- Journal-complete executions: `13/23`
- `sealed`: `false`
- `seal`: `null`
- Raw journal SHA-256:
  `6a28dc04d30bcda00d472550736d58fb4e22c846ca8dcc9d51c596b95e94bc46`
- Raw run-log SHA-256:
  `e8ebb901463dc8744a921acd0dbc5974c79132595ad824484b84c66b64967913`

The journal is intentionally not described as sealed: the runner only seals
when all 23 executions are journal-complete and all five repeat records are
technically valid. Ten executions had an OPEN cost ledger, so that condition
was not met.

## Compressed artifacts

All gzip files were created deterministically with `gzip -n -9`.

| Artifact | SHA-256 | Purpose |
|---|---|---|
| `final_acceptance_v1.json.gz` | `710f80b6d876d447b338bc198c4e4ec798495108c770da524b8fbeafc4161a60` | Complete raw run journal |
| `final_acceptance_v1.log.gz` | `18cdb7ff2dcb31dd25453b30e990ef256d843eba6ffaac1d1d11e44594dd5fd7` | Runner stdout/stderr and true exit |
| `final_acceptance_stack_deploy.log.gz` | `6fe5bba3193ff4af3c88da58c334a78d6c4659b4f02ed82644e7e8eb2878ca9f` | Complete-stack deployment |
| `final_acceptance_stack_attestation.log.gz` | `a56170c1e17dc589c4b6fe92ea4e8d7ab33f6a777c8e8cf68666224e26eab811` | Pre-run health/flag attestation |
| `final_acceptance_report_verification.log.gz` | `765442b4e1bbb3733a7733270a7a61840eb54e5ea824029dfb81b5a3a651c937` | Independent report-fact assertions |
| `final_acceptance_atomic_rollback.log.gz` | `03e3212617049e1fc047f8adbd6f74ab88e12dad4833078183d2d48de66c74db` | First rollback recreate |
| `final_acceptance_rollback_attestation.log.gz` | `1f96abb1fc1bae5353720163567e1da47ee9c9df63abb3a3279c8506375327cc` | First rollback attestation showing telemetry omission |
| `final_acceptance_atomic_rollback_v2.log.gz` | `2a19edc4b2e1372ea8151508e22264b7bfd71e8231ddaf3f41047fe191f5e3de` | Corrected rollback recreate |
| `final_acceptance_rollback_attestation_v2.log.gz` | `283a00f07dd28937adbc60fe9467a373022b47ff67e0503853f4f14ace602c06` | Correct flags; invalid mount-count harness exit |
| `final_acceptance_rollback_attestation_v3.log.gz` | `f3b7f044c9459622c9e5f80501313a9f26bd8b6c5ac5046ae5a216e31c32bbbe` | Final valid health/flag/mount attestation |

The first rollback recreate inherited the default-false chat-cost telemetry
value because that variable was omitted from the command. It was immediately
corrected by the second recreate. No acceptance query ran after the verdict.
The second attestation printed the correct state but returned `EXIT=1`
because its no-match mount counter rendered an empty string; the unchanged
state was re-attested with a corrected counter in v3, `EXIT=0`.

## Selection identity

- Query manifest SHA-256:
  `abdc68bb937c2c47c88eeafe918e19cbb462cf44ca9c2ec56e5ae351a6d8eac5`
- Exact query surface SHA-256:
  `ae0e7cedfe3cfb5eb9cc41361962fb7b85113eeb6defffceb51f1b43e085b24d`
- Parent acceptance spec SHA-256:
  `3ffec2b1b4de8cd2432ff3a52d3baa42935fa828d0f91564d2f6476d91a3d737`
- Price registry SHA-256:
  `9644c084f356db14a0f437ef280f2797cbe362fce264f48f5e4ca767e5f63b6d`

## Secret safety

A literal-prefix scan found no API key, bearer header, authorization header,
password field, or RunPod key in the journal or log. The candidate receipt
records only `credential_present=true`; it contains no credential value.
