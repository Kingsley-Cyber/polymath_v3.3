/**
 * GraphViewer — orchestration layer over `useSigma` (hook port of
 * GitNexus's useSigma.ts) and `polymath-graph-adapter` (Polymath payload
 * → graphology). All rendering / physics / reducer logic lives in those
 * two modules; this file owns:
 *
 *   • Multi-corpus data fetch (Brain View domains/books, Query View)
 *   • Cache-warming poll
 *   • UI chrome: corpus pill stats, color/view toggles, breadcrumb,
 *     hover tooltip, selection bar, controls cluster, prose pane
 *   • Drill stack management (concept community drill, book drill)
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";
import {
  Loader2,
  Maximize2,
  Pause,
  Play,
  ZoomIn,
  ZoomOut,
} from "lucide-react";

import * as api from "../../lib/api";
import { useSigma } from "../../hooks/useSigma";
import {
  polymathToGraphology,
  type ColorMode,
} from "../../lib/polymath-graph-adapter";
import { cleanBookLabel } from "../../lib/label-utils";
import { BrainViewDashboard, type DashboardTab } from "./BrainViewDashboard";
import { GalaxyBackground } from "./GalaxyBackground";

// Pt 4 polish: adaptive top-N forceLabel. At 16 books, hardcoded N=20
// meant every label rendered every frame → overlap storm. The new
// formula scales N with the corpus size so the canvas always has
// breathing room: ~30% of books get the forced label, clamped to
// [3, 24]. A 16-book brain view labels ~5 anchors; 100 books → 24;
// 1000+ → 24 too.
function adaptiveTopN(total: number): number {
  return Math.min(24, Math.max(3, Math.ceil(total * 0.3)));
}

// ─── Types ────────────────────────────────────────────────────────────────

export type GraphViewerMode = "brain" | "query";

interface GraphViewerProps {
  mode: GraphViewerMode;
  corpusIds: string[];
  query?: string;
  onRerun?: () => void;
  onClose?: () => void;
  model?: string;
  /** Pt 7: callback fired when the user picks a refined chip / entity in
   *  the Graph Query tab. Parent typically closes the modal and loads
   *  the text into the chat input. */
  onSendToChat?: (text: string) => void;
}

// Books-as-clusters drill: we only ever drill into a book (no concept-
// community drill anymore — that was the domains mode that's been retired).
type DrillFrame = {
  docId: string;
  label: string;
};

// ─── Brain mode data hook ─────────────────────────────────────────────────

// Note: client-side bridge synthesis from `top_entities` intersection used to
// live here as `computeClusterBridges`. Replaced by /api/graph/brain-view
// which computes ground-truth bridge strengths in Cypher (shared MENTIONS
// + RELATES_TO traversal) so we no longer approximate.

