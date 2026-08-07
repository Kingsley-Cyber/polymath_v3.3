# Complex Query — Production-Break Gap Analysis (Graphify)

**Date:** 2026-08-05  
**Scope:** Holistic complex-query / multi-hop / Graph Semantic design vs production path  
**Graph:** Updated 2026-08-04 (29569 nodes, 55034 edges) — AST incremental refresh  
**Authority:** Graph Semantic E2E hard stop lifted for **bounded dark canary only**
(shadow metrics). Global activation / user-visible ranking / production migration
still **NOT_AUTHORIZED**.

---

## 1. Executive verdict

**What breaks first in production:** Enabling complex-query flags on a default deploy (without `docker-compose.override.yml`) does **nothing** — `COMPLEX_QUERY_FIXTURE_RUNTIME_ENABLED` defaults false and the executor never runs. If an operator enables fixture runtime + allowlist on production backend **expecting retrieval behavior to change**, the system **silently keeps the legacy `retrieve_planned` ranking** while burning extra Neo4j/Qdrant work in a diagnostics sidecar (`ranking_mutated: false`). The first **user-visible** break on a dark canary is more likely **synthesis 401 / empty answer stream** when the chat user's pool credential is invalid — retrieval completes, generation does not (measured and closed on fixture only via explicit `overrides.model: pool:…`).

**Second break class:** Multi-corpus Graph requests where any selected corpus lacks `use_neo4j=true` → strategy intersection **downgrades to Hybrid** before CQ traversal runs; graph paths drop to zero without failing the request — looks like “Graph mode broken” rather than a clear capability error.

---

## 2. Gap table

