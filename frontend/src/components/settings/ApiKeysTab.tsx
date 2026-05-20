// ApiKeysTab.tsx — Phase 19 Wave 2 Cloud API Key Manager
// Lets the user paste cloud provider API keys directly into Settings.
// Backend Fernet-encrypts them at rest in MongoDB. Plaintext NEVER round-trips
// back to the UI — masked values only on read.

import { useCallback, useEffect, useState } from "react";
import {
  Key,
  Save,
  Loader2,
  CheckCircle,
  AlertTriangle,
  Eye,
  EyeOff,
  Trash2,
} from "lucide-react";
import * as api from "../../lib/api";

type ProviderMeta = {
  label: string;
  placeholder: string;
  group: "chat" | "embedder";
  routing: string;
};

const PROVIDER_LABELS: Record<string, ProviderMeta> = {
  openai: {
    label: "OpenAI",
    placeholder: "sk-proj-...",
    group: "chat",
    routing: "via LiteLLM proxy as api_key",
  },
  anthropic: {
    label: "Anthropic (Claude)",
    placeholder: "sk-ant-api03-...",
    group: "chat",
    routing: "via LiteLLM proxy as api_key",
  },
  deepseek: {
    label: "DeepSeek",
    placeholder: "sk-...",
    group: "chat",
    routing: "via LiteLLM proxy as api_key",
  },
  gemini: {
    label: "Google Gemini",
    placeholder: "AIza...",
    group: "chat",
    routing: "via LiteLLM proxy as api_key",
  },
  openrouter: {
    label: "OpenRouter",
    placeholder: "sk-or-v1-...",
    group: "chat",
    routing: "via LiteLLM proxy as api_key",
  },
  siliconflow: {
    label: "SiliconFlow",
    placeholder: "sk-...",
    group: "embedder",
    routing: "direct to SiliconFlow embedder API (bypasses LiteLLM)",
  },
  // Modal credentials (token_id, token_secret, proxy bearer) live under
  // Settings → Infrastructure → Modal as of Sprint 2B — co-located with
  // the deploy controls.
  // Phase 19.3 — new LiteLLM wildcard providers. Storage keys match the
  // route prefixes in litellm/config.yaml so `_provider_for_model`
  // finds the user's per-user Mongo key at chat time.
  mistral: {
    label: "Mistral",
    placeholder: "(api.mistral.ai key)",
    group: "chat",
    routing: "via LiteLLM proxy as api_key — overrides .env MISTRAL_API_KEY",
  },
  kimi: {
    label: "Kimi / Moonshot",
    placeholder: "sk-...  (api.moonshot.ai key)",
    group: "chat",
    routing: "via LiteLLM proxy as api_key — overrides .env MOONSHOT_API_KEY",
  },
  minimax: {
    label: "MiniMax",
    placeholder: "eyJhbGciOi...  (api.minimaxi.chat key)",
    group: "chat",
    routing: "via LiteLLM proxy as api_key — overrides .env MINIMAX_API_KEY",
  },
  "glm-coding": {
    label: "Z.AI Coding (GLM)",
    placeholder: "(api.z.ai coding key)",
    group: "chat",
    routing: "via LiteLLM proxy as api_key — overrides .env Z_AI_API_KEY",
  },
  mimo: {
    label: "Xiaomi MiMo",
    placeholder: "(token-plan-sgp.xiaomimimo.com key)",
    group: "chat",
    routing: "via LiteLLM proxy as api_key — corpus MiMo preset uses token-plan SGP",
  },
  "mimo-coding": {
    label: "Xiaomi MiMo Coding",
    placeholder: "(token-plan-sgp.xiaomimimo.com key)",
    group: "chat",
    routing: "via LiteLLM proxy as api_key — same MIMO_API_KEY as MiMo usually",
  },
};

