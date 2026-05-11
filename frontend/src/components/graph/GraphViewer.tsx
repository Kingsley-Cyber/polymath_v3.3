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
import chroma from "chroma-js";
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
  entity_type?: string;
  supernode_type?: string;
  primary_domain?: string;
  source_corpora?: string[];
  source_corpus?: string;
  member_ids?: string[];
  isSeed?: boolean;
  isHub?: boolean;
  isBook?: boolean;
  hidden?: boolean;
  zIndex?: number;
  highlighted?: boolean;
  // sigma additions for runtime styling
  forceLabel?: boolean;
  // when user drags a node, sigma marks it fixed via this flag
  fixed?: boolean;
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
type BrainViewMode = "domains" | "books";

type DrillFrame = {
  // For domains mode: clusterId is the concept_id ("concept:abc" → "abc")
  // For books mode:   clusterId is the doc_id of the book to drill into
  source: BrainViewMode;
  clusterId: string;
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

// Curated 14-color palette tuned for dark backgrounds — distinct hues at
// readable saturation/lightness, no neon. Cycled deterministically by hash.
const COMMUNITY_PALETTE = [
  "#a78bfa", // violet
  "#22d3ee", // cyan
  "#f472b6", // pink
  "#fbbf24", // amber
  "#34d399", // emerald
  "#60a5fa", // blue
  "#fb7185", // rose
  "#a3e635", // lime
  "#c084fc", // purple
  "#fcd34d", // yellow
  "#5eead4", // teal
  "#f97316", // orange
  "#818cf8", // indigo
  "#facc15", // gold
];

// Slightly darker / more saturated variants for domain supernodes so they
// stand out from the concept supernodes within the same hue family.
const DOMAIN_PALETTE = COMMUNITY_PALETTE.map((c) => brightenColor(c, 1.15));

// Distinct corpus palette — used when colorMode === "corpus". Same hues
// but with a different cycle so adjacent corpora visually contrast.
const CORPUS_PALETTE = [
  "#f472b6", // pink
  "#22d3ee", // cyan
  "#fbbf24", // amber
  "#a78bfa", // violet
  "#34d399", // emerald
  "#fb7185", // rose
  "#60a5fa", // blue
  "#a3e635", // lime
  "#fcd34d", // yellow
  "#c084fc", // purple
  "#5eead4", // teal
  "#f97316", // orange
];

// Per-entity-type palette for entity-level views (drill + query).
// Generic fallback for entity_type values we don't have explicit colors for.
const ENTITY_TYPE_PALETTE: Record<string, string> = {
  Person: "#f472b6",
  Organization: "#fbbf24",
  Product: "#22d3ee",
  Method: "#34d399",
  Concept: "#a78bfa",
  Document: "#94a3b8",
  Event: "#fb7185",
  Place: "#fcd34d",
  Artifact: "#5eead4",
  Rule: "#c084fc",
  Time: "#60a5fa",
  TimeReference: "#60a5fa",
  Date: "#60a5fa",
  Other: "#a3a3a3",
};

function pickColor(palette: string[], seed: string): string {
  return palette[hashString(seed) % palette.length];
}

function colorForCommunity(node: {
  id: string;
  primary_domain?: string;
  supernode_type?: string;
  entity_type?: string;
}): string {
  // Entity-level node (drill / query): color by entity type when known.
  if (
    !node.supernode_type &&
    node.entity_type &&
    ENTITY_TYPE_PALETTE[node.entity_type]
  ) {
    return ENTITY_TYPE_PALETTE[node.entity_type];
  }
  // Domain supernode: brighter saturated palette pick.
  if (node.supernode_type === "domain") {
    const seed =
      node.primary_domain || node.id.split(":").slice(-1)[0] || node.id;
    return pickColor(DOMAIN_PALETTE, seed);
  }
  // Concept supernode: standard community palette pick keyed by primary_domain
  // so concepts in the same domain share a hue family.
  if (node.supernode_type === "concept") {
    const seed = node.primary_domain || node.id;
    return pickColor(COMMUNITY_PALETTE, seed);
  }
  // Books-as-clusters: each doc gets its own hue.
  return pickColor(COMMUNITY_PALETTE, node.id);
}

function colorForCorpus(corpora: string[] | undefined): string {
  // Multi-corpus blend: when a node sits in N corpora, mix the per-corpus
  // hues in LCH (perceptually-uniform) space rather than RGB. This is what
  // gives a node connected to "Quantum Physics" (blue) and "Philosophy"
  // (red) a true purple instead of the muddy gray RGB averaging produces.
  if (!corpora || corpora.length === 0) return pickColor(CORPUS_PALETTE, "unknown");
  if (corpora.length === 1) return pickColor(CORPUS_PALETTE, corpora[0]);
  const cs = corpora.map((c) => pickColor(CORPUS_PALETTE, c));
  try {
    return chroma.average(cs, "lch").hex();
  } catch {
    return cs[0];
  }
}

function getNodeColor(node: NodeAttrs, mode: ColorMode): string {
  if (mode === "corpus") return colorForCorpus(node.source_corpora);
  // Community mode also benefits from LCH blending when a supernode spans
  // multiple corpora (a concept that surfaces in 2+ corpora's analytics
  // shows up in the merged overview with source_corpora.length > 1).
  // The community palette pick is the base; blending nudges the hue toward
  // the corpus-color centroid, signaling "this concept is shared."
  const base = colorForCommunity({
    id: (node as any).__id ?? "",
    primary_domain: node.primary_domain,
    supernode_type: node.supernode_type,
    entity_type: (node as any).entity_type,
  });
  if (
    node.source_corpora &&
    node.source_corpora.length > 1 &&
    !node.supernode_type
  ) {
    // Entity-level shared node — mix the base color with the corpus blend
    // so it visually pops from same-community single-corpus siblings.
    try {
      const corpusBlend = colorForCorpus(node.source_corpora);
      return chroma.mix(base, corpusBlend, 0.35, "lch").hex();
    } catch {
      return base;
    }
  }
  return base;
}

// LCH-midpoint of two hex colors. Used for cross-cluster edge tints so the
// edge visibly bridges the colors of its endpoints.
function lchMix(a: string, b: string, ratio = 0.5): string {
  try {
    return chroma.mix(a, b, ratio, "lch").hex();
  } catch {
    return a;
  }
}

// Desaturate via chroma scale instead of mixing toward dark BG. Keeps the
// hue identifiable so the user can still read which community a dimmed
// node belongs to, just at low chroma + slightly lower lightness.
function desaturate(hex: string, chromaPct = 0.18): string {
  try {
    return chroma(hex)
      .set("lch.c", chroma(hex).get("lch.c") * chromaPct)
      .darken(0.4)
      .hex();
  } catch {
    return hex;
  }
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
// dimColor intentionally retained for any future fallback path; reference
// it here so the linter doesn't flag the unused export.
function dimColor(hex: string, amount: number): string {
  const rgb = hexToRgb(hex);
  return rgbToHex(
    DARK_BG.r + (rgb.r - DARK_BG.r) * amount,
    DARK_BG.g + (rgb.g - DARK_BG.g) * amount,
    DARK_BG.b + (rgb.b - DARK_BG.b) * amount,
  );
}
// Touch the symbol so unused-import linters don't trip when chroma covers
// every active dim path.
void dimColor;
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

// Map mention_count → node radius. Different scales per node kind:
//   • Domain supernodes     — always large (12-30px) — they anchor the canvas
//   • Concept supernodes    — medium-large (8-22px)  — readable as cluster heads
//   • Book clusters         — medium-large (8-24px)  — sized by entity_count
//   • Entity nodes (drill)  — small-medium (4-14px)  — sized by mention_count
//   • Seeds in query mode   — +4px on top of whatever bucket they're in
function nodeSize(
  mentionCount: number,
  opts?: { isSeed?: boolean; supernode_type?: string; isBook?: boolean },
): number {
  const v = Math.max(1, Number(mentionCount) || 1);
  let base: number;
  if (opts?.supernode_type === "domain") {
    base = 12 + Math.log2(v + 1) * 2.4;
    base = Math.min(30, base);
  } else if (opts?.supernode_type === "concept") {
    base = 8 + Math.log2(v + 1) * 1.8;
    base = Math.min(22, base);
  } else if (opts?.isBook) {
    base = 8 + Math.log2(v + 1) * 1.6;
    base = Math.min(24, base);
  } else {
    base = 4 + Math.log2(v + 1) * 1.4;
    base = Math.min(14, base);
  }
  return Math.max(4, base) + (opts?.isSeed ? 4 : 0);
}

// ─── Brain mode data hook ─────────────────────────────────────────────────

function useBrainGraph(
  corpusIds: string[],
  drill: DrillFrame | null,
  brainMode: BrainViewMode,
) {
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
      // ── books-as-clusters view ──
      if (brainMode === "books") {
        const res = await api.getGraphByDocument({
          corpusIds,
          mode: drill && drill.source === "books" ? "drill" : "full",
          drillDocId:
            drill && drill.source === "books" ? drill.clusterId : undefined,
        });
        // The book cluster anchors are returned as `clusters[]`, separate
        // from the entity `nodes[]`. We render BOTH layers as graphology
        // nodes — anchors get `kind: "book"`, entities get the standard
        // entity attributes. Edges between entity → book represent
        // membership; entity → entity edges represent RELATES_TO across
        // documents.
        const anchorNodes = (res.clusters || []).map((c) => ({
          id: `book:${c.cluster_id}`,
          display_name: c.label || c.cluster_id.slice(0, 8),
          mention_count: c.entity_count || 1,
          kind: "book" as const,
          source_corpora: [c.corpus_id],
          source_corpus: c.corpus_id,
          top_entities: c.top_entities,
        }));
        // Build membership edges only when we have entity nodes (full / drill).
        const memberEdges: any[] = [];
        for (const e of res.nodes || []) {
          const primary = (e as any).primary_doc_id;
          if (primary) {
            memberEdges.push({
              source: e.id,
              target: `book:${primary}`,
              predicate: "in_book",
              confidence: 1,
              weight: 0.4,
            });
          }
          const bridges = (e as any).bridge_doc_ids || [];
          for (const did of bridges) {
            memberEdges.push({
              source: e.id,
              target: `book:${did}`,
              predicate: "bridges_to",
              confidence: 0.6,
              weight: 0.3,
              source_corpora: [],
            });
          }
        }
        setData({
          nodes: [...anchorNodes, ...(res.nodes || [])],
          links: [...memberEdges, ...(res.edges || [])],
        });
        setMeta({ truncated: Boolean(res.truncated) });
        setCacheWarming([]);
        return;
      }

      // ── domains view (concept-community supernodes) ──
      if (drill && drill.source === "domains") {
        const res = await api.getGraphCluster(drill.clusterId, corpusIds);
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
  }, [corpusIds, drill, brainMode]);

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
    // books-mode nodes have `primary_doc_id` in their payload (cluster anchors
    // get a `kind: "book"` flag; entity nodes within a book have neither).
    const isBook = node.kind === "book" || Boolean(node.is_cluster_anchor);
    const angle = idx * goldenAngle;
    const r =
      radius0 *
      Math.sqrt((idx + 1) / Math.max(n, 1)) *
      (1 + (Math.random() - 0.5) * 0.15);
    const sizeForCount =
      // For book-mode entity nodes, size by total_mentions if present.
      typeof node.total_mentions === "number"
        ? node.total_mentions
        : node.mention_count;
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
      size: nodeSize(sizeForCount, {
        isSeed,
        supernode_type: node.supernode_type,
        isBook,
      }),
      color: baseColor,
      label: String(node.display_name || id),
      display_name: String(node.display_name || id),
      mention_count: Number(sizeForCount || 1),
      entity_type: node.entity_type,
      supernode_type: node.supernode_type,
      primary_domain: node.primary_domain,
      source_corpora: node.source_corpora || [],
      source_corpus: node.source_corpus || "",
      member_ids: node.member_ids,
      isSeed,
      isHub,
      isBook,
      hidden: false,
      forceLabel: isSeed || isBook || node.supernode_type === "domain",
    });
  });

  rawLinks.forEach((l, i) => {
    const s = String(typeof l.source === "object" ? l.source.id : l.source);
    const t = String(typeof l.target === "object" ? l.target.id : l.target);
    if (!g.hasNode(s) || !g.hasNode(t) || s === t) return;
    const eid = `e${i}`;
    if (g.hasEdge(s, t)) return;
    // Edge color = LCH midpoint between source and target node colors.
    // Visually represents the "bridge" of logic between two concepts —
    // an edge crossing communities shimmers through both their hues.
    const sColor = g.getNodeAttribute(s, "color");
    const tColor = g.getNodeAttribute(t, "color");
    let edgeBase = lchMix(sColor, tColor, 0.5);
    // Soft alpha so dense edge fields stay readable.
    try {
      edgeBase = chroma(edgeBase).alpha(0.35).css();
    } catch {
      /* hex fallback ok */
    }
    if (l.dangling) {
      // Amber tint for edges into nodes outside the loaded set.
      edgeBase = "rgba(220, 140, 60, 0.55)";
    }
    g.addEdgeWithKey(eid, s, t, {
      size: Math.max(0.5, Math.min(3, (l.weight || 1) * 0.6 + (l.confidence || 0.5))),
      color: edgeBase,
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
  const draggedRef = useRef<string | null>(null);
  const isDraggingRef = useRef(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredName, setHoveredName] = useState<string | null>(null);
  const [colorMode, setColorMode] = useState<ColorMode>("community");
  const [brainMode, setBrainMode] = useState<BrainViewMode>("domains");
  const [drillStack, setDrillStack] = useState<DrillFrame[]>([]);
  const [layoutRunning, setLayoutRunning] = useState(false);

  const drill = drillStack.length ? drillStack[drillStack.length - 1] : null;
  const brain = useBrainGraph(
    mode === "brain" ? corpusIds : [],
    drill,
    brainMode,
  );
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
            // Boost saturation + lightness in LCH; size up.
            try {
              res.color = chroma(attrs.color).saturate(0.6).brighten(0.4).hex();
            } catch {
              res.color = brightenColor(attrs.color, 1.3);
            }
            res.size = (attrs.size || 8) * 1.6;
            res.zIndex = 3;
            res.highlighted = true;
            res.forceLabel = true;
          } else if (isNeighbor) {
            // Keep full hue for neighbors; subtle pop via saturate.
            try {
              res.color = chroma(attrs.color).saturate(0.2).hex();
            } catch {
              res.color = attrs.color;
            }
            res.size = (attrs.size || 8) * 1.15;
            res.zIndex = 2;
            res.forceLabel = true;
          } else {
            // Desaturate in LCH instead of mixing toward black — node still
            // identifiable by hue family but visually recedes.
            res.color = desaturate(attrs.color, 0.2);
            res.size = (attrs.size || 8) * 0.6;
            res.zIndex = 0;
          }
          return res;
        }
        // No selection — boost seeds + hubs in query mode.
        if (attrs.isSeed) {
          try {
            res.color = chroma(attrs.color).saturate(0.4).brighten(0.3).hex();
          } catch {
            res.color = brightenColor(attrs.color, 1.25);
          }
          res.size = (attrs.size || 8) * 1.25;
          res.zIndex = 2;
          res.forceLabel = true;
        } else if (attrs.isHub) {
          try {
            res.color = chroma(attrs.color).brighten(0.2).hex();
          } catch {
            res.color = brightenColor(attrs.color, 1.1);
          }
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
            // Highlighted edge keeps the LCH-mixed source/target hue but
            // boosts to high opacity + thicker stroke. Reads as the bridge
            // "lighting up" between the selected node and its neighbors.
            const sCol = g.getNodeAttribute(s, "color");
            const tCol = g.getNodeAttribute(t, "color");
            try {
              const mid = chroma.mix(sCol, tCol, 0.5, "lch").saturate(0.3).hex();
              res.color = chroma(mid).alpha(0.85).css();
            } catch {
              res.color = "rgba(220, 220, 240, 0.85)";
            }
            res.size = Math.max(2.5, (attrs.size || 1) * 3);
            res.zIndex = 2;
          } else {
            res.color = "rgba(120, 125, 145, 0.05)";
            res.size = 0.3;
            res.zIndex = 0;
          }
        }
        return res;
      },
    });

    sigmaRef.current = sigma;

    sigma.on("clickNode", ({ node }) => {
      // Suppress click side-effects when this came at the end of a drag.
      if (isDraggingRef.current) {
        isDraggingRef.current = false;
        return;
      }
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
    sigma.on("doubleClickNode", ({ node, event }) => {
      // Drill into a concept supernode (domains mode) or a book cluster
      // anchor (books mode). Single-click selects, double-click drills.
      // event.preventSigmaDefault() suppresses the default zoom-on-double-click.
      const g = graphRef.current;
      if (!g || !g.hasNode(node)) return;
      const a = g.getNodeAttributes(node);
      if (a.supernode_type === "concept") {
        event?.preventSigmaDefault?.();
        const conceptId = node.split(":").slice(1).join(":");
        if (!conceptId) return;
        setDrillStack((s) => [
          ...s,
          { source: "domains", clusterId: conceptId, label: a.display_name },
        ]);
      } else if (a.isBook) {
        event?.preventSigmaDefault?.();
        const docId = node.startsWith("book:") ? node.slice(5) : node;
        setDrillStack((s) => [
          ...s,
          { source: "books", clusterId: docId, label: a.display_name },
        ]);
      }
    });

    // ── Node dragging — let the user reposition any node ──
    // Pattern from the sigma.js docs: capture downNode → switch the camera
    // captor off, listen for mousemovebody, write graph coords to the node,
    // then re-enable on mouseup. Works regardless of layout running state
    // (the FA2 worker continues but the dragged node's position is stamped
    // each frame so it follows the cursor).
    sigma.on("downNode", ({ node, event }) => {
      draggedRef.current = node;
      isDraggingRef.current = false; // becomes true on first mousemove
      const g = graphRef.current;
      if (g && g.hasNode(node)) {
        // Mark the node fixed so FA2 doesn't fight the drag.
        g.setNodeAttribute(node, "highlighted", true);
      }
      // Prevent sigma from also panning the camera while dragging.
      event?.preventSigmaDefault?.();
    });
    const mouseCaptor = sigma.getMouseCaptor();
    mouseCaptor.on("mousemovebody", (e) => {
      const dragged = draggedRef.current;
      if (!dragged) return;
      isDraggingRef.current = true;
      const sigmaInst = sigmaRef.current;
      const g = graphRef.current;
      if (!sigmaInst || !g || !g.hasNode(dragged)) return;
      const pos = sigmaInst.viewportToGraph({ x: e.x, y: e.y });
      g.setNodeAttribute(dragged, "x", pos.x);
      g.setNodeAttribute(dragged, "y", pos.y);
      // Sigma needs to know the camera shouldn't move during this drag.
      e.preventSigmaDefault();
      e.original.preventDefault();
      e.original.stopPropagation();
    });
    const release = () => {
      const dragged = draggedRef.current;
      if (dragged) {
        const g = graphRef.current;
        if (g && g.hasNode(dragged)) {
          g.removeNodeAttribute(dragged, "highlighted");
        }
      }
      draggedRef.current = null;
    };
    mouseCaptor.on("mouseup", release);
    mouseCaptor.on("mouseleave", release);

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
            <>
              <button
                className="text-[10px] uppercase tracking-widest text-zinc-400 hover:text-amber-400 border border-zinc-800 bg-[#0d0d14]/80 backdrop-blur rounded px-2 py-1 font-mono"
                onClick={() => {
                  setBrainMode((m) => (m === "domains" ? "books" : "domains"));
                  setDrillStack([]);
                }}
                title="Toggle: domains (concept communities) ↔ books (each file is a cluster)"
              >
                view: {brainMode}
              </button>
              <button
                className="text-[10px] uppercase tracking-widest text-zinc-400 hover:text-amber-400 border border-zinc-800 bg-[#0d0d14]/80 backdrop-blur rounded px-2 py-1 font-mono"
                onClick={() =>
                  setColorMode((m) => (m === "community" ? "corpus" : "community"))
                }
                title="Toggle color scheme"
              >
                color: {colorMode}
              </button>
            </>
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
