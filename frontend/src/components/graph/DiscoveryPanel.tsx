import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, FileText, FlaskConical, GitBranch, Loader2, MessageSquare, Network, SendHorizontal, Sparkles, Tags, Trash2, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import {
  deleteGraphSession,
  discoverGraph,
  getGraphSession,
  getGraphSuggestions,
  listGraphSessions,
} from "../../lib/api";
import { contextGraphFromDiscoverResponse } from "../../lib/contextGraph";
import { useChatStore } from "../../stores/chatStore";
import { useGraphSessionStore } from "../../stores/graphSessionStore";
import { useSettingsStore } from "../../stores/settingsStore";
import type {
  AutoSynthesisPayload,
  DiscoverMode,
  GraphSourceMetadata,
  GraphDiscoverResponse,
  GraphDiscoverSession,
  SynthesisSource,
} from "../../types";

interface DiscoveryPanelProps {
  corpusId: string | null;
  onClose: () => void;
}

type Phase = "idle" | "curating" | "synthesizing" | "done" | "error";
type MissionSection = "synthesis" | "sources" | "trace";
type ClaimLevel = "observed" | "structure" | "hypothesis" | "action";

type ReceiptFile = {
  doc_id: string;
  source_label: string;
  source?: GraphSourceMetadata;
  chunk_count: number;
  chunk_ids: string[];
  has_temporal?: boolean;
};

type ReceiptChunk = {
  chunk_id: string;
  doc_id: string;
  source_label: string;
  source?: GraphSourceMetadata;
  preview: string;
  has_temporal?: boolean;
};

type EvidenceFilterTrace = NonNullable<GraphDiscoverResponse["trace"]>["evidence_filter"];
type GraphHintTrace = NonNullable<GraphDiscoverResponse["trace"]>["graph_hint"];

const MISSION_CONTEXT_GRAPH_EVENT = "mission-control-context-graph";
const PANEL_WIDTH_STORAGE_KEY = "mission-control-panel-width";
const PANEL_DEFAULT_WIDTH = 672;
const PANEL_MIN_WIDTH = 420;
const PANEL_VIEWPORT_MARGIN = 24;

function panelWidthBounds() {
  if (typeof window === "undefined") {
    return {
      min: PANEL_MIN_WIDTH,
      max: PANEL_DEFAULT_WIDTH,
      preferred: PANEL_DEFAULT_WIDTH,
    };
  }
  const max = Math.max(320, window.innerWidth - PANEL_VIEWPORT_MARGIN);
  const min = Math.min(PANEL_MIN_WIDTH, max);
  return {
    min,
    max,
    preferred: Math.min(PANEL_DEFAULT_WIDTH, max),
  };
}

function clampPanelWidth(value: number) {
  const bounds = panelWidthBounds();
  return Math.min(bounds.max, Math.max(bounds.min, Math.round(value)));
}

function storedPanelWidth() {
  if (typeof window === "undefined") return PANEL_DEFAULT_WIDTH;
  const stored = Number(window.localStorage.getItem(PANEL_WIDTH_STORAGE_KEY));
  if (Number.isFinite(stored) && stored > 0) return clampPanelWidth(stored);
  return panelWidthBounds().preferred;
}

