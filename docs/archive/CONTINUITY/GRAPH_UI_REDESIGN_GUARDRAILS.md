# Graph UI Redesign Guardrails

Purpose: preserve a reversible checkpoint before using an external/open-source
UI/UX design model to redesign the graph query screen.

This file is the context packet for that model and for any future agent that
implements its output.

## Rollback Point

The safe rollback target is the pushed `main` commit that adds this file.
Treat any later graph UI redesign commit as reversible with:

```bash
git revert <redesign_commit_sha>
```

If several redesign commits are made, revert the redesign range back to this
guardrail checkpoint.

## Redesign Scope

Allowed target:

```text
Graph query / graph discovery user experience.
```

Primary frontend files:

```text
frontend/src/components/graph/GraphViewer.tsx
frontend/src/components/graph/AtomicView.tsx
frontend/src/components/graph/BrainViewDashboard.tsx
frontend/src/components/graph/ConstellationCanvas.tsx
frontend/src/components/graph/BookDrillPanel.tsx
frontend/src/lib/polymath-graph-adapter.ts
frontend/src/lib/api.ts
frontend/src/types/discover.ts
frontend/src/lib/graph-colors.ts
```

Primary backend/API contracts:

```text
POST /api/graph/query
POST /api/graph/discover
POST /api/graph/refine
POST /api/graph/node-insight
POST /api/graph/brain-view
POST /api/graph/by-document
POST /api/graph/book-drilldown
POST /api/graph/cache/rebuild
GET  /api/graph/sessions
GET  /api/graph/sessions/{session_id}
```

## Non-Negotiable Contracts

Do not break these:

```text
1. Keep graph query route behavior intact.
   UI may change, but request/response shapes must remain compatible with
   frontend/src/types/discover.ts and backend/models/schemas.py.

2. Do not remove these four synthesis modes:
   research, nuance, ideation, gap.

3. Do not rename the three retrieval route UI names:
   Fast Search, Hybrid Search, Graph Augmentation.

4. Do not remove graph progress visibility.
   The graph screen must still show a live/stepwise state for query,
   following, analyzing, packing, synthesizing, done, and error cases.

5. Do not hide source grounding.
   Local corpus evidence, graph evidence, and optional web evidence must stay
   visually distinguishable when present.

6. Do not leak private corpus text into web search queries.
   Web-enabled graph synthesis may use user query text, resolved public entity
   names, and short concept labels only.

7. Do not change backend retrieval, ingestion, or Neo4j query behavior as part
   of a visual redesign unless explicitly requested.

8. Do not introduce unbounded graph rendering.
   The UI must keep bounded node/edge rendering, cache warming, loading states,
   and graceful empty/error states.

9. Do not remove multi-corpus support.
   `corpus_ids` is canonical; single `corpus_id` exists only as legacy alias.

10. Do not turn the graph screen into a static landing page.
    The first screen should remain a usable graph/query workspace.
```

## UX Goals

Improve these without weakening the contracts above:

```text
Clarity:
  Make it obvious what the graph is doing, what evidence it found, and why the
  synthesis is trustworthy.

Hierarchy:
  Separate query input, graph canvas, synthesis output, evidence/source panels,
  session history, and control surfaces.

State:
  Design polished states for idle, loading, partial graph, success, empty,
  degraded backend, and error.

Graph Advantage:
  Show why graph output is better than plain text retrieval when facts,
  entities, relations, bridges, weak links, or gaps are present.

Density:
  This is a working research tool. Favor scan-friendly operational UI over
  marketing-style hero sections or decorative cards.

Accessibility:
  Preserve keyboard access, visible focus, readable contrast, and responsive
  behavior on laptop and desktop widths.
```

## Data Structures To Preserve

The frontend must continue to accept these graph/discovery fields when present:

```text
GraphDiscoverResponse:
  session_id
  corpus_id
  corpus_ids
  query
  mode
  interpretation
  frontier
  analogies
  bridges
  weak_links
  transfers
  questions
  metrics
  graph.nodes
  graph.links
  anchors
  concept_communities
  entity_concept_map
  headline
  themes
  bridges_v2
  gaps_v2
  latent_topics
  tensions
  trace
  auto_synthesis
  insight_packet_summary
  context_graph
```

For graph web grounding:

```text
web_search_enabled defaults false.
web_fetch_depth is snippets | normal | deep.
web_max_results is bounded.
web evidence is separate from corpus evidence.
```

## Implementation Rules For The Design Model

When generating code or design instructions:

```text
1. Prefer editing existing graph components over inventing a parallel graph app.
2. Keep lucide-react icons where buttons need icons.
3. Keep cards only for individual panels/items; do not nest cards inside cards.
4. Do not add decorative gradient orbs/bokeh backgrounds.
5. Do not scale font sizes directly with viewport width.
6. Do not allow text overlap in buttons, tabs, chips, node tooltips, or panels.
7. Preserve stable dimensions for toolbars, canvas controls, tabs, chips, and
   graph stats so loading/progress text does not shift layout.
8. Keep graph canvas interaction obvious: zoom, fit, pause/play, select, drill,
   inspect, and send-to-chat actions must remain findable.
9. Keep backend errors actionable. Show what failed and what can still be used.
10. If a route returns partial data, display the partial result instead of
    blanking the graph.
```

## Validation Before Accepting A Redesign

Run at minimum:

```bash
npm --prefix frontend run build

python3 -m py_compile \
  backend/models/schemas.py \
  backend/routers/graph.py \
  scripts/verify_runtime_contracts.py

./scripts/check-install.sh --skip-compose-config
```

If backend graph contracts or discovery types change, also run:

```bash
docker compose run --rm -T \
  -v /Users/king/polymath_v3.3/backend:/app \
  backend python -m pytest \
  tests/test_corpus_ids_pr1.py \
  tests/graph/test_orchestrator_payload.py \
  tests/test_retrieval_three_tier_validation.py -q
```

For live graph/retrieval confidence after code changes:

```bash
set -a
source .env
set +a
scripts/retrieval_three_tier_eval.py \
  --stop-after-sources \
  --pretty \
  --assert \
  --output data_eval/retrieval_three_tier_retrieval_only.json
```

## Acceptance Criteria

The redesign is acceptable only if:

```text
The graph query screen still runs a query.
The graph canvas still renders bounded graph data.
The user can still inspect nodes and evidence.
The four synthesis modes still produce visible output.
Graph progress and error states are visible.
Local corpus evidence remains distinct from optional web evidence.
No backend retrieval/ingestion contract is weakened.
Install/runtime contracts still pass.
The frontend build passes.
```

## Message To Future Agents

The goal is a simpler, more elegant graph query UI, not a retrieval rewrite.
Protect the retrieval pipeline that is currently working. Redesign the screen
around the existing graph contracts, evidence lanes, and bounded query behavior.
