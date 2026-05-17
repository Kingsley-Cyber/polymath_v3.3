/**
 * BrainViewDashboard — right-side flex panel that holds all the brain-view
 * chrome previously floated as absolute overlays on the canvas:
 *   • Mode header (Corpora View / Query View)
 *   • Breadcrumb / drill stack
 *   • Corpus + node/edge stats
 *   • Cache warming chip
 *   • Color-mode toggle (community vs corpus)
 *   • Re-run + close (query mode)
 *   • Selection info bar (book + entity details when selected)
 *   • Layout running indicator
 *
 * Lives at flex-shrink:0 next to the sigma canvas. Collapses to a 36px
 * vertical strip via the toggle button so the canvas can reclaim the
 * width. Pt 5 of the Brain View refactor.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  Loader2,
  PanelRightClose,
  PanelRightOpen,
  Send,
  Sparkles,
  X,
  Zap,
} from "lucide-react";
import ReactMarkdown from "react-markdown";

import type {
  CacheStatus,
  ExtractedEntity,
  RefinementResult,
} from "../../lib/api";
import * as api from "../../lib/api";
import { cleanBookLabel } from "../../lib/label-utils";
import { EDGE_COLORS_BY_FAMILY } from "../../lib/sigma-constants";
import type { DocExtractionItem } from "../../types";
import type { GraphSynthesisMode } from "../../types/discover";

export type DashboardTab = "brain" | "agent" | "graph-query";

// Sidebar width bounds when expanded. Default mirrors Pt 5 baseline (320).
const SIDEBAR_MIN_W = 240;
const SIDEBAR_MAX_W = 900;
const SIDEBAR_DEFAULT_W = 320;

export type DrillFrame = { docId: string; label: string };

interface SelectedDisplay {
  id: string;
  display_name: string;
  source_corpora: string[];
  source_corpus?: string;
  kind?: string;
  nodeKind?: string;
  filename?: string | null;
  label?: string;
  bridge_count?: number;
  chunk_count?: number;
  parent_count?: number;
  entity_count?: number;
  top_entities?: string[];
  dominant_family?: string | null;
  dominant_entity_type?: string | null;
  ghost_b_success_rate?: number | null;
  ghost_b_extracted?: number | null;
  ghost_b_total?: number | null;
  primary_doc_id?: string;
  entity_type?: string;
  accent_color?: string;
}

type RenderedGraphEdge = {
  id: string;
  source: string;
  target: string;
  sourceLabel: string;
  targetLabel: string;
  predicate?: string;
  relationFamily?: string | null;
  dominantRelationFamily?: string | null;
  confidence?: number;
  weight?: number;
  sharedEntities?: number;
  topSharedEntities?: string[];
  sourceDocId?: string;
  targetDocId?: string;
  sourceCorpusId?: string;
  targetCorpusId?: string;
  color?: string;
};

type RenderedGraphStats = {
  visibleNodes: number;
  totalNodes: number;
  visibleEdges: number;
  totalEdges: number;
  lodMode?: "satellites" | "books" | "clusters";
}

export interface BrainViewDashboardProps {
  collapsed: boolean;
  onToggle: () => void;

  // Mode + breadcrumb
  mode: "brain" | "query";
  drillStack: DrillFrame[];
  setDrillStack: (frames: DrillFrame[]) => void;

  // Stats
  corpusIds: string[];
  data: { nodes: any[]; links: any[] } | null;

  // Cache warming (brain mode only)
  cacheWarming: string[];
  cacheStatuses: Record<string, CacheStatus>;
  rebuildingIds: Set<string>;
  onRebuild: (ids: string[]) => Promise<void> | void;

  // Pt 7: tab + Agent / Graph Query content
  activeTab: DashboardTab;
  onActiveTabChange: (t: DashboardTab) => void;
  // Agent Search tab — wired to existing useQueryGraph state
  agentQuery: string;
  onAgentQueryChange: (q: string) => void;
  onAgentRun: () => void;
  agentPhase: "idle" | "loading" | "ready" | "error";
  agentError?: string | null;
  agentSynthesisMarkdown?: string | null;
  agentSeedNames?: string[];
  agentBridgeNames?: string[];
  agentHubNames?: string[];
  agentGaps?: Array<{ entity_a_name?: string; entity_b_name?: string }>;
  // Phase 3 — synthesis-mode selector ("research" | "ideation" | "nuance").
  // Forwarded into the Agent Search tab so the user can flip between
  // concrete-claim research, [BUILD IDEA] ideation, and conceptual nuance
  // without leaving the panel. Default "research" preserves existing UX.
  synthesisMode?: GraphSynthesisMode;
  onSynthesisModeChange?: (m: GraphSynthesisMode) => void;
  // Sprint #2 — opt-in synthesis validation (draft → critique → revise).
  // Adds 2-3× latency/tokens; surfaced as a small checkbox in the tab.
  validateSynthesis?: boolean;
  onValidateSynthesisChange?: (v: boolean) => void;
  agentSources?: Array<{
    source_label?: string;
    doc_id?: string;
    chunk_id?: string;
    snippet?: string;
  }>;
  // Pt 7: Graph Query tab — send a refined chip back to the chat
  onSendToChat?: (text: string) => void;
  onOpenAtomic?: (text: string, mode?: GraphSynthesisMode) => void;
  /** Pt 7: model id passed through to api.refineQuery so the LLM call
   *  uses the user's currently-selected chat model. Required by LiteLLM
   *  (rejects empty model in the body). */
  model?: string;

  // Query mode actions
  onRerun?: () => void;

  // Close viewer
  onClose?: () => void;

  // Selection info
  selectedDisplay: SelectedDisplay | null;
  selectedEdge?: RenderedGraphEdge | null;
  renderedEdges?: RenderedGraphEdge[];
  renderedStats?: RenderedGraphStats;
  onClearSelection: () => void;

  // Layout state
  isLayoutRunning: boolean;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono mb-1.5">
      {children}
    </div>
  );
}

function statusDot(s: string | undefined): string {
  if (s === "ready") return "bg-emerald-400";
  if (s === "warming") return "bg-amber-400 animate-pulse";
  return "bg-rose-500";
}

