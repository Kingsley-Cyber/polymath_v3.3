/**
 * GraphViewer — sigma.js v3 + graphology + ForceAtlas2 + edge-curve.
 *
 * Patterned after GitNexus's polished sigma viewer (golden-angle initial
 * positioning, FA2 with adaptive settings, noverlap cleanup, curved edges,
 * dark hover pill, dim/highlight nodeReducer + edgeReducer).
 *
 * Two modes:
 *   • brain — multi-corpus cached supernode overview from
 *     POST /api/graph/overview. Click a concept supernode to drill via
 *     POST /api/graph/cluster/{concept_id}. Breadcrumb pops back.
 *   • query — Agent Query subgraph + Mission Control synthesis prose.
 *     Seeds get a halo + larger size; bridges + hubs auto-highlight; gaps
 *     surface as separate inline list. Synthesis prose pane on the right.
 *
 * Color toggle: community (default; HSL hue from primary_domain or node id)
 * vs corpus (HSL hue from source_corpora[0]).
 *
 * Cache warming chip + 15s poll against /api/corpora/{cid}/cache-status.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Sigma from "sigma";
import Graph from "graphology";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import forceAtlas2 from "graphology-layout-forceatlas2";
import noverlap from "graphology-layout-noverlap";
import EdgeCurveProgram from "@sigma/edge-curve";
import ReactMarkdown from "react-markdown";
import {
  ChevronLeft,
  Loader2,
  Maximize2,
  Pause,
  Play,
  X,
  Zap,
  ZoomIn,
  ZoomOut,
} from "lucide-react";

import * as api from "../../lib/api";

// ─── Types ────────────────────────────────────────────────────────────────

export type GraphViewerMode = "brain" | "query";

interface GraphViewerProps {
  mode: GraphViewerMode;
  corpusIds: string[];
  query?: string;
  onRerun?: () => void;
  onClose?: () => void;
  model?: string;
}

interface NodeAttrs {
  x: number;
  y: number;
  size: number;
  color: string;
  label: string;
  // Polymath payload metadata.
  display_name: string;
  mention_count: number;
  supernode_type?: string;
  primary_domain?: string;
  source_corpora?: string[];
  source_corpus?: string;
  member_ids?: string[];
  isSeed?: boolean;
  isHub?: boolean;
  hidden?: boolean;
  zIndex?: number;
  highlighted?: boolean;
  // sigma additions for runtime styling
  forceLabel?: boolean;
}

interface EdgeAttrs {
  size: number;
  color: string;
  type?: string;
  predicate?: string;
  weight?: number;
  confidence?: number;
  source_corpora?: string[];
  source_corpus?: string;
  dangling?: boolean;
  hidden?: boolean;
  zIndex?: number;
}

type ColorMode = "community" | "corpus";

type DrillFrame = {
  conceptId: string;
  label: string;
};

// ─── Color helpers ────────────────────────────────────────────────────────

function hashString(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) >>> 0;
  }
  return h;
}

function hslToHex(h: number, s: number, l: number): string {
  s /= 100;
  l /= 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => {
    const v = l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return Math.round(255 * v)
      .toString(16)
      .padStart(2, "0");
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

function colorForCommunity(node: {
  id: string;
  primary_domain?: string;
  supernode_type?: string;
}): string {
  const seed =
    node.primary_domain || node.id.split(":").slice(-1)[0] || node.id;
  const hue = hashString(seed) % 360;
  if (node.supernode_type === "domain") return hslToHex(hue, 70, 60);
  if (node.supernode_type === "concept") return hslToHex(hue, 55, 65);
  return hslToHex(hue, 50, 65);
}

function colorForCorpus(corpora: string[] | undefined): string {
  const seed = (corpora && corpora[0]) || "unknown";
  return hslToHex(hashString(seed) % 360, 65, 55);
}

function getNodeColor(node: NodeAttrs, mode: ColorMode): string {
  if (mode === "corpus") return colorForCorpus(node.source_corpora);
  return colorForCommunity({
    id: (node as any).__id ?? "",
    primary_domain: node.primary_domain,
    supernode_type: node.supernode_type,
  });
}

// Mix toward dark background so dimmed nodes still hint at color.
const DARK_BG = { r: 14, g: 14, b: 22 };

function hexToRgb(hex: string): { r: number; g: number; b: number } {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!m) return { r: 110, g: 110, b: 130 };
  return {
    r: parseInt(m[1], 16),
    g: parseInt(m[2], 16),
    b: parseInt(m[3], 16),
  };
}
function rgbToHex(r: number, g: number, b: number): string {
  const c = (n: number) =>
    Math.max(0, Math.min(255, Math.round(n)))
      .toString(16)
      .padStart(2, "0");
  return `#${c(r)}${c(g)}${c(b)}`;
}
function dimColor(hex: string, amount: number): string {
  const rgb = hexToRgb(hex);
  return rgbToHex(
    DARK_BG.r + (rgb.r - DARK_BG.r) * amount,
    DARK_BG.g + (rgb.g - DARK_BG.g) * amount,
    DARK_BG.b + (rgb.b - DARK_BG.b) * amount,
  );
}
function brightenColor(hex: string, factor: number): string {
  const rgb = hexToRgb(hex);
  return rgbToHex(
    rgb.r + ((255 - rgb.r) * (factor - 1)) / factor,
    rgb.g + ((255 - rgb.g) * (factor - 1)) / factor,
    rgb.b + ((255 - rgb.b) * (factor - 1)) / factor,
  );
}

// ─── ForceAtlas2 + noverlap settings (adaptive) ───────────────────────────

const NOVERLAP_SETTINGS = {
  maxIterations: 30,
  ratio: 1.1,
  margin: 8,
  expansion: 1.05,
};

function getFA2Settings(nodeCount: number) {
  const isSmall = nodeCount < 200;
  const isMedium = nodeCount >= 200 && nodeCount < 1500;
  const isLarge = nodeCount >= 1500;
  return {
    gravity: isSmall ? 0.8 : isMedium ? 0.5 : isLarge ? 0.3 : 0.15,
    scalingRatio: isSmall ? 12 : isMedium ? 25 : isLarge ? 50 : 80,
    slowDown: isSmall ? 1 : isMedium ? 2 : isLarge ? 3 : 5,
    barnesHutOptimize: nodeCount > 100,
    barnesHutTheta: isLarge ? 0.8 : 0.6,
    strongGravityMode: false,
    outboundAttractionDistribution: true,
    linLogMode: false,
    adjustSizes: true,
    edgeWeightInfluence: 1,
  };
}

function getLayoutDuration(nodeCount: number): number {
  if (nodeCount > 5000) return 25000;
  if (nodeCount > 1500) return 18000;
  if (nodeCount > 500) return 14000;
  return 10000;
}

// Map mention_count → node radius (log-scaled, clamped).
function nodeSize(mentionCount: number, isSeed?: boolean): number {
  const base = Math.log2((mentionCount || 1) + 1) * 2.5 + 4;
  const clamped = Math.max(4, Math.min(22, base));
  return clamped + (isSeed ? 4 : 0);
}

// ─── Brain mode data hook ─────────────────────────────────────────────────

function useBrainGraph(corpusIds: string[], drill: DrillFrame | null) {
  const [data, setData] = useState<{
    nodes: any[];
    links: any[];
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
        const res = await api.getGraphCluster(drill.conceptId, corpusIds);
        setData({ nodes: res.nodes || [], links: res.edges || [] });
        setMeta({ truncated: res.truncated });
        setCacheWarming(res._meta?.cache_warming_corpora || []);
      } else {
        const res = await api.getGraphOverviewMulti(corpusIds);
        setData({ nodes: res.nodes || [], links: res.edges || [] });
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
        if (stillWarming.length === 0) reload();
        else setCacheWarming(stillWarming);
      } catch {
        /* swallow */
      }
    }, 15000);
    return () => clearInterval(t);
  }, [cacheWarming, reload]);

  return { data, loading, error, cacheWarming, meta, reload };
}

