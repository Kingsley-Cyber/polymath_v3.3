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
} from "lucide-react";
import type { ModelProfileRef } from "../../types";
import { PROVIDER_PRESETS, composeModelString } from "../../types";
import * as api from "../../lib/api";
import type {
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
};

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
  const modelInputRef = useRef<HTMLInputElement>(null);

  const canAdd = draft.model.trim().length > 0;
  const rowDisabled = !editing || readOnly;

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
    return {
      provider_preset: draft.provider_preset,
      // Safety net: prefix with the preset's LiteLLM provider unless already
      // prefixed. SiliconFlow model IDs contain slashes but still need openai/*.
      model: composeModel(draft.provider_preset, bare),
      base_url: draft.base_url.trim() || null,
      api_key: draft.api_key ? draft.api_key : null,
      max_concurrent: Math.max(1, Math.min(64, Number(draft.max_concurrent) || 1)),
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
          const isFresh =
            flashId !== null &&
            idx === value.length - 1 &&
            flashId.endsWith(`-${value.length - 1}`);
          return (
            <div
              key={fid}
              className={`group flex items-center gap-1.5 px-2 py-1 rounded border text-[10px] font-mono tracking-wide transition-all ${
                isFresh
                  ? "border-accent-main bg-accent-main/15 scale-[1.03]"
                  : "border-white/10 bg-[#0b0c10]"
              }`}
              title={`${m.provider_preset || "custom"} • ${m.base_url || "default gateway"} • conc=${m.max_concurrent}`}
            >
              <span className="text-content-tertiary uppercase text-[8px]">
                {m.provider_preset || "custom"}
              </span>
              <span className="text-white">{m.model}</span>
              <span className="flex items-center gap-0.5 text-amber-300">
                <Cpu className="w-2.5 h-2.5" />
                {m.max_concurrent}
              </span>
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
                className="ml-0.5 text-content-tertiary hover:text-accent-main disabled:opacity-40"
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
            className="flex-1 min-w-[140px] bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono placeholder:text-content-tertiary"
          />
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
            className="flex-1 min-w-[130px] bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono placeholder:text-content-tertiary"
          />
          <input
            type="number"
            min={1}
            max={64}
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
            className="w-[110px] bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono placeholder:text-content-tertiary"
          />
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
            Test
          </button>
          <button
            onClick={commit}
            disabled={!canAdd}
            className="flex items-center gap-1 px-2 py-1 rounded border border-accent-main/60 bg-accent-main/10 text-accent-main text-[10px] font-bold uppercase tracking-widest hover:bg-accent-main/20 disabled:opacity-40 disabled:cursor-not-allowed"
            title="Add to pool (or press Enter)"
          >
            <Plus className="w-3 h-3" />
            Add
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
    </div>
  );
}
