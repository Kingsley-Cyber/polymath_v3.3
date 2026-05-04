// IngestionModelPool.tsx
// Chip-based editor for a list<ModelProfileRef>. Add-row at top captures
// provider preset + URL + model + concurrency + api_key; pressing Enter
// pushes a chip onto the list. Chips are removable. Readonly mode shows
// chips only (used on the "mirrored" extraction card when models_linked).

import { useState, useRef, useEffect } from "react";
import { Plus, X, KeyRound, Cpu } from "lucide-react";
import type { ModelProfileRef } from "../../types";
import { PROVIDER_PRESETS, composeModelString } from "../../types";

type PoolPreset = {
  id: string;
  name: string;
  base_url: string;
  example_model: string;
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
}

const EMPTY_DRAFT = {
  provider_preset: "",
  model: "",
  base_url: "",
  api_key: "",
  max_concurrent: 1,
  // 0 means "auto-detect from model_pool / utils.tokens registry". User
  // can override (e.g. 12288 for lfm2 fine-tunes whose context isn't in
  // the registry).
  context_length: 0,
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
}: Props) {
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [flashId, setFlashId] = useState<string | null>(null);
  const modelInputRef = useRef<HTMLInputElement>(null);

  const canAdd = draft.model.trim().length > 0;
  const rowDisabled = !editing || readOnly;

  const commit = () => {
    if (!canAdd) return;
    // Safety net: prefix with the preset's LiteLLM provider unless already
    // prefixed. SiliconFlow model IDs contain slashes but still need openai/*.
    const preset = presets.find((p) => p.id === draft.provider_preset);
    const bare = draft.model.trim();
    const finalModel = composeModel(draft.provider_preset, bare);
    const extraParams: Record<string, unknown> = preset?.kwargs
      ? { ...(preset.kwargs as Record<string, unknown>) }
      : {};
    const next: ModelProfileRef = {
      provider_preset: draft.provider_preset,
      model: finalModel,
      base_url: draft.base_url.trim() || null,
      api_key: draft.api_key ? draft.api_key : null,
      // Schema cap is 512; clamp UI input to that range. Was 64 historically.
      max_concurrent: Math.max(1, Math.min(512, Number(draft.max_concurrent) || 1)),
      extra_params: extraParams,
      // 0 → fall back to model_pool / registry resolution at corpus save.
      // Non-zero → user-supplied authoritative context window for this lane.
      context_length:
        Number(draft.context_length) > 0 ? Number(draft.context_length) : null,
    };
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
              {typeof m.context_length === "number" && m.context_length > 0 && (
                <span
                  className="flex items-center text-cyan-300"
                  title={`Context window: ${m.context_length.toLocaleString()} tokens (frozen)`}
                >
                  ⌬{(m.context_length / 1024).toFixed(0)}k
                </span>
              )}
              {m.api_key && (
                <KeyRound
                  className="w-2.5 h-2.5 text-emerald-400"
                  aria-label="api key set"
                />
              )}
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
            max={512}
            value={draft.max_concurrent}
            onChange={(e) =>
              setDraft({ ...draft, max_concurrent: Number(e.target.value) || 1 })
            }
            title="Max in-flight calls for this entry (1 — 512)"
            className="w-14 bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono text-center"
          />
          <input
            type="number"
            min={0}
            max={1048576}
            step={1024}
            value={draft.context_length}
            onChange={(e) =>
              setDraft({ ...draft, context_length: Number(e.target.value) || 0 })
            }
            placeholder="ctx"
            title="Context window in tokens (0 = auto-detect from model_pool / registry)"
            className="w-16 bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono text-center placeholder:text-content-tertiary"
          />
          <input
            type="password"
            value={draft.api_key}
            onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
            placeholder="api_key"
            className="w-[110px] bg-[#0b0c10] text-white border border-white/10 rounded px-1.5 py-1 text-[10px] font-mono placeholder:text-content-tertiary"
          />
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
    </div>
  );
}
