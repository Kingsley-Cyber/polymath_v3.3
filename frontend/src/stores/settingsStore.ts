// Settings Store - Zustand state management for all user settings
import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { SettingsState, Tool, ModelInfo, Skill } from "../types";
import * as api from "../lib/api";

const DEFAULT_SETTINGS: Omit<SettingsState, "selectedModel"> = {
  // Model Settings
  temperature: 0.7,
  topP: 0.9,
  maxTokens: 2048,

  // RAG Settings
  retrievalK: 60,
  retrievalSummaryK: 20,
  retrievalFinalK: 8,
  retrievalFactSeedLimit: 12,
  retrievalGraphExpansion: 24,
  graphQuerySeedEntities: 3,
  graphQueryMaxHops: 2,
  graphQueryNodeLimit: 80,
  vectorChildChunks: 70,
  vectorSummaries: 30,
  vectorFinalSources: 12,
  vectorReranker: true,
  hybridChildChunks: 60,
  hybridSummaries: 20,
  hybridFinalSources: 8,
  hybridReranker: true,
  graphChildChunks: 60,
  graphSummaries: 20,
  graphFactSeeds: 12,
  graphExpansion: 24,
  graphFinalSources: 8,
  graphReranker: true,
  hydeEnabled: false,
  webSearchEnabled: false,
  rerankingEnabled: true,
  selectedCollectionIds: [],
  retrievalTier: "qdrant_mongo",
  selectedCorpusIds: [],

  // UI Settings
  theme: "ayu-mirage",
  fontSize: "medium",
  reducedMotion: false,
  sidebarOpen: true,

  // Agent Tool Settings (Added)
  availableTools: [],
  selectedToolIds: [],

  // Agentic Mode (Phase 14.1) — defaults overridden on loadFromAPI()
  // Empty string means "not configured yet". ToggleBar renders NOT_SET
  // instead of a ghost model name when the user hasn't picked one.
  agenticModeEnabled: false,
  agenticModel: "",

  // Cloud embed mode (Phase 14.3) — read-only from backend
  modalEnabled: false,
  modalEmbedderUrl: "",

  // Reasoning modes (Phase 15) — per-query selection persists across sessions
  reasoningMode: "none",
  reasoningBlend: [],

  // Search mode dispatch (Phase 27) — "auto" lets the backend infer
  // local vs global from the query shape; "local" forces the full
  // pipeline (vector+BM25+graph+rerank+hydrate); "global" returns
  // summaries-only for thematic / corpus-wide questions.
  searchMode: "auto",

  // Thinking-effort dial (Phase 28) — per-turn reasoning depth for
  // models that expose it (OpenAI o-series, Claude, Gemini 2.5+,
  // DeepSeek-R1). "auto" → provider picks default; UI hides the
  // selector entirely for non-reasoning models.
  thinkingEffort: "auto",

  // HyDE (Phase 17) — cheap model override. Empty = fall back to backend
  // HYDE_MODEL env var.
  hydeModel: "",

  // Query Profile (Phase 18) — speed preset; default mirrors backend.
  queryProfile: "balanced",

  // Power-user reasoning (P5) — off by default; persists via partialize below
  powerUserReasoning: false,

  // Phase 24 — Skills (multi-select) + Reasoning Cascade (per-turn opt-in)
  availableSkills: [],
  selectedSkillIds: [],
  reasoningCascadeEnabled: false,
};

interface SettingsStore extends SettingsState {
  chatModels: ModelInfo[];
  embeddingModels: ModelInfo[];

