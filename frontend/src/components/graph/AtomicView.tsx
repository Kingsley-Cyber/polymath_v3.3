/**
 * Phase 4.C3+C4 — Atomic query view.
 *
 * Renders a query result as concentric orbitals around a nucleus:
 *
 *   ┌─────────────────────────────────────┐
 *   │              NUCLEUS                │  ← synthesis prose (graph/discover)
 *   │   ╱ ╲   ╱ ╲   ╱ ╲   ╱ ╲             │
 *   │  seed seed seed seed                │  ← query entities (orbit 1)
 *   │   ●   ●   ●   ●   ●   ●             │  ← evidence chunks (orbit 2)
 *   │  bridge   bridge   bridge           │  ← bridge entities (orbit 3)
 *   │    □   □   □   □                    │  ← gaps (orbit 4, red dashed)
 *   └─────────────────────────────────────┘
 *
 * Fires graph/discover (slow, LLM) + graph/query (fast, Cypher only) in
 * parallel. Renders seeds/bridges/gaps immediately from query response,
 * swaps in synthesis prose at the nucleus when discover returns.
 *
 * Failure mode: if discover errors or times out, the nucleus shows a
 * "Synthesis unavailable" placeholder and the rest of the atom still
 * renders. The user is never blocked on the LLM.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { discoverGraph, queryGraph } from "../../lib/api";
import { graphColors, nodeFillColor } from "../../lib/graph-colors";
import { useSettingsStore } from "../../stores/settingsStore";
import type {
  GraphBridge,
  GraphGap,
  GraphQueryNode,
  GraphQueryResult,
} from "../../types/chat";
import type {
  GraphDiscoverRequest,
  GraphDiscoverResponse,
  GraphSynthesisMode,
} from "../../types/discover";

interface AtomicNode {
  id: string;
  type: "synthesis" | "seed" | "evidence" | "bridge" | "gap";
  label: string;
  hover?: string;
  entityType?: string;
  /** All corpora that contributed this entity (post-merge from PR 3
   *  backend). length > 1 → cross-corpus bridge, gets amber accent. */
  sourceCorpora?: string[];
  orbit: number;
  x?: number;
  y?: number;
  radius?: number;
  labelBox?: LabelBox;
  labelVisible?: boolean;
  displayLabel?: string;
}

interface AtomicEdge {
  source: string;
  target: string;
  kind: "supports" | "mentions" | "bridges" | "gap";
}

interface LabelBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

type PlacedAtomicNode = AtomicNode & {
  x: number;
  y: number;
  radius: number;
};

type Props = {
  /**
   * Multi-corpus by default (PR 3 backend merges per-corpus
   * discover/query into a single response). Pass every selected
   * corpus_id; the atom shows ONE unified nucleus with merged
   * orbits and source-corpus provenance on each dot.
   */
  corpusIds: string[];
  query: string;
  synthesisMode?: GraphSynthesisMode;
  /**
   * Same model reference chat passes through overrides.model — may be
   * a raw LiteLLM id, `pool:<entry_id>`, or `profile:<entry_id>`. The
   * backend's _resolve_graph_model resolves all three forms. Wired
   * through from App.tsx's top-bar ModelSelector so atom synthesis
   * uses the user's selected model just like Brain / Query modes do.
   * Optional: when omitted, the backend falls back to the user's
   * query-model preference.
   */
  model?: string;
  onSelectSeed?: (entityId: string) => void;
};

const LABEL_FONT = "11px Inter, ui-sans-serif, system-ui, sans-serif";
const NUCLEUS_FONT = "13px Inter, ui-sans-serif, system-ui, sans-serif";
const LABEL_HEIGHT = 16;
const LABEL_GAP = 12;
const HIT_PADDING = 8;
const EDGE_HIT_RADIUS = 7;

function nodeRadius(type: AtomicNode["type"]): number {
  if (type === "synthesis") return 38;
  if (type === "seed") return 18;
  if (type === "bridge") return 12;
  if (type === "gap") return 11;
  return 8;
}