export function DiscoveryPanel({ corpusId, onClose }: DiscoveryPanelProps) {
  const turns = useGraphSessionStore((s) => s.turns);
  const activeSessionId = useGraphSessionStore((s) => s.activeSessionId);
  const sessions = useGraphSessionStore((s) => s.sessions);
  const loading = useGraphSessionStore((s) => s.loading);
  const error = useGraphSessionStore((s) => s.error);
  const draftQuerySeed = useGraphSessionStore((s) => s.draftQuerySeed);
  const nodeNavigation = useGraphSessionStore((s) => s.nodeNavigation);
  const setActiveCorpus = useGraphSessionStore((s) => s.setActiveCorpus);
  const setActiveSession = useGraphSessionStore((s) => s.setActiveSession);
  const setTurns = useGraphSessionStore((s) => s.setTurns);
  const resetTurns = useGraphSessionStore((s) => s.resetTurns);
  const pushTurn = useGraphSessionStore((s) => s.pushTurn);
  const setLoading = useGraphSessionStore((s) => s.setLoading);
  const setError = useGraphSessionStore((s) => s.setError);
  const setSessions = useGraphSessionStore((s) => s.setSessions);
  const clearNodeNavigation = useGraphSessionStore((s) => s.clearNodeNavigation);
  const setPendingPrompt = useChatStore((s) => s.setPendingPrompt);
  const selectedModel = useSettingsStore((s) => s.selectedModel);
  const [query, setQuery] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [loadingSessionId, setLoadingSessionId] = useState<string | null>(null);
  const [requestStartedAt, setRequestStartedAt] = useState<number | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [lastDurationMs, setLastDurationMs] = useState<number | null>(null);
  const [activeQuery, setActiveQuery] = useState("");
  const [panelWidth, setPanelWidth] = useState(storedPanelWidth);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const mainRef = useRef<HTMLElement | null>(null);
  const inFlightRef = useRef(false);
  const wasLoadingRef = useRef(false);
  const seenDraft = useRef<number | null>(null);
  const seenNode = useRef<number | null>(null);
  const panelWidthRef = useRef(panelWidth);
  const latest = turns.length ? turns[turns.length - 1].response : null;

  const refreshSessions = useCallback(async () => {
    if (!corpusId) {
      setSessions([]);
      return;
    }
    try {
      setSessions(await listGraphSessions(corpusId));
    } catch (err) {
      console.warn("Failed to load graph sessions", err);
    }
  }, [corpusId, setSessions]);

  useEffect(() => {
    setActiveCorpus(corpusId);
    setPhase("idle");
    setQuery("");
    void refreshSessions();
  }, [corpusId, refreshSessions, setActiveCorpus]);

  useEffect(() => {
    if (!corpusId) {
      setSuggestions([]);
      return;
    }
    let cancelled = false;
    getGraphSuggestions(corpusId)
      .then((payload) => {
        if (!cancelled) setSuggestions(payload.suggestions.slice(0, 5).map((item) => item.text));
      })
      .catch(() => {
        if (!cancelled) setSuggestions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [corpusId]);

  useEffect(() => {
    if (!draftQuerySeed || draftQuerySeed.nonce === seenDraft.current) return;
    seenDraft.current = draftQuerySeed.nonce;
    setQuery(draftQuerySeed.text);
    window.setTimeout(() => inputRef.current?.focus(), 20);
  }, [draftQuerySeed]);

  useEffect(() => {
    if (!nodeNavigation || nodeNavigation.nonce === seenNode.current) return;
    seenNode.current = nodeNavigation.nonce;
    const targets = nodeNavigation.links.map((link) => sectionForJump(link.section)).filter(Boolean);
    if (targets.length === 1 && targets[0] === "synthesis") scrollToSection("synthesis");
  }, [nodeNavigation]);

  useEffect(() => {
    if (!loading || !requestStartedAt) return;
    const tick = () => setElapsedMs(Date.now() - requestStartedAt);
    tick();
    const timer = window.setInterval(tick, 250);
    return () => window.clearInterval(timer);
  }, [loading, requestStartedAt]);

  // Scroll the panel to the top whenever a new query starts so the running
  // banner is unmissable — even if the user is mid-read on a previous result.
  useEffect(() => {
    if (loading && !wasLoadingRef.current) {
      mainRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    }
    wasLoadingRef.current = loading;
  }, [loading]);

  useEffect(() => {
    panelWidthRef.current = panelWidth;
  }, [panelWidth]);

  useEffect(() => {
    const handleResize = () => setPanelWidth((width) => clampPanelWidth(width));
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  const persistPanelWidth = useCallback((width: number) => {
    window.localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, String(clampPanelWidth(width)));
  }, []);

  const beginPanelResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelWidthRef.current;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (moveEvent: PointerEvent) => {
      const nextWidth = clampPanelWidth(startWidth + startX - moveEvent.clientX);
      panelWidthRef.current = nextWidth;
      setPanelWidth(nextWidth);
    };
    const onUp = () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      persistPanelWidth(panelWidthRef.current);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }, [persistPanelWidth]);

  const resizePanelWithKeyboard = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 96 : 32;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setPanelWidth((width) => {
        const next = clampPanelWidth(width + step);
        panelWidthRef.current = next;
        persistPanelWidth(next);
        return next;
      });
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      setPanelWidth((width) => {
        const next = clampPanelWidth(width - step);
        panelWidthRef.current = next;
        persistPanelWidth(next);
        return next;
      });
    }
  }, [persistPanelWidth]);

  const runDiscover = useCallback(async (rawQuery: string) => {
    const text = rawQuery.trim();
    if (!text) return;
    if (inFlightRef.current) return;
    if (!corpusId) {
      setError("Select a corpus before opening Mission Control.");
      setPhase("error");
      return;
    }
    inFlightRef.current = true;
    setLoading(true);
    setError(null);
    setPhase("curating");
    setActiveQuery(text);
    const startedAt = Date.now();
    setRequestStartedAt(startedAt);
    setElapsedMs(0);
    setLastDurationMs(null);
    const timers = [window.setTimeout(() => setPhase("synthesizing"), 500)];
    try {
      const response = await discoverGraph({
        corpus_id: corpusId,
        query: text,
        mode: "auto",
        session_id: activeSessionId ?? undefined,
        model: selectedModel || undefined,
      });
      setActiveSession(response.session_id);
      pushTurn({
        query: text,
        mode: (response.mode || "auto") as DiscoverMode,
        response,
        createdAt: Date.now(),
      });
      emitContextGraph(response);
      setQuery("");
      setPhase("done");
      setLastDurationMs(Date.now() - startedAt);
      void refreshSessions();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Graph discovery failed.");
      setPhase("error");
      setLastDurationMs(Date.now() - startedAt);
    } finally {
      timers.forEach((timer) => window.clearTimeout(timer));
      inFlightRef.current = false;
      setLoading(false);
      setRequestStartedAt(null);
      setActiveQuery("");
    }
  }, [activeSessionId, corpusId, pushTurn, refreshSessions, selectedModel, setActiveSession, setError, setLoading]);

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void runDiscover(query.trim() || latest?.query || suggestions[0] || "");
  };

  const openSession = useCallback(async (sessionId: string) => {
    setLoadingSessionId(sessionId);
    setError(null);
    try {
      setActiveSession(sessionId);
      const detail = await getGraphSession(sessionId);
      const sessionTurns = detail.turns
          .filter((turn) => turn.response)
          .map((turn) => ({
            query: turn.query,
            mode: (turn.mode || turn.response?.mode || "auto") as DiscoverMode,
            response: turn.response as GraphDiscoverResponse,
            createdAt: Date.parse(turn.created_at) || Date.now(),
          }));
      setTurns(sessionTurns);
      const latestTurn = sessionTurns[sessionTurns.length - 1];
      if (latestTurn) emitContextGraph(latestTurn.response);
      setSessionsOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load graph session.");
    } finally {
      setLoadingSessionId(null);
    }
  }, [setActiveSession, setError, setTurns]);

  const removeSession = useCallback(async (sessionId: string) => {
    try {
      await deleteGraphSession(sessionId);
      if (activeSessionId === sessionId) {
        setActiveSession(null);
        resetTurns();
      }
      await refreshSessions();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete graph session.");
    }
  }, [activeSessionId, refreshSessions, resetTurns, setActiveSession, setError]);

  const newRead = () => {
    setActiveSession(null);
    resetTurns();
    setPhase("idle");
    setError(null);
    window.setTimeout(() => inputRef.current?.focus(), 20);
  };

  const sendLatestToChat = useCallback(() => {
    if (!latest) return;
    setPendingPrompt(chatPromptForGraphRead(latest));
    onClose();
  }, [latest, onClose, setPendingPrompt]);

  const trimmedQuery = query.trim();
  const latestQuery = latest?.query?.trim() || "";
  const suggestedQuery = suggestions[0]?.trim() || "";
  const submitQuery = trimmedQuery || latestQuery || suggestedQuery;
  const submitDisabled = loading || !corpusId || !submitQuery;
  const submitTitle = loading
    ? "Graph synthesis is already running"
    : !corpusId
      ? "Select a corpus before graph synthesis can run"
      : trimmedQuery
        ? "Run graph synthesis"
        : latestQuery
          ? "Run the latest graph query again"
          : suggestedQuery
            ? "Run the top suggested graph query"
          : "Type a graph query first";
  const footerStatus = loading
    ? `sent to /api/graph/discover · ${phase === "synthesizing" ? "synthesizing insight" : "curating graph"}`
    : !corpusId
      ? "select a corpus before graph synthesis can run"
      : !trimmedQuery && latestQuery
        ? "send reruns the latest graph query"
      : !trimmedQuery && suggestedQuery
        ? "send runs the top suggested graph query"
      : !trimmedQuery && lastDurationMs == null
        ? "type a graph question to enable send"
        : lastDurationMs != null
          ? `last graph query finished in ${formatDuration(lastDurationMs)}`
          : "enter sends one graph synthesis request";

  return (
    <aside
      className="relative flex h-full shrink-0 flex-col border-l border-white/10 bg-[#050607] text-neutral-100 shadow-2xl"
      style={{ width: panelWidth }}
    >
      <div
        role="separator"
        aria-label="Resize Mission Control panel"
        aria-orientation="vertical"
        tabIndex={0}
        onPointerDown={beginPanelResize}
        onKeyDown={resizePanelWithKeyboard}
        className="group absolute inset-y-0 left-0 z-30 flex w-3 -translate-x-1/2 cursor-col-resize touch-none items-center justify-center outline-none"
        title="Drag to resize Mission Control"
      >
        <div className="h-16 w-[2px] rounded-full bg-white/10 transition-colors group-hover:bg-amber-200/60 group-focus:bg-amber-200/70" />
      </div>
      {/* Header-level progress strip — full-width, always visible regardless
          of where the inner main is scrolled. Animates only while loading. */}
      {loading && (
        <div className="absolute inset-x-0 top-0 z-40 h-1 overflow-hidden bg-amber-200/10">
          <div className="h-full w-1/3 animate-loading-bar bg-gradient-to-r from-transparent via-amber-300 to-transparent" />
        </div>
      )}
      <header className="border-b border-white/10 bg-[#08090d]/95 px-4 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.28em] text-amber-200/80">
              <Sparkles className="h-3.5 w-3.5" /> Mission Control
              {loading && (
                <span className="ml-2 inline-flex items-center gap-2 rounded-full border border-amber-200/40 bg-amber-200/[0.10] px-2 py-0.5 normal-case tracking-normal">
                  <span className="flex items-center gap-0.5">
                    <span className="h-1 w-1 animate-bounce rounded-full bg-amber-300" style={{ animationDelay: "0ms" }} />
                    <span className="h-1 w-1 animate-bounce rounded-full bg-amber-300" style={{ animationDelay: "150ms" }} />
                    <span className="h-1 w-1 animate-bounce rounded-full bg-amber-300" style={{ animationDelay: "300ms" }} />
                  </span>
                  <span className="font-mono text-[10px] tabular-nums text-amber-100">{(elapsedMs/1000).toFixed(1)}s</span>
                </span>
              )}
            </div>
            <h2 className="mt-1 truncate text-sm font-semibold tracking-wide text-neutral-50">Auto-Synthesis Graph Query</h2>
            <p className="mt-1 text-[11px] leading-relaxed text-neutral-500">
Query-first synthesis. Groups are query-scoped concept/document neighborhoods, not whole-corpus buckets.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-white/10 bg-white/[0.03] p-1.5 text-neutral-400 hover:border-white/20 hover:text-neutral-100"
            aria-label="Close graph view"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button type="button" onClick={newRead} className="rounded border border-white/10 bg-white/[0.03] px-2.5 py-1 text-[10px] uppercase tracking-wider text-neutral-300 hover:border-amber-200/30 hover:text-amber-100">new read</button>
          <button type="button" onClick={() => setSessionsOpen((v) => !v)} className="rounded border border-white/10 bg-white/[0.03] px-2.5 py-1 text-[10px] uppercase tracking-wider text-neutral-400 hover:border-white/20 hover:text-neutral-100">sessions {sessions.length || ""}</button>
          {latest && (
            <button type="button" onClick={sendLatestToChat} className="inline-flex items-center gap-1.5 rounded border border-emerald-300/20 bg-emerald-300/[0.04] px-2.5 py-1 text-[10px] uppercase tracking-wider text-emerald-100/80 hover:border-emerald-200/40 hover:text-emerald-50" title="Copy this graph synthesis into the chat input for review before sending.">
              <MessageSquare className="h-3 w-3" />
              send to chat
            </button>
          )}
        </div>
        <RequestStages phase={phase} loading={loading} hasResponse={!!latest} elapsedMs={elapsedMs} />
      </header>

      {sessionsOpen && (
        <SessionShelf
          sessions={sessions}
          activeSessionId={activeSessionId}
          loadingSessionId={loadingSessionId}
          onOpen={openSession}
          onDelete={removeSession}
        />
      )}

      {nodeNavigation && (
        <div className="border-b border-amber-200/15 bg-amber-200/[0.045] px-4 py-2">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[10px] uppercase tracking-[0.24em] text-amber-100/70">node jump targets</div>
              <div className="mt-0.5 truncate text-[12px] text-neutral-200">{nodeNavigation.entityName}</div>
            </div>
            <button type="button" onClick={clearNodeNavigation} className="text-neutral-600 hover:text-neutral-200" aria-label="Close node jump targets"><X className="h-3.5 w-3.5" /></button>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {nodeNavigation.links.map((link, index) => {
              const section = sectionForJump(link.section);
              if (!section) return null;
              return (
                <button key={`${link.section}:${link.detail}:${index}`} type="button" onClick={() => scrollToSection(section)} className="rounded border border-amber-200/20 bg-black/25 px-2 py-1 text-[10px] text-amber-100/80 hover:border-amber-100/40" title={link.detail}>
                  {link.label || section}
                </button>
              );
            })}
          </div>
        </div>
      )}

      <main ref={mainRef} className="relative min-h-0 flex-1 overflow-y-auto px-4 py-4 custom-scrollbar">
        {loading && <RunningQueryBanner phase={phase} query={activeQuery} elapsedMs={elapsedMs} />}
        {error && <div className="mb-4 rounded border border-red-400/25 bg-red-500/[0.06] px-3 py-2 text-[12px] leading-relaxed text-red-100">{error}</div>}
        {latest ? <MissionRead response={latest} onPickQuery={setQuery} /> : <EmptyState suggestions={suggestions} onPickSuggestion={setQuery} />}
        {loading && <FloatingTimerChip elapsedMs={elapsedMs} phase={phase} />}
      </main>

      <form onSubmit={onSubmit} className="border-t border-white/10 bg-[#07080b] p-3">
        <div className="flex items-center gap-2 rounded-md border border-white/10 bg-black/35 px-2 py-2 transition-colors focus-within:border-amber-200/40">
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            disabled={loading}
            placeholder={corpusId ? "Ask the corpus what structure it sees..." : "Select a corpus first"}
            className="min-w-0 flex-1 bg-transparent px-1 text-[13px] text-neutral-100 placeholder:text-neutral-600 focus:outline-none"
          />
          <button
            type="submit"
            disabled={submitDisabled}
            title={submitTitle}
            aria-label="Run graph synthesis"
            className={cx(
              "group relative inline-flex h-9 min-w-[68px] items-center justify-center gap-1.5 overflow-hidden rounded-md px-3 text-[11px] font-semibold uppercase tracking-[0.16em] transition-all duration-150 ease-out",
              "active:scale-[0.94] active:duration-75",
              loading
                ? "border border-amber-200/60 bg-amber-200/30 text-amber-50 shadow-[0_0_18px_rgba(251,191,36,0.35)]"
                : submitDisabled
                  ? "cursor-not-allowed border border-white/10 bg-white/[0.03] text-neutral-600"
                  : "border border-amber-200/40 bg-amber-200/15 text-amber-100 shadow-[0_0_0_0_rgba(251,191,36,0.0)] hover:border-amber-200/70 hover:bg-amber-200/30 hover:text-amber-50 hover:shadow-[0_0_14px_rgba(251,191,36,0.30)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-200/60"
            )}
          >
            {/* Click ripple — kicks in via :active scale + this brief flash. */}
            {!submitDisabled && !loading && (
              <span aria-hidden className="pointer-events-none absolute inset-0 -translate-x-full bg-gradient-to-r from-transparent via-amber-100/30 to-transparent transition-transform duration-700 ease-out group-hover:translate-x-full" />
            )}
            {loading ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span>thinking</span>
              </>
            ) : (
              <>
                <SendHorizontal className="h-3.5 w-3.5 transition-transform duration-150 group-active:translate-x-0.5" />
                <span>send</span>
              </>
            )}
          </button>
        </div>
        <div className="mt-2 flex items-center justify-between gap-3 font-mono text-[9px] uppercase tracking-wider text-neutral-600">
          <span className={loading ? "text-amber-100/80" : !corpusId ? "text-red-200/80" : "text-neutral-600"}>
            {footerStatus}
          </span>
          {loading && <span className="shrink-0 text-amber-100/70">{formatDuration(elapsedMs)}</span>}
        </div>
      </form>
    </aside>
  );
}

