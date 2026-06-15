// App.tsx - Main application component (3-Pane Deterministic Graph Architecture)
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Menu, Network, Share2, X } from "lucide-react";
import { Sidebar } from "./components/chat/Sidebar";
import { ChatWindow } from "./components/chat/ChatWindow";
import { ChatInput } from "./components/chat/ChatInput";
import { ChatContextMenu } from "./components/chat/ChatContextMenu";
import { GraphViewer } from "./components/graph/GraphViewer";
import { SettingsModal } from "./components/settings/SettingsModal";
import { LoginView } from "./components/auth/LoginView";
import { IngestionDashboard } from "./components/ingestion/IngestionDashboard";
import { useSettingsStore } from "./stores/settingsStore";
import { useQueryModelPoolStore } from "./stores/queryModelPoolStore";
import { useChatStore } from "./stores/chatStore";
import { useAuthStore } from "./stores/authStore";
import * as api from "./lib/api";
import type { ChatMessage, ChatRequest, Collection, CorpusResponse } from "./types";

function summarizeToolResultDetail(name: string, result: string): string | undefined {
  try {
    const parsed = JSON.parse(result) as {
      error?: string;
      url?: string;
      query?: string;
      method?: string;
      title?: string;
      chars?: number;
      status?: string;
      pipeline?: {
        candidate_results?: number;
        full_page_fetch_attempts?: number;
        full_page_fetch_successes?: number;
        final_reranked_results?: number;
        ranked_by?: string;
        freshness_time_range?: string | null;
        snippet_only?: boolean;
        snippet_sufficiency_score?: number;
        redis_search_cache_hit?: boolean;
        redis_page_cache_hit?: boolean;
        js_render?: { attempted?: boolean; rendered?: boolean };
      };
    };
    if (parsed.error) return parsed.error;
    if (name === "fetch_page") {
      const lines = [
        parsed.url ? `url: ${parsed.url}` : "",
        parsed.title ? `title: ${parsed.title}` : "",
        parsed.method ? `method: ${parsed.method}` : "",
        parsed.status ? `status: ${parsed.status}` : "",
        typeof parsed.chars === "number" ? `chars: ${parsed.chars}` : "",
      ].filter(Boolean);
      return lines.join("\n") || undefined;
    }
    if (name !== "web_search") return undefined;
    const p = parsed.pipeline;
    if (!p) return undefined;
    const freshness = p.freshness_time_range ? ` · ${p.freshness_time_range}` : "";
    const js = p.js_render?.rendered
      ? " · js rendered"
      : p.js_render?.attempted
        ? " · js tried"
        : "";
    const snippet = p.snippet_only
      ? ` · snippets enough ${p.snippet_sufficiency_score ?? ""}`.trimEnd()
      : "";
    const cache = p.redis_search_cache_hit || p.redis_page_cache_hit
      ? " · cache hit"
      : "";
    return `candidates ${p.candidate_results ?? 0} → fetched ${p.full_page_fetch_successes ?? 0}/${p.full_page_fetch_attempts ?? 0} → final ${p.final_reranked_results ?? 0}${freshness}${snippet}${cache}${js}`;
  } catch {
    return undefined;
  }
}

function summarizeToolStartDetail(
  name: string,
  args?: string,
): string | undefined {
  if (!args) return undefined;
  try {
    const parsed = JSON.parse(args) as {
      query?: string;
      max_results?: number;
      url?: string;
      reason?: string;
    };
    const lines = [
      parsed.query ? `query: ${parsed.query}` : "",
      parsed.url ? `url: ${parsed.url}` : "",
      parsed.reason ? `reason: ${parsed.reason}` : "",
      typeof parsed.max_results === "number"
        ? `max_results: ${parsed.max_results}`
        : "",
    ].filter(Boolean);
    return lines.join("\n") || undefined;
  } catch {
    return name === "tool" ? undefined : args;
  }
}