export function BrainViewDashboard(props: BrainViewDashboardProps) {
  const {
    collapsed,
    onToggle,
    mode,
    drillStack,
    setDrillStack,
    corpusIds,
    data,
    cacheWarming,
    cacheStatuses,
    rebuildingIds,
    onRebuild,
    activeTab,
    onActiveTabChange,
    agentQuery,
    onAgentQueryChange,
    onAgentRun,
    agentPhase,
    agentError,
    agentSynthesisMarkdown,
    agentSeedNames,
    agentBridgeNames,
    agentHubNames,
    agentGaps,
    synthesisMode,
    onSynthesisModeChange,
    validateSynthesis,
    onValidateSynthesisChange,
    agentSources,
    onSendToChat,
    onOpenAtomic,
    model,
    onRerun,
    onClose,
    selectedDisplay,
    selectedEdge,
    renderedEdges,
    renderedStats,
    onClearSelection,
    isLayoutRunning,
  } = props;

  // Drag-resize state. Persists across renders within the component
  // instance; resets to default when the dashboard remounts.
  const [width, setWidth] = useState<number>(SIDEBAR_DEFAULT_W);
  const draggingRef = useRef<{ startX: number; startW: number } | null>(null);

  const onResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    draggingRef.current = { startX: e.clientX, startW: width };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, [width]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const drag = draggingRef.current;
      if (!drag) return;
      // Dragging LEFT (toward canvas) widens; dragging RIGHT narrows —
      // because the sidebar is on the right, the handle sits at the
      // sidebar's left edge.
      const delta = drag.startX - e.clientX;
      const next = Math.max(
        SIDEBAR_MIN_W,
        Math.min(SIDEBAR_MAX_W, drag.startW + delta),
      );
      setWidth(next);
    };
    const onUp = () => {
      if (!draggingRef.current) return;
      draggingRef.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  if (collapsed) {
    return (
      <aside className="z-30 flex h-full w-9 flex-col items-center border-l border-zinc-900 bg-[#0a0a0e]/95 backdrop-blur">
        <button
          onClick={onToggle}
          className="mt-3 flex h-9 w-9 items-center justify-center text-zinc-500 hover:text-zinc-200"
          title="Expand dashboard"
        >
          <PanelRightOpen className="h-4 w-4" />
        </button>
        {/* Vertical layout indicator when collapsed */}
        {isLayoutRunning && (
          <div className="mt-3 h-2 w-2 animate-ping rounded-full bg-emerald-400" />
        )}
      </aside>
    );
  }

  return (
    <aside
      className="relative z-30 flex h-full shrink-0 flex-col border-l border-zinc-900 bg-[#0a0a0e]/95 backdrop-blur"
      style={{ width: `${width}px` }}
    >
      {/* Drag handle — 6px hot-zone on the sidebar's left edge. Hover
          shows the col-resize cursor; drag adjusts width within
          [SIDEBAR_MIN_W, SIDEBAR_MAX_W]. */}
      <div
        onMouseDown={onResizeStart}
        className="group absolute -left-1 top-0 z-40 h-full w-1.5 cursor-col-resize"
        title="Drag to resize"
      >
        <div className="h-full w-px bg-zinc-900 transition-colors group-hover:bg-amber-500/40 group-active:bg-amber-500/70" />
      </div>
      {/* Header strip — close-viewer button lives at the TOP-LEFT now
          (previously at the bottom of the panel as a footer). Moving it
          here surfaces the escape action as soon as you enter the graph
          screen with nothing selected, so first-time users always have
          an obvious way back. */}
      <div className="flex items-start justify-between border-b border-zinc-900 px-3 py-2.5 gap-2">
        <div className="flex items-start gap-2 min-w-0">
          {onClose && (
            <button
              onClick={onClose}
              className="shrink-0 mt-0.5 flex h-6 w-6 items-center justify-center rounded border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:border-rose-700 hover:text-rose-300"
              title="Close viewer"
            >
              <X className="h-3 w-3" />
            </button>
          )}
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono">
              {mode === "brain" ? "Corpora View" : "Query View"}
            </div>
            <div className="text-xs text-zinc-300 font-mono mt-0.5 truncate">
              {corpusIds.length} corpora
              {data && renderedStats && (
                <span className="text-zinc-500">
                  {" "}· {renderedStats.visibleNodes}/{renderedStats.totalNodes}n ·{" "}
                  {renderedStats.visibleEdges}/{renderedStats.totalEdges}e
                </span>
              )}
              {renderedStats?.lodMode && (
                <span className="ml-1 text-zinc-600">
                  · {renderedStats.lodMode}
                </span>
              )}
            </div>
          </div>
        </div>
        <button
          onClick={onToggle}
          className="shrink-0 text-zinc-500 hover:text-zinc-200"
          title="Collapse dashboard"
        >
          <PanelRightClose className="h-4 w-4" />
        </button>
      </div>

      {/* Pt 7: Tab strip — three tabs. State is fully owned by the parent
          so each tab can independently retain its own content (query input,
          entity-type filter, refinement chips) when the user flips back. */}
      <div className="flex items-stretch border-b border-zinc-900 px-1.5 pt-1.5 pb-0 gap-0.5">
        {([
          { id: "brain", label: "Brain" },
          { id: "agent", label: "Agent Search" },
          { id: "graph-query", label: "Graph Query" },
        ] as const).map((t) => (
          <button
            key={t.id}
            onClick={() => onActiveTabChange(t.id)}
            className={`flex-1 px-2 py-1.5 font-mono text-[10px] uppercase tracking-widest border-b-2 transition-colors ${
              activeTab === t.id
                ? "border-amber-500/60 bg-amber-500/5 text-amber-300"
                : "border-transparent text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-4">
        {activeTab === "brain" && (selectedDisplay || selectedEdge) && (
          <EvidenceInspector
            selectedNode={selectedDisplay}
            selectedEdge={selectedEdge || null}
            renderedEdges={renderedEdges || []}
            sources={agentSources || []}
            onClear={onClearSelection}
            onSendToChat={onSendToChat}
            onOpenAtomic={onOpenAtomic}
          />
        )}

        {/* Pt 7: Agent Search tab content — input + run + synthesis chips. */}
        {activeTab === "agent" && (
          <AgentSearchTab
            query={agentQuery}
            onChange={onAgentQueryChange}
            onRun={onAgentRun}
            phase={agentPhase}
            error={agentError}
            synthesisMarkdown={agentSynthesisMarkdown}
            seedNames={agentSeedNames}
            bridgeNames={agentBridgeNames}
            hubNames={agentHubNames}
            gaps={agentGaps}
            synthesisMode={synthesisMode}
            onSynthesisModeChange={onSynthesisModeChange}
            validateSynthesis={validateSynthesis}
            onValidateSynthesisChange={onValidateSynthesisChange}
            sources={agentSources || []}
            onOpenAtomic={onOpenAtomic}
          />
        )}

        {/* Pt 7: Graph Query tab content — entity-type search + HyDE refinement. */}
        {activeTab === "graph-query" && (
          <GraphQueryTab
            corpusIds={corpusIds}
            onSendToChat={onSendToChat}
            onOpenAtomic={onOpenAtomic}
            model={model}
          />
        )}

        {/* Brain tab — original sections (only render when this tab is active). */}
        {activeTab === "brain" && mode === "brain" && drillStack.length > 0 && (
          <section>
            <SectionLabel>Drill Stack</SectionLabel>
            <div className="space-y-1 font-mono text-xs text-zinc-300">
              <button
                className="block w-full text-left hover:text-amber-400 transition-colors"
                onClick={() => setDrillStack([])}
              >
                ← Overview
              </button>
              {drillStack.map((f, i) => (
                <button
                  key={i}
                  className="flex w-full items-center gap-1 truncate text-left hover:text-amber-400 transition-colors"
                  onClick={() => setDrillStack(drillStack.slice(0, i + 1))}
                  title={f.label}
                >
                  <ChevronLeft className="h-3 w-3 rotate-180 text-zinc-600 shrink-0" />
                  <span className="truncate">{f.label}</span>
                </button>
              ))}
              <button
                className="mt-1 text-[11px] text-zinc-500 hover:text-zinc-300"
                onClick={() => setDrillStack(drillStack.slice(0, -1))}
              >
                ↩ pop one level
              </button>
            </div>
          </section>
        )}

        {/* Cache health — brain mode + brain tab only */}
        {activeTab === "brain" && mode === "brain" && cacheWarming.length > 0 && (
          <section>
            <SectionLabel>Cache Health</SectionLabel>
            <ul className="space-y-1.5 font-mono text-[11px]">
              {cacheWarming.map((cid) => {
                const s = cacheStatuses[cid];
                const rebuilding = rebuildingIds.has(cid);
                return (
                  <li
                    key={cid}
                    className="flex items-center gap-2 rounded border border-zinc-800 bg-[#0d0d14] px-2 py-1.5"
                  >
                    <span
                      className={`h-2 w-2 shrink-0 rounded-full ${statusDot(
                        s?.metrics_cache,
                      )}`}
                    />
                    <span className="flex-1 truncate text-zinc-400" title={cid}>
                      {cid.slice(0, 8)}…
                    </span>
                    <button
                      disabled={rebuilding}
                      onClick={() => onRebuild([cid])}
                      className="rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] uppercase text-zinc-300 hover:border-amber-700 hover:text-amber-300 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {rebuilding ? "…" : "build"}
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        )}

        {activeTab === "brain" && mode === "brain" && (
          <section>
            <SectionLabel>Relation Legend</SectionLabel>
            <div className="grid grid-cols-2 gap-1.5 font-mono text-[10px]">
              {Object.entries(EDGE_COLORS_BY_FAMILY).map(([family, color]) => (
                <div
                  key={family}
                  className="flex min-w-0 items-center gap-1.5 text-zinc-400"
                  title={family}
                >
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ backgroundColor: color }}
                  />
                  <span className="truncate">{family}</span>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* Query mode actions — only on the legacy Query mode (different
            from the new Agent Search tab which has its own re-run button). */}
        {activeTab === "brain" && mode === "query" && onRerun && (
          <section>
            <SectionLabel>Actions</SectionLabel>
            <button
              onClick={onRerun}
              className="flex w-full items-center gap-2 rounded border border-zinc-800 bg-[#0d0d14] px-2 py-1.5 font-mono text-[11px] uppercase tracking-widest text-zinc-300 hover:border-amber-700 hover:text-amber-300"
            >
              <Zap className="h-3 w-3" /> re-run synthesis
            </button>
          </section>
        )}
      </div>

      {/* Footer close button removed — relocated to the top-left of the
          header strip (above). One escape action, surfaced where the
          user lands first when entering the graph view. */}
    </aside>
  );
}


// ────────────────────────────────────────────────────────────────────────
// Evidence Inspector
// ────────────────────────────────────────────────────────────────────────

function compactName(value: string | undefined | null): string {
  const clean = cleanBookLabel(String(value || "")) || String(value || "");
  return clean.length > 52 ? clean.slice(0, 51) + "…" : clean;
}

function numberText(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString()
    : "0";
}

function EvidenceInspector({
  selectedNode,
  selectedEdge,
  renderedEdges,
  sources,
  onClear,
  onSendToChat,
  onOpenAtomic,
}: {
  selectedNode: SelectedDisplay | null;
  selectedEdge: RenderedGraphEdge | null;
  renderedEdges: RenderedGraphEdge[];
  sources: Array<{
    source_label?: string;
    doc_id?: string;
    chunk_id?: string;
    snippet?: string;
  }>;
  onClear: () => void;
  onSendToChat?: (text: string) => void;
  onOpenAtomic?: (text: string, mode?: GraphSynthesisMode) => void;
}) {
  const [chunksOpen, setChunksOpen] = useState(false);
  const [chunksLoading, setChunksLoading] = useState(false);
  const [chunkRows, setChunkRows] = useState<
    Array<DocExtractionItem & { docLabel?: string }>
  >([]);
  const [chunkError, setChunkError] = useState<string | null>(null);

  const nodeId = selectedNode?.id || "";
  const docId = nodeId.startsWith("book:") ? nodeId.slice(5) : undefined;
  const corpusId =
    selectedNode?.source_corpus || selectedNode?.source_corpora?.[0] || "";
  const selectionKey = selectedEdge
    ? `edge:${selectedEdge.id}`
    : selectedNode
      ? `node:${selectedNode.id}`
      : "none";
  const supportDocs = useMemo(() => {
    const rows: Array<{ docId: string; corpusId: string; label: string }> = [];
    if (selectedEdge) {
      if (selectedEdge.sourceDocId && selectedEdge.sourceCorpusId) {
        rows.push({
          docId: selectedEdge.sourceDocId,
          corpusId: selectedEdge.sourceCorpusId,
          label: selectedEdge.sourceLabel,
        });
      }
      if (
        selectedEdge.targetDocId &&
        selectedEdge.targetCorpusId &&
        selectedEdge.targetDocId !== selectedEdge.sourceDocId
      ) {
        rows.push({
          docId: selectedEdge.targetDocId,
          corpusId: selectedEdge.targetCorpusId,
          label: selectedEdge.targetLabel,
        });
      }
    } else if (docId && corpusId) {
      rows.push({
        docId,
        corpusId,
        label: selectedNode?.filename || selectedNode?.display_name || docId,
      });
    }
    return rows;
  }, [
    corpusId,
    docId,
    selectedEdge,
    selectedNode?.display_name,
    selectedNode?.filename,
  ]);
  const supportDocIds = useMemo(
    () => new Set(supportDocs.map((doc) => doc.docId)),
    [supportDocs],
  );

  useEffect(() => {
    setChunksOpen(false);
    setChunksLoading(false);
    setChunkRows([]);
    setChunkError(null);
  }, [selectionKey]);

  const incidentEdges = selectedNode
    ? renderedEdges.filter(
        (edge) => edge.source === selectedNode.id || edge.target === selectedNode.id,
      )
    : [];
  const relationFamilies = Array.from(
    new Set(
      (selectedEdge ? [selectedEdge] : incidentEdges)
        .map((edge) => edge.dominantRelationFamily || edge.relationFamily)
        .filter(Boolean) as string[],
    ),
  );
  const topShared = Array.from(
    new Set([
      ...(selectedEdge?.topSharedEntities || []),
      ...((selectedNode?.top_entities || []) as string[]),
      ...incidentEdges.flatMap((edge) => edge.topSharedEntities || []),
    ]),
  ).slice(0, 8);
  const matchedSources = sources.filter((source) => {
    if (supportDocIds.size === 0 || !source.doc_id) return false;
    return supportDocIds.has(source.doc_id);
  });

  const title = selectedEdge
    ? `${compactName(selectedEdge.sourceLabel)} -> ${compactName(
        selectedEdge.targetLabel,
      )}`
    : compactName(selectedNode?.display_name || selectedNode?.id);
  const why = selectedEdge
    ? `This bridge connects two document anchors through ${numberText(
        selectedEdge.sharedEntities,
      )} shared entities and ${numberText(
        selectedEdge.weight,
      )} relation-path hits. Its dominant family is ${
        selectedEdge.dominantRelationFamily || selectedEdge.relationFamily || "unknown"
      }.`
    : selectedNode?.kind === "book" || selectedNode?.nodeKind === "Book"
      ? `This document anchor contributes ${numberText(
          selectedNode.chunk_count,
        )} chunks, ${numberText(
          selectedNode.entity_count,
        )} extracted entities, and ${numberText(
          selectedNode.bridge_count,
        )} visible bridge opportunities to the corpus map.`
      : `This node is connected to ${numberText(
          incidentEdges.length,
        )} visible relation edges in the rendered graph. Its current role is visual context, not a new retrieval/ranking signal.`;

  const loadChunks = useCallback(async () => {
    setChunksOpen((v) => !v);
    if (supportDocs.length === 0 || chunkRows.length > 0 || chunksLoading) return;
    setChunksLoading(true);
    setChunkError(null);
    try {
      const rows = await Promise.all(
        supportDocs.map(async (doc) => {
          const docRows = await api.getDocExtraction(doc.corpusId, doc.docId);
          return docRows.map((row) => ({ ...row, docLabel: doc.label }));
        }),
      );
      setChunkRows(rows.flat());
    } catch (e) {
      setChunkError(e instanceof Error ? e.message : String(e));
    } finally {
      setChunksLoading(false);
    }
  }, [chunkRows.length, chunksLoading, supportDocs]);

  const sendText = selectedEdge
    ? `Explain the bridge between ${selectedEdge.sourceLabel} and ${selectedEdge.targetLabel}.`
    : selectedNode?.display_name || "";
  const nuanceText = selectedEdge
    ? `Where is the nuance in the bridge between ${selectedEdge.sourceLabel} and ${selectedEdge.targetLabel}?`
    : `Where is the nuance around ${selectedNode?.display_name || "this graph node"}?`;

  return (
    <section>
      <SectionLabel>Evidence Inspector</SectionLabel>
      <div className="rounded border border-zinc-800 bg-[#0d0d14] p-2.5">
        <div className="flex items-start gap-2">
          <span
            className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full"
            style={{
              backgroundColor:
                selectedEdge?.color || selectedNode?.accent_color || "#a78bfa",
            }}
          />
          <div className="min-w-0 flex-1">
            <div className="break-words font-mono text-xs text-zinc-100">
              {title || "Selection"}
            </div>
            <div className="mt-1 text-[10px] uppercase tracking-widest text-zinc-500 font-mono">
              {selectedEdge ? "bridge edge" : selectedNode?.entity_type || "node"}
            </div>
          </div>
          <button
            onClick={onClear}
            className="shrink-0 rounded px-1.5 py-0.5 text-[10px] text-zinc-500 hover:bg-white/10 hover:text-zinc-200"
          >
            Clear
          </button>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-2 font-mono text-[10px] text-zinc-400">
          <Metric label="Relation" value={relationFamilies[0] || "mixed"} />
          <Metric
            label="Strength"
            value={numberText(selectedEdge?.weight ?? selectedNode?.bridge_count)}
          />
          <Metric
            label="Sources"
            value={
              selectedEdge
                ? supportDocs.length > 0
                  ? `${supportDocs.length} docs`
                  : "aggregate"
                : `${selectedNode?.source_corpora?.length || 0} corpora`
            }
          />
          <Metric
            label="Ghost B"
            value={
              selectedNode?.ghost_b_total
                ? `${selectedNode.ghost_b_extracted || 0}/${selectedNode.ghost_b_total}`
                : "not loaded"
            }
          />
        </div>

        <InspectorBlock title="Why This Matters">
          <p>{why}</p>
        </InspectorBlock>

        <InspectorBlock title="Top Shared Entities">
          {topShared.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {topShared.map((name) => (
                <span
                  key={name}
                  className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300"
                  title={name}
                >
                  {name.length > 24 ? name.slice(0, 23) + "…" : name}
                </span>
              ))}
            </div>
          ) : (
            <p>No shared-entity labels in the rendered payload.</p>
          )}
        </InspectorBlock>

        <InspectorBlock
          title={
            selectedEdge && supportDocs.length === 0
              ? "Endpoints"
              : "Source Documents"
          }
        >
          <ul className="space-y-1 font-mono text-[10px] text-zinc-400">
            {selectedEdge ? (
              <>
                {(supportDocs.length
                  ? supportDocs
                  : [
                      {
                        docId: selectedEdge.sourceDocId || selectedEdge.source,
                        label: selectedEdge.sourceLabel,
                      },
                      {
                        docId: selectedEdge.targetDocId || selectedEdge.target,
                        label: selectedEdge.targetLabel,
                      },
                    ]
                ).map((doc) => (
                  <li key={doc.docId} className="truncate" title={doc.label}>
                    {doc.label}
                  </li>
                ))}
              </>
            ) : (
              <li className="truncate" title={selectedNode?.filename || selectedNode?.display_name}>
                {selectedNode?.filename || selectedNode?.display_name || "Unknown document"}
              </li>
            )}
          </ul>
        </InspectorBlock>

        <InspectorBlock title="Evidence Phrases / Facts">
          <p>
            Ghost B evidence phrases and extracted facts are not part of this
            rendered graph payload yet, so the inspector shows relation
            families, bridge entities, source docs, and chunk ids without
            changing retrieval or synthesis behavior.
          </p>
        </InspectorBlock>

        {matchedSources.length > 0 && (
          <InspectorBlock title={`Query Sources (${matchedSources.length})`}>
            <ul className="space-y-1.5">
              {matchedSources.slice(0, 5).map((source, index) => (
                <li
                  key={source.chunk_id || index}
                  className="rounded border border-zinc-900 bg-black/20 px-2 py-1"
                >
                  <div className="truncate font-mono text-[10px] text-zinc-300">
                    {source.source_label || source.doc_id || source.chunk_id}
                  </div>
                  {source.snippet && (
                    <div className="mt-0.5 line-clamp-2 text-[10px] leading-relaxed text-zinc-500">
                      {source.snippet}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </InspectorBlock>
        )}

        <div className="mt-3 grid grid-cols-1 gap-1.5">
          <button
            onClick={() => sendText && onSendToChat?.(sendText)}
            disabled={!onSendToChat || !sendText}
            className="flex items-center justify-center gap-2 rounded border border-zinc-700 px-2 py-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-300 hover:border-amber-700 hover:text-amber-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Send className="h-3 w-3" /> Send to Chat
          </button>
          <button
            onClick={() => onOpenAtomic?.(nuanceText, "nuance")}
            disabled={!onOpenAtomic}
            className="flex items-center justify-center gap-2 rounded border border-zinc-700 px-2 py-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-300 hover:border-amber-700 hover:text-amber-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Zap className="h-3 w-3" /> Run Nuance
          </button>
          <button
            onClick={loadChunks}
            disabled={supportDocs.length === 0}
            className="flex items-center justify-center gap-2 rounded border border-zinc-700 px-2 py-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-300 hover:border-amber-700 hover:text-amber-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Sparkles className="h-3 w-3" /> Show Supporting Chunks
          </button>
        </div>

        {chunksOpen && (
          <div className="mt-2 max-h-40 overflow-y-auto border-t border-zinc-900 pt-2">
            {chunksLoading && (
              <div className="flex items-center gap-2 text-[10px] text-zinc-500">
                <Loader2 className="h-3 w-3 animate-spin" /> loading chunks
              </div>
            )}
            {chunkError && (
              <div className="text-[10px] text-rose-300">{chunkError}</div>
            )}
            {!chunksLoading && !chunkError && chunkRows.length === 0 && (
              <div className="text-[10px] text-zinc-600">
                No chunk extraction rows loaded.
              </div>
            )}
            <ul className="space-y-1 font-mono text-[10px] text-zinc-400">
              {chunkRows.slice(0, 30).map((row) => (
                <li
                  key={row.chunk_id}
                  className="flex items-center justify-between gap-2"
                  title={row.chunk_id}
                >
                  <span className="truncate">
                    {row.docLabel ? `${compactName(row.docLabel)} · ` : ""}
                    {row.chunk_id}
                  </span>
                  <span className="shrink-0 text-zinc-600">
                    {row.entity_count}e/{row.relation_count}r
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-zinc-900 bg-black/20 px-2 py-1">
      <div className="uppercase tracking-widest text-zinc-600">{label}</div>
      <div className="mt-0.5 truncate text-zinc-300" title={value}>
        {value}
      </div>
    </div>
  );
}

function InspectorBlock({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mt-3">
      <div className="mb-1 font-mono text-[10px] uppercase tracking-widest text-zinc-600">
        {title}
      </div>
      <div className="text-[11px] leading-relaxed text-zinc-400">{children}</div>
    </div>
  );
}


// ────────────────────────────────────────────────────────────────────────
// Pt 7 — Agent Search tab. Wraps the existing query state that
// GraphViewer's useQueryGraph already manages; the tab just renders
// the input + the result summary chips. Heavy lifting (extracting
// seeds, expanding subgraph, calling discover for synthesis) happens
// in the parent so this component is purely presentational.
// ────────────────────────────────────────────────────────────────────────

interface AgentSearchTabProps {
  query: string;
  onChange: (q: string) => void;
  onRun: () => void;
  phase: "idle" | "loading" | "ready" | "error";
  error?: string | null;
  synthesisMarkdown?: string | null;
  seedNames?: string[];
  bridgeNames?: string[];
  hubNames?: string[];
  gaps?: Array<{ entity_a_name?: string; entity_b_name?: string }>;
  synthesisMode?: GraphSynthesisMode;
  onSynthesisModeChange?: (m: GraphSynthesisMode) => void;
  validateSynthesis?: boolean;
  onValidateSynthesisChange?: (v: boolean) => void;
  sources?: Array<{
    source_label?: string;
    doc_id?: string;
    chunk_id?: string;
    snippet?: string;
  }>;
  onOpenAtomic?: (text: string, mode?: GraphSynthesisMode) => void;
}

function AgentSearchTab(props: AgentSearchTabProps) {
  const {
    query,
    onChange,
    onRun,
    phase,
    error,
    synthesisMarkdown,
    seedNames,
    bridgeNames,
    hubNames,
    gaps,
    synthesisMode = "research",
    onSynthesisModeChange,
    validateSynthesis = false,
    onValidateSynthesisChange,
    sources = [],
    onOpenAtomic,
  } = props;
  const canRun = phase !== "loading" && query.trim().length > 0;
  return (
    <>
      <section>
        <div className="flex items-center justify-between gap-2">
          <SectionLabel>Ask</SectionLabel>
          {onSynthesisModeChange && (
            <div
              role="radiogroup"
              aria-label="Synthesis mode"
              className="inline-flex rounded-full bg-zinc-900 p-0.5 text-[9px] font-mono uppercase tracking-wider"
            >
              <button
                type="button"
                role="radio"
                aria-checked={synthesisMode === "research"}
                onClick={() => onSynthesisModeChange("research")}
                title="Faithful synthesis grounded in evidence."
                className={
                  "px-1.5 py-0.5 rounded-full transition-colors " +
                  (synthesisMode === "research"
                    ? "bg-amber-600 text-zinc-100"
                    : "text-zinc-500 hover:text-zinc-300")
                }
              >
                research
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={synthesisMode === "nuance"}
                onClick={() => onSynthesisModeChange("nuance")}
                title="Conceptual exploration of gaps, analogies, transfers, and bridges."
                className={
                  "px-1.5 py-0.5 rounded-full transition-colors " +
                  (synthesisMode === "nuance"
                    ? "bg-amber-600 text-zinc-100"
                    : "text-zinc-500 hover:text-zinc-300")
                }
              >
                nuance
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={synthesisMode === "ideation"}
                onClick={() => onSynthesisModeChange("ideation")}
                title="Speculative [BUILD IDEA] output grounded in corpus APIs."
                className={
                  "px-1.5 py-0.5 rounded-full transition-colors " +
                  (synthesisMode === "ideation"
                    ? "bg-amber-600 text-zinc-100"
                    : "text-zinc-500 hover:text-zinc-300")
                }
              >
                ideation
              </button>
            </div>
          )}
        </div>
        <textarea
          rows={3}
          value={query}
          onChange={(e) => onChange(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && canRun) {
              e.preventDefault();
              onRun();
            }
          }}
          placeholder={
            synthesisMode === "ideation"
              ? "What could I build from this corpus?"
              : synthesisMode === "nuance"
                ? "Where is the conceptual tension or hidden bridge?"
              : "What does my library think about…?"
          }
          className="w-full resize-none rounded border border-zinc-800 bg-[#0d0d14] px-2 py-1.5 font-mono text-xs text-zinc-100 placeholder:text-zinc-600 focus:border-amber-700 focus:outline-none"
        />
        <button
          onClick={onRun}
          disabled={!canRun}
          aria-busy={phase === "loading"}
          className={
            "mt-2 flex w-full items-center justify-center gap-2 rounded border px-2 py-1.5 font-mono text-[11px] uppercase tracking-widest disabled:cursor-not-allowed " +
            (phase === "loading"
              ? "border-amber-600/60 bg-amber-500/10 text-amber-200"
              : "border-zinc-800 bg-[#0d0d14] text-zinc-300 hover:border-amber-700 hover:text-amber-300 disabled:opacity-40")
          }
        >
          {phase === "loading" ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin" /> query accepted
            </>
          ) : (
            <>
              <Zap className="h-3 w-3" /> run query
            </>
          )}
        </button>
        <button
          onClick={() => onOpenAtomic?.(query, synthesisMode)}
          disabled={!onOpenAtomic || query.trim().length === 0}
          className="mt-1.5 flex w-full items-center justify-center gap-2 rounded border border-zinc-800 bg-[#0d0d14] px-2 py-1.5 font-mono text-[11px] uppercase tracking-widest text-zinc-300 hover:border-cyan-700 hover:text-cyan-300 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Sparkles className="h-3 w-3" /> open atom
        </button>
        <div className="mt-1 text-[10px] text-zinc-500 font-mono">
          {phase === "loading"
            ? "building subgraph first, then synthesis prose…"
            : phase === "error"
              ? error || "error"
              : "⌘/Ctrl + Enter to run"}
        </div>

        {onValidateSynthesisChange && (
          <label
            className={
              "mt-2 flex items-center gap-2 cursor-pointer select-none text-[10px] font-mono uppercase tracking-wider " +
              (validateSynthesis ? "text-amber-300" : "text-zinc-500 hover:text-zinc-300")
            }
            title="Run a second auditor + editor pass to catch fabricated terms, missing citations, and shell sentences. Costs ~3× the tokens of a normal query (draft + critique + revise calls)."
          >
            <input
              type="checkbox"
              checked={validateSynthesis}
              onChange={(e) => onValidateSynthesisChange(e.currentTarget.checked)}
              className="accent-amber-600"
            />
            <span className="flex items-center gap-1.5">
              <span>validate · draft → critique → revise</span>
              <span
                className={
                  "px-1 py-px rounded-sm text-[9px] tracking-widest " +
                  (validateSynthesis
                    ? "bg-amber-900/40 text-amber-200 border border-amber-700/40"
                    : "bg-zinc-800 text-zinc-500 border border-zinc-700")
                }
              >
                ~3× cost
              </span>
            </span>
          </label>
        )}
      </section>

      {phase === "ready" && synthesisMarkdown && (
        <section>
          <SectionLabel>
            {synthesisMode === "ideation"
              ? "Build Idea"
              : synthesisMode === "nuance"
                ? "Nuance"
                : "Synthesis"}
          </SectionLabel>
          <div className="synthesis-body rounded border border-zinc-800 bg-[#0d0d14] px-2.5 py-2 text-xs text-zinc-200 leading-relaxed max-h-[40vh] overflow-y-auto">
            <ReactMarkdown>{synthesisMarkdown}</ReactMarkdown>
          </div>
        </section>
      )}

      {phase === "ready" && sources.length > 0 && (
        <section>
          <SectionLabel>Sources ({sources.length})</SectionLabel>
          <ol className="space-y-1.5 font-mono text-[11px] text-zinc-400">
            {sources.slice(0, 10).map((source, i) => (
              <li
                key={source.chunk_id || i}
                className="rounded border border-zinc-900 bg-[#0d0d14] px-2 py-1"
                title={source.snippet}
              >
                <div className="truncate text-zinc-300">
                  {source.source_label || source.doc_id || source.chunk_id}
                </div>
                {source.snippet && (
                  <div className="mt-0.5 line-clamp-2 text-[10px] leading-relaxed text-zinc-500">
                    {source.snippet}
                  </div>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      {phase === "ready" && (seedNames?.length || 0) > 0 && (
        <ChipList label="Seeds" items={seedNames || []} tone="cyan" />
      )}
      {phase === "ready" && (bridgeNames?.length || 0) > 0 && (
        <ChipList label="Bridges" items={bridgeNames || []} tone="violet" />
      )}
      {phase === "ready" && (hubNames?.length || 0) > 0 && (
        <ChipList label="Hubs" items={hubNames || []} tone="amber" />
      )}
      {phase === "ready" && (gaps?.length || 0) > 0 && (
        <section>
          <SectionLabel>Gaps</SectionLabel>
          <ul className="space-y-1 font-mono text-[11px] text-zinc-400">
            {(gaps || []).slice(0, 8).map((g, i) => (
              <li key={i}>
                <span className="text-zinc-200">{g.entity_a_name || "?"}</span>{" "}
                ↔{" "}
                <span className="text-zinc-200">{g.entity_b_name || "?"}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

function ChipList({
  label,
  items,
  tone,
}: {
  label: string;
  items: string[];
  tone: "cyan" | "violet" | "amber";
}) {
  const toneClass =
    tone === "cyan"
      ? "border-cyan-700/40 bg-cyan-500/5 text-cyan-200"
      : tone === "violet"
        ? "border-violet-700/40 bg-violet-500/5 text-violet-200"
        : "border-amber-700/40 bg-amber-500/5 text-amber-200";
  return (
    <section>
      <SectionLabel>{label}</SectionLabel>
      <div className="flex flex-wrap gap-1.5">
        {items.slice(0, 12).map((name, i) => (
          <span
            key={`${name}-${i}`}
            className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${toneClass}`}
            title={name}
          >
            {name.length > 28 ? name.slice(0, 28) + "…" : name}
          </span>
        ))}
      </div>
    </section>
  );
}


