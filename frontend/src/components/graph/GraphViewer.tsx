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
import { useSigma } from "../../hooks/useSigma";
import {
  polymathToGraphology,
  type ColorMode,
} from "../../lib/polymath-graph-adapter";

// ─── Types ────────────────────────────────────────────────────────────────

export type GraphViewerMode = "brain" | "query";
type BrainViewMode = "domains" | "books";

interface GraphViewerProps {
  mode: GraphViewerMode;
  corpusIds: string[];
  query?: string;
  onRerun?: () => void;
  onClose?: () => void;
  model?: string;
}

type DrillFrame = {
  source: BrainViewMode;
  clusterId: string;
  label: string;
};

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

  const reload = useCallback(async () => {
    if (corpusIds.length === 0) {
      setData(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      if (brainMode === "books") {
        const res = await api.getGraphByDocument({
          corpusIds,
          mode: drill && drill.source === "books" ? "drill" : "full",
          drillDocId:
            drill && drill.source === "books" ? drill.clusterId : undefined,
        });
        const anchorNodes = (res.clusters || []).map((c) => ({
          id: `book:${c.cluster_id}`,
          display_name: c.label || c.cluster_id.slice(0, 8),
          mention_count: c.entity_count || 1,
          kind: "book" as const,
          source_corpora: [c.corpus_id],
          source_corpus: c.corpus_id,
          top_entities: c.top_entities,
        }));
        // Membership + bridge edges so anchors physically pull entities.
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
            });
          }
        }
        setData({
          nodes: [...anchorNodes, ...(res.nodes || [])],
          links: [...memberEdges, ...(res.edges || [])],
        });
        setCacheWarming([]);
        return;
      }

      // Domains view — concept-community supernodes.
      if (drill && drill.source === "domains") {
        const res = await api.getGraphCluster(drill.clusterId, corpusIds);
        setData({ nodes: res.nodes || [], links: res.edges || [] });
        setCacheWarming(res._meta?.cache_warming_corpora || []);
      } else {
        const res = await api.getGraphOverviewMulti(corpusIds);
        setData({ nodes: res.nodes || [], links: res.edges || [] });
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

  return { data, loading, error, cacheWarming, reload };
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

// ─── Component ────────────────────────────────────────────────────────────

export function GraphViewer({
  mode,
  corpusIds,
  query,
  onRerun,
  onClose,
  model,
}: GraphViewerProps) {
  const [colorMode, setColorMode] = useState<ColorMode>("community");
  const [brainMode, setBrainMode] = useState<BrainViewMode>("domains");
  const [drillStack, setDrillStack] = useState<DrillFrame[]>([]);
  const [hoveredName, setHoveredName] = useState<string | null>(null);
  const drillStackRef = useRef(drillStack);
  drillStackRef.current = drillStack;

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

  // Double-click handler — drills into a cluster (concept supernode in
  // domains mode) or a book anchor (books mode).
  const handleDoubleClickNode = useCallback(
    (nodeId: string) => {
      if (mode !== "brain") return;
      const ds = drillStackRef.current;
      // We don't have direct access to the graph node here, but the id
      // encodes the kind: concept supernodes start with "concept:" and
      // book anchors start with "book:".
      if (nodeId.startsWith("concept:")) {
        const conceptId = nodeId.split(":").slice(1).join(":");
        if (!conceptId) return;
        // Look up the label from the current data so the breadcrumb
        // doesn't show "concept:abc123".
        const found = (data?.nodes || []).find((n: any) => String(n.id) === nodeId);
        const label =
          (found && (found.display_name as string)) || conceptId.slice(0, 8);
        setDrillStack([
          ...ds,
          { source: "domains", clusterId: conceptId, label },
        ]);
      } else if (nodeId.startsWith("book:")) {
        const docId = nodeId.slice(5);
        const found = (data?.nodes || []).find((n: any) => String(n.id) === nodeId);
        const label =
          (found && (found.display_name as string)) || docId.slice(0, 8);
        setDrillStack([
          ...ds,
          { source: "books", clusterId: docId, label },
        ]);
      }
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
    });
    sigma.setGraph(newGraph);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-run on data change
  }, [data, mode, q.seedIds, q.hubIds, q.bridgeIds]);

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
    <div className="relative h-full w-full bg-[#06060a]">
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
            {sigma.isLayoutRunning && (
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

      {/* Hovered node tooltip — only when nothing is selected */}
      {hoveredName && !selectedId && (
        <div className="pointer-events-none absolute top-16 left-1/2 z-20 -translate-x-1/2 rounded-lg border border-zinc-800 bg-[#0d0d14]/95 px-3 py-1.5 backdrop-blur">
          <span className="font-mono text-sm text-zinc-100">{hoveredName}</span>
        </div>
      )}

      {/* Selection info bar */}
      {selectedDisplay && (
        <div className="absolute top-16 left-1/2 z-20 flex -translate-x-1/2 items-center gap-2 rounded-xl border border-violet-500/30 bg-violet-500/10 px-4 py-2 backdrop-blur">
          <div className="h-2 w-2 animate-pulse rounded-full bg-violet-300" />
          <span className="font-mono text-sm text-zinc-100">
            {selectedDisplay.display_name}
          </span>
          {selectedDisplay.source_corpora.length > 0 && (
            <span className="text-[10px] text-zinc-400 font-mono">
              · {selectedDisplay.source_corpora.length} corpora
            </span>
          )}
          <button
            onClick={() => sigma.setSelectedNode(null)}
            className="ml-2 rounded px-2 py-0.5 text-xs text-zinc-300 transition-colors hover:bg-white/10 hover:text-zinc-50"
          >
            Clear
          </button>
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

      {/* Sigma canvas + optional prose pane */}
      <div className="absolute inset-0 flex">
        <div
          ref={sigma.containerRef}
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
      </div>

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
    </div>
  );
}

export default GraphViewer;