| id | severity | symptom in production | root cause (file/symbol) | design invariant violated | evidence |
|---|---|---|---|---|---|
| **G01** | **P0** | `/api/chat` on default deploy never runs CQ executor; diagnostics show `execution_mode: inactive` | `COMPLEX_QUERY_FIXTURE_RUNTIME_ENABLED` default `False` in `config.py`; live enable only in gitignored `docker-compose.override.yml` — absent from `docker-compose.yml` | Fixture-gated dark deploy; production activation prohibited | `config.py` `COMPLEX_QUERY_FIXTURE_RUNTIME_ENABLED`; `docker-compose.override.yml` L22–26; graphify query on planner gates |
| **G02** | **P0** | Executor runs (fixture) but returned `chunks` / rerank order unchanged; `ranking_mutated` always false | `retrieve_planned()` calls `run_complex_query_fixture()` then continues normal fusion → `finalists`; `_cq_run.mmr_selected_child_ids` written to diagnostics only, never merged into pools | Subquery DAG must be **control plane** for evidence selection, not a parallel shadow | `backend/services/retriever/__init__.py` L1840–1963 vs L5016–5017; `complex_query_runtime.py` acceptance L410, L425 (`production_behavior_changed: False`) |
| **G03** | **P0** | `ContextPacketV1`, bridges, contradictions, `AnswerVerificationV1` never reach synthesis prompt | `chat_orchestrator.py` has **zero** references to `complex_query`, `ContextPacketV1`, or `AnswerVerificationV1`; synthesis uses `retrieval.chunks` from legacy path | Post-answer verification + obligation-scoped context packet required by design | graphify path `chat_orchestrator` → `ContextPacketV1` = **no direct path**; only via `retrieve_planned` → fixture (5-hop inferred); grep chat_orchestrator |
| **G04** | **P1** | User selects Graph tier; effective tier becomes Hybrid; CQ graph traversal skipped (`trav_diag.skipped`) | `_enforce_strategy_intersection()` downgrades `qdrant_mongo_graph` → `qdrant_mongo` when any corpus lacks `default_ingestion_config.use_neo4j` | Graph tier must not silently degrade to Hybrid (`graph_authority.py` law); strategy intersection comment says “never fail” | `__init__.py` L1171–1228; graphify query on strategy intersection |
| **G05** | **P1** | Graph Semantic queries return `graph_paths_used: 0` on otherwise healthy fixture if Neo4j driver unavailable or tier downgraded | `run_complex_query_fixture()` skips traversal when `neo4j_driver is None` or no seed entities (`complex_query_runtime.py` L172–186) | Graph mode requires batched Neo4j traversal (≤2 RT); partial status preferred over silent empty graph | `complex_query_runtime.py` L170–186; closeout allows paths=0 for one-hop but not for cross-domain class |
| **G06** | **P1** | SSE retrieval OK, answer stream 401 / no tokens for real users | Answer stream uses resolved chat model (`_resolve_synthesis_route_override`); probes pass only with explicit `overrides.model: pool:provider-readiness-longcat-1` | Provider-backed E2E must not depend on probe-only overrides | `run_complex_query_generation_probes.py` L23–27, L93; closeout §Provider-backed synthesis; `chat_orchestrator.py` L7268–7282 |
| **G07** | **P1** | Operator sets `COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED=true` expecting live multi-hop; only plan hashes in diagnostics | `planner_enabled()` branch calls `plan_complex_query()` only — `execution_mode: plan_only`, `complex_query_executor_ran: false` | Global planner must not imply runtime activation without explicit fixture/global runtime wire | `__init__.py` L1565–1589; `complex_query_executor.py` L44–49, L116 |
| **G08** | **P1** | Dark canary adds production corpus to allowlist + enables fixture runtime → executor diagnostics look green but answer quality unchanged | Same as G02 — allowlist gates **execution** not **ranking adoption** | Enabling beyond `gsem-e2e-20260804a` must change retrieval outcomes, not telemetry only | `corpus_allowlisted()` + `ranking_mutated: False` contract in validation delta |
| **G09** | **P2** | Fixture chat latency ≈2× retrieval work: full `retrieve_planned` lanes **plus** CQ Wave-1 Qdrant/vocab/Neo4j sidecar | Fixture hook runs after root embed but before vocabulary lane merge; Wave-1 repeats searches (`complex_query_wave1.py`) | One root embed; no duplicate lane work per subquery | `__init__.py` L1836–1963; `complex_query_wave1.py` header; closeout perf separates retrieval vs full chat |
| **G10** | **P2** | Two incompatible “context packet” shapes: cross-domain curation packet feeds diagnostics; CQ `ContextPacketV1` is orphan | `build_cross_domain_context_packet()` builds from `finalists` + `CROSS_DOMAIN_CURATION_*`; CQ packet built inside fixture only | Single ContextPacket contract for synthesis | `context_packet.py`; `__init__.py` L4990–5004 vs fixture packet in `complex_query_runtime.py` L356–402 |
| **G11** | **P2** | Fixture corpus hidden from UI lists but reachable by explicit `corpus_ids` in chat API | `_corpus_hidden_from_user_search()` filters `list_corpora()` only | Isolated fixture must not leak into production search surfaces | `ingestion_service.py` L1358–1366; baseline §Fixture authority |
| **G12** | **P2** | Validation scripts run against wrong container → “modules missing” or flags false | `ingest-worker` uses same `./backend` image but **no** `COMPLEX_QUERY_*` env in `docker-compose.offline-ingest.yml`; CQ runs on **backend** only | Durability: backend image contains modules; runtime flags on query container | `run_complex_query_validation_delta.py` `_durability_probe()`; offline compose L34–40 |
| **G13** | **P1** | `CROSS_DOMAIN_CURATION_ENABLED=false` globally — protected anchors/MMR from complex-query fusion never replace curation finalists | Separate flag/allowlist (`CROSS_DOMAIN_CURATION_*`) from complex-query; Phase 9 wiring to obligations incomplete per baseline | One final rerank + global protect/MMR wired to obligations | `config.py` L2309+; baseline gap matrix Phase 9; `__init__.py` protected_selection timing shows 0.0 in legacy path |

---

## 3. Dependency / path findings (graphify)

### Paths that exist (fixture-only sidecar)

```
retrieve_planned() --calls--> run_complex_query_fixture() --calls--> ContextPacketV1
retrieve_planned() --calls--> run_complex_query_fixture() --calls--> AnswerVerificationV1
chat() --...--> retrieve_planned()  (SSE entry confirmed)
```

### Paths that are missing or dark

| Expected path | Status |
|---|---|
| `run_complex_query_fixture()` → `finalists` / `fuse_planned_pools()` | **Missing** — no graph edge; code confirms diagnostics-only |
| `chat_orchestrator` → `ContextPacketV1` | **Missing** — 5-hop detour through retrieve only; orchestrator never reads packet |
| `build_cross_domain_context_packet()` → `ContextPacketV1` | **Not equivalent** — different types/modules; shortest path goes through fixture, not curation packet |
| `COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED` → ranking mutation | **Dark** — ends at `plan_complex_query()` / `placeholder_results()` |
| `ingest-worker` → complex-query runtime | **Absent by design** — worker does not serve `/api/chat` |

### Key graph communities touched

- **Shelf Role Probe** — `retrieve_planned`, planned fusion, CQ hook  
- **Release Field Matching** — `complex_query_*` modules, typed contracts  
- **Dependency Path Extraction** — chat orchestrator (no CQ consumption)

