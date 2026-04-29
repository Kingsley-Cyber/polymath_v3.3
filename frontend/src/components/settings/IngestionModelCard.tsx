// Phase 19.3 — Reusable ingestion model card.
// One card = one ghost's model profile (Summary or Extraction). Used in the
// Corpus Manager's ingestion tab. When the "Use same" checkbox is ON, the
// parent renders a single card whose onChange writes BOTH summary_* and
// extraction_* fields. When OFF, two cards render side-by-side with
// independent state.
//
// This card intentionally holds no local state for the "hot" fields — all
// values are controlled by the parent — so the shared/split toggle can mirror
// or split values cleanly without reconciliation bugs.

import { useMemo, useState } from "react";
import { Eye, EyeOff, Key, Loader2, Zap } from "lucide-react";
import { PROVIDER_PRESETS, composeModelString } from "../../types";

export interface IngestionModelCardValue {
  model: string;
  base_url: string | null;
  api_key: string | null;               // plaintext buffer; "" means "leave unchanged"
  extra_params: Record<string, unknown>;
}

export interface IngestionModelCardProps {
  title: string;                        // "Summary" | "Extraction"
  subtitle?: string;
  value: IngestionModelCardValue;
  onChange: (patch: Partial<IngestionModelCardValue>) => void;
  /** When true, api_key placeholder shows '(unchanged)' — existing ciphertext stays. */
  editing?: boolean;
  /** Show this card's api_key with an existing-key badge. */
  hasExistingKey?: boolean;
  /** Optional test handler — called with the current card state. */
  onTest?: () => Promise<void>;
  testing?: boolean;
  testResult?: {
    ok: boolean;
    status?: number;
    latency_ms?: number;
    error?: string;
  } | null;
  accent?: "emerald" | "purple";        // color theme for the card
}

