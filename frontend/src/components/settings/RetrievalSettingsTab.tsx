// RetrievalSettingsTab.tsx — core retrieval-shape controls.
import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  Boxes,
  CheckCircle,
  ChevronDown,
  Database,
  Loader2,
  Network,
  Save,
  Search,
} from "lucide-react";
import * as api from "../../lib/api";
import { useSettingsStore } from "../../stores/settingsStore";
import type { GlobalSettingsUpdate, RetrievalSettings } from "../../types";

type PanelId = "vector" | "hybrid" | "graph" | "graphQuery";

const BEST_DEFAULTS: RetrievalSettings = {
  default_tier: "qdrant_mongo",
  top_k_child: 60,
  top_k_summary: 20,
  reranker_model: "cross-encoder/ms-marco-MiniLM-L6-v2",
  rerank_top_n: 40,
  rerank_enabled: true,
  similarity_threshold: 0,
  max_corpora_per_query: 32,
  neo4j_expansion_cap: 24,
  final_top_k: 8,
  fact_seed_limit: 12,
  vector_child_chunks: 70,
  vector_summaries: 30,
  vector_final_sources: 12,
  vector_reranker: true,
  hybrid_child_chunks: 60,
  hybrid_summaries: 20,
  hybrid_final_sources: 8,
  hybrid_reranker: true,
  graph_child_chunks: 60,
  graph_summaries: 20,
  graph_fact_seeds: 12,
  graph_expansion: 24,
  graph_final_sources: 8,
  graph_reranker: true,
  graph_query_seed_entities: 3,
  graph_query_max_hops: 2,
  graph_query_node_limit: 80,
};

const PANEL_META: Record<
  PanelId,
  { title: string; subtitle: string; icon: typeof Database }
> = {
  vector: {
    title: "Vector Base",
    subtitle: "Wide Qdrant child + summary retrieval",
    icon: Database,
  },
  hybrid: {
    title: "Hybrid",
    subtitle: "Vector, summary, lexical, and hydration",
    icon: Boxes,
  },
  graph: {
    title: "Graph Augmented",
    subtitle: "Hybrid retrieval with fact seeds and expansion",
    icon: Network,
  },
  graphQuery: {
    title: "Graph Query",
    subtitle: "Entity seeds and topology breadth",
    icon: Search,
  },
};

function mergeDefaults(settings: RetrievalSettings): RetrievalSettings {
  return { ...BEST_DEFAULTS, ...settings, similarity_threshold: 0 };
}

function normalizeForSave(settings: RetrievalSettings): RetrievalSettings {
  return {
    ...mergeDefaults(settings),
    top_k_child: settings.hybrid_child_chunks ?? BEST_DEFAULTS.hybrid_child_chunks,
    top_k_summary: settings.hybrid_summaries ?? BEST_DEFAULTS.hybrid_summaries,
    final_top_k: settings.hybrid_final_sources ?? BEST_DEFAULTS.hybrid_final_sources,
    rerank_enabled: settings.hybrid_reranker ?? BEST_DEFAULTS.hybrid_reranker,
    fact_seed_limit: settings.graph_fact_seeds ?? BEST_DEFAULTS.graph_fact_seeds,
    neo4j_expansion_cap: settings.graph_expansion ?? BEST_DEFAULTS.graph_expansion,
    rerank_top_n: 40,
    similarity_threshold: 0,
    max_corpora_per_query: 32,
  };
}

function NumberField({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="block">
      <span className="block text-[11px] font-semibold uppercase tracking-widest text-gray-500 mb-1">
        {label}
      </span>
      <input
        type="number"
        min={min}
        max={max}
        placeholder={String(value)}
        value={value}
        onChange={(e) => {
          const parsed = Number.parseInt(e.target.value, 10);
          onChange(Number.isFinite(parsed) ? parsed : min);
        }}
        className="w-full bg-[#1a1a1a] border border-white/10 rounded px-3 py-2 text-[13px] text-white focus:outline-none focus:border-cyan-500"
      />
    </label>
  );
}

