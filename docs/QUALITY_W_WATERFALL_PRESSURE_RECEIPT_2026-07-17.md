# W Waterfall Pressure Receipt — 2026-07-17

Status: **RED — feature remains OFF**

The senior-preregistered six-query bridge set ran at exactly 1,500 tokens
per query. The run made zero synthesis calls and zero corpus writes.

Results:

- executions: 6/6;
- packet hashes stable across the required repeat: 6/6;
- bridge quality and evidence selection preserved: 6/6;
- hydration telemetry recorded: 6/6;
- full decisions: 13;
- summary decisions: 5;
- skip decisions: 0;
- wrapper: `EXIT=1`.

Acceptance required at least one `summary` and at least one `skip`. The
summary requirement passed; the skip requirement failed. The only
preregistered fallback to 750 tokens was not authorized because the 1,500
artifact was not all-full.

Artifact:
`docs/baselines/QUALITY_W_WATERFALL_PRESSURE_1500_2026-07-17.json`

SHA-256:
`cac253aa78137d5baefa3df80f8b152d85203c05296e7bd135b18531bd493b7a`

Runtime returned to waterfall OFF with relationship allocation and
corpus-scope v2 ON and temporal OFF.
