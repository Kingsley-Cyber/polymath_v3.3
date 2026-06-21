/**
 * BrainViewDashboard — premium redesign (Pt 7d).
 *
 * Right-rail intelligence panel for the graph workspace. Owns:
 *   • Header (mode, corpora, lane legend, collapse toggle)
 *   • Inspector — selection, evidence, semantic map, drill back
 *   • Composer — synthesis mode, query, run/continue, validate, web
 *   • Output   — synthesis body, seed/source/bridge/hub pills, gaps
 *   • Brain    — drill stack, cache health, color scheme, relation legend,
 *               bridge filters, layout, re-run
 *
 * Lives at flex-shrink:0 next to the sigma canvas. Collapses to a 32px
 * vertical strip via the toggle button so the canvas can reclaim width.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import {
  ArrowRight,
  ChevronLeft,
  CircleDot,
  Compass,
  Database,
  Eye,
  FileText,
  Filter,
  GitBranch,
  Globe2,
  Layers,
  Loader2,
  PanelRightClose,
  PanelRightOpen,
  Pause,
  Play,
  PlusCircle,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  Target,
  Wand2,
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
import {
  laneBg,
  laneBorder,
  laneColor,
  laneText,
} from "../../lib/graph-colors";
import { useQueryModelPoolStore } from "../../stores/queryModelPoolStore";
import type { GraphNodeInsightResponse } from "../../types/chat";
import type { GraphSynthesisMode } from "../../types/discover";

export type DashboardTab = "brain" | "agent";

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

// Sidebar width bounds when expanded.
const SIDEBAR_MIN_W = 280;
const SIDEBAR_MAX_W = 880;
const SIDEBAR_DEFAULT_W = 360;

export type DrillFrame = { docId: string; label: string };

interface SelectedDisplay {
  id: string;
  display_name: string;
  source_corpora: string[];
  source_corpus?: string;
  nodeKind?: string;
  kind?: string;
  entity_type?: string;
  primary_entity_type?: string | null;
  definitional_phrase?: string | null;
  observed_entity_types?: string[] | null;
  canonical_family?: string | null;
  confidence?: number | null;
  mention_count?: number;
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
  colorMode: "entity_type" | "community" | "corpus";
  onColorModeToggle: () => void;

  // Bridge filter knobs (brain mode only)
  minBridgeStrength: number;
  onMinBridgeStrengthChange: (n: number) => void;
  maxBridgesPerBook: number;
  onMaxBridgesPerBookChange: (n: number) => void;

  // Settle-restart-after-drag toggle
  settleAfterDrag: boolean;
  onSettleAfterDragToggle: () => void;

  // Tab + Agent / Graph Query content
  activeTab: DashboardTab;
  onActiveTabChange: (t: DashboardTab) => void;
  agentQuery: string;
  onAgentQueryChange: (q: string) => void;
  onAgentRun: (mode?: GraphRunMode) => void;
  agentPhase: "idle" | "loading" | "ready" | "error";
  agentError?: string | null;
  agentSynthesisMarkdown?: string | null;
  agentProgressSteps?: GraphProgressStep[];
  questionPhase?: "idle" | "loading" | "ready" | "error";
  questionProgressSteps?: GraphProgressStep[];
  agentSeedNames?: string[];
  agentSourceNames?: string[];
  agentBridgeNames?: string[];
  agentHubNames?: string[];
  agentGaps?: Array<{ entity_a_name?: string; entity_b_name?: string }>;
  synthesisMode?: GraphSynthesisMode;
  onSynthesisModeChange?: (m: GraphSynthesisMode) => void;
  validateSynthesis?: boolean;
  onValidateSynthesisChange?: (v: boolean) => void;
  webGroundingEnabled?: boolean;
  onWebGroundingChange?: (v: boolean) => void;
  followUpAvailable?: boolean;
  followUpPreview?: string;
  onSendToChat?: (text: string) => void;
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

// ── Small primitives ─────────────────────────────────────────────────────

function SectionLabel({
  children,
  icon,
  hint,
}: {
  children: React.ReactNode;
  icon?: React.ReactNode;
  hint?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-2 mb-2">
      <div className="flex items-center gap-1.5 text-[9.5px] font-mono uppercase tracking-[0.2em] text-content-tertiary">
        {icon}
        <span>{children}</span>
      </div>
      {hint && <div className="text-[10px] text-content-tertiary">{hint}</div>}
    </div>
  );
}

function Card({
  children,
  accent = false,
  className = "",
}: {
  children: React.ReactNode;
  accent?: boolean;
  className?: string;
}) {
  return (
    <div
      className={`rounded-md border border-border-minimal bg-[var(--bg-raised)] px-3 py-3 ${
        accent ? "border-l-2 border-l-accent-main/60" : ""
      } ${className}`}
    >
      {children}
    </div>
  );
}

function Badge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "accent" | "success" | "error" | "warning" | "info";
}) {
  const toneClass =
    tone === "accent"
      ? "border-accent-main/40 bg-accent-main/10 text-accent-main"
      : tone === "success"
        ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
        : tone === "error"
          ? "border-rose-500/40 bg-rose-500/10 text-rose-300"
          : tone === "warning"
            ? "border-amber-500/40 bg-amber-500/10 text-amber-300"
            : tone === "info"
              ? "border-sky-500/40 bg-sky-500/10 text-sky-300"
              : "border-border-minimal bg-[var(--bg-base)] text-content-tertiary";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[9.5px] font-mono ${toneClass}`}
    >
      {children}
    </span>
  );
}

function LaneChip({
  lane,
  label,
  count,
}: {
  lane: "corpus" | "graph" | "web";
  label: string;
  count?: number;
}) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-widest"
      style={{
        borderColor: laneBorder(lane),
        background: laneBg(lane),
        color: laneText(lane),
      }}
      title={`${label} lane evidence`}
    >
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: laneColor(lane) }}
      />
      {label}
      {typeof count === "number" && count > 0 && (
        <span className="ml-0.5 tabular-nums text-[9px] opacity-80">
          ×{count}
        </span>
      )}
    </span>
  );
}

function PrimaryButton({
  onClick,
  children,
  disabled,
  title,
  active,
}: {
  onClick?: () => void;
  children: React.ReactNode;
  disabled?: boolean;
  title?: string;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`group relative inline-flex w-full cursor-pointer items-center justify-center gap-1.5 overflow-hidden rounded-md border px-3 py-2 text-[12px] font-semibold shadow-[0_8px_24px_-20px_rgba(125,211,252,0.65)] transition-all duration-200 hover:-translate-y-0.5 hover:shadow-[0_14px_34px_-22px_rgba(125,211,252,0.95)] active:translate-y-px active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40 ${
        active
          ? "border-accent-main bg-accent-main/20 text-accent-main"
          : "border-accent-main/55 bg-accent-main/10 text-accent-main hover:border-accent-main hover:bg-accent-main/18"
      }`}
    >
      <span className="pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-200 group-hover:opacity-100 bg-[radial-gradient(circle_at_26%_0%,rgba(255,255,255,0.24),transparent_36%),linear-gradient(110deg,transparent,rgba(125,211,252,0.14),transparent)]" />
      <span className="relative z-10 inline-flex items-center justify-center gap-1.5">
        {children}
      </span>
    </button>
  );
}

function SecondaryButton({
  onClick,
  children,
  disabled,
  title,
}: {
  onClick?: () => void;
  children: React.ReactNode;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="group relative inline-flex cursor-pointer items-center justify-center gap-1.5 overflow-hidden rounded-md border border-border-minimal bg-[var(--bg-base)] px-2.5 py-1.5 text-[11px] font-mono uppercase tracking-[0.16em] text-content-secondary transition-all duration-200 hover:-translate-y-0.5 hover:border-accent-main/70 hover:text-content-primary hover:shadow-[0_10px_28px_-22px_rgba(125,211,252,0.7)] active:translate-y-px active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40"
    >
      <span className="pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-200 group-hover:opacity-100 bg-[linear-gradient(110deg,transparent,rgba(148,163,184,0.12),transparent)]" />
      <span className="relative z-10 inline-flex items-center justify-center gap-1.5">
        {children}
      </span>
    </button>
  );
}

// ── Progress narrator ────────────────────────────────────────────────────

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
    if (status === "done") return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
    if (status === "running") return "border-amber-500/40 bg-amber-500/10 text-amber-300";
    if (status === "error") return "border-rose-500/40 bg-rose-500/10 text-rose-300";
    return "border-border-minimal bg-[var(--bg-base)] text-content-tertiary";
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
    <Card className="mb-3">
      <SectionLabel icon={<Loader2 className="h-3 w-3" />}>Progress</SectionLabel>
      <ol className="space-y-1.5">
        {visible.map((step) => (
          <li
            key={step.id}
            className={`rounded border px-2 py-1.5 text-[11px] leading-relaxed ${statusClass(step.status)}`}
          >
            <div className="flex items-start gap-2">
              <span className="flex h-4 w-4 shrink-0 items-center justify-center font-mono">
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
                <div className="relative h-1 overflow-hidden rounded-full bg-[var(--bg-base)]">
                  <div
                    className="absolute top-0 h-full w-[28%] rounded-full bg-accent-main/80 transition-transform duration-200"
                    style={{ transform: `translateX(${meterLeft(step)})` }}
                  />
                </div>
                <div className="mt-1 font-mono text-[9px] uppercase tracking-[0.18em] text-content-tertiary">
                  running · {elapsedLabel(step)}
                </div>
              </div>
            )}
            {step.detail && (
              <div className="mt-1 pl-5 font-mono text-[10px] text-content-tertiary">
                {step.detail}
              </div>
            )}
          </li>
        ))}
      </ol>
    </Card>
  );
}

// ── Cache helpers ────────────────────────────────────────────────────────

function statusDot(s: string | undefined): string {
  if (s === "ready") return "bg-emerald-400";
  if (s === "warming") return "bg-amber-400 animate-pulse";
  return "bg-error";
}

// ── Graph-model control ──────────────────────────────────────────────────

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
    activeTab === "agent" ? "Graph Synthesis Model" : "Graph Question Model";

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
    <div className="border-b border-border-minimal px-3 py-2">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-[9.5px] font-mono uppercase tracking-[0.18em] text-content-tertiary">
          <Database className="h-3 w-3" />
          {label}
        </span>
        <Badge tone="accent">graph role</Badge>
      </div>
      {modelOptions.length > 0 ? (
        <select
          value={selectedEntryId}
          onChange={(e) => void handleChange(e.currentTarget.value)}
          disabled={saving}
          className="w-full rounded-md border border-border-minimal bg-[var(--bg-base)] px-2 py-1.5 text-xs font-mono text-content-primary outline-none focus:border-accent-main"
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
        <div className="truncate rounded-md border border-border-minimal bg-[var(--bg-base)] px-2 py-1.5 text-xs font-mono text-content-tertiary">
          No models in pool
        </div>
      )}
      <div className="mt-1 min-h-[14px] text-[10px] font-mono text-content-tertiary">
        {saving ? "saving..." : error ? error : "Graph Query #1 and #2 use this role."}
      </div>
    </div>
  );
}

// ── Selection helpers ────────────────────────────────────────────────────

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

// ── Main component ───────────────────────────────────────────────────────

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
    questionPhase = "idle",
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
    webGroundingEnabled,
    onWebGroundingChange,
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

  // Drag-resize state.
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

  // Counts for header strip.
  const corpusCount = corpusIds.length;
  const nodeCount = data?.nodes.length ?? 0;
  const linkCount = data?.links.length ?? 0;
  const insightCount = nodeInsight?.related_entities?.length ?? 0;

  // Lane counts (best-effort from payload + insight).
  const corpusLaneCount = useMemo(() => {
    let n = 0;
    if (data?.links) {
      for (const l of data.links) {
        if (l?.source_corpus || l?.source_corpora?.length) n += 1;
      }
    }
    return n;
  }, [data]);
  const graphLaneCount = useMemo(() => {
    let n = 0;
    if (data?.links) {
      for (const l of data.links) {
        if (l?.predicate === "bridges_to" || l?.dominant_relation_family) n += 1;
      }
    }
    return n;
  }, [data]);

  if (collapsed) {
    return (
      <aside className="z-30 flex h-10 w-full flex-row items-center border-t border-border-minimal bg-[var(--bg-raised)] md:h-full md:w-9 md:flex-col md:border-t-0 md:border-l">
        <button
          onClick={onToggle}
          className="flex h-9 w-9 items-center justify-center text-content-tertiary hover:text-content-primary md:mt-3"
          title="Expand dashboard"
        >
          <PanelRightOpen className="h-4 w-4" />
        </button>
        {isLayoutRunning && (
          <div className="mt-3 h-2 w-2 animate-ping rounded-full bg-accent-main" />
        )}
      </aside>
    );
  }

  return (
    <aside
      className="relative z-30 flex h-[58dvh] w-full shrink-0 flex-col overflow-hidden rounded-t-xl border-t border-border-minimal bg-[var(--bg-raised)] md:h-full md:w-[var(--dashboard-width)] md:rounded-none md:border-t-0 md:border-l"
      style={{ "--dashboard-width": `${width}px` } as CSSProperties}
    >
      {/* Drag handle */}
      <div
        onMouseDown={onResizeStart}
        className="group absolute -left-1 top-0 z-40 hidden h-full w-1.5 cursor-col-resize md:block"
        title="Drag to resize"
      >
        <div className="h-full w-px bg-border-minimal transition-colors group-hover:bg-accent-main" />
      </div>

      {/* Header strip */}
      <div className="flex items-start justify-between gap-2 border-b border-border-minimal px-3 py-2.5">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex items-center gap-1.5 text-[9.5px] font-mono uppercase tracking-[0.2em] text-content-tertiary">
            {mode === "brain" ? (
              <Compass className="h-3 w-3" />
            ) : (
              <Search className="h-3 w-3" />
            )}
            <span>{mode === "brain" ? "Corpora view" : "Query view"}</span>
            {mode === "query" && agentPhase === "loading" && (
              <Loader2 className="h-3 w-3 animate-spin text-accent-main" />
            )}
          </div>
          <div className="flex items-center gap-1.5 truncate font-mono text-[11px] text-content-secondary">
            <span className="tabular-nums text-content-primary">
              {corpusCount} {corpusCount === 1 ? "corpus" : "corpora"}
            </span>
            <span className="text-content-tertiary">·</span>
            <span className="tabular-nums text-content-tertiary">
              {nodeCount}n · {linkCount}e
            </span>
          </div>
        </div>
        <button
          onClick={onToggle}
          className="shrink-0 text-content-tertiary hover:text-content-primary"
          title="Collapse dashboard"
          aria-label="Collapse dashboard"
        >
          <PanelRightClose className="h-4 w-4" />
        </button>
      </div>

      {/* Lane legend strip */}
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border-minimal px-3 py-1.5">
        <span className="text-[9px] font-mono uppercase tracking-[0.18em] text-content-tertiary">
          Evidence lanes
        </span>
        <LaneChip
          lane="corpus"
          label="corpus"
          count={Math.min(corpusLaneCount, 99)}
        />
        <LaneChip
          lane="graph"
          label="graph"
          count={Math.min(graphLaneCount, 99)}
        />
        <LaneChip
          lane="web"
          label="web"
          count={webGroundingEnabled ? 1 : 0}
        />
      </div>

      {/* Tab strip */}
      <div className="flex items-stretch border-b border-border-minimal px-1.5 pt-1.5">
        {([
          { id: "brain", label: "Brain" },
          { id: "agent", label: "Graph Query" },
        ] as const).map((t) => (
          <button
            key={t.id}
            onClick={() => onActiveTabChange(t.id)}
            className={`flex-1 px-2 py-1.5 font-mono text-[10px] uppercase tracking-[0.18em] border-b-2 transition-colors ${
              activeTab === t.id
                ? "border-accent-main bg-accent-main/5 text-accent-main"
                : "border-transparent text-content-tertiary hover:text-content-secondary"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <GraphPathModelControl activeTab={activeTab} />

      {/* Scrollable body */}
      <div className="flex-1 overscroll-contain overflow-y-auto px-3 py-3 space-y-5 custom-scroll">
        {/* ── Inspector ─────────────────────────────────────────────── */}
        {selectedDisplay && (
          <section>
            <SectionLabel icon={<Eye className="h-3 w-3" />}>Inspector</SectionLabel>
            <Card accent>
              <div className="flex items-start gap-2 min-w-0">
                <div className="h-1.5 w-1.5 mt-1.5 shrink-0 rounded-full bg-accent-main" />
                <div
                  className="min-w-0 flex-1 font-mono text-xs text-content-primary break-words"
                  title={selectedDisplay.display_name}
                >
                  {cleanBookLabel(selectedDisplay.display_name) ||
                    selectedDisplay.display_name}
                </div>
              </div>
              {(() => {
                const primaryType =
                  selectedDisplay.primary_entity_type ||
                  selectedDisplay.entity_type ||
                  selectedDisplay.dominant_entity_type ||
                  null;
                const observed = (
                  selectedDisplay.observed_entity_types || []
                ).filter((t) => t && t !== primaryType);
                const conf =
                  typeof selectedDisplay.confidence === "number"
                    ? Math.round(selectedDisplay.confidence * 100)
                    : null;
                const mentions = selectedDisplay.mention_count;
                const meta = [
                  typeof mentions === "number" && mentions > 0
                    ? `${mentions} mention${mentions === 1 ? "" : "s"}`
                    : null,
                  conf != null ? `${conf}% conf` : null,
                ].filter(Boolean);
                return (
                  <div className="mt-2 ml-4 space-y-1.5">
                    {primaryType && <Badge tone="accent">{primaryType}</Badge>}
                    {selectedDisplay.definitional_phrase && (
                      <div className="text-[10px] italic leading-relaxed text-content-secondary">
                        {selectedDisplay.definitional_phrase}
                      </div>
                    )}
                    {(observed.length > 0 ||
                      selectedDisplay.canonical_family) && (
                      <div className="flex flex-wrap gap-1">
                        {observed.slice(0, 3).map((t) => (
                          <span
                            key={t}
                            className="rounded border border-border-minimal bg-[var(--bg-base)] px-1.5 py-0.5 text-[10px] text-content-tertiary"
                          >
                            {t}
                          </span>
                        ))}
                        {selectedDisplay.canonical_family && (
                          <span className="rounded border border-border-minimal bg-[var(--bg-base)] px-1.5 py-0.5 text-[10px] text-content-tertiary">
                            family: {selectedDisplay.canonical_family}
                          </span>
                        )}
                      </div>
                    )}
                    {meta.length > 0 && (
                      <div className="font-mono text-[10px] text-content-tertiary">
                        {meta.join(" · ")}
                      </div>
                    )}
                  </div>
                );
              })()}
              {selectedDisplay.source_corpora.length > 0 && (
                <div className="mt-1 ml-4 text-[10px] text-content-secondary font-mono">
                  {selectedDisplay.source_corpora.length} corpora
                </div>
              )}
              <div className="mt-2 ml-4 flex flex-wrap gap-1">
                {selectionBadgeItems(selectedDisplay).map((item) => (
                  <span
                    key={item}
                    className="rounded border border-border-minimal bg-[var(--bg-base)] px-1.5 py-0.5 text-[10px] text-content-tertiary"
                  >
                    {item}
                  </span>
                ))}
              </div>
              {(selectedDisplay.top_entities?.length || 0) > 0 && (
                <div className="mt-2 ml-4 line-clamp-2 text-[10px] leading-relaxed text-content-tertiary">
                  {selectedDisplay.top_entities?.slice(0, 5).join(" · ")}
                </div>
              )}
              <button
                onClick={onClearSelection}
                className="mt-2 ml-4 rounded px-2 py-0.5 text-[11px] text-content-secondary hover:bg-white/5 hover:text-content-primary"
              >
                Clear
              </button>
            </Card>
          </section>
        )}

        {/* Evidence Inspector */}
        {selectedDisplay && data && (
          <section>
            <SectionLabel icon={<Layers className="h-3 w-3" />}>
              Relationships
            </SectionLabel>
            <ul className="space-y-1.5 font-mono text-[11px] max-h-56 overflow-auto pr-1 custom-scroll">
              {(() => {
                const selId = String(selectedDisplay.id);
                const rels = (data.links || []).filter((l: any) => {
                  const s = endpointId(l.source);
                  const t = endpointId(l.target);
                  return s === selId || t === selId;
                });
                if (rels.length === 0) {
                  return (
                    <li className="text-content-tertiary italic">
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
                      className="rounded border border-border-minimal bg-[var(--bg-base)] px-2 py-1.5 text-content-secondary"
                    >
                      <div className="flex items-center gap-2">
                        <span
                          className="h-2 w-2 shrink-0 rounded-full"
                          style={{ backgroundColor: relationColor(l) }}
                        />
                        <span
                          className="text-content-tertiary"
                          title={isSource ? "outgoing" : "incoming"}
                        >
                          {isSource ? "→" : "←"}
                        </span>
                        <span className="min-w-0 flex-1 truncate" title={name}>
                          {name}
                        </span>
                        <span className="shrink-0 rounded bg-[var(--bg-raised)] px-1 text-[10px] text-content-tertiary">
                          {relationLabel(l)}
                        </span>
                      </div>
                      <div className="mt-1 ml-7 flex flex-wrap items-center gap-1 text-[10px] text-content-tertiary">
                        <span>{family}</span>
                        {typeof l.weight === "number" && (
                          <span>strength {l.weight}</span>
                        )}
                        {typeof l.confidence === "number" && (
                          <span>{Math.round(l.confidence * 100)}% conf</span>
                        )}
                      </div>
                      {shared.length > 0 && (
                        <div className="mt-1 ml-7 truncate text-[10px] text-content-secondary">
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

        {/* Semantic Map */}
        {selectedDisplay && (
          <section>
            <SectionLabel
              icon={<Compass className="h-3 w-3" />}
              hint={
                <Badge
                  tone={
                    nodeInsightPhase === "loading"
                      ? "warning"
                      : nodeInsightPhase === "ready"
                        ? "success"
                        : nodeInsightPhase === "error"
                          ? "error"
                          : "neutral"
                  }
                >
                  {nodeInsightPhase}
                </Badge>
              }
            >
              Semantic map
            </SectionLabel>
            <Card>
              {nodeInsightPhase === "loading" && (
                <div className="text-[10px] text-content-tertiary">
                  searching nearby passages and documents…
                </div>
              )}
              {nodeInsightPhase === "error" && (
                <div className="text-[10px] text-error">
                  {nodeInsightError || "semantic lookup failed"}
                </div>
              )}
              {nodeInsightPhase === "ready" && nodeInsight && (
                <div className="space-y-1.5">
                  <div className="text-[10px] text-content-tertiary">
                    {insightCount} vector neighbor{insightCount === 1 ? "" : "s"}
                  </div>
                  {insightCount > 0 ? (
                    nodeInsight.related_entities.slice(0, 6).map((entity) => {
                      const confidence =
                        typeof entity.confidence === "number"
                          ? Math.round(entity.confidence * 100)
                          : null;
                      return (
                        <div
                          key={`${entity.name}-${entity.predicate || ""}`}
                          className="flex items-center justify-between gap-2 rounded border border-border-minimal bg-[var(--bg-base)] px-2 py-1.5"
                          title={[
                            entity.predicate,
                            entity.relation_family,
                            confidence != null ? `${confidence}%` : "",
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        >
                          <span className="min-w-0 truncate text-[11px] font-semibold text-content-primary">
                            {entity.name}
                          </span>
                          <div className="flex shrink-0 items-center gap-1.5">
                            {entity.count > 1 && (
                              <Badge tone="accent">{entity.count}×</Badge>
                            )}
                            {confidence != null && (
                              <span className="font-mono text-[10px] text-content-tertiary">
                                {confidence}%
                              </span>
                            )}
                          </div>
                        </div>
                      );
                    })
                  ) : (
                    <div className="text-[10px] text-content-tertiary">
                      No entity associations found.
                    </div>
                  )}
                </div>
              )}
            </Card>
          </section>
        )}

        {/* ── Composer / Output (Agent tab) ───────────────────────── */}
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
            webGroundingEnabled={webGroundingEnabled}
            onWebGroundingChange={onWebGroundingChange}
            followUpAvailable={followUpAvailable}
            followUpPreview={followUpPreview}
            onSendToChat={onSendToChat}
          />
        )}

        {/* ── Brain tab: Quick Graph + Refine ─────────────────────── */}
        {activeTab === "brain" && (
          <BrainQuickGraphCard
            query={agentQuery}
            onChange={onAgentQueryChange}
            onRun={() => {
              const normalized = agentQuery.trim();
              if (normalized) onBuildQuestionGraph?.(normalized);
            }}
            phase={questionPhase}
            progressSteps={questionProgressSteps}
            disabled={corpusIds.length === 0}
          />
        )}

        {activeTab === "brain" && (
          <GraphQueryTab
            corpusIds={corpusIds}
            onSendToChat={onSendToChat}
            graphActive={mode === "query"}
            onClearGraph={onClearGraphQuery}
          />
        )}

        {/* ── Brain controls ──────────────────────────────────────── */}
        {activeTab === "brain" && mode === "brain" && drillStack.length > 0 && (
          <section>
            <SectionLabel icon={<Target className="h-3 w-3" />}>Drill stack</SectionLabel>
            <Card>
              <button
                className="block w-full text-left text-[11px] font-mono text-content-secondary hover:text-accent-main transition-colors"
                onClick={() => setDrillStack([])}
              >
                ← Overview
              </button>
              <div className="mt-1.5 space-y-0.5">
                {drillStack.map((f, i) => (
                  <button
                    key={i}
                    className="flex w-full items-center gap-1 truncate text-left text-[11px] font-mono text-content-secondary hover:text-accent-main transition-colors"
                    onClick={() => setDrillStack(drillStack.slice(0, i + 1))}
                    title={f.label}
                  >
                    <ChevronLeft className="h-3 w-3 rotate-180 text-content-tertiary shrink-0" />
                    <span className="truncate">{f.label}</span>
                  </button>
                ))}
              </div>
              {drillStack.length > 1 && (
                <button
                  className="mt-1.5 text-[10px] font-mono text-content-tertiary hover:text-content-secondary"
                  onClick={() => setDrillStack(drillStack.slice(0, -1))}
                >
                  ↩ pop one level
                </button>
              )}
            </Card>
          </section>
        )}

        {activeTab === "brain" && mode === "brain" && cacheWarming.length > 0 && (
          <section>
            <SectionLabel icon={<Database className="h-3 w-3" />}>Cache health</SectionLabel>
            <ul className="space-y-1.5 font-mono text-[11px]">
              {cacheWarming.map((cid) => {
                const s = cacheStatuses[cid];
                const rebuilding = rebuildingIds.has(cid);
                return (
                  <li
                    key={cid}
                    className="flex items-center gap-2 rounded border border-border-minimal bg-[var(--bg-base)] px-2 py-1.5"
                  >
                    <span
                      className={`h-2 w-2 shrink-0 rounded-full ${statusDot(
                        s?.metrics_cache,
                      )}`}
                    />
                    <span className="flex-1 truncate text-content-secondary" title={cid}>
                      {cid.slice(0, 8)}…
                    </span>
                    <button
                      disabled={rebuilding}
                      onClick={() => onRebuild([cid])}
                      className="rounded border border-border-minimal px-1.5 py-0.5 text-[10px] uppercase tracking-widest text-content-secondary hover:border-accent-main hover:text-accent-main disabled:opacity-40 disabled:cursor-not-allowed"
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
            <SectionLabel icon={<Filter className="h-3 w-3" />}>Color scheme</SectionLabel>
            <SecondaryButton onClick={onColorModeToggle}>
              <CircleDot className="h-3 w-3" />
              {colorMode === "entity_type"
                ? "by type"
                : colorMode === "community"
                  ? "by community"
                  : "by corpus"}
            </SecondaryButton>
          </section>
        )}

        {activeTab === "brain" && mode === "brain" && (
          <section>
            <SectionLabel icon={<Layers className="h-3 w-3" />}>Relation legend</SectionLabel>
            <Card>
              <div className="grid grid-cols-1 gap-1.5 min-[420px]:grid-cols-2">
                {RELATION_LEGEND_ENTRIES.map(([family, color]) => (
                  <div
                    key={family}
                    className="flex min-w-0 items-center gap-1.5 text-[10px] font-mono text-content-secondary"
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
            </Card>
          </section>
        )}

        {activeTab === "brain" && mode === "brain" && (
          <section>
            <SectionLabel icon={<Filter className="h-3 w-3" />}>Bridge filters</SectionLabel>
            <Card>
              <div className="space-y-2.5">
                <label className="block">
                  <div className="flex items-center justify-between font-mono text-[10px] uppercase tracking-[0.16em] text-content-tertiary">
                    <span>min strength</span>
                    <span className="text-accent-main tabular-nums">
                      {minBridgeStrength}
                    </span>
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
                    className="mt-1 w-full accent-accent-main"
                  />
                  <div className="mt-0.5 text-[10px] text-content-tertiary font-mono">
                    hide bridges with fewer shared entities
                  </div>
                </label>
                <label className="block">
                  <div className="flex items-center justify-between font-mono text-[10px] uppercase tracking-[0.16em] text-content-tertiary">
                    <span>top-N per book</span>
                    <span className="text-accent-main tabular-nums">
                      {maxBridgesPerBook}
                    </span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={10}
                    step={1}
                    value={maxBridgesPerBook}
                    onChange={(e) =>
                      onMaxBridgesPerBookChange(Number(e.currentTarget.value))
                    }
                    className="mt-1 w-full accent-accent-main"
                  />
                  <div className="mt-0.5 text-[10px] text-content-tertiary font-mono">
                    0 shows document jellyfish only; raise for strongest bridges
                  </div>
                </label>
              </div>
            </Card>
          </section>
        )}

        {activeTab === "brain" && (
          <section>
            <SectionLabel icon={<Wand2 className="h-3 w-3" />}>Layout</SectionLabel>
            <Card>
              <SecondaryButton
                onClick={isLayoutRunning ? stopLayout : startLayout}
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
              </SecondaryButton>
              <label className="mt-2 flex items-center gap-2 font-mono text-[11px] text-content-secondary cursor-pointer">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-accent-main cursor-pointer"
                  checked={settleAfterDrag}
                  onChange={onSettleAfterDragToggle}
                />
                <span>re-settle after drag</span>
              </label>
              <div className="mt-1 ml-5 text-[10px] text-content-tertiary font-mono">
                restart layout briefly after releasing a node
              </div>
            </Card>
          </section>
        )}

        {activeTab === "brain" && mode === "query" && showQueryRerun && onRerun && (
          <section>
            <SectionLabel icon={<Zap className="h-3 w-3" />}>Actions</SectionLabel>
            <SecondaryButton onClick={onRerun}>
              <Zap className="h-3 w-3" /> re-run synthesis
            </SecondaryButton>
          </section>
        )}
      </div>
    </aside>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Agent Search tab — composer + output. The tab owns the four synthesis
// modes, web grounding, validate, and the synthesis body with explicit
// lane pills so the user always knows where evidence came from.
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
  webGroundingEnabled?: boolean;
  onWebGroundingChange?: (v: boolean) => void;
  followUpAvailable?: boolean;
  followUpPreview?: string;
  onSendToChat?: (text: string) => void;
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
    webGroundingEnabled = false,
    onWebGroundingChange,
    followUpAvailable = false,
    onSendToChat,
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
      <Card accent>
        <div className="mb-3 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <SectionLabel icon={<Search className="h-3 w-3" />}>Composer</SectionLabel>
            <div className="text-[11px] text-content-tertiary">
              Nodes · edges · synthesis
            </div>
          </div>
          <Badge
            tone={
              phase === "loading"
                ? "warning"
                : phase === "ready"
                  ? "success"
                  : phase === "error"
                    ? "error"
                    : "neutral"
            }
          >
            {phase === "loading"
              ? "running"
              : phase === "ready"
                ? "ready"
                : phase === "error"
                  ? "error"
                  : "idle"}
          </Badge>
        </div>
        <GraphProgressNarrator steps={progressSteps} />
        {phase === "ready" && onClear && (
          <button
            onClick={onClear}
            className="mb-3 inline-flex w-full items-center justify-between rounded-md border border-border-minimal bg-[var(--bg-base)] px-3 py-1.5 text-[10px] font-mono uppercase tracking-[0.18em] text-content-secondary transition-colors hover:border-content-secondary hover:text-content-primary"
          >
            <span className="flex items-center gap-1.5">
              <ChevronLeft className="h-3 w-3" />
              Back to brain
            </span>
            <span className="h-1.5 w-1.5 rounded-full bg-accent-main" />
          </button>
        )}
        <div className="space-y-3">
          {onSynthesisModeChange && (
            <div
              role="radiogroup"
              aria-label="Synthesis mode"
              className="grid grid-cols-2 gap-1.5"
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
                      "rounded-md border px-2 py-2 text-left transition-colors " +
                      (active
                        ? "border-accent-main bg-accent-main/15 text-accent-main"
                        : "border-border-minimal bg-[var(--bg-base)] text-content-tertiary hover:border-content-secondary hover:text-content-secondary")
                    }
                  >
                    <span className="block text-[11px] font-semibold">
                      {opt.label}
                    </span>
                    <span
                      className={
                        "mt-0.5 block text-[9px] font-mono uppercase tracking-widest " +
                        (active ? "text-accent-main/80" : "text-content-tertiary")
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
            className="w-full resize-none rounded-md border border-border-minimal bg-[var(--bg-base)] px-3 py-2 text-[13px] leading-relaxed text-content-primary placeholder:text-content-tertiary focus:border-accent-main focus:outline-none"
          />
          {phase === "ready" && followUpAvailable ? (
            <div className="grid grid-cols-2 gap-1.5">
              <SecondaryButton onClick={() => onRun("new")} disabled={!canRun}>
                <PlusCircle className="h-3.5 w-3.5" /> New
              </SecondaryButton>
              <PrimaryButton onClick={() => onRun("followup")} disabled={!canRun} active>
                <GitBranch className="h-3.5 w-3.5" /> Continue
              </PrimaryButton>
            </div>
          ) : (
            <PrimaryButton onClick={() => onRun("new")} disabled={!canRun} active={phase === "loading"}>
              {phase === "loading" ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Building Graph
                </>
              ) : (
                <>
                  <Zap className="h-3.5 w-3.5" /> Run Graph Query
                </>
              )}
            </PrimaryButton>
          )}
          <div className="min-h-4 text-[10px] text-content-tertiary font-mono">
            {phase === "loading"
              ? "Building query nodes, edges, and synthesis..."
              : phase === "error"
                ? error || "error"
                : ""}
          </div>
        </div>

        {onWebGroundingChange && (
          <label
            className={
              "mt-3 flex items-center gap-2 cursor-pointer select-none text-[10px] font-mono " +
              (webGroundingEnabled ? "text-sky-300" : "text-content-tertiary hover:text-content-secondary")
            }
            title="Add bounded live-web grounding to the graph synthesis. The graph still leads; web sources are tagged separately and used for current/public context."
          >
            <input
              type="checkbox"
              checked={webGroundingEnabled}
              onChange={(e) => onWebGroundingChange(e.currentTarget.checked)}
              className="accent-sky-500"
            />
            <span className="flex items-center gap-1.5">
              <Globe2 className="h-3 w-3" />
              <span>web grounding · current sources</span>
              <LaneChip lane="web" label="opt-in" />
            </span>
          </label>
        )}

        {onValidateSynthesisChange && (
          <label
            className={
              "mt-2 flex items-center gap-2 cursor-pointer select-none text-[10px] font-mono " +
              (validateSynthesis ? "text-amber-300" : "text-content-tertiary hover:text-content-secondary")
            }
            title="Run a second auditor + editor pass to catch fabricated terms, missing citations, and shell sentences. Costs ~3× the tokens of a normal query (draft + critique + revise calls)."
          >
            <input
              type="checkbox"
              checked={validateSynthesis}
              onChange={(e) => onValidateSynthesisChange(e.currentTarget.checked)}
              className="accent-amber-500"
            />
            <span className="flex items-center gap-1.5">
              <ShieldCheck className="h-3 w-3" />
              <span>validate · draft - critique - revise</span>
              <Badge tone={validateSynthesis ? "warning" : "neutral"}>~3× cost</Badge>
            </span>
          </label>
        )}
      </Card>

      {/* Synthesis output */}
      {phase === "ready" && synthesisMarkdown && (
        <section>
          <SectionLabel icon={<Sparkles className="h-3 w-3" />} hint={
            <LaneChip lane="corpus" label="corpus" />
          }>
            {synthesisMode === "ideation"
              ? "Build idea"
              : synthesisMode === "nuance"
                ? "Nuance"
                : synthesisMode === "gap"
                  ? "Gap analysis"
                  : "Synthesis"}
          </SectionLabel>
          <Card>
            <div className="synthesis-body max-h-[70vh] overflow-y-auto custom-scroll">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {synthesisMarkdown || ""}
              </ReactMarkdown>
            </div>
          </Card>
        </section>
      )}

      {/* Source / seed / bridge / hub chips */}
      {phase === "ready" && (
        <>
          <ChipList label="Seeds" items={seedNames || []} lane="corpus" />
          <ChipList label="Files used" items={sourceNames || []} lane="corpus" />
          <ChipList label="Bridges" items={bridgeNames || []} lane="graph" />
          <ChipList label="Hubs" items={hubNames || []} lane="graph" />

          {onSendToChat && (gaps?.length || 0) > 0 && (
            <section>
              <SectionLabel icon={<Target className="h-3 w-3" />}>Gaps</SectionLabel>
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
      )}
    </>
  );
}

function ChipList({
  label,
  items,
  lane,
  onPick,
}: {
  label: string;
  items: string[];
  lane: "corpus" | "graph" | "web";
  onPick?: (text: string) => void;
}) {
  if (items.length === 0) return null;
  const bg = laneBg(lane);
  const border = laneBorder(lane);
  const text = laneText(lane);
  return (
    <section>
      <SectionLabel hint={<LaneChip lane={lane} label={lane} count={items.length} />}>
        {label}
      </SectionLabel>
      <div className="flex flex-wrap gap-1.5">
        {items.slice(0, 12).map((name, i) => (
          <button
            key={`${name}-${i}`}
            type="button"
            onClick={() => onPick?.(name)}
            disabled={!onPick}
            className="group inline-flex max-w-full items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] hover:opacity-90 disabled:cursor-default"
            style={{
              borderColor: border,
              background: bg,
              color: text,
            }}
            title={onPick ? `Send "${name}" to chat` : name}
          >
            <span className="truncate max-w-[14rem]">{name.length > 28 ? name.slice(0, 28) + "…" : name}</span>
            {onPick && (
              <ArrowRight className="h-2.5 w-2.5 shrink-0 opacity-50 transition-opacity group-hover:opacity-100" />
            )}
          </button>
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
    <Card accent>
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <SectionLabel icon={<Zap className="h-3 w-3" />}>Build query graph</SectionLabel>
          <div className="text-[11px] text-content-tertiary">
            Builds only the graph for this query
          </div>
        </div>
        <Badge
          tone={
            phase === "loading"
              ? "warning"
              : phase === "ready"
                ? "success"
                : phase === "error"
                  ? "error"
                  : "neutral"
          }
        >
          {phase === "loading"
            ? "building"
            : phase === "ready"
              ? "ready"
              : phase === "error"
                ? "error"
                : "idle"}
        </Badge>
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
        className="w-full resize-none rounded-md border border-border-minimal bg-[var(--bg-base)] px-3 py-2 text-[13px] leading-relaxed text-content-primary placeholder:text-content-tertiary focus:border-accent-main focus:outline-none disabled:opacity-50"
      />
      <PrimaryButton onClick={onRun} disabled={!canRun} active={phase === "loading"}>
        {phase === "loading" ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Building Graph
          </>
        ) : (
          <>
            <Zap className="h-3.5 w-3.5" /> Build Graph
          </>
        )}
      </PrimaryButton>
      <div className="mt-1 text-[10px] text-content-tertiary font-mono">
        {disabled
          ? "select a corpus first"
          : phase === "loading"
            ? "building bounded query graph..."
            : "Ctrl + Enter"}
      </div>
    </Card>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Graph Query tab — refine + contextual questions + entity filter.
// Two subsections in one tab:
//   (a) Entity-type search — filter the corpora's entities by type +
//       substring. Results are clickable; clicking sends to chat or
//       just shows the entity name.
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
  graphActive?: boolean;
  onClearGraph?: () => void;
}

function GraphQueryTab({
  corpusIds,
  onSendToChat,
  graphActive,
  onClearGraph,
}: GraphQueryTabProps) {
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
    try {
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
  }, [canRefine, refineQ, corpusIds]);

  const filteredEntities =
    entityTypeFilter === ""
      ? entities
      : entities.filter((e) => e.entity_type === entityTypeFilter);

  const typesSeen = Array.from(
    new Set(entities.map((e) => e.entity_type).filter(Boolean)),
  );
  const contextualFailedWithoutPacket = Boolean(contextualError && !contextualQuestions);
  const usingLocalFallback = contextualSource === "local_fallback";
  const questionBuilderProgressSteps: GraphProgressStep[] = [
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
      <Card accent>
        <div className="mb-3 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <SectionLabel icon={<Wand2 className="h-3 w-3" />}>Refine query</SectionLabel>
            <div className="text-[11px] text-content-tertiary">
              Semantic query builder
            </div>
          </div>
          <Badge
            tone={
              refineLoading || contextualLoading
                ? "warning"
                : refinement
                  ? "success"
                  : "neutral"
            }
          >
            {refineLoading
              ? "quick"
              : contextualLoading
                ? "context"
                : refinement
                  ? "ready"
                  : "idle"}
          </Badge>
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
          className="w-full resize-none rounded-md border border-border-minimal bg-[var(--bg-base)] px-3 py-2 text-[13px] leading-relaxed text-content-primary placeholder:text-content-tertiary focus:border-accent-main focus:outline-none"
        />
        <PrimaryButton onClick={runRefine} disabled={!canRefine} active={refineLoading}>
          {refineLoading ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Quick Refine
            </>
          ) : (
            <>
              <Sparkles className="h-3.5 w-3.5" /> Refine Query
            </>
          )}
        </PrimaryButton>
        <div className="mt-1 text-[10px] text-content-tertiary font-mono">
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
          <button
            onClick={onClearGraph}
            className="mt-2 inline-flex w-full items-center justify-between rounded-md border border-border-minimal bg-[var(--bg-base)] px-3 py-1.5 text-[10px] font-mono uppercase tracking-[0.18em] text-content-secondary transition-colors hover:border-content-secondary hover:text-content-primary"
          >
            <span className="flex items-center gap-1.5">
              <ChevronLeft className="h-3 w-3" />
              Back to brain
            </span>
            <span className="h-1.5 w-1.5 rounded-full bg-accent-main" />
          </button>
        )}
      </Card>

      {refinement && (
        <>
          {refinement.alternative_phrasings.length > 0 && (
            <RefineChips
              label="Alternative phrasings"
              lane="corpus"
              items={refinement.alternative_phrasings}
              onPick={onSendToChat}
            />
          )}
          {refinement.opposing_framings.length > 0 && (
            <RefineChips
              label="Opposing framings"
              lane="graph"
              items={refinement.opposing_framings}
              onPick={onSendToChat}
            />
          )}
          {refinement.related_questions.length > 0 && (
            <RefineChips
              label="Related questions"
              lane="graph"
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

      {entities.length > 0 && (
        <section>
          <SectionLabel
            icon={<FileText className="h-3 w-3" />}
            hint={
              <span className="font-mono text-content-tertiary">
                {filteredEntities.length}/{entities.length}
              </span>
            }
          >
            Entities in your library
          </SectionLabel>
          {typesSeen.length > 1 && (
            <div className="mb-2 flex flex-wrap gap-1">
              <button
                onClick={() => setEntityTypeFilter("")}
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-widest ${
                  entityTypeFilter === ""
                    ? "border-accent-main bg-accent-main/10 text-accent-main"
                    : "border-border-minimal bg-[var(--bg-base)] text-content-tertiary hover:text-content-secondary"
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
                      ? "border-accent-main bg-accent-main/10 text-accent-main"
                      : "border-border-minimal bg-[var(--bg-base)] text-content-tertiary hover:text-content-secondary"
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>
          )}
          <ul className="max-h-72 overflow-y-auto rounded border border-border-minimal custom-scroll">
            {filteredEntities.map((row) => (
              <li key={row.entity_id}>
                <button
                  onClick={() => onSendToChat?.(row.display_name)}
                  className="flex w-full items-center justify-between gap-2 border-b border-border-minimal px-2 py-1 text-left last:border-b-0 hover:bg-[var(--bg-base)]/50"
                  title={`${row.entity_type} · ${row.mention_count} mentions${
                    row.score != null ? ` · score ${row.score.toFixed(1)}` : ""
                  }`}
                >
                  <span className="font-mono text-[11px] text-content-primary truncate">
                    {row.display_name}
                  </span>
                  <span className="shrink-0 font-mono text-[9px] uppercase tracking-widest text-content-tertiary">
                    {row.entity_type}
                  </span>
                </button>
              </li>
            ))}
            {filteredEntities.length === 0 && (
              <li className="px-2 py-2 text-[10px] text-content-tertiary font-mono">
                no entities match this filter
              </li>
            )}
          </ul>
        </section>
      )}

      {!refinement && !refineLoading && (
        <div className="text-[11px] text-content-tertiary font-mono leading-relaxed">
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
  lane,
  items,
  onPick,
}: {
  label: string;
  lane: "corpus" | "graph" | "web";
  items: string[];
  onPick?: (text: string) => void;
}) {
  const border = laneBorder(lane);
  const bg = laneBg(lane);
  const text = laneText(lane);
  return (
    <section>
      <SectionLabel hint={<LaneChip lane={lane} label={lane} count={items.length} />}>
        {label}
      </SectionLabel>
      <ul className="space-y-1.5">
        {items.map((t, i) => (
          <li key={`${i}-${t.slice(0, 24)}`}>
            <button
              onClick={() => onPick?.(t)}
              disabled={!onPick}
              className="group flex w-full items-start gap-2 rounded border px-2 py-1.5 text-left font-mono text-[11px] leading-relaxed transition-colors disabled:cursor-not-allowed disabled:opacity-50 hover:opacity-90"
              style={{ borderColor: border, background: bg, color: text }}
              title={onPick ? "Send to chat" : "Read-only"}
            >
              <Send className="h-3 w-3 shrink-0 mt-0.5 opacity-50 group-hover:opacity-100 transition-opacity" />
              <span className="flex-1 min-w-0 break-words">{t}</span>
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
    lane: "corpus" | "graph" | "web";
  }> = [
    { key: "rag", label: "RAG", meta: "retrieve", lane: "corpus" },
    { key: "research", label: "Research", meta: "evidence", lane: "corpus" },
    { key: "nuance", label: "Nuance", meta: "tension", lane: "graph" },
    { key: "ideation", label: "Ideation", meta: "build", lane: "graph" },
    { key: "gap", label: "Gap", meta: "absence", lane: "graph" },
  ];
  const hasAny = buckets.some((b) => (questions?.[b.key]?.length || 0) > 0);

  return (
    <section>
      <SectionLabel
        icon={<Sparkles className="h-3 w-3" />}
        hint={
          <Badge
            tone={
              loading
                ? "warning"
                : cached
                  ? "neutral"
                  : source === "local_fallback"
                    ? "error"
                    : "success"
            }
          >
            {loading
              ? "building"
              : source === "local_fallback"
                ? "local"
                : cached
                  ? "cached"
                  : source || "ready"}
          </Badge>
        }
      >
        Context-aware questions
      </SectionLabel>
      <Card>
        <div className="mb-3 text-[11px] text-content-tertiary">
          Corpus entities · graph neighbors · source hints
          {conceptPacket && (
            <span className="text-content-tertiary">
              {" "}· {conceptPacket.matched_entities.length} concepts
            </span>
          )}
        </div>
        {loading && !hasAny && (
          <div className="flex items-center gap-2 rounded-md border border-border-minimal bg-[var(--bg-base)] px-3 py-2 text-[11px] text-content-secondary">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-accent-main" />
            generating graph-aware question buckets
          </div>
        )}

        {!loading && !hasAny && (
          <div className="rounded-md border border-border-minimal bg-[var(--bg-base)] px-3 py-2 text-[11px] text-content-tertiary">
            no graph-aware buckets generated
          </div>
        )}

        {hasAny && (
          <div className="grid gap-2">
            {buckets.map((bucket) => {
              const items = questions?.[bucket.key] || [];
              if (items.length === 0) return null;
              const border = laneBorder(bucket.lane);
              const bg = laneBg(bucket.lane);
              const text = laneText(bucket.lane);
              return (
                <div
                  key={bucket.key}
                  className="rounded-md border border-border-minimal bg-[var(--bg-base)] p-2"
                >
                  <div className="mb-1.5 flex items-center justify-between gap-2">
                    <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-content-secondary">
                      {bucket.label}
                      <LaneChip lane={bucket.lane} label={bucket.meta} />
                    </span>
                    <span className="font-mono text-[9px] text-content-tertiary">
                      {items.length}
                    </span>
                  </div>
                  <div className="space-y-1.5">
                    {items.map((t, i) => (
                      <button
                        key={`${bucket.key}-${i}-${t.slice(0, 24)}`}
                        onClick={() => onPick?.(t)}
                        disabled={!onPick}
                        className="group flex w-full items-start gap-2 rounded border px-2 py-1.5 text-left font-mono text-[11px] leading-relaxed transition-colors disabled:cursor-not-allowed disabled:opacity-50 hover:opacity-90"
                        style={{ borderColor: border, background: bg, color: text }}
                        title={onPick ? "Send to chat" : "Read-only"}
                      >
                        <Send className="mt-0.5 h-3 w-3 shrink-0 opacity-50 group-hover:opacity-100 transition-opacity" />
                        <span className="min-w-0 flex-1 break-words">
                          {t}
                        </span>
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </section>
  );
}
