/**
 * GraphViewer — PR 4 of the multi-corpus rollout.
 *
 * One component, two modes:
 *
 *   • Brain View (mode="brain"): renders the cached supernode graph for the
 *     selected corpora. Click a cluster → drill into its full entity
 *     subgraph in-place (zoom + crossfade), breadcrumb pops back. Hover
 *     dim, label gating, color toggle (community vs corpus), cache-warming
 *     chip with 15s poll.
 *
 *   • Query View (mode="query"): runs the Agent Query + Mission Control
 *     synthesis against the selected corpora; plays a one-shot ~1.2s
 *     gravity entry animation (seeds → bridges → settle) then renders
 *     synthesis prose alongside the static subgraph. Citation `[n]` clicks
 *     pulse the matching node.
 *
 * Backend contracts (PR 2 + PR 3):
 *   • POST /api/graph/overview  → brain view
 *   • POST /api/graph/cluster/{concept_id} → drill
 *   • POST /api/graph/full → drill fallback
 *   • GET  /api/corpora/{cid}/cache-status → warming chip poll
 *   • POST /api/graph/query → query view subgraph
 *   • POST /api/graph/discover → query view synthesis prose
 *
 * Replaces (per Phase F cleanup): GraphView.tsx, BooksClusterView.tsx,
 * DiscoveryPanel.tsx, RelationGraph.tsx — the legacy 6-mode tab
 * proliferation collapses into these two views.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, {
  type ForceGraphMethods,
  type GraphData as RFGraphData,
} from "react-force-graph-2d";
import ReactMarkdown from "react-markdown";
import { useSpring, animated } from "@react-spring/web";

// React 19 + @react-spring/web types miss `children` on AnimatedProps;
// cast to any-typed React component to silence the false positive while
// preserving runtime behavior.
const AnimatedDiv = animated.div as any;
import { ChevronLeft, Loader2, X, Zap } from "lucide-react";

import * as api from "../../lib/api";

// ─── Types ────────────────────────────────────────────────────────────────

export type GraphViewerMode = "brain" | "query";

interface GraphViewerProps {
  mode: GraphViewerMode;
  corpusIds: string[];
  /** Required when mode="query". Controlled by parent. */
  query?: string;
  /** Called when user clicks Re-run (query mode). Parent re-asks the LLM. */
  onRerun?: () => void;
  /** Called when user wants to dismiss the viewer entirely. */
  onClose?: () => void;
  /** Optional model override forwarded to /graph/discover. */
  model?: string;
}

type ViewerNode = {
  id: string;
  display_name: string;
  entity_type?: string;
  mention_count?: number;
  supernode_type?: string;
  primary_domain?: string;
  source_corpora?: string[];
  source_corpus?: string;
  member_ids?: string[];
  // react-force-graph runtime fields
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  __isHub?: boolean;
  __isTopLabel?: boolean;
  __isSeed?: boolean;
};

type ViewerLink = {
  source: string | ViewerNode;
  target: string | ViewerNode;
  predicate?: string;
  confidence?: number;
  weight?: number;
  source_corpora?: string[];
  source_corpus?: string;
  dangling?: boolean;
};

type ColorMode = "community" | "corpus";

type DrillFrame = {
  conceptId: string;
  label: string;
};

// ─── Color utilities ──────────────────────────────────────────────────────

function hashString(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) >>> 0;
  }
  return h;
}

function colorForCommunity(node: ViewerNode): string {
  const seed = node.primary_domain || node.id.split(":")[1] || node.id;
  const hue = hashString(seed) % 360;
  if (node.supernode_type === "domain") return `hsl(${hue}, 65%, 55%)`;
  if (node.supernode_type === "concept") return `hsl(${hue}, 55%, 65%)`;
  return `hsl(${hue}, 45%, 70%)`;
}

function colorForCorpus(node: ViewerNode): string {
  const seed = (node.source_corpora && node.source_corpora[0]) || "unknown";
  const hue = hashString(seed) % 360;
  return `hsl(${hue}, 65%, 55%)`;
}

function getNodeColor(node: ViewerNode, mode: ColorMode): string {
  return mode === "corpus" ? colorForCorpus(node) : colorForCommunity(node);
}