function emitContextGraph(response: GraphDiscoverResponse) {
  const contextGraph = contextGraphFromDiscoverResponse(response);
  if (!contextGraph?.nodes?.length) return;
  window.dispatchEvent(
    new CustomEvent(MISSION_CONTEXT_GRAPH_EVENT, { detail: contextGraph }),
  );
}

function chatPromptForGraphRead(response: GraphDiscoverResponse): string {
  const synthesis = synthesisForResponse(response);
  const files = receiptFiles(response)
    .slice(0, 6)
    .map((file, index) => `- ${readableSourceLabel(file.source_label, index)} (${file.chunk_count || file.chunk_ids?.length || 0} chunks)`)
    .join("\n");
  const sources = synthesis.sources
    .slice(0, 12)
    .map((source) => `[${source.index}] ${source.source_label}${source.snippet ? ` — ${clamp(source.snippet, 160)}` : ""}`)
    .join("\n");
  return [
    "Use this Mission Control graph synthesis as context for my next chat turn. Verify any new claims against retrieval before answering.",
    `Graph query: ${response.query}`,
    `Corpus: ${response.corpus_id}`,
    synthesis.headline ? `Headline: ${synthesis.headline}` : "",
    synthesis.markdown ? `Synthesis:\n${synthesis.markdown}` : "",
    sources ? `Cited sources:\n${sources}` : "",
    files ? `Evidence files sent to the graph LLM:\n${files}` : "",
  ]
    .filter(Boolean)
    .join("\n\n");
}

function RequestStages({ phase, loading, hasResponse, elapsedMs }: { phase: Phase; loading: boolean; hasResponse: boolean; elapsedMs: number }) {
  const steps = [
    { id: "curating", label: "curating graph" },
    { id: "synthesizing", label: "synthesizing insight" },
  ] as const;
  const activeIndex = steps.findIndex((step) => step.id === phase);
  return (
    <div className="mt-3 space-y-1.5">
      <div className="grid gap-1.5 sm:grid-cols-2">
        {steps.map((step, index) => {
          const complete = (!loading && (hasResponse || phase === "done")) || (loading && activeIndex > index);
          const active = loading && activeIndex === index;
          return (
            <div key={step.id} className={cx("flex items-center gap-2 rounded border px-2 py-1.5 text-[10px] uppercase tracking-wider", complete ? "border-emerald-300/20 bg-emerald-300/[0.04] text-emerald-100/80" : active ? "border-amber-200/30 bg-amber-200/[0.06] text-amber-100" : "border-white/10 bg-white/[0.02] text-neutral-600")}>
              {active ? <Loader2 className="h-3 w-3 animate-spin" /> : <span className={cx("h-1.5 w-1.5 rounded-full", complete ? "bg-emerald-300/70" : "bg-neutral-700")} />}
              <span>{step.label}</span>
              {active && <span className="ml-auto text-amber-100/60">{formatDuration(elapsedMs)}</span>}
            </div>
          );
        })}
      </div>
      {loading && <div className="h-[2px] overflow-hidden rounded-full bg-white/10"><div className="h-full w-1/3 animate-pulse bg-amber-200/70" /></div>}
    </div>
  );
}

function FloatingTimerChip({ elapsedMs, phase }: { elapsedMs: number; phase: Phase }) {
  const seconds = (elapsedMs / 1000).toFixed(1);
  const label = phase === "synthesizing" ? "synthesizing" : "curating";
  return (
    <div className="pointer-events-none sticky bottom-3 z-30 ml-auto flex w-fit items-center gap-2 rounded-full border border-amber-200/40 bg-[#0c0d10]/95 px-3 py-1.5 shadow-[0_0_24px_rgba(251,191,36,0.18)] backdrop-blur">
      <div className="flex shrink-0 items-center gap-1">
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-amber-300" style={{ animationDelay: "0ms" }} />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-amber-300" style={{ animationDelay: "150ms" }} />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-amber-300" style={{ animationDelay: "300ms" }} />
      </div>
      <span className="text-[10px] uppercase tracking-[0.2em] text-amber-100/80">{label}</span>
      <span className="font-mono text-[12px] font-semibold tabular-nums text-amber-100">{seconds}s</span>
    </div>
  );
}

function RunningQueryBanner({ phase, query, elapsedMs }: { phase: Phase; query: string; elapsedMs: number }) {
  const label = phase === "synthesizing" ? "Polymath is synthesizing" : "Curating the graph packet";
  const detail = phase === "synthesizing"
    ? "Reading evidence, weaving bridges and contradictions into prose…"
    : "Selecting anchored chunks, edges, and concept neighborhoods…";
  const seconds = (elapsedMs / 1000).toFixed(1);
  return (
    <div className="sticky top-0 z-20 mb-4 overflow-hidden rounded-lg border border-amber-200/30 bg-gradient-to-br from-amber-200/[0.08] via-[#11141b]/95 to-amber-200/[0.03] px-4 py-3 shadow-[0_0_42px_rgba(251,191,36,0.18)]">
      <div className="flex items-center gap-3">
        {/* Three-dot thinking animation, matching the chat bot's loading state. */}
        <div className="flex shrink-0 items-center gap-1.5">
          <span className="h-2 w-2 animate-bounce rounded-full bg-amber-300" style={{ animationDelay: "0ms" }} />
          <span className="h-2 w-2 animate-bounce rounded-full bg-amber-300" style={{ animationDelay: "150ms" }} />
          <span className="h-2 w-2 animate-bounce rounded-full bg-amber-300" style={{ animationDelay: "300ms" }} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-3">
            <div className="text-[12px] font-semibold uppercase tracking-[0.20em] text-amber-100">{label}</div>
            <div className="font-mono text-[14px] font-semibold tabular-nums text-amber-200">{seconds}s</div>
          </div>
          <div className="mt-0.5 text-[11px] leading-relaxed text-amber-100/70">{detail}</div>
        </div>
      </div>
      {query && (
        <div className="mt-2 truncate border-t border-amber-200/20 pt-2 font-mono text-[10px] text-neutral-400">
          <span className="text-amber-200/60">query › </span>{query}
        </div>
      )}
      <div className="mt-2 h-[3px] overflow-hidden rounded-full bg-white/5">
        <div className="h-full w-1/3 animate-[pulse_1.6s_ease-in-out_infinite] rounded-full bg-gradient-to-r from-amber-300/60 via-amber-200 to-amber-300/60" />
      </div>
    </div>
  );
}

