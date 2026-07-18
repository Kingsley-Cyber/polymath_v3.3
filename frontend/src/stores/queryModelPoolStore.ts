// Sprint 3 — unified query_model_pool store.
//
// Replaces useModelPoolStore + useModelProfilesStore for the settings UI.
// Backed by `/api/settings/models` (single GET / POST endpoint) + dedicated
// bulk-ollama and delete endpoints. Persists the last-loaded config to
// localStorage so the chat model dropdown has something to render while
// the GET is in flight.
//
// The chat `ModelSelector` still reads from the two legacy stores this
// sprint (4B). Sprint 4C switches it over to `useQueryModelPoolStore` and
// drops the two legacy stores + their types.

import { create } from "zustand";
import { persist } from "zustand/middleware";
import * as api from "../lib/api";
import type {
  AgenticConfig,
  GraphQueryConfig,
  HydeConfig,
  ModelsConfig,
  QueryModelPoolEntry,
  ReasoningConfig,
  SynthesisConfig,
  UtilityConfig,
} from "../types";

const EMPTY_CONFIG: ModelsConfig = {
  query_model_pool: [],
  hyde: { default_enabled: false, pool_entry_id: null },
  agentic: { default_enabled: false, pool_entry_id: null },
  // Phase 24 — Reasoning Cascade target. Disabled + unset by default.
  reasoning: { default_enabled: false, pool_entry_id: null },
  utility: { default_enabled: false, pool_entry_id: null },
  graph_query: { pool_entry_id: null },
  synthesis: { pool_entry_id: null },
};

interface QueryModelPoolState {
  config: ModelsConfig;
  loading: boolean;
  error: string | null;
  /** True between a local edit and the next successful save. */
  dirty: boolean;

  load: () => Promise<void>;
  /** Replace the WHOLE models subdoc (pool + hyde + agentic). Mark dirty. */
  setConfig: (next: ModelsConfig) => void;
  patchHyde: (patch: Partial<HydeConfig>) => void;
  patchAgentic: (patch: Partial<AgenticConfig>) => void;
  patchReasoning: (patch: Partial<ReasoningConfig>) => void;
  patchUtility: (patch: Partial<UtilityConfig>) => void;
  patchGraphQuery: (patch: Partial<GraphQueryConfig>) => void;
  patchSynthesis: (patch: Partial<SynthesisConfig>) => void;
  /** Append an entry locally. Dirty until save(). */
  addEntry: (entry: QueryModelPoolEntry) => void;
  /** Toggle an entry's `enabled` flag. */
  toggleEntry: (entryId: string) => void;
  /** POST the current config to the backend. Clears dirty on success. */
  save: () => Promise<void>;
  /** DELETE one entry via the dedicated endpoint. Non-dirty — writes through. */
  deleteEntry: (entryId: string) => Promise<void>;
  /** Bulk-add ollama entries via the dedicated endpoint. Writes through. */
  bulkAddOllama: (modelNames: string[]) => Promise<void>;
  reset: () => void;
}

export const useQueryModelPoolStore = create<QueryModelPoolState>()(
  persist(
    (set, get) => ({
      config: EMPTY_CONFIG,
      loading: false,
      error: null,
      dirty: false,

      load: async () => {
        set({ loading: true, error: null });
        try {
          const config = await api.getModelsSettings();
          set({ config, loading: false, dirty: false });
        } catch (e) {
          set({
            loading: false,
            error: e instanceof Error ? e.message : String(e),
          });
        }
      },

      setConfig: (next) => set({ config: next, dirty: true }),

      patchHyde: (patch) =>
        set((s) => ({
          config: { ...s.config, hyde: { ...s.config.hyde, ...patch } },
          dirty: true,
        })),

      patchAgentic: (patch) =>
        set((s) => ({
          config: { ...s.config, agentic: { ...s.config.agentic, ...patch } },
          dirty: true,
        })),

      patchReasoning: (patch) =>
        set((s) => ({
          config: {
            ...s.config,
            reasoning: { ...(s.config.reasoning || { default_enabled: false, pool_entry_id: null }), ...patch },
          },
          dirty: true,
        })),

      patchUtility: (patch) =>
        set((s) => ({
          config: {
            ...s.config,
            utility: { ...(s.config.utility || { default_enabled: false, pool_entry_id: null }), ...patch },
          },
          dirty: true,
        })),

      patchGraphQuery: (patch) =>
        set((s) => ({
          config: {
            ...s.config,
            graph_query: { ...(s.config.graph_query || { pool_entry_id: null }), ...patch },
          },
          dirty: true,
        })),

      patchSynthesis: (patch) =>
        set((s) => ({
          config: {
            ...s.config,
            synthesis: {
              ...(s.config.synthesis || { pool_entry_id: null }),
              ...patch,
            },
          },
          dirty: true,
        })),

      addEntry: (entry) =>
        set((s) => ({
          config: {
            ...s.config,
            query_model_pool: [...s.config.query_model_pool, entry],
          },
          dirty: true,
        })),

      toggleEntry: (entryId) =>
        set((s) => ({
          config: {
            ...s.config,
            query_model_pool: s.config.query_model_pool.map((e) =>
              e.entry_id === entryId ? { ...e, enabled: !e.enabled } : e,
            ),
          },
          dirty: true,
        })),

      save: async () => {
        const { config } = get();
        set({ loading: true, error: null });
        try {
          const saved = await api.updateModelsSettings(config);
          set({ config: saved, loading: false, dirty: false });
        } catch (e) {
          set({
            loading: false,
            error: e instanceof Error ? e.message : String(e),
          });
          throw e;
        }
      },

      deleteEntry: async (entryId) => {
        const updated = await api.deletePoolEntry(entryId);
        set({ config: updated, dirty: false });
      },

      bulkAddOllama: async (modelNames) => {
        if (modelNames.length === 0) return;
        const updated = await api.addOllamaToPool(modelNames);
        set({ config: updated, dirty: false });
      },

      reset: () => set({ config: EMPTY_CONFIG, dirty: false, error: null }),
    }),
    {
      name: "polymath-query-model-pool",
      partialize: (s) => ({ config: s.config }),
    },
  ),
);
