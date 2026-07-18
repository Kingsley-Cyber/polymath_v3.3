// Sprint 4B — Unified Models tab.
//
// One tab, role sections, one save button:
//   1. Query Model Pool  — add cloud / add Ollama, chip list with delete + toggle
//   2. HyDE              — enable toggle + pool-entry dropdown
//   3. Agentic           — tool-capable fallback dropdown
//   4. Graph Query       — graph synthesis + question-builder dropdown
//   5. Synthesis         — optional low-latency final-answer route
//   6. API Keys (Shared) — collapsible, mounts existing ApiKeysTab as-is
//
// "Save Models Settings" POSTs pool + model-role settings to
// /api/settings/models. API Keys has its OWN save flow inside ApiKeysTab —
// the helper text makes the split explicit.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Plus,
  X,
  Cpu,
  Save,
  AlertTriangle,
  CheckCircle,
  Loader2,
  Brain,
  Network,
  Wand2,
  Download,
  ChevronDown,
  ChevronRight,
  KeyRound,
  LogIn,
  RefreshCw,
  SquareTerminal,
} from "lucide-react";
import { useQueryModelPoolStore } from "../../stores/queryModelPoolStore";
import * as api from "../../lib/api";
import type {
  OllamaInstalledModel,
  PoolProvider,
  QueryModelPoolEntry,
} from "../../types";
import { POOL_PROVIDER_PRESETS, findPreset } from "../../types";
import { ApiKeysTab } from "./ApiKeysTab";


function newEntryId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return (crypto as unknown as { randomUUID(): string }).randomUUID();
  }
  return "fe-" + Math.random().toString(36).slice(2, 10);
}


// ── CLI Subscriptions section (T4) ───────────────────────────────────────
// Status + login + one-click model sync for the host CLI shim lanes
// (ChatGPT/codex, Cursor, Antigravity/gemini). Synced entries land in the
// query_model_pool as $0 subscription_flat routes.

