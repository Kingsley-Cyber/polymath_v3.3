# Q9 Final Authority Manifest — 2026-08-05

Schema: `RepositoryIntentManifestV1`
Artifact: `data_eval/q9_final/authority_manifest.json`

## Resolved owner intent
- Finish Polymath for Sambenja only on the ten q9 source files.
- Clean reingest via `relex_local` + `mac_safe`, then summaries/vocab/embed/graph, then Fast/Hybrid/Graph + CQ, then restart replay, then atomic visible activation.
- Do not stop at reports/shadow telemetry.

## Authority precedence applied
1. This session's XML mandate
2. AGENTS.md / CLAUDE.md
3. Latest categorical closeouts
4. Architecture docs (incl. renamed CONTINUITY/POLYMATH_ARCHITECTURE.md)
5. Typed contracts + live code
6. Older plans as history only

## Conflicts resolved
### document_rename:ARCHITECTURE.md
- XML required ARCHITECTURE.md
- repository uses CONTINUITY/POLYMATH_ARCHITECTURE.md
- **Controlling:** CONTINUITY/POLYMATH_ARCHITECTURE.md
- **Why:** authority level 1 XML discovery_rule: locate by content; renamed path preserves architecture authority under CONTINUITY/docs

### document_rename:Overall Architecture.md
- XML required Overall Architecture.md
- repository uses CONTINUITY/POLYMATH_ARCHITECTURE.md
- **Controlling:** CONTINUITY/POLYMATH_ARCHITECTURE.md
- **Why:** authority level 1 XML discovery_rule: locate by content; renamed path preserves architecture authority under CONTINUITY/docs

### document_rename:detailed_architecture.md
- XML required detailed_architecture.md
- repository uses CONTINUITY/POLYMATH_ARCHITECTURE.md
- **Controlling:** CONTINUITY/POLYMATH_ARCHITECTURE.md
- **Why:** authority level 1 XML discovery_rule: locate by content; renamed path preserves architecture authority under CONTINUITY/docs

### document_rename:Enrichment_facets_claims_summaries_aliases_etcs.md
- XML required Enrichment_facets_claims_summaries_aliases_etcs.md
- repository uses docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md
- **Controlling:** docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md
- **Why:** authority level 1 XML discovery_rule: locate by content; renamed path preserves architecture authority under CONTINUITY/docs

### visible_complex_query_activation
- Prior closeouts: HOLD visible CQ until DeepSeek key rotation
- XML 2026-08-05: atomically activate Fast/Hybrid/Graph/Complex Query for Sambenja+q9 after gates
- **Controlling:** XML owner mandate (authority level 1)
- **Why:** current owner mandate supersedes prior hold once mandatory gates pass; key hygiene still required before enablement

### q9_corpus_identity_vs_clean_generation
- XML target_corpus_id=6a766597-29f3-4a3e-8918-5de10f0053b3
- generation_policy: build new generation alongside active; do not activate until gates pass
- engine lock: non-empty corpus cannot silently change extraction identity via ordinary config update
- **Controlling:** build sibling corpus q9_final_e2e_20260805 with identical sources/engine, keep old corpus as rollback; activate by switching Sambenja scope after gates (preserve old corpus_id artifacts)
- **Why:** satisfies alongside+rollback without mixing engines/artifacts inside one active generation; product cutover recorded in activation closeout

### summary_policy
- q9 step11 batch used chunk_summarization=false
- XML phases 4/5 require deterministic hierarchical summaries + vocabulary
- Contaminated sibling `7d801816-…` opened DeepSeek / summary cost authority as if required
- Owner 2026-08-05: required baseline = `deterministic_summary.v1`; cloud Ghost A = deprecated enrichment only (`llm_summary_enrichment.v1`) behind explicit cost authority; not removed
- **Controlling:** `CONTINUITY/INGESTION_CONTROL_PLANE_20260805.md` + XML phase 4/5 (summaries on, deterministic only for clean redo)
- **Why:** extraction/summary control layer must match architecture; prior clean-gen acceptance is void until redo under this contract

