// ToggleBar.tsx — Per-query toggles
// Phase 24: agentic toggle KILLED. Tool selection itself activates the
// ReAct loop; auto-fallback to the agentic pool entry happens silently
// when the chat model can't tool-call. Reasoning Cascade added.
import { Brain, Globe2, Telescope } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import { useQueryModelPoolStore } from "../../stores/queryModelPoolStore";
import { ActivatorSelector } from "./ActivatorSelector";

interface ToggleBarProps {
  className?: string;
}

export function ToggleBar({ className = "" }: ToggleBarProps) {
  const {
    hydeEnabled,
    toggleHyDE,
    webSearchEnabled,
    toggleWebSearch,
    reasoningCascadeEnabled,
    toggleReasoningCascade,
    selectedToolIds,
  } = useSettingsStore();

  // Phase 24 — auto-fallback badge. Shows the model that WILL handle the
  // tool call when tools are selected. Kept visible so the model swap is
  // never invisible — the user always knows what's about to run.
  const { config: poolConfig } = useQueryModelPoolStore();
  const fallbackEntry = poolConfig.query_model_pool.find(
    (e) => e.entry_id === poolConfig.agentic.pool_entry_id,
  );
  const fallbackLabel = fallbackEntry?.model_name
    ? fallbackEntry.model_name.includes("/")
      ? fallbackEntry.model_name.split("/").slice(1).join("/")
      : fallbackEntry.model_name
    : null;

  return (
    <div
      className={`
        flex items-center gap-2 sm:gap-4 px-1
        ${className}
      `}
    >
      <ToggleButton
        icon={Brain}
        label="HyDE"
        description="Think twice, search once — re-writes query before retrieval"
        isActive={hydeEnabled}
        onClick={toggleHyDE}
        activeColor="bg-accent-main"
      />

      <ToggleButton
        icon={Telescope}
        label="Reason"
        description="Reasoning cascade — analyst digests retrieved chunks before chat model writes (~20× cost)"
        isActive={reasoningCascadeEnabled}
        onClick={toggleReasoningCascade}
        activeColor="bg-accent-secondary"
      />

      <ToggleButton
        icon={Globe2}
        label="Web"
        description="Opt in to live web context through local SearXNG for this chat turn"
        isActive={webSearchEnabled}
        onClick={toggleWebSearch}
        activeColor="bg-accent-main"
      />

      {selectedToolIds.length > 0 && fallbackLabel && (
        <span
          className="text-[9px] font-bold uppercase tracking-widest text-amber-300/80 border border-amber-500/30 bg-amber-950/20 px-1.5 py-0.5"
          title={`Tools active — auto-routing to ${fallbackLabel} for tool support`}
        >
          AUTO: {fallbackLabel}
        </span>
      )}

      <div className="h-4 w-px bg-border-minimal" />
      <ActivatorSelector />
    </div>
  );
}

interface ToggleButtonProps {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  description: string;
  isActive: boolean;
  onClick: () => void;
  activeColor: string;
}

function ToggleButton({
  icon: Icon,
  label,
  description,
  isActive,
  onClick,
  activeColor,
}: ToggleButtonProps) {
  // `accent` derives directly from the activeColor token name so the four
  // visual roles (knob, text, border, tint) stay in lock-step. Anything
  // containing "secondary" → secondary palette; default → main.
  const accent = activeColor.includes("secondary")
    ? {
        knob: "bg-accent-secondary",
        text: "text-accent-secondary",
        border: "border-accent-secondary/60",
        tint: "bg-accent-secondary/10",
      }
    : {
        knob: "bg-accent-main",
        text: "text-accent-main",
        border: "border-accent-main/60",
        tint: "bg-accent-main/10",
      };

  // The `!` prefix on transition utilities is REQUIRED. The global
  // `* { @apply ... transition-none }` rule in index.css @layer components
  // outranks ordinary utility classes; only !important overrides it.
  // Do NOT remove the global rule — it's load-bearing for the brutalist
  // design language elsewhere.
  return (
    <button
      onClick={onClick}
      className={`group flex items-center gap-2 px-2 py-1 rounded border uppercase tracking-widest !transition-colors !duration-150 ${
        isActive
          ? `${accent.tint} ${accent.border}`
          : "bg-transparent border-transparent hover:border-border-minimal"
      }`}
      title={description}
    >
      {/* Toggle Switch */}
      <div
        className={`relative inline-flex h-3.5 w-7 items-center rounded-full border !transition-colors !duration-150 ${
          isActive
            ? `${accent.border} ${accent.tint}`
            : "border-border-minimal bg-bg-base"
        }`}
      >
        <span
          className={`inline-block h-2.5 w-2.5 transform rounded-full !transition-transform !duration-150 ${
            isActive
              ? `translate-x-3.5 ${accent.knob}`
              : "translate-x-0.5 bg-content-tertiary"
          }`}
        />
      </div>

      {/* Label */}
      <div className="flex items-center gap-1.5">
        <Icon
          className={`w-3 h-3 !transition-colors !duration-150 ${
            isActive ? accent.text : "text-content-tertiary"
          }`}
        />
        <span
          className={`text-[9px] font-bold !transition-colors !duration-150 ${
            isActive ? accent.text : "text-content-secondary"
          }`}
        >
          {label}
        </span>
      </div>
    </button>
  );
}
