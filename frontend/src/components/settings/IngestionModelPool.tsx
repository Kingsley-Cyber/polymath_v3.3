// IngestionModelPool.tsx
// Chip-based editor for a list<ModelProfileRef>. Add-row at top captures
// provider preset + URL + model + concurrency + api_key; pressing Enter
// pushes a chip onto the list. Chips are removable. Readonly mode shows
// chips only (used on the "mirrored" extraction card when models_linked).

import { useState, useRef, useEffect } from "react";
import {
  Plus,
  X,
  KeyRound,
  Cpu,
  Loader2,
  Zap,
  CheckCircle,
  AlertTriangle,
  Power,
  List,
  Cloud,
  Server,
} from "lucide-react";
import type { ModelProfileRef } from "../../types";
import { PROVIDER_PRESETS, composeModelString } from "../../types";
import * as api from "../../lib/api";
import type {
  IngestionModelListResult,
  IngestionModelPoolField,
  IngestionModelTestKind,
  IngestionModelTestResult,
} from "../../lib/api";

type PoolPreset = {
  id: string;
  name: string;
  base_url: string;
  example_model: string;
  default_max_concurrent?: number;
  example_models?: string[];
  model_dropdown_only?: boolean;
  lifecycle?: {
    base_url: string;
    auto_start?: boolean;
    auto_stop?: boolean;
    ready_timeout_seconds?: number;
    idle_shutdown_seconds?: number;
  };
  kwargs?: Record<string, unknown>;
};

interface Props {
  title: string;
  subtitle?: string;
  value: ModelProfileRef[];
  onChange: (next: ModelProfileRef[]) => void;
  editing: boolean;
  /** When true, the add-row is hidden and chips are non-removable. */
  readOnly?: boolean;
  /** Hint shown when readOnly (e.g. "using Summary pool"). */
  readOnlyHint?: string;
  presets?: PoolPreset[];
  composeModel?: (presetId: string, model: string) => string;
  modelPlaceholder?: string;
  testKind?: IngestionModelTestKind;
  testContext?: {
    corpusId?: string | null;
    poolField?: IngestionModelPoolField;
  };
}

const EMPTY_DRAFT = {
  provider_preset: "",
  model: "",
  base_url: "",
  api_key: "",
  max_concurrent: 1,
  lifecycle_base_url: "",
  lifecycle_api_key: "",
  lifecycle_auto_start: false,
  lifecycle_auto_stop: false,
  lifecycle_ready_timeout_seconds: 360,
};

type RouteKind = "private_rtx" | "cloud_api" | "custom" | "local";

function inferRouteKind(entry: ModelProfileRef): RouteKind {
  const provider = (entry.provider_preset || "").toLowerCase();
  const base = (entry.base_url || "").toLowerCase();
  const model = (entry.model || "").toLowerCase();
  const extra = entry.extra_params || {};
  if (
    provider === "vllm-rtx" ||
    provider === "vllm" ||
    extra.resource_class === "rtx" ||
    extra.resource_class === "remote_vllm" ||
    extra.managed_vllm === true ||
    model.includes("polymath-extract") ||
    base.includes("192.168.") ||
    base.includes("host.docker.internal")
  ) {
    return "private_rtx";
  }
  if (base.startsWith("http")) return "cloud_api";
  if (provider) return "custom";
  return "local";
}

function routeBadge(kind: RouteKind): { label: string; className: string } {
  if (kind === "private_rtx") {
    return {
      label: "Private RTX",
      className: "border-cyan-300/40 bg-cyan-300/10 text-cyan-100",
    };
  }
  if (kind === "cloud_api") {
    return {
      label: "Cloud API",
      className: "border-sky-300/40 bg-sky-300/10 text-sky-100",
    };
  }
  if (kind === "custom") {
    return {
      label: "Custom",
      className: "border-content-secondary/30 bg-content-secondary/10 text-content-secondary",
    };
  }
  return {
    label: "Default",
    className: "border-border-minimal bg-bg-base text-content-tertiary",
  };
}

function RouteIcon({ kind }: { kind: RouteKind }) {
  const className = "w-3 h-3 shrink-0";
  if (kind === "private_rtx") return <Server className={className} />;
  if (kind === "cloud_api") return <Cloud className={className} />;
  return <Cpu className={className} />;
}