// ─── Query mode data hook ─────────────────────────────────────────────────

function useQueryGraph(
  corpusIds: string[],
  query: string | undefined,
  model: string | undefined,
) {
  const [phase, setPhase] = useState<"idle" | "loading" | "ready" | "error">(
    "idle",
  );
  const [data, setData] = useState<{ nodes: any[]; links: any[] } | null>(null);
  const [seedIds, setSeedIds] = useState<Set<string>>(new Set());
  const [hubIds, setHubIds] = useState<Set<string>>(new Set());
  const [bridgeIds, setBridgeIds] = useState<Set<string>>(new Set());
  const [gaps, setGaps] = useState<any[]>([]);
  const [synthesis, setSynthesis] = useState<{
    markdown: string;
    sources: any[];
    perCorpus?: Array<{ corpus_id: string; markdown: string }>;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancel = false;
    const run = async () => {
      if (!corpusIds.length || !query?.trim()) return;
      setPhase("loading");
      setError(null);
      try {
        const subgraphP = api.queryGraph(corpusIds, query, 2, 50);
        const synthP = api.discoverGraph({
          corpus_ids: corpusIds as any,
          query,
          mode: "auto",
          ...(model ? { model } : {}),
        } as any);
        const sub = await subgraphP;
        if (cancel) return;
        const seedSet = new Set<string>(
          (sub.seed_entities || []).map((s: any) => String(s.id)),
        );
        const hubSet = new Set<string>(
          (sub.hubs || []).map((h: any) => String(h.entity_id)),
        );
        const bridgeSet = new Set<string>(
          (sub.bridges || []).map((b: any) => String(b.entity_id)),
        );
        setSeedIds(seedSet);
        setHubIds(hubSet);
        setBridgeIds(bridgeSet);
        setGaps(sub.gaps || []);
        setData({ nodes: sub.nodes || [], links: sub.links || [] });
        setPhase("ready");
        const synth = await synthP;
        if (cancel) return;
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
        if (cancel) return;
        setError(e instanceof Error ? e.message : String(e));
        setPhase("error");
      }
    };
    run();
    return () => {
      cancel = true;
    };
  }, [corpusIds, query, model]);

  return {
    phase,
    data,
    seedIds,
    hubIds,
    bridgeIds,
    gaps,
    synthesis,
    error,
  };
}

