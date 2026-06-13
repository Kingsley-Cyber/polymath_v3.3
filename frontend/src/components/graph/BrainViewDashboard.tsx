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

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ChevronLeft,
  GitBranch,
  Loader2,
  PanelRightClose,
  PanelRightOpen,
  Pause,
  Play,
  PlusCircle,
  Send,
  Sparkles,
  Zap,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type {
  CacheStatus,
  ConceptQuestionPacket,
  ContextualQuestions,
  ExtractedEntity,
  RefinementResult,
} from "../../lib/api";
import * as api from "../../lib/api";
import { cleanBookLabel } from "../../lib/label-utils";
import {
  EDGE_COLORS_BY_FAMILY,
  type RelationFamily,
} from "../../lib/sigma-constants";
import { useQueryModelPoolStore } from "../../stores/queryModelPoolStore";
import type { GraphNodeInsightResponse } from "../../types/chat";
import type { GraphSynthesisMode } from "../../types/discover";

export type DashboardTab = "brain" | "agent" | "graph-query";

export type GraphRunMode = "new" | "followup";

export type GraphProgressStatus =
  | "pending"
  | "running"
  | "done"
  | "error"
  | "skipped";

export interface GraphProgressStep {
  id: string;
  label: string;
  status: GraphProgressStatus;
  detail?: string;
}

function BackToBrainButton({
  onClick,
  className = "",
}: {
  onClick: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="Back to Brain"
      title="Back to Brain"
      className={
        "group relative flex w-full items-center justify-between overflow-hidden rounded-md border border-cyan-500/25 bg-[radial-gradient(circle_at_15%_50%,rgba(34,211,238,0.14),transparent_42%),linear-gradient(90deg,rgba(8,47,73,0.28),rgba(9,9,14,0.82))] px-3 py-2 text-[10px] font-technical uppercase tracking-widest text-cyan-100 shadow-[0_0_24px_rgba(8,145,178,0.08)] transition-all hover:border-cyan-300/55 hover:bg-cyan-500/10 hover:text-white " +
        className
      }
    >
      <span className="flex items-center gap-2">
        <span className="relative grid h-6 w-8 place-items-center rounded border border-cyan-400/20 bg-black/20">
          <span className="absolute left-1 top-1 h-1.5 w-1.5 rounded-full bg-amber-300 shadow-[0_0_10px_rgba(252,211,77,0.8)] transition-transform group-hover:-translate-x-0.5" />
          <span className="absolute right-1.5 top-1 h-1 w-1 rounded-full bg-cyan-300/80 transition-transform group-hover:translate-x-0.5" />
          <span className="absolute bottom-1 right-2 h-1 w-1 rounded-full bg-emerald-300/80 transition-transform group-hover:translate-x-0.5" />
          <span className="absolute bottom-1.5 left-3.5 h-1 w-1 rounded-full bg-violet-300/70 transition-transform group-hover:translate-x-0.5" />
          <span className="absolute left-2 top-[11px] h-px w-4 origin-left rotate-[-12deg] bg-cyan-300/25" />
          <ChevronLeft className="relative h-3.5 w-3.5 text-cyan-200 transition-transform group-hover:-translate-x-0.5" />
        </span>
        Back to Brain
      </span>
      <span className="h-1.5 w-1.5 rounded-full bg-cyan-300/70 shadow-[0_0_12px_rgba(103,232,249,0.9)]" />
    </button>
  );
}

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
  nodeKind?: string;
  kind?: string;
  entity_type?: string;
  dominant_entity_type?: string;
  dominant_relation_family?: string;
  primary_doc_id?: string;
  bridge_count?: number;
  chunk_count?: number;
  parent_count?: number;
  entity_count?: number;
  top_entities?: string[];
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

  // Color mode (brain only)
  colorMode: "community" | "corpus";
  onColorModeToggle: () => void;

  // Pt 6: bridge filter knobs (brain mode only)
  minBridgeStrength: number;
  onMinBridgeStrengthChange: (n: number) => void;
  maxBridgesPerBook: number;
  onMaxBridgesPerBookChange: (n: number) => void;

  // Pt 6: settle-restart-after-drag toggle
  settleAfterDrag: boolean;
  onSettleAfterDragToggle: () => void;

  // Pt 7: tab + Agent / Graph Query content
  activeTab: DashboardTab;
  onActiveTabChange: (t: DashboardTab) => void;
  // Agent Search tab — wired to existing useQueryGraph state
  agentQuery: string;
  onAgentQueryChange: (q: string) => void;
  onAgentRun: (mode?: GraphRunMode) => void;
  agentPhase: "idle" | "loading" | "ready" | "error";
  agentError?: string | null;
  agentSynthesisMarkdown?: string | null;
  agentProgressSteps?: GraphProgressStep[];
  questionProgressSteps?: GraphProgressStep[];
  agentSeedNames?: string[];
  agentSourceNames?: string[];
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
  followUpAvailable?: boolean;
  followUpPreview?: string;
  // Pt 7: Graph Query tab — send a refined chip back to the chat.
  onSendToChat?: (text: string) => void;
  // Lightweight question-builder path: build a visual query graph without
  // invoking the heavier graph synthesis pipeline.
  onBuildQuestionGraph?: (query: string) => void;

  // Query mode actions
  onRerun?: () => void;
  showQueryRerun?: boolean;

  onClearGraphQuery?: () => void;

  // Selection info
  selectedDisplay: SelectedDisplay | null;
  onClearSelection: () => void;

  // Layout state
  isLayoutRunning: boolean;
  startLayout: () => void;
  stopLayout: () => void;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono mb-1.5">
      {children}
    </div>
  );
}

