# GPU arbiter Q1–Q5 promotion window v3

This directory durably files the sealed raw OFF/ON journals from the
owner-approved Q promotion window completed on 2026-07-18. The JSON is
gzip-compressed without filename/timestamp headers (`gzip -n -9`) so the
repository bytes are reproducible. Decompress with `gzip -dc`.

| Arm | Raw JSON SHA-256 | Compressed SHA-256 | Canonical seal |
|---|---|---|---|
| OFF | `717345e9ce3c77fb3b174401d9651ec276096182d3ed2c1982068d0f3dfe68ef` | `a357f119efa186169656d36a1162d811a4d30f8f5a70b4703640b2eae408c018` | `3a15cc92b3532e725bdc1bd2a9c20afff7d8b4822d14484b6b22b66bfec2da65` |
| ON | `19b30432ec8ad11f398cf03396cf83902fe8f94eca1b3e757924db0f89497999` | `e9d203df804d5f64eb199d34c3088291244eee6ea283ae62fa94b2193f5de572` | `3ec79f0237657c7ba8c696edc7dc5b0f0ab66be9011f7f763057d8eaffa2c5bb` |

The ON journal records all Q1–Q5 gates green:

- Q1: embed/rerank maximum absolute difference `0.0`.
- Q2: embeds `100/100`, zero failures, p95 `1.674034875s`.
- Q3: arbiter rerank-hold p95 `531.499916ms` (ceiling `600ms`), mixed/solo
  p95 ratio `1.028566`.
- Q4: fixed-point mid-soak arbiter kill and live fail-open recovery passed.
- Q5: OFF/ON corpus selection and canonical build-suite checks passed.

The source directory was `/tmp/q_senior_window_v3`; its promotion wrapper
reported `passed=true`, seal
`3ec79f0237657c7ba8c696edc7dc5b0f0ab66be9011f7f763057d8eaffa2c5bb`,
and true `EXIT=0`. A pre-commit scan found only the literal test-only values
`AUTH_SECRET_KEY=test` and `DEFAULT_ADMIN_PASSWORD=test`; no live credential,
API key, bearer token, or encrypted settings value is present.
