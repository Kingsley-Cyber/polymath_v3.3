// RetrievalSettingsTab.tsx — Editable retrieval params form → PUT /api/settings
import { useState, useEffect, useCallback, FormEvent } from "react";
import {
  Save,
  Loader2,
  CheckCircle,
  AlertTriangle,
  Settings2,
  Gauge,
} from "lucide-react";
import * as api from "../../lib/api";
import { useSettingsStore } from "../../stores/settingsStore";
import type { RetrievalSettings, GlobalSettingsUpdate } from "../../types";

const RETRIEVAL_TIERS = [
  {
    value: "qdrant_only",
    label: "Qdrant Only",
    hint: "Vector search only — fastest",
  },
  {
    value: "qdrant_mongo",
    label: "Qdrant + Mongo",
    hint: "Vector search + full-text hydration",
  },
  {
    value: "qdrant_mongo_graph",
    label: "Qdrant + Mongo + Graph",
    hint: "All stores — slowest, most context",
  },
] as const;

export function RetrievalSettingsTab() {
  const { loadFromAPI, queryProfile, setQueryProfile } = useSettingsStore();
  const isCustomActive = queryProfile === "custom";

  const [form, setForm] = useState<RetrievalSettings>({
    default_tier: "qdrant_mongo",
    top_k_child: 30,
    top_k_summary: 10,
    reranker_model: "cross-encoder/ms-marco-MiniLM-L6-v2",
    rerank_top_n: 40,
    rerank_enabled: true,
    similarity_threshold: 0.7,
    max_corpora_per_query: 32,
    neo4j_expansion_cap: 20,
    final_top_k: 8,
  });

  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [isDirty, setIsDirty] = useState(false);

  // Load current settings
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await api.getGlobalSettings();
        if (!cancelled) {
          setForm(resp.settings.retrieval);
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
    setForm((prev) => ({ ...prev, [key]: value }));
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
        const patch: GlobalSettingsUpdate = { retrieval: form };
        await api.updateGlobalSettings(patch);
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

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-[12px] text-gray-500">
        <Loader2 className="w-4 h-4 animate-spin mr-2" />
        Loading retrieval settings…
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold text-white mb-2">Retrieval</h2>
        <p className="text-[13px] text-gray-500">
          <span className="text-cyan-400 font-semibold">Final K</span> applies to every
          speed profile. The other advanced values drive the{" "}
          <span className="text-cyan-400 font-semibold">Custom</span> query profile.
        </p>
      </div>

      {/* Active / Inactive badge */}
      <div
        className={`flex items-start gap-3 border rounded-lg px-4 py-3 ${
          isCustomActive
            ? "border-cyan-500/40 bg-cyan-500/5"
            : "border-amber-400/30 bg-amber-400/5"
        }`}
      >
        <Gauge
          className={`w-4 h-4 mt-0.5 shrink-0 ${
            isCustomActive ? "text-cyan-400" : "text-amber-400"
          }`}
        />
        <div className="flex-1">
          {isCustomActive ? (
            <p className="text-[13px] text-cyan-200">
              <span className="font-bold">Custom profile is active.</span>{" "}
              All saved values below apply on every query.
            </p>
          ) : (
            <p className="text-[13px] text-amber-200/90">
              <span className="font-bold">
                Custom profile is not active ({queryProfile}).
              </span>{" "}
              Final K still controls chunks sent to the LLM; the other
              advanced sliders wait until you pick Custom in the chat
              header's SPEED selector.{" "}
              <button
                type="button"
                onClick={() => setQueryProfile("custom")}
                className="text-cyan-400 hover:text-cyan-300 underline"
              >
                Switch now
              </button>
            </p>
          )}
        </div>
      </div>


      {/* Status Messages */}
      {saveSuccess && (
        <div className="flex items-center gap-3 border border-emerald-500/30 bg-emerald-500/5 px-4 py-3 rounded-lg">
          <CheckCircle className="w-4 h-4 text-emerald-400 shrink-0" />
          <p className="text-[13px] text-emerald-300">
            Retrieval settings saved.
          </p>
        </div>
      )}

      {saveError && (
        <div className="flex items-start gap-3 border border-red-500/30 bg-red-500/5 px-4 py-3 rounded-lg">
          <AlertTriangle className="w-4 h-4 text-red-400 mt-0.5 shrink-0" />
          <div>
            <p className="text-[11px] font-bold uppercase tracking-widest text-red-400">
              Save Failed
            </p>
            <p className="text-[13px] text-red-300/80 mt-1">{saveError}</p>
          </div>
        </div>
      )}

      <form onSubmit={handleSave} className="space-y-6">
        {/* Retrieval Tier */}
        <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-4">
          <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
            <Settings2 size={16} className="text-cyan-400" /> Retrieval Strategy
          </h3>

          <div>
            <label className="block text-[12px] font-medium text-gray-400 mb-2">
              Default Tier
            </label>
            <div className="space-y-2">
              {RETRIEVAL_TIERS.map((tier) => (
                <label
                  key={tier.value}
                  className={`flex items-start gap-3 p-3 border rounded cursor-pointer transition-colors ${
                    form.default_tier === tier.value
                      ? "border-cyan-500/50 bg-cyan-500/5"
                      : "border-white/5 hover:border-white/10"
                  }`}
                >
                  <input
                    type="radio"
                    name="default_tier"
                    value={tier.value}
                    checked={form.default_tier === tier.value}
                    onChange={() => updateField("default_tier", tier.value)}
                    className="mt-0.5 accent-cyan-500"
                  />
                  <div>
                    <div className="text-[13px] text-white">{tier.label}</div>
                    <div className="text-[11px] text-gray-500">{tier.hint}</div>
                  </div>
                </label>
              ))}
            </div>
          </div>
        </div>

        {/* Top-K Settings */}
        <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-4">
          <h3 className="text-[15px] font-semibold text-white">Result Count</h3>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-[12px] font-medium text-gray-400 mb-1">
                Top-K Child Chunks
              </label>
              <input
                type="number"
                min={1}
                max={100}
                value={form.top_k_child}
                onChange={(e) =>
                  updateField("top_k_child", parseInt(e.target.value) || 30)
                }
                className="w-full bg-[#1a1a1a] border border-white/10 rounded px-3 py-2 text-[13px] text-white focus:outline-none focus:border-cyan-500"
              />
              <p className="text-[10px] text-gray-600 mt-1">1–100</p>
            </div>

            <div>
              <label className="block text-[12px] font-medium text-gray-400 mb-1">
                Top-K Summaries
              </label>
              <input
                type="number"
                min={1}
                max={50}
                value={form.top_k_summary}
                onChange={(e) =>
                  updateField("top_k_summary", parseInt(e.target.value) || 10)
                }
                className="w-full bg-[#1a1a1a] border border-white/10 rounded px-3 py-2 text-[13px] text-white focus:outline-none focus:border-cyan-500"
              />
              <p className="text-[10px] text-gray-600 mt-1">1–50</p>
            </div>

            <div>
              <label className="block text-[12px] font-medium text-gray-400 mb-1">
                Final K{" "}
                <span className="text-gray-500 font-normal">
                  (chunks fed to LLM after rerank)
                </span>
              </label>
              <input
                type="number"
                min={1}
                max={50}
                value={form.final_top_k}
                onChange={(e) =>
                  updateField("final_top_k", parseInt(e.target.value) || 8)
                }
                className="w-full bg-[#1a1a1a] border border-white/10 rounded px-3 py-2 text-[13px] text-white focus:outline-none focus:border-cyan-500"
              />
              <p className="text-[10px] text-gray-600 mt-1">
                1–50 · Top-K-Child fills the pre-rerank pool; Final K is what
                actually reaches the LLM across all speed profiles. Sweet
                spot: 6–10.
              </p>
            </div>

            <div>
              <label className="block text-[12px] font-medium text-gray-400 mb-1">
                Max Corpora per Query
              </label>
              <input
                type="number"
                min={1}
                max={64}
                value={form.max_corpora_per_query}
                onChange={(e) =>
                  updateField(
                    "max_corpora_per_query",
                    parseInt(e.target.value) || 32,
                  )
                }
                className="w-full bg-[#1a1a1a] border border-white/10 rounded px-3 py-2 text-[13px] text-white focus:outline-none focus:border-cyan-500"
              />
              <p className="text-[10px] text-gray-600 mt-1">1–64</p>
            </div>

            <div>
              <label className="block text-[12px] font-medium text-gray-400 mb-1">
                Neo4j Expansion Cap
              </label>
              <input
                type="number"
                min={0}
                max={100}
                value={form.neo4j_expansion_cap}
                onChange={(e) =>
                  updateField(
                    "neo4j_expansion_cap",
                    parseInt(e.target.value) || 20,
                  )
                }
                className="w-full bg-[#1a1a1a] border border-white/10 rounded px-3 py-2 text-[13px] text-white focus:outline-none focus:border-cyan-500"
              />
              <p className="text-[10px] text-gray-600 mt-1">0 = disabled</p>
            </div>
          </div>
        </div>

        {/* Reranker */}
        <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-[15px] font-semibold text-white">Reranker</h3>
            <button
              type="button"
              onClick={() =>
                updateField("rerank_enabled", !form.rerank_enabled)
              }
              className={`relative w-11 h-6 rounded-full transition-colors ${
                form.rerank_enabled ? "bg-cyan-500" : "bg-gray-600"
              }`}
              aria-label="Toggle reranker"
            >
              <span
                className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
                  form.rerank_enabled ? "translate-x-[22px]" : "translate-x-0.5"
                }`}
              />
            </button>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-[12px] font-medium text-gray-400 mb-1">
                Reranker Model
              </label>
              <input
                type="text"
                value={form.reranker_model}
                onChange={(e) => updateField("reranker_model", e.target.value)}
                className="w-full bg-[#1a1a1a] border border-white/10 rounded px-3 py-2 text-[13px] text-white font-mono focus:outline-none focus:border-cyan-500"
              />
            </div>

            <div>
              <label className="block text-[12px] font-medium text-gray-400 mb-1">
                Rerank Top-N
              </label>
              <input
                type="number"
                min={1}
                max={200}
                value={form.rerank_top_n}
                onChange={(e) =>
                  updateField("rerank_top_n", parseInt(e.target.value) || 40)
                }
                className="w-full bg-[#1a1a1a] border border-white/10 rounded px-3 py-2 text-[13px] text-white focus:outline-none focus:border-cyan-500"
              />
              <p className="text-[10px] text-gray-600 mt-1">
                Candidates fed to reranker
              </p>
            </div>
          </div>
        </div>

        {/* Similarity Threshold */}
        <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-4">
          <h3 className="text-[15px] font-semibold text-white">
            Similarity Threshold
          </h3>

          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-[12px] font-medium text-gray-400">
                Minimum Score
              </label>
              <span className="text-[13px] text-white font-mono">
                {form.similarity_threshold.toFixed(2)}
              </span>
            </div>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={form.similarity_threshold}
              onChange={(e) =>
                updateField("similarity_threshold", parseFloat(e.target.value))
              }
              className="w-full accent-cyan-500"
            />
            <div className="flex justify-between text-[10px] text-gray-600 mt-1">
              <span>0.00 (all)</span>
              <span>0.50</span>
              <span>1.00 (exact)</span>
            </div>
          </div>
        </div>

        {/* Save Button */}
        <div className="flex items-center gap-3">
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
            {isSaving ? "Saving…" : "Save Retrieval Settings"}
          </button>

          {isDirty && !isSaving && (
            <span className="text-[11px] text-amber-400">Unsaved changes</span>
          )}
        </div>
      </form>
    </div>
  );
}