export function ApiKeysTab() {
  const [masked, setMasked] = useState<Record<string, string>>({});
  const [providers, setProviders] = useState<string[]>([]);
  const [pending, setPending] = useState<Record<string, string>>({});
  const [reveal, setReveal] = useState<Record<string, boolean>>({});

  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [savedKey, setSavedKey] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const data = await api.listApiKeys();
      setMasked(data.keys || {});
      setProviders(data.providers || []);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleSave = async (provider: string) => {
    const value = (pending[provider] || "").trim();
    setSavingKey(provider);
    setSavedKey(null);
    setSaveError(null);
    try {
      const data = await api.updateApiKeys({ [provider]: value });
      setMasked(data.keys || {});
      setPending((p) => ({ ...p, [provider]: "" }));
      setReveal((r) => ({ ...r, [provider]: false }));
      setSavedKey(provider);
      setTimeout(() => setSavedKey(null), 2500);
    } catch (err) {
      setSaveError(
        `${provider}: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setSavingKey(null);
    }
  };

  const handleClear = async (provider: string) => {
    if (
      !confirm(
        `Clear stored ${provider} API key? The backend will fall back to .env (or fail if no env value is set).`,
      )
    )
      return;
    setSavingKey(provider);
    try {
      const data = await api.updateApiKeys({ [provider]: "" });
      setMasked(data.keys || {});
    } catch (err) {
      setSaveError(
        `${provider}: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setSavingKey(null);
    }
  };

  return (
    <div className="space-y-8">
      {/* QueryPrefsSection removed in Sprint 4B — HyDE/agentic selection
          now lives in the Models tab (ModelsTab.tsx). */}

      <div>
        <h2 className="text-xl font-semibold text-white mb-2">API Keys</h2>
        <p className="text-[13px] text-gray-500 leading-relaxed">
          All keys are <strong>Fernet-encrypted at rest</strong> in MongoDB
          and never round-trip back to the UI as plaintext. Two routing paths:
        </p>
        <ul className="text-[12px] text-gray-500 leading-relaxed mt-2 space-y-1 pl-4 list-disc">
          <li>
            <strong className="text-blue-300">Chat providers</strong> (OpenAI,
            Anthropic, DeepSeek, Gemini, OpenRouter) — backend injects the
            decrypted key as the per-call <code className="bg-[#1a1a1a] px-1 rounded">api_key</code>{" "}
            param to the <strong>LiteLLM proxy</strong>. LiteLLM still does
            routing, retries, fallback, cost tracking.
          </li>
          <li>
            <strong className="text-emerald-300">Embedder providers</strong>{" "}
            (Modal, SiliconFlow) — used directly by the embedder dispatcher in{" "}
            <code className="bg-[#1a1a1a] px-1 rounded">services/embedder.py</code>{" "}
            because LiteLLM doesn't proxy our embedding paths.
          </li>
        </ul>
      </div>

      {(loadError || saveError) && (
        <div className="flex items-start gap-2 text-[12px] text-red-300 border border-red-500/30 bg-red-500/5 rounded p-3">
          <AlertTriangle size={14} className="shrink-0 mt-0.5" />
          <span className="font-mono break-words">
            {loadError || saveError}
          </span>
        </div>
      )}

      {isLoading && providers.length === 0 && (
        <div className="text-[12px] text-gray-500 italic flex items-center gap-2 py-4">
          <Loader2 size={14} className="animate-spin" />
          Loading…
        </div>
      )}

      {(["chat", "embedder"] as const).map((groupKey) => {
        const groupLabel =
          groupKey === "chat"
            ? "Chat (via LiteLLM proxy)"
            : "Embedder (direct, bypasses LiteLLM)";
        const groupColor =
          groupKey === "chat" ? "text-blue-300" : "text-emerald-300";
        const inGroup = providers.filter(
          (p) => (PROVIDER_LABELS[p]?.group ?? "chat") === groupKey,
        );
        if (inGroup.length === 0) return null;

        return (
          <section key={groupKey} className="space-y-3">
            <div
              className={`text-[10px] font-bold tracking-widest uppercase ${groupColor} pl-1`}
            >
              {groupLabel}
            </div>
            {inGroup.map((provider) => {
              const meta =
                PROVIDER_LABELS[provider] ||
                {
                  label: provider,
                  placeholder: "",
                  group: "chat" as const,
                  routing: "",
                };
              const stored = masked[provider] || "[not set]";
              const isStored = stored !== "[not set]";
          const draft = pending[provider] ?? "";
          const isRevealed = !!reveal[provider];
          const isSaving = savingKey === provider;
          const wasJustSaved = savedKey === provider;

          return (
            <div
              key={provider}
              className="bg-[#2a2a2a] border border-white/5 rounded-lg p-4 space-y-3"
            >
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Key
                    size={14}
                    className={
                      isStored ? "text-emerald-400" : "text-gray-500"
                    }
                  />
                  <span className="text-[13px] font-semibold text-white">
                    {meta.label}
                  </span>
                  <span className="text-[10px] font-mono text-gray-500">
                    {provider}
                  </span>
                </div>
                <span
                  className={`text-[11px] font-mono ${
                    isStored ? "text-emerald-300" : "text-gray-500"
                  }`}
                >
                  {stored}
                </span>
              </div>

              <div className="flex gap-2">
                <div className="flex-1 relative">
                  <input
                    type={isRevealed ? "text" : "password"}
                    value={draft}
                    onChange={(e) =>
                      setPending((p) => ({ ...p, [provider]: e.target.value }))
                    }
                    placeholder={meta.placeholder || `Paste ${provider} key`}
                    disabled={isSaving}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !isSaving) handleSave(provider);
                    }}
                    className="w-full bg-[#1a1a1a] border border-white/10 rounded px-3 py-2 pr-9 text-[13px] text-white placeholder:text-gray-600 focus:outline-none focus:border-blue-400 font-mono"
                  />
                  <button
                    type="button"
                    onClick={() =>
                      setReveal((r) => ({ ...r, [provider]: !r[provider] }))
                    }
                    title={isRevealed ? "Hide" : "Show"}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-white"
                  >
                    {isRevealed ? <EyeOff size={14} /> : <Eye size={14} />}
                  </button>
                </div>

                <button
                  onClick={() => handleSave(provider)}
                  disabled={isSaving || !draft.trim()}
                  className="flex items-center gap-1.5 px-3 py-2 text-[12px] font-semibold border border-blue-500/50 text-blue-300 hover:bg-blue-500/10 disabled:opacity-40 disabled:cursor-not-allowed rounded"
                >
                  {isSaving ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : wasJustSaved ? (
                    <CheckCircle size={14} className="text-emerald-400" />
                  ) : (
                    <Save size={14} />
                  )}
                  Save
                </button>

                {isStored && (
                  <button
                    onClick={() => handleClear(provider)}
                    disabled={isSaving}
                    title="Clear this key"
                    className="px-2.5 py-2 text-gray-500 hover:text-red-400 hover:bg-red-500/10 rounded transition-colors disabled:opacity-40"
                  >
                    <Trash2 size={14} />
                  </button>
                )}
              </div>

              {meta.routing && (
                <div className="text-[10px] text-gray-500 italic pl-1">
                  → {meta.routing}
                </div>
              )}
            </div>
          );
            })}
          </section>
        );
      })}

      <div className="text-[11px] text-gray-600 leading-relaxed px-1">
        Encryption uses a Fernet cipher derived from{" "}
        <code className="bg-[#333] px-1 py-0.5 rounded">AUTH_SECRET_KEY</code>{" "}
        unless{" "}
        <code className="bg-[#333] px-1 py-0.5 rounded">APP_ENCRYPTION_KEY</code>{" "}
        is set explicitly. Rotating either env var will invalidate stored
        keys (you'll need to re-save them). Keys are passed to LiteLLM at
        request time as the per-call{" "}
        <code className="bg-[#333] px-1 py-0.5 rounded">api_key</code> param —
        if no Mongo key exists for a provider, LiteLLM falls back to its
        env-configured key.
      </div>
    </div>
  );
}