function compactLabel(label: string, type: AtomicNode["type"]): string {
  const limit = type === "gap" ? 34 : type === "bridge" ? 26 : 28;
  if (label.length <= limit) return label;
  return `${label.slice(0, Math.max(0, limit - 1))}…`;
}

function getMeasureContext(): CanvasRenderingContext2D | null {
  if (typeof document === "undefined") return null;
  const canvas = document.createElement("canvas");
  return canvas.getContext("2d");
}

function measureLabel(
  ctx: CanvasRenderingContext2D | null,
  label: string,
): number {
  if (!ctx) return label.length * 6.4 + 16;
  ctx.font = LABEL_FONT;
  return ctx.measureText(label).width + 16;
}

function labelBoxForNode(node: PlacedAtomicNode): LabelBox | undefined {
  if (!node.labelVisible || !node.displayLabel || node.type === "synthesis") {
    return undefined;
  }
  const w = node.labelBox?.w ?? node.displayLabel.length * 6.4 + 16;
  return {
    x: node.x - w / 2,
    y: node.y + node.radius + LABEL_GAP - LABEL_HEIGHT / 2,
    w,
    h: LABEL_HEIGHT,
  };
}

function boxesOverlap(a: LabelBox, b: LabelBox, pad = 4): boolean {
  return !(
    a.x + a.w + pad < b.x ||
    b.x + b.w + pad < a.x ||
    a.y + a.h + pad < b.y ||
    b.y + b.h + pad < a.y
  );
}

function pointInBox(x: number, y: number, box: LabelBox): boolean {
  return x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h;
}

function edgeKey(edge: AtomicEdge): string {
  return `${edge.source}->${edge.target}:${edge.kind}`;
}

function distanceToSegment(
  px: number,
  py: number,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  if (lenSq === 0) return Math.hypot(px - x1, py - y1);
  const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lenSq));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

