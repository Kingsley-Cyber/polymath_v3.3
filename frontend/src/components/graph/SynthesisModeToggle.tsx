/**
 * Phase 3.A1 — synthesis-mode toggle UI.
 *
 * Two-position pill switch between "Research" (concrete-claim synthesis)
 * and "Ideation" (build-advisor). Drives the `synthesis_mode` field on
 * GraphDiscoverRequest. Default: research.
 */

import type { GraphSynthesisMode } from "../../types/discover";

type Props = {
  value: GraphSynthesisMode;
  onChange: (mode: GraphSynthesisMode) => void;
  className?: string;
};

const OPTIONS: Array<{
  id: GraphSynthesisMode;
  label: string;
  hint: string;
}> = [
  {
    id: "research",
    label: "Research",
    hint: "Faithful synthesis of what the corpus says.",
  },
  {
    id: "ideation",
    label: "Ideation",
    hint: "Speculative build ideas grounded in corpus APIs.",
  },
];

export default function SynthesisModeToggle({
  value,
  onChange,
  className = "",
}: Props) {
  return (
    <div
      role="radiogroup"
      aria-label="Synthesis mode"
      className={
        "inline-flex rounded-full bg-slate-100 p-1 text-xs font-medium " +
        className
      }
    >
      {OPTIONS.map((opt) => {
        const active = opt.id === value;
        return (
          <button
            key={opt.id}
            type="button"
            role="radio"
            aria-checked={active}
            title={opt.hint}
            onClick={() => onChange(opt.id)}
            className={
              "px-3 py-1 rounded-full transition-colors " +
              (active
                ? "bg-white text-slate-900 shadow-sm"
                : "text-slate-500 hover:text-slate-700")
            }
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
