# Premium Graph Builder UI/UX Redesign — Creative Freedom Handoff

**Scope:** Full visual and interaction redesign of the Polymath graph builder / graph query workspace. The previous redesign was too flat and utilitarian; this brief grants the next agent freedom to make it feel **premium, scan-friendly, and responsive** while keeping the backend contracts intact.

## Status

- Current code is build-passing and functionally intact, but the UI is visually underwhelming.
- You are authorized to redesign **every UI/UX surface** in the graph builder: layout, canvas, panels, query composer, synthesis presentation, evidence/source lanes, progress states, and the graph renderer’s look.
- You are authorized to **change how the graph itself looks** (node shapes, edge curves, canvas background, physics defaults, label strategy, hover/selection affordances) as long as it remains performant, bounded, and does not change retrieval behavior.

## Non-negotiable contracts (from GRAPH_UI_REDESIGN_GUARDRAILS.md)

```text
1. Keep graph query route behavior intact.
   UI may change, request/response shapes must remain compatible with
   frontend/src/types/discover.ts and backend/models/schemas.py.

2. Do not remove the four synthesis modes:
   research, nuance, ideation, gap.

3. Do not rename the three retrieval route UI names:
   Fast Search, Hybrid Search, Graph Augmentation.

4. Do not remove graph progress visibility.
   The graph screen must still show live/stepwise state for query,
   following, analyzing, packing, synthesizing, done, and error cases.

5. Do not hide source grounding.
   Local corpus evidence, graph evidence, and optional web evidence must stay
   visually distinguishable when present.

6. Do not leak private corpus text into web search queries.

7. Do not change backend retrieval, ingestion, Neo4j, or Qdrant behavior.

8. Do not introduce unbounded graph rendering.
   Keep bounded node/edge rendering, cache warming, loading states,
   and graceful empty/error states.

9. Do not remove multi-corpus support. corpus_ids is canonical.

10. Do not turn the graph screen into a static landing page.
    The first screen should remain a usable graph/query workspace.
```

## Design direction — premium + optimized

You have full creative freedom, but the result should read as a **research-grade premium product**, not a dashboard or marketing page.

### Must keep
- This is a **workspace**, not a landing page. The first screen must be immediately usable.
- No decorative gradient orbs, bokeh, or galaxy dust backgrounds.
- No nested cards inside cards.
- No text overlap or overflow at any width.
- Responsive behavior on laptop, desktop, and tablet widths.
- Icons for tool buttons.
- Professional, calm, scan-friendly states.
- All existing graph interactions: zoom, fit, pause/play, select, drill, inspect, send-to-chat.

### Encouraged to rethink
- **Layout architecture** — how the canvas, sidebar, query composer, and synthesis output share the screen. Consider a clean three-zone model: top control strip / left or right intelligence panel / main canvas.
- **Canvas aesthetic** — node shape, size, color, edge curves, selection halos, background grid or subtle texture. Make the graph look intentional and expensive.
- **Typography + spacing** — use the existing font families (Rubik, Atkinson Hyperlegible, Roboto Mono) but establish a stronger hierarchy with size, weight, and color.
- **Color system** — build a refined palette from the existing CSS custom properties. Restrained, high-legibility, with clear semantic lanes for corpus, graph, and web evidence.
- **Query composer** — redesign the synthesis-mode selector, query input, run/continue actions, web-grounding toggle, and validate toggle into one coherent control surface.
- **Progress and state** — make loading, partial, empty, degraded, and error states feel calm and informative, not alarming.
- **Evidence and source panels** — redesign how corpus evidence, graph evidence, and optional web evidence are presented. Make the distinction obvious. Show why graph output is better than plain text retrieval.
- **Node inspect / drill / send-to-chat workflows** — preserve the behavior, but make the UI for each action feel premium (context menus, smooth panels, clear affordances).

### Reference files to edit
```text
frontend/src/components/graph/GraphViewer.tsx
frontend/src/components/graph/AtomicView.tsx
frontend/src/components/graph/BrainViewDashboard.tsx
frontend/src/components/graph/ConstellationCanvas.tsx
frontend/src/components/graph/BookDrillPanel.tsx
frontend/src/lib/graph-colors.ts
frontend/src/types/discover.ts
frontend/src/lib/sigma-constants.ts        # if you change graph rendering look
frontend/src/lib/polymath-graph-adapter.ts # if you change graph physics/positioning
frontend/src/hooks/useSigma.ts             # if you change selection/hover/physics behavior
frontend/src/index.css                     # if you add global graph UI utilities
```

## Implementation rules

1. Prefer editing existing graph components over inventing a parallel graph app.
2. Keep lucide-react icons where buttons need icons.
3. Keep cards only for individual panels/items; do not nest cards inside cards.
4. Do not add decorative gradient orbs/bokeh backgrounds.
5. Do not scale font sizes directly with viewport width.
6. Do not allow text overlap in buttons, tabs, chips, node tooltips, or panels.
7. Preserve stable dimensions for toolbars, canvas controls, tabs, chips, and graph stats so loading/progress text does not shift layout.
8. Keep graph canvas interaction obvious: zoom, fit, pause/play, select, drill, inspect, and send-to-chat must remain findable.
9. Keep backend errors actionable. Show what failed and what can still be used.
10. If a route returns partial data, display the partial result instead of blanking the graph.

## Validation before accepting the redesign

Run at minimum:

```bash
npm --prefix frontend run build

python3 -m py_compile \
  backend/models/schemas.py \
  backend/routers/graph.py \
  scripts/verify_runtime_contracts.py

./scripts/check-install.sh --skip-compose-config
```

If backend graph contracts or discovery types change, also run the backend graph tests from the guardrail file.

## Acceptance criteria

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

## Message to the redesign agent

Make it beautiful, but make it first and foremost a fast, calm, trustworthy research workspace. The user should feel the tool is premium and opinionated, not cluttered. Protect the retrieval pipeline; redesign the surface.
