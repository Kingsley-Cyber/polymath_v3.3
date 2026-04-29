// RetrievalTierSelector.tsx - UI for selecting the retrieval strategy tier
import { useState, useRef, useEffect } from "react";
import { ChevronDown, Check, Database, Boxes, Network } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import type { RetrievalTier } from "../../types/chat";

const TIERS: { id: RetrievalTier; label: string; icon: any; description: string; disabledMsg?: string }[] = [
  {
    id: "qdrant_only",
    label: "Vector Base (Degraded)",
    icon: Database,
    description: "Raw vector similarity only. No MongoDB hydration or entity extraction.",
  },
  {
    id: "qdrant_mongo",
    label: "Hybrid (Default)",
    icon: Boxes,
    description: "Vector + lexical recall with full parent document hydration from MongoDB.",
  },
  {
    id: "qdrant_mongo_graph",
    label: "Graph Augmented",
    icon: Network,
    description: "Vector + lexical + Mongo hydration + Neo4j graph traversal. Requires graph-ingested corpora.",
  },
];

export function RetrievalTierSelector() {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const { retrievalTier, setRetrievalTier } = useSettingsStore();

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const selectedTier = TIERS.find((t) => t.id === retrievalTier) || TIERS[1];
  const Icon = selectedTier.icon;

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-1.5 px-2 py-1 text-[10px] font-bold tracking-widest uppercase border border-transparent hover:border-border-minimal transition-none rounded-none group"
        title="Select Retrieval Tier"
      >
        <Icon className="w-3.5 h-3.5 text-content-tertiary group-hover:text-accent-main" />
        <span className="text-content-secondary hidden sm:inline-block">
          [{selectedTier.label.replace(/ \(.+\)/, "")}]
        </span>
        <ChevronDown className="w-3 h-3 text-content-tertiary group-hover:text-accent-main" />
      </button>

      {isOpen && (
        <div className="absolute top-full right-0 mt-1 w-64 border border-white/10 bg-[#2a2a2a] z-[60] p-1 shadow-xl rounded">
          <div className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary px-2 py-1.5 border-b border-border-minimal mb-1">
            Retrieval Tier
          </div>
          <div className="flex flex-col gap-0.5">
            {TIERS.map((tier) => {
              const TierIcon = tier.icon;
              const isSelected = retrievalTier === tier.id;

              // Backend filters stale/unavailable corpora and downgrades the
              // tier if a graph-only corpus isn't selected. No need for a
              // client-side gate — keep all tiers selectable.
              const isDisabled = false;

              return (
                <button
                  key={tier.id}
                  onClick={() => {
                    if (!isDisabled) {
                      setRetrievalTier(tier.id);
                      setIsOpen(false);
                    }
                  }}
                  disabled={isDisabled}
                  className={`flex items-start gap-2 px-2 py-2 text-left border transition-none rounded-none ${
                    isSelected
                      ? "bg-accent-main/10 border-accent-main text-content-primary"
                      : isDisabled
                      ? "opacity-50 cursor-not-allowed border-transparent text-content-tertiary"
                      : "border-transparent hover:bg-bg-base text-content-secondary hover:text-content-primary"
                  }`}
                >
                  <div className="w-4 h-4 border border-border-minimal flex-shrink-0 flex items-center justify-center bg-bg-base mt-0.5">
                    {isSelected && <Check className="w-3 h-3 text-accent-main" />}
                  </div>
                  <div className="flex-1 overflow-hidden">
                    <div className="text-[10px] font-bold tracking-widest uppercase flex items-center gap-1.5">
                      <TierIcon className="w-3 h-3" />
                      {tier.label}
                    </div>
                    <div className="text-[9px] text-content-tertiary mt-0.5 leading-tight">
                      {isDisabled ? "Disabled: Enable Graph routing first." : tier.description}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