function GraphProgressNarrator({ steps }: { steps?: GraphProgressStep[] }) {
  const visible = (steps || []).filter((step) => step.status !== "pending");
  const runningSinceRef = useRef<Record<string, number>>({});
  const [nowMs, setNowMs] = useState(() => Date.now());
  const hasRunning = visible.some((step) => step.status === "running");

  for (const step of visible) {
    if (step.status === "running" && !runningSinceRef.current[step.id]) {
      runningSinceRef.current[step.id] = Date.now();
    }
  }

  useEffect(() => {
    if (!hasRunning) {
      runningSinceRef.current = {};
      return;
    }
    const timer = window.setInterval(() => setNowMs(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [hasRunning]);

  if (visible.length === 0) return null;

  const statusClass = (status: GraphProgressStatus) => {
    if (status === "done") return "border-emerald-500/35 bg-emerald-500/10 text-emerald-200";
    if (status === "running") return "border-amber-500/45 bg-amber-500/10 text-amber-100";
    if (status === "error") return "border-rose-500/40 bg-rose-500/10 text-rose-200";
    if (status === "skipped") return "border-zinc-700 bg-zinc-950/70 text-zinc-500";
    return "border-zinc-800 bg-zinc-950/70 text-zinc-500";
  };
  const mark = (status: GraphProgressStatus) => {
    if (status === "done") return "✓";
    if (status === "error") return "✗";
    if (status === "running") return "→";
    return "·";
  };
  const elapsedLabel = (step: GraphProgressStep) => {
    const started = runningSinceRef.current[step.id];
    if (!started) return "";
    const elapsed = Math.max(0, nowMs - started);
    const seconds = Math.floor(elapsed / 1000);
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
  };
  const meterLeft = (step: GraphProgressStep) => {
    const started = runningSinceRef.current[step.id] || nowMs;
    const elapsed = Math.max(0, nowMs - started);
    return `${((elapsed % 1200) / 1200) * 72}%`;
  };

  return (
    <div className="mb-3 rounded-md border border-zinc-800 bg-zinc-950/65 px-2.5 py-2">
      <div className="mb-1.5 text-[9px] font-technical uppercase tracking-widest text-zinc-500">
        What I'm doing
      </div>
      <ol className="space-y-1.5">
        {visible.map((step) => (
          <li
            key={step.id}
            className={`rounded border px-2 py-1.5 text-[11px] leading-relaxed ${statusClass(step.status)}`}
          >
            <div className="flex gap-2">
              <span className="flex h-4 w-4 shrink-0 items-center justify-center font-mono text-[11px]">
                {step.status === "running" ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  mark(step.status)
                )}
              </span>
              <span className="min-w-0 flex-1">{step.label}</span>
            </div>
            {step.status === "running" && (
              <div className="mt-1.5 pl-6">
                <div className="relative h-1 overflow-hidden rounded-full bg-zinc-950/80">
                  <div
                    className="absolute top-0 h-full w-[28%] rounded-full bg-amber-300/80 shadow-[0_0_10px_rgba(252,211,77,0.45)] transition-transform duration-200"
                    style={{ transform: `translateX(${meterLeft(step)})` }}
                  />
                </div>
                <div className="mt-1 font-mono text-[9px] uppercase tracking-widest text-amber-200/70">
                  running · {elapsedLabel(step)}
                </div>
              </div>
            )}
            {step.detail && (
              <div className="mt-1 pl-5 font-mono text-[10px] text-zinc-500">
                {step.detail}
              </div>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

function statusDot(s: string | undefined): string {
  if (s === "ready") return "bg-emerald-400";
  if (s === "warming") return "bg-amber-400 animate-pulse";
  return "bg-rose-500";
}

function GraphPathModelControl({ activeTab }: { activeTab: DashboardTab }) {
  const pool = useQueryModelPoolStore((s) => s.config.query_model_pool);
  const graphQuery = useQueryModelPoolStore((s) => s.config.graph_query);
  const patchGraphQuery = useQueryModelPoolStore((s) => s.patchGraphQuery);
  const saveModels = useQueryModelPoolStore((s) => s.save);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const modelOptions = pool.filter((entry) => entry.enabled);
  const selectedEntryId = graphQuery?.pool_entry_id ?? "";
  const label =
    activeTab === "graph-query"
      ? "Refine Model"
      : activeTab === "agent"
        ? "Graph Synthesis Model"
        : "Graph Question Model";

  const handleChange = async (entryId: string) => {
    setError(null);
    patchGraphQuery({ pool_entry_id: entryId || null });
    setSaving(true);
    try {
      await saveModels();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="border-b border-zinc-900 px-3 py-2">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono">
          {label}
        </span>
        <span className="rounded border border-amber-500/25 bg-amber-500/5 px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-widest text-amber-300/80">
          graph role
        </span>
      </div>
      {modelOptions.length > 0 ? (
        <select
          value={selectedEntryId}
          onChange={(e) => void handleChange(e.currentTarget.value)}
          disabled={saving}
          className="w-full rounded-md border border-zinc-800 bg-[#09090f] px-2 py-1.5 text-xs font-mono text-zinc-200 outline-none focus:border-amber-500/70"
          title="Graph Query and Refine use this graph-specific model role."
        >
          <option value="">Fallback to query/chat model</option>
          {modelOptions.map((entry) => (
            <option key={entry.entry_id} value={entry.entry_id}>
              {entry.provider} · {entry.model_name}
            </option>
          ))}
        </select>
      ) : (
        <div className="truncate rounded-md border border-zinc-800 bg-[#09090f] px-2 py-1.5 text-xs font-mono text-zinc-500">
          No models in pool
        </div>
      )}
      <div className="mt-1 min-h-[14px] text-[10px] font-mono text-zinc-500">
        {saving ? "saving..." : error ? error : "Graph Query #1 and #2 use this role."}
      </div>
    </div>
  );
}

const RELATION_LEGEND_ENTRIES = Object.entries(EDGE_COLORS_BY_FAMILY) as Array<
  [RelationFamily, string]
>;

function endpointId(value: unknown): string {
  if (value && typeof value === "object") {
    const obj = value as { id?: unknown; key?: unknown };
    return String(obj.id ?? obj.key ?? "");
  }
  return String(value ?? "");
}

function relationFamily(edge: any): string {
  return String(
    edge?.dominant_relation_family ||
      edge?.relation_family ||
      (edge?.predicate === "bridges_to" ? "WeakAssociation" : "") ||
      "WeakAssociation",
  );
}

function relationColor(edge: any): string {
  const family = relationFamily(edge) as RelationFamily;
  return EDGE_COLORS_BY_FAMILY[family] ?? EDGE_COLORS_BY_FAMILY.WeakAssociation;
}

function relationLabel(edge: any): string {
  const predicate = String(edge?.predicate || "related_to");
  if (predicate === "bridges_to") return "bridge";
  return predicate.replace(/_/g, " ");
}

function nodeLabel(data: { nodes: any[] }, id: string, fallback?: string): string {
  const node = (data.nodes || []).find((n: any) => String(n.id) === String(id));
  return String(node?.display_name || node?.label || fallback || id).trim();
}

function selectionBadgeItems(selected: SelectedDisplay): string[] {
  const items = [
    selected.nodeKind || selected.kind,
    selected.entity_type || selected.dominant_entity_type,
    selected.dominant_relation_family,
    typeof selected.bridge_count === "number"
      ? `${selected.bridge_count} bridges`
      : null,
  ];
  const seen = new Set<string>();
  return items
    .filter(Boolean)
    .map((item) => String(item).trim())
    .filter((item) => {
      if (!item) return false;
      const key = item.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
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
    colorMode,
    onColorModeToggle,
    minBridgeStrength,
    onMinBridgeStrengthChange,
    maxBridgesPerBook,
    onMaxBridgesPerBookChange,
    settleAfterDrag,
    onSettleAfterDragToggle,
    activeTab,
    onActiveTabChange,
    agentQuery,
    onAgentQueryChange,
    onAgentRun,
    agentPhase,
    agentError,
    agentSynthesisMarkdown,
    agentProgressSteps,
    questionProgressSteps,
    agentSeedNames,
    agentSourceNames,
    agentBridgeNames,
    agentHubNames,
    agentGaps,
    synthesisMode,
    onSynthesisModeChange,
    validateSynthesis,
    onValidateSynthesisChange,
    followUpAvailable,
    followUpPreview,
    onSendToChat,
    onBuildQuestionGraph,
    onRerun,
    showQueryRerun,
    onClearGraphQuery,
    selectedDisplay,
    onClearSelection,
    isLayoutRunning,
    startLayout,
    stopLayout,
  } = props;

  // Drag-resize state. Persists across renders within the component
  // instance; resets to default when the dashboard remounts.
  const [width, setWidth] = useState<number>(SIDEBAR_DEFAULT_W);
  const [nodeInsight, setNodeInsight] =
    useState<GraphNodeInsightResponse | null>(null);
  const [nodeInsightPhase, setNodeInsightPhase] = useState<
    "idle" | "loading" | "ready" | "error"
  >("idle");
  const [nodeInsightError, setNodeInsightError] = useState<string | null>(null);
  const draggingRef = useRef<{ startX: number; startW: number } | null>(null);
  const insightRequestRef = useRef(0);

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

  useEffect(() => {
    const requestId = ++insightRequestRef.current;
    if (!selectedDisplay || corpusIds.length === 0) {
      setNodeInsight(null);
      setNodeInsightPhase("idle");
      setNodeInsightError(null);
      return;
    }

    setNodeInsight(null);
    setNodeInsightPhase("loading");
    setNodeInsightError(null);

    api
      .getGraphNodeInsight({
        corpusIds,
        nodeId: selectedDisplay.id,
        label:
          cleanBookLabel(selectedDisplay.display_name) ||
          selectedDisplay.display_name,
        entityType:
          selectedDisplay.entity_type ||
          selectedDisplay.dominant_entity_type ||
          null,
        nodeKind: selectedDisplay.nodeKind || selectedDisplay.kind || null,
        topEntities: selectedDisplay.top_entities || [],
        limit: 8,
      })
      .then((res) => {
        if (requestId !== insightRequestRef.current) return;
        setNodeInsight(res);
        setNodeInsightPhase("ready");
      })
      .catch((err) => {
        if (requestId !== insightRequestRef.current) return;
        setNodeInsight(null);
        setNodeInsightPhase("error");
        setNodeInsightError(err instanceof Error ? err.message : String(err));
      });
  }, [
    corpusIds,
    selectedDisplay?.id,
    selectedDisplay?.display_name,
    selectedDisplay?.entity_type,
    selectedDisplay?.dominant_entity_type,
    selectedDisplay?.nodeKind,
    selectedDisplay?.kind,
    selectedDisplay?.top_entities,
  ]);

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
      {/* Header strip — graph close is handled by the modal-level button in
          App.tsx. This panel only owns dashboard collapse / graph controls. */}
      <div className="flex items-start justify-between border-b border-zinc-900 px-3 py-2.5 gap-2">
        <div className="flex items-start gap-2 min-w-0">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono">
              {mode === "brain" ? "Corpora View" : "Query View"}
            </div>
            <div className="text-xs text-zinc-300 font-mono mt-0.5 truncate">
              {corpusIds.length} corpora
              {data && (
                <span className="text-zinc-500">
                  {" "}· {data.nodes.length}n · {data.links.length}e
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

      {/* Pt 7: Tab strip. The lightweight question-builder now lives inside
          Brain, so users don't have to decide between "Brain" and "Refine."
          Graph Query remains the heavier synthesis path. */}
      <div className="flex items-stretch border-b border-zinc-900 px-1.5 pt-1.5 pb-0 gap-0.5">
        {([
          { id: "brain", label: "Brain" },
          { id: "agent", label: "Graph Query" },
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

      <GraphPathModelControl activeTab={activeTab} />

      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-4">
        {/* Persistent Selection card — visible on every tab, sits at the
            top of the body so clicking a node in the canvas always shows
            its info regardless of which tab is active. */}
        {selectedDisplay && (
          <section>
            <SectionLabel>Selection</SectionLabel>
            <div className="rounded-lg border border-violet-500/30 bg-violet-500/10 p-2">
              <div className="flex items-start gap-2 min-w-0">
                <div className="h-2 w-2 animate-pulse rounded-full bg-violet-300 mt-1.5 shrink-0" />
                <div
                  className="font-mono text-xs text-zinc-100 break-words min-w-0 flex-1"
                  title={selectedDisplay.display_name}
                >
                  {cleanBookLabel(selectedDisplay.display_name) ||
                    selectedDisplay.display_name}
                </div>
              </div>
              {selectedDisplay.source_corpora.length > 0 && (
                <div className="mt-1 ml-4 text-[10px] text-zinc-400 font-mono">
                  {selectedDisplay.source_corpora.length} corpora
                </div>
              )}
              <div className="mt-2 ml-4 flex flex-wrap gap-1">
                {selectionBadgeItems(selectedDisplay)
                  .map((item) => (
                    <span
                      key={item}
                      className="rounded border border-zinc-800 bg-zinc-950/70 px-1.5 py-0.5 text-[10px] text-zinc-400"
                    >
                      {item}
                    </span>
                  ))}
              </div>
              {(selectedDisplay.top_entities?.length || 0) > 0 && (
                <div className="mt-2 ml-4 line-clamp-2 text-[10px] leading-relaxed text-zinc-500">
                  {selectedDisplay.top_entities?.slice(0, 5).join(" · ")}
                </div>
              )}
              <button
                onClick={onClearSelection}
                className="mt-2 ml-4 rounded px-2 py-0.5 text-[11px] text-zinc-300 hover:bg-white/10 hover:text-zinc-50"
              >
                Clear
              </button>
            </div>
          </section>
        )}

        {/* Connections — answers "how is this node connected to others?"
            Reads the rendered graph payload (data.links + data.nodes) and
            lists each edge incident to the selected node with direction,
            predicate, and weight. Closes the "dead-end click" gap where
            selecting a node previously only showed its name. */}
        {selectedDisplay && data && (
          <section className="mt-3">
            <SectionLabel>Evidence Inspector</SectionLabel>
            <ul className="space-y-1.5 font-mono text-[11px] max-h-56 overflow-auto pr-1 custom-scroll">
              {(() => {
                const selId = String(selectedDisplay.id);
                const rels = (data.links || []).filter((l: any) => {
                  // Edges arrive either flat (source: "id") or hydrated
                  // (source: {id}) depending on the renderer pass. Handle
                  // both shapes so this works in every code path.
                  const s = endpointId(l.source);
                  const t = endpointId(l.target);
                  return s === selId || t === selId;
                });
                if (rels.length === 0) {
                  return (
                    <li className="text-zinc-600 italic">
                      No visible connections
                    </li>
                  );
                }
                return rels.slice(0, 25).map((l: any, i: number) => {
                  const s = endpointId(l.source);
                  const t = endpointId(l.target);
                  const isSource = s === selId;
                  const nid = isSource ? t : s;
                  const explicitLabel = isSource ? l.target_label : l.source_label;
                  const name = nodeLabel(data, nid, explicitLabel);
                  const family = relationFamily(l);
                  const shared = Array.isArray(l.top_shared_entities)
                    ? l.top_shared_entities.slice(0, 3)
                    : [];
                  return (
                    <li
                      key={`${nid}-${i}`}
                      className="rounded border border-zinc-800 bg-[#0d0d14] px-2 py-1.5 text-zinc-300"
                    >
                      <div className="flex items-center gap-2">
                        <span
                          className="h-2 w-2 shrink-0 rounded-full"
                          style={{ backgroundColor: relationColor(l) }}
                        />
                        <span
                          className="text-zinc-500"
                          title={isSource ? "outgoing" : "incoming"}
                        >
                          {isSource ? "→" : "←"}
                        </span>
                        <span className="min-w-0 flex-1 truncate" title={name}>
                          {name}
                        </span>
                        <span className="shrink-0 rounded bg-zinc-900 px-1 text-[10px] text-zinc-500">
                          {relationLabel(l)}
                        </span>
                      </div>
                      <div className="mt-1 ml-7 flex flex-wrap items-center gap-1 text-[10px] text-zinc-500">
                        <span>{family}</span>
                        {typeof l.weight === "number" && (
                          <span>strength {l.weight}</span>
                        )}
                        {typeof l.confidence === "number" && (
                          <span>{Math.round(l.confidence * 100)}% conf</span>
                        )}
                      </div>
                      {shared.length > 0 && (
                        <div className="mt-1 ml-7 truncate text-[10px] text-zinc-400">
                          {shared.join(" · ")}
                          {typeof l.shared_entities === "number" &&
                            l.shared_entities > shared.length &&
                            ` +${l.shared_entities - shared.length}`}
                        </div>
                      )}
                    </li>
                  );
                });
              })()}
            </ul>
          </section>
        )}

        {selectedDisplay && (
          <section className="mt-3">
            <SectionLabel>Semantic Map</SectionLabel>
            <div className="rounded border border-cyan-500/20 bg-cyan-500/[0.035] p-2">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0 truncate text-[11px] text-zinc-300">
                  vector neighbors · read-only
                </div>
                <div
                  className={
                    "shrink-0 rounded-full border px-1.5 py-0.5 text-[9px] font-mono " +
                    (nodeInsightPhase === "loading"
                      ? "border-cyan-500/40 bg-cyan-500/10 text-cyan-200"
                      : nodeInsightPhase === "ready"
                        ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                        : nodeInsightPhase === "error"
                          ? "border-rose-500/40 bg-rose-500/10 text-rose-200"
                          : "border-zinc-800 bg-zinc-950/70 text-zinc-500")
                  }
                >
                  {nodeInsightPhase}
                </div>
              </div>

              {nodeInsightPhase === "loading" && (
                <div className="mt-2 text-[10px] text-zinc-500">
                  searching nearby passages and documents…
                </div>
              )}

              {nodeInsightPhase === "error" && (
                <div className="mt-2 text-[10px] text-rose-300">
                  {nodeInsightError || "semantic lookup failed"}
                </div>
              )}

              {nodeInsightPhase === "ready" && nodeInsight && (
                <div className="mt-2">
                  {nodeInsight.related_entities.length > 0 ? (
                    <div className="grid grid-cols-1 gap-1.5">
                      {nodeInsight.related_entities.slice(0, 8).map((entity) => {
                        const confidence =
                          typeof entity.confidence === "number"
                            ? Math.round(entity.confidence * 100)
                            : null;
                        return (
                          <div
                            key={`${entity.name}-${entity.predicate || ""}`}
                            className="rounded border border-cyan-700/30 bg-[#0d0d14] px-2 py-1.5"
                            title={[
                              entity.predicate,
                              entity.relation_family,
                              confidence != null ? `${confidence}%` : "",
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                          >
                            <div className="flex items-start justify-between gap-2">
                              <div className="min-w-0 truncate text-[11px] font-semibold text-cyan-100">
                                {entity.name}
                              </div>
                              {entity.count > 1 && (
                                <div className="shrink-0 rounded bg-cyan-500/10 px-1.5 py-0.5 text-[9px] text-cyan-300">
                                  {entity.count}x
                                </div>
                              )}
                            </div>
                            <div className="mt-1 flex flex-wrap gap-1 text-[9px] text-zinc-500">
                              {entity.relation_family && (
                                <span>{entity.relation_family}</span>
                              )}
                              {entity.predicate && (
                                <span>{entity.predicate.replace(/_/g, " ")}</span>
                              )}
                              {confidence != null && confidence > 0 && (
                                <span>{confidence}%</span>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="text-[10px] text-zinc-500">
                      No entity associations found.
                    </div>
                  )}
                </div>
              )}
            </div>
          </section>
        )}

        {/* Pt 7: Agent Search tab content — input + run + synthesis chips. */}
        {activeTab === "agent" && (
          <AgentSearchTab
            query={agentQuery}
            onChange={onAgentQueryChange}
            onRun={onAgentRun}
            onClear={onClearGraphQuery}
            phase={agentPhase}
            error={agentError}
            synthesisMarkdown={agentSynthesisMarkdown}
            progressSteps={agentProgressSteps}
            seedNames={agentSeedNames}
            sourceNames={agentSourceNames}
            bridgeNames={agentBridgeNames}
            hubNames={agentHubNames}
            gaps={agentGaps}
            synthesisMode={synthesisMode}
            onSynthesisModeChange={onSynthesisModeChange}
            validateSynthesis={validateSynthesis}
            onValidateSynthesisChange={onValidateSynthesisChange}
            followUpAvailable={followUpAvailable}
            followUpPreview={followUpPreview}
          />
        )}

        {/* Brain tab — original sections (only render when this tab is active). */}
        {activeTab === "brain" && mode === "brain" && (
          <BrainQuickGraphCard
            query={agentQuery}
            onChange={onAgentQueryChange}
            onRun={() => onAgentRun("new")}
            phase={agentPhase}
            progressSteps={agentProgressSteps}
            disabled={corpusIds.length === 0}
          />
        )}

        {activeTab === "brain" && (
          <GraphQueryTab
            corpusIds={corpusIds}
            onSendToChat={onSendToChat}
            onBuildGraph={onBuildQuestionGraph}
            graphProgressSteps={questionProgressSteps}
            graphActive={mode === "query"}
            onClearGraph={onClearGraphQuery}
          />
        )}

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

        {/* Selection card was moved up — now persistent across all tabs
            (rendered above the tab-specific content). Removed from here. */}

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

        {/* Color mode — brain tab only */}
        {activeTab === "brain" && mode === "brain" && (
          <section>
            <SectionLabel>Color Scheme</SectionLabel>
            <button
              className="w-full rounded border border-zinc-800 bg-[#0d0d14] px-2 py-1.5 text-left text-[11px] font-mono uppercase tracking-widest text-zinc-300 hover:border-amber-700 hover:text-amber-300"
              onClick={onColorModeToggle}
            >
              {colorMode}
              <span className="ml-2 text-zinc-500 normal-case tracking-normal">
                (click to swap)
              </span>
            </button>
          </section>
        )}

        {activeTab === "brain" && mode === "brain" && (
          <section>
            <SectionLabel>Relation Legend</SectionLabel>
            <div className="grid grid-cols-2 gap-1.5 rounded border border-zinc-800 bg-[#0d0d14] p-2">
              {RELATION_LEGEND_ENTRIES.map(([family, color]) => (
                <div
                  key={family}
                  className="flex min-w-0 items-center gap-1.5 text-[10px] font-mono text-zinc-400"
                  title={`${family} relations`}
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

        {/* Pt 6: Bridge filters — brain tab only. Sliders let the user
            tune which bridges show without re-fetching from backend. */}
        {activeTab === "brain" && mode === "brain" && (
          <section>
            <SectionLabel>Bridge Filters</SectionLabel>
            <div className="space-y-2.5">
              <label className="block">
                <div className="flex items-center justify-between font-mono text-[10px] uppercase tracking-widest text-zinc-400">
                  <span>min strength</span>
                  <span className="text-amber-300">{minBridgeStrength}</span>
                </div>
                <input
                  type="range"
                  min={1}
                  max={20}
                  step={1}
                  value={minBridgeStrength}
                  onChange={(e) =>
                    onMinBridgeStrengthChange(Number(e.currentTarget.value))
                  }
                  className="mt-1 w-full accent-amber-400"
                />
                <div className="mt-0.5 text-[10px] text-zinc-500 font-mono">
                  hide bridges with fewer shared entities
                </div>
              </label>
              <label className="block">
                <div className="flex items-center justify-between font-mono text-[10px] uppercase tracking-widest text-zinc-400">
                  <span>top-N per book</span>
                  <span className="text-amber-300">{maxBridgesPerBook}</span>
                </div>
                <input
                  type="range"
                  min={1}
                  max={10}
                  step={1}
                  value={maxBridgesPerBook}
                  onChange={(e) =>
                    onMaxBridgesPerBookChange(Number(e.currentTarget.value))
                  }
                  className="mt-1 w-full accent-amber-400"
                />
                <div className="mt-0.5 text-[10px] text-zinc-500 font-mono">
                  keep only strongest N bridges per book
                </div>
              </label>
            </div>
          </section>
        )}

        {/* Layout controls — brain tab only (Agent + Graph Query tabs have
            their own panels and don't need pan/zoom-style layout knobs). */}
        {activeTab === "brain" && (
          <section>
          <SectionLabel>Layout</SectionLabel>
          <button
            onClick={isLayoutRunning ? stopLayout : startLayout}
            className={`flex w-full items-center gap-2 rounded border px-2 py-1.5 font-mono text-[11px] uppercase tracking-widest ${
              isLayoutRunning
                ? "border-violet-500 bg-violet-500/20 text-violet-100"
                : "border-zinc-800 bg-[#0d0d14] text-zinc-300 hover:border-amber-700 hover:text-amber-300"
            }`}
          >
            {isLayoutRunning ? (
              <>
                <Pause className="h-3 w-3" /> pause settling
              </>
            ) : (
              <>
                <Play className="h-3 w-3" /> run layout
              </>
            )}
          </button>
          {/* Pt 6: settle-after-drag toggle. When ON, FA2 re-runs for ~5s
              after the user releases a dragged book so neighbors re-arrange
              around the new position. */}
          <label className="mt-2 flex items-center gap-2 font-mono text-[11px] text-zinc-300 cursor-pointer">
            <input
              type="checkbox"
              className="h-3.5 w-3.5 accent-amber-400 cursor-pointer"
              checked={settleAfterDrag}
              onChange={onSettleAfterDragToggle}
            />
            <span>re-settle after drag</span>
          </label>
          <div className="mt-1 ml-5 text-[10px] text-zinc-500 font-mono">
            restart layout briefly after releasing a node
          </div>
        </section>
        )}

        {/* Query mode actions — only on the legacy Query mode (different
            from the new Agent Search tab which has its own re-run button). */}
        {activeTab === "brain" && mode === "query" && showQueryRerun && onRerun && (
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
// Pt 7 — Agent Search tab. Wraps the existing query state that
// GraphViewer's useQueryGraph already manages; the tab just renders
// the input + the result summary chips. Heavy lifting (extracting
// seeds, expanding subgraph, calling discover for synthesis) happens
// in the parent so this component is purely presentational.
// ────────────────────────────────────────────────────────────────────────

interface AgentSearchTabProps {
  query: string;
  onChange: (q: string) => void;
  onRun: (mode?: GraphRunMode) => void;
  onClear?: () => void;
  phase: "idle" | "loading" | "ready" | "error";
  error?: string | null;
  synthesisMarkdown?: string | null;
  progressSteps?: GraphProgressStep[];
  seedNames?: string[];
  sourceNames?: string[];
  bridgeNames?: string[];
  hubNames?: string[];
  gaps?: Array<{ entity_a_name?: string; entity_b_name?: string }>;
  synthesisMode?: GraphSynthesisMode;
  onSynthesisModeChange?: (m: GraphSynthesisMode) => void;
  validateSynthesis?: boolean;
  onValidateSynthesisChange?: (v: boolean) => void;
  followUpAvailable?: boolean;
  followUpPreview?: string;
}

function AgentSearchTab(props: AgentSearchTabProps) {
  const {
    query,
    onChange,
    onRun,
    onClear,
    phase,
    error,
    synthesisMarkdown,
    progressSteps,
    seedNames,
    sourceNames,
    bridgeNames,
    hubNames,
    gaps,
    synthesisMode = "research",
    onSynthesisModeChange,
    validateSynthesis = false,
    onValidateSynthesisChange,
    followUpAvailable = false,
    followUpPreview = "",
  } = props;
  const canRun = phase !== "loading" && query.trim().length > 0;
  const modeOptions: Array<{
    id: GraphSynthesisMode;
    label: string;
    meta: string;
  }> = [
    { id: "research", label: "Research", meta: "evidence" },
    { id: "nuance", label: "Nuance", meta: "tension" },
    { id: "ideation", label: "Ideation", meta: "build" },
    { id: "gap", label: "Gap", meta: "absence" },
  ];
  return (
    <>
      <section className="rounded-lg border border-amber-500/25 bg-[radial-gradient(circle_at_top_left,rgba(245,158,11,0.14),transparent_45%),linear-gradient(135deg,rgba(24,24,27,0.98),rgba(9,9,14,0.98))] p-3 shadow-[0_16px_40px_rgba(0,0,0,0.28)]">
        <div className="mb-3 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <SectionLabel>Query Graph</SectionLabel>
            <div className="mt-1 text-[11px] text-zinc-400">
              Nodes · edges · synthesis
            </div>
          </div>
          <div
            className={
              "shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-technical " +
              (phase === "loading"
                ? "border-amber-500/50 bg-amber-500/10 text-amber-200"
                : phase === "ready"
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                  : phase === "error"
                    ? "border-rose-500/40 bg-rose-500/10 text-rose-200"
                    : "border-zinc-800 bg-zinc-950/70 text-zinc-500")
            }
          >
            {phase === "loading"
              ? "running"
              : phase === "ready"
                ? "ready"
                : phase === "error"
                  ? "error"
                  : "idle"}
          </div>
        </div>
        <GraphProgressNarrator steps={progressSteps} />
        {phase === "ready" && onClear && (
          <BackToBrainButton onClick={onClear} className="mb-3" />
        )}
        <div className="space-y-3">
          {onSynthesisModeChange && (
            <div
              role="radiogroup"
              aria-label="Synthesis mode"
              className="grid grid-cols-2 gap-1.5 rounded-lg border border-zinc-800 bg-zinc-950/75 p-1"
            >
              {modeOptions.map((opt) => {
                const active = synthesisMode === opt.id;
                return (
                  <button
                    key={opt.id}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    onClick={() => onSynthesisModeChange(opt.id)}
                    className={
                      "rounded-md border px-2 py-2 text-left transition-all " +
                      (active
                        ? "border-amber-400/70 bg-amber-500/15 text-amber-100 shadow-[0_0_18px_rgba(245,158,11,0.16)]"
                        : "border-transparent bg-zinc-900/60 text-zinc-500 hover:border-zinc-700 hover:text-zinc-200")
                    }
                  >
                    <span className="block text-[11px] font-semibold">
                      {opt.label}
                    </span>
                    <span
                      className={
                        "mt-0.5 block text-[9px] font-technical " +
                        (active ? "text-amber-300/80" : "text-zinc-600")
                      }
                    >
                      {opt.meta}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
          <textarea
            rows={4}
            value={query}
            onChange={(e) => onChange(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && canRun) {
                e.preventDefault();
                onRun("new");
              }
            }}
            placeholder={
              synthesisMode === "ideation"
                ? "What could I build from this corpus?"
                : synthesisMode === "nuance"
                  ? "Where is the conceptual tension or hidden bridge?"
                  : synthesisMode === "gap"
                    ? "What should this corpus connect but doesn't?"
                    : "What does my library think about...?"
            }
            className="w-full resize-none rounded-md border border-zinc-800 bg-[#09090f] px-3 py-2 text-sm leading-relaxed text-zinc-100 placeholder:text-zinc-600 focus:border-amber-500/70 focus:outline-none focus:ring-1 focus:ring-amber-500/20"
          />
          {phase === "ready" && followUpAvailable ? (
            <div className="grid grid-cols-2 gap-1.5">
              <button
                type="button"
                onClick={() => onRun("new")}
                disabled={!canRun}
                className="flex min-h-10 items-center justify-center gap-1.5 rounded-md border border-sky-500/35 bg-sky-500/10 px-2 py-2 text-xs font-semibold text-sky-100 transition-all hover:border-sky-300/70 hover:bg-sky-500/18 disabled:cursor-not-allowed disabled:border-zinc-800 disabled:bg-zinc-900/60 disabled:text-zinc-600"
                title="Start a clean graph query from the text above"
              >
                <PlusCircle className="h-3.5 w-3.5" /> New
              </button>
              <button
                type="button"
                onClick={() => onRun("followup")}
                disabled={!canRun}
                className="flex min-h-10 items-center justify-center gap-1.5 rounded-md border border-amber-500/45 bg-amber-500/12 px-2 py-2 text-xs font-semibold text-amber-100 transition-all hover:border-amber-300/75 hover:bg-amber-500/22 disabled:cursor-not-allowed disabled:border-zinc-800 disabled:bg-zinc-900/60 disabled:text-zinc-600"
                title={followUpPreview || "Continue from the last graph answer"}
              >
                <GitBranch className="h-3.5 w-3.5" /> Continue
              </button>
            </div>
          ) : (
            <button
              onClick={() => onRun("new")}
              disabled={!canRun}
              aria-busy={phase === "loading"}
              className={
                "flex w-full items-center justify-center gap-2 rounded-md border px-3 py-2 text-xs font-semibold disabled:cursor-not-allowed " +
                (phase === "loading"
                  ? "border-amber-500/60 bg-amber-500/15 text-amber-100"
                  : "border-amber-500/35 bg-amber-500/10 text-amber-100 hover:border-amber-400/70 hover:bg-amber-500/20 disabled:border-zinc-800 disabled:bg-zinc-900/60 disabled:text-zinc-600")
              }
            >
              {phase === "loading" ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Building Graph
                </>
              ) : (
                <>
                  <Zap className="h-3.5 w-3.5" /> Run Graph Query
                </>
              )}
            </button>
          )}
          <div className="min-h-4 text-[10px] text-zinc-500">
            {phase === "loading"
              ? "Building query nodes, edges, and synthesis..."
              : phase === "error"
                ? error || "error"
                : ""}
          </div>
        </div>

        {onValidateSynthesisChange && (
          <label
            className={
              "mt-2 flex items-center gap-2 cursor-pointer select-none text-[10px] font-technical " +
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
              <span>validate · draft - critique - revise</span>
              <span
                className={
                  "px-1 py-px rounded-sm text-[9px] " +
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
                : synthesisMode === "gap"
                  ? "Gap Analysis"
                  : "Synthesis"}
          </SectionLabel>
          <div className="rounded border border-zinc-800 bg-[#0d0d14] px-3 py-3 synthesis-body custom-scroll max-h-[70vh] overflow-y-auto">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {synthesisMarkdown || ""}
            </ReactMarkdown>
          </div>
        </section>
      )}

      {phase === "ready" && (seedNames?.length || 0) > 0 && (
        <ChipList label="Seeds" items={seedNames || []} tone="cyan" />
      )}
      {phase === "ready" && (sourceNames?.length || 0) > 0 && (
        <ChipList label="Files used" items={sourceNames || []} tone="emerald" />
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
          <ul className="flex flex-wrap gap-1.5 font-mono text-[10px]">
            {(gaps || []).slice(0, 8).map((g, i) => (
              <li
                key={i}
                className="max-w-full rounded border border-rose-700/40 bg-rose-500/5 px-1.5 py-0.5 text-rose-200"
                title={`${g.entity_a_name || "?"} ↔ ${g.entity_b_name || "?"}`}
              >
                <span className="inline-block max-w-[9rem] truncate align-bottom">
                  {g.entity_a_name || "?"}
                </span>{" "}
                <span className="text-rose-400">↔</span>{" "}
                <span className="inline-block max-w-[9rem] truncate align-bottom">
                  {g.entity_b_name || "?"}
                </span>
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
  tone: "cyan" | "violet" | "amber" | "emerald";
}) {
  const toneClass =
    tone === "cyan"
      ? "border-cyan-700/40 bg-cyan-500/5 text-cyan-200"
      : tone === "violet"
        ? "border-violet-700/40 bg-violet-500/5 text-violet-200"
        : tone === "emerald"
          ? "border-emerald-700/40 bg-emerald-500/5 text-emerald-200"
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


function BrainQuickGraphCard({
  query,
  onChange,
  onRun,
  phase,
  progressSteps,
  disabled,
}: {
  query: string;
  onChange: (q: string) => void;
  onRun: () => void;
  phase: "idle" | "loading" | "ready" | "error";
  progressSteps?: GraphProgressStep[];
  disabled?: boolean;
}) {
  const canRun = !disabled && phase !== "loading" && query.trim().length > 0;
  return (
    <section className="rounded-lg border border-amber-500/25 bg-[radial-gradient(circle_at_top_left,rgba(245,158,11,0.14),transparent_45%),linear-gradient(135deg,rgba(24,24,27,0.98),rgba(9,9,14,0.98))] p-3 shadow-[0_16px_40px_rgba(0,0,0,0.28)]">
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <SectionLabel>Build Query Graph</SectionLabel>
          <div className="mt-1 text-[11px] text-zinc-400">
            Quick graph from the Brain overview
          </div>
        </div>
        <div
          className={
            "shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-technical " +
            (phase === "loading"
              ? "border-amber-500/50 bg-amber-500/10 text-amber-200"
              : phase === "ready"
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                : phase === "error"
                  ? "border-rose-500/40 bg-rose-500/10 text-rose-200"
                  : "border-zinc-800 bg-zinc-950/70 text-zinc-500")
          }
        >
          {phase === "loading"
            ? "building"
            : phase === "ready"
              ? "ready"
              : phase === "error"
                ? "error"
                : "idle"}
        </div>
      </div>
      <GraphProgressNarrator steps={progressSteps} />
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
        placeholder="Ask the graph: how does X connect to Y across this corpus?"
        disabled={disabled}
        className="w-full resize-none rounded-md border border-zinc-800 bg-[#09090f] px-3 py-2 text-sm leading-relaxed text-zinc-100 placeholder:text-zinc-600 focus:border-amber-500/70 focus:outline-none focus:ring-1 focus:ring-amber-500/20 disabled:opacity-50"
      />
      <button
        onClick={onRun}
        disabled={!canRun}
        aria-busy={phase === "loading"}
        className={
          "mt-2 flex w-full items-center justify-center gap-2 rounded-md border px-3 py-2 text-xs font-semibold disabled:cursor-not-allowed " +
          (phase === "loading"
            ? "border-amber-500/60 bg-amber-500/15 text-amber-100"
            : "border-amber-500/35 bg-amber-500/10 text-amber-100 hover:border-amber-400/70 hover:bg-amber-500/20 disabled:border-zinc-800 disabled:bg-zinc-900/60 disabled:text-zinc-600")
        }
      >
        {phase === "loading" ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Building Graph
          </>
        ) : (
          <>
            <Zap className="h-3.5 w-3.5" /> Build Graph
          </>
        )}
      </button>
      <div className="mt-1 text-[10px] text-zinc-500 font-mono">
        {disabled
          ? "select a corpus first"
          : phase === "loading"
            ? "building query graph + synthesis..."
            : "Ctrl + Enter"}
      </div>
    </section>
  );
}


// ────────────────────────────────────────────────────────────────────────
// Pt 7 — Graph Query tab. Two subsections in one tab:
//   (a) Entity-type search — filter the corpora's entities by type +
//       substring. Results are clickable; clicking sends to chat or
//       just shows the entity name (TODO: highlight on canvas).
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
  onBuildGraph?: (query: string) => void;
  graphProgressSteps?: GraphProgressStep[];
  graphActive?: boolean;
  onClearGraph?: () => void;
}

function GraphQueryTab({
  corpusIds,
  onSendToChat,
  onBuildGraph,
  graphProgressSteps,
  graphActive,
  onClearGraph,
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
  const [contextualQuestions, setContextualQuestions] =
    useState<ContextualQuestions | null>(null);
  const [contextualLoading, setContextualLoading] = useState(false);
  const [contextualError, setContextualError] = useState<string | null>(null);
  const [contextualCached, setContextualCached] = useState<boolean>(false);
  const [contextualSource, setContextualSource] = useState<string | null>(null);
  const [conceptPacket, setConceptPacket] =
    useState<ConceptQuestionPacket | null>(null);
  const [refineCached, setRefineCached] = useState<boolean>(false);
  const [entities, setEntities] = useState<ExtractedEntity[]>([]);
  const [entityTypeFilter, setEntityTypeFilter] = useState<string>("");

  const canRefine =
    !refineLoading && refineQ.trim().length > 0 && corpusIds.length > 0;

  const runRefine = useCallback(async () => {
    if (!canRefine) return;
    const question = refineQ.trim();
    setRefineLoading(true);
    setRefineError(null);
    setContextualQuestions(null);
    setContextualError(null);
    setContextualCached(false);
    setContextualSource(null);
    setConceptPacket(null);
    onBuildGraph?.(question);
    try {
      // refineQuery is idempotent: backend has a 24h Mongo cache keyed by
      // hash(question + corpus_ids + resolved graph model); frontend layers a 5min
      // result cache + in-flight dedupe on top of that. Same question
      // never triggers two LLM calls.
      //
      // Pt 7b: the same response now ALSO carries `entities` extracted
      // from the question via extract_query_entities (pure Cypher, not
      // cached on the backend so it reflects live graph state). One
      // round trip, two outputs.
      const res = await api.refineQuery(question, corpusIds);
      setRefinement(res.result);
      setEntities(res.entities || []);
      setRefineCached(res.cached);
      if (res.error) setRefineError(res.error);
    } catch (e: any) {
      setRefineError(String(e?.message || e));
      return;
    } finally {
      setRefineLoading(false);
    }

    setContextualLoading(true);
    try {
      const res = await api.refineQuery(
        question,
        corpusIds,
        undefined,
        false,
        true,
      );
      setContextualQuestions(res.contextual_questions || null);
      setContextualCached(Boolean(res.contextual_cached));
      setContextualSource(res.contextual_source || null);
      setConceptPacket(res.concept_packet || null);
      if (res.contextual_error) setContextualError(res.contextual_error);
      if (res.entities?.length) setEntities(res.entities);
    } catch (e: any) {
      setContextualError(String(e?.message || e));
    } finally {
      setContextualLoading(false);
    }
  }, [canRefine, refineQ, corpusIds, onBuildGraph]);

  // Filter the surfaced entities by type pill (client-side, no extra fetch).
  const filteredEntities =
    entityTypeFilter === ""
      ? entities
      : entities.filter((e) => e.entity_type === entityTypeFilter);

  // Build the distinct entity-type pill row from what came back.
  const typesSeen = Array.from(
    new Set(entities.map((e) => e.entity_type).filter(Boolean)),
  );
  const contextualFailedWithoutPacket = Boolean(contextualError && !contextualQuestions);
  const usingLocalFallback = contextualSource === "local_fallback";
  const questionBuilderProgressSteps: GraphProgressStep[] = [
    ...(graphProgressSteps || []),
    {
      id: "match-concepts",
      label: "Matching your words to concepts in the corpus.",
      status: refineError
        ? "error"
        : refineLoading
          ? "running"
          : refinement
            ? "done"
            : "pending",
      detail: refineError || undefined,
    },
    {
      id: "collect-neighbors",
      label: "Collecting nearby relationships.",
      status: contextualFailedWithoutPacket
        ? "error"
        : contextualLoading
          ? "running"
          : contextualQuestions
            ? "done"
            : "pending",
      detail: contextualFailedWithoutPacket ? contextualError || undefined : undefined,
    },
    {
      id: "local-questions",
      label: "Making offline questions from the concept map.",
      status: contextualLoading
        ? "running"
        : contextualQuestions
          ? "done"
          : "pending",
    },
    {
      id: "enrich-questions",
      label: "Trying to enrich those questions with the graph model.",
      status: contextualLoading
        ? "running"
        : usingLocalFallback
          ? "error"
          : contextualQuestions
            ? "done"
            : "pending",
      detail: usingLocalFallback
        ? "Question suggestions are local-only because the model/provider rejected the request."
        : undefined,
    },
    {
      id: "local-fallback",
      label: "Using local questions because the model did not answer.",
      status: usingLocalFallback ? "done" : "pending",
    },
  ];

  return (
    <>
      {/* ── A. Question input — THE primary interaction. Drives both
              HyDE refinement and entity extraction in one call. ──── */}
      <section className="rounded-lg border border-amber-500/25 bg-[radial-gradient(circle_at_top_left,rgba(245,158,11,0.14),transparent_45%),linear-gradient(135deg,rgba(24,24,27,0.98),rgba(9,9,14,0.98))] p-3 shadow-[0_16px_40px_rgba(0,0,0,0.28)]">
        <div className="mb-3 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <SectionLabel>Refine Query</SectionLabel>
            <div className="mt-1 text-[11px] text-zinc-400">
              Quick chips · graph-aware buckets
            </div>
          </div>
          <div
            className={
              "shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-technical " +
              (refineLoading || contextualLoading
                ? "border-amber-500/50 bg-amber-500/10 text-amber-200"
                : refinement
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                  : "border-zinc-800 bg-zinc-950/70 text-zinc-500")
            }
          >
            {refineLoading
              ? "quick"
              : contextualLoading
                ? "context"
                : refinement
                  ? "ready"
                  : "idle"}
          </div>
        </div>
        <GraphProgressNarrator steps={questionBuilderProgressSteps} />
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
          placeholder="Type a question. Generate sharper corpus-aware variants."
          className="w-full resize-none rounded-md border border-zinc-800 bg-[#09090f] px-3 py-2 text-sm leading-relaxed text-zinc-100 placeholder:text-zinc-600 focus:border-amber-500/70 focus:outline-none focus:ring-1 focus:ring-amber-500/20"
        />
        <button
          onClick={runRefine}
          disabled={!canRefine}
          aria-busy={refineLoading}
          className={
            "mt-2 flex w-full items-center justify-center gap-2 rounded-md border px-3 py-2 text-xs font-semibold disabled:cursor-not-allowed " +
            (refineLoading
              ? "border-amber-500/60 bg-amber-500/15 text-amber-100"
              : "border-amber-500/35 bg-amber-500/10 text-amber-100 hover:border-amber-400/70 hover:bg-amber-500/20 disabled:border-zinc-800 disabled:bg-zinc-900/60 disabled:text-zinc-600")
          }
        >
        {refineLoading ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Quick Refine
            </>
          ) : (
            <>
              <Sparkles className="h-3.5 w-3.5" /> Run + Build Questions
            </>
          )}
        </button>
        <div className="mt-1 text-[10px] text-zinc-500 font-mono">
          {refineLoading
            ? "building quick phrasings + extracting entities..."
            : contextualLoading
              ? "quick chips ready · building graph-aware buckets..."
            : refineError
              ? refineError
              : contextualSource === "local_fallback"
                ? "local graph questions ready · model enrichment unavailable"
              : contextualError
                ? `context pass: ${contextualError}`
              : refineCached
                ? contextualCached
                  ? "quick + context from cache · entities fresh · Ctrl + Enter"
                  : "quick from cache · context live · Ctrl + Enter"
                : "fresh run · cached 24h · Ctrl + Enter"}
        </div>
        {graphActive && onClearGraph && (
          <BackToBrainButton onClick={onClearGraph} className="mt-2" />
        )}
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

      {(contextualLoading || contextualQuestions) && (
        <ContextualQuestionBuckets
          questions={contextualQuestions}
          loading={contextualLoading}
          cached={contextualCached}
          source={contextualSource}
          conceptPacket={conceptPacket}
          onPick={onSendToChat}
        />
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

function ContextualQuestionBuckets({
  questions,
  loading,
  cached,
  source,
  conceptPacket,
  onPick,
}: {
  questions: ContextualQuestions | null;
  loading: boolean;
  cached: boolean;
  source?: string | null;
  conceptPacket?: ConceptQuestionPacket | null;
  onPick?: (text: string) => void;
}) {
  const buckets: Array<{
    key: keyof ContextualQuestions;
    label: string;
    meta: string;
    className: string;
  }> = [
    {
      key: "rag",
      label: "RAG",
      meta: "retrieve",
      className:
        "border-cyan-700/40 bg-cyan-500/5 text-cyan-100 hover:border-cyan-500/70 hover:bg-cyan-500/15",
    },
    {
      key: "research",
      label: "Research",
      meta: "evidence",
      className:
        "border-amber-700/40 bg-amber-500/5 text-amber-100 hover:border-amber-500/70 hover:bg-amber-500/15",
    },
    {
      key: "nuance",
      label: "Nuance",
      meta: "tension",
      className:
        "border-violet-700/40 bg-violet-500/5 text-violet-100 hover:border-violet-500/70 hover:bg-violet-500/15",
    },
    {
      key: "ideation",
      label: "Ideation",
      meta: "build",
      className:
        "border-emerald-700/40 bg-emerald-500/5 text-emerald-100 hover:border-emerald-500/70 hover:bg-emerald-500/15",
    },
    {
      key: "gap",
      label: "Gap",
      meta: "absence",
      className:
        "border-rose-700/40 bg-rose-500/5 text-rose-100 hover:border-rose-500/70 hover:bg-rose-500/15",
    },
  ];
  const hasAny = buckets.some((b) => (questions?.[b.key]?.length || 0) > 0);

  return (
    <section className="rounded-lg border border-amber-500/25 bg-[radial-gradient(circle_at_top_left,rgba(245,158,11,0.12),transparent_45%),linear-gradient(135deg,rgba(24,24,27,0.96),rgba(9,9,14,0.98))] p-3 shadow-[0_16px_40px_rgba(0,0,0,0.24)]">
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <SectionLabel>Context-Aware Questions</SectionLabel>
          <div className="mt-1 text-[11px] text-zinc-400">
            Corpus entities · graph neighbors · source hints
            {conceptPacket && (
              <span className="text-zinc-500">
                {" "}· {conceptPacket.matched_entities.length} concepts
              </span>
            )}
          </div>
        </div>
        <div
          className={
            "shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-technical " +
            (loading
              ? "border-amber-500/50 bg-amber-500/10 text-amber-200"
              : cached
                ? "border-zinc-700 bg-zinc-950/70 text-zinc-400"
                : "border-emerald-500/40 bg-emerald-500/10 text-emerald-200")
          }
        >
          {loading
            ? "building"
            : source === "local_fallback"
              ? "local"
              : cached
                ? "cached"
                : source || "ready"}
        </div>
      </div>

      {loading && !hasAny && (
        <div className="flex items-center gap-2 rounded-md border border-zinc-800 bg-zinc-950/70 px-3 py-2 text-[11px] text-zinc-400">
          <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-300" />
          generating graph-aware question buckets
        </div>
      )}

      {!loading && !hasAny && (
        <div className="rounded-md border border-zinc-800 bg-zinc-950/70 px-3 py-2 text-[11px] text-zinc-500">
          no graph-aware buckets generated
        </div>
      )}

      {hasAny && (
        <div className="grid gap-2">
          {buckets.map((bucket) => {
            const items = questions?.[bucket.key] || [];
            if (items.length === 0) return null;
            return (
              <div
                key={bucket.key}
                className="rounded-md border border-zinc-800 bg-zinc-950/60 p-2"
              >
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <span className="text-[10px] font-semibold uppercase tracking-widest text-zinc-300">
                    {bucket.label}
                  </span>
                  <span className="font-technical text-[9px] text-zinc-600">
                    {bucket.meta}
                  </span>
                </div>
                <div className="space-y-1.5">
                  {items.map((text, i) => (
                    <button
                      key={`${bucket.key}-${i}-${text.slice(0, 24)}`}
                      onClick={() => onPick?.(text)}
                      disabled={!onPick}
                      className={`group flex w-full items-start gap-2 rounded border px-2 py-1.5 text-left font-mono text-[11px] leading-relaxed transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${bucket.className}`}
                      title={onPick ? "Send to chat" : "Read-only"}
                    >
                      <Send className="mt-0.5 h-3 w-3 shrink-0 opacity-50 group-hover:opacity-100 transition-opacity" />
                      <span className="min-w-0 flex-1 break-words">
                        {text}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
