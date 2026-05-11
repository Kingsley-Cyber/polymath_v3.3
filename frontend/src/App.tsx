// App.tsx - Main application component (3-Pane Deterministic Graph Architecture)
import { useCallback, useState, useEffect } from "react";
import { Menu, Network, Share2 } from "lucide-react";
import { Sidebar } from "./components/chat/Sidebar";
import { ChatWindow } from "./components/chat/ChatWindow";
import { ChatInput } from "./components/chat/ChatInput";
import { CollectionSelector } from "./components/chat/CollectionSelector";
import { CorpusMultiSelect } from "./components/chat/CorpusMultiSelect";
import { ModelSelector } from "./components/chat/ModelSelector";
import { ReasoningModeSelector } from "./components/chat/ReasoningModeSelector";
import { QueryProfileSelector } from "./components/chat/QueryProfileSelector";
import { RetrievalTierSelector } from "./components/chat/RetrievalTierSelector";
import { GraphViewer } from "./components/graph/GraphViewer";
import { SettingsModal } from "./components/settings/SettingsModal";
import { LoginView } from "./components/auth/LoginView";
import { IngestionDashboard } from "./components/ingestion/IngestionDashboard";
import { useSettingsStore } from "./stores/settingsStore";
import { useChatStore } from "./stores/chatStore";
import { useAuthStore } from "./stores/authStore";
import { useIngestionQueueStore } from "./stores/ingestionQueueStore";
import * as api from "./lib/api";
import type { ChatMessage, ChatRequest, Collection } from "./types";