function RerankerSwitch({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border border-white/5 bg-[#1f1f1f] rounded px-3 py-2">
      <span className="text-[11px] font-semibold uppercase tracking-widest text-gray-500">
        Reranker
      </span>
      <button
        type="button"
        onClick={() => onChange(!checked)}
        className={`relative w-11 h-6 rounded-full transition-colors ${
          checked ? "bg-cyan-500" : "bg-gray-600"
        }`}
        aria-label="Toggle reranker"
      >
        <span
          className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
            checked ? "translate-x-[22px]" : "translate-x-0.5"
          }`}
        />
      </button>
    </div>
  );
}

export function RetrievalSettingsTab() {
  const { loadFromAPI } = useSettingsStore();
  const [form, setForm] = useState<RetrievalSettings>(BEST_DEFAULTS);
  const [openPanel, setOpenPanel] = useState<PanelId | null>("vector");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [isDirty, setIsDirty] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await api.getGlobalSettings();
        if (!cancelled) {
          setForm(mergeDefaults(resp.settings.retrieval));
        }
      } catch (err) {
        if (!cancelled) {
          console.error("Failed to load retrieval settings:", err);
        }
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const updateField = <K extends keyof RetrievalSettings>(
    key: K,
    value: RetrievalSettings[K],
  ) => {
    setForm((prev) => ({ ...prev, [key]: value, similarity_threshold: 0 }));
    setIsDirty(true);
    setSaveSuccess(false);
    setSaveError(null);
  };

  const handleSave = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setIsSaving(true);
      setSaveError(null);
      setSaveSuccess(false);

      try {
        const normalized = normalizeForSave(form);
        const patch: GlobalSettingsUpdate = { retrieval: normalized };
        await api.updateGlobalSettings(patch);
        setForm(normalized);
        await loadFromAPI();
        setSaveSuccess(true);
        setIsDirty(false);
        setTimeout(() => setSaveSuccess(false), 3000);
      } catch (err) {
        setSaveError(
          err instanceof Error
            ? err.message
            : "Failed to save retrieval settings",
        );
      } finally {
        setIsSaving(false);
      }
    },
    [form, loadFromAPI],
  );

  const renderPanelBody = (panel: PanelId) => {
    if (panel === "vector") {
      return (
        <div className="grid grid-cols-2 gap-4">
          <NumberField
            label="Child chunks"
            min={1}
            max={150}
            value={form.vector_child_chunks}
            onChange={(value) => updateField("vector_child_chunks", value)}
          />
          <NumberField
            label="Summaries"
            min={0}
            max={100}
            value={form.vector_summaries}
            onChange={(value) => updateField("vector_summaries", value)}
          />
          <NumberField
            label="Final sources"
            min={1}
            max={50}
            value={form.vector_final_sources}
            onChange={(value) => updateField("vector_final_sources", value)}
          />
          <RerankerSwitch
            checked={form.vector_reranker}
            onChange={(value) => updateField("vector_reranker", value)}
          />
        </div>
      );
    }

    if (panel === "hybrid") {
      return (
        <div className="grid grid-cols-2 gap-4">
          <NumberField
            label="Child chunks"
            min={1}
            max={150}
            value={form.hybrid_child_chunks}
            onChange={(value) => updateField("hybrid_child_chunks", value)}
          />
          <NumberField
            label="Summaries"
            min={0}
            max={100}
            value={form.hybrid_summaries}
            onChange={(value) => updateField("hybrid_summaries", value)}
          />
          <NumberField
            label="Final sources"
            min={1}
            max={50}
            value={form.hybrid_final_sources}
            onChange={(value) => updateField("hybrid_final_sources", value)}
          />
          <RerankerSwitch
            checked={form.hybrid_reranker}
            onChange={(value) => updateField("hybrid_reranker", value)}
          />
        </div>
      );
    }

    if (panel === "graph") {
      return (
        <div className="grid grid-cols-2 gap-4">
          <NumberField
            label="Child chunks"
            min={1}
            max={150}
            value={form.graph_child_chunks}
            onChange={(value) => updateField("graph_child_chunks", value)}
          />
          <NumberField
            label="Summaries"
            min={0}
            max={100}
            value={form.graph_summaries}
            onChange={(value) => updateField("graph_summaries", value)}
          />
          <NumberField
            label="Fact seeds"
            min={0}
            max={50}
            value={form.graph_fact_seeds}
            onChange={(value) => updateField("graph_fact_seeds", value)}
          />
          <NumberField
            label="Graph expansion"
            min={0}
            max={100}
            value={form.graph_expansion}
            onChange={(value) => updateField("graph_expansion", value)}
          />
          <NumberField
            label="Final sources"
            min={1}
            max={50}
            value={form.graph_final_sources}
            onChange={(value) => updateField("graph_final_sources", value)}
          />
          <RerankerSwitch
            checked={form.graph_reranker}
            onChange={(value) => updateField("graph_reranker", value)}
          />
        </div>
      );
    }

    return (
      <div className="grid grid-cols-3 gap-4">
        <NumberField
          label="Seed entities"
          min={1}
          max={10}
          value={form.graph_query_seed_entities}
          onChange={(value) => updateField("graph_query_seed_entities", value)}
        />
        <NumberField
          label="Max hops"
          min={1}
          max={3}
          value={form.graph_query_max_hops}
          onChange={(value) => updateField("graph_query_max_hops", value)}
        />
        <NumberField
          label="Node limit"
          min={1}
          max={200}
          value={form.graph_query_node_limit}
          onChange={(value) => updateField("graph_query_node_limit", value)}
        />
      </div>
    );
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-[12px] text-gray-500">
        <Loader2 className="w-4 h-4 animate-spin mr-2" />
        Loading retrieval settings...
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-white mb-2">Retrieval</h2>
        <p className="text-[13px] text-gray-500">
          Core gather and filter controls for chat retrieval and Graph View
          query.
        </p>
      </div>

      {saveSuccess && (
        <div className="flex items-center gap-3 border border-emerald-500/30 bg-emerald-500/5 px-4 py-3 rounded-lg">
          <CheckCircle className="w-4 h-4 text-emerald-400 shrink-0" />
          <p className="text-[13px] text-emerald-300">
            Retrieval settings saved.
          </p>
        </div>
      )}

      {saveError && (
        <div className="border border-red-500/30 bg-red-500/5 px-4 py-3 rounded-lg">
          <p className="text-[13px] text-red-300">{saveError}</p>
        </div>
      )}

      <form onSubmit={handleSave} className="space-y-4">
        {(Object.keys(PANEL_META) as PanelId[]).map((panel) => {
          const meta = PANEL_META[panel];
          const Icon = meta.icon;
          const isOpen = openPanel === panel;
          return (
            <section
              key={panel}
              className="border border-white/5 bg-[#2a2a2a] rounded-lg overflow-hidden"
            >
              <button
                type="button"
                onClick={() => setOpenPanel(isOpen ? null : panel)}
                className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-white/[0.03]"
              >
                <Icon className="w-4 h-4 text-cyan-400 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] font-bold uppercase tracking-widest text-white">
                    {meta.title}
                  </div>
                  <div className="text-[11px] text-gray-500 truncate">
                    {meta.subtitle}
                  </div>
                </div>
                <ChevronDown
                  className={`w-4 h-4 text-gray-500 transition-transform ${
                    isOpen ? "rotate-180" : ""
                  }`}
                />
              </button>
              {isOpen && (
                <div className="border-t border-white/5 px-4 py-4">
                  {renderPanelBody(panel)}
                </div>
              )}
            </section>
          );
        })}

        <div className="flex items-center gap-3 pt-2">
          <button
            type="submit"
            disabled={!isDirty || isSaving}
            className="flex items-center gap-2 px-4 py-2 text-[12px] font-bold uppercase tracking-widest bg-cyan-600 hover:bg-cyan-500 disabled:bg-gray-700 disabled:cursor-not-allowed text-white rounded transition-colors"
          >
            {isSaving ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Save className="w-3.5 h-3.5" />
            )}
            {isSaving ? "Saving..." : "Save Retrieval Settings"}
          </button>

          {isDirty && !isSaving && (
            <span className="text-[11px] text-amber-400">Unsaved changes</span>
          )}
        </div>
      </form>
    </div>
  );
}
