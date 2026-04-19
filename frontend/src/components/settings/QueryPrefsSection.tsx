// Phase F — Query Preferences section.
// Renders ABOVE the existing API Keys panel inside the API Keys tab.
//
// Two sub-panels:
//   A. Role → Pool entry mapping (3 selectors: HyDE / Agentic / Query default)
//      Pool chips themselves live in model_pool — this UI references them by
//      entry_id. Edit chips via the existing Model Pool UI (linked from here).
//   B. Ollama installed-model exclusions — per-user filter that hides chosen
//      ollama models from the global /api/models response.

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Cpu, Loader2, RefreshCw } from "lucide-react";

import * as api from "../../lib/api";
import type { ModelPoolEntry, QueryPrefs } from "../../types";

type Role = "hyde" | "agentic" | "query";

const ROLE_FIELDS: Record<Role, keyof Pick<QueryPrefs, "hyde_pool_id" | "agentic_pool_id" | "query_pool_id">> = {
  hyde: "hyde_pool_id",
  agentic: "agentic_pool_id",
  query: "query_pool_id",
};

interface OllamaModel {
  id: string;            // "ollama/llama3.2:3b"
  name: string;
  size?: number | null;
  modified?: string | null;
}

export function QueryPrefsSection() {
  const [prefs, setPrefs] = useState<QueryPrefs | null>(null);
  const [pool, setPool] = useState<ModelPoolEntry[]>([]);
  const [installed, setInstalled] = useState<OllamaModel[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savingRole, setSavingRole] = useState<Role | null>(null);
  const [savingExclusions, setSavingExclusions] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [p, plist, oll] = await Promise.all([
        api.getQueryPrefs(),
        api.listModelPool(),
        api.listOllamaInstalled().catch(() => ({ models: [] as OllamaModel[] })),
      ]);
      setPrefs(p);
      setPool(plist.entries || []);
      const ollList: OllamaModel[] = (oll as { models?: OllamaModel[] }).models || [];
      setInstalled(ollList);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const flashSaved = (label: string) => {
    setSaved(label);
    setTimeout(() => setSaved(null), 2000);
  };

  const handleRoleChange = async (role: Role, value: string) => {
    if (!prefs) return;
    const field = ROLE_FIELDS[role];
    const next: Partial<QueryPrefs> = { [field]: value || null };
    setSavingRole(role);
    setError(null);
    try {
      const updated = await api.updateQueryPrefs(next);
      setPrefs(updated);
      flashSaved(`${role} → ${value || "(unset)"}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingRole(null);
    }
  };

  const excluded = useMemo(
    () => new Set(prefs?.ollama_exclusions || []),
    [prefs?.ollama_exclusions],
  );

  const toggleExclusion = async (modelId: string) => {
    if (!prefs) return;
    const nextSet = new Set(excluded);
    if (nextSet.has(modelId)) nextSet.delete(modelId);
    else nextSet.add(modelId);
    setSavingExclusions(true);
    setError(null);
    try {
      const updated = await api.updateQueryPrefs({
        ollama_exclusions: Array.from(nextSet),
      });
      setPrefs(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingExclusions(false);
    }
  };

  const setAllExclusions = async (mode: "all" | "none") => {
    if (!prefs) return;
    const next = mode === "all" ? installed.map((m) => m.id) : [];
    setSavingExclusions(true);
    try {
      const updated = await api.updateQueryPrefs({ ollama_exclusions: next });
      setPrefs(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingExclusions(false);
    }
  };

  return (
    <div className="space-y-4 border border-amber-500/20 bg-amber-500/5 rounded-md p-4">
      <div>
        <div className="flex items-center justify-between">
          <h3 className="text-[14px] font-semibold text-amber-200">
            Query Model Preferences (Phase F)
          </h3>
          <button
            onClick={refresh}
            disabled={loading}
            className="flex items-center gap-1 text-[11px] text-content-tertiary hover:text-amber-200 disabled:opacity-40"
            title="Refresh prefs + pool + ollama list"
          >
            {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            <span className="uppercase tracking-widest">Reload</span>
          </button>
        </div>
        <p className="text-[11px] text-gray-500 mt-1 leading-relaxed">
          Pick which Model Pool chip handles each query-time role. Edit chips
          themselves in the <strong>Model Pool</strong> tab (these dropdowns
          stay in sync). Ingestion-time models are configured per-corpus and
          are NOT affected by anything on this page.
        </p>
      </div>

      {error && (
        <div className="flex items-start gap-2 text-[12px] text-red-300 border border-red-500/30 bg-red-500/10 rounded p-2">
          <AlertTriangle size={14} className="shrink-0 mt-0.5" />
          <span className="font-mono break-words">{error}</span>
        </div>
      )}
      {saved && (
        <div className="flex items-center gap-2 text-[11px] text-emerald-300">
          <Check size={12} /> Saved: {saved}
        </div>
      )}

      {/* A. Role mapping */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {(["query", "hyde", "agentic"] as Role[]).map((role) => {
          const field = ROLE_FIELDS[role];
          const current = (prefs?.[field] as string | null) ?? "";
          return (
            <div key={role}>
              <label className="text-[10px] uppercase tracking-widest text-content-tertiary block mb-1">
                {role === "query" ? "Default Query" : role === "hyde" ? "HyDE" : "Agentic (tools)"}
              </label>
              <select
                value={current}
                disabled={savingRole === role || loading}
                onChange={(e) => handleRoleChange(role, e.target.value)}
                className="w-full px-2 py-1.5 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-amber-400 disabled:opacity-50"
              >
                <option value="">— use server default —</option>
                {pool.map((e) => (
                  <option key={e.entry_id} value={e.entry_id}>
                    {e.label} ({e.model_name})
                  </option>
                ))}
              </select>
            </div>
          );
        })}
      </div>
      {pool.length === 0 && (
        <div className="text-[11px] text-amber-300/70">
          No chips in Model Pool. Add one in the Model Pool tab to enable
          role selection.
        </div>
      )}

      {/* B. Ollama exclusions */}
      <div className="border-t border-amber-500/20 pt-3">
        <div className="flex items-center justify-between mb-2">
          <h4 className="text-[12px] font-semibold text-amber-200 flex items-center gap-1.5">
            <Cpu size={12} /> Ollama Installed Models
          </h4>
          <div className="flex items-center gap-2 text-[10px]">
            <button
              onClick={() => setAllExclusions("none")}
              disabled={savingExclusions || installed.length === 0}
              className="text-content-tertiary hover:text-amber-200 uppercase tracking-widest disabled:opacity-40"
            >
              Show All
            </button>
            <span className="text-content-tertiary">/</span>
            <button
              onClick={() => setAllExclusions("all")}
              disabled={savingExclusions || installed.length === 0}
              className="text-content-tertiary hover:text-amber-200 uppercase tracking-widest disabled:opacity-40"
            >
              Hide All
            </button>
          </div>
        </div>
        <p className="text-[10px] text-gray-500 mb-2 leading-relaxed">
          Unchecked = excluded from your /api/models list. Filter is per-user
          and never alters Ollama itself.
        </p>
        {installed.length === 0 ? (
          <div className="text-[11px] text-content-tertiary italic">
            No ollama models installed (or ollama unreachable).
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
            {installed.map((m) => {
              const isExcluded = excluded.has(m.id);
              return (
                <label
                  key={m.id}
                  className="flex items-center gap-2 text-[11px] text-content-secondary hover:text-content-primary cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={!isExcluded}
                    disabled={savingExclusions}
                    onChange={() => toggleExclusion(m.id)}
                    className="accent-amber-400"
                  />
                  <span className="font-mono truncate">{m.id}</span>
                </label>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
