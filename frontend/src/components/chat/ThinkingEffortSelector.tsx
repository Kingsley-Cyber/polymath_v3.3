// ThinkingEffortSelector.tsx (Phase 28)
//
// Per-turn reasoning-effort dropdown for the top bar. Renders ONLY when
// the currently selected model exposes a thinking dial (OpenAI o-series,
// Claude 3.7+, Gemini 2.5+, DeepSeek-R1). For non-reasoning models the
// component returns null — zero visual clutter for the 90% of queries
// that hit a regular chat model.
//
// Read at send-time by App.tsx.handleSend, injected as
// `overrides.thinking_effort`. Backend's services/thinking_mapper.py
// translates the agnostic value to provider-native body params.

import { useEffect, useRef, useState } from "react";
import { Brain, ChevronDown } from "lucide-react";

import { useSettingsStore } from "../../stores/settingsStore";
import { useQueryModelPoolStore } from "../../stores/queryModelPoolStore";
import {
  supportsThinking,
  whyNoThinking,
  THINKING_EFFORT_OPTIONS,
  type ThinkingEffort,
} from "../../lib/modelCapabilities";

/**
 * Resolve the selected model string to a raw provider/model id.
 * The settings store may hold a `pool:<entry_id>` reference; the
 * capability heuristic needs the actual model name to pattern-match.
 */
function useResolvedModelName(): string {
  const { selectedModel } = useSettingsStore();
  const pool = useQueryModelPoolStore((s) => s.config.query_model_pool);
  if (!selectedModel) return "";
  if (selectedModel.startsWith("pool:")) {
    const id = selectedModel.slice("pool:".length);
    const entry = pool.find((e) => e.entry_id === id);
    return entry?.model_name ?? "";
  }
  if (selectedModel.startsWith("profile:")) {
    // Profiles wrap a pool entry; we don't have the profile→entry map in
    // this surface. Treat as unknown → selector hides. That's fine —
    // most thinking-capable models are selected directly from the pool.
    return "";
  }
  return selectedModel;
}

export function ThinkingEffortSelector() {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const { thinkingEffort, setThinkingEffort } = useSettingsStore();
  const modelName = useResolvedModelName();
  const thinkingSupported = supportsThinking(modelName);

  // Renders ALWAYS now. When the selected model doesn't expose a thinking
  // dial, the selector shows a "[THINK: N/A]" disabled trigger with a
  // tooltip explaining why and a hint pointing at a thinking-capable
  // model. This trades a tiny bit of visual clutter for discoverability
  // — users no longer have to guess that the dial exists at all.

  // Click-outside to close (mirrors ReasoningModeSelector pattern).
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(event.target as Node)
      ) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const active = THINKING_EFFORT_OPTIONS.find(
    (o) => o.value === thinkingEffort,
  );
  const triggerLabel = thinkingSupported
    ? `[THINK: ${(active?.label ?? "AUTO").toUpperCase()}]`
    : "[THINK: N/A]";
  const disabledHint = thinkingSupported
    ? null
    : whyNoThinking(modelName) +
      " Pick a thinking-capable model (DeepSeek V4, Magistral, GLM-5, Claude, o-series, Gemini 2.5+).";

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        data-testid="thinking-effort-selector"
        onClick={() => setIsOpen(!isOpen)}
        disabled={!thinkingSupported}
        className={`flex items-center gap-1.5 px-2 py-1 text-[10px] font-bold tracking-widest uppercase border transition-none rounded-none group ${
          thinkingSupported
            ? "border-transparent hover:border-border-minimal"
            : "border-transparent opacity-50 cursor-not-allowed"
        }`}
        title={
          disabledHint ??
          active?.hint ??
          "Thinking effort — applies to reasoning models (DeepSeek V4, Magistral, GLM-5, Claude, o-series, Gemini 2.5+)."
        }
      >
        <Brain
          className={`w-3.5 h-3.5 ${
            thinkingSupported
              ? "text-content-tertiary group-hover:text-accent-main"
              : "text-content-tertiary"
          }`}
        />
        <span
          className={`hidden sm:inline-block ${
            thinkingSupported ? "text-content-secondary" : "text-content-tertiary"
          }`}
        >
          {triggerLabel}
        </span>
        <ChevronDown
          className={`w-3 h-3 ${
            thinkingSupported
              ? "text-content-tertiary group-hover:text-accent-main"
              : "text-content-tertiary"
          }`}
        />
      </button>

      {isOpen && thinkingSupported && (
        // bottom-full — pops the panel UP (the selector now lives in the
        // ChatInput orchestration row, which is near the bottom of the
        // viewport). Top-full would clip against the page edge.
        <div className="absolute bottom-full right-0 mb-1 w-64 border border-white/10 bg-[#2a2a2a] z-[60] p-1 shadow-xl rounded">
          <div className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary px-2 py-1.5 border-b border-border-minimal mb-1">
            Thinking Effort
          </div>
          {THINKING_EFFORT_OPTIONS.map((opt) => {
            const isActive = thinkingEffort === opt.value;
            return (
              <button
                key={opt.value}
                onClick={() => {
                  setThinkingEffort(opt.value as ThinkingEffort);
                  setIsOpen(false);
                }}
                title={opt.hint}
                className={`w-full text-left px-2 py-1.5 text-[10px] font-bold tracking-widest uppercase border transition-none rounded-none flex flex-col items-start gap-0.5 ${
                  isActive
                    ? "bg-accent-main/10 border-accent-main text-accent-main"
                    : "border-transparent text-content-secondary hover:bg-bg-base hover:text-content-primary"
                }`}
              >
                <span>{opt.label}</span>
                <span className="text-[8px] tracking-wider normal-case font-normal text-content-tertiary truncate w-full">
                  {opt.hint}
                </span>
              </button>
            );
          })}
          <div className="px-2 py-1.5 mt-1 border-t border-border-minimal text-[8px] text-content-tertiary uppercase tracking-wider text-center">
            Model: {modelName.slice(-40)}
          </div>
        </div>
      )}
    </div>
  );
}