---

## 4. What is safe / already fixture-gated

| Mechanism | Behavior |
|---|---|
| `COMPLEX_QUERY_FIXTURE_RUNTIME_ENABLED=false` (production default) | CQ block skipped entirely — legacy retrieval only |
| `corpus_allowlisted()` | Refuses runtime when corpus ∉ `COMPLEX_QUERY_CORPUS_ALLOWLIST` |
| `assert_fixture_safe()` | Scripts refuse corpora without `production_visible=false` + `excluded_from_user_search=true` |
| `COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED=false` | Global planner off; no plan-only leakage into ranking |
| `ranking_mutated: false` enforced in acceptance | Validation **expects** no production ranking change today |
| `graph_authority.blocked_graph_diagnostics` | Blocks empty-graph tier when no capability (stricter than strategy intersection) |
| Module durability | All `complex_query_*` modules importable from baked backend image (validation delta) |
| Restart replay / hash identity | Proven on fixture — does not imply production wire |

---

## 5. Recommended fix order (before hard-stop lift or dark canary)

1. **G02 + G03 — Wire control plane:** Merge CQ `mmr_selected_child_ids` + protected anchors into `finalists` (or replace pool input) behind `ranking_mutated=true` flag; pass `ContextPacketV1` into `chat_orchestrator` synthesis assembly.
2. **G10 — Unify context packet:** Deprecate duplicate cross-domain dict builder or map CQ packet → synthesis contract one way.
3. **G13 — Obligation-aware curation:** Connect per-obligation fusion output to global protect/MMR (Phase 9 baseline gap).
4. **G04 + G05 — Graph downgrade honesty:** Align strategy intersection with `graph_authority` — block or explicit partial status instead of silent Hybrid when Graph requested.
5. **G06 — Synthesis credential gate:** Pre-flight synthesis route before retrieval (or surface clear SSE error); document pool entry requirement per owner user — not probe-only override.
6. **G09 — Cost guard:** Short-circuit duplicate Wave-1 when CQ executor adopts shared lane outputs (single pass).
7. **G01 — Deploy contract:** Document required env block for any canary in committed compose overlay template (not gitignored override alone).
8. **G07/G08 — Activation matrix:** Enforce `complex_query_executor_ran && ranking_mutated` in canary acceptance before expanding allowlist beyond fixture.

**Do not** enable `COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED` globally until G02–G03 are merged and measured on fixture with `ranking_mutated=true`.

---

## 6. Commands run + graph status

```bash
cd /Users/king/polymath_v3.3
which graphify
# /Users/king/.local/bin/graphify

head -1 $(which graphify)  # uv tool python for graphifyy

graphify update .
# SUCCESS: 29569 nodes, 55034 edges, 1748 communities
# graph.json timestamp: 2026-08-04 ~20:43 local
# Note: --no-viz not supported on this CLI; HTML skipped (>5000 nodes)

graphify query "How does COMPLEX_QUERY_FIXTURE_RUNTIME_ENABLED connect retrieve_planned to run_complex_query_fixture and SSE chat?"
graphify query "What gates COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED and corpus allowlist in production deploy?"
graphify path "retrieve_planned" "run_complex_query_fixture"
graphify path "chat" "AnswerVerificationV1"
graphify query "Does retrieve_planned merge complex query mmr_selected_child_ids into returned chunks or only diagnostics?"
graphify query "Where does chat synthesis get ContextPacketV1 from complex query vs build_cross_domain_context_packet?"
graphify explain "run_complex_query_fixture()"
graphify path "chat_orchestrator" "ContextPacketV1"
graphify query "use_neo4j strategy intersection downgrade graph tier silent hybrid"
graphify path "build_cross_domain_context_packet" "ContextPacketV1"
```

**Graph updated:** YES (incremental AST, 4593 changed files)  
**Semantic/doc layer:** Not re-extracted (code-only update path)  
**Staleness:** CONTINUITY docs and `docker-compose.override.yml` may be newer than semantic nodes — code paths verified by direct read + AST graph.

---

## References (design truth)

- `CONTINUITY/COMPLEX_QUERY_MULTIHOP_E2E_CLOSEOUT_20260805.md`
- `CONTINUITY/COMPLEX_QUERY_MULTIHOP_IMPLEMENTATION_20260804.md`
- `CONTINUITY/COMPLEX_QUERY_SUBQUERY_BASELINE_20260804.md`
- `AGENTS.md` / `CLAUDE.md` retrieval invariants