function CliProvidersSection() {
  const { load } = useQueryModelPoolStore();
  const [status, setStatus] = useState<Record<
    string,
    import("../../lib/api").CliProviderStatus
  > | null>(null);
  const [statusErr, setStatusErr] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const [loginMsg, setLoginMsg] = useState<string | null>(null);

  const refresh = async () => {
    setStatusErr(null);
    try {
      const res = await api.getCliProvidersStatus();
      setStatus(res.providers);
    } catch (e) {
      setStatus(null);
      setStatusErr(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleLogin = async (name: string) => {
    setLoginMsg(null);
    try {
      const res = await api.cliProviderLogin(name);
      setLoginMsg(
        res.spawned
          ? "Login window opened — approve it in your browser, then Sync."
          : (res.hint ?? "Follow the CLI's login instructions in Terminal."),
      );
    } catch (e) {
      setLoginMsg(e instanceof Error ? e.message : String(e));
    }
  };

  const handleSync = async () => {
    setSyncing(true);
    setSyncMsg(null);
    try {
      const res = await api.syncCliProviderModels();
      setSyncMsg(
        `Discovered ${res.discovered} models · added ${res.added} new entr${
          res.added === 1 ? "y" : "ies"
        }.`,
      );
      await load(); // refresh the pool list + chat dropdown data
      await refresh();
    } catch (e) {
      setSyncMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  const authBadge = (s: import("../../lib/api").CliProviderStatus) => {
    if (!s.installed)
      return (
        <span className="rounded border border-white/10 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-gray-500">
          Not installed
        </span>
      );
    if (s.auth === "login_required")
      return (
        <span className="rounded border border-amber-400/40 bg-amber-400/10 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-amber-300">
          Login required
        </span>
      );
    return (
      <span className="rounded border border-emerald-400/40 bg-emerald-400/10 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-emerald-300">
        Connected
      </span>
    );
  };

  return (
    <section
      className="rounded-lg border border-white/10 bg-[#141519] p-4 space-y-3"
      data-testid="cli-providers-section"
    >
      <div className="flex items-center gap-2">
        <SquareTerminal className="w-4 h-4 text-amber-300" />
        <h3 className="text-[14px] font-semibold text-white">
          CLI Subscriptions
        </h3>
        <span className="rounded border border-amber-300/30 bg-amber-300/10 px-1.5 text-[9px] uppercase tracking-widest text-amber-200">
          $0 flat
        </span>
        <span className="flex-1" />
        <button
          onClick={handleSync}
          disabled={syncing || !status}
          data-testid="cli-providers-sync"
          className="flex items-center gap-1.5 rounded border border-accent-main/60 bg-accent-main/15 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-widest text-accent-main hover:bg-accent-main/25 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {syncing ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <RefreshCw className="w-3.5 h-3.5" />
          )}
          Sync models
        </button>
      </div>
      <p className="text-[12px] leading-relaxed text-gray-500">
        Chat through the coding-agent subscriptions installed on this Mac —
        no API keys, billed flat by each plan. Sync pulls every model each
        connected CLI offers into the pool (and the chat dropdown) in one
        click.
      </p>

      {statusErr && (
        <div className="flex items-start gap-2 rounded border border-red-500/40 bg-red-950/40 px-3 py-2 text-[12px] text-red-300">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            CLI shim unreachable ({statusErr}). Is the host service on :8090
            running?
          </span>
        </div>
      )}

      {status && (
        <div className="grid gap-2 sm:grid-cols-3">
          {Object.entries(status).map(([name, s]) => (
            <div
              key={name}
              className="flex flex-col gap-2 rounded border border-white/10 bg-[#0b0c10] p-3"
              data-testid={`cli-provider-${name}`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-[12px] font-semibold text-white">
                  {s.display}
                </span>
                <span
                  className={`h-2 w-2 shrink-0 rounded-full ${
                    !s.installed
                      ? "bg-gray-600"
                      : s.auth === "ok"
                        ? "bg-emerald-400"
                        : "bg-amber-400"
                  }`}
                />
              </div>
              <div className="flex items-center justify-between gap-2">
                {authBadge(s)}
                {s.installed &&
                  s.auth === "login_required" &&
                  s.can_spawn_login && (
                    <button
                      onClick={() => handleLogin(name)}
                      className="flex items-center gap-1 rounded border border-white/15 px-2 py-1 text-[10px] uppercase tracking-widest text-gray-300 hover:border-accent-main hover:text-accent-main"
                      data-testid={`cli-provider-login-${name}`}
                    >
                      <LogIn className="h-3 w-3" />
                      Login
                    </button>
                  )}
              </div>
              <div className="truncate text-[10px] text-gray-600">
                binary: {s.binary}
              </div>
            </div>
          ))}
        </div>
      )}

      {loginMsg && (
        <div className="text-[12px] text-amber-200">{loginMsg}</div>
      )}
      {syncMsg && (
        <div className="flex items-center gap-2 text-[12px] text-emerald-300">
          <CheckCircle className="h-3.5 w-3.5" />
          {syncMsg}
        </div>
      )}
    </section>
  );
}

// ── Query Model Pool section ─────────────────────────────────────────────

function PoolSection() {
  const { config, addEntry, toggleEntry, deleteEntry } = useQueryModelPoolStore();
  const pool = config.query_model_pool;

  const [provider, setProvider] = useState<PoolProvider>("openai");
  const [baseUrl, setBaseUrl] = useState("https://api.openai.com/v1");
  const [apiKey, setApiKey] = useState("");
  const [modelName, setModelName] = useState("");
  const [label, setLabel] = useState("");
  const modelInputRef = useRef<HTMLInputElement>(null);
  const [flashId, setFlashId] = useState<string | null>(null);

  const [showOllamaModal, setShowOllamaModal] = useState(false);

  const applyPreset = (id: PoolProvider) => {
    const p = POOL_PROVIDER_PRESETS.find((pp) => pp.id === id);
    setProvider(id);
    setBaseUrl(p?.base_url || "");
    if (p?.model_dropdown_only && p.example_model) {
      setModelName(p.example_model);
    } else if (!modelName && p && p.litellm_provider && p.example_model) {
      setModelName(p.example_model);
    } else if (!modelName) {
      setModelName(p?.example_model || "");
    }
  };

  // Require an API key for cloud entries. A custom endpoint (OpenCode Go,
  // SiliconFlow, …) has no "shared" key behind it, so a keyless entry saves
  // fine then fails at query time with the provider's "Missing API key" 401 —
  // a silent dead-end. Requiring the key here also neutralizes the model-field
  // Enter shortcut firing Add before the key is typed.
  const canAddCloud =
    modelName.trim().length > 0 &&
    apiKey.trim().length > 0 &&
    (provider === "custom" || baseUrl.trim().length > 0);

  const handleAddCloud = () => {
    if (!canAddCloud) return;
    const cleanModel = modelName.trim();
    const preset = findPreset(provider);
    const entry: QueryModelPoolEntry = {
      entry_id: newEntryId(),
      label: label.trim() || `${provider} · ${cleanModel}`,
      provider,
      base_url: baseUrl.trim() || null,
      api_key_ciphertext: apiKey.trim() || null,
      model_name: cleanModel,
      source: "cloud",
      enabled: true,
      created_at: new Date().toISOString(),
      extra_params: preset?.kwargs || {},
    };
    addEntry(entry);
    setFlashId(entry.entry_id);
    setTimeout(() => setFlashId(null), 450);
    setApiKey("");
    setModelName("");
    setLabel("");
    setTimeout(() => modelInputRef.current?.focus(), 0);
  };
  const selectedPreset = findPreset(provider);

  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
          <Brain size={16} className="text-purple-400" />
          Query Model Pool
        </h3>
        <div className="text-[10px] tracking-widest uppercase text-gray-500">
          {pool.length} {pool.length === 1 ? "model" : "models"}
        </div>
      </div>
      <p className="text-[12px] text-gray-500 leading-relaxed">
        Your one shelf of chat models. Add cloud APIs with per-entry keys, or
        pick from Ollama models already pulled on this machine. The chat
        dropdown, HyDE, and Agentic all read from this pool.
      </p>

      {/* Chips */}
      <div className="flex flex-wrap gap-1.5">
        {pool.length === 0 && (
          <div className="text-[11px] italic text-gray-500 py-1">
            No models yet — add a cloud model below or pull from Ollama.
          </div>
        )}
        {pool.map((e) => {
          const isFresh = e.entry_id === flashId;
          const dim = e.enabled ? "" : "opacity-50";
          return (
            <div
              key={e.entry_id}
              className={`group flex items-center gap-1.5 px-2 py-1 rounded border text-[10px] font-mono tracking-wide transition-all ${dim} ${
                isFresh
                  ? "border-accent-main bg-accent-main/15 scale-[1.03]"
                  : "border-white/10 bg-[#0b0c10]"
              }`}
              title={`${e.provider} · ${e.base_url || "no base url"} · ${e.source}`}
            >
              <button
                onClick={() => toggleEntry(e.entry_id)}
                className="flex items-center gap-1.5 hover:text-accent-main"
                title={e.enabled ? "Click to disable" : "Click to enable"}
              >
                <span className="text-gray-400 uppercase text-[8px]">
                  {e.provider}
                </span>
                <span className="text-white">{e.model_name}</span>
              </button>
              {e.source === "ollama" ? (
                <Cpu className="w-2.5 h-2.5 text-cyan-300" />
              ) : e.api_key_ciphertext ? (
                <KeyRound className="w-2.5 h-2.5 text-emerald-400" />
              ) : (
                <KeyRound className="w-2.5 h-2.5 text-red-400" />
              )}
              <button
                onClick={() => deleteEntry(e.entry_id).catch(() => void 0)}
                className="ml-0.5 text-gray-500 hover:text-red-400"
                title="Remove from pool"
              >
                <X className="w-2.5 h-2.5" />
              </button>
            </div>
          );
        })}
      </div>

      {/* Add cloud row */}
      <div className="border-t border-white/5 pt-3 space-y-2">
        <div className="text-[11px] font-bold tracking-widest uppercase text-gray-400">
          Add cloud model
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <select
            value={provider}
            onChange={(e) => applyPreset(e.target.value as PoolProvider)}
            className="bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[11px] font-mono min-w-[110px]"
          >
            {POOL_PROVIDER_PRESETS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <input
            type="text"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="base_url"
            className="flex-1 min-w-full sm:min-w-[180px] bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono placeholder:text-gray-600"
          />
          {selectedPreset?.model_dropdown_only ? (
            <select
              value={modelName}
              onChange={(e) => setModelName(e.target.value)}
              className="flex-1 min-w-full sm:min-w-[160px] bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono"
            >
              {(selectedPreset.example_models || [selectedPreset.example_model]).map((m) => {
                return (
                  <option key={m} value={m}>
                    {m}
                  </option>
                );
              })}
            </select>
          ) : (
            <input
              ref={modelInputRef}
              type="text"
              list={
                selectedPreset?.example_models
                  ? `models-${provider}`
                  : undefined
              }
              value={modelName}
              onChange={(e) => setModelName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  handleAddCloud();
                }
              }}
              placeholder="model name"
              className="flex-1 min-w-full sm:min-w-[160px] bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono placeholder:text-gray-600"
            />
          )}
          {/* Datalist suggestions surface every preset's example_models so a
              provider switch immediately offers the curated shortlist. The
              input itself stays free-text — typing a custom model still works. */}
          {POOL_PROVIDER_PRESETS.map((p) =>
            p.example_models && p.example_models.length > 0 ? (
              <datalist key={p.id} id={`models-${p.id}`}>
                {p.example_models.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            ) : null
          )}
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="api key (required)"
            className={`flex-1 min-w-full sm:min-w-[180px] bg-[#0b0c10] text-white border rounded px-2 py-1 text-[11px] font-mono placeholder:text-gray-600 ${
              apiKey.trim() ? "border-white/10" : "border-amber-500/40"
            }`}
          />
          <input
            type="text"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="label (optional)"
            className="flex-1 min-w-full sm:min-w-[140px] bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono placeholder:text-gray-600"
          />
          <button
            onClick={handleAddCloud}
            disabled={!canAddCloud}
            title={
              !canAddCloud
                ? "Needs a model name, base URL, and an API key"
                : "Add this model to the query pool"
            }
            className="flex items-center gap-1 px-3 py-1 rounded border border-accent-main/60 bg-accent-main/10 text-accent-main text-[11px] font-bold uppercase tracking-widest hover:bg-accent-main/20 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Plus className="w-3 h-3" />
            Add to pool
          </button>
        </div>
        <p className="text-[10px] text-gray-600">
          API key is Fernet-encrypted at rest and masked as{" "}
          <span className="font-mono">[set]</span> on read. Leave blank to use
          the shared key from the API Keys section below.
        </p>
      </div>

      {/* Add ollama */}
      <div className="border-t border-white/5 pt-3">
        <button
          onClick={() => setShowOllamaModal(true)}
          className="flex items-center gap-2 px-3 py-1.5 rounded border border-cyan-500/40 bg-cyan-500/5 text-cyan-300 text-[11px] font-semibold hover:bg-cyan-500/10"
        >
          <Download className="w-3.5 h-3.5" />
          Add from installed Ollama models
        </button>
      </div>

      {showOllamaModal && (
        <OllamaBulkAddModal onClose={() => setShowOllamaModal(false)} />
      )}
    </div>
  );
}

// ── Ollama bulk-add modal ────────────────────────────────────────────────

function OllamaBulkAddModal({ onClose }: { onClose: () => void }) {
  const bulkAddOllama = useQueryModelPoolStore((s) => s.bulkAddOllama);
  const pool = useQueryModelPoolStore((s) => s.config.query_model_pool);
  const [installed, setInstalled] = useState<OllamaInstalledModel[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const alreadyInPool = useMemo(
    () =>
      new Set(
        pool.filter((e) => e.provider === "ollama").map((e) => e.model_name),
      ),
    [pool],
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.listInstalledOllamaModels();
        if (!cancelled) setInstalled(list);
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const toggle = (name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const handleAdd = async () => {
    if (selected.size === 0) return;
    setSaving(true);
    try {
      await bulkAddOllama(Array.from(selected));
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  // Backend already formats size_human; fall back to computing from bytes
  // if a legacy payload omits it.
  const fmtSize = (m: OllamaInstalledModel) => {
    if (m.size_human) return m.size_human;
    const b = m.size_bytes;
    if (!b) return "";
    const gb = b / 1e9;
    if (gb >= 1) return `${gb.toFixed(1)} GB`;
    return `${Math.round(b / 1e6)} MB`;
  };

  return (
    <div
      className="fixed inset-0 z-[110] flex items-center justify-center"
      onClick={onClose}
    >
      <div className="absolute inset-0 bg-black/70" />
      <div
        className="relative w-full max-w-[520px] bg-[#242424] rounded-xl border border-white/5 shadow-2xl p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-4">
          <div>
            <h3 className="text-[15px] font-semibold text-white">
              Add from installed Ollama models
            </h3>
            <p className="text-[12px] text-gray-500 mt-0.5">
              Local models already pulled via{" "}
              <span className="font-mono">ollama pull</span>.
            </p>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-white">
            <X className="w-4 h-4" />
          </button>
        </div>

        {loading && (
          <div className="py-8 flex items-center justify-center text-gray-500">
            <Loader2 className="w-4 h-4 animate-spin mr-2" />
            Loading installed models…
          </div>
        )}

        {err && (
          <div className="mb-3 flex items-start gap-2 px-3 py-2 bg-red-950/40 border border-red-500/40 rounded text-[12px] text-red-300">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            <span>{err}</span>
          </div>
        )}

        {!loading && !err && installed.length === 0 && (
          <div className="py-6 text-center text-[12px] text-gray-500">
            No Ollama models found. Pull one with
            <div className="mt-2 font-mono text-gray-400">
              ollama pull qwen3:1.7b
            </div>
          </div>
        )}

        {installed.length > 0 && (
          <div className="max-h-[340px] overflow-y-auto custom-scrollbar space-y-1 mb-4">
            {installed.map((m) => {
              const isAlready = alreadyInPool.has(m.name);
              const isSel = selected.has(m.name);
              return (
                <label
                  key={m.name}
                  className={`flex items-center gap-3 px-3 py-2 rounded border text-[12px] ${
                    isAlready
                      ? "border-white/5 bg-[#1d1d1d] opacity-50 cursor-not-allowed"
                      : isSel
                        ? "border-accent-main/60 bg-accent-main/10 cursor-pointer"
                        : "border-white/5 bg-[#1d1d1d] hover:border-white/15 cursor-pointer"
                  }`}
                >
                  <input
                    type="checkbox"
                    disabled={isAlready}
                    checked={isSel}
                    onChange={() => toggle(m.name)}
                    className="accent-accent-main"
                  />
                  <span className="text-white font-mono flex-1">{m.name}</span>
                  {fmtSize(m) && (
                    <span className="text-[10px] text-gray-500 font-mono">
                      {fmtSize(m)}
                    </span>
                  )}
                  {isAlready && (
                    <span className="text-[10px] text-gray-500 uppercase tracking-widest">
                      in pool
                    </span>
                  )}
                </label>
              );
            })}
          </div>
        )}

        <div className="flex items-center justify-between">
          <div className="text-[11px] text-gray-500">
            {selected.size} selected
          </div>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-[12px] text-gray-400 hover:text-white"
            >
              Cancel
            </button>
            <button
              onClick={handleAdd}
              disabled={selected.size === 0 || saving}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-accent-main/60 bg-accent-main/10 text-accent-main text-[12px] font-semibold disabled:opacity-40"
            >
              {saving ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Plus className="w-3.5 h-3.5" />
              )}
              Add {selected.size > 0 ? `(${selected.size})` : ""}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── HyDE + Agentic ───────────────────────────────────────────────────────

function PoolDropdown({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (v: string | null) => void;
}) {
  const pool = useQueryModelPoolStore((s) => s.config.query_model_pool);
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
      className="bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[12px] font-mono min-w-0 sm:min-w-[280px]"
    >
      <option value="">— fall back to server default —</option>
      {pool
        .filter((e) => e.enabled)
        .map((e) => (
          <option key={e.entry_id} value={e.entry_id}>
            {e.provider} · {e.model_name}
          </option>
        ))}
    </select>
  );
}

function HydeSection() {
  const { config, patchHyde } = useQueryModelPoolStore();
  const { hyde } = config;
  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
      <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
        <Wand2 size={16} className="text-amber-400" />
        HyDE — Query Rewriting
      </h3>
      <p className="text-[12px] text-gray-500 leading-relaxed">
        HyDE runs a small model before retrieval to draft a hypothetical
        answer, which is embedded and used as the retrieval query. Better
        recall on sparse queries at a small latency cost.
      </p>
      <label className="flex items-center gap-2 text-[12px] text-gray-300">
        <input
          type="checkbox"
          checked={hyde.default_enabled}
          onChange={(e) => patchHyde({ default_enabled: e.target.checked })}
          className="accent-accent-main"
        />
        Enable by default
      </label>
      <div className="flex items-center gap-3">
        <div className="text-[11px] uppercase tracking-widest text-gray-500 w-24">
          Model
        </div>
        <PoolDropdown
          value={hyde.pool_entry_id}
          onChange={(v) => patchHyde({ pool_entry_id: v })}
        />
      </div>
    </div>
  );
}

function AgenticSection() {
  const { config, patchAgentic } = useQueryModelPoolStore();
  const { agentic } = config;
  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
      <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
        <Brain size={16} className="text-green-400" />
        Tool-Capable Fallback Model
      </h3>
      <p className="text-[12px] text-gray-500 leading-relaxed">
        When you activate a tool in chat and your selected chat model can't
        function-call, Polymath silently swaps to this entry. Pick a model with
        native tool support (Mistral Large, GPT-4o, Claude Sonnet, Mimo).
      </p>
      <div className="flex items-center gap-3">
        <div className="text-[11px] uppercase tracking-widest text-gray-500 w-24">
          Model
        </div>
        <PoolDropdown
          value={agentic.pool_entry_id}
          onChange={(v) => patchAgentic({ pool_entry_id: v })}
        />
      </div>
    </div>
  );
}

function GraphQuerySection() {
  const { config, patchGraphQuery } = useQueryModelPoolStore();
  const graphQuery = config.graph_query || { pool_entry_id: null };
  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
      <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
        <Network size={16} className="text-amber-300" />
        Graph Query Model
      </h3>
      <p className="text-[12px] text-gray-500 leading-relaxed">
        Used by graph synthesis and the lighter refine/question-builder path.
        Leave unset to fall back to the normal query/chat model.
      </p>
      <div className="flex items-center gap-3">
        <div className="text-[11px] uppercase tracking-widest text-gray-500 w-24">
          Model
        </div>
        <PoolDropdown
          value={graphQuery.pool_entry_id}
          onChange={(v) => patchGraphQuery({ pool_entry_id: v })}
        />
      </div>
    </div>
  );
}

function SynthesisSection() {
  const { config, patchSynthesis } = useQueryModelPoolStore();
  const synthesis = config.synthesis || { pool_entry_id: null };
  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
      <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
        <Wand2 size={16} className="text-cyan-300" />
        Final Answer Synthesis Route
      </h3>
      <p className="text-[12px] text-gray-500 leading-relaxed">
        Optional low-latency model for the final answer call. The server-wide
        synthesis-route flag controls activation; disabling it immediately
        restores the normal chat model. Tool-enabled turns are never swapped.
      </p>
      <div className="flex items-center gap-3">
        <div className="text-[11px] uppercase tracking-widest text-gray-500 w-24">
          Model
        </div>
        <PoolDropdown
          value={synthesis.pool_entry_id}
          onChange={(v) => patchSynthesis({ pool_entry_id: v })}
        />
      </div>
    </div>
  );
}

// Phase 24 — Reasoning Cascade analyst model.
// Used by the Reason toggle in the chat header. Digests retrieved chunks
// before the chat model writes the user-facing answer.
function ReasoningSection() {
  const { config, patchReasoning } = useQueryModelPoolStore();
  const reasoning = config.reasoning || {
    default_enabled: false,
    pool_entry_id: null,
  };
  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
      <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
        <Brain size={16} className="text-purple-400" />
        Reasoning Cascade — Analyst Model
      </h3>
      <p className="text-[12px] text-gray-500 leading-relaxed">
        When the <span className="text-purple-300">Reason</span> toggle is on
        in chat, this model digests retrieved chunks into a structured briefing
        before the chat model writes the answer. Cost ≈ 20× a Balanced query.
        Pick a fast cloud model to avoid blocking the response — slow local
        models (Ollama 1B–3B) can add 60+ seconds per turn.
      </p>
      <div className="flex items-center gap-3">
        <div className="text-[11px] uppercase tracking-widest text-gray-500 w-24">
          Model
        </div>
        <PoolDropdown
          value={reasoning.pool_entry_id}
          onChange={(v) => patchReasoning({ pool_entry_id: v })}
        />
      </div>
    </div>
  );
}

// ── Shared API Keys (collapsible, optional) ──────────────────────────────

function SharedApiKeysSection() {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg">
      <button
        onClick={() => setExpanded((x) => !x)}
        className="w-full flex items-center justify-between px-5 py-3 text-left hover:bg-white/5 rounded-lg"
      >
        <div className="flex items-center gap-2">
          <KeyRound size={14} className="text-gray-400" />
          <span className="text-[13px] font-semibold text-gray-300">
            API Keys (Shared) — advanced / optional
          </span>
        </div>
        {expanded ? (
          <ChevronDown className="w-4 h-4 text-gray-500" />
        ) : (
          <ChevronRight className="w-4 h-4 text-gray-500" />
        )}
      </button>
      {expanded && (
        <div className="border-t border-white/5 p-5">
          <p className="text-[11px] text-gray-500 mb-3 leading-relaxed">
            Shared credentials are a fallback for pool entries that opt into{" "}
            <span className="font-mono">use_shared_key</span>. Most setups put
            the key directly on each pool entry and leave this empty.
          </p>
          <p className="text-[11px] text-amber-300/80 mb-4">
            API Keys use a separate save action. Edit them and click{" "}
            <span className="font-semibold">Save API Keys</span> within this
            section — the "Save Models Settings" button below does NOT write
            this section.
          </p>
          <ApiKeysTab />
        </div>
      )}
    </div>
  );
}

// ── Main export ──────────────────────────────────────────────────────────

export function ModelsTab() {
  const { config, loading, error, dirty, load, save } = useQueryModelPoolStore();
  const [saveOk, setSaveOk] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    load();
  }, [load]);

  const handleSave = async () => {
    setSaving(true);
    setSaveOk(false);
    setSaveErr(null);
    try {
      await save();
      setSaveOk(true);
      setTimeout(() => setSaveOk(false), 2500);
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6 pb-24">
      <div>
        <h2 className="text-xl font-semibold text-white mb-2">Models</h2>
        <p className="text-[13px] text-gray-500">
          Every chat model lives here. The chat dropdown, HyDE, Agentic,
          Graph Query, Synthesis, and Reasoning roles all read from the pool.
          One save covers the pool and model roles — API Keys has its own save
          inside its collapsible section.
        </p>
      </div>

      {loading && config.query_model_pool.length === 0 && (
        <div className="flex items-center gap-2 text-[12px] text-gray-500">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          Loading models…
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 px-4 py-3 bg-red-950/40 border border-red-500/40 rounded text-[12px] text-red-300">
          <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      <CliProvidersSection />
      <PoolSection />
      <HydeSection />
      <AgenticSection />
      <GraphQuerySection />
      <SynthesisSection />
      <ReasoningSection />
      <SharedApiKeysSection />

      {/* Sticky save bar */}
      <div className="fixed bottom-0 right-0 left-0 sm:left-[260px] z-[105] bg-[#1a1a1a]/95 border-t border-white/5 px-3 sm:px-6 py-3 flex flex-wrap items-center justify-end gap-2 sm:gap-3 backdrop-blur">
        {saveErr && (
          <div className="flex items-center gap-2 text-[12px] text-red-300">
            <AlertTriangle className="w-3.5 h-3.5" />
            {saveErr}
          </div>
        )}
        {saveOk && (
          <div className="flex items-center gap-2 text-[12px] text-green-400">
            <CheckCircle className="w-3.5 h-3.5" />
            Saved
          </div>
        )}
        <div className="text-[11px] text-gray-500">
          {dirty ? "Unsaved changes" : "All changes saved"}
        </div>
        <button
          onClick={handleSave}
          disabled={!dirty || saving}
          data-testid="models-save"
          className="flex items-center gap-1.5 px-4 py-2 rounded border border-accent-main/60 bg-accent-main/15 text-accent-main text-[12px] font-semibold tracking-widest uppercase hover:bg-accent-main/25 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {saving ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <Save className="w-3.5 h-3.5" />
          )}
          Save Models Settings
        </button>
      </div>
    </div>
  );
}

export default ModelsTab;