export function IngestionModelPool({
  title,
  subtitle,
  value,
  onChange,
  editing,
  readOnly = false,
  readOnlyHint,
  presets = PROVIDER_PRESETS,
  composeModel = composeModelString,
  modelPlaceholder = "model (required)",
  testKind = "chat",
  testContext,
}: Props) {
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [flashId, setFlashId] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [lastTest, setLastTest] = useState<{
    id: string;
    label: string;
    result: IngestionModelTestResult;
  } | null>(null);
  const [listingId, setListingId] = useState<string | null>(null);
  const [lastModels, setLastModels] = useState<{
    id: string;
    label: string;
    result: IngestionModelListResult;
  } | null>(null);
  const modelInputRef = useRef<HTMLInputElement>(null);

  const canAdd = draft.model.trim().length > 0;
  const rowDisabled = !editing || readOnly;
  const selectedPreset = presets.find((p) => p.id === draft.provider_preset);
  const lockedModelOptions = selectedPreset?.model_dropdown_only
    ? selectedPreset.example_models ?? []
    : [];

  const commit = () => {
    if (!canAdd) return;
    const next = buildDraftRef();
    onChange([...value, next]);
    const fid = `${Date.now()}-${value.length}`;
    setFlashId(fid);
    setDraft(EMPTY_DRAFT);
    setTimeout(() => modelInputRef.current?.focus(), 0);
  };

  useEffect(() => {
    if (!flashId) return;
    const t = setTimeout(() => setFlashId(null), 450);
    return () => clearTimeout(t);
  }, [flashId]);

  const remove = (idx: number) => {
    if (rowDisabled) return;
    onChange(value.filter((_, i) => i !== idx));
  };

  const buildDraftRef = (): ModelProfileRef => {
    const preset = presets.find((p) => p.id === draft.provider_preset);
    const bare = draft.model.trim();
    const extraParams: Record<string, unknown> = preset?.kwargs
      ? { ...(preset.kwargs as Record<string, unknown>) }
      : {};
    if (preset?.lifecycle?.idle_shutdown_seconds) {
      extraParams.lifecycle_idle_shutdown_seconds =
        preset.lifecycle.idle_shutdown_seconds;
    }
    return {
      provider_preset: draft.provider_preset,
      // Safety net: prefix with the preset's LiteLLM provider unless already
      // prefixed. SiliconFlow model IDs contain slashes but still need openai/*.
      model: composeModel(draft.provider_preset, bare),
      base_url: draft.base_url.trim() || null,
      api_key: draft.api_key ? draft.api_key : null,
      max_concurrent: Math.max(1, Math.min(256, Number(draft.max_concurrent) || 1)),
      lifecycle_base_url: draft.lifecycle_base_url.trim() || null,
      lifecycle_api_key: draft.lifecycle_api_key ? draft.lifecycle_api_key : null,
      lifecycle_auto_start: Boolean(draft.lifecycle_auto_start),
      lifecycle_auto_stop: Boolean(draft.lifecycle_auto_stop),
      lifecycle_up_path: "/up",
      lifecycle_status_path: "/status",
      lifecycle_down_path: "/down",
      lifecycle_ready_timeout_seconds: Math.max(
        5,
        Math.min(1800, Number(draft.lifecycle_ready_timeout_seconds) || 360),
      ),
      extra_params: extraParams,
    };
  };

  const testEntry = async (id: string, label: string, entry: ModelProfileRef, index?: number) => {
    if (!entry.model.trim()) return;
    setTestingId(id);
    setLastTest(null);
    try {
      const result = await api.testIngestionModelRef({
        kind: testKind,
        entry,
        corpus_id: testContext?.corpusId ?? null,
        pool_field: testContext?.poolField ?? null,
        index: index ?? null,
      });
      setLastTest({ id, label, result });
    } catch (err) {
      setLastTest({
        id,
        label,
        result: {
          ok: false,
          kind: testKind,
          model: entry.model,
          base_url: entry.base_url,
          error: err instanceof Error ? err.message : String(err),
        },
      });
    } finally {
      setTestingId(null);
    }
  };

  const listEntryModels = async (
    id: string,
    label: string,
    entry: ModelProfileRef,
    index?: number,
  ) => {
    if (!entry.base_url?.trim()) return;
    setListingId(id);
    setLastModels(null);
    try {
      const result = await api.listIngestionModelRefModels({
        kind: testKind,
        entry,
        corpus_id: testContext?.corpusId ?? null,
        pool_field: testContext?.poolField ?? null,
        index: index ?? null,
      });
      setLastModels({ id, label, result });
    } catch (err) {
      setLastModels({
        id,
        label,
        result: {
          ok: false,
          models: [],
          base_url: entry.base_url,
          error: err instanceof Error ? err.message : String(err),
        },
      });
    } finally {
      setListingId(null);
    }
  };

  const applyPreset = (presetId: string) => {
    const p = presets.find((pp) => pp.id === presetId);
    setDraft((d) => ({
      ...d,
      provider_preset: presetId,
      base_url: p?.base_url ?? d.base_url,
      // Compose `{litellm_provider}/{example_model}` so LiteLLM's wildcard
      // router can match. For "custom" / missing providers leave the model
      // field alone.
      model: p?.example_model ? composeModel(presetId, p.example_model) : d.model,
      max_concurrent: p?.default_max_concurrent ?? d.max_concurrent,
      lifecycle_base_url: p?.lifecycle?.base_url ?? d.lifecycle_base_url,
      lifecycle_auto_start: p?.lifecycle?.auto_start ?? d.lifecycle_auto_start,
      lifecycle_auto_stop: p?.lifecycle?.auto_stop ?? d.lifecycle_auto_stop,
      lifecycle_ready_timeout_seconds:
        p?.lifecycle?.ready_timeout_seconds ?? d.lifecycle_ready_timeout_seconds,
    }));
  };

  return (
    <div className="bg-[#121418] border border-white/5 rounded-lg p-3 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[11px] font-bold tracking-widest uppercase text-content-primary">
            {title}
          </div>
          {subtitle && (
            <div className="text-[10px] text-content-tertiary mt-0.5">
              {subtitle}
            </div>
          )}
        </div>
        <div className="text-[9px] tracking-widest uppercase text-content-tertiary">
          {value.length} {value.length === 1 ? "model" : "models"}
        </div>
      </div>

      {/* Chips */}
      <div className="flex flex-wrap gap-1.5">
        {value.length === 0 && (
          <div className="text-[10px] italic text-content-tertiary px-1 py-0.5">
            {readOnly
              ? (readOnlyHint ?? "No models configured.")
              : "No models yet — add one below."}
          </div>
        )}
        {value.map((m, idx) => {
          const fid = `${idx}-${m.model}`;
          const testId = `chip-${idx}`;
          const kind = inferRouteKind(m);
          const badge = routeBadge(kind);
          const isFresh =
            flashId !== null &&
            idx === value.length - 1 &&
            flashId.endsWith(`-${value.length - 1}`);
          return (
            <div
              key={fid}
              className={`group flex flex-wrap items-center gap-1.5 px-2 py-1 rounded border text-[10px] font-mono tracking-wide transition-all ${
                isFresh
                  ? "border-accent-main bg-accent-main/15 scale-[1.03]"
                  : "border-white/10 bg-[#0b0c10]"
              }`}
              title={`${m.provider_preset || "custom"} • ${m.base_url || "default gateway"} • conc=${m.max_concurrent}${
                m.lifecycle_auto_start && m.lifecycle_base_url
                  ? ` • warms ${m.lifecycle_base_url}`
                  : ""
              }`}
            >
              <span
                className={`inline-flex items-center gap-1 border px-1 py-0.5 text-[8px] font-bold tracking-widest uppercase ${badge.className}`}
              >
                <RouteIcon kind={kind} />
                {badge.label}
              </span>
              <span className="text-content-tertiary uppercase text-[8px]">
                {m.provider_preset || "custom"}
              </span>
              <span className="text-white">{m.model}</span>
              <span className="flex items-center gap-0.5 text-amber-300">
                <Cpu className="w-2.5 h-2.5" />
                {m.max_concurrent}
              </span>
              {m.lifecycle_auto_start && m.lifecycle_base_url && (
                <span className="flex items-center gap-0.5 text-cyan-300">
                  <Power className="w-2.5 h-2.5" />
                  up
                </span>
              )}
              {m.api_key && (
                <KeyRound
                  className="w-2.5 h-2.5 text-emerald-400"
                  aria-label="api key set"
                />
              )}
              <button
                type="button"
                onClick={() => testEntry(testId, m.model, m, idx)}
                disabled={testingId !== null}
                className="ml-0.5 inline-flex items-center gap-0.5 border border-border-minimal px-1 py-0.5 text-[8px] font-bold uppercase text-content-tertiary hover:text-accent-main hover:border-accent-main disabled:opacity-40"
                title="Test API key and model connection"
              >
                {testingId === testId ? (
                  <Loader2 className="w-2.5 h-2.5 animate-spin" />
                ) : lastTest?.id === testId && lastTest.result.ok ? (
                  <CheckCircle className="w-2.5 h-2.5 text-emerald-400" />
                ) : lastTest?.id === testId && !lastTest.result.ok ? (
                  <AlertTriangle className="w-2.5 h-2.5 text-error" />
                ) : (
                  <Zap className="w-2.5 h-2.5" />
                )}
                Test
              </button>
              <button
                type="button"
                onClick={() => listEntryModels(testId, m.model, m, idx)}
                disabled={listingId !== null || !m.base_url}
                className="ml-0.5 inline-flex items-center gap-0.5 border border-border-minimal px-1 py-0.5 text-[8px] font-bold uppercase text-content-tertiary hover:text-accent-main hover:border-accent-main disabled:opacity-40"
                title="List live models from this endpoint"
              >
                {listingId === testId ? (
                  <Loader2 className="w-2.5 h-2.5 animate-spin" />
                ) : (
                  <List className="w-2.5 h-2.5" />
                )}
                Models
              </button>
              {!readOnly && editing && (
                <button
                  onClick={() => remove(idx)}
                  className="ml-0.5 text-content-tertiary hover:text-red-400"
                  title="Remove"
                >
                  <X className="w-2.5 h-2.5" />
                </button>
              )}
            </div>
          );
        })}
      </div>

      {/* Add row — hidden when readOnly */}
      {!readOnly && (
        <div
          className={`flex flex-wrap items-center gap-1.5 border-t border-white/5 pt-2.5 ${
            rowDisabled ? "opacity-50 pointer-events-none" : ""
          }`}
        >
          <div className="basis-full flex flex-wrap items-center justify-between gap-2 text-[9px] text-content-tertiary">
            <span className="font-bold tracking-widest uppercase">
              Add model card
            </span>
            <span>
              Provider → Base URL → Model → Concurrency → Key. Use Test or Models before Add.
            </span>
          </div>
          <select
            value={draft.provider_preset}
            onChange={(e) => applyPreset(e.target.value)}
            className="bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono min-w-[110px]"
          >
            <option value="" className="bg-[#0b0c10] text-white">
              custom
            </option>
            {presets.map((p) => (
              <option
                key={p.id}
                value={p.id}
                className="bg-[#0b0c10] text-white"
              >
                {p.name}
              </option>
            ))}
          </select>
          <input
            type="text"
            value={draft.base_url}
            onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
            placeholder="base_url (blank = default)"
            className="flex-1 min-w-full sm:min-w-[140px] bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono placeholder:text-content-tertiary"
          />
          {lockedModelOptions.length > 0 ? (
            <select
              value={draft.model}
              onChange={(e) => setDraft({ ...draft, model: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commit();
                }
              }}
              className="flex-1 min-w-full sm:min-w-[130px] bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono"
              title="Approved MiMo extraction models"
            >
              {lockedModelOptions.map((model) => (
                <option
                  key={model}
                  value={composeModel(draft.provider_preset, model)}
                  className="bg-[#0b0c10] text-white"
                >
                  {model}
                </option>
              ))}
            </select>
          ) : (
            <input
              ref={modelInputRef}
              type="text"
              value={draft.model}
              onChange={(e) => setDraft({ ...draft, model: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commit();
                }
              }}
              placeholder={modelPlaceholder}
              className="flex-1 min-w-full sm:min-w-[130px] bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono placeholder:text-content-tertiary"
            />
          )}
          <input
            type="number"
            min={1}
            max={256}
            value={draft.max_concurrent}
            onChange={(e) =>
              setDraft({ ...draft, max_concurrent: Number(e.target.value) || 1 })
            }
            title="Max in-flight calls for this entry"
            className="w-14 bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono text-center"
          />
          <input
            type="password"
            value={draft.api_key}
            onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
            placeholder="api_key"
            className="w-full sm:w-[110px] bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono placeholder:text-content-tertiary"
          />
          <label className="flex items-center gap-1.5 px-1.5 py-1 text-[10px] font-bold uppercase tracking-widest text-content-secondary">
            <input
              type="checkbox"
              checked={draft.lifecycle_auto_start}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  lifecycle_auto_start: e.target.checked,
                })
              }
              className="accent-accent-main"
            />
            Managed
          </label>
          {draft.lifecycle_auto_start && (
            <div className="flex flex-wrap items-center gap-1.5 basis-full">
              <input
                type="text"
                value={draft.lifecycle_base_url}
                onChange={(e) =>
                  setDraft({ ...draft, lifecycle_base_url: e.target.value })
                }
                placeholder="control base_url for /up + /status"
                className="flex-1 min-w-full sm:min-w-[220px] bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono placeholder:text-content-tertiary"
              />
              <input
                type="password"
                value={draft.lifecycle_api_key}
                onChange={(e) =>
                  setDraft({ ...draft, lifecycle_api_key: e.target.value })
                }
                placeholder="X-Api-Key"
                className="w-full sm:w-[120px] bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono placeholder:text-content-tertiary"
              />
              <input
                type="number"
                min={5}
                max={1800}
                value={draft.lifecycle_ready_timeout_seconds}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    lifecycle_ready_timeout_seconds: Number(e.target.value) || 360,
                  })
                }
                title="Seconds to wait for ready:true after /up"
                className="w-16 bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono text-center"
              />
              <label className="flex items-center gap-1.5 px-1.5 py-1 text-[10px] text-content-secondary">
                <input
                  type="checkbox"
                  checked={draft.lifecycle_auto_stop}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      lifecycle_auto_stop: e.target.checked,
                    })
                  }
                  className="accent-accent-main"
                />
                idle stop
              </label>
            </div>
          )}
          <button
            type="button"
            onClick={() => testEntry("draft", "draft model", buildDraftRef())}
            disabled={!canAdd || testingId !== null}
            className="flex items-center gap-1 px-2 py-1 rounded border border-border-minimal text-content-secondary text-[10px] font-bold uppercase tracking-widest hover:border-accent-main hover:text-accent-main disabled:opacity-40 disabled:cursor-not-allowed"
            title="Test API key and model connection before adding"
          >
            {testingId === "draft" ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <Zap className="w-3 h-3" />
            )}
            Test route
          </button>
          <button
            type="button"
            onClick={() => listEntryModels("draft", "draft route", buildDraftRef())}
            disabled={!draft.base_url.trim() || listingId !== null}
            className="flex items-center gap-1 px-2 py-1 rounded border border-border-minimal text-content-secondary text-[10px] font-bold uppercase tracking-widest hover:border-accent-main hover:text-accent-main disabled:opacity-40 disabled:cursor-not-allowed"
            title="List live models from base_url + /models"
          >
            {listingId === "draft" ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <List className="w-3 h-3" />
            )}
            List models
          </button>
          <button
            onClick={commit}
            disabled={!canAdd}
            className="flex items-center gap-1 px-2 py-1 rounded border border-accent-main/60 bg-accent-main/10 text-accent-main text-[10px] font-bold uppercase tracking-widest hover:bg-accent-main/20 disabled:opacity-40 disabled:cursor-not-allowed"
            title="Add to pool (or press Enter)"
          >
            <Plus className="w-3 h-3" />
            Add route
          </button>
        </div>
      )}

      {lastTest && (
        <div
          className={`flex items-start gap-2 px-2 py-1.5 rounded border text-[10px] font-mono leading-snug ${
            lastTest.result.ok
              ? "border-emerald-500/30 bg-emerald-500/5 text-emerald-300"
              : "border-error/30 bg-error/5 text-error"
          }`}
        >
          {lastTest.result.ok ? (
            <CheckCircle className="w-3 h-3 shrink-0 mt-0.5" />
          ) : (
            <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
          )}
          <span className="break-words">
            {lastTest.label}:{" "}
            {lastTest.result.ok
              ? `connected · HTTP ${lastTest.result.status ?? "ok"} · ${
                  lastTest.result.latency_ms ?? "?"
                }ms${
                  lastTest.result.dimension
                    ? ` · ${lastTest.result.dimension}d`
                    : ""
                }`
              : lastTest.result.error || "connection failed"}
          </span>
        </div>
      )}

      {lastModels && (
        <div
          className={`px-2 py-1.5 rounded border text-[10px] font-mono leading-snug ${
            lastModels.result.ok
              ? "border-cyan-400/30 bg-cyan-400/5 text-cyan-200"
              : "border-error/30 bg-error/5 text-error"
          }`}
        >
          <div className="flex items-start gap-2">
            {lastModels.result.ok ? (
              <CheckCircle className="w-3 h-3 shrink-0 mt-0.5" />
            ) : (
              <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
            )}
            <span className="break-words">
              {lastModels.label}:{" "}
              {lastModels.result.ok
                ? `${lastModels.result.models.length} live model(s) · ${
                    lastModels.result.latency_ms ?? "?"
                  }ms`
                : lastModels.result.error || "model list failed"}
            </span>
          </div>
          {lastModels.result.ok && lastModels.result.models.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {lastModels.result.models.slice(0, 24).map((model) => (
                <button
                  key={model}
                  type="button"
                  onClick={() =>
                    setDraft((prev) => ({
                      ...prev,
                      model: composeModel(prev.provider_preset, model),
                    }))
                  }
                  className="px-1.5 py-0.5 rounded border border-cyan-300/20 bg-cyan-300/5 text-cyan-100 hover:border-cyan-200"
                  title="Use this model name in the draft row"
                >
                  {model}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