export function IngestionModelCard(props: IngestionModelCardProps) {
  const {
    title,
    subtitle,
    value,
    onChange,
    editing,
    hasExistingKey,
    onTest,
    testing,
    testResult,
    accent = "emerald",
  } = props;

  const [showKey, setShowKey] = useState(false);
  const [extraParamsText, setExtraParamsText] = useState<string>(() =>
    JSON.stringify(value.extra_params ?? {}, null, 2),
  );

  // Parse extra params as user types. If valid, sync up to parent.
  const parsedExtra = useMemo((): {
    ok: boolean;
    value: Record<string, unknown>;
    error?: string;
  } => {
    const raw = extraParamsText.trim();
    if (!raw || raw === "{}") return { ok: true, value: {} };
    try {
      const v = JSON.parse(raw);
      if (typeof v !== "object" || v === null || Array.isArray(v)) {
        return { ok: false, value: {}, error: "Must be a JSON object" };
      }
      return { ok: true, value: v as Record<string, unknown> };
    } catch (e) {
      return {
        ok: false,
        value: {},
        error: e instanceof Error ? e.message : "Invalid JSON",
      };
    }
  }, [extraParamsText]);

  const accentText = accent === "emerald" ? "text-emerald-300" : "text-purple-300";
  const accentBg = accent === "emerald" ? "bg-emerald-500/5" : "bg-purple-500/5";
  const accentBorder =
    accent === "emerald" ? "border-emerald-500/20" : "border-purple-500/20";

  return (
    <div
      className={`rounded-lg border ${accentBorder} ${accentBg} p-3 space-y-2.5`}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className={`text-[12px] font-bold tracking-widest uppercase ${accentText}`}>
            {title}
          </div>
          {subtitle && (
            <div className="text-[11px] text-content-tertiary mt-0.5">
              {subtitle}
            </div>
          )}
        </div>
        {onTest && (
          <button
            type="button"
            onClick={onTest}
            disabled={testing}
            title="Send a 1-token ping to verify creds + URL"
            className="flex items-center gap-1 px-2 py-1 text-[9px] font-bold tracking-widest uppercase border border-border-minimal text-content-secondary hover:border-accent-main hover:text-accent-main disabled:opacity-50 transition-none"
          >
            {testing ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <Zap className="w-3 h-3" />
            )}
            Test
          </button>
        )}
      </div>

      {/* Preset */}
      <div>
        <label className="text-[11px] text-content-tertiary tracking-wider block mb-0.5">
          Provider preset
        </label>
        <select
          defaultValue="custom"
          onChange={(e) => {
            const preset = PROVIDER_PRESETS.find((p) => p.id === e.target.value);
            if (!preset) return;
            // Compose provider-prefixed model string so the LiteLLM wildcard
            // router can match (openai/*, deepseek/*, anthropic/*, ...).
            const composed = preset.example_model
              ? composeModelString(e.target.value, preset.example_model)
              : value.model;
            onChange({
              base_url: preset.base_url || value.base_url,
              model: composed,
              extra_params: preset.kwargs
                ? {
                    ...(preset.kwargs as Record<string, unknown>),
                    ...value.extra_params,
                  }
                : value.extra_params,
            });
          }}
          className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
        >
          {PROVIDER_PRESETS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>

      {/* Base URL */}
      <div>
        <label className="text-[11px] text-content-tertiary tracking-wider block mb-0.5">
          Base URL
          <span className="ml-1 text-content-tertiary/60 normal-case font-normal">
            (leave blank for default gateway)
          </span>
        </label>
        <input
          type="text"
          value={value.base_url ?? ""}
          placeholder="https://api.provider.com/v1"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          onChange={(e) => onChange({ base_url: e.target.value || null })}
          className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary font-mono focus:outline-none focus:border-accent-main"
        />
      </div>

      {/* Model */}
      <div>
        <label className="text-[11px] text-content-tertiary tracking-wider block mb-0.5">
          Model
        </label>
        <input
          type="text"
          value={value.model}
          placeholder="deepseek-chat"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          onChange={(e) => onChange({ model: e.target.value })}
          className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary font-mono focus:outline-none focus:border-accent-main"
        />
      </div>

      {/* API key */}
      <div>
        <label className="text-[11px] text-content-tertiary tracking-wider block mb-0.5">
          <Key className="w-3 h-3 inline mr-1" />
          API key
          {editing && hasExistingKey && (
            <span className="ml-1 text-content-tertiary/60 normal-case font-normal">
              (leave blank to keep existing)
            </span>
          )}
        </label>
        <div className="relative">
          <input
            type={showKey ? "text" : "password"}
            value={value.api_key ?? ""}
            placeholder={
              editing && hasExistingKey ? "(existing key kept)" : "Paste provider API key"
            }
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => onChange({ api_key: e.target.value || null })}
            className="w-full pl-2 pr-8 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary font-mono focus:outline-none focus:border-accent-main"
          />
          <button
            type="button"
            onClick={() => setShowKey(!showKey)}
            className="absolute right-1.5 top-1/2 -translate-y-1/2 text-content-tertiary hover:text-content-primary"
          >
            {showKey ? (
              <EyeOff className="w-3 h-3" />
            ) : (
              <Eye className="w-3 h-3" />
            )}
          </button>
        </div>
      </div>

      {/* Extra params */}
      <div>
        <label className="text-[11px] text-content-tertiary tracking-wider block mb-0.5">
          Extra params (JSON)
        </label>
        <textarea
          rows={3}
          value={extraParamsText}
          spellCheck={false}
          onChange={(e) => {
            setExtraParamsText(e.target.value);
            // Push to parent only when JSON is valid — prevents clobbering with {}
            try {
              const parsed = JSON.parse(e.target.value || "{}");
              if (
                typeof parsed === "object" &&
                parsed !== null &&
                !Array.isArray(parsed)
              ) {
                onChange({ extra_params: parsed });
              }
            } catch {
              /* keep parent unchanged; UI shows error below */
            }
          }}
          placeholder='{ "temperature": 0 }'
          className={`w-full px-2 py-1 bg-bg-base border text-[12px] text-content-primary font-mono focus:outline-none resize-y ${
            parsedExtra.ok
              ? "border-border-minimal focus:border-accent-main"
              : "border-error/60"
          }`}
        />
        {!parsedExtra.ok && (
          <div className="text-[11px] text-error mt-0.5">{parsedExtra.error}</div>
        )}
      </div>

      {/* Test result */}
      {testResult && (
        <div
          className={`flex items-start gap-2 px-2 py-1.5 text-[11px] border rounded ${
            testResult.ok
              ? "border-emerald-500/30 bg-emerald-500/5 text-emerald-300"
              : "border-error/30 bg-error/5 text-error"
          }`}
        >
          {testResult.ok ? (
            <span>
              ✓ Connected · HTTP {testResult.status}
              {testResult.latency_ms != null ? ` · ${testResult.latency_ms}ms` : ""}
            </span>
          ) : (
            <span className="break-words">
              ✗ {testResult.error || "Connection failed"}
              {testResult.status ? ` (${testResult.status})` : ""}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
