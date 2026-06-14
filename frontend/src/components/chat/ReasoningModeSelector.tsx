// ReasoningModeSelector.tsx (Phase 15)
// Per-query reasoning mode picker in the ToggleBar. Modeled on ToolSelector.
// User picks mode → types query → hits send. App.tsx.handleSend() reads
// settingsStore.reasoningMode at send time and injects into overrides.

import { useState, useRef, useEffect } from "react";
import { Brain, ChevronDown, Check, Zap } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import { REASONING_MODES, REASONING_RAW_MODES } from "../../types";

export function ReasoningModeSelector() {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const {
    reasoningMode,
    setReasoningMode,
    powerUserReasoning,
    togglePowerUserReasoning,
  } = useSettingsStore();

  const pool = powerUserReasoning ? REASONING_RAW_MODES : REASONING_MODES;

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

  const active =
    pool.find((m) => m.key === reasoningMode) ??
    REASONING_MODES.find((m) => m.key === reasoningMode) ??
    REASONING_RAW_MODES.find((m) => m.key === reasoningMode);
  const isOff = !reasoningMode || reasoningMode === "none";

  // Short label for the trigger button — drop provider-style noise
  const triggerLabel = isOff
    ? "[REASON: OFF]"
    : `[REASON: ${active?.label.toUpperCase() ?? reasoningMode}]`;

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        data-testid="strategy-selector"
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-1.5 px-2 py-1 text-[10px] font-bold tracking-widest uppercase border border-transparent hover:border-border-minimal transition-none rounded-none group"
        title={
          active
            ? `${active.label}: ${active.description}`
            : "Pick a reasoning style before pressing send"
        }
      >
        <Brain className="w-3.5 h-3.5 text-content-tertiary group-hover:text-accent-main" />
        <span className="text-content-secondary">{triggerLabel}</span>
        <ChevronDown className="w-3 h-3 text-content-tertiary group-hover:text-accent-main" />
      </button>

      {isOpen && (
        <div className="absolute top-full right-0 mt-1 w-80 max-w-[calc(100vw-1rem)] max-h-[calc(100dvh-7rem)] overflow-hidden border border-white/10 bg-[#2a2a2a] z-[60] p-1 shadow-xl rounded">
          <div className="flex items-center justify-between gap-2 px-2 py-1.5 border-b border-border-minimal mb-1">
            <div className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary">
              Reasoning Mode (for next send)
            </div>
            <button
              onClick={(e) => {
                e.stopPropagation();
                togglePowerUserReasoning();
              }}
              title={
                powerUserReasoning
                  ? "Power user: showing 40 raw modes. Click to switch back to 12 curated."
                  : "Click to unlock 40 raw reasoning modes."
              }
              className={`flex items-center gap-1 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-widest border rounded-sm transition-colors ${powerUserReasoning
                  ? "border-amber-500/60 text-amber-300 bg-amber-950/30"
                  : "border-border-minimal text-content-tertiary hover:border-amber-500/40 hover:text-amber-400"
                }`}
            >
              <Zap className="w-2.5 h-2.5" />
              {powerUserReasoning ? "RAW 40" : "POWER"}
            </button>
          </div>
          <div className="max-h-[min(20rem,calc(100dvh-12rem))] overflow-y-auto custom-scrollbar flex flex-col gap-0.5">
            {pool.map((mode) => {
              const selected = mode.key === reasoningMode;
              return (
                <button
                  key={mode.key}
                  onClick={() => {
                    setReasoningMode(mode.key);
                    setIsOpen(false);
                  }}
                  title={mode.description}
                  className={`flex items-start gap-2 px-2 py-1.5 text-left border transition-none rounded-none text-content-secondary hover:text-content-primary ${selected
                      ? "bg-accent-main/10 border-accent-main"
                      : "border-transparent hover:bg-bg-base"
                    }`}
                >
                  <div className="w-4 h-4 border border-border-minimal flex-shrink-0 flex items-center justify-center bg-bg-base mt-0.5">
                    {selected && <Check className="w-3 h-3 text-accent-main" />}
                  </div>
                  <div className="flex-1 overflow-hidden">
                    <div className="text-[10px] font-bold tracking-widest uppercase truncate flex items-center gap-1">
                      {mode.label}
                      {mode.retrievalLevel && (
                        <Zap
                          className="w-3 h-3 text-amber-400 flex-shrink-0"
                          aria-label="extra LLM call"
                        />
                      )}
                    </div>
                    <div className="text-[9px] text-content-tertiary">
                      {mode.description}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
          <div className="text-[9px] text-content-tertiary px-2 py-1.5 border-t border-border-minimal mt-1">
            <Zap className="w-3 h-3 inline text-amber-400 mr-1" />
            modes with the bolt icon trigger extra LLM calls (~2-3× cost).
          </div>
        </div>
      )}
    </div>
  );
}