function hexToRgba(color: string, alpha: number): string {
  // Accepts hsl(...) or hex; for hsl we replace via CSS string.
  if (color.startsWith("hsl")) {
    return color.replace(/^hsl\((.*)\)$/, `hsla($1, ${alpha})`);
  }
  if (color.startsWith("rgb")) {
    return color.replace(/^rgb\((.*)\)$/, `rgba($1, ${alpha})`);
  }
  // Hex fallback
  const hex = color.replace("#", "");
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// ─── Brain mode data hook ─────────────────────────────────────────────────

function useBrainGraph(corpusIds: string[], drill: DrillFrame | null) {
  const [data, setData] = useState<{
    nodes: ViewerNode[];
    links: ViewerLink[];
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cacheWarming, setCacheWarming] = useState<string[]>([]);
  const [meta, setMeta] = useState<{
    raw_node_count?: number;
    raw_edge_count?: number;
    truncated?: boolean;
  }>({});

  const reload = useCallback(async () => {
    if (corpusIds.length === 0) {
      setData(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      if (drill) {
        // Drill mode — fetch the single-cluster subgraph.
        const res = await api.getGraphCluster(drill.conceptId, corpusIds);
        const nodes = (res.nodes || []).map(annotateBrainNode);
        const links = (res.edges || []).map(annotateBrainLink);
        markHubs(nodes, links);
        markTopLabels(nodes, 30);
        setData({ nodes, links });
        setMeta({ truncated: res.truncated });
        setCacheWarming(res._meta?.cache_warming_corpora || []);
      } else {
        const res = await api.getGraphOverviewMulti(corpusIds);
        const nodes = (res.nodes || []).map(annotateBrainNode);
        const links = (res.edges || []).map(annotateBrainLink);
        markHubs(nodes, links);
        markTopLabels(nodes, 30);
        setData({ nodes, links });
        setMeta({
          raw_node_count: res.raw_node_count,
          raw_edge_count: res.raw_edge_count,
          truncated: res.truncated,
        });
        setCacheWarming(res._meta?.cache_warming_corpora || []);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [corpusIds, drill]);

  useEffect(() => {
    reload();
  }, [reload]);

  // Cache-warming poll: every 15s while cacheWarming non-empty, check each
  // cold corpus and reload when all return ready.
  useEffect(() => {
    if (!cacheWarming.length) return;
    const t = setInterval(async () => {
      try {
        const statuses = await Promise.all(
          cacheWarming.map((cid) => api.getCorpusCacheStatus(cid)),
        );
        const stillWarming = statuses
          .filter((s) => s.metrics_cache !== "ready" || s.domain_cache !== "ready")
          .map((s) => s.corpus_id);
        if (stillWarming.length === 0) {
          // All caches landed — reload graph.
          reload();
        } else {
          setCacheWarming(stillWarming);
        }
      } catch {
        /* swallow — next tick will retry */
      }
    }, 15000);
    return () => clearInterval(t);
  }, [cacheWarming, reload]);

  return { data, loading, error, cacheWarming, meta, reload };
}

function annotateBrainNode(n: any): ViewerNode {
  return {
    id: String(n.id),
    display_name: String(n.display_name || n.id),
    entity_type: n.entity_type,
    mention_count: typeof n.mention_count === "number" ? n.mention_count : 1,
    supernode_type: n.supernode_type,
    primary_domain: n.primary_domain,
    source_corpora: n.source_corpora || [],
    source_corpus: n.source_corpus || (n.source_corpora?.[0] ?? ""),
    member_ids: n.member_ids,
  };
}

function annotateBrainLink(e: any): ViewerLink {
  return {
    source: String(e.source),
    target: String(e.target),
    predicate: e.predicate,
    confidence: typeof e.confidence === "number" ? e.confidence : undefined,
    weight: typeof e.weight === "number" ? e.weight : undefined,
    source_corpora: e.source_corpora || [],
    source_corpus: e.source_corpus,
    dangling: Boolean(e.dangling),
  };
}

function markHubs(nodes: ViewerNode[], links: ViewerLink[]) {
  const degree = new Map<string, number>();
  for (const l of links) {
    const s = typeof l.source === "string" ? l.source : l.source.id;
    const t = typeof l.target === "string" ? l.target : l.target.id;
    degree.set(s, (degree.get(s) || 0) + 1);
    degree.set(t, (degree.get(t) || 0) + 1);
  }
  const sortedByDeg = [...nodes].sort(
    (a, b) => (degree.get(b.id) || 0) - (degree.get(a.id) || 0),
  );
  for (let i = 0; i < Math.min(3, sortedByDeg.length); i++) {
    sortedByDeg[i].__isHub = true;
  }
}

function markTopLabels(nodes: ViewerNode[], topN: number) {
  const sorted = [...nodes].sort(
    (a, b) => (b.mention_count || 0) - (a.mention_count || 0),
  );
  const top = new Set(sorted.slice(0, topN).map((n) => n.id));
  for (const n of nodes) {
    n.__isTopLabel = top.has(n.id);
  }
}

// ─── Query mode data hook ─────────────────────────────────────────────────

function useQueryGraph(
  corpusIds: string[],
  query: string | undefined,
  model: string | undefined,
) {
  const [phase, setPhase] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [data, setData] = useState<{
    nodes: ViewerNode[];
    links: ViewerLink[];
  } | null>(null);
  const [bridges, setBridges] = useState<any[]>([]);
  const [hubs, setHubs] = useState<any[]>([]);
  const [gaps, setGaps] = useState<any[]>([]);
  const [seedIds, setSeedIds] = useState<Set<string>>(new Set());
  const [synthesis, setSynthesis] = useState<{
    markdown: string;
    sources: any[];
    perCorpus?: Array<{ corpus_id: string; markdown: string }>;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async () => {
    if (!corpusIds.length || !query?.trim()) return;
    setPhase("loading");
    setError(null);
    try {
      // Fire both calls in parallel — the structural subgraph lands first
      // (cheap Cypher), the LLM synthesis lands later but the entry
      // animation can already play.
      const subgraphP = api.queryGraph(corpusIds, query, 2, 50);
      const synthP = api.discoverGraph({
        corpus_ids: corpusIds as any,
        query,
        mode: "auto",
        ...(model ? { model } : {}),
      } as any);

      const sub = await subgraphP;
      const seedSet = new Set<string>((sub.seed_entities || []).map((s: any) => s.id));
      const nodes: ViewerNode[] = (sub.nodes || []).map((n: any) => ({
        id: String(n.id),
        display_name: String(n.display_name || n.id),
        entity_type: n.entity_type,
        mention_count: n.mention_count,
        source_corpora: n.source_corpora || [],
        source_corpus: n.source_corpus || (n.source_corpora?.[0] ?? ""),
        __isSeed: seedSet.has(String(n.id)),
      }));
      const links: ViewerLink[] = (sub.links || []).map((l: any) => ({
        source: String(l.source),
        target: String(l.target),
        predicate: l.predicate,
        confidence: l.confidence,
        source_corpora: l.source_corpora || [],
        source_corpus: l.source_corpus,
      }));
      markHubs(nodes, links);
      markTopLabels(nodes, 30);
      setSeedIds(seedSet);
      setBridges(sub.bridges || []);
      setHubs(sub.hubs || []);
      setGaps(sub.gaps || []);
      setData({ nodes, links });
      setPhase("ready");

      const synth = await synthP;
      const auto = (synth as any).auto_synthesis || {};
      setSynthesis({
        markdown:
          auto.markdown ||
          (synth as any).interpretation ||
          "(no synthesis generated)",
        sources: auto.sources || [],
        perCorpus: auto.per_corpus_synthesis || undefined,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  }, [corpusIds, query, model]);

  useEffect(() => {
    run();
  }, [run]);

  return { phase, data, bridges, hubs, gaps, seedIds, synthesis, error, run };
}

// ─── Component ────────────────────────────────────────────────────────────

export function GraphViewer({
  mode,
  corpusIds,
  query,
  onRerun,
  onClose,
  model,
}: GraphViewerProps) {
  const fgRef = useRef<ForceGraphMethods | undefined>(undefined);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [colorMode, setColorMode] = useState<ColorMode>("community");
  const [drillStack, setDrillStack] = useState<DrillFrame[]>([]);
  const [pulseId, setPulseId] = useState<string | null>(null);
  const skipAnimations = useMemo(() => {
    try {
      return localStorage.getItem("polymath-skip-animations") === "true";
    } catch {
      return false;
    }
  }, []);

  // Brain mode hooks
  const drill = drillStack.length ? drillStack[drillStack.length - 1] : null;
  const brain = useBrainGraph(mode === "brain" ? corpusIds : [], drill);

  // Query mode hooks
  const q = useQueryGraph(
    mode === "query" ? corpusIds : [],
    query,
    model,
  );

  const data = mode === "brain" ? brain.data : q.data;
  const loading = mode === "brain" ? brain.loading : q.phase === "loading";
  const error = mode === "brain" ? brain.error : q.error;

  // Resize observer keeps the canvas filling its container.
  useEffect(() => {
    const el = wrapperRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        const cr = e.contentRect;
        setSize({ w: Math.max(200, cr.width), h: Math.max(200, cr.height) });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Neighbor index for hover dimming.
  const neighborIds = useMemo(() => {
    if (!hoveredId || !data) return new Set<string>();
    const set = new Set<string>([hoveredId]);
    for (const l of data.links) {
      const s = typeof l.source === "string" ? l.source : l.source.id;
      const t = typeof l.target === "string" ? l.target : l.target.id;
      if (s === hoveredId) set.add(t);
      else if (t === hoveredId) set.add(s);
    }
    return set;
  }, [hoveredId, data]);

  // Query-mode entry animation: spring scales seeds in then fades the rest.
  const entryAnim = useSpring({
    from: { progress: 0 },
    to: { progress: 1 },
    config: { tension: 80, friction: 14 },
    immediate: skipAnimations || mode !== "query",
    reset: q.phase === "ready",
  });

  // Pause animation after force-layout settles in brain mode (no need to
  // burn CPU once the graph is at rest).
  useEffect(() => {
    if (mode !== "brain") return;
    if (!fgRef.current) return;
    const t = setTimeout(() => {
      try {
        fgRef.current?.pauseAnimation();
      } catch {
        /* ignore */
      }
    }, 1500);
    return () => clearTimeout(t);
  }, [mode, data]);

  // Drill: click a supernode → push frame, reload graph for that cluster.
  const handleNodeClick = useCallback(
    (n: ViewerNode) => {
      if (mode !== "brain") return;
      if (n.supernode_type !== "concept") return;
      // concept ids look like "concept:<id>"
      const conceptId = n.id.split(":").slice(1).join(":");
      if (!conceptId) return;
      setDrillStack((s) => [...s, { conceptId, label: n.display_name }]);
    },
    [mode],
  );

  // Citation click in synthesis prose → pulse the matching node.
  const handleCitationClick = useCallback((sourceLabel: string) => {
    setPulseId(sourceLabel);
    setTimeout(() => setPulseId(null), 800);
  }, []);

  // Custom node renderer.
  const drawNode = useCallback(
    (n: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const node = n as ViewerNode;
      const r = Math.max(
        3,
        Math.min(18, Math.log2((node.mention_count || 1) + 1) * 3 + (node.__isSeed ? 4 : 0)),
      );
      const dim = hoveredId !== null && !neighborIds.has(node.id);
      const opacity = dim ? 0.15 : 1.0;
      const baseColor = getNodeColor(node, colorMode);

      // Hub or seed glow.
      if ((node.__isHub || node.__isSeed || pulseId === node.id) && !dim) {
        const haloR = r * (pulseId === node.id ? 3.4 : 2.5);
        const grad = ctx.createRadialGradient(node.x!, node.y!, r, node.x!, node.y!, haloR);
        grad.addColorStop(0, hexToRgba(baseColor, 0.4));
        grad.addColorStop(1, hexToRgba(baseColor, 0));
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(node.x!, node.y!, haloR, 0, Math.PI * 2);
        ctx.fill();
      }

      ctx.fillStyle = hexToRgba(baseColor, opacity);
      ctx.beginPath();
      ctx.arc(node.x!, node.y!, r, 0, Math.PI * 2);
      ctx.fill();

      // Query-mode entry animation: scale seeds first, dim others until
      // mid-animation. progress goes 0 → 1.
      if (mode === "query" && !skipAnimations) {
        const progress = entryAnim.progress.get();
        if (!node.__isSeed && progress < 0.4) return;
      }

      const labelVisible =
        (node.__isTopLabel || node.__isSeed || globalScale > 1.5) && !dim;
      if (labelVisible) {
        const fontSize = Math.max(10, 12 / globalScale);
        ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
        ctx.fillStyle = `rgba(220, 220, 220, ${opacity * 0.85})`;
        ctx.textAlign = "center";
        ctx.fillText(node.display_name, node.x!, node.y! + r + fontSize + 2);
      }
    },
    [hoveredId, neighborIds, colorMode, pulseId, mode, skipAnimations, entryAnim.progress],
  );

  const linkColor = useCallback(
    (l: any) => {
      const link = l as ViewerLink;
      const source = typeof link.source === "string" ? link.source : link.source.id;
      const target = typeof link.target === "string" ? link.target : link.target.id;
      const isHovered = hoveredId === source || hoveredId === target;
      const isCrossCorpus =
        link.source_corpora && link.source_corpora.length > 1;
      const baseAlpha = isHovered ? 0.45 : isCrossCorpus ? 0.25 : 0.12;
      if (link.dangling) return `rgba(180, 100, 60, ${baseAlpha * 0.7})`;
      return `rgba(180, 180, 180, ${baseAlpha})`;
    },
    [hoveredId],
  );

  const linkWidth = useCallback(
    (l: any) => {
      const link = l as ViewerLink;
      const source = typeof link.source === "string" ? link.source : link.source.id;
      const target = typeof link.target === "string" ? link.target : link.target.id;
      const hovered = hoveredId === source || hoveredId === target;
      return hovered ? 1.4 : 0.5;
    },
    [hoveredId],
  );

  // ─── Render ───────────────────────────────────────────────────────────

  // Empty state — no corpora selected.
  if (!corpusIds.length) {
    return (
      <div
        className="flex h-full w-full items-center justify-center bg-[#0a0a0a] text-zinc-400"
        ref={wrapperRef}
      >
        <div className="text-center">
          <div className="text-base mb-2">Select a corpus from the sidebar to begin</div>
          <div className="text-xs text-zinc-600">
            Tip: select multiple corpora to see cross-book connections
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-full w-full bg-[#0a0a0a]" ref={wrapperRef}>
      {/* Top chrome */}
      <div className="absolute top-3 left-3 right-3 z-20 flex items-start justify-between pointer-events-none">
        <div className="flex flex-col gap-2 pointer-events-auto">
          <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono">
            {mode === "brain" ? "Corpora View" : "Query View"}
          </div>
          {/* Breadcrumb (brain mode drill) */}
          {mode === "brain" && drillStack.length > 0 && (
            <div className="flex items-center gap-1 text-xs text-zinc-300">
              <button
                className="hover:text-amber-400 transition-colors"
                onClick={() => setDrillStack([])}
              >
                Overview
              </button>
              {drillStack.map((f, i) => (
                <span key={i} className="flex items-center gap-1">
                  <ChevronLeft className="w-3 h-3 rotate-180" />
                  <button
                    className="hover:text-amber-400 transition-colors"
                    onClick={() => setDrillStack(drillStack.slice(0, i + 1))}
                  >
                    {f.label}
                  </button>
                </span>
              ))}
              <button
                className="ml-2 text-zinc-500 hover:text-zinc-300"
                onClick={() => setDrillStack(drillStack.slice(0, -1))}
                title="Pop one level"
              >
                ↩ back
              </button>
            </div>
          )}
          {/* Stats pill */}
          <div className="text-[11px] text-zinc-500 font-mono">
            {corpusIds.length} corpora
            {data && ` · ${data.nodes.length} nodes · ${data.links.length} edges`}
            {mode === "brain" && brain.cacheWarming.length > 0 && (
              <span className="ml-2 text-amber-400">
                · {brain.cacheWarming.length} warming…
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 pointer-events-auto">
          {mode === "brain" && (
            <button
              className="text-[10px] uppercase tracking-widest text-zinc-400 hover:text-amber-400 border border-zinc-700 rounded px-2 py-1 font-mono"
              onClick={() =>
                setColorMode((m) => (m === "community" ? "corpus" : "community"))
              }
              title="Toggle color scheme"
            >
              color: {colorMode}
            </button>
          )}
          {mode === "query" && onRerun && (
            <button
              className="text-[10px] uppercase tracking-widest text-zinc-400 hover:text-amber-400 border border-zinc-700 rounded px-2 py-1 font-mono flex items-center gap-1"
              onClick={onRerun}
              title="Re-run synthesis"
            >
              <Zap className="w-3 h-3" /> re-run
            </button>
          )}
          {onClose && (
            <button
              className="text-zinc-500 hover:text-zinc-200"
              onClick={onClose}
              title="Close viewer"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {/* Loading / Error overlay */}
      {(loading || error) && (
        <div className="absolute inset-0 z-10 flex items-center justify-center pointer-events-none">
          {loading && !error && (
            <div className="flex items-center gap-2 text-zinc-400 text-xs font-mono">
              <Loader2 className="w-4 h-4 animate-spin" />
              {mode === "query" ? "synthesizing across corpora…" : "loading graph…"}
            </div>
          )}
          {error && (
            <div className="text-rose-400 text-xs font-mono pointer-events-auto bg-zinc-900 border border-rose-900 rounded px-3 py-2 max-w-md">
              {error}
            </div>
          )}
        </div>
      )}

      {/* Main canvas + optional prose panel */}
      <div className="flex h-full w-full">
        <div className="flex-1 min-w-0 relative">
          {data && (
            <ForceGraph2D
              ref={fgRef as any}
              graphData={data as RFGraphData<ViewerNode, ViewerLink>}
              width={mode === "query" && q.synthesis ? size.w * 0.6 : size.w}
              height={size.h}
              backgroundColor="#0a0a0a"
              nodeRelSize={4}
              nodeCanvasObject={drawNode}
              nodeCanvasObjectMode={() => "replace"}
              linkColor={linkColor}
              linkWidth={linkWidth}
              linkDirectionalParticles={0}
              cooldownTicks={mode === "query" ? 80 : 200}
              d3AlphaDecay={mode === "query" ? 0.025 : 0.04}
              d3VelocityDecay={0.4}
              onNodeHover={(n: any) => setHoveredId(n ? n.id : null)}
              onNodeClick={(n: any) => handleNodeClick(n as ViewerNode)}
              enableNodeDrag={false}
              minZoom={0.2}
              maxZoom={8}
            />
          )}
        </div>
        {mode === "query" && q.synthesis && (
          <AnimatedDiv
            className="w-[40%] min-w-[320px] max-w-[640px] border-l border-zinc-800 bg-zinc-950/80 backdrop-blur overflow-y-auto"
            style={{
              opacity: skipAnimations
                ? 1
                : entryAnim.progress.to((p: number) => Math.max(0, (p - 0.6) / 0.4)),
            }}
          >
            <div className="p-4 space-y-3">
              <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono">
                synthesis
              </div>
              <div className="prose prose-invert prose-sm max-w-none">
                <ReactMarkdown
                  components={{
                    a: ({ children, href, ...props }) => (
                      <a
                        {...props}
                        href={href}
                        onClick={(e) => {
                          if (href?.startsWith("#cite-")) {
                            e.preventDefault();
                            handleCitationClick(href.slice(6));
                          }
                        }}
                      >
                        {children}
                      </a>
                    ),
                  }}
                >
                  {q.synthesis.markdown}
                </ReactMarkdown>
              </div>
              {q.synthesis.sources && q.synthesis.sources.length > 0 && (
                <div className="border-t border-zinc-800 pt-3">
                  <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono mb-2">
                    sources ({q.synthesis.sources.length})
                  </div>
                  <ol className="text-xs text-zinc-400 space-y-1 list-decimal list-inside">
                    {q.synthesis.sources.map((s: any, i: number) => (
                      <li key={s.chunk_id || i} className="truncate" title={s.snippet}>
                        {s.source_label || s.doc_id || s.chunk_id}
                      </li>
                    ))}
                  </ol>
                </div>
              )}
              {q.synthesis.perCorpus && q.synthesis.perCorpus.length > 1 && (
                <details className="border-t border-zinc-800 pt-3">
                  <summary className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono cursor-pointer">
                    per-corpus syntheses ({q.synthesis.perCorpus.length})
                  </summary>
                  <div className="mt-2 space-y-3">
                    {q.synthesis.perCorpus.map((p) => (
                      <div key={p.corpus_id}>
                        <div className="text-xs font-mono text-amber-400 mb-1">
                          {p.corpus_id.slice(0, 8)}
                        </div>
                        <div className="prose prose-invert prose-sm max-w-none text-zinc-300">
                          <ReactMarkdown>{p.markdown}</ReactMarkdown>
                        </div>
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          </AnimatedDiv>
        )}
      </div>
    </div>
  );
}

export default GraphViewer;