function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [isGraphViewOpen, setIsGraphViewOpen] = useState(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [modelsLoaded, setModelsLoaded] = useState(false);
  const [pipelineStatus, setPipelineStatus] = useState<string>("");

  // PR 4 — graph viewer mode + query state. Brain mode renders the
  // multi-corpus supernode overview; query mode runs Mission Control
  // synthesis against the selected corpora and plays the entry animation.
  const [graphViewerMode, setGraphViewerMode] = useState<"brain" | "query">("brain");
  const [graphViewerQuery, setGraphViewerQuery] = useState<string>("");
  const [graphViewerQueryDraft, setGraphViewerQueryDraft] = useState<string>("");
  const [graphViewerRunCount, setGraphViewerRunCount] = useState(0);

  const { selectedModel, setSelectedModel, setModels, maxTokens, theme, selectedCorpusIds } =
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

  // PR 4 — no auto-fallback when no corpora selected. The new GraphViewer
  // renders an empty-state prompt instead. Multi-corpus selection is
  // explicit; the legacy "first available corpus" fallback is retired
  // (Phase F cleanup of GRAPH_VIEWER_BRIDGE.md).

  // Uploads each file into the user's selected corpus, pushes the returned
  // doc_id into the ingestion queue store, and returns immediately. The
  // IngestionDashboard panel watches progress via SSE — no blocking here.
  const handleFileUpload = async (files: File[]) => {
    if (files.length === 0) return;

    const selectedCorpusIds = useSettingsStore.getState().selectedCorpusIds;
    const targetCorpusId = selectedCorpusIds[0];
    if (!targetCorpusId) {
      setPipelineStatus(
        "select a corpus first — use the Corpus dropdown to pick one",
      );
      setTimeout(() => setPipelineStatus(""), 4000);
      return;
    }

    let corpusName = targetCorpusId.slice(0, 8);
    try {
      const all = await api.listCorpora();
      const match = all.find((c) => c.corpus_id === targetCorpusId);
      if (match) corpusName = match.name;
    } catch {
      /* fall back to id prefix */
    }

    const { enqueue } = useIngestionQueueStore.getState();
    setPipelineStatus(`uploading ${files.length} file(s)…`);

    // Continuous-replenish worker pool. N parallel uploaders each pull from
    // the queue until empty. Keeps the docling sidecar + backend ingest
    // queue full so the LLM extraction pool saturates. Per-doc worker on
    // the backend then chews through its chunks via the multi-lane ghost
    // pool. Tune UPLOAD_CONCURRENCY based on docling throughput and
    // backend CPU — 10 is the safe default for single-box dev; bump to
    // 20-30 on beefier hosts if docling isn't saturated.
    const UPLOAD_CONCURRENCY = 10;
    const queue = [...files];
    let uploaded = 0;
    let failed = 0;

    const worker = async () => {
      while (queue.length > 0) {
        const file = queue.shift();
        if (!file) break;
        try {
          const result = await api.uploadDocumentToCorpus(
            targetCorpusId,
            file,
          );
          if (result.doc_id) {
            enqueue({
              doc_id: result.doc_id,
              filename: result.filename || file.name,
              corpus_id: result.corpus_id || targetCorpusId,
              corpus_name: corpusName,
            });
            uploaded += 1;
          }
        } catch (err) {
          console.error(`upload failed for ${file.name}:`, err);
          failed += 1;
        }
        // Live status so the user sees progress even before the dashboard
        // SSE streams start landing.
        setPipelineStatus(
          `uploading ${uploaded + failed}/${files.length}` +
            (failed > 0 ? ` (${failed} failed)` : ""),
        );
      }
    };

    await Promise.all(
      Array.from({ length: Math.min(UPLOAD_CONCURRENCY, files.length) }, () =>
        worker(),
      ),
    );

    setPipelineStatus(
      `queued ${uploaded}/${files.length} file(s) — see panel` +
        (failed > 0 ? ` · ${failed} failed` : ""),
    );
    setTimeout(() => setPipelineStatus(""), 4000);
    return;
  };

  const handleSend = useCallback(
    async (message: string, _attachments?: File[]) => {
      console.log("handleSend triggered");
      const chat = useChatStore.getState();
      const settings = useSettingsStore.getState();

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

      const hydeRequested =
        settings.hydeEnabled || settings.queryProfile === "thorough";

      const request: ChatRequest = {
        conversation_id: cid,
        message,
        corpus_ids: settings.selectedCorpusIds,
        retrieval_tier: settings.retrievalTier,
        collections: settings.selectedCollectionIds,
        overrides: {
          model: settings.selectedModel,
          temperature: settings.temperature,
          max_tokens: settings.maxTokens,
          hyde_enabled: settings.hydeEnabled ? true : undefined,
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
          // Phase 17 — HyDE per-request model (only when HyDE toggle is on).
          // Empty string shadows Phase F pool resolution, so we send undefined
          // when no explicit per-request model is set.
          hyde_model:
            hydeRequested && settings.hydeModel
              ? settings.hydeModel
              : undefined,
          // Phase 18 — Query Profile speed preset (backend resolver expands
          // the preset into retrieval_k/rerank/hyde defaults). Individual
          // overrides (retrieval_k, rerank_enabled) can still win if set.
          query_profile:
            settings.queryProfile && settings.queryProfile !== "balanced"
              ? settings.queryProfile
              : undefined,
          // rerank_enabled: explicit toggle from store overrides the profile preset.
          // settings.rerankingEnabled is a boolean in store; only send when it
          // diverges from the profile's expected value (let backend resolver
          // fill in the default most of the time).
          rerank_enabled:
            settings.rerankingEnabled === false ? false : undefined,
        },
        selected_tools: settings.selectedToolIds,
        // Phase 24 — Skills + Reasoning Cascade
        active_skill_ids:
          settings.selectedSkillIds && settings.selectedSkillIds.length > 0
            ? settings.selectedSkillIds
            : undefined,
        reasoning_cascade: settings.reasoningCascadeEnabled || undefined,
      };

      try {
        await api.streamChat(
          request,
          (event) => {
            switch (event.type) {
              case "token":
                if (event.content) chat.updateStreamingContent(event.content);
                break;
              case "thinking":
                if (event.thinking)
                  chat.updateStreamingThinking(event.thinking);
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
                // Surface as an italic notice inline in the assistant message
                const msg =
                  event.content ||
                  "Retrieval tier downgraded (strategy intersection).";
                chat.updateStreamingContent(`\n\n*⚠ ${msg}*\n\n`);
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
                // Render inline as an italic status line within the assistant message
                try {
                  const calls = JSON.parse(event.content || "[]") as Array<{
                    name: string;
                    args?: string;
                  }>;
                  for (const c of calls) {
                    chat.updateStreamingContent(
                      `\n\n*⚙ Running: \`${c.name}(${c.args ?? ""})\`*\n\n`,
                    );
                  }
                } catch {
                  chat.updateStreamingContent(
                    `\n\n*⚙ Running tool…*\n\n`,
                  );
                }
                break;
              }
              case "tool_result": {
                try {
                  const results = JSON.parse(event.content || "[]") as Array<{
                    name: string;
                    result: string;
                  }>;
                  for (const r of results) {
                    const preview =
                      r.result.length > 200
                        ? `${r.result.slice(0, 200)}…`
                        : r.result;
                    chat.updateStreamingContent(
                      `\n\n*✓ ${r.name} → ${preview}*\n\n`,
                    );
                  }
                } catch {
                  chat.updateStreamingContent(`\n\n*✓ Tool complete*\n\n`);
                }
                break;
              }
              case "error":
                chat.setError(event.content || "An error occurred");
                chat.stopStreaming();
                break;
              case "done": {
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
                  model_used: event.model_used,
                  created_at: new Date().toISOString(),
                  trimming_applied: event.trimming_applied,
                  collections_queried:
                    event.collections_queried ?? settings.selectedCorpusIds,
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
            chat.setError(err.message);
            chat.stopStreaming();
          },
        );
      } catch (err) {
        chat.setError(err instanceof Error ? err.message : "Unknown error");
        chat.stopStreaming();
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
    loadModels();
    useSettingsStore.getState().loadFromAPI();
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

  const loadModels = async () => {
    try {
      const data = await api.getModels();
      setModels(data.chat_models, data.embedding_models);
      if (data.chat_models.length > 0 && !selectedModel) {
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
    <div className="flex h-screen bg-bg-base text-text-primary overflow-hidden font-mono selection:bg-accent-main/30">
      {/* 1. LEFT PANE: Directory / Explorer */}
      <Sidebar
        isOpen={sidebarOpen}
        onToggle={() => setSidebarOpen(!sidebarOpen)}
      />

      {/* 2. CENTER PANE: Active Workspace (Graph/Chat) */}
      <main className="flex-1 flex flex-col min-w-0 border-r border-border-minimal relative bg-bg-surface">
        {/* Header - Terminal Status Bar + session controls */}
        <header className="h-24 border-b border-border-minimal flex items-center justify-between px-6 bg-bg-base z-30 shrink-0">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setSidebarOpen(true)}
              className="lg:hidden p-1 text-content-secondary hover:text-accent-main transition-none"
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

          <div className="flex items-center gap-3 flex-wrap justify-end">
            <CorpusMultiSelect />
            <div className="h-4 w-px bg-border-minimal" />
            <CollectionSelector collections={collections} />
            <div className="h-4 w-px bg-border-minimal" />
            <ModelSelector />
            <div className="h-4 w-px bg-border-minimal" />
            <ReasoningModeSelector />
            <div className="h-4 w-px bg-border-minimal" />
            <QueryProfileSelector />
            <div className="h-4 w-px bg-border-minimal" />
            <RetrievalTierSelector />
            <div className="h-4 w-px bg-border-minimal" />
            <button
              onClick={() => setIsGraphViewOpen(true)}
              className="p-1.5 border transition-none rounded-none border-transparent text-content-secondary hover:text-accent-main"
              title="Global Graph"
            >
              <Network className="w-4 h-4" />
            </button>
          </div>
        </header>

        {/* Content Area (Force-directed Graph / Chat View) */}
        <div className="flex-1 relative flex flex-col overflow-hidden bg-[radial-gradient(ellipse_at_center,_var(--bg-raised)_0%,_var(--bg-surface)_100%)]">
          <ChatWindow />
        </div>

        {pipelineStatus && (
          <div
            data-testid="pipeline-status"
            className="absolute bottom-32 left-1/2 -translate-x-1/2 bg-bg-surface border border-border-minimal px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-accent-main z-50">
            [ PIPELINE STATUS: {pipelineStatus} ]
            {/* Duplicate for older test locator compatibility */}
            <div data-testid="upload-status" className="hidden">{pipelineStatus}</div>
          </div>
        )}

        {/* Input Area - Command Line Interface */}
        <div className="shrink-0 p-4 bg-bg-base border-t border-border-minimal z-10">
          <div className="max-w-7xl mx-auto">
            <ChatInput
              onSend={handleSend}
              onFileUpload={handleFileUpload}
              isLoading={!modelsLoaded}
              placeholder={
                modelsLoaded
                  ? "EXECUTE QUERY // INJECT CONTEXT..."
                  : "INITIALIZING SYSTEMS..."
              }
              tokenCount={{ current: 0, max: maxTokens }}
            />
          </div>
        </div>
      </main>

      {/* Global Views & Modals */}
      {isGraphViewOpen && (
        <div className="fixed inset-0 z-50 bg-[#0a0a0a]/95">
          <div className="absolute inset-0 flex flex-col">
            {/* GraphViewer fills the canvas area */}
            <div className="flex-1 min-h-0">
              <GraphViewer
                mode={graphViewerMode}
                corpusIds={selectedCorpusIds}
                query={graphViewerMode === "query" ? graphViewerQuery : undefined}
                model={selectedModel || undefined}
                onRerun={
                  graphViewerMode === "query"
                    ? () => setGraphViewerRunCount((n) => n + 1)
                    : undefined
                }
                onClose={() => setIsGraphViewOpen(false)}
                key={`gv-${graphViewerMode}-${graphViewerQuery}-${graphViewerRunCount}`}
              />
            </div>
            {/* Query input bar — bottom-center, switches mode between
                brain and query. PR 4 of the multi-corpus rollout. */}
            <div className="border-t border-zinc-800 bg-zinc-950/90 backdrop-blur p-3 z-50">
              <form
                className="max-w-3xl mx-auto flex items-center gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  const q = graphViewerQueryDraft.trim();
                  if (!q) return;
                  setGraphViewerQuery(q);
                  setGraphViewerMode("query");
                  setGraphViewerRunCount((n) => n + 1);
                }}
              >
                <input
                  type="text"
                  value={graphViewerQueryDraft}
                  onChange={(e) => setGraphViewerQueryDraft(e.target.value)}
                  placeholder={
                    selectedCorpusIds.length === 0
                      ? "Select a corpus first…"
                      : graphViewerMode === "query"
                      ? "Ask another question across selected corpora…"
                      : "Ask the graph: how does X relate to Y across corpora?"
                  }
                  disabled={selectedCorpusIds.length === 0}
                  className="flex-1 bg-zinc-900 border border-zinc-800 rounded px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-amber-700 font-mono"
                />
                <button
                  type="submit"
                  disabled={
                    selectedCorpusIds.length === 0 || !graphViewerQueryDraft.trim()
                  }
                  className="text-[10px] uppercase tracking-widest text-zinc-200 hover:text-amber-400 border border-zinc-700 rounded px-3 py-2 font-mono disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Synthesize
                </button>
                {graphViewerMode === "query" && (
                  <button
                    type="button"
                    onClick={() => {
                      setGraphViewerMode("brain");
                      setGraphViewerQuery("");
                      setGraphViewerQueryDraft("");
                    }}
                    className="text-[10px] uppercase tracking-widest text-zinc-500 hover:text-zinc-200 border border-zinc-800 rounded px-3 py-2 font-mono"
                  >
                    Back to Brain
                  </button>
                )}
              </form>
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