// ─── Sigma initialization helper ──────────────────────────────────────────

function buildGraphologyGraph(
  rawNodes: any[],
  rawLinks: any[],
  opts: {
    colorMode: ColorMode;
    seedIds: Set<string>;
    hubIds: Set<string>;
    bridgeIds: Set<string>;
  },
): Graph<NodeAttrs, EdgeAttrs> {
  const { colorMode, seedIds, hubIds, bridgeIds } = opts;
  const g = new Graph<NodeAttrs, EdgeAttrs>({ multi: false, type: "directed" });
  const n = rawNodes.length;
  // Initial radial positioning by golden-angle so layout converges fast.
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));
  const radius0 = Math.sqrt(Math.max(n, 50)) * 8;

  rawNodes.forEach((node, idx) => {
    const id = String(node.id);
    if (g.hasNode(id)) return;
    const isSeed = seedIds.has(id);
    const isHub = hubIds.has(id) || bridgeIds.has(id);
    const angle = idx * goldenAngle;
    const r =
      radius0 *
      Math.sqrt((idx + 1) / Math.max(n, 1)) *
      (1 + (Math.random() - 0.5) * 0.15);
    const baseColor = getNodeColor(
      {
        ...node,
        __id: id,
        source_corpora: node.source_corpora || [],
      } as any,
      colorMode,
    );
    g.addNode(id, {
      x: r * Math.cos(angle),
      y: r * Math.sin(angle),
      size: nodeSize(node.mention_count, isSeed),
      color: baseColor,
      label: String(node.display_name || id),
      display_name: String(node.display_name || id),
      mention_count: Number(node.mention_count || 1),
      supernode_type: node.supernode_type,
      primary_domain: node.primary_domain,
      source_corpora: node.source_corpora || [],
      source_corpus: node.source_corpus || "",
      member_ids: node.member_ids,
      isSeed,
      isHub,
      hidden: false,
      forceLabel: isSeed,
    });
  });

  rawLinks.forEach((l, i) => {
    const s = String(typeof l.source === "object" ? l.source.id : l.source);
    const t = String(typeof l.target === "object" ? l.target.id : l.target);
    if (!g.hasNode(s) || !g.hasNode(t) || s === t) return;
    const eid = `e${i}`;
    if (g.hasEdge(s, t)) return;
    const isCross =
      Array.isArray(l.source_corpora) && l.source_corpora.length > 1;
    const baseEdgeColor = l.dangling
      ? "rgba(180, 100, 60, 0.5)"
      : isCross
      ? dimColor("#a78bfa", 0.5)
      : "rgba(170, 175, 195, 0.18)";
    g.addEdgeWithKey(eid, s, t, {
      size: Math.max(0.5, Math.min(3, (l.weight || 1) * 0.6 + (l.confidence || 0.5))),
      color: baseEdgeColor,
      type: "curved",
      predicate: l.predicate,
      weight: l.weight,
      confidence: l.confidence,
      source_corpora: l.source_corpora || [],
      source_corpus: l.source_corpus,
      dangling: Boolean(l.dangling),
    });
  });

  return g;
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
  const containerRef = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const graphRef = useRef<Graph<NodeAttrs, EdgeAttrs> | null>(null);
  const layoutRef = useRef<FA2Layout | null>(null);
  const layoutTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const selectedRef = useRef<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredName, setHoveredName] = useState<string | null>(null);
  const [colorMode, setColorMode] = useState<ColorMode>("community");
  const [drillStack, setDrillStack] = useState<DrillFrame[]>([]);
  const [layoutRunning, setLayoutRunning] = useState(false);

  const drill = drillStack.length ? drillStack[drillStack.length - 1] : null;
  const brain = useBrainGraph(mode === "brain" ? corpusIds : [], drill);
  const q = useQueryGraph(
    mode === "query" ? corpusIds : [],
    query,
    model,
  );

  const data = mode === "brain" ? brain.data : q.data;
  const loading = mode === "brain" ? brain.loading : q.phase === "loading";
  const error = mode === "brain" ? brain.error : q.error;

  // ── Sigma setup ONCE ──
  useEffect(() => {
    if (!containerRef.current) return;

    const graph = new Graph<NodeAttrs, EdgeAttrs>();
    graphRef.current = graph;

    const sigma = new Sigma(graph, containerRef.current, {
      renderLabels: true,
      labelFont:
        "Inter, JetBrains Mono, ui-sans-serif, system-ui, sans-serif",
      labelSize: 12,
      labelWeight: "500",
      labelColor: { color: "#e4e4ed" },
      labelRenderedSizeThreshold: 6,
      labelDensity: 0.12,
      labelGridCellSize: 80,
      defaultNodeColor: "#6b7280",
      defaultEdgeColor: "rgba(170, 175, 195, 0.18)",
      defaultEdgeType: "curved",
      edgeProgramClasses: {
        curved: EdgeCurveProgram,
      },
      minCameraRatio: 0.05,
      maxCameraRatio: 12,
      hideEdgesOnMove: true,
      zIndex: true,
      defaultDrawNodeHover: (
        context: CanvasRenderingContext2D,
        d: any,
        settings: any,
      ) => {
        const label = d.label;
        if (!label) return;
        const size = settings.labelSize || 12;
        const font =
          settings.labelFont ||
          "Inter, JetBrains Mono, ui-sans-serif, system-ui, sans-serif";
        const weight = settings.labelWeight || "500";
        context.font = `${weight} ${size}px ${font}`;
        const textWidth = context.measureText(label).width;
        const nodeSizeR = d.size || 8;
        const x = d.x;
        const y = d.y - nodeSizeR - 12;
        const padX = 10;
        const padY = 6;
        const height = size + padY * 2;
        const width = textWidth + padX * 2;
        const radius = 6;
        // Dark pill.
        context.fillStyle = "#0d0d14";
        context.beginPath();
        if (typeof (context as any).roundRect === "function") {
          (context as any).roundRect(x - width / 2, y - height / 2, width, height, radius);
        } else {
          context.rect(x - width / 2, y - height / 2, width, height);
        }
        context.fill();
        context.strokeStyle = d.color || "#a78bfa";
        context.lineWidth = 1.5;
        context.stroke();
        // Label.
        context.fillStyle = "#f5f5f7";
        context.textAlign = "center";
        context.textBaseline = "middle";
        context.fillText(label, x, y);
        // Subtle ring around node.
        context.beginPath();
        context.arc(d.x, d.y, nodeSizeR + 4, 0, Math.PI * 2);
        context.strokeStyle = d.color || "#a78bfa";
        context.lineWidth = 1.5;
        context.globalAlpha = 0.5;
        context.stroke();
        context.globalAlpha = 1;
      },
      nodeReducer: (node, attrs: any) => {
        const res = { ...attrs };
        if (attrs.hidden) {
          res.hidden = true;
          return res;
        }
        const sel = selectedRef.current;
        if (sel) {
          const g = graphRef.current!;
          const isSelected = node === sel;
          const isNeighbor =
            g.hasEdge(node, sel) || g.hasEdge(sel, node);
          if (isSelected) {
            res.color = brightenColor(attrs.color, 1.3);
            res.size = (attrs.size || 8) * 1.6;
            res.zIndex = 3;
            res.highlighted = true;
            res.forceLabel = true;
          } else if (isNeighbor) {
            res.color = attrs.color;
            res.size = (attrs.size || 8) * 1.15;
            res.zIndex = 2;
            res.forceLabel = true;
          } else {
            res.color = dimColor(attrs.color, 0.18);
            res.size = (attrs.size || 8) * 0.55;
            res.zIndex = 0;
          }
          return res;
        }
        // No selection — boost seeds + hubs in query mode.
        if (attrs.isSeed) {
          res.color = brightenColor(attrs.color, 1.25);
          res.size = (attrs.size || 8) * 1.25;
          res.zIndex = 2;
          res.forceLabel = true;
        } else if (attrs.isHub) {
          res.color = brightenColor(attrs.color, 1.1);
          res.zIndex = 1;
        }
        return res;
      },
      edgeReducer: (edge, attrs: any) => {
        const res = { ...attrs };
        const sel = selectedRef.current;
        if (sel) {
          const g = graphRef.current!;
          const [s, t] = g.extremities(edge);
          if (s === sel || t === sel) {
            res.color = brightenColor("#a78bfa", 1.3);
            res.size = Math.max(2.5, (attrs.size || 1) * 3);
            res.zIndex = 2;
          } else {
            res.color = "rgba(120, 125, 145, 0.06)";
            res.size = 0.3;
            res.zIndex = 0;
          }
        }
        return res;
      },
    });

    sigmaRef.current = sigma;

    sigma.on("clickNode", ({ node }) => {
      selectedRef.current = node;
      setSelectedId(node);
      sigma.refresh();
    });
    sigma.on("clickStage", () => {
      selectedRef.current = null;
      setSelectedId(null);
      sigma.refresh();
    });
    sigma.on("enterNode", ({ node }) => {
      const g = graphRef.current;
      if (g && g.hasNode(node)) {
        const a = g.getNodeAttributes(node);
        setHoveredName(a.display_name || node);
      }
      if (containerRef.current) containerRef.current.style.cursor = "pointer";
    });
    sigma.on("leaveNode", () => {
      setHoveredName(null);
      if (containerRef.current) containerRef.current.style.cursor = "grab";
    });
    sigma.on("doubleClickNode", ({ node }) => {
      // Drill into a concept supernode.
      if (mode !== "brain") return;
      const g = graphRef.current;
      if (!g || !g.hasNode(node)) return;
      const a = g.getNodeAttributes(node);
      if (a.supernode_type !== "concept") return;
      const conceptId = node.split(":").slice(1).join(":");
      if (!conceptId) return;
      setDrillStack((s) => [...s, { conceptId, label: a.display_name }]);
    });

    return () => {
      if (layoutTimeoutRef.current) clearTimeout(layoutTimeoutRef.current);
      layoutRef.current?.kill();
      sigma.kill();
      sigmaRef.current = null;
      graphRef.current = null;
    };
  // mode is captured once — safe; drillStack is set elsewhere
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Run layout helper ──
  const runLayout = useCallback((g: Graph<NodeAttrs, EdgeAttrs>) => {
    const n = g.order;
    if (n === 0) return;
    if (layoutRef.current) {
      layoutRef.current.kill();
      layoutRef.current = null;
    }
    if (layoutTimeoutRef.current) {
      clearTimeout(layoutTimeoutRef.current);
      layoutTimeoutRef.current = null;
    }
    const inferred = forceAtlas2.inferSettings(g);
    const settings = { ...inferred, ...getFA2Settings(n) };
    const layout = new FA2Layout(g, { settings });
    layoutRef.current = layout;
    layout.start();
    setLayoutRunning(true);
    const dur = getLayoutDuration(n);
    layoutTimeoutRef.current = setTimeout(() => {
      if (layoutRef.current) {
        layoutRef.current.stop();
        layoutRef.current = null;
        try {
          noverlap.assign(g, NOVERLAP_SETTINGS);
        } catch {
          /* ignore */
        }
        sigmaRef.current?.refresh();
        setLayoutRunning(false);
      }
    }, dur);
  }, []);

  // ── Push new data into sigma when it lands ──
  useEffect(() => {
    const sigma = sigmaRef.current;
    if (!sigma || !data) return;
    const seedIds = mode === "query" ? q.seedIds : new Set<string>();
    const hubIds = mode === "query" ? q.hubIds : new Set<string>();
    const bridgeIds = mode === "query" ? q.bridgeIds : new Set<string>();
    const newGraph = buildGraphologyGraph(data.nodes, data.links, {
      colorMode,
      seedIds,
      hubIds,
      bridgeIds,
    });
    if (layoutRef.current) {
      layoutRef.current.kill();
      layoutRef.current = null;
    }
    if (layoutTimeoutRef.current) {
      clearTimeout(layoutTimeoutRef.current);
      layoutTimeoutRef.current = null;
    }
    graphRef.current = newGraph;
    sigma.setGraph(newGraph);
    selectedRef.current = null;
    setSelectedId(null);
    runLayout(newGraph);
    sigma.getCamera().animatedReset({ duration: 400 });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-run when underlying data changes
  }, [data, mode, q.seedIds, q.hubIds, q.bridgeIds]);

  // ── Apply colorMode toggle without rebuilding graph ──
  useEffect(() => {
    const g = graphRef.current;
    const sigma = sigmaRef.current;
    if (!g || !sigma) return;
    g.forEachNode((id, attrs) => {
      const newColor = getNodeColor(
        { ...attrs, __id: id } as any,
        colorMode,
      );
      g.mergeNodeAttributes(id, { color: newColor });
    });
    sigma.refresh();
  }, [colorMode]);

  // ── Controls ──
  const handleZoomIn = useCallback(() => {
    sigmaRef.current?.getCamera().animatedZoom({ duration: 220 });
  }, []);
  const handleZoomOut = useCallback(() => {
    sigmaRef.current?.getCamera().animatedUnzoom({ duration: 220 });
  }, []);
  const handleResetView = useCallback(() => {
    selectedRef.current = null;
    setSelectedId(null);
    sigmaRef.current?.getCamera().animatedReset({ duration: 320 });
    sigmaRef.current?.refresh();
  }, []);
  const handleStartLayout = useCallback(() => {
    const g = graphRef.current;
    if (g && g.order > 0) runLayout(g);
  }, [runLayout]);
  const handleStopLayout = useCallback(() => {
    if (layoutTimeoutRef.current) {
      clearTimeout(layoutTimeoutRef.current);
      layoutTimeoutRef.current = null;
    }
    if (layoutRef.current) {
      layoutRef.current.stop();
      layoutRef.current = null;
      try {
        const g = graphRef.current;
        if (g) noverlap.assign(g, NOVERLAP_SETTINGS);
      } catch {
        /* ignore */
      }
      sigmaRef.current?.refresh();
      setLayoutRunning(false);
    }
  }, []);

  const selectedNode = useMemo(() => {
    const g = graphRef.current;
    if (!selectedId || !g || !g.hasNode(selectedId)) return null;
    return { id: selectedId, ...g.getNodeAttributes(selectedId) };
  }, [selectedId]);

  // ── Render ──

  if (corpusIds.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-[#06060a] text-zinc-400">
        <div className="text-center">
          <div className="text-base mb-2 font-mono">
            Select a corpus from the sidebar to begin
          </div>
          <div className="text-xs text-zinc-600 font-mono">
            Tip: select multiple corpora to see cross-book connections
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-full w-full bg-[#06060a]">
      {/* Background gradient — soft purple bloom from center */}
      <div className="pointer-events-none absolute inset-0">
        <div
          className="absolute inset-0"
          style={{
            background: `
              radial-gradient(circle at 50% 50%, rgba(124, 58, 237, 0.04) 0%, transparent 65%),
              linear-gradient(to bottom, #06060a, #0a0a14)
            `,
          }}
        />
      </div>

      {/* Top chrome */}
      <div className="absolute top-3 left-3 right-3 z-20 flex items-start justify-between pointer-events-none">
        <div className="flex flex-col gap-1.5 pointer-events-auto">
          <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono">
            {mode === "brain" ? "Corpora View" : "Query View"}
          </div>
          {mode === "brain" && drillStack.length > 0 && (
            <div className="flex items-center gap-1 text-xs text-zinc-300 font-mono">
              <button
                className="hover:text-amber-400 transition-colors"
                onClick={() => setDrillStack([])}
              >
                Overview
              </button>
              {drillStack.map((f, i) => (
                <span key={i} className="flex items-center gap-1">
                  <ChevronLeft className="w-3 h-3 rotate-180 text-zinc-600" />
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
          <div className="text-[11px] text-zinc-500 font-mono">
            {corpusIds.length} corpora
            {data && ` · ${data.nodes.length} nodes · ${data.links.length} edges`}
            {mode === "brain" && brain.cacheWarming.length > 0 && (
              <span className="ml-2 text-amber-400">
                · {brain.cacheWarming.length} warming…
              </span>
            )}
            {layoutRunning && (
              <span className="ml-2 text-violet-300/80">· settling…</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 pointer-events-auto">
          {mode === "brain" && (
            <button
              className="text-[10px] uppercase tracking-widest text-zinc-400 hover:text-amber-400 border border-zinc-800 bg-[#0d0d14]/80 backdrop-blur rounded px-2 py-1 font-mono"
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
              className="text-[10px] uppercase tracking-widest text-zinc-200 hover:text-amber-400 border border-zinc-800 bg-[#0d0d14]/80 backdrop-blur rounded px-2 py-1 font-mono flex items-center gap-1"
              onClick={onRerun}
              title="Re-run synthesis"
            >
              <Zap className="w-3 h-3" /> re-run
            </button>
          )}
          {onClose && (
            <button
              className="text-zinc-500 hover:text-zinc-200 bg-[#0d0d14]/80 backdrop-blur rounded p-1.5"
              onClick={onClose}
              title="Close viewer"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {/* Hovered node tooltip */}
      {hoveredName && !selectedNode && (
        <div className="pointer-events-none absolute top-16 left-1/2 z-20 -translate-x-1/2 rounded-lg border border-zinc-800 bg-[#0d0d14]/95 px-3 py-1.5 backdrop-blur">
          <span className="font-mono text-sm text-zinc-100">{hoveredName}</span>
        </div>
      )}

      {/* Selection info bar */}
      {selectedNode && (
        <div className="absolute top-16 left-1/2 z-20 flex -translate-x-1/2 items-center gap-2 rounded-xl border border-violet-500/30 bg-violet-500/10 px-4 py-2 backdrop-blur">
          <div className="h-2 w-2 animate-pulse rounded-full bg-violet-300" />
          <span className="font-mono text-sm text-zinc-100">
            {(selectedNode as any).display_name}
          </span>
          {(selectedNode as any).source_corpora?.length ? (
            <span className="text-[10px] text-zinc-400 font-mono">
              · {(selectedNode as any).source_corpora.length} corpora
            </span>
          ) : null}
          <button
            onClick={handleResetView}
            className="ml-2 rounded px-2 py-0.5 text-xs text-zinc-300 transition-colors hover:bg-white/10 hover:text-zinc-50"
          >
            Clear
          </button>
        </div>
      )}

      {/* Loading / error */}
      {(loading || error) && (
        <div className="absolute inset-0 z-10 flex items-center justify-center pointer-events-none">
          {loading && !error && (
            <div className="flex items-center gap-2 text-zinc-400 text-xs font-mono">
              <Loader2 className="w-4 h-4 animate-spin" />
              {mode === "query" ? "synthesizing across corpora…" : "loading graph…"}
            </div>
          )}
          {error && (
            <div className="text-rose-300 text-xs font-mono pointer-events-auto bg-[#0d0d14] border border-rose-900/50 rounded px-3 py-2 max-w-md">
              {error}
            </div>
          )}
        </div>
      )}

      {/* Empty data fallback */}
      {data && data.nodes.length === 0 && !loading && (
        <div className="absolute inset-0 z-10 flex items-center justify-center pointer-events-none">
          <div className="text-center text-zinc-500 text-xs font-mono">
            <div>no graph data yet</div>
            <div className="text-zinc-700 mt-1">
              {mode === "brain"
                ? brain.cacheWarming.length > 0
                  ? `${brain.cacheWarming.length} corpora still warming — wait ~30s`
                  : "the selected corpora have empty supernode overviews"
                : "no query result yet — type a question below"}
            </div>
          </div>
        </div>
      )}

      {/* Sigma + optional prose pane */}
      <div className="absolute inset-0 flex">
        <div
          ref={containerRef}
          className="flex-1 min-w-0 cursor-grab active:cursor-grabbing"
        />
        {mode === "query" && q.synthesis && (
          <div className="w-[40%] min-w-[320px] max-w-[640px] border-l border-zinc-900 bg-[#08080d]/90 backdrop-blur overflow-y-auto z-10">
            <div className="p-4 space-y-3">
              <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono">
                synthesis
              </div>
              <div className="prose prose-invert prose-sm max-w-none">
                <ReactMarkdown>{q.synthesis.markdown}</ReactMarkdown>
              </div>
              {q.synthesis.sources && q.synthesis.sources.length > 0 && (
                <div className="border-t border-zinc-900 pt-3">
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
              {q.gaps && q.gaps.length > 0 && (
                <div className="border-t border-zinc-900 pt-3">
                  <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono mb-2">
                    gaps detected ({q.gaps.length})
                  </div>
                  <ul className="text-xs text-zinc-400 space-y-1">
                    {q.gaps.slice(0, 8).map((g: any, i: number) => (
                      <li key={i}>
                        <span className="text-zinc-300">
                          {g.entity_a_name}
                        </span>{" "}
                        ↔{" "}
                        <span className="text-zinc-300">{g.entity_b_name}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {q.synthesis.perCorpus && q.synthesis.perCorpus.length > 1 && (
                <details className="border-t border-zinc-900 pt-3">
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
          </div>
        )}
      </div>

      {/* Bottom-right control cluster (zoom + reset + layout) */}
      <div className="absolute right-4 bottom-4 z-20 flex flex-col gap-1 pointer-events-auto">
        <button
          onClick={handleZoomIn}
          className="flex h-9 w-9 items-center justify-center rounded-md border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          title="Zoom in"
        >
          <ZoomIn className="h-4 w-4" />
        </button>
        <button
          onClick={handleZoomOut}
          className="flex h-9 w-9 items-center justify-center rounded-md border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          title="Zoom out"
        >
          <ZoomOut className="h-4 w-4" />
        </button>
        <button
          onClick={handleResetView}
          className="flex h-9 w-9 items-center justify-center rounded-md border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          title="Fit to screen"
        >
          <Maximize2 className="h-4 w-4" />
        </button>
        <div className="h-px bg-zinc-800 my-1" />
        <button
          onClick={layoutRunning ? handleStopLayout : handleStartLayout}
          className="flex h-9 w-9 items-center justify-center rounded-md border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          title={layoutRunning ? "Pause layout" : "Resume layout"}
        >
          {layoutRunning ? (
            <Pause className="h-4 w-4" />
          ) : (
            <Play className="h-4 w-4" />
          )}
        </button>
      </div>
    </div>
  );
}

export default GraphViewer;
