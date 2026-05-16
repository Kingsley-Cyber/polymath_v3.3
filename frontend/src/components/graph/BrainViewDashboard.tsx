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
  Loader2,
  PanelRightClose,
  PanelRightOpen,
  Pause,
  Play,
  Send,
  Sparkles,
  X,
  Zap,
} from "lucide-react";

import type {
  CacheStatus,
  ExtractedEntity,
  RefinementResult,
} from "../../lib/api";
import * as api from "../../lib/api";
import { cleanBookLabel } from "../../lib/label-utils";

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
  onAgentRun: () => void;
  agentPhase: "idle" | "loading" | "ready" | "error";
  agentError?: string | null;
  agentSynthesisMarkdown?: string | null;
  agentSeedNames?: string[];
  agentBridgeNames?: string[];
  agentHubNames?: string[];
  agentGaps?: Array<{ entity_a_name?: string; entity_b_name?: string }>;
  // Phase 3 — synthesis-mode selector ("research" | "ideation").
  // Forwarded into the Agent Search tab so the user can flip between
  // concrete-claim research synthesis and [BUILD IDEA] ideation output
  // without leaving the panel. Default "research" preserves existing UX.
  synthesisMode?: "research" | "ideation";
  onSynthesisModeChange?: (m: "research" | "ideation") => void;
  // Sprint #2 — opt-in synthesis validation (draft → critique → revise).
  // Adds 2-3× latency/tokens; surfaced as a small checkbox in the tab.
  validateSynthesis?: boolean;
  onValidateSynthesisChange?: (v: boolean) => void;
  // Pt 7: Graph Query tab — send a refined chip back to the chat
  onSendToChat?: (text: string) => void;
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
    agentSeedNames,
    agentBridgeNames,
    agentHubNames,
    agentGaps,
    synthesisMode,
    onSynthesisModeChange,
    validateSynthesis,
    onValidateSynthesisChange,
    onSendToChat,
    model,
    onRerun,
    onClose,
    selectedDisplay,
    onClearSelection,
    isLayoutRunning,
    startLayout,
    stopLayout,
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
            <SectionLabel>Connections</SectionLabel>
            <ul className="space-y-1 font-mono text-[11px] max-h-48 overflow-auto">
              {(() => {
                const selId = String(selectedDisplay.id);
                const rels = (data.links || []).filter((l: any) => {
                  // Edges arrive either flat (source: "id") or hydrated
                  // (source: {id}) depending on the renderer pass. Handle
                  // both shapes so this works in every code path.
                  const s = String(
                    typeof l.source === "object" ? l.source?.id : l.source,
                  );
                  const t = String(
                    typeof l.target === "object" ? l.target?.id : l.target,
                  );
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
                  const s = String(
                    typeof l.source === "object" ? l.source?.id : l.source,
                  );
                  const t = String(
                    typeof l.target === "object" ? l.target?.id : l.target,
                  );
                  const isSource = s === selId;
                  const nid = isSource ? t : s;
                  const node = (data.nodes || []).find(
                    (n: any) => String(n.id) === String(nid),
                  );
                  const name =
                    node?.display_name ||
                    node?.label ||
                    String(nid).slice(0, 20);
                  return (
                    <li
                      key={`${nid}-${i}`}
                      className="flex items-center gap-2 text-zinc-300"
                    >
                      <span className="text-zinc-500" title={isSource ? "outgoing" : "incoming"}>
                        {isSource ? "→" : "←"}
                      </span>
                      <span className="truncate flex-1" title={name}>
                        {name}
                      </span>
                      {l.predicate && l.predicate !== "bridges_to" && (
                        <span className="text-[10px] px-1 rounded bg-zinc-800 text-zinc-400">
                          {l.predicate}
                        </span>
                      )}
                      {typeof l.weight === "number" && (
                        <span className="text-zinc-600">({l.weight})</span>
                      )}
                    </li>
                  );
                });
              })()}
            </ul>
          </section>
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
          />
        )}

        {/* Pt 7: Graph Query tab content — entity-type search + HyDE refinement. */}
        {activeTab === "graph-query" && (
          <GraphQueryTab
            corpusIds={corpusIds}
            onSendToChat={onSendToChat}
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
  synthesisMode?: "research" | "ideation";
  onSynthesisModeChange?: (m: "research" | "ideation") => void;
  validateSynthesis?: boolean;
  onValidateSynthesisChange?: (v: boolean) => void;
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
              className="inline-flex rounded-full bg-zinc-900 p-0.5 text-[10px] font-mono uppercase tracking-wider"
            >
              <button
                type="button"
                role="radio"
                aria-checked={synthesisMode === "research"}
                onClick={() => onSynthesisModeChange("research")}
                title="Faithful synthesis grounded in evidence."
                className={
                  "px-2 py-0.5 rounded-full transition-colors " +
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
                aria-checked={synthesisMode === "ideation"}
                onClick={() => onSynthesisModeChange("ideation")}
                title="Speculative [BUILD IDEA] output grounded in corpus APIs."
                className={
                  "px-2 py-0.5 rounded-full transition-colors " +
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
          <SectionLabel>Synthesis</SectionLabel>
          <div className="rounded border border-zinc-800 bg-[#0d0d14] px-2.5 py-2 text-xs text-zinc-200 whitespace-pre-wrap leading-relaxed max-h-[40vh] overflow-y-auto">
            {synthesisMarkdown}
          </div>
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
  model?: string;
}

function GraphQueryTab({ corpusIds, onSendToChat, model }: GraphQueryTabProps) {
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
  void model; // marker for the lint that model affects the cache key

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