// ────────────────────────────────────────────────────────────────────────
// Pt 7 — Graph Query tab. Two subsections in one tab:
//   (a) Entity-type search — filter the corpora's entities by type +
//       substring. Results are clickable and send the entity text to chat.
//   (b) HyDE refinement — user types a draft question, hits "Refine",
//       gets back three structured chip lists (alternative phrasings /
//       opposing framings / related questions). Each chip is clickable
//       → sends that text to the chat for actual RAG retrieval.
//
// Idempotency: api.refineQuery() + api.searchEntities() implement
// in-flight dedupe + short-TTL result cache so double-clicks, rapid
// re-runs, and re-mounts never duplicate LLM calls or DB reads.
// ────────────────────────────────────────────────────────────────────────

interface GraphQueryTabProps {
  corpusIds: string[];
  onSendToChat?: (text: string) => void;
  onOpenAtomic?: (text: string, mode?: GraphSynthesisMode) => void;
  model?: string;
}

function GraphQueryTab({
  corpusIds,
  onSendToChat,
  onOpenAtomic,
  model,
}: GraphQueryTabProps) {
  // Pt 7b: ONE input drives both HyDE refinement AND entity extraction.
  // The question is the all-purpose entity search — typing it surfaces:
  //   • alternative / opposing / related question chips (LLM-driven, cached)
  //   • entities mentioned in the question that already exist in the corpus
  //     (pure Cypher, recomputed every call)
  // A second optional filter at the bottom (entity-type pill row) lets the
  // user narrow the entity list without re-running.
  const [refineQ, setRefineQ] = useState<string>("");
  const [refineLoading, setRefineLoading] = useState(false);
  const [refineError, setRefineError] = useState<string | null>(null);
  const [refinement, setRefinement] = useState<RefinementResult | null>(null);
  const [refineCached, setRefineCached] = useState<boolean>(false);
  const [entities, setEntities] = useState<ExtractedEntity[]>([]);
  const [entityTypeFilter, setEntityTypeFilter] = useState<string>("");

  const canRefine =
    !refineLoading && refineQ.trim().length > 0 && corpusIds.length > 0;

  const runRefine = useCallback(async () => {
    if (!canRefine) return;
    setRefineLoading(true);
    setRefineError(null);
    try {
      // refineQuery is idempotent: backend has a 24h Mongo cache keyed by
      // hash(question + corpus_ids + model); frontend layers a 5min
      // result cache + in-flight dedupe on top of that. Same question
      // never triggers two LLM calls.
      //
      // Pt 7b: the same response now ALSO carries `entities` extracted
      // from the question via extract_query_entities (pure Cypher, not
      // cached on the backend so it reflects live graph state). One
      // round trip, two outputs.
      const res = await api.refineQuery(refineQ, corpusIds, model);
      setRefinement(res.result);
      setEntities(res.entities || []);
      setRefineCached(res.cached);
      if (res.error) setRefineError(res.error);
    } catch (e: any) {
      setRefineError(String(e?.message || e));
    } finally {
      setRefineLoading(false);
    }
  }, [canRefine, refineQ, corpusIds, model]);

  // Filter the surfaced entities by type pill (client-side, no extra fetch).
  const filteredEntities =
    entityTypeFilter === ""
      ? entities
      : entities.filter((e) => e.entity_type === entityTypeFilter);

  // Build the distinct entity-type pill row from what came back.
  const typesSeen = Array.from(
    new Set(entities.map((e) => e.entity_type).filter(Boolean)),
  );

  return (
    <>
      {/* ── A. Question input — THE primary interaction. Drives both
              HyDE refinement and entity extraction in one call. ──── */}
      <section>
        <SectionLabel>Ask a Question</SectionLabel>
        <textarea
          rows={3}
          value={refineQ}
          onChange={(e) => setRefineQ(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && canRefine) {
              e.preventDefault();
              runRefine();
            }
          }}
          placeholder="Type a question. Get alternative phrasings + the entities your library already has on this topic."
          className="w-full resize-none rounded border border-zinc-800 bg-[#0d0d14] px-2 py-1.5 font-mono text-xs text-zinc-100 placeholder:text-zinc-600 focus:border-amber-700 focus:outline-none"
        />
        <button
          onClick={runRefine}
          disabled={!canRefine}
          aria-busy={refineLoading}
          className={
            "mt-2 flex w-full items-center justify-center gap-2 rounded border px-2 py-1.5 font-mono text-[11px] uppercase tracking-widest disabled:cursor-not-allowed " +
            (refineLoading
              ? "border-amber-600/60 bg-amber-500/10 text-amber-200"
              : "border-zinc-800 bg-[#0d0d14] text-zinc-300 hover:border-amber-700 hover:text-amber-300 disabled:opacity-40")
          }
        >
          {refineLoading ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin" /> query accepted
            </>
          ) : (
            <>
              <Sparkles className="h-3 w-3" /> run
            </>
          )}
        </button>
        <button
          onClick={() => onOpenAtomic?.(refineQ, "research")}
          disabled={!onOpenAtomic || refineQ.trim().length === 0}
          className="mt-1.5 flex w-full items-center justify-center gap-2 rounded border border-zinc-800 bg-[#0d0d14] px-2 py-1.5 font-mono text-[11px] uppercase tracking-widest text-zinc-300 hover:border-cyan-700 hover:text-cyan-300 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Sparkles className="h-3 w-3" /> open atom
        </button>
        <div className="mt-1 text-[10px] text-zinc-500 font-mono">
          {refineLoading
            ? "refining phrasings + extracting entities…"
            : refineError
              ? refineError
              : refineCached
                ? "refinement from cache · entities fresh · ⌘/Ctrl + Enter"
                : "fresh run · cached 24h · ⌘/Ctrl + Enter"}
        </div>
      </section>

      {/* ── B. HyDE chip groups (when refined) ─────────────────────── */}
      {refinement && (
        <>
          {refinement.alternative_phrasings.length > 0 && (
            <RefineChips
              label="Alternative Phrasings"
              tone="cyan"
              items={refinement.alternative_phrasings}
              onPick={onSendToChat}
            />
          )}
          {refinement.opposing_framings.length > 0 && (
            <RefineChips
              label="Opposing Framings"
              tone="rose"
              items={refinement.opposing_framings}
              onPick={onSendToChat}
            />
          )}
          {refinement.related_questions.length > 0 && (
            <RefineChips
              label="Related Questions"
              tone="emerald"
              items={refinement.related_questions}
              onPick={onSendToChat}
            />
          )}
        </>
      )}

      {/* ── C. Entities surfaced from the question ─────────────────── */}
      {entities.length > 0 && (
        <section>
          <SectionLabel>
            Entities in Your Library ({filteredEntities.length}/{entities.length})
          </SectionLabel>
          {typesSeen.length > 1 && (
            <div className="mb-2 flex flex-wrap gap-1">
              <button
                onClick={() => setEntityTypeFilter("")}
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-widest ${
                  entityTypeFilter === ""
                    ? "border-amber-500/60 bg-amber-500/10 text-amber-200"
                    : "border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:text-zinc-200"
                }`}
              >
                all
              </button>
              {typesSeen.map((t) => (
                <button
                  key={t}
                  onClick={() => setEntityTypeFilter(t)}
                  className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-widest ${
                    entityTypeFilter === t
                      ? "border-amber-500/60 bg-amber-500/10 text-amber-200"
                      : "border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:text-zinc-200"
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>
          )}
          <div className="max-h-72 overflow-y-auto rounded border border-zinc-900">
            {filteredEntities.map((row) => (
              <button
                key={row.entity_id}
                onClick={() => onSendToChat?.(row.display_name)}
                className="flex w-full items-center justify-between gap-2 border-b border-zinc-900 px-2 py-1 text-left last:border-b-0 hover:bg-zinc-900/50"
                title={`${row.entity_type} · ${row.mention_count} mentions${
                  row.score != null ? ` · score ${row.score.toFixed(1)}` : ""
                }`}
              >
                <span className="font-mono text-xs text-zinc-100 truncate">
                  {row.display_name}
                </span>
                <span className="shrink-0 font-mono text-[9px] uppercase tracking-widest text-zinc-500">
                  {row.entity_type}
                </span>
              </button>
            ))}
            {filteredEntities.length === 0 && (
              <div className="px-2 py-2 text-[10px] text-zinc-600 font-mono">
                no entities match this filter
              </div>
            )}
          </div>
        </section>
      )}

      {!refinement && !refineLoading && (
        <div className="text-[11px] text-zinc-600 font-mono">
          Type a question above and run it. You'll get suggested phrasings +
          the actual entities your library has on the topic. Click any chip
          or entity to send it to the chat.
        </div>
      )}
    </>
  );
}

function RefineChips({
  label,
  tone,
  items,
  onPick,
}: {
  label: string;
  tone: "cyan" | "rose" | "emerald";
  items: string[];
  onPick?: (text: string) => void;
}) {
  const toneClass =
    tone === "cyan"
      ? "border-cyan-700/40 bg-cyan-500/5 text-cyan-100 hover:border-cyan-500/70 hover:bg-cyan-500/15"
      : tone === "rose"
        ? "border-rose-700/40 bg-rose-500/5 text-rose-100 hover:border-rose-500/70 hover:bg-rose-500/15"
        : "border-emerald-700/40 bg-emerald-500/5 text-emerald-100 hover:border-emerald-500/70 hover:bg-emerald-500/15";
  return (
    <section>
      <SectionLabel>{label}</SectionLabel>
      <ul className="space-y-1.5">
        {items.map((text, i) => (
          <li key={`${i}-${text.slice(0, 24)}`}>
            <button
              onClick={() => onPick?.(text)}
              disabled={!onPick}
              className={`group flex w-full items-start gap-2 rounded border px-2 py-1.5 text-left font-mono text-[11px] leading-relaxed transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${toneClass}`}
              title={onPick ? "Send to chat" : "Read-only"}
            >
              <Send className="h-3 w-3 shrink-0 mt-0.5 opacity-50 group-hover:opacity-100 transition-opacity" />
              <span className="flex-1 min-w-0 break-words">{text}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
