// SearchModeSelector.tsx (Phase 27)
// 3-way pill in the ToggleBar: Auto · Local · Global. Sits next to
// ReasoningModeSelector. Read at send-time by App.tsx.handleSend and
// injected as `overrides.search_mode` on the chat request.
//
// Auto (default): backend infers local vs global from query shape
//   (broad/thematic → global, specific → local). Safe default.
// Local: forces the full retrieval pipeline (vector + BM25 + graph
//   expansion + rerank + hydrate to full parent text). Best for
//   specific questions, debugging, exact citations.
// Global: returns SUMMARY chunks only (Funnel A, no Funnel B, no
//   hydration). Lets the LLM see ~50 summaries instead of ~5 full
//   chunks — powers "what are the main themes?" type queries that
//   are structurally impossible in local mode.

import { Telescope } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";

type Mode = "auto" | "local" | "global";

const MODES: Array<{ id: Mode; label: string; hint: string }> = [
  {
    id: "auto",
    label: "auto",
    hint: "Backend picks local vs global from query shape. Safe default — broad questions get summaries, specific questions get full text.",
  },
  {
    id: "local",
    label: "local",
    hint: "Full retrieval pipeline (vector + BM25 + graph + rerank + hydrate). Specific questions, debugging, exact citations.",
  },
  {
    id: "global",
    label: "global",
    hint: "Summaries only (Funnel A). Thematic / corpus-wide queries — 'what are the main themes', 'summarize my library'. Impossible in local mode.",
  },
];

export function SearchModeSelector() {
  const { searchMode, setSearchMode } = useSettingsStore();

  return (
    <div
      role="radiogroup"
      aria-label="Search mode"
      className="flex items-center gap-0.5 border border-transparent hover:border-border-minimal transition-none rounded-none px-1 py-0.5"
      title="Search mode — Auto infers from your query; switch to Local for exact code, Global for thematic overviews."
    >
      <Telescope className="w-3.5 h-3.5 text-content-tertiary mr-1" />
      {MODES.map((m) => {
        const active = searchMode === m.id;
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
