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
  PanelRightClose,
  PanelRightOpen,
  Pause,
  Play,
  X,
  Zap,
} from "lucide-react";

import type { CacheStatus } from "../../lib/api";
import { cleanBookLabel } from "../../lib/label-utils";

// Sidebar width bounds when expanded. Default mirrors Pt 5 baseline (320).
const SIDEBAR_MIN_W = 240;
const SIDEBAR_MAX_W = 560;
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
      {/* Header strip */}
      <div className="flex items-start justify-between border-b border-zinc-900 px-3 py-2.5">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-mono">
            {mode === "brain" ? "Corpora View" : "Query View"}
          </div>
          <div className="text-xs text-zinc-300 font-mono mt-0.5">
            {corpusIds.length} corpora
            {data && (
              <span className="text-zinc-500">
                {" "}· {data.nodes.length}n · {data.links.length}e
              </span>
            )}
          </div>
        </div>
        <button
          onClick={onToggle}
          className="text-zinc-500 hover:text-zinc-200"
          title="Collapse dashboard"
        >
          <PanelRightClose className="h-4 w-4" />
        </button>
      </div>

      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-4">
        {/* Drill stack — brain mode only */}
        {mode === "brain" && drillStack.length > 0 && (
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

        {/* Selection — when a node is selected.
            Pt 5 polish: long raw filenames distilled to "Title — Author"
            via cleanBookLabel so the selection card doesn't blow out
            the sidebar's column width. Full raw filename kept on the
            element's `title` attribute for tooltip on hover. */}
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

        {/* Cache health — brain mode */}
        {mode === "brain" && cacheWarming.length > 0 && (
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

        {/* Color mode — brain mode */}
        {mode === "brain" && (
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

        {/* Layout controls */}
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
        </section>

        {/* Query mode actions */}
        {mode === "query" && onRerun && (
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

      {/* Footer — close button */}
      {onClose && (
        <div className="border-t border-zinc-900 px-3 py-2.5">
          <button
            onClick={onClose}
            className="flex w-full items-center justify-center gap-2 rounded border border-zinc-800 bg-[#0d0d14] py-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:border-rose-700 hover:text-rose-300"
          >
            <X className="h-3 w-3" /> close viewer
          </button>
        </div>
      )}
    </aside>
  );
}