  // Actions
  updateSettings: (settings: Partial<SettingsState>) => void;
  setModels: (chatModels: ModelInfo[], embeddingModels: ModelInfo[]) => void;
  setSelectedModel: (model: string) => void;
  loadTools: (tools: Tool[]) => void;
  toggleTool: (toolId: string) => void;
  // Phase 24 — Skills + Reasoning Cascade
  loadSkills: (skills: Skill[]) => void;
  toggleSkill: (skillId: string) => void;
  toggleReasoningCascade: () => void;
  toggleHyDE: () => void;
  toggleWebSearch: () => void;
  toggleReranking: () => void;
  toggleAgenticMode: () => Promise<void>;
  setAgenticModel: (model: string) => Promise<void>;
  setReasoningMode: (mode: string) => void;
  setReasoningBlend: (blend: string[]) => Promise<void>;
  setSearchMode: (mode: "auto" | "local" | "global") => void;
  setThinkingEffort: (effort: "auto" | "none" | "low" | "medium" | "high") => void;
  /** Phase 18 — per-query speed preset (client-only; read at send-time) */
  setQueryProfile: (profile: "fast" | "balanced" | "thorough" | "custom") => void;
  togglePowerUserReasoning: () => void;
  loadFromAPI: () => Promise<void>;
  toggleCollection: (collectionId: string) => void;
  selectAllCollections: (collectionIds: string[]) => void;
  clearCollections: () => void;
  setRetrievalTier: (tier: SettingsState["retrievalTier"]) => void;
  toggleCorpus: (corpusId: string) => void;
  /** Drop any selected corpus ids not in the given valid set. Called after
   *  corpora list loads so deleted corpora don't keep poisoning retrieval. */
  purgeStaleCorpusIds: (validIds: string[]) => void;
  setTheme: (theme: SettingsState["theme"]) => void;
  toggleSidebar: () => void;
  resetToDefaults: () => void;
}

