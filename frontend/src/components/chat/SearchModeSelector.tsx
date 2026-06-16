// SearchModeSelector.tsx (Phase 27)
// 2-way pill in the ToggleBar: Default · Global. Sits next to
// ReasoningModeSelector. Read at send-time by App.tsx.handleSend and
// injected as `overrides.search_mode` on the chat request.
//
// Default ("local"): the tier-driven path. Your retrieval tier
//   (Vector / Hybrid / Graph) decides the depth and the work — vectors,
//   +lexical+hydration, or +graph expansion — returning deep evidence
//   chunks. This is the normal mode; nothing silently inflates it.
// Global: an explicit overview button. Returns SUMMARY chunks only
//   (Funnel A, no Funnel B, no hydration) — the LLM sees ~50 summaries
//   instead of ~8 full chunks. Click it when you want a thematic,
//   corpus-wide skim ("what are the main themes", "summarize my library").

import { Telescope } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";

type Mode = "local" | "global";

const MODES: Array<{ id: Mode; label: string; hint: string }> = [
  {
    id: "local",
    label: "default",
    hint: "Default tier-driven retrieval. Your tier (Vector / Hybrid / Graph) drives the depth and the wait — deep evidence chunks, hydrated to full text. Nothing auto-upgrades to the overview path.",
  },
  {
    id: "global",
    label: "global",
    hint: "Explicit overview. Summaries only (Funnel A) — ~50 summaries instead of ~8 full chunks. Click for thematic / corpus-wide queries: 'what are the main themes', 'summarize my library'. Ignores the tier's deep-evidence work.",
  },
];

export function SearchModeSelector() {
  const { searchMode, setSearchMode } = useSettingsStore();
  // Legacy "auto" maps to Default (the backend now resolves auto→local).
  const effectiveMode = searchMode === "global" ? "global" : "local";

  return (
    <div
      role="radiogroup"
      aria-label="Search mode"
      className="flex items-center gap-0.5 border border-transparent hover:border-border-minimal transition-none rounded-none px-1 py-0.5"
      title="Search mode — Default lets your tier drive retrieval; click Global for a thematic ~50-summary overview."
    >
      <Telescope className="w-3.5 h-3.5 text-content-tertiary mr-1" />
      {MODES.map((m) => {
        const active = effectiveMode === m.id;
        return (
          <button
            key={m.id}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => setSearchMode(m.id)}
            title={m.hint}
            className={
              "px-1.5 py-0.5 text-[10px] font-bold tracking-widest uppercase transition-none rounded-none " +
              (active
                ? "text-accent-main bg-accent-main/10"
                : "text-content-tertiary hover:text-content-secondary")
            }
          >
            {m.label}
          </button>
        );
      })}
    </div>
  );
}