function MissionRead({ response, onPickQuery }: { response: GraphDiscoverResponse; onPickQuery: (text: string) => void }) {
  const synthesis = useMemo(() => synthesisForResponse(response), [response]);
  return (
    <article className="space-y-6 text-[13px] leading-6 text-neutral-300">
      <header id="mission-headline" className="space-y-2">
        <h3 className="text-2xl font-semibold leading-tight tracking-tight text-neutral-50">
          {synthesis.headline || "The graph did not have enough evidence for a confident synthesis."}
        </h3>
        {synthesis.fallback && (
          <div className="inline-flex items-center gap-2 rounded border border-amber-200/30 bg-amber-200/[0.08] px-2 py-1 text-[10px] uppercase tracking-wider text-amber-100/85">
            <AlertTriangle className="h-3 w-3" />
            deterministic fallback{synthesis.fallback_reason ? ` · ${synthesis.fallback_reason}` : ""}
          </div>
        )}
      </header>
      <SynthesisProse markdown={synthesis.markdown} sources={synthesis.sources} />
      <SourceReceipts sources={synthesis.sources} />
      <NextQueryHints questions={response.questions || []} onPickQuery={onPickQuery} />
      <DiagnosticsDisclosure response={response} />
    </article>
  );
}

function DiagnosticsDisclosure({ response }: { response: GraphDiscoverResponse }) {
  return (
    <details className="group rounded border border-white/10 bg-white/[0.015] open:bg-white/[0.025]">
      <summary className="flex cursor-pointer items-center gap-2 px-3 py-2 text-[10px] uppercase tracking-[0.24em] text-neutral-500 hover:text-neutral-300">
        <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" />
        diagnostics &amp; trace
        <span className="ml-auto font-mono text-neutral-700">advanced</span>
      </summary>
      <div className="space-y-5 border-t border-white/10 px-3 py-4">
        <ReadStatusStrip response={response} />
        <GroupingBasis response={response} />
        <ClaimLevelGuide response={response} />
        <OntologyLensPanel response={response} />
        <ContextTerminal response={response} />
        <GraphHintPanel response={response} />
        <EvidenceTrace response={response} />
      </div>
    </details>
  );
}

