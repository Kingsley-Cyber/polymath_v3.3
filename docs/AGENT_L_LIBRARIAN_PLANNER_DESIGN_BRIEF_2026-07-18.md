# Agent L — Deterministic Librarian Subquery Orchestrator: Design Brief

Senior-authored implementation brief, 2026-07-18. Governing directives:
COORDINATION 23:12Z (design law), 23:41Z (plan-time doc-summary
grounding). Owner intent: subquery setup, librarian-driven retrieval,
polymath-deterministic, latency fixed.

## 0. One-paragraph shape

Every question gets a typed, hashed, replayable PLAN before retrieval.
Deterministic rule planners handle the known question shapes; an LLM
decomposer fires only for shapes the rules cannot parse, grounded by a
Tier-0 document-summary shortlist, cached so repeats are byte-stable.
The librarian allocates evidence seats per subquery with the proven
reservation/spillover machinery; subqueries retrieve in parallel; one
batched rerank under per-subquery caps; assembly stays LLM-free;
cross-encoder stays sole scoring authority.

## 1. QueryPlanV1 (typed, Pydantic, durable)

```
plan_version: "query_plan.v1"
plan_hash: sha256(normalized_query | corpus_doc_version | planner_version)
normalized_query: str          # casefold, whitespace/punct-normalized
corpus_id: str
corpus_doc_version: str        # content hash of the corpus doc set
planner: "rule:<shape>" | "llm:v1" | "fallback:simple"
shape: relationship | comparison | temporal | enumerative_trace |
       entity_bridge | simple | complex
shortlist: [{doc_id, title, score}]          # ≤8, Tier-0 grounding
subqueries: [{
  role: main | side_a | side_b | facet | hop | time_slice,
  text: str,
  target_doc_ids: [str],       # optional, from shortlist affinity
  seat_quota: int,
  tier: fast | mongo | graph,
  rerank_cap: int
}]                              # 1..4 subqueries
refusal_signals: {shortlist_empty: bool, named_source_missing: bool}
cache: {hit: bool, key: str}
```
Plan recorded in trace metadata on every query; plans are replayable
artifacts. Same normalized question + same corpus state ⇒ identical
plan_hash and identical seat assignments (gate L-G1).

## 2. Rule planners (ordered registry; first match wins; all deterministic)

1. relationship/comparison — the LIVE classifier (unchanged); emits
   side_a/side_b subqueries; side→document targeting by deterministic
   lexical affinity of side terms against shortlist summaries.
2. temporal — temporal v1 family detector; adds a time_slice subquery
   with temporal-evidence preference (consumes the flipped temporal
   machinery; degrades to no-op while temporal is OFF).
3. enumerative/trace — surface patterns ("list", "steps", "stages",
   "trace how X becomes Y"); main + hop subqueries when the shortlist
   shows the endpoints live in different documents.
4. entity_bridge — ≥2 proper-noun entities mapping to distinct shortlist
   documents → per-entity subqueries + one bridge subquery.
5. simple — default single-subquery plan. Byte-equivalent to today's
   pipeline; zero added latency for ordinary lookups.
Unparsed/complex → LLM escalation (§4).

## 3. Tier-0 shortlist grounding (owner amendment, 23:41Z)

Before any planning that needs it: deterministic pass over the
doc_summaries surface (vector + lexical over titles/summaries — reuse
router lanes 1+2 machinery), top-N=8. Titles+summaries feed rule
targeting and the LLM decomposer prompt. Empty/irrelevant shortlist for
a named-source question sets refusal_signals — plan-time refusal
evidence for corpus_scope.v3, before retrieval spend. doc_summaries
remains the ONLY summary-vector surface (canon unchanged).

## 4. LLM escalation decomposer (bounded, cached, validated)

Input: question + shortlist (titles/summaries only). Output: subqueries
JSON validated into QueryPlanV1 (max 4; roles constrained; rejects
answers/prose). Route: fast provider, thinking disabled, temperature 0,
max_tokens ≤600. Cache key: (normalized_query, corpus_doc_version,
decomposer_prompt_hash) — new ingests lawfully invalidate once. Provider
failure → fallback:simple plan + named signal `planner_llm_unavailable`
(fail-open to today's behavior; counted per silent-fallback law).

## 5. Librarian seat allocation

Total seat budget K unchanged. Per-subquery reservation
quota_i = max(1, floor(K·w_role)); sides equal; facet/hop smaller.
Existing laws carry over verbatim: strong-match coverage, protected
per-doc cap, threshold spillover (unused reservations release). When
two-lane (T) promotes, anchor/expansion lanes apply WITHIN each
subquery's seats. Relationship per-side allocation = the 2-subquery
special case of this generalization (no parallel implementation — one
allocator). Cross-encoder remains sole scoring authority.

## 6. Parallel execution + latency budget

ONE batched embed call for all subquery texts (query-cache aware) →
concurrent Qdrant searches → chunk-id dedup (keep max-affinity subquery
tag) → ONE batched rerank over the union, bounded by Σ rerank_cap ≤
today's single-query candidate count → deterministic assembly →
single synthesis call (unchanged contract).

Stage budgets at deep tier (targets: fast ≤5s / mongo ≤10s / deep ≤15s
p50 at floors): plan ≤0.05s (rules or cache) · shortlist ≤0.3s ·
batched embed ≤0.5s · vector ≤1.5s · hydrate ≤0.5s · graph ≤2.5s ·
batched rerank ≤5s (caps sized to this) · assembly ≤0.2s · synthesis =
the remainder (separate measured lever: faster synthesis route A/B).

## 7. Flags and shadow mode

`LIBRARIAN_PLANNER_ENABLED` default False (behavior flip).
`LIBRARIAN_PLANNER_SHADOW` default False; when True, plans are computed
and logged in trace with ZERO behavior change — ride the existing
QUERY_PLAN_V2_SHADOW pattern. Shadow ships first: it collects
plan-determinism and shape-coverage evidence on real traffic before any
flip. Both flags get Compose passthroughs at introduction (engraved
silent-fallback law).

## 8. Build phases

- L1: QueryPlanV1 schema + rule planners + shadow trace (dark; no
  behavior change). MAY BUILD IMMEDIATELY (no T dependency).
- L2: Tier-0 shortlist + grounding + plan cache. MAY BUILD IMMEDIATELY.
- L3: allocation integration (generalized reservation) — AFTER the T
  window verdict (its lane machinery and receipts are inputs).
- L4: parallel execution + per-subquery rerank caps.
- L5: LLM escalation decomposer + named-source refusal signal wiring.
- L6: eval window — gates below.

## 9. Acceptance gates (preregistered; atomic window law applies)

- L-G1 plan determinism: 3 repeats per question, byte-identical
  plan_hash AND seat assignments, across all frozen + depth questions.
- L-G2 frozen floors: direct ≥85%, lay ≥75%, relationship ≥75%,
  original negatives 9/9, corpus/citation 100%.
- L-G3 held-out v2: non-degradation vs the canonical baseline
  (canonical three-state definition, 00:04Z law).
- L-G4 depth: D1 ≥3/4, D2 ≥3/4, D6 ≥1/2 per the depth-probe spec; the
  6-question bridge set rides here — router round-3 verdict comes from
  THIS window (camera/lens book must not take rank one).
- L-G5 latency: fast ≤5s / mongo ≤10s / deep ≤15s p50 on the same
  window's executions (small-sample p50; p95 reported, not gated).
- L-G6 simple-shape parity: single-subquery plans byte-match today's
  evidence selection (no regression for ordinary lookups).
