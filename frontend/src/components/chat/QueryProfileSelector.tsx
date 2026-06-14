// QueryProfileSelector.tsx (Phase 18)
// Per-query speed preset picker in the ToggleBar. Picks one of 3 profiles
// (fast | balanced | thorough) that bundles retrieval width + rerank.
// HyDE is controlled only by the visible HyDE toggle.
// Read at App.tsx.handleSend and injected into overrides.query_profile.

import { useState, useRef, useEffect } from "react";
import { Gauge, ChevronDown, Check } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import { QUERY_PROFILES } from "../../types";

export function QueryProfileSelector() {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const { queryProfile, setQueryProfile } = useSettingsStore();

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

  const active = QUERY_PROFILES.find((p) => p.key === queryProfile);
  const triggerLabel = `[SPEED: ${active?.label.toUpperCase() ?? "BALANCED"}]`;

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-1.5 px-2 py-1 text-[10px] font-bold tracking-widest uppercase border border-transparent hover:border-border-minimal transition-none rounded-none group"
        title={
          active
            ? `${active.label}: ${active.description} (${active.approxLatency})`
            : "Pick a speed profile"
        }
      >
        <Gauge className="w-3.5 h-3.5 text-content-tertiary group-hover:text-accent-main" />
        <span className="text-content-secondary">{triggerLabel}</span>
        <ChevronDown className="w-3 h-3 text-content-tertiary group-hover:text-accent-main" />
      </button>

      {isOpen && (
        <div className="fixed left-2 right-2 top-20 z-[90] w-auto max-h-[calc(100dvh-6rem)] overflow-hidden border border-white/10 bg-[#2a2a2a] p-1 shadow-xl rounded sm:absolute sm:left-auto sm:right-0 sm:top-full sm:mt-1 sm:w-80 sm:max-w-[calc(100vw-1rem)] sm:max-h-[calc(100dvh-7rem)]">
          <div className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary px-2 py-1.5 border-b border-border-minimal mb-1">
            Query Speed (for next send)
          </div>
          <div className="flex flex-col gap-0.5">
            {QUERY_PROFILES.map((profile) => {
              const selected = profile.key === queryProfile;
              return (
                <button
                  key={profile.key}
                  onClick={() => {
                    setQueryProfile(profile.key);
                    setIsOpen(false);
                  }}
                  title={profile.description}
                  className={`flex items-start gap-2 px-2 py-1.5 text-left border transition-none rounded-none text-content-secondary hover:text-content-primary ${
                    selected
                      ? "bg-accent-main/10 border-accent-main"
                      : "border-transparent hover:bg-bg-base"
                  }`}
                >
                  <div className="w-4 h-4 border border-border-minimal flex-shrink-0 flex items-center justify-center bg-bg-base mt-0.5">
                    {selected && <Check className="w-3 h-3 text-accent-main" />}
                  </div>
                  <div className="flex-1 overflow-hidden">
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-[10px] font-bold tracking-widest uppercase truncate">
                        {profile.label}
                      </div>
                      <div className="text-[9px] text-content-tertiary shrink-0">
                        {profile.approxLatency}
                      </div>
                    </div>
                    <div className="text-[9px] text-content-tertiary mt-0.5">
                      {profile.key === "custom"
                        ? "uses your Retrieval Settings"
                        : `k=${profile.retrieval_k} · lexical=${
                            profile.key === "fast" ? "off" : profile.key === "thorough" ? "deep" : "on"
                          } · rerank=${profile.rerank_enabled ? "on" : "off"}`}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
          <div className="text-[9px] text-content-tertiary px-2 py-1.5 border-t border-border-minimal mt-1 leading-snug">
            Settings → Retrieval controls the core gather and final-source
            shape for the selected tier.
          </div>
        </div>
      )}
    </div>
  );
}