## Documents reviewed
- [OK] `AGENTS.md` → `AGENTS.md` `sha256:3c6c8517ae948082b0b9b6d51b8c261a9bc78873cd90fc9c19b97f187b17b229` level=1
- [OK] `CLAUDE.md` → `CLAUDE.md` `sha256:d4c9d9781b5ce31685fd7892629a58687aa5e67f085b7cb35960317757e7c729` level=2
- [OK] `README.md` → `README.md` `sha256:bb8f54f8bbaa9e76ea9e62f8c9ae7dcfd57e113e952e526b333765e0119df203` level=4
- [OK] `ARCHITECTURE.md` → `CONTINUITY/POLYMATH_ARCHITECTURE.md` `sha256:1725e4003294092e44920dde3924eb1542e6928897877aee1b252be3ac312349` level=4
- [OK] `Overall Architecture.md` → `CONTINUITY/POLYMATH_ARCHITECTURE.md` `sha256:1725e4003294092e44920dde3924eb1542e6928897877aee1b252be3ac312349` level=4
- [OK] `detailed_architecture.md` → `CONTINUITY/POLYMATH_ARCHITECTURE.md` `sha256:1725e4003294092e44920dde3924eb1542e6928897877aee1b252be3ac312349` level=4
- [OK] `Enrichment_facets_claims_summaries_aliases_etcs.md` → `docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md` `sha256:c58ca39decc8d28129058f4dd890e05d1b97a0062c25dfa6dacb291f50019db2` level=4
- [OK] `CONTINUITY/POLYMATH_ARCHITECTURE.md` → `CONTINUITY/POLYMATH_ARCHITECTURE.md` `sha256:1725e4003294092e44920dde3924eb1542e6928897877aee1b252be3ac312349` level=4
- [OK] `CONTINUITY/QUERY_CONTROL_PLANE_REPO_AUDIT.md` → `CONTINUITY/QUERY_CONTROL_PLANE_REPO_AUDIT.md` `sha256:3b30390fa0f469f5567586f4fafb5cfc3820c236116f209e026976556a6a39ba` level=4
- [OK] `CONTINUITY/QUERY_ARCHITECTURE_CONFLICT_MATRIX.md` → `CONTINUITY/QUERY_ARCHITECTURE_CONFLICT_MATRIX.md` `sha256:5e40d457639bf348ca44102ef323b88a0f2d67121e00673348f1195679232feb` level=4
- [OK] `docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md` → `docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md` `sha256:c58ca39decc8d28129058f4dd890e05d1b97a0062c25dfa6dacb291f50019db2` level=3
- [OK] `BUILDLINE.md` → `BUILDLINE.md` `sha256:78abc2306fb23fc16bb9f9aacb1c66e113a76661b9588e401143f68b2d8d97c4` level=3
- [OK] `CONTINUITY/COMPLEX_QUERY_DARK_QUALITY_CLOSEOUT_20260805.md` → `CONTINUITY/COMPLEX_QUERY_DARK_QUALITY_CLOSEOUT_20260805.md` `sha256:c53e0545576edc6ab099a12eb2bb15c07b599ae14a6dd113605817414a0c5784` level=3
- [OK] `CONTINUITY/COMPLEX_QUERY_CANDIDATE_ADOPTION_CLOSEOUT_20260805.md` → `CONTINUITY/COMPLEX_QUERY_CANDIDATE_ADOPTION_CLOSEOUT_20260805.md` `sha256:44b62a96d2c4af72d74ebdc72263fcae9b3951ff91dd43feddc734cd3070c15f` level=3
- [OK] `CONTINUITY/GRAPH_PROJECTION_CONTROL_PLANE_CLOSEOUT_20260804.md` → `CONTINUITY/GRAPH_PROJECTION_CONTROL_PLANE_CLOSEOUT_20260804.md` `sha256:e32fed8b57f344ac2939a0af68e813d12850ca2537f77ed017cf5814182db876` level=3
- [OK] `CONTINUITY/Q9_STEPS_10_17_CLOSEOUT_20260804.md` → `CONTINUITY/Q9_STEPS_10_17_CLOSEOUT_20260804.md` `sha256:798b8e551cd80e9924015c2f400a6a780a77626a428d90d2008f1ef4857e966e` level=3
- [OK] `CONTINUITY/Q9_STEP10_BASELINE_CLOSEOUT_20260804.md` → `CONTINUITY/Q9_STEP10_BASELINE_CLOSEOUT_20260804.md` `sha256:1188365bfeabb65e0a94b4e958e0f54e52c4398eaa5acf14df650e6beedea2f4` level=3
- [OK] `CONTINUITY/PREQ9_16GB_MEMORY_ARCHITECTURE_BASELINE.md` → `CONTINUITY/PREQ9_16GB_MEMORY_ARCHITECTURE_BASELINE.md` `sha256:38159735ce55d857d52f53be27f317997e1aaef153c2be6cc427d0369fcf79b0` level=4