function SynthesisProse({ markdown, sources }: { markdown: string; sources: SynthesisSource[] }) {
  const sourceByIndex = useMemo(() => {
    const map = new Map<number, SynthesisSource>();
    for (const source of sources) map.set(source.index, source);
    return map;
  }, [sources]);
  const stats = useMemo(() => readingStats(markdown), [markdown]);
  if (!markdown.trim()) {
    return (
      <section id="mission-synthesis" className="scroll-mt-20">
        <p className="text-[13px] leading-relaxed text-neutral-500">No synthesis prose was returned for this query.</p>
      </section>
    );
  }
  const annotated = annotateCitations(markdown, sourceByIndex);
  return (
    <section id="mission-synthesis" className="scroll-mt-20">
      <div className="mb-3 flex items-center gap-3 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
        <span className="rounded-full border border-white/10 bg-white/[0.03] px-2 py-0.5">~{stats.minutes} min read</span>
        <span className="rounded-full border border-white/10 bg-white/[0.03] px-2 py-0.5">{stats.paragraphs} sections</span>
        {sources.length > 0 && <span className="rounded-full border border-amber-200/20 bg-amber-200/[0.04] px-2 py-0.5 text-amber-100/70">{sources.length} sources</span>}
      </div>
      <div className="prose prose-invert max-w-none text-[14px] leading-[1.75] text-neutral-200 [&_h2]:mt-6 [&_h2]:mb-3 [&_h2]:text-neutral-50 [&_h2]:text-[17px] [&_h2]:font-semibold [&_h3]:mt-5 [&_h3]:mb-2 [&_h3]:text-neutral-100 [&_h3]:text-[15px] [&_h3]:font-semibold [&_p]:my-5 [&_p]:relative [&_p]:pl-5 [&_p]:before:absolute [&_p]:before:left-0 [&_p]:before:top-3 [&_p]:before:h-1.5 [&_p]:before:w-1.5 [&_p]:before:rounded-full [&_p]:before:bg-amber-200/30 [&_p:first-child]:mt-0 [&_p:first-child:first-letter]:float-left [&_p:first-child:first-letter]:mr-2 [&_p:first-child:first-letter]:text-[34px] [&_p:first-child:first-letter]:font-bold [&_p:first-child:first-letter]:leading-none [&_p:first-child:first-letter]:text-amber-200 [&_p:first-child:first-letter]:pt-1 [&_blockquote]:my-4 [&_blockquote]:border-l-2 [&_blockquote]:border-l-amber-200/40 [&_blockquote]:bg-amber-200/[0.04] [&_blockquote]:px-4 [&_blockquote]:py-2 [&_blockquote]:text-amber-100/90 [&_blockquote]:not-italic [&_strong]:font-semibold [&_strong]:text-amber-100 [&_em]:text-neutral-300 [&_a]:text-amber-200 [&_a]:no-underline hover:[&_a]:text-amber-100 [&_li]:my-1.5 [&_ul]:my-3 [&_ol]:my-3 [&_code]:rounded [&_code]:border [&_code]:border-white/10 [&_code]:bg-white/[0.04] [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:text-[12.5px] [&_hr]:my-6 [&_hr]:border-white/10">
        <ReactMarkdown
          components={{
            a: ({ href, children, ...rest }) => {
              const m = String(href || "").match(/^#cite-(\d+)$/);
              if (m) {
                const idx = Number(m[1]);
                const src = sourceByIndex.get(idx);
                return (
                  <a
                    href={`#mission-source-${idx}`}
                    title={src ? `${src.source_label}${src.snippet ? ` — ${src.snippet}` : ""}` : `Source ${idx}`}
                    onClick={(event) => {
                      event.preventDefault();
                      document.getElementById(`mission-source-${idx}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
                    }}
                    className="ml-0.5 inline-flex items-center align-baseline rounded border border-amber-200/25 bg-amber-200/[0.06] px-1 py-0 text-[10px] font-mono text-amber-100/85 hover:border-amber-100/40 hover:text-amber-50"
                  >
                    {children}
                  </a>
                );
              }
              return <a href={href} {...rest}>{children}</a>;
            },
          }}
        >
          {annotated}
        </ReactMarkdown>
      </div>
    </section>
  );
}

function readingStats(markdown: string): { minutes: number; paragraphs: number; words: number } {
  const text = markdown
    .replace(/\[\d+\]/g, " ")
    .replace(/[#>*_`-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  const words = text ? text.split(/\s+/).length : 0;
  // Average ADHD-friendly read pace: ~220 wpm (slower than the 250 typical
  // benchmark, since this is dense analytical prose, not light reading).
  const minutes = Math.max(1, Math.round(words / 220));
  const paragraphs = markdown.split(/\n{2,}/).filter((p) => p.trim()).length || 1;
  return { minutes, paragraphs, words };
}

function annotateCitations(markdown: string, sourceByIndex: Map<number, SynthesisSource>): string {
  return markdown.replace(/\[(\d{1,3})\]/g, (raw, num: string) => {
    const idx = Number(num);
    if (!sourceByIndex.has(idx)) return raw;
    return `[\\[${num}\\]](#cite-${num})`;
  });
}

function SourceReceipts({ sources }: { sources: SynthesisSource[] }) {
  if (!sources.length) return null;
  return (
    <section id="mission-sources" className="scroll-mt-20 border-t border-white/10 pt-5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="text-[11px] font-semibold uppercase tracking-[0.20em] text-neutral-400">Sources</div>
        <span className="text-[10px] text-neutral-600">{sources.length}</span>
      </div>
      <div className="space-y-2">
        {sources.map((source) => (
          <div key={`source:${source.index}`} id={`mission-source-${source.index}`} className="scroll-mt-20 rounded-md border border-white/10 bg-white/[0.02] px-3 py-2.5 hover:border-white/20">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="rounded border border-amber-200/30 bg-amber-200/[0.08] px-1.5 py-0.5 font-mono text-[10px] font-semibold text-amber-100">[{source.index}]</span>
              <span className="text-[12.5px] font-semibold text-neutral-100">{source.source_label || `Source ${source.index}`}</span>
              {source.chunk_id && <span className="ml-auto font-mono text-[9px] text-neutral-600">{source.chunk_id.slice(0, 12)}</span>}
            </div>
            {source.snippet && <p className="mt-1.5 text-[12px] leading-relaxed text-neutral-400">{source.snippet}</p>}
          </div>
        ))}
      </div>
    </section>
  );
}

function NextQueryHints({ questions, onPickQuery }: { questions: GraphDiscoverResponse["questions"]; onPickQuery: (text: string) => void }) {
  if (!questions || questions.length === 0) return null;
  return (
    <section className="scroll-mt-20 border-t border-white/10 pt-5">
      <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.20em] text-neutral-400">Pull on these threads</div>
      <div className="space-y-2">
        {questions.slice(0, 5).map((q, index) => (
          <button
            key={`${q.text}:${index}`}
            type="button"
            onClick={() => onPickQuery(q.text)}
            className="block w-full rounded-md border border-white/10 bg-white/[0.02] px-3 py-2 text-left text-[12.5px] text-neutral-300 hover:border-amber-200/30 hover:bg-amber-200/[0.04] hover:text-amber-50"
          >
            {q.text}
          </button>
        ))}
      </div>
    </section>
  );
}

function ReadStatusStrip({ response }: { response: GraphDiscoverResponse }) {
  const chunks = receiptChunks(response);
  const filter = response.trace?.evidence_filter;
  const counts = filter ? evidenceFilterCounts(filter, chunks.length) : null;
  const fallbackReason = response.insight_packet_summary?.fallback_reason;
  const sparse = Boolean(response.insight_packet_summary?.sparse || response.trace?.llm_context?.visibility?.sparse);
  const synthesisTone = fallbackReason ? "warn" : "ok";
  const evidenceTone = filter?.all_rejected ? "danger" : counts && counts.accepted === 0 ? "warn" : "ok";
  const graphTone = (response.graph?.nodes?.length || 0) > 0 ? "ok" : "warn";
  return (
    <section className="rounded border border-emerald-300/20 bg-emerald-300/[0.035] px-3 py-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.28em] text-emerald-100/80">
          <CheckCircle2 className="h-3.5 w-3.5" />
          Graph query complete
        </div>
        <span className="font-mono text-[9px] uppercase tracking-wider text-neutral-500">{response.mode || "auto"} synthesis</span>
      </div>
      <div className="grid gap-2 md:grid-cols-4">
        <StatusTile tone="ok" label="request" value="returned" detail="backend responded" />
        <StatusTile tone={evidenceTone} label="evidence" value={counts ? `${counts.accepted}/${counts.raw}` : `${chunks.length}`} detail={filter?.all_rejected ? "all chunks filtered" : "accepted snippets"} />
        <StatusTile tone={graphTone} label="structure" value={`${response.graph?.nodes?.length || 0}`} detail={`${response.graph?.links?.length || 0} visible links`} />
        <StatusTile tone={synthesisTone} label="synthesis" value={fallbackReason ? "fallback" : sparse ? "thin" : "ready"} detail={fallbackReason || (sparse ? "limited packet" : "LLM packet valid")} />
      </div>
    </section>
  );
}

function ClaimLevelGuide({ response }: { response: GraphDiscoverResponse }) {
  const contract = response.trace?.llm_context?.research_contract;
  const levels = contract?.claim_levels?.length
    ? contract.claim_levels
    : ["observed evidence", "graph structure", "testable hypothesis"];
  return (
    <section className="rounded border border-white/10 bg-white/[0.025] px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-[0.28em] text-neutral-500">research contract</div>
          <p className="mt-1 text-[12px] leading-relaxed text-neutral-400">
            {contract?.job || "The backend must turn retrieved evidence and graph structure into grounded research insight."}
          </p>
        </div>
        <span className="shrink-0 rounded border border-white/10 bg-black/20 px-2 py-1 font-mono text-[9px] uppercase tracking-wider text-neutral-600">claim levels</span>
      </div>
      <div className="mt-3 grid gap-2 md:grid-cols-3">
        {levels.slice(0, 3).map((level, index) => {
          const claim = index === 0 ? "observed" : index === 1 ? "structure" : "hypothesis";
          return (
            <div key={level} className="rounded border border-white/10 bg-black/20 px-2 py-2">
              <div className="mb-1"><ClaimBadge level={claim} /></div>
              <div className="text-[11px] leading-relaxed text-neutral-500">{claimLevelDescription(claim)}</div>
            </div>
          );
        })}
      </div>
      {contract?.avoid && <div className="mt-2 text-[10px] leading-relaxed text-neutral-600">avoids: {contract.avoid}</div>}
    </section>
  );
}

function OntologyLensPanel({ response }: { response: GraphDiscoverResponse }) {
  const nodes = response.graph?.nodes || [];
  const links = [
    ...(response.graph?.links || []),
    ...((response.trace?.selected_edges || []) as Array<Record<string, any>>),
  ];
  const domains = rankedValues(nodes.map((node) => node.domain_type || node.domain));
  const objects = rankedValues(nodes.map((node) => node.object_kind));
  const families = rankedValues(nodes.map((node) => node.canonical_family));
  const relations = rankedValues(links.map((link) => String(link.relation_family || link.predicate || "").trim()));
  const hasLens = domains.length || objects.length || families.length || relations.length;
  if (!hasLens) return null;
  return (
    <section className="rounded border border-sky-300/15 bg-sky-300/[0.025] px-3 py-3">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.28em] text-sky-100/75">
            <Tags className="h-3.5 w-3.5" />
            ontology lens
          </div>
          <p className="mt-1 text-[12px] leading-relaxed text-neutral-500">
            These facets came from extracted entities and relations; they help the backend group concepts before synthesis.
          </p>
        </div>
        <span className="shrink-0 rounded border border-sky-200/15 bg-black/20 px-2 py-1 font-mono text-[9px] uppercase tracking-wider text-neutral-600">facets</span>
      </div>
      <div className="grid gap-2 md:grid-cols-4">
        <FacetColumn title="domain type" values={domains} />
        <FacetColumn title="object kind" values={objects} />
        <FacetColumn title="canonical family" values={families} />
        <FacetColumn title="relation family" values={relations} />
      </div>
    </section>
  );
}

function GroupingBasis({ response }: { response: GraphDiscoverResponse }) {
  const context = contextGraphFromDiscoverResponse(response);
  const meta = context?.meta || {};
  const files = receiptFiles(response);
  const groups = context?.nodes.filter((node) => node.kind === "topic") || [];
  const docs = context?.nodes.filter((node) => node.kind === "document") || [];
  const basis = String(
    meta.grouping_basis ||
      "Only concepts and files surfaced by this query are grouped; the whole corpus is not pre-bucketed for this view."
  );
  return (
    <section className="rounded border border-amber-200/20 bg-amber-200/[0.045] px-3 py-3">
      <div className="text-[10px] uppercase tracking-[0.28em] text-amber-100/80">how the backend scoped this query</div>
      <p className="mt-2 text-[13px] leading-6 text-neutral-300">{basis}</p>
      <div className="mt-3 grid grid-cols-3 gap-2 font-mono text-[10px]">
        <div className="rounded border border-white/10 bg-black/25 px-2 py-1.5"><div className="text-neutral-200">{groups.length}</div><div className="uppercase tracking-wider text-neutral-600">query groups</div></div>
        <div className="rounded border border-white/10 bg-black/25 px-2 py-1.5"><div className="text-neutral-200">{docs.length || files.length}</div><div className="uppercase tracking-wider text-neutral-600">unique files</div></div>
        <div className="rounded border border-white/10 bg-black/25 px-2 py-1.5"><div className="text-neutral-200">{response.graph.nodes.length}</div><div className="uppercase tracking-wider text-neutral-600">concept nodes</div></div>
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-neutral-500">
        This is the backend's scoped working set for the query, not a whole-corpus ranking.
      </p>
    </section>
  );
}

function GraphHintPanel({ response }: { response: GraphDiscoverResponse }) {
  const hint = graphHintForResponse(response);
  if (!hint) return null;
  const shape = hint.shape || {};
  const gateways = (hint.gateways || []).filter((gateway) => gateway?.name).slice(0, 4);
  const gaps = (hint.gap_depths || []).filter((gap) => gap?.question).slice(0, 3);
  const statements = (hint.supporting_statements || []).filter((item) => item?.statement).slice(0, 2);

  return (
    <section className="rounded border border-white/10 bg-white/[0.025] px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-[0.28em] text-neutral-500">structural hint sent to synthesis</div>
          <div className="mt-1 text-[13px] font-semibold text-neutral-100">{shape.label || "query-scoped read"}</div>
        </div>
        <span className="shrink-0 rounded border border-white/10 bg-black/20 px-2 py-1 font-mono text-[9px] uppercase tracking-wider text-neutral-600">graph hint</span>
      </div>
      {(hint.context_hint || shape.description) && (
        <p className="mt-2 text-[12px] leading-relaxed text-neutral-400">{hint.context_hint || shape.description}</p>
      )}
      {gateways.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {gateways.map((gateway) => (
            <span key={gateway.id || gateway.name} className="rounded border border-emerald-300/15 bg-emerald-300/[0.035] px-2 py-1 text-[10px] text-emerald-100/75" title={gateway.reason || gateway.connects?.join(" · ")}>
              {gateway.name}
            </span>
          ))}
        </div>
      )}
      {gaps.length > 0 && (
        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          {gaps.map((gap) => (
            <div key={gap.id || gap.question} className="rounded border border-white/10 bg-black/20 px-2 py-1.5">
              <div className="text-[9px] uppercase tracking-wider text-neutral-600">{gap.depth || "gap"}</div>
              <div className="mt-1 text-[10px] leading-snug text-neutral-400">{clamp(gap.question || "", 120)}</div>
            </div>
          ))}
        </div>
      )}
      {statements.length > 0 && (
        <div className="mt-3 border-t border-white/10 pt-2">
          <div className="mb-1 text-[9px] uppercase tracking-[0.22em] text-neutral-600">supporting statements</div>
          <div className="space-y-1">
            {statements.map((statement) => (
              <div key={`${statement.evidence_id || statement.source_label}:${statement.statement}`} className="text-[10px] leading-relaxed text-neutral-500">
                <span className="text-neutral-400">{statement.evidence_id || statement.source_label || "evidence"}</span> {statement.statement}
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}


function ContextTerminal({ response }: { response: GraphDiscoverResponse }) {
  const [collapsed, setCollapsed] = useState(true);
  const files = receiptFiles(response);
  const chunks = receiptChunks(response);
  const filter = response.trace?.evidence_filter;
  const filterCounts = filter ? evidenceFilterCounts(filter, chunks.length) : null;
  const receiptSummary = [
    files.length ? `${files.length} file${files.length === 1 ? "" : "s"}` : "no file receipt",
    `${chunks.length} snippet${chunks.length === 1 ? "" : "s"}`,
    filterCounts ? `${filterCounts.accepted} of ${filterCounts.raw} accepted` : "",
  ].filter(Boolean).join(" · ");
  const requestLines = [
    `POST /api/graph/discover`,
    `query="${clamp(response.query, 120)}"`,
    `corpus_id=${response.corpus_id}`,
    `mode=${response.mode || "auto"}`,
    response.session_id ? `session_id=${response.session_id}` : "session_id=new",
  ];

  useEffect(() => {
    setCollapsed(true);
  }, [response]);

  return (
    <section className="rounded border border-emerald-300/15 bg-[#030504] p-3 font-mono shadow-[0_0_48px_rgba(0,0,0,0.35)]">
      <button
        type="button"
        onClick={() => setCollapsed((value) => !value)}
        className="mb-3 flex w-full items-center justify-between gap-3 text-left"
        aria-expanded={!collapsed}
      >
        <div>
          <div className="text-[9px] uppercase tracking-[0.3em] text-emerald-200/80">backend request</div>
          <div className="mt-1 text-[10px] text-neutral-600">{collapsed ? receiptSummary : "$ graph.discover --request"}</div>
        </div>
        <span className="inline-flex items-center gap-1 text-[9px] uppercase tracking-[0.2em] text-neutral-600">
          {collapsed ? "show" : "hide"}
          {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        </span>
      </button>
      {!collapsed && (
        <>
          <div className="space-y-1 text-[10px] leading-relaxed text-neutral-500">
            <TerminalLine command="endpoint" text={requestLines[0]} />
            <TerminalLine command="payload" text={requestLines.slice(1, 3).join(" | ")} />
            <TerminalLine command="options" text={requestLines.slice(3).join(" | ")} />
          </div>
          <div className="mt-4 border-t border-white/10 pt-3">
            <div className="mb-2 text-[9px] uppercase tracking-[0.24em] text-emerald-200/70">backend synthesis receipt</div>
            {files.length ? (
              <div className="space-y-1.5">
                {files.slice(0, 8).map((file, index) => (
                  <div key={`${file.doc_id}:${file.source_label}:${index}`} className="flex items-start justify-between gap-3 text-[10px]">
                    <span className="min-w-0">
                      <span className="block truncate text-neutral-300">{readableSourceLabel(sourceDisplayLabel(file, index), index)}</span>
                      {formatSourceMeta(file.source) && <span className="mt-0.5 block truncate text-[9px] text-neutral-600">{formatSourceMeta(file.source)}</span>}
                    </span>
                    <span className="shrink-0 text-neutral-600">{file.has_temporal ? "ordered" : "source"}</span>
                  </div>
                ))}
                {files.length > 8 && <div className="text-[10px] text-neutral-700">+{files.length - 8} more files in trace</div>}
              </div>
            ) : <div className="text-[10px] text-neutral-700">The browser did not send files in this request, and the backend did not return a file-level synthesis receipt.</div>}
          </div>
          <div className="mt-4 border-t border-white/10 pt-3">
            <div className="mb-2 flex items-center justify-between gap-3 text-[9px] uppercase tracking-[0.24em] text-emerald-200/70">
              <span>source snippets returned by backend</span>
              {filterCounts && <span className={filter?.all_rejected ? "text-red-200/80" : "text-neutral-600"}>{filterCounts.accepted} of {filterCounts.raw} accepted · {filterCounts.rejected} dropped</span>}
            </div>
            {chunks.length ? (
              <div className="space-y-2">
                {chunks.slice(0, 5).map((chunk) => (
                  <div key={`${chunk.doc_id}:${chunk.chunk_id}`} className="rounded border border-white/10 bg-white/[0.025] px-2 py-1.5">
                    <div className="flex items-center justify-between gap-2 text-[9px] uppercase tracking-wider text-neutral-600"><span className="truncate">{sourceDisplayLabel(chunk, 0)}</span><span className="shrink-0">{chunk.chunk_id}</span></div>
                    {formatSourceMeta(chunk.source) && <div className="mt-1 truncate text-[9px] text-neutral-600">{formatSourceMeta(chunk.source)}</div>}
                    <div className="mt-1 text-[10px] leading-relaxed text-neutral-400">{chunk.preview || "No preview text returned."}</div>
                  </div>
                ))}
                {chunks.length > 5 && <div className="text-[10px] text-neutral-700">+{chunks.length - 5} more chunks in Evidence / Trace</div>}
              </div>
            ) : <div className="text-[10px] text-neutral-700">The backend did not expose snippet previews in this response trace.</div>}
          </div>
        </>
      )}
    </section>
  );
}

function EvidenceTrace({ response }: { response: GraphDiscoverResponse }) {
  const files = receiptFiles(response);
  const chunks = receiptChunks(response);
  const context = contextGraphFromDiscoverResponse(response);
  const llm = response.trace?.llm_context;
  const retrieval = response.trace?.retrieval_evidence;
  const filter = response.trace?.evidence_filter;
  const filterCounts = filter ? evidenceFilterCounts(filter, chunks.length) : null;
  const rejectionReasons = Object.entries(filter?.rejection_reasons || {}).filter(([, count]) => Number(count) > 0);
  const hasRetrievalFilter = Boolean(retrieval || filter);
  const collections = Object.entries(llm?.collections || {}).map(([kind, name]) => `${kind} :: ${name}`);
  const promptRows = llm?.prompt
    ? [
        `system :: ${llm.prompt.system_chars ?? 0} chars`,
        `user :: ${llm.prompt.user_chars ?? 0} chars`,
        `estimate :: ${llm.prompt.estimated_tokens ?? 0} tokens`,
        llm.prompt.preview ? `preview :: ${clamp(llm.prompt.preview, 420)}` : "",
      ].filter(Boolean)
    : [];
  return (
    <section id="mission-trace" className="scroll-mt-20 pb-6">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h4 className="text-[11px] font-semibold uppercase tracking-[0.28em] text-neutral-400">Evidence / Trace</h4>
          <ClaimBadge level="observed" />
        </div>
        <span className="text-[10px] text-neutral-700">advanced</span>
      </div>
      {hasRetrievalFilter && (
        <div className="mb-3 rounded border border-white/10 bg-black/20 p-3 font-mono">
          <div className="mb-2 text-[9px] uppercase tracking-[0.24em] text-neutral-500">retrieval & filter</div>
          <div className="space-y-1.5">
            {retrieval && (
              <div className="flex flex-wrap items-center gap-1.5 rounded border border-white/10 bg-white/[0.025] px-2 py-1.5">
                <span className="mr-1 text-[9px] uppercase tracking-wider text-neutral-600">retrieval</span>
                <TraceChip label={`source: ${retrieval.source || "unknown"}`} />
                <TraceChip label={`effective_tier: ${retrieval.effective_tier || retrieval.requested_tier || "unknown"}`} />
                {retrieval.final_top_k !== undefined && <TraceChip label={`cap=${retrieval.final_top_k}`} />}
                {retrieval.hydrated_chunks !== undefined && <TraceChip label={`hydrated=${retrieval.hydrated_chunks}`} />}
                {retrieval.downgrade_reason && <TraceChip tone="warn" label={`tier downgraded: ${clamp(retrieval.downgrade_reason, 90)}`} />}
                {retrieval.error && <TraceChip tone="danger" label={`error: ${clamp(retrieval.error, 90)}`} />}
              </div>
            )}
            {filter && filterCounts && (
              <div className={cx("flex flex-wrap items-center gap-1.5 rounded border px-2 py-1.5", filter.all_rejected ? "border-red-300/25 bg-red-500/[0.06]" : "border-white/10 bg-white/[0.025]")}>
                <span className="mr-1 text-[9px] uppercase tracking-wider text-neutral-600">filter</span>
                <span className={cx("text-[10px]", filter.all_rejected ? "text-red-100/80" : "text-neutral-300")}>{filterCounts.accepted} of {filterCounts.raw} passed</span>
                {filter.all_rejected && <TraceChip tone="danger" label="all rejected" />}
                {rejectionReasons.map(([reason, count]) => (
                  <TraceChip key={reason} label={`${reason}:${count}`} tone={filter.all_rejected ? "danger" : "neutral"} />
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      <details className="rounded border border-white/10 bg-black/20 p-3">
        <summary className="cursor-pointer text-[10px] uppercase tracking-[0.24em] text-neutral-500">packet internals and raw trace</summary>
        <div className="mt-3 space-y-4 font-mono text-[10px] leading-relaxed text-neutral-500">
          {!!response.trace?.stages?.length && <TraceBlock title="ordered stages" rows={response.trace.stages.map((stage) => `${stage.label || stage.stage} :: ${stage.count} :: ${stage.detail}`)} />}
          <TraceBlock title="collections" rows={collections} />
          <TraceBlock title="prompt" rows={promptRows} />
          <TraceBlock title="files" rows={files.map((file, index) => `${sourceDisplayLabel(file, index)} :: ${formatSourceMeta(file.source) || "no source metadata"} :: ${file.chunk_count} chunks :: ${file.doc_id}`)} />
          <TraceBlock title="chunks" rows={chunks.map((chunk, index) => `${sourceDisplayLabel(chunk, index)} :: ${formatSourceMeta(chunk.source) || "no source metadata"} :: ${chunk.chunk_id} :: ${clamp(chunk.preview, 180)}`)} />
          <TraceBlock title="relationships for review" rows={(response.weak_links || []).map((link) => `${link.source_name} -> ${link.target_name} :: ${link.weakness_type} :: ${link.rationale}`)} />
          <TraceBlock title="suggested gap edges" rows={(context?.links || []).filter((link) => link.suggested).map((link) => `${link.source} -> ${link.target} :: suggested :: ${link.evidence || "no evidence label"}`)} />
          <pre className="max-h-72 overflow-auto rounded border border-white/10 bg-black/35 p-2 text-[9px] text-neutral-600 custom-scrollbar">{JSON.stringify({ insight_packet_summary: response.insight_packet_summary, trace_counts: response.trace?.llm_context?.counts, visibility: response.trace?.llm_context?.visibility }, null, 2)}</pre>
        </div>
      </details>
    </section>
  );
}

function TraceBlock({ title, rows }: { title: string; rows: string[] }) {
  return <div><div className="mb-1 text-neutral-400">{title}</div>{rows.length ? <div className="space-y-1">{rows.slice(0, 12).map((row, index) => <div key={`${title}:${index}`} className="border-t border-white/5 pt-1">{row}</div>)}{rows.length > 12 && <div className="text-neutral-700">+{rows.length - 12} more</div>}</div> : <div className="text-neutral-700">none returned</div>}</div>;
}

function TraceChip({ label, tone = "neutral" }: { label: string; tone?: "neutral" | "warn" | "danger" }) {
  return (
    <span className={cx(
      "rounded border px-1.5 py-0.5 text-[9px]",
      tone === "warn" && "border-amber-200/25 bg-amber-200/[0.06] text-amber-100/75",
      tone === "danger" && "border-red-300/30 bg-red-500/[0.08] text-red-100/80",
      tone === "neutral" && "border-white/10 bg-white/[0.03] text-neutral-500"
    )}>
      {label}
    </span>
  );
}

function StatusTile({ tone, label, value, detail }: { tone: "ok" | "warn" | "danger"; label: string; value: string; detail: string }) {
  const Icon = tone === "ok" ? CheckCircle2 : tone === "warn" ? AlertTriangle : AlertTriangle;
  return (
    <div className={cx(
      "rounded border px-2 py-2",
      tone === "ok" && "border-emerald-300/15 bg-black/20",
      tone === "warn" && "border-amber-200/25 bg-amber-200/[0.045]",
      tone === "danger" && "border-red-300/25 bg-red-500/[0.055]",
    )}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[9px] uppercase tracking-[0.22em] text-neutral-600">{label}</span>
        <Icon className={cx("h-3.5 w-3.5", tone === "ok" && "text-emerald-200/70", tone === "warn" && "text-amber-100/80", tone === "danger" && "text-red-100/80")} />
      </div>
      <div className="mt-1 font-mono text-[13px] text-neutral-100">{value}</div>
      <div className="mt-0.5 text-[10px] text-neutral-600">{detail}</div>
    </div>
  );
}

function ClaimBadge({ level }: { level: ClaimLevel }) {
  const config = claimConfig(level);
  const Icon = config.icon;
  return (
    <span className={cx("inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[9px] uppercase tracking-wider", config.className)} title={config.description}>
      <Icon className="h-3 w-3" />
      {config.label}
    </span>
  );
}

function FacetColumn({ title, values }: { title: string; values: string[] }) {
  return (
    <div className="rounded border border-white/10 bg-black/20 px-2 py-2">
      <div className="mb-1.5 text-[9px] uppercase tracking-[0.22em] text-neutral-600">{title}</div>
      {values.length ? (
        <div className="flex flex-wrap gap-1.5">
          {values.slice(0, 5).map((value) => (
            <span key={`${title}:${value}`} className="rounded border border-sky-200/15 bg-sky-200/[0.035] px-1.5 py-0.5 text-[9px] text-sky-100/70">
              {value}
            </span>
          ))}
        </div>
      ) : (
        <div className="text-[10px] text-neutral-700">none surfaced</div>
      )}
    </div>
  );
}

function evidenceFilterCounts(filter: NonNullable<EvidenceFilterTrace>, fallbackAccepted: number) {
  const explicitRejected = Number.isFinite(filter.rejected) ? Number(filter.rejected) : null;
  const accepted = Number.isFinite(filter.accepted) ? Number(filter.accepted) : fallbackAccepted;
  const raw = Number.isFinite(filter.raw) ? Number(filter.raw) : accepted + (explicitRejected ?? 0);
  const rejected = explicitRejected ?? Math.max(raw - accepted, 0);
  return {
    accepted: Math.max(0, accepted),
    raw: Math.max(0, raw),
    rejected: Math.max(0, rejected),
  };
}

function EmptyState({ suggestions, onPickSuggestion }: { suggestions: string[]; onPickSuggestion: (text: string) => void }) {
  return (
    <div className="flex min-h-full flex-col justify-center py-8">
      <div className="max-w-xl">
        <div className="mb-3 text-[10px] uppercase tracking-[0.28em] text-neutral-600">query-first</div>
        <h3 className="text-2xl font-semibold tracking-tight text-neutral-50">Ask for the structural read, not a dashboard.</h3>
        <p className="mt-3 text-[13px] leading-6 text-neutral-500">Mission Control now starts by showing how the query forced the corpus to group: scoped concepts, unique evidence files, then the synthesis.</p>
        {suggestions.length > 0 && <div className="mt-5 space-y-2">{suggestions.map((suggestion) => <button key={suggestion} type="button" onClick={() => onPickSuggestion(suggestion)} className="block w-full rounded border border-white/10 bg-white/[0.025] px-3 py-2 text-left text-[12px] text-neutral-300 hover:border-amber-200/25 hover:text-amber-100">{suggestion}</button>)}</div>}
      </div>
    </div>
  );
}

function SessionShelf({ sessions, activeSessionId, loadingSessionId, onOpen, onDelete }: { sessions: GraphDiscoverSession[]; activeSessionId: string | null; loadingSessionId: string | null; onOpen: (sessionId: string) => void; onDelete: (sessionId: string) => void }) {
  return (
    <div className="max-h-56 overflow-y-auto border-b border-white/10 bg-[#07080b] p-3 custom-scrollbar">
      {sessions.length ? <div className="space-y-1.5">{sessions.map((session) => <div key={session.session_id} className="flex items-center gap-2 rounded border border-white/10 bg-white/[0.02] px-2 py-1.5"><button type="button" onClick={() => onOpen(session.session_id)} className="min-w-0 flex-1 text-left"><div className={cx("truncate text-[12px]", activeSessionId === session.session_id ? "text-amber-100" : "text-neutral-300")}>{session.title || session.first_query || "Untitled graph read"}</div><div className="mt-0.5 text-[9px] uppercase tracking-wider text-neutral-700">{shortDate(session.updated_at)} / {session.turn_count} turn{session.turn_count === 1 ? "" : "s"}</div></button>{loadingSessionId === session.session_id && <Loader2 className="h-3.5 w-3.5 animate-spin text-neutral-500" />}<button type="button" onClick={() => onDelete(session.session_id)} className="rounded p-1 text-neutral-700 hover:text-red-200" aria-label="Delete graph session"><Trash2 className="h-3.5 w-3.5" /></button></div>)}</div> : <div className="text-[11px] text-neutral-600">No saved graph sessions for this corpus yet.</div>}
    </div>
  );
}

function TerminalLine({ command, text }: { command: string; text: string }) {
  return <div><span className="text-emerald-300/70">$ {command}</span> {text}</div>;
}

function synthesisForResponse(response: GraphDiscoverResponse): AutoSynthesisPayload {
  return normalizeSynthesis(response.auto_synthesis, response);
}

function normalizeSynthesis(
  payload: AutoSynthesisPayload | undefined,
  response: GraphDiscoverResponse,
): AutoSynthesisPayload {
  const fallbackHeadline = response.headline?.headline || response.interpretation || "Structural read from legacy graph response";
  return {
    headline: payload?.headline || fallbackHeadline,
    markdown: payload?.markdown || "",
    sources: Array.isArray(payload?.sources) ? payload.sources : [],
    fallback: !!payload?.fallback,
    fallback_reason: payload?.fallback_reason || null,
  };
}

function graphHintForResponse(response: GraphDiscoverResponse): GraphHintTrace | null {
  return response.trace?.graph_hint || response.trace?.llm_context?.graph_hint || null;
}

function sourceMetaFromRecord(record: Record<string, unknown>): GraphSourceMetadata {
  const raw = record.source;
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as GraphSourceMetadata;
  }
  return {};
}

function sourceDisplayLabel(item: { source_label: string; source?: GraphSourceMetadata }, index: number): string {
  const source = item.source || {};
  return String(source.title || source.filename || item.source_label || `Source ${index + 1}`);
}

function formatSourceMeta(source?: GraphSourceMetadata): string {
  if (!source) return "";
  const location = [source.section, source.page_range || source.page ? `p. ${source.page_range || source.page}` : ""].filter(Boolean).join(" / ");
  const parts = [
    source.author ? `author: ${source.author}` : "",
    source.publisher ? `publisher: ${source.publisher}` : "",
    source.publication_date || source.date ? `date: ${source.publication_date || source.date}` : "",
    source.source_type || source.type ? `type: ${source.source_type || source.type}` : "",
    location ? `section: ${location}` : "",
  ].filter(Boolean);
  return clamp(parts.join(" | "), 180);
}

function receiptFiles(response: GraphDiscoverResponse): ReceiptFile[] {
  const direct = response.trace?.llm_context?.files;
  if (direct?.length) return uniqueReceiptFiles(direct);
  return uniqueReceiptFiles((response.trace?.source_docs || []).slice(0, 12).map((doc, index) => {
    const record = doc as Record<string, unknown>;
    const source = sourceMetaFromRecord(record);
    const docId = String(record.doc_id ?? record.id ?? record.document_id ?? `source-${index + 1}`);
    const chunkIds = Array.isArray(record.chunk_ids) ? record.chunk_ids.map(String) : [];
    const singleChunkId = record.chunk_id ? String(record.chunk_id) : "";
    const allChunkIds = [...chunkIds, singleChunkId].filter(Boolean);
    return {
      doc_id: docId,
      source_label: String(record.source_label ?? record.filename ?? source.title ?? source.filename ?? docId),
      source,
      chunk_count: Number(record.chunk_count ?? record.chunks ?? allChunkIds.length) || allChunkIds.length,
      chunk_ids: allChunkIds,
      has_temporal: Boolean(record.has_temporal ?? record.date ?? source.publication_date ?? source.date),
    };
  }));
}

function uniqueReceiptFiles(files: ReceiptFile[]): ReceiptFile[] {
  const bySource = new Map<string, ReceiptFile>();
  for (const file of files) {
    const key = `${file.doc_id || ""}:${file.source_label || ""}`;
    const existing = bySource.get(key);
    if (!existing) {
      bySource.set(key, {
        ...file,
        chunk_ids: [...new Set(file.chunk_ids || [])],
      });
      continue;
    }
    const chunkIds = [...new Set([...(existing.chunk_ids || []), ...(file.chunk_ids || [])])];
    existing.chunk_ids = chunkIds;
    existing.chunk_count = Math.max(existing.chunk_count || 0, file.chunk_count || 0, chunkIds.length);
    existing.has_temporal = Boolean(existing.has_temporal || file.has_temporal);
    existing.source = existing.source || file.source;
  }
  return [...bySource.values()];
}

function receiptChunks(response: GraphDiscoverResponse): ReceiptChunk[] {
  const direct = response.trace?.llm_context?.chunks;
  if (direct?.length) return direct;
  return (response.trace?.source_docs || []).slice(0, 10).map((doc, index) => {
    const record = doc as Record<string, unknown>;
    const source = sourceMetaFromRecord(record);
    const docId = String(record.doc_id ?? record.id ?? record.document_id ?? `source-${index + 1}`);
    return {
      chunk_id: String(record.chunk_id ?? record.id ?? `chunk-${index + 1}`),
      doc_id: docId,
      source_label: String(record.source_label ?? record.filename ?? source.title ?? source.filename ?? docId),
      source,
      preview: clamp(String(record.preview ?? record.text ?? record.excerpt ?? record.summary ?? ""), 260),
      has_temporal: Boolean(record.has_temporal ?? record.date ?? source.publication_date ?? source.date),
    };
  });
}

function readableSourceLabel(label: string, index: number): string {
  const clean = String(label || "").trim();
  if (/^[a-f0-9]{32,}$/i.test(clean)) return `Source ${index + 1} (${clean.slice(0, 8)})`;
  if (clean.length > 64) return `${clean.slice(0, 61).trim()}...`;
  return clean || `Source ${index + 1}`;
}

function rankedValues(values: Array<string | null | undefined>): string[] {
  const counts = new Map<string, number>();
  values
    .map((value) => String(value || "").trim())
    .filter(Boolean)
    .filter((value) => value.toLowerCase() !== "unknown")
    .forEach((value) => counts.set(value, (counts.get(value) || 0) + 1));
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 5)
    .map(([value, count]) => `${value}${count > 1 ? ` x${count}` : ""}`);
}

function claimConfig(level: ClaimLevel) {
  if (level === "observed") {
    return {
      label: "observed evidence",
      description: "Retrieved chunks or source metadata accepted by the evidence gate.",
      icon: FileText,
      className: "border-emerald-300/20 bg-emerald-300/[0.04] text-emerald-100/75",
    };
  }
  if (level === "structure") {
    return {
      label: "graph structure",
      description: "Entities, relations, ontology facets, and selected graph neighborhoods.",
      icon: Network,
      className: "border-sky-300/20 bg-sky-300/[0.04] text-sky-100/75",
    };
  }
  if (level === "hypothesis") {
    return {
      label: "testable hypothesis",
      description: "A candidate gap or bridge that needs more evidence before becoming a claim.",
      icon: FlaskConical,
      className: "border-amber-200/25 bg-amber-200/[0.045] text-amber-100/80",
    };
  }
  return {
    label: "next action",
    description: "A follow-up query or inspection move.",
    icon: GitBranch,
    className: "border-violet-300/20 bg-violet-300/[0.04] text-violet-100/75",
  };
}

function claimLevelDescription(level: ClaimLevel): string {
  if (level === "observed") return "Real source snippets and file metadata that passed the evidence filter.";
  if (level === "structure") return "Ontology-shaped graph neighborhoods: entities, relations, facets, and bridges.";
  if (level === "hypothesis") return "A possible hidden connection that should be tested by retrieval before being trusted.";
  return "A useful next query or inspection path for continuing the research thread.";
}

function clamp(text: string, limit: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  if (clean.length <= limit) return clean;
  return `${clean.slice(0, Math.max(0, limit - 3)).trim()}...`;
}

function sectionForJump(section: string): MissionSection | null {
  // The card-shaped sections (themes/bridges/gaps/signals/moves) folded into a
  // single woven synthesis. Legacy jump targets all land on the synthesis prose.
  if (section === "themes" || section === "bridges" || section === "gaps" || section === "tensions" || section === "signals" || section === "moves" || section === "synthesis") {
    return "synthesis";
  }
  if (section === "sources") return "sources";
  if (section === "trace") return "trace";
  return null;
}

function scrollToSection(section: MissionSection) {
  document.getElementById(`mission-${section}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function shortDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "0.0s";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
