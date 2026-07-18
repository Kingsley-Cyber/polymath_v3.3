# Scholar — Deep Research to Artifact (package spec, owner-ordered)

Owner intent: NotebookLM × ChatGPT Deep Research over the owner's corpora
— autoresearch, subqueries, follow-up subqueries, traversals, hops,
synthesis, agent loop — delivering a designed HTML + PDF artifact.
Latency is DELIBERATELY spent: "it must fully exemplify fine grain
details, breadth and depth… increasing top k and final n… deep
understanding and nuance synthesis."

## Deep-research retrieval profile (the owner's depth contract)

A dedicated profile, used ONLY by Scholar (chat latency untouched):
- retrieval_k ×4 the chat default; rerank candidate cap ×3.
- final_n per SECTION (not per query): packet budget 24,000 tokens/section.
- Follow-up loop: refinement rounds until the gap detector runs dry,
  ceiling 5 rounds/section (vs 1 in chat).
- Graph hops mandatory on connection-type sections; entity-bridge
  subqueries always planned.
- Claims mandatory: every factual assertion in the artifact carries a
  claim anchor (sentence-level citation) or is marked inference.
- Tiers: always deep (qdrant+mongo+graph). Minutes-scale latency
  accepted by owner; progress streamed per section.

## Stages

1. PLAN — question → report outline (sections + per-section research
   questions), grounded on the doc-summary shortlist across ALL selected
   corpora. Outline is a typed artifact (replayable).
2. RESEARCH — per section: librarian subqueries → retrieval at the deep
   profile → follow-up subqueries until gap-dry (≤5 rounds) → evidence
   pool with claim anchors + graph paths.
3. CROSS-SECTION — dedup evidence across sections; contradiction scan
   (same claim, conflicting sources → surfaced as a "tension" callout);
   chart-data extraction (structured JSON emitted for quantitative or
   comparative material).
4. SYNTHESIS — per-section drafts then an executive summary, on the
   fast synthesis route; nuance contract in the prompt: comparisons,
   qualifications, and source disagreements must be voiced, not
   averaged away.
5. RENDER — one self-contained HTML artifact + a PDF twin (WeasyPrint):
   - Typography: display heading scale + readable body, consistent
     hierarchy; accent + semantic color palette; print-safe.
   - Charts: inline SVG only (bar/line/donut/timeline) generated from
     stage-3 chart JSON — identical in HTML and PDF, offline-forever.
   - Tables/bullets/bold: styled from the synthesis markdown contract.
   - Copy UX: per-section, per-table, per-chart-data, whole-report, and
     copy-the-question buttons (HTML artifact).
   - Appendix: full citation list (doc → page/heading → sentence), the
     run's cost ledger stamp, corpus + flag attestation, plan hash.

## API + storage

POST /api/research {question, corpus_ids[], depth: standard|elite} →
research_run_id; progress via SSE; artifacts stored under
/artifacts/research/<run_id>/ (report.html, report.pdf, evidence.json).

## Chat-UI rider (same frontend rebuild)

Per-message copy buttons in the everyday chat (user inputs AND assistant
outputs); code/table blocks get their own copy affordance.

## Gates (compact, ≤10 probes)

- One elite run on a 3-corpus question: every section gap-dry or at
  ceiling; ≥90% of factual paragraphs carry ≥1 claim anchor; ≥1 chart
  rendered from real data; HTML and PDF byte-render without external
  fetches; ledger CLOSED with the run's exact cost in the appendix.
- One standard run under 5 minutes wall.
- Copy buttons verified in both artifact and chat.

## Reuse plan (GitHub survey, 2026-07-19 — all lift-grade licenses)

1. open_deep_research legacy `graph.py` + `prompts.py` (MIT): the
   outline-planner + per-section query→write→GRADE loop with follow-up
   queries — Scholar's exact state machine; retrieval is one swappable
   function → pointed at Polymath's deep profile.
2. STORM `VectorRM` + outline/article/polish modules (MIT): Qdrant-backed
   retrieval contract (same store as ours), draft→refine outline, forced
   inline [n] citations against numbered snippets, polish pass.
3. gpt-researcher `write_md_to_pdf` + prompt family (Apache-2.0): the
   md→WeasyPrint PDF twin and battle-tested report prompts;
   `skills/deep_research.py` depth/breadth recursion.
4. local-deep-research retriever-as-search-engine seam (MIT): the adapter
   pattern for plugging Polymath retrieval into any loop.
5. deer-flow `main-1.x` Plan schema with `has_enough_context` +
   reporter prompt (MIT): structured early-exit planning.
CUSTOM-ONLY layer (no prior art ships it): the designed self-contained
HTML artifact — typography/color system, inline-SVG charts, copy-
everything UX. Revised estimate: ~1 day orchestrator assembly + the
custom artifact layer.