function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [isGraphViewOpen, setIsGraphViewOpen] = useState(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [corpora, setCorpora] = useState<CorpusResponse[]>([]);
  const corporaRef = useRef<CorpusResponse[]>([]);
  const [modelsLoaded, setModelsLoaded] = useState(false);

  // Pt 7: prefill bridge from GraphViewer's Graph Query tab to ChatInput.
  // When the user clicks a refined chip in the dashboard, GraphViewer
  // fires onSendToChat(text). We bump nonce + load the text; ChatInput's
  // useEffect on prefill.nonce replaces its input and focuses. We also
  // close the graph modal so the chat is visible.
  const [chatPrefill, setChatPrefill] = useState<{ text: string; nonce: number }>({
    text: "",
    nonce: 0,
  });
  const handleGraphSendToChat = useCallback((text: string) => {
    setChatPrefill((prev) => ({ text, nonce: prev.nonce + 1 }));
    setIsGraphViewOpen(false);
  }, []);

  const { setSelectedModel, setModels, maxTokens, theme, selectedCorpusIds } =
    useSettingsStore();

  // Auth state — route guard depends on isAuthenticated
  const { isAuthenticated, setAuth, clearAuth } = useAuthStore();
  const [authChecked, setAuthChecked] = useState(false);

  // Verify persisted token on mount — call GET /api/auth/me
  // If token is expired/invalid, clear auth and show login screen
  useEffect(() => {
    const checkAuth = async () => {
      const { token } = useAuthStore.getState();
      if (token) {
        try {
          const me = await api.getMe();
          // Token valid — refresh user data from server
          setAuth(token, {
            id: me.id,
            username: me.username,
            created_at: me.created_at,
          });
        } catch {
          // Token expired or invalid — force re-login
          clearAuth();
        }
      }
      setAuthChecked(true);
    };
    checkAuth();
  }, []);

  const effectiveGraphCorpusIds = useMemo(() => {
    const explicit = selectedCorpusIds.filter(Boolean);
    if (explicit.length > 0) return Array.from(new Set(explicit));
    return Array.from(
      new Set(corpora.map((corpus) => corpus.corpus_id).filter(Boolean)),
    );
  }, [corpora, selectedCorpusIds]);

  useEffect(() => {
    corporaRef.current = corpora;
  }, [corpora]);

  const handleSend = useCallback(
    async (message: string, attachedFiles?: File[]) => {
      console.log("handleSend triggered");
      const chat = useChatStore.getState();
      const settings = useSettingsStore.getState();
      const requestCorpusIds =
        settings.selectedCorpusIds.length > 0
          ? settings.selectedCorpusIds
          : Array.from(
              new Set(
                corporaRef.current
                  .map((corpus) => corpus.corpus_id)
                  .filter(Boolean),
              ),
            );

      // Phase 29 — convert paperclip-staged File[] into the
      // ChatAttachment shape the backend expects. Images become base64;
      // text files become UTF-8 strings. Anything unsupported (PDF /
      // DOCX / etc.) is rejected here with a toast — fail loud at the
      // entrypoint instead of silently dropping the file.
      let attachments: import("./types/chat").ChatAttachment[] | undefined;
      if (attachedFiles && attachedFiles.length > 0) {
        const { filesToAttachments } = await import("./lib/attachments");
        const { ok, failed } = await filesToAttachments(attachedFiles);
        if (failed.length > 0) {
          const msg = failed
            .map((f) => `${f.filename}: ${f.reason}`)
            .join("\n");
          chat.setError(`Some attachments couldn't be processed:\n${msg}`);
          // If ALL failed, abort the turn. If only some failed, continue
          // with the successful ones — the user still gets their message.
          if (ok.length === 0) return;
        }
        attachments = ok.length > 0 ? ok : undefined;
      }

      let cid = chat.activeConversationId;
      if (!cid) {
        try {
          const { id } = await api.createConversation({
            title: message.slice(0, 50),
          });
          cid = id;
          chat.addConversation({
            id,
            title: message.slice(0, 50),
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            message_count: 0,
          });
        } catch {
          chat.setError("Failed to create conversation");
          return;
        }
      }

      const userMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: "user",
        content: message,
        created_at: new Date().toISOString(),
      };
      chat.addMessage(cid, userMessage);
      chat.startStreaming();

      const retrievalConfig =
        settings.retrievalTier === "qdrant_only"
          ? {
              retrieval_k: settings.vectorChildChunks,
              top_k_summary: settings.vectorSummaries,
              final_top_k: settings.vectorFinalSources,
              rerank_enabled: settings.vectorReranker,
            }
          : settings.retrievalTier === "qdrant_mongo_graph"
          ? {
              retrieval_k: settings.graphChildChunks,
              top_k_summary: settings.graphSummaries,
              final_top_k: settings.graphFinalSources,
              rerank_enabled: settings.graphReranker,
              fact_seed_limit: settings.graphFactSeeds,
              neo4j_expansion_cap: settings.graphExpansion,
            }
          : {
              retrieval_k: settings.hybridChildChunks,
              top_k_summary: settings.hybridSummaries,
              final_top_k: settings.hybridFinalSources,
              rerank_enabled: settings.hybridReranker,
            };

      const request: ChatRequest = {
        conversation_id: cid,
        message,
        // Phase 29 — per-turn attachments (images base64, text UTF-8).
        // Omitted entirely when there are none; backend treats absence
        // as "no multimodal content" and runs the regular text pipeline.
        attachments,
        corpus_ids: requestCorpusIds,
        retrieval_tier: settings.retrievalTier,
        collections: settings.selectedCollectionIds,
        overrides: {
          model: settings.selectedModel,
          temperature: settings.temperature,
          max_tokens: settings.maxTokens,
          // The visible HyDE toggle is authoritative. Send false explicitly so
          // backend query-profile defaults cannot silently turn HyDE on.
          hyde_enabled: settings.hydeEnabled,
          web_search_enabled: settings.webSearchEnabled ? true : undefined,
          // Evidence-controller defaults. Keep the chat bar simple:
          // Web ON means normal fetch policy, native YouTube transcript
          // extraction when a YouTube URL is actually relevant, and the
          // default source budget. Advanced internals stay out of the
          // per-turn toolbar.
          web_fetch_depth: settings.webSearchEnabled ? "normal" : undefined,
          web_research_mode: undefined,
          web_youtube_transcripts: settings.webSearchEnabled ? true : undefined,
          web_max_sources: settings.webSearchEnabled ? 9 : undefined,
          collection_ids: settings.selectedCollectionIds,
          // Phase 14.1 — agentic override (per-request)
          agentic_mode: settings.agenticModeEnabled || undefined,
          // Only send when non-empty — empty string shadows Phase F pool
          // resolution on the backend. Empty = let backend resolve via
          // models.agentic.pool_entry_id (Phase F).
          agentic_model:
            settings.agenticModeEnabled && settings.agenticModel
              ? settings.agenticModel
              : undefined,
          // Phase 15 — reasoning mode (picked in ToggleBar before send)
          reasoning_mode:
            settings.reasoningMode && settings.reasoningMode !== "none"
              ? settings.reasoningMode
              : undefined,
          reasoning_blend:
            settings.reasoningBlend && settings.reasoningBlend.length > 0
              ? settings.reasoningBlend
              : undefined,
          // Phase 27 — search-mode dispatch (auto / local / global).
          // Omit when "auto" so the backend uses its default (which IS
          // auto) — keeps wire payload small and means an old client
          // that doesn't set this still behaves correctly.
          search_mode:
            settings.searchMode && settings.searchMode !== "auto"
              ? settings.searchMode
              : undefined,
          // Phase 28 — thinking-effort dial. Send AUTO explicitly so the
          // visible selector wins over stale model-pool extras such as a
          // saved `thinking: disabled`. The backend mapper is a no-op for
          // ordinary chat models, so this stays safe for non-reasoning paths.
          thinking_effort: settings.thinkingEffort || "auto",
          // Phase 17/24 — HyDE model routing lives on the backend:
          // Settings -> Models -> HyDE wins when configured; otherwise the
          // helper inherits the active chat model for this turn. Do not send
          // the legacy flat chat.hyde_model here, because it would shadow the
          // dedicated HyDE card and make the visible chat selector lie.
          hyde_model: undefined,
          // Phase 18 — Query Profile speed preset (backend resolver expands
          // the preset into retrieval_k/rerank/hyde defaults). Individual
          // overrides (retrieval_k, rerank_enabled) can still win if set.
          query_profile:
            settings.queryProfile && settings.queryProfile !== "balanced"
              ? settings.queryProfile
              : undefined,
          // Tier-specific retrieval shape. These are the core gather/filter
          // factors from Settings → Retrieval. Match sensitivity is kept off
          // deliberately so ranking stays relative instead of deleting weak
          // bridge candidates before the reranker sees them.
          ...retrievalConfig,
          similarity_threshold: 0,
        },
        selected_tools: settings.selectedToolIds,
        // Phase 24 — Skills + Reasoning Cascade
        active_skill_ids:
          settings.selectedSkillIds && settings.selectedSkillIds.length > 0
            ? settings.selectedSkillIds
            : undefined,
        reasoning_cascade: settings.reasoningCascadeEnabled || undefined,
      };

      let tokenBuffer = "";
      let thinkingBuffer = "";
      let flushTimer: number | undefined;
      const flushStreamingBuffers = () => {
        if (flushTimer !== undefined) {
          window.clearTimeout(flushTimer);
          flushTimer = undefined;
        }
        const tokenChunk = tokenBuffer;
        const thinkingChunk = thinkingBuffer;
        tokenBuffer = "";
        thinkingBuffer = "";
        if (tokenChunk) chat.updateStreamingContent(tokenChunk);
        if (thinkingChunk) chat.updateStreamingThinking(thinkingChunk);
      };
      const scheduleStreamingFlush = () => {
        if (flushTimer !== undefined) return;
        flushTimer = window.setTimeout(flushStreamingBuffers, 33);
      };

      const preserveStreamingFailure = (errorMessage: string) => {
        flushStreamingBuffers();
        chat.setError(errorMessage);
        const state = useChatStore.getState();
        const hasPartialAssistant =
          Boolean(state.streamingContent) ||
          Boolean(state.streamingThinking) ||
          state.streamingTraceEvents.length > 0 ||
          state.streamingToolActivity.length > 0 ||
          state.streamingProcessTimeline.length > 0 ||
          state.streamingSources.length > 0;

        if (!cid || !hasPartialAssistant) {
          chat.stopStreaming();
          return;
        }

        chat.finalizeStreamingMessage(cid, {
          id: crypto.randomUUID(),
          role: "assistant",
          content:
            state.streamingContent ||
            `Stream failed before the final answer.\n\n[ERROR] ${errorMessage}`,
          thinking: state.streamingThinking || undefined,
          trace_events: state.streamingTraceEvents,
          process_timeline: state.streamingProcessTimeline,
          model_used: request.overrides?.model || settings.selectedModel,
          created_at: new Date().toISOString(),
          collections_queried: requestCorpusIds,
          sources: state.streamingSources,
        });
      };

      try {
        await api.streamChat(
          request,
          (event) => {
            switch (event.type) {
              case "token":
                if (event.content) {
                  tokenBuffer += event.content;
                  scheduleStreamingFlush();
                }
                break;
              case "thinking":
                if (event.thinking) {
                  thinkingBuffer += event.thinking;
                  scheduleStreamingFlush();
                }
                break;
              case "trace_event":
                flushStreamingBuffers();
                if (event.trace_event) {
                  chat.addStreamingTraceEvent(event.trace_event);
                }
                break;
              case "trimming":
                console.log("[TRIM]", event.content || event.trimming_details);
                break;
              case "budget":
                if (
                  typeof event.tokens_used === "number" &&
                  typeof event.tokens_max === "number"
                ) {
                  chat.setTokenBudget(event.tokens_used, event.tokens_max);
                }
                break;
              case "tier_downgraded": {
                flushStreamingBuffers();
                // Surface as an italic notice inline in the assistant message
                const msg =
                  event.content ||
                  "Retrieval tier downgraded (strategy intersection).";
                chat.updateStreamingContent(`\n\n**<WRN>:** ${msg}\n\n`);
                break;
              }
              case "sources": {
                // Capture chunks for the RetrievalBadge expand panel.
                // (Pre-fix this branch was missing — chunks were emitted
                // by the backend but dropped silently by the FE.)
                if (event.sources) {
                  chat.setStreamingSources(event.sources);
                }
                break;
              }
              case "tool_call_start": {
                flushStreamingBuffers();
                // Keep tool activity out of the assistant answer text. The
                // live MessageBubble renders this in a separate status lane.
                try {
                  const calls = JSON.parse(event.content || "[]") as Array<{
                    name: string;
                    args?: string;
                  }>;
                  for (const c of calls) {
                    chat.addStreamingToolActivity({
                      id: crypto.randomUUID(),
                      name: c.name || "tool",
                      status: "running",
                      detail: summarizeToolStartDetail(
                        c.name || "tool",
                        c.args,
                      ),
                    });
                  }
                } catch {
                  chat.addStreamingToolActivity({
                    id: crypto.randomUUID(),
                    name: "tool",
                    status: "running",
                  });
                }
                break;
              }
              case "tool_result": {
                flushStreamingBuffers();
                try {
                  const results = JSON.parse(event.content || "[]") as Array<{
                    name: string;
                    result: string;
                  }>;
                  for (const r of results) {
                    chat.completeStreamingToolActivity(
                      r.name || "tool",
                      summarizeToolResultDetail(r.name || "tool", r.result || ""),
                    );
                  }
                } catch {
                  chat.completeStreamingToolActivity("tool");
                }
                break;
              }
              case "error":
                flushStreamingBuffers();
                preserveStreamingFailure(event.content || "An error occurred");
                break;
              case "done": {
                flushStreamingBuffers();
                const state = useChatStore.getState();
                // Trust-signal fields ride on the `done` SSE frame so the
                // live message renders the RetrievalBadge immediately.
                // collections_queried falls back to the request-time corpus
                // selection when the backend doesn't echo it (older builds).
                chat.finalizeStreamingMessage(cid!, {
                  id: crypto.randomUUID(),
                  role: "assistant",
                  content: state.streamingContent,
                  thinking: state.streamingThinking || undefined,
                  trace_events: state.streamingTraceEvents,
                  process_timeline: state.streamingProcessTimeline,
                  model_used: event.model_used,
                  created_at: new Date().toISOString(),
                  trimming_applied: event.trimming_applied,
                  collections_queried:
                    event.collections_queried ?? requestCorpusIds,
                  chunks_returned: event.chunks_returned,
                  strategy_used: event.strategy_used,
                  query_profile_used: event.query_profile_used,
                  reasoning_mode_used: event.reasoning_mode_used,
                  hyde_applied: event.hyde_applied,
                  agentic_mode_used: event.agentic_mode_used,
                  downgrade_reason: event.downgrade_reason,
                  // Phase 24 — skill/tool/reasoning trust signals
                  skills_used: event.skills_used,
                  tools_used: event.tools_used,
                  reasoning_cascade_applied: event.reasoning_cascade_applied,
                  sources: state.streamingSources,
                });
                break;
              }
            }
          },
          (err) => {
            preserveStreamingFailure(err.message);
          },
        );
      } catch (err) {
        preserveStreamingFailure(
          err instanceof Error ? err.message : "Unknown error",
        );
      }
    },
    [],
  );

  // Window event wiring (always bind, auth-independent)
  useEffect(() => {
    const handleToggleGraph = () => setIsGraphViewOpen((prev) => !prev);
    const handleOpenSettings = () => setIsSettingsOpen(true);

    window.addEventListener("toggle-graph-view", handleToggleGraph);
    window.addEventListener("open-settings", handleOpenSettings);

    return () => {
      window.removeEventListener("toggle-graph-view", handleToggleGraph);
      window.removeEventListener("open-settings", handleOpenSettings);
    };
  }, []);

  // Auth-gated bootstrap — collections, models, settings all require a token.
  // Fires once after authChecked && isAuthenticated flip true. Prevents the
  // 403 pile-up we saw when this ran on bare mount.
  useEffect(() => {
    if (!authChecked || !isAuthenticated) return;
    loadCollections();
    loadCorpora();
    loadModels();
    useSettingsStore.getState().loadFromAPI();
    useQueryModelPoolStore.getState().load();
  }, [authChecked, isAuthenticated]);

  // Apply deterministic theme mapping to document root
  useEffect(() => {
    const root = document.documentElement;

    // Clear previous protocol themes (includes legacy `dark`/`light` for
    // users whose persisted localStorage still carries those values).
    root.classList.remove(
      "theme-ayu-mirage",
      "theme-gruvbox",
      "theme-serendipity",
      "theme-nord",
      "theme-dracula",
      "theme-solar",
      "theme-claude",
      "dark",
      "light",
    );

    // Enforce protocol themes. Legacy persisted values (`system`/`light`/
    // `dark`) fail the include check and fall through to the Obsidian
    // default — we keep that rescue path for back-compat.
    const validThemes = [
      "ayu-mirage",
      "gruvbox",
      "serendipity",
      "nord",
      "dracula",
      "solar",
      "claude",
    ];

    if (validThemes.includes(theme)) {
      root.classList.add(`theme-${theme}`);
    } else {
      root.classList.add("theme-ayu-mirage"); // Default Obsidian Protocol
    }
  }, [theme]);

  const loadCollections = async () => {
    try {
      const data = await api.getCollections();
      setCollections(data);
    } catch (error) {
      console.error("Failed to load collections:", error);
    }
  };

  const loadCorpora = async () => {
    try {
      const data = await api.listCorpora();
      setCorpora(data);
      useSettingsStore
        .getState()
        .purgeStaleCorpusIds(data.map((corpus) => corpus.corpus_id));
    } catch (error) {
      console.error("Failed to load corpora:", error);
    }
  };

  const loadModels = async () => {
    try {
      const data = await api.getModels();
      setModels(data.chat_models, data.embedding_models);
      const currentSelectedModel = useSettingsStore.getState().selectedModel;
      if (data.chat_models.length > 0 && !currentSelectedModel) {
        setSelectedModel(data.default_model || data.chat_models[0].id);
      }
      setModelsLoaded(true);
    } catch (error) {
      console.error("Failed to load models:", error);
    }
  };

  // ── Auth Route Guard ──
  // Show loading screen while verifying token
  if (!authChecked) {
    return (
      <div className="flex h-screen items-center justify-center bg-[var(--bg-base)]">
        <div className="text-[10px] font-bold uppercase tracking-[0.4em] text-[var(--text-tertiary)] animate-pulse">
          VERIFYING SESSION...
        </div>
      </div>
    );
  }

  // Show login screen if not authenticated
  if (!isAuthenticated) {
    return <LoginView />;
  }

  return (
    <div className="flex h-dvh bg-bg-base text-text-primary overflow-hidden selection:bg-accent-main/30">
      {/* 1. LEFT PANE: Directory / Explorer */}
      <Sidebar
        isOpen={sidebarOpen}
        onToggle={() => setSidebarOpen(!sidebarOpen)}
      />

      {/* 2. CENTER PANE: Active Workspace (Graph/Chat) */}
      <main className="flex-1 flex flex-col min-w-0 border-r border-border-minimal relative bg-bg-surface">
        {/* Header - Terminal Status Bar + session controls */}
        <header className="min-h-16 md:h-24 border-b border-border-minimal flex flex-col sm:flex-row sm:items-center justify-between gap-2 px-3 sm:px-6 py-2 md:py-0 bg-bg-base z-[80] shrink-0">
          <div className="flex w-full sm:w-auto items-center justify-between gap-3">
            <button
              onClick={() => setSidebarOpen(true)}
              className="lg:hidden flex h-9 w-9 items-center justify-center text-content-secondary hover:text-accent-main transition-none"
              aria-label="Open navigation"
            >
              <Menu className="w-4 h-4" />
            </button>
            <div className="hidden md:flex items-center gap-3">
              <div className="w-8 h-8 rounded-full bg-[#1c4e80] flex items-center justify-center border border-[#2a6baf] shrink-0">
                <Share2 className="w-4 h-4 text-white" />
              </div>
              <div>
                <h1 className="text-content-primary text-[15px] font-semibold tracking-wide leading-tight">
                  Polymath
                </h1>
                <div className="text-[10px] text-content-tertiary font-mono tracking-widest uppercase mt-0.5">
                  KNOWLEDGE GRAPH
                </div>
              </div>
            </div>
          </div>

          <div className="flex w-full min-w-0 items-center justify-end gap-2 pb-1 sm:w-auto sm:pb-0">
            <ChatContextMenu collections={collections} />
            <button
              onClick={() => setIsGraphViewOpen(true)}
              className="pm-soft-control flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-border-minimal bg-bg-surface/95 text-content-secondary hover:text-accent-main"
              title="Global Graph"
              aria-label="Open global graph"
            >
              <Network className="w-4 h-4" />
            </button>
          </div>
        </header>

        {/* Content Area (Force-directed Graph / Chat View) */}
        <div className="flex-1 relative flex flex-col overflow-hidden bg-[radial-gradient(ellipse_at_center,_var(--bg-raised)_0%,_var(--bg-surface)_100%)]">
          <ChatWindow />
        </div>

        {/* Input Area - Command Line Interface */}
        <div className="shrink-0 p-2 sm:p-4 bg-bg-base border-t border-border-minimal z-50">
          <div className="max-w-7xl mx-auto">
            <ChatInput
              onSend={handleSend}
              isLoading={!modelsLoaded}
              placeholder={
                modelsLoaded
                  ? "Ask Polymath..."
                  : "INITIALIZING SYSTEMS..."
              }
              tokenCount={{ current: 0, max: maxTokens }}
              prefill={chatPrefill}
            />
          </div>
        </div>
      </main>

      {/* Global Views & Modals */}
      {/* z-[100]: must sit above all chat chrome — the header is z-[80], the
          input bar z-50, and the ChatContextMenu (Sources/Context) panels
          z-[95]; at z-50 those painted THROUGH this full-screen graph overlay. */}
      {isGraphViewOpen && (
        <div className="fixed inset-0 z-[100] bg-[#0a0a0a]/95">
          <div className="absolute inset-0 flex flex-col">
            <button
              type="button"
              onClick={() => setIsGraphViewOpen(false)}
              className="absolute left-3 top-3 z-[70] flex h-10 w-10 items-center justify-center rounded border border-zinc-800 bg-zinc-950/85 text-zinc-400 backdrop-blur hover:border-rose-700 hover:text-rose-300"
              title="Close graph view"
              aria-label="Close graph view"
            >
              <X className="h-4 w-4" />
            </button>
            {/* GraphViewer fills the canvas area. Graph questions always
                route to query mode so the response becomes graph nodes and
                edges rather than a separate visual mode. */}
            <div className="flex-1 min-h-0 relative">
              <GraphViewer
                mode="brain"
                corpusIds={effectiveGraphCorpusIds}
                onSendToChat={handleGraphSendToChat}
              />
            </div>
          </div>
        </div>
      )}

      <SettingsModal
        isOpen={isSettingsOpen}
        onClose={() => setIsSettingsOpen(false)}
      />

      {/* Phase G — live ingestion progress, floats bottom-right. Self-hides when empty. */}
      <IngestionDashboard />
    </div>
  );
}

export default App;