export default function AtomicView({
  corpusIds,
  query,
  synthesisMode = "research",
  model,
  onSelectSeed,
}: Props) {
  const graphQuerySeedEntities = useSettingsStore(
    (state) => state.graphQuerySeedEntities,
  );
  const graphQueryMaxHops = useSettingsStore((state) => state.graphQueryMaxHops);
  const graphQueryNodeLimit = useSettingsStore(
    (state) => state.graphQueryNodeLimit,
  );
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [queryData, setQueryData] = useState<GraphQueryResult | null>(null);
  const [synthesis, setSynthesis] = useState<GraphDiscoverResponse | null>(
    null,
  );
  const [synthError, setSynthError] = useState<string | null>(null);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [hoveredNode, setHoveredNode] = useState<AtomicNode | null>(null);
  const [hoveredEdgeKey, setHoveredEdgeKey] = useState<string | null>(null);

  // Resize observer.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ro = new ResizeObserver((entries) => {
      const e = entries[0];
      if (!e) return;
      const rect = e.contentRect;
      setSize({ w: Math.floor(rect.width), h: Math.floor(rect.height) });
    });
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, []);

  // Fire both endpoints in parallel; render whichever returns first.
  // Both endpoints are multi-corpus (PR 3 backend) — we pass the full
  // corpusIds list and the backend merges per-corpus results.
  useEffect(() => {
    if (corpusIds.length === 0 || !query.trim()) return;
    let cancelled = false;
    setQueryData(null);
    setSynthesis(null);
    setSynthError(null);
    setQueryError(null);

    // graph/query — fast (Cypher only). Surface seeds/bridges/gaps ASAP.
    queryGraph(corpusIds, query, graphQueryMaxHops, graphQueryNodeLimit, {
      seedLimitPerToken: graphQuerySeedEntities,
    })
      .then((res) => {
        if (cancelled) return;
        setQueryData(res);
      })
      .catch((exc) => {
        if (cancelled) return;
        setQueryError(exc instanceof Error ? exc.message : String(exc));
      });

    // graph/discover — slow (LLM). Independent failure mode.
    const discoverReq: GraphDiscoverRequest = {
      corpus_ids: corpusIds,
      query,
      synthesis_mode: synthesisMode,
      // Conditionally spread so an unset model omits the field
      // (backend then resolves the user's query-model preference)
      // instead of explicitly sending `model: undefined`.
      ...(model ? { model } : {}),
    };
    discoverGraph(discoverReq)
      .then((res) => {
        if (cancelled) return;
        setSynthesis(res);
      })
      .catch((exc) => {
        if (cancelled) return;
        setSynthError(exc instanceof Error ? exc.message : String(exc));
      });

    return () => {
      cancelled = true;
    };
  }, [
    corpusIds.join(","),
    query,
    synthesisMode,
    model,
    graphQuerySeedEntities,
    graphQueryMaxHops,
    graphQueryNodeLimit,
  ]);

  // Build the atomic node graph from whatever has arrived so far.
  const { nodes, edges } = useMemo(() => {
    const nodes: AtomicNode[] = [];
    const edges: AtomicEdge[] = [];

    // ORBIT 0 — nucleus (synthesis prose, or placeholder).
    if (synthesis) {
      const headline = synthesis.interpretation || synthesis.query || "Synthesis";
      nodes.push({
        id: "nucleus",
        type: "synthesis",
        label: headline.slice(0, 80),
        hover: headline,
        orbit: 0,
      });
    } else if (synthError) {
      nodes.push({
        id: "nucleus",
        type: "synthesis",
        label: "Synthesis unavailable",
        hover: synthError,
        orbit: 0,
      });
    } else {
      nodes.push({
        id: "nucleus",
        type: "synthesis",
        label: "Synthesizing…",
        orbit: 0,
      });
    }

    // ORBIT 1 — seeds. The PR 3 merged response stamps `source_corpora`
    // on each entity; we cast through a soft type because the type
    // declaration in chat.ts predates the multi-corpus PR.
    const seeds: GraphQueryNode[] = queryData?.seed_entities ?? [];
    for (const seed of seeds) {
      const sourceCorpora =
        (seed as unknown as { source_corpora?: string[] }).source_corpora ?? undefined;
      nodes.push({
        id: `seed:${seed.id}`,
        type: "seed",
        label: seed.display_name,
        entityType: seed.entity_type,
        sourceCorpora,
        orbit: 1,
      });
      edges.push({ source: "nucleus", target: `seed:${seed.id}`, kind: "supports" });
    }

    // ORBIT 2 — evidence (from synthesis trace, if any).
    const evidence = (
      (synthesis as unknown as { trace?: { source_docs?: unknown[] } })?.trace
        ?.source_docs ?? []
    ).slice(0, 8) as Array<{ doc_id?: string; source_label?: string; text?: string }>;
    evidence.forEach((ev, i) => {
      const id = `ev:${i}`;
      nodes.push({
        id,
        type: "evidence",
        label: ev.source_label || `Source ${i + 1}`,
        hover: ev.text?.slice(0, 220),
        orbit: 2,
      });
      // Heuristic: connect evidence to the seed it textually mentions.
      for (const seed of seeds) {
        const needle = seed.display_name.toLowerCase();
        if (
          needle &&
          (ev.text || "").toLowerCase().includes(needle)
        ) {
          edges.push({
            source: `seed:${seed.id}`,
            target: id,
            kind: "mentions",
          });
        }
      }
    });

    // ORBIT 3 — bridges. Same source_corpora soft-cast as seeds.
    const bridges: GraphBridge[] = queryData?.bridges ?? [];
    for (const b of bridges) {
      const id = `br:${b.entity_id}`;
      const sourceCorpora =
        (b as unknown as { source_corpora?: string[] }).source_corpora ?? undefined;
      nodes.push({
        id,
        type: "bridge",
        label: b.display_name,
        entityType: b.entity_type,
        sourceCorpora,
        orbit: 3,
      });
      // Only connect each bridge to the seeds it actually links (the
      // backend already computes connected_seeds on each GraphBridge).
      for (const seedId of b.connected_seeds || []) {
        edges.push({
          source: `seed:${seedId}`,
          target: id,
          kind: "bridges",
        });
      }
    }

    // ORBIT 4 — gaps. The backend's GraphGap shape is a pair of entities
    // (a, b) that share no graph-edge but have semantic overlap; we render
    // the pair as a single gap node labeled "A ↔ B".
    const gaps: GraphGap[] = queryData?.gaps ?? [];
    gaps.slice(0, 6).forEach((g, i) => {
      const id = `gap:${i}`;
      const label = `${g.entity_a_name} ↔ ${g.entity_b_name}`.slice(0, 60);
      nodes.push({
        id,
        type: "gap",
        label,
        hover: label,
        orbit: 4,
      });
      edges.push({ source: "nucleus", target: id, kind: "gap" });
    });

    return { nodes, edges };
  }, [synthesis, synthError, queryData]);

  // Position nodes on concentric orbits with label-aware angular demand.
  const placedNodes = useMemo<PlacedAtomicNode[]>(() => {
    if (size.w === 0) return [];
    const cx = size.w / 2;
    const cy = size.h / 2;
    const maxR = Math.min(size.w, size.h) * 0.42;
    const orbits = [0, maxR * 0.25, maxR * 0.45, maxR * 0.7, maxR * 0.92];
    const measureCtx = getMeasureContext();

    const byOrbit: Record<number, AtomicNode[]> = { 0: [], 1: [], 2: [], 3: [], 4: [] };
    for (const n of nodes) byOrbit[n.orbit].push(n);

    const placed: PlacedAtomicNode[] = [];
    for (const orbit of [0, 1, 2, 3, 4] as const) {
      const ring = byOrbit[orbit];
      if (orbit === 0) {
        // Nucleus is centered.
        ring.forEach((n) =>
          placed.push({
            ...n,
            x: cx,
            y: cy,
            radius: nodeRadius(n.type),
            labelVisible: true,
            displayLabel: compactLabel(n.label, n.type),
          }),
        );
        continue;
      }
      if (ring.length === 0) continue;

      const baseR = orbits[orbit];
      const measured = ring.map((n) => {
        const radius = nodeRadius(n.type);
        const displayLabel = compactLabel(n.label, n.type);
        const labelVisible = n.type !== "evidence";
        const labelW = labelVisible ? measureLabel(measureCtx, displayLabel) : radius * 2 + 14;
        return { node: n, radius, displayLabel, labelVisible, labelW };
      });
      const demandFor = (labelW: number, radius: number, r: number) => {
        const linear = Math.max(labelW, radius * 2 + 18);
        return Math.min(Math.PI * 0.8, Math.atan2(linear / 2, Math.max(r, 1)) * 2 + 0.08);
      };
      const totalDemand = measured.reduce(
        (sum, m) => sum + demandFor(m.labelW, m.radius, baseR),
        0,
      );
      const lanes = Math.max(1, Math.min(3, Math.ceil(totalDemand / (Math.PI * 2 * 0.86))));
      const laneGap = Math.max(22, Math.min(34, maxR * 0.08));
      const laneOffsets =
        lanes === 1 ? [0] : lanes === 2 ? [-0.5, 0.5] : [-1, 0, 1];

      laneOffsets.forEach((offset, laneIndex) => {
        const group = measured.filter((_, i) => i % lanes === laneIndex);
        if (group.length === 0) return;
        const r = Math.max(maxR * 0.18, Math.min(maxR * 0.98, baseR + offset * laneGap));
        const demands = group.map((m) => demandFor(m.labelW, m.radius, r));
        const total = demands.reduce((sum, d) => sum + d, 0);
        const sparseArc = group.length <= 4 ? Math.PI * 1.35 : Math.PI * 2;
        const usableArc = total < sparseArc ? sparseArc : Math.PI * 2;
        const extra = Math.max(0, (usableArc - total) / group.length);
        let angle = -Math.PI / 2 - usableArc / 2 + extra / 2;
        group.forEach((m, i) => {
          const center = angle + demands[i] / 2;
          angle += demands[i] + extra;
          const x = cx + Math.cos(center) * r;
          const y = cy + Math.sin(center) * r;
          const baseLabelBox = m.labelVisible
            ? { x: 0, y: 0, w: m.labelW, h: LABEL_HEIGHT }
            : undefined;
          placed.push({
            ...m.node,
            x,
            y,
            radius: m.radius,
            labelVisible: m.labelVisible,
            displayLabel: m.displayLabel,
            labelBox: baseLabelBox,
          });
        });
      });
    }

    // Hide lower-priority labels that would collide after lane packing.
    const accepted: LabelBox[] = [];
    for (const n of placed) {
      const box = labelBoxForNode(n);
      if (!box) continue;
      if (accepted.some((existing) => boxesOverlap(existing, box))) {
        n.labelVisible = false;
        n.labelBox = undefined;
        continue;
      }
      n.labelBox = box;
      accepted.push(box);
    }

    return placed;
  }, [nodes, size.w, size.h]);

  // Draw.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || size.w === 0) return;
    canvas.width = size.w * window.devicePixelRatio;
    canvas.height = size.h * window.devicePixelRatio;
    canvas.style.width = `${size.w}px`;
    canvas.style.height = `${size.h}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);

    ctx.fillStyle = graphColors.atomic.background;
    ctx.fillRect(0, 0, size.w, size.h);

    // Orbital rings (faint dashed).
    const cx = size.w / 2;
    const cy = size.h / 2;
    const maxR = Math.min(size.w, size.h) * 0.42;
    ctx.strokeStyle = graphColors.atomic.ring;
    ctx.setLineDash([4, 8]);
    for (const r of [maxR * 0.25, maxR * 0.45, maxR * 0.7, maxR * 0.92]) {
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    ctx.setLineDash([]);

    const byId = new Map<string, AtomicNode>(
      placedNodes.map((n) => [n.id, n]),
    );

    // Edges first so dots sit on top.
    for (const e of edges) {
      const s = byId.get(e.source);
      const t = byId.get(e.target);
      if (
        !s || !t ||
        s.x == null || s.y == null ||
        t.x == null || t.y == null
      ) {
        continue;
      }
      const isEdgeHover = edgeKey(e) === hoveredEdgeKey;
      ctx.beginPath();
      ctx.moveTo(s.x, s.y);
      ctx.lineTo(t.x, t.y);
      if (e.kind === "gap") {
        ctx.strokeStyle = graphColors.relation.gap;
        ctx.lineWidth = isEdgeHover ? 2.4 : 1.2;
        ctx.setLineDash([3, 5]);
      } else if (e.kind === "supports") {
        ctx.strokeStyle = graphColors.relation.supports;
        ctx.lineWidth = isEdgeHover ? 2.8 : 1.6;
        ctx.setLineDash([]);
      } else if (e.kind === "bridges") {
        ctx.strokeStyle = graphColors.relation.bridges;
        ctx.lineWidth = isEdgeHover ? 2.4 : 1.1;
        ctx.setLineDash([]);
      } else {
        ctx.strokeStyle = graphColors.relation.mentions;
        ctx.lineWidth = isEdgeHover ? 2.2 : 1;
        ctx.setLineDash([]);
      }
      ctx.globalAlpha = isEdgeHover ? 0.95 : 0.72;
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.setLineDash([]);
    }

    // Nodes.
    for (const n of placedNodes) {
      if (n.x == null || n.y == null) continue;
      const isHover = hoveredNode?.id === n.id;
      const size_px = n.radius;
      ctx.beginPath();
      ctx.arc(n.x, n.y, size_px, 0, Math.PI * 2);
      // Color dispatch — seeds and bridges flow through nodeFillColor
      // so cross-corpus provenance (> 1 source_corpora) shows as amber,
      // while single-corpus entities use the entity-type palette or
      // fall back to the corpus's HSL hash.
      const provColor = nodeFillColor(n.entityType, n.sourceCorpora);
      const crossCorpus =
        Array.isArray(n.sourceCorpora) && n.sourceCorpora.length > 1;
      if (n.type === "synthesis") {
        ctx.fillStyle = graphColors.atomic.nucleus;
      } else if (n.type === "seed") {
        ctx.fillStyle = provColor;
      } else if (n.type === "bridge") {
        // Bridges keep their light fill (so they read as connectors)
        // but get a vivid stroke colored by provenance.
        ctx.fillStyle = crossCorpus
          ? graphColors.atomic.crossCorpusBridgeFill
          : graphColors.atomic.bridgeFill;
      } else if (n.type === "gap") {
        ctx.fillStyle = graphColors.atomic.gapFill;
      } else {
        ctx.fillStyle = graphColors.atomic.evidence;
      }
      ctx.fill();
      ctx.strokeStyle =
        n.type === "gap"
          ? graphColors.relation.gap
          : n.type === "synthesis"
            ? graphColors.atomic.nucleusStroke
            : n.type === "seed"
              ? provColor
              : n.type === "bridge"
                ? provColor
                : graphColors.relation.supports;
      ctx.lineWidth = isHover ? 3 : crossCorpus ? 2.5 : 1.5;
      ctx.stroke();

      // Labels for everything but evidence (which is dense).
      if (n.type !== "evidence" && n.labelVisible) {
        ctx.fillStyle =
          n.type === "synthesis"
            ? graphColors.atomic.background
            : graphColors.atomic.label;
        ctx.font = n.type === "synthesis" ? NUCLEUS_FONT : LABEL_FONT;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        if (n.type === "synthesis") {
          // Wrap into 2 lines to fit inside the dark nucleus dot.
          const words = n.label.split(/\s+/);
          const lines: string[] = [];
          let cur = "";
          for (const w of words) {
            const trial = cur ? `${cur} ${w}` : w;
            if (ctx.measureText(trial).width > size_px * 1.6) {
              if (cur) lines.push(cur);
              cur = w;
            } else {
              cur = trial;
            }
            if (lines.length === 1) break;
          }
          if (cur) lines.push(cur);
          lines.slice(0, 2).forEach((line, i) =>
            ctx.fillText(line, n.x!, n.y! + (i - 0.5) * 14),
          );
        } else {
          ctx.fillText(n.displayLabel || n.label, n.x!, n.y! + size_px + LABEL_GAP);
        }
      }
    }
  }, [placedNodes, edges, hoveredNode, hoveredEdgeKey, size.w, size.h]);

  // Hit testing.
  const hitTest = (
    evt: React.MouseEvent<HTMLCanvasElement>,
  ): AtomicNode | null => {
    const rect = evt.currentTarget.getBoundingClientRect();
    const mx = evt.clientX - rect.left;
    const my = evt.clientY - rect.top;
    for (let i = placedNodes.length - 1; i >= 0; i--) {
      const n = placedNodes[i];
      if (n.x == null || n.y == null) continue;
      if (n.labelBox && pointInBox(mx, my, n.labelBox)) return n;
      const r = Math.max(n.radius + HIT_PADDING, n.type === "evidence" ? 13 : n.radius);
      const dx = n.x - mx;
      const dy = n.y - my;
      if (dx * dx + dy * dy <= r * r) return n;
    }
    return null;
  };

  const edgeHitTest = (
    evt: React.MouseEvent<HTMLCanvasElement>,
  ): string | null => {
    const rect = evt.currentTarget.getBoundingClientRect();
    const mx = evt.clientX - rect.left;
    const my = evt.clientY - rect.top;
    const byId = new Map<string, PlacedAtomicNode>(
      placedNodes.map((n) => [n.id, n]),
    );
    for (let i = edges.length - 1; i >= 0; i--) {
      const e = edges[i];
      const s = byId.get(e.source);
      const t = byId.get(e.target);
      if (!s || !t) continue;
      if (distanceToSegment(mx, my, s.x, s.y, t.x, t.y) <= EDGE_HIT_RADIUS) {
        return edgeKey(e);
      }
    }
    return null;
  };

  const handleClick = (evt: React.MouseEvent<HTMLCanvasElement>) => {
    const n = hitTest(evt);
    if (!n) return;
    if (n.type === "seed" && onSelectSeed) {
      onSelectSeed(n.id.replace(/^seed:/, ""));
    }
  };

  const handleMouseMove = (evt: React.MouseEvent<HTMLCanvasElement>) => {
    const n = hitTest(evt);
    setHoveredNode(n);
    setHoveredEdgeKey(n ? null : edgeHitTest(evt));
  };

  const hoveredEdge = hoveredEdgeKey
    ? edges.find((edge) => edgeKey(edge) === hoveredEdgeKey) || null
    : null;

  return (
    <div className="relative w-full h-full">
      <canvas
        ref={canvasRef}
        className="absolute inset-0 cursor-pointer"
        onMouseMove={handleMouseMove}
        onMouseLeave={() => {
          setHoveredNode(null);
          setHoveredEdgeKey(null);
        }}
        onClick={handleClick}
      />

      {queryError && !queryData && (
        <div className="absolute top-4 left-4 right-4 bg-rose-50 border border-rose-200 text-rose-800 text-sm rounded p-2">
          Graph query failed: {queryError}
        </div>
      )}

      {/* Multi-corpus legend — only renders when >1 corpus is selected,
          so the affordance disappears when the convention is irrelevant. */}
      {corpusIds.length > 1 && (
        <div className="absolute bottom-4 left-4 flex items-center gap-2 bg-white/95 border border-slate-200 rounded px-2.5 py-1 text-[11px] font-mono text-slate-700 shadow-sm">
          <span
            className="inline-block w-2.5 h-2.5 rounded-full"
            style={{ background: graphColors.state.crossCorpus }}
            aria-hidden
          />
          <span>cross-corpus entity</span>
          <span className="text-slate-400">·</span>
          <span>{corpusIds.length} corpora merged</span>
        </div>
      )}

      {hoveredNode?.hover && (
        <div className="absolute top-4 right-4 max-w-md bg-slate-900 text-slate-100 text-xs rounded p-3 shadow-lg">
          <div className="font-medium mb-1">{hoveredNode.label}</div>
          <div className="text-slate-300">{hoveredNode.hover}</div>
        </div>
      )}

      {!hoveredNode && hoveredEdge && (
        <div className="absolute top-4 right-4 max-w-sm bg-slate-900 text-slate-100 text-xs rounded p-3 shadow-lg">
          <div className="font-medium mb-1">
            {hoveredEdge.kind === "gap"
              ? "Missing connection"
              : hoveredEdge.kind === "bridges"
                ? "Bridge relationship"
                : hoveredEdge.kind === "supports"
                  ? "Synthesis support"
                  : "Mention relationship"}
          </div>
          <div className="text-slate-300">
            {hoveredEdge.source.replace(/^[^:]+:/, "")} {"->"}{" "}
            {hoveredEdge.target.replace(/^[^:]+:/, "")}
          </div>
        </div>
      )}

      {/* Synthesis panel — slides up from the bottom when available */}
      {synthesis?.auto_synthesis &&
        typeof (synthesis.auto_synthesis as { markdown?: string }).markdown ===
          "string" && (
          <details className="absolute bottom-4 left-4 right-4 max-h-[40%] bg-white border border-slate-200 rounded-lg shadow-xl overflow-auto">
            <summary className="px-4 py-2 cursor-pointer font-medium text-slate-800 bg-slate-50">
              {synthesisMode === "ideation"
                ? "Build idea"
                : synthesisMode === "nuance"
                  ? "Nuance"
                  : "Synthesis"}
              {" · click to expand"}
            </summary>
            <div className="p-4 synthesis-body">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {
                  (synthesis.auto_synthesis as { markdown: string })
                    .markdown
                }
              </ReactMarkdown>
            </div>
          </details>
        )}
    </div>
  );
}