export const useSettingsStore = create<SettingsStore>()(
  persist(
    (set, get) => ({
      ...DEFAULT_SETTINGS,
      selectedModel: "", // Will be populated from /api/models
      availableTools: [],
      selectedToolIds: [],
      chatModels: [],
      embeddingModels: [],

      updateSettings: (settings) => set((state) => ({ ...state, ...settings })),

      setSelectedModel: (model) => set({ selectedModel: model }),

      setModels: (chatModels, embeddingModels) =>
        set({ chatModels, embeddingModels }),

      loadTools: (tools) => set({ availableTools: tools }),

      // Phase 24 — Skills + Reasoning Cascade actions
      loadSkills: (skills) => set({ availableSkills: skills }),
      toggleSkill: (skillId) =>
        set((state) => {
          const sel = new Set(state.selectedSkillIds);
          if (sel.has(skillId)) sel.delete(skillId);
          else sel.add(skillId);
          return { selectedSkillIds: Array.from(sel) };
        }),
      toggleReasoningCascade: () =>
        set((state) => ({ reasoningCascadeEnabled: !state.reasoningCascadeEnabled })),

      toggleTool: (toolId) =>
        set((state) => {
          const selected = new Set(state.selectedToolIds);
          if (selected.has(toolId)) {
            selected.delete(toolId);
          } else {
            selected.add(toolId);
          }
          return { selectedToolIds: Array.from(selected) };
        }),

      toggleHyDE: () => set((state) => ({ hydeEnabled: !state.hydeEnabled })),

      toggleWebSearch: () =>
        set((state) => ({ webSearchEnabled: !state.webSearchEnabled })),

      toggleReranking: () =>
        set((state) => ({ rerankingEnabled: !state.rerankingEnabled })),

      toggleAgenticMode: async () => {
        const state = get();
        const next = !state.agenticModeEnabled;
        set({ agenticModeEnabled: next });
        try {
          await api.updateGlobalSettings({
            chat: {
              default_chat_model: state.selectedModel || "",
              max_context_tokens: state.maxTokens,
              max_completion_tokens: 2048,
              temperature: state.temperature,
              top_p: state.topP,
              agentic_mode_enabled: next,
              agentic_model: state.agenticModel,
              default_reasoning_mode: state.reasoningMode,
              reasoning_blend: state.reasoningBlend,
              hyde_model: state.hydeModel,
              query_profile: state.queryProfile,
            },
          });
        } catch (err) {
          console.error("Failed to persist agenticModeEnabled:", err);
        }
      },

      setAgenticModel: async (model: string) => {
        const state = get();
        set({ agenticModel: model });
        try {
          await api.updateGlobalSettings({
            chat: {
              default_chat_model: state.selectedModel || "",
              max_context_tokens: state.maxTokens,
              max_completion_tokens: 2048,
              temperature: state.temperature,
              top_p: state.topP,
              agentic_mode_enabled: state.agenticModeEnabled,
              agentic_model: model,
              default_reasoning_mode: state.reasoningMode,
              reasoning_blend: state.reasoningBlend,
              hyde_model: state.hydeModel,
              query_profile: state.queryProfile,
            },
          });
        } catch (err) {
          console.error("Failed to persist agenticModel:", err);
        }
      },

      setReasoningMode: (mode: string) => {
        // Per-query knob — purely client-side, read by App.tsx.handleSend at
        // send time and injected into ChatRequest.overrides.reasoning_mode.
        set({ reasoningMode: mode });
      },

      setSearchMode: (mode: "auto" | "local" | "global") => {
        // Per-query knob (Phase 27). Read by App.tsx.handleSend and
        // injected into ChatRequest.overrides.search_mode. "auto" lets
        // the backend infer; "local"/"global" override.
        set({ searchMode: mode });
      },

      setThinkingEffort: (
        effort: "auto" | "none" | "low" | "medium" | "high",
      ) => {
        // Per-turn knob (Phase 28). Read by App.tsx.handleSend and
        // injected into ChatRequest.overrides.thinking_effort. "auto"
        // means "omit the field and let the backend's thinking_mapper
        // pick the per-provider default". The selector is hidden in
        // the UI when the selected model has no thinking dial.
        set({ thinkingEffort: effort });
      },

      togglePowerUserReasoning: () =>
        set((state) => ({ powerUserReasoning: !state.powerUserReasoning })),

      setQueryProfile: (profile) => {
        // Phase 18 — per-query speed preset. Read at send-time and injected
        // into overrides.query_profile; the backend resolver (chat_orchestrator
        // ._resolve_query_profile) expands the preset into retrieval_k +
        // rerank_enabled + hyde_enabled.
        set({ queryProfile: profile });
      },

      setReasoningBlend: async (blend: string[]) => {
        const state = get();
        set({ reasoningBlend: blend });
        try {
          await api.updateGlobalSettings({
            chat: {
              default_chat_model: state.selectedModel || "",
              max_context_tokens: state.maxTokens,
              max_completion_tokens: 2048,
              temperature: state.temperature,
              top_p: state.topP,
              agentic_mode_enabled: state.agenticModeEnabled,
              agentic_model: state.agenticModel,
              default_reasoning_mode: state.reasoningMode,
              reasoning_blend: blend,
              hyde_model: state.hydeModel,
              query_profile: state.queryProfile,
            },
          });
        } catch (err) {
          console.error("Failed to persist reasoningBlend:", err);
        }
      },

      loadFromAPI: async () => {
        try {
          const resp = await api.getGlobalSettings();
          const s = resp.settings;

          // Map API retrieval_tier values → store retrievalTier values
          const tierMap: Record<string, SettingsState["retrievalTier"]> = {
            qdrant_only: "qdrant_only",
            qdrant_mongo: "qdrant_mongo",
            qdrant_mongo_graph: "qdrant_mongo_graph",
          };

          // Legacy defaults that must clear so Phase F pool resolution wins.
          // If the Mongo doc still carries the old hardcoded value AND the
          // user has a pool entry configured, prefer the pool (treat the
          // flat field as empty).
          const LEGACY_MODEL_DEFAULT = "ollama/llama3.2:3b";
          const stripLegacy = (m: string | null | undefined) =>
            !m || m === LEGACY_MODEL_DEFAULT ? "" : m;

          set({
            // Agentic mode (Phase 14.1)
            agenticModeEnabled: s.chat.agentic_mode_enabled,
            agenticModel: stripLegacy(s.chat.agentic_model),

            // Infrastructure
            modalEnabled: s.infrastructure.modal_enabled,
            modalEmbedderUrl: s.infrastructure.modal_embedder_url,

            // Chat defaults — sync API → store on first load
            selectedModel:
              get().selectedModel || s.chat.default_chat_model || "",
            temperature: s.chat.temperature ?? get().temperature,
            topP: s.chat.top_p ?? get().topP,
            maxTokens: s.chat.max_context_tokens ?? get().maxTokens,

            // Retrieval defaults — sync API → store on first load
            retrievalTier:
              tierMap[s.retrieval.default_tier] ?? get().retrievalTier,
            retrievalK: s.retrieval.top_k_child ?? get().retrievalK,
            retrievalSummaryK:
              s.retrieval.top_k_summary ?? get().retrievalSummaryK,
            retrievalFinalK: s.retrieval.final_top_k ?? get().retrievalFinalK,
            retrievalFactSeedLimit:
              s.retrieval.fact_seed_limit ?? get().retrievalFactSeedLimit,
            retrievalGraphExpansion:
              s.retrieval.neo4j_expansion_cap ?? get().retrievalGraphExpansion,
            graphQuerySeedEntities:
              s.retrieval.graph_query_seed_entities ??
              get().graphQuerySeedEntities,
            graphQueryMaxHops:
              s.retrieval.graph_query_max_hops ?? get().graphQueryMaxHops,
            graphQueryNodeLimit:
              s.retrieval.graph_query_node_limit ?? get().graphQueryNodeLimit,
            vectorChildChunks:
              s.retrieval.vector_child_chunks ?? get().vectorChildChunks,
            vectorSummaries:
              s.retrieval.vector_summaries ?? get().vectorSummaries,
            vectorFinalSources:
              s.retrieval.vector_final_sources ?? get().vectorFinalSources,
            vectorReranker:
              s.retrieval.vector_reranker ?? get().vectorReranker,
            hybridChildChunks:
              s.retrieval.hybrid_child_chunks ?? get().hybridChildChunks,
            hybridSummaries:
              s.retrieval.hybrid_summaries ?? get().hybridSummaries,
            hybridFinalSources:
              s.retrieval.hybrid_final_sources ?? get().hybridFinalSources,
            hybridReranker:
              s.retrieval.hybrid_reranker ?? get().hybridReranker,
            graphChildChunks:
              s.retrieval.graph_child_chunks ?? get().graphChildChunks,
            graphSummaries:
              s.retrieval.graph_summaries ?? get().graphSummaries,
            graphFactSeeds:
              s.retrieval.graph_fact_seeds ?? get().graphFactSeeds,
            graphExpansion:
              s.retrieval.graph_expansion ?? get().graphExpansion,
            graphFinalSources:
              s.retrieval.graph_final_sources ?? get().graphFinalSources,
            graphReranker:
              s.retrieval.graph_reranker ?? get().graphReranker,
            rerankingEnabled:
              s.retrieval.rerank_enabled ?? get().rerankingEnabled,

            // Reasoning modes (Phase 15) — seed current selection from server
            // default only on first load when the user hasn't chosen anything yet
            reasoningMode:
              get().reasoningMode !== "none"
                ? get().reasoningMode
                : s.chat.default_reasoning_mode ?? "none",
            reasoningBlend: s.chat.reasoning_blend ?? [],

            // HyDE (Phase 17) — strip legacy default so Phase F pool wins
            hydeModel: stripLegacy(s.chat.hyde_model ?? get().hydeModel),

            // Query Profile (Phase 18) — only seed from API if user hasn't
            // set one this session (preserves per-session selection).
            queryProfile:
              get().queryProfile !== "balanced"
                ? get().queryProfile
                : (s.chat.query_profile as any) ?? "balanced",
          });
        } catch (err) {
          console.warn("Settings API unavailable; using local defaults:", err);
        }
      },

      toggleCollection: (collectionId) =>
        set((state) => {
          const current = state.selectedCollectionIds;
          const exists = current.includes(collectionId);
          return {
            selectedCollectionIds: exists
              ? current.filter((id) => id !== collectionId)
              : [...current, collectionId],
          };
        }),

      selectAllCollections: (collectionIds) =>
        set({ selectedCollectionIds: collectionIds }),

      clearCollections: () => set({ selectedCollectionIds: [] }),

      setRetrievalTier: (tier) => set({ retrievalTier: tier }),

      toggleCorpus: (corpusId) =>
        set((state) => {
          const current = state.selectedCorpusIds;
          const exists = current.includes(corpusId);
          return {
            selectedCorpusIds: exists
              ? current.filter((id) => id !== corpusId)
              : [...current, corpusId],
          };
        }),

      purgeStaleCorpusIds: (validIds) =>
        set((state) => {
          const valid = new Set(validIds);
          const filtered = state.selectedCorpusIds.filter((id) => valid.has(id));
          if (filtered.length === state.selectedCorpusIds.length) return {};
          const dropped = state.selectedCorpusIds.filter((id) => !valid.has(id));
          console.warn("Purged stale corpus ids:", dropped);
          return { selectedCorpusIds: filtered };
        }),

      setTheme: (theme) => set({ theme }),

      toggleSidebar: () =>
        set((state) => ({ sidebarOpen: !state.sidebarOpen })),

      resetToDefaults: () =>
        set({ ...DEFAULT_SETTINGS, selectedModel: get().selectedModel }),
    }),
    {
      name: "polymath-settings",
      // Phase 23 — strip hardcoded legacy model defaults out of restored
      // localStorage state. Anyone who saved "ollama/llama3.2:3b" before the
      // hardcode removal would otherwise keep shadowing Phase F pool
      // resolution on the backend.
      version: 2,
      migrate: (persisted: unknown) => {
        const p = (persisted as Record<string, unknown>) || {};
        const LEGACY = "ollama/llama3.2:3b";
        const strip = (v: unknown) => (v === LEGACY ? "" : v);
        return {
          ...p,
          agenticModel: strip(p.agenticModel),
          hydeModel: strip(p.hydeModel),
        };
      },
      partialize: (state) => ({
        temperature: state.temperature,
        topP: state.topP,
        maxTokens: state.maxTokens,
        hydeEnabled: state.hydeEnabled,
        webSearchEnabled: state.webSearchEnabled,
        rerankingEnabled: state.rerankingEnabled,
        reasoningMode: state.reasoningMode,
        searchMode: state.searchMode,
        thinkingEffort: state.thinkingEffort,
        theme: state.theme,
        fontSize: state.fontSize,
        reducedMotion: state.reducedMotion,
        sidebarOpen: state.sidebarOpen,
        selectedModel: state.selectedModel,
        selectedToolIds: state.selectedToolIds,
        retrievalTier: state.retrievalTier,
        retrievalK: state.retrievalK,
        retrievalSummaryK: state.retrievalSummaryK,
        retrievalFinalK: state.retrievalFinalK,
        retrievalFactSeedLimit: state.retrievalFactSeedLimit,
        retrievalGraphExpansion: state.retrievalGraphExpansion,
        graphQuerySeedEntities: state.graphQuerySeedEntities,
        graphQueryMaxHops: state.graphQueryMaxHops,
        graphQueryNodeLimit: state.graphQueryNodeLimit,
        vectorChildChunks: state.vectorChildChunks,
        vectorSummaries: state.vectorSummaries,
        vectorFinalSources: state.vectorFinalSources,
        vectorReranker: state.vectorReranker,
        hybridChildChunks: state.hybridChildChunks,
        hybridSummaries: state.hybridSummaries,
        hybridFinalSources: state.hybridFinalSources,
        hybridReranker: state.hybridReranker,
        graphChildChunks: state.graphChildChunks,
        graphSummaries: state.graphSummaries,
        graphFactSeeds: state.graphFactSeeds,
        graphExpansion: state.graphExpansion,
        graphFinalSources: state.graphFinalSources,
        graphReranker: state.graphReranker,
        selectedCorpusIds: state.selectedCorpusIds,
        agenticModeEnabled: state.agenticModeEnabled,
        agenticModel: state.agenticModel,
        // reasoningMode already included above; also persist the blend
        reasoningBlend: state.reasoningBlend,
        // HyDE (Phase 17)
        hydeModel: state.hydeModel,
        // Query Profile (Phase 18) — persists across reload
        queryProfile: state.queryProfile,
        // Power-user reasoning (P5) — persists across reload
        powerUserReasoning: state.powerUserReasoning,
        // Phase 24 — selected skills + reasoning cascade per-turn pref
        selectedSkillIds: state.selectedSkillIds,
        reasoningCascadeEnabled: state.reasoningCascadeEnabled,
      }),
    },
  ),
);