function useBrainGraph(
  corpusIds: string[],
  drill: DrillFrame | null,
) {
  const [data, setData] = useState<{
    nodes: any[];
    links: any[];
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cacheWarming, setCacheWarming] = useState<string[]>([]);
  // Per-corpus cache classification: "missing" (never built — needs manual
  // rebuild) vs "warming" (stale signature — rebuild already needed) vs
  // "ready". Populated by polling /api/corpora/{cid}/cache-status whenever
  // cacheWarming is non-empty.
  const [cacheStatuses, setCacheStatuses] = useState<
    Record<string, api.CacheStatus>
  >({});
  const [rebuildingIds, setRebuildingIds] = useState<Set<string>>(new Set());

  const reload = useCallback(async () => {
    if (corpusIds.length === 0) {
      setData(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      // ── DRILL: one Document anchor + its local entities + cross-book bridges ──
      // Powered by the rich :Document anchor schema — Cypher returns the
      // anchor's local_entities + intra-book relations + cross_book_bridges
      // in one query. No MongoDB enrichment, no Python aggregation.
      if (drill) {
        const drillRes = await api.getBookDrilldown(
          drill.docId,
          corpusIds,
        );

        const anchorRaw =
          drillRes.anchor?.label ||
          drillRes.anchor?.filename ||
          drillRes.anchor?.doc_id?.slice(0, 8) ||
          "";
        const anchorNode: any = drillRes.anchor
          ? {
              id: `book:${drillRes.anchor.doc_id}`,
              display_name: anchorRaw,
              // Sigma renders `label`; tooltip / selection bar reads
              // `display_name` for full context.
              label: cleanBookLabel(anchorRaw) || anchorRaw,
              mention_count: drillRes.anchor.chunk_count || 1,
              kind: "book" as const,
              source_corpora: [drillRes.anchor.corpus_id],
              source_corpus: drillRes.anchor.corpus_id,
              is_cluster_anchor: true,
              forceLabel: true,
            }
          : null;

        const entityNodes = drillRes.local_entities.map((e) => ({
          id: e.entity_id,
          display_name: e.display_name,
          entity_type: e.entity_type,
          object_kind: e.object_kind,
          canonical_family: e.canonical_family,
          primary_doc_id: drill.docId,
          mention_count: 1,
        }));

        // Bridge entities (from cross-book results) become their own nodes
        // so the user sees the literal entity that connects two books.
        const bridgeNodes = drillRes.cross_book_bridges.map((b) => ({
          id: b.bridge_entity_id,
          display_name: b.bridge_entity_name || b.bridge_entity_id,
          entity_type: "Concept",
          mention_count: b.strength || 1,
        }));

        // Edges: book→entity (membership), entity→entity (intra-book), entity→other-book (bridge).
        const memberEdges = entityNodes.map((e) => ({
          source: e.id,
          target: `book:${drill.docId}`,
          predicate: "in_book",
          confidence: 1,
          weight: 0.4,
        }));
        const intraEdges = drillRes.local_relations.map((r) => ({
          source: r.source_id,
          target: r.target_id,
          predicate: r.predicate,
          relation_family: r.relation_family,
          confidence: r.confidence,
        }));
        const bridgeEdges = drillRes.cross_book_bridges.flatMap((b) => [
          {
            source: b.via_entity_id,
            target: b.bridge_entity_id,
            predicate: "bridges_to",
            weight: b.strength,
            confidence: 0.7,
          },
          {
            source: b.bridge_entity_id,
            target: `book:${b.target_doc_id}`,
            predicate: "in_book",
            weight: 0.3,
            confidence: 0.6,
          },
        ]);
        // Synthetic anchors for the bridge target books so the canvas has
        // a node to land the bridge edge on.
        const targetAnchorNodes = drillRes.cross_book_bridges.map((b) => {
          const rawTarget = b.target_filename || b.target_doc_id.slice(0, 8);
          return {
            id: `book:${b.target_doc_id}`,
            display_name: rawTarget,
            label: cleanBookLabel(rawTarget) || rawTarget,
            mention_count: 1,
            kind: "book" as const,
            source_corpora: [b.target_corpus_id || ""],
            source_corpus: b.target_corpus_id || "",
            is_cluster_anchor: true,
          };
        });

        const nodes = [
          ...(anchorNode ? [anchorNode] : []),
          ...entityNodes,
          ...bridgeNodes,
          ...targetAnchorNodes,
        ];
        // Dedup by id (target anchors may collide with the drilled anchor on
        // self-bridges; entity nodes can repeat across local + bridge).
        const seen = new Set<string>();
        const dedupedNodes = nodes.filter((n) => {
          if (seen.has(n.id)) return false;
          seen.add(n.id);
          return true;
        });

        setData({
          nodes: dedupedNodes,
          links: [...memberEdges, ...intraEdges, ...bridgeEdges],
        });
        setCacheWarming([]);
        return;
      }

      // ── TOP-LEVEL: Brain View v2 — pure-Cypher anchors + bridge strengths ──
      // POST /api/graph/brain-view keys off :Document {is_cluster_anchor: true}
      // and computes pairwise bridge strength on the Neo4j side. Anchor
      // metadata (filename, chunk_count, ghost_b_success_rate) lives on the
      // Document node so no MongoDB round-trip is needed.
      const bv = await api.getBrainView(corpusIds);
      // Sort by bridge_count desc so we can tag the top-N anchors with
      // forceLabel — those are the most-connected books and worth always
      // labelling, the long tail relies on semantic zoom.
      const sortedDocs = [...bv.documents].sort(
        (a, b) => (b.bridge_count || 0) - (a.bridge_count || 0),
      );
      const topN = adaptiveTopN(sortedDocs.length);

      // Octopus / spotlight mode. The top SPOTLIGHT_COUNT docs by bridge
      // count grow satellites (orbiting Entity nodes). The long tail stays
      // as plain head-only anchors so the canvas doesn't drown in dots.
      // 100 docs × 8 satellites = 800 satellite nodes max — well within
      // sigma's smooth-render budget.
      const SPOTLIGHT_COUNT = 100;
      const SAT_ORBIT_R = 28; // initial orbit radius (FA2 will adjust)

      const anchorNodes: any[] = [];
      const satelliteNodes: any[] = [];
      const satelliteEdges: any[] = [];

      sortedDocs.forEach((d, idx) => {
        const rawLabel = d.label || d.filename || d.doc_id.slice(0, 8);
        const bookId = `book:${d.doc_id}`;
        anchorNodes.push({
          id: bookId,
          // Full text kept on display_name for tooltip; sigma renders `label`.
          display_name: rawLabel,
          label: cleanBookLabel(rawLabel) || rawLabel,
          // mention_count drives node size in the adapter — use chunk_count
          // so a 5000-chunk book is visually larger than a 50-chunk one.
          mention_count: Math.max(1, d.chunk_count || d.actual_chunk_count || 1),
          kind: "book" as const,
          source_corpora: [d.corpus_id],
          source_corpus: d.corpus_id,
          is_cluster_anchor: true,
          // Top-N strongest anchors keep their label visible at all zoom
          // levels; the long tail relies on semantic-zoom logic in useSigma.
          forceLabel: idx < topN,
          // Pt 5: extraction-schema facets drive deterministic node color
          // in polymath-graph-adapter::pickNodeColor.
          dominant_family: d.dominant_family,
          dominant_entity_type: d.dominant_entity_type,
          // Pass anchor metadata through so the selection bar can render it.
          ghost_b_success_rate: d.ghost_b_success_rate,
          ghost_b_extracted: d.ghost_b_extracted,
          ghost_b_total: d.ghost_b_total,
          chunk_count: d.chunk_count,
          parent_count: d.parent_count,
          bridge_count: d.bridge_count,
          filename: d.filename,
          entity_count: d.entity_count,
        });

        // Only the spotlight (top SPOTLIGHT_COUNT by bridge_count) gets
        // satellites. Long-tail books read as solo dots, keeping the
        // canvas legible at 1000+ books.
        if (idx < SPOTLIGHT_COUNT) {
          const topEntities = d.top_entities || [];
          const satCount = topEntities.length;
          topEntities.forEach((name, i) => {
            // Pre-bake polar position around (0,0) — the adapter resolves
            // the book's anchor position and adds these as offsets, so
            // satellites start in a ring instead of being dragged into
            // orbit by FA2.
            const angle =
              (i / Math.max(satCount, 1)) * Math.PI * 2 + idx * 0.7;
            const entityId = `ent:${d.doc_id}:${i}`;
            satelliteNodes.push({
              id: entityId,
              display_name: name,
              label: name.length > 18 ? name.slice(0, 17) + "…" : name,
              entity_type: "Concept",
              kind: "Concept", // adapter picks Concept color
              source_corpus: d.corpus_id,
              source_corpora: [d.corpus_id],
              // primary_doc_id wires the adapter's "orbit your book"
              // positioning. Without this, FA2 would scatter satellites.
              primary_doc_id: d.doc_id,
              mention_count: 1,
              // Pre-baked polar — adapter adds anchor position.
              x: Math.cos(angle) * SAT_ORBIT_R,
              y: Math.sin(angle) * SAT_ORBIT_R,
            });
            satelliteEdges.push({
              source: bookId,
              target: entityId,
              predicate: "contains",
              weight: 0.2,
            });
          });
        }
      });

      const bridgeLinks = bv.bridges.map((b) => ({
        source: `book:${b.source}`,
        target: `book:${b.target}`,
        predicate: "bridges_to",
        // Use strength on weight so the adapter / sigma reducer thickens
        // bridges by how many cross-book entity pairs they represent.
        weight: b.strength,
        confidence: Math.min(1, (b.shared_entities || 0) / 12),
        // Pt 5: passes through to the adapter, which uses
        // EDGE_COLORS_BY_FAMILY[dominant_relation_family] for the edge color.
        dominant_relation_family: b.dominant_relation_family,
        // Pt 7c: top shared concept names + total count. The adapter
        // builds an on-edge label string like "Bayes' theorem +5" so
        // users see what links two books without clicking.
        top_shared_entities: b.top_shared_entities || [],
        shared_entities: b.shared_entities,
      }));
      setData({
        nodes: [...anchorNodes, ...satelliteNodes],
        links: [...bridgeLinks, ...satelliteEdges],
      });
      setCacheWarming([]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [corpusIds, drill]);

  useEffect(() => {
    reload();
  }, [reload]);

  // Whenever cacheWarming changes, fetch per-corpus statuses ONCE so the
  // chip + Build button can show the real state (missing vs warming vs
  // running rebuild). Then poll every 15s if any are still not ready.
  useEffect(() => {
    if (!cacheWarming.length) {
      setCacheStatuses({});
      return;
    }
    let cancelled = false;
    const refresh = async () => {
      try {
        const [statuses, rebuildState] = await Promise.all([
          Promise.all(cacheWarming.map((cid) => api.getCorpusCacheStatus(cid))),
          api.getGraphCacheRebuildStatus().catch(() => ({
            in_flight: [],
            finished: [],
          })),
        ]);
        if (cancelled) return;
        setCacheStatuses(
          Object.fromEntries(statuses.map((s) => [s.corpus_id, s])),
        );
        setRebuildingIds(new Set(rebuildState.in_flight));
        const stillNotReady = statuses
          .filter(
            (s) => s.metrics_cache !== "ready" || s.domain_cache !== "ready",
          )
          .map((s) => s.corpus_id);
        if (stillNotReady.length === 0) reload();
        else setCacheWarming(stillNotReady);
      } catch {
        /* swallow */
      }
    };
    refresh();
    const t = setInterval(refresh, 15000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [cacheWarming, reload]);

  const triggerRebuild = useCallback(
    async (ids: string[]) => {
      if (ids.length === 0) return;
      try {
        const res = await api.rebuildGraphCache(ids);
        const triggered = new Set([...res.rebuilding, ...res.already_running]);
        setRebuildingIds((prev) => {
          const next = new Set(prev);
          for (const cid of triggered) next.add(cid);
          return next;
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [],
  );

  return {
    data,
    loading,
    error,
    cacheWarming,
    cacheStatuses,
    rebuildingIds,
    triggerRebuild,
    reload,
  };
}

// ─── Query mode data hook ─────────────────────────────────────────────────

function useQueryGraph(
  corpusIds: string[],
  query: string | undefined,
  model: string | undefined,
  synthesisMode: "research" | "ideation" = "research",
  validateSynthesis: boolean = false,
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
          synthesis_mode: synthesisMode,
          validate_synthesis: validateSynthesis,
          ...(model ? { model } : {}),
        } as any);
        const sub = await subgraphP;
        if (cancel) return;
        setSeedIds(
          new Set<string>((sub.seed_entities || []).map((s: any) => String(s.id))),
        );
        setHubIds(
          new Set<string>((sub.hubs || []).map((h: any) => String(h.entity_id))),
        );
        setBridgeIds(
          new Set<string>((sub.bridges || []).map((b: any) => String(b.entity_id))),
        );
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
  }, [corpusIds, query, model, synthesisMode, validateSynthesis]);

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

// ─── Component ────────────────────────────────────────────────────────────

export function GraphViewer({
  mode,
  corpusIds,
  query,
  onRerun,
  onClose,
  model,
  onSendToChat,
}: GraphViewerProps) {
  const [colorMode, setColorMode] = useState<ColorMode>("community");
  const [drillStack, setDrillStack] = useState<DrillFrame[]>([]);
  const [hoveredName, setHoveredName] = useState<string | null>(null);
  // Pt 5: right sidebar dashboard collapse state.
  const [dashboardCollapsed, setDashboardCollapsed] = useState(false);
  // Pt 6: bridge filter knobs (driven by dashboard sliders). Defaults match
  // the Pt 5 hardcoded behavior so the canvas looks the same on load.
  const [minBridgeStrength, setMinBridgeStrength] = useState(2);
  const [maxBridgesPerBook, setMaxBridgesPerBook] = useState(3);
  // Pt 6: re-settle FA2 layout briefly after the user releases a dragged
  // node. Off by default; user toggles in the Layout section of the dashboard.
  const [settleAfterDrag, setSettleAfterDrag] = useState(false);
  // Pt 7: tab selection in the sidebar + Agent Search tab's local input.
  // `agentInput` is what the user types; `agentQuery` is what useQueryGraph
  // actually consumes (only promoted when the user hits Run). This way
  // every keystroke doesn't refire the query.
  const [activeTab, setActiveTab] = useState<DashboardTab>(
    mode === "query" ? "agent" : "brain",
  );
  const [agentInput, setAgentInput] = useState<string>(query ?? "");
  const [agentQuery, setAgentQuery] = useState<string | undefined>(query);
  // Phase 3 — synthesis-mode toggle. "research" (default) gives the
  // concrete-claim synthesis; "ideation" produces [BUILD IDEA] blocks
  // (see backend/services/graph/orchestrator._IDEATION_SYSTEM_PROMPT).
  const [synthesisMode, setSynthesisMode] =
    useState<"research" | "ideation">("research");
  // Sprint #2 — opt-in critique + revise loop. When true, the backend
  // runs auditor + editor passes after the draft (2-3× LLM cost).
  // Off by default so the common case stays single-call.
  const [validateSynthesis, setValidateSynthesis] = useState<boolean>(false);
  const drillStackRef = useRef(drillStack);
  drillStackRef.current = drillStack;

  const drill = drillStack.length ? drillStack[drillStack.length - 1] : null;
  const brain = useBrainGraph(
    mode === "brain" ? corpusIds : [],
    drill,
  );
  // Pt 7: useQueryGraph now reads `agentQuery` state instead of the props.query.
  // For mode==="query" callers, props.query seeds agentQuery on mount. For
  // mode==="brain", the Agent Search tab inside the dashboard can drive
  // queries by promoting agentInput → agentQuery via onAgentRun.
  // Corpora pool is always available (regardless of mode) so users can
  // run an agent query from the brain canvas without switching modes.
  const q = useQueryGraph(
    corpusIds,
    agentQuery,
    model,
    synthesisMode,
    validateSynthesis,
  );

  const data = mode === "brain" ? brain.data : q.data;
  const loading = mode === "brain" ? brain.loading : q.phase === "loading";
  const error = mode === "brain" ? brain.error : q.error;

  // Double-click handler — drills into a book anchor (single-click selects
  // the node + neighbors as usual). Books-as-clusters is the only Brain
  // View now, so the only drill target is `book:<doc_id>`.
  const handleDoubleClickNode = useCallback(
    (nodeId: string) => {
      if (mode !== "brain") return;
      if (!nodeId.startsWith("book:")) return;
      const docId = nodeId.slice(5);
      const found = (data?.nodes || []).find((n: any) => String(n.id) === nodeId);
      const label =
        (found && (found.display_name as string)) || docId.slice(0, 8);
      setDrillStack([...drillStackRef.current, { docId, label }]);
    },
    [mode, data],
  );

  const sigma = useSigma({
    onNodeHover: (id) => {
      if (!id || !data) {
        setHoveredName(null);
        return;
      }
      const found = (data.nodes || []).find((n: any) => String(n.id) === id);
      setHoveredName(found ? String(found.display_name || id) : id);
    },
    onDoubleClickNode: handleDoubleClickNode,
    settleAfterDrag,
  });

  // Push new data into sigma when it lands.
  useEffect(() => {
    if (!data) return;
    const seedIds = mode === "query" ? q.seedIds : new Set<string>();
    const hubIds = mode === "query" ? q.hubIds : new Set<string>();
    const bridgeIds = mode === "query" ? q.bridgeIds : new Set<string>();
    const newGraph = polymathToGraphology(data.nodes, data.links, {
      colorMode,
      seedIds,
      hubIds,
      bridgeIds,
      minBridgeStrength,
      maxBridgesPerBook,
    });
    sigma.setGraph(newGraph);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-run on data change
  }, [data, mode, q.seedIds, q.hubIds, q.bridgeIds, minBridgeStrength, maxBridgesPerBook]);

  // Apply colorMode toggle without rebuilding graph.
  useEffect(() => {
    const sigmaInst = sigma.sigmaRef.current;
    const g = (sigmaInst as any)?.getGraph?.();
    if (!sigmaInst || !g || g.order === 0 || !data) return;
    // Easiest correct path: rebuild the graph with new colorMode. The
    // adapter is fast enough that this stays under a few ms even for
    // overview payloads (~80 nodes).
    const seedIds = mode === "query" ? q.seedIds : new Set<string>();
    const hubIds = mode === "query" ? q.hubIds : new Set<string>();
    const bridgeIds = mode === "query" ? q.bridgeIds : new Set<string>();
    const newGraph = polymathToGraphology(data.nodes, data.links, {
      colorMode,
      seedIds,
      hubIds,
      bridgeIds,
      minBridgeStrength,
      maxBridgesPerBook,
    });
    sigma.setGraph(newGraph);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- triggered only on colorMode flip
  }, [colorMode]);

  const selectedId = sigma.selectedNode;
  const selectedDisplay = useMemo(() => {
    if (!selectedId || !data) return null;
    const found = (data.nodes || []).find((n: any) => String(n.id) === selectedId);
    if (!found) return null;
    return {
      id: selectedId,
      display_name: String((found as any).display_name || selectedId),
      source_corpora: ((found as any).source_corpora || []) as string[],
    };
  }, [selectedId, data]);

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
    <div className="relative flex h-full w-full bg-[#06060a]">
      {/* Canvas column (fills remaining width) */}
      <div className="relative flex-1 min-w-0">
        {/* Background gradient — same recipe as GitNexus GraphCanvas */}
        <div className="pointer-events-none absolute inset-0">
          <div
            className="absolute inset-0"
            style={{
              background: `
                radial-gradient(circle at 50% 50%, rgba(124, 58, 237, 0.05) 0%, transparent 65%),
                linear-gradient(to bottom, #06060a, #0a0a14)
              `,
            }}
          />
        </div>

      {/* Hovered node tooltip — only when nothing is selected. Stays on
          canvas (cursor-following pill) rather than in the dashboard. */}
      {hoveredName && !selectedId && (
        <div className="pointer-events-none absolute top-3 left-1/2 z-20 -translate-x-1/2 rounded-lg border border-zinc-800 bg-[#0d0d14]/95 px-3 py-1.5 backdrop-blur">
          <span className="font-mono text-sm text-zinc-100">{hoveredName}</span>
        </div>
      )}

      {/* Loading / error overlay */}
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
        <div className="absolute inset-0 z-10 flex items-center justify-center">
          <EmptyDataState
            mode={mode}
            cacheWarming={mode === "brain" ? brain.cacheWarming : []}
            statuses={mode === "brain" ? brain.cacheStatuses : {}}
            rebuildingIds={mode === "brain" ? brain.rebuildingIds : new Set()}
            onRebuild={mode === "brain" ? brain.triggerRebuild : async () => {}}
          />
        </div>
      )}

      {/* Sigma canvas + optional prose pane */}
      <div className="absolute inset-0 flex">
        <div className="relative flex-1 min-w-0">
          {/* Pt 6: Galaxy background canvas — dust particles + family
              nebulae + Book-anchor glow halos, all painted in lockstep
              with sigma via its `afterRender` event. Sits BEHIND sigma's
              own canvas (z-0) with pointer-events:none so it never
              swallows clicks. */}
          <GalaxyBackground
            sigmaRef={sigma.sigmaRef as any}
            isLayoutRunning={sigma.isLayoutRunning}
          />
          <div
            ref={sigma.containerRef}
            className="absolute inset-0 z-10 cursor-grab active:cursor-grabbing"
          />
        </div>
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
                        <span className="text-zinc-300">{g.entity_a_name}</span>{" "}
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
      </div>{/* end absolute inset-0 flex */}

      {/* Bottom-right control cluster — same layout as GitNexus */}
      <div className="absolute right-4 bottom-4 z-20 flex flex-col gap-1 pointer-events-auto">
        <button
          onClick={sigma.zoomIn}
          className="flex h-9 w-9 items-center justify-center rounded-md border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          title="Zoom in"
        >
          <ZoomIn className="h-4 w-4" />
        </button>
        <button
          onClick={sigma.zoomOut}
          className="flex h-9 w-9 items-center justify-center rounded-md border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          title="Zoom out"
        >
          <ZoomOut className="h-4 w-4" />
        </button>
        <button
          onClick={sigma.resetZoom}
          className="flex h-9 w-9 items-center justify-center rounded-md border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          title="Fit to screen"
        >
          <Maximize2 className="h-4 w-4" />
        </button>
        <div className="h-px bg-zinc-800 my-1" />
        <button
          onClick={sigma.isLayoutRunning ? sigma.stopLayout : sigma.startLayout}
          className={`flex h-9 w-9 items-center justify-center rounded-md border transition-all ${
            sigma.isLayoutRunning
              ? "animate-pulse border-violet-500 bg-violet-500/30 text-white"
              : "border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          }`}
          title={sigma.isLayoutRunning ? "Pause layout" : "Resume layout"}
        >
          {sigma.isLayoutRunning ? (
            <Pause className="h-4 w-4" />
          ) : (
            <Play className="h-4 w-4" />
          )}
        </button>
      </div>

      {/* Layout running indicator */}
      {sigma.isLayoutRunning && (
        <div className="absolute bottom-4 left-1/2 z-10 flex -translate-x-1/2 items-center gap-2 rounded-full border border-emerald-500/30 bg-emerald-500/20 px-3 py-1.5 backdrop-blur">
          <div className="h-2 w-2 animate-ping rounded-full bg-emerald-400" />
          <span className="text-xs font-medium text-emerald-200 font-mono">
            layout optimizing…
          </span>
        </div>
      )}
      </div>{/* end canvas column */}

      {/* Right sidebar dashboard (Pt 5 — flex layout). Holds breadcrumb,
          stats, cache health, color mode, selection info, layout controls.
          Collapses to a 36px strip via the panel toggle. */}
      <BrainViewDashboard
        collapsed={dashboardCollapsed}
        onToggle={() => setDashboardCollapsed((v) => !v)}
        mode={mode}
        drillStack={drillStack}
        setDrillStack={setDrillStack}
        corpusIds={corpusIds}
        data={data as any}
        cacheWarming={mode === "brain" ? brain.cacheWarming : []}
        cacheStatuses={mode === "brain" ? brain.cacheStatuses : {}}
        rebuildingIds={mode === "brain" ? brain.rebuildingIds : new Set()}
        onRebuild={mode === "brain" ? brain.triggerRebuild : async () => {}}
        colorMode={colorMode}
        onColorModeToggle={() =>
          setColorMode((m) => (m === "community" ? "corpus" : "community"))
        }
        minBridgeStrength={minBridgeStrength}
        onMinBridgeStrengthChange={setMinBridgeStrength}
        maxBridgesPerBook={maxBridgesPerBook}
        onMaxBridgesPerBookChange={setMaxBridgesPerBook}
        settleAfterDrag={settleAfterDrag}
        onSettleAfterDragToggle={() => setSettleAfterDrag((v) => !v)}
        activeTab={activeTab}
        onActiveTabChange={setActiveTab}
        agentQuery={agentInput}
        onAgentQueryChange={setAgentInput}
        onAgentRun={() => setAgentQuery(agentInput)}
        synthesisMode={synthesisMode}
        onSynthesisModeChange={setSynthesisMode}
        validateSynthesis={validateSynthesis}
        onValidateSynthesisChange={setValidateSynthesis}
        agentPhase={q.phase}
        agentError={q.error}
        agentSynthesisMarkdown={q.synthesis?.markdown ?? null}
        agentSeedNames={(q.data?.nodes || [])
          .filter((n: any) => q.seedIds.has(String(n.id)))
          .map((n: any) => String(n.display_name || n.id))}
        agentBridgeNames={(q.data?.nodes || [])
          .filter((n: any) => q.bridgeIds.has(String(n.id)))
          .map((n: any) => String(n.display_name || n.id))}
        agentHubNames={(q.data?.nodes || [])
          .filter((n: any) => q.hubIds.has(String(n.id)))
          .map((n: any) => String(n.display_name || n.id))}
        agentGaps={q.gaps as any}
        onSendToChat={onSendToChat}
        model={model}
        onRerun={onRerun}
        onClose={onClose}
        selectedDisplay={selectedDisplay}
        onClearSelection={() => sigma.setSelectedNode(null)}
        isLayoutRunning={sigma.isLayoutRunning}
        startLayout={sigma.startLayout}
        stopLayout={sigma.stopLayout}
      />
    </div>
  );
}

export default GraphViewer;


// ─── Cache-warming sub-components ─────────────────────────────────────────
// Pt 5: CacheWarmingChip + CacheChipProps removed. BrainViewDashboard
// renders per-corpus cache health inline as a list with individual
// build buttons, which is more informative than the aggregate chip.

interface EmptyStateProps {
  mode: GraphViewerMode;
  cacheWarming: string[];
  statuses: Record<string, api.CacheStatus>;
  rebuildingIds: Set<string>;
  onRebuild: (ids: string[]) => Promise<void>;
}

function EmptyDataState({
  mode,
  cacheWarming,
  statuses,
  rebuildingIds,
  onRebuild,
}: EmptyStateProps) {
  if (mode !== "brain") {
    return (
      <div className="text-center text-zinc-500 text-xs font-mono pointer-events-none">
        <div>no query result yet</div>
        <div className="text-zinc-700 mt-1">type a question below</div>
      </div>
    );
  }
  if (cacheWarming.length === 0) {
    return (
      <div className="text-center text-zinc-500 text-xs font-mono pointer-events-none">
        <div>no graph data</div>
        <div className="text-zinc-700 mt-1">
          the selected corpora have empty supernode overviews
        </div>
      </div>
    );
  }
  const missingIds = cacheWarming.filter((cid) => {
    const s = statuses[cid];
    return s && (s.metrics_cache === "missing" || s.domain_cache === "missing");
  });
  const warmingIds = cacheWarming.filter((cid) => {
    const s = statuses[cid];
    return s && s.metrics_cache !== "missing" && s.domain_cache !== "missing";
  });
  const rebuildingHere = cacheWarming.filter((cid) => rebuildingIds.has(cid));
  const buildable = cacheWarming.filter((cid) => !rebuildingIds.has(cid));
  return (
    <div className="text-center text-zinc-300 text-xs font-mono max-w-xl px-6 space-y-2 pointer-events-auto">
      <div className="text-zinc-200 text-sm mb-3">
        Analytics cache not ready for{" "}
        <span className="text-amber-300">{cacheWarming.length}</span> corpor
        {cacheWarming.length === 1 ? "us" : "a"}
      </div>
      {missingIds.length > 0 && (
        <div className="text-zinc-400">
          <span className="text-rose-300">{missingIds.length} never built</span>{" "}
          — Polymath warms the analytics cache automatically at the end of
          ingestion, but these corpora were ingested before that hook landed
          (or never finished). Rebuild manually below.
        </div>
      )}
      {warmingIds.length > 0 && (
        <div className="text-zinc-400">
          <span className="text-amber-300">{warmingIds.length} stale</span> —
          new docs were ingested since the last cache build. Rebuild to refresh.
        </div>
      )}
      {rebuildingHere.length > 0 && (
        <div className="text-violet-300">
          {rebuildingHere.length} currently rebuilding (this can take seconds
          for tiny corpora to several minutes for thousands of entities).
          Frontend polls every 15s; the canvas will populate when each
          finishes.
        </div>
      )}
      {buildable.length > 0 && (
        <button
          className="mt-3 text-[11px] uppercase tracking-widest text-amber-100 hover:text-amber-50 border border-amber-500/50 bg-amber-500/10 rounded px-3 py-1.5 font-mono"
          onClick={() => onRebuild(buildable)}
        >
          Build cache for {buildable.length} corpor{buildable.length === 1 ? "us" : "a"}
        </button>
      )}
      <div className="text-zinc-600 text-[10px] mt-2">
        Behind the scenes: services/graph/analytics.py:emerge_domains runs
        Louvain on document embeddings, computes PageRank + concept
        communities, then writes the result to graph_domain_cache and
        graph_metrics_cache.
      </div>
    </div>
  );
}
