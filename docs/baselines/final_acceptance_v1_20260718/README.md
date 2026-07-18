# Final Acceptance v1 — durable run artifacts

This directory preserves the single owner-authorized 23-query final
acceptance window executed on 2026-07-18.

## Verdict

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
