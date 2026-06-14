// ModelSelector.tsx — Sprint 4C.
// One flat grouped list sourced from the unified query_model_pool. No more
// Pool / Profiles / Discovered three-section split. Selecting any entry
// writes `pool:<entry_id>` into settingsStore. Legacy selected values
// (`profile:<id>`, raw LiteLLM ids) keep rendering — the backend resolver
// handles them on the chat-send path.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Sparkles,
  ChevronDown,
  Cloud,
  CloudOff,
  Cpu,
  KeyRound,
  Settings2,
} from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import { useQueryModelPoolStore } from "../../stores/queryModelPoolStore";
import type { PoolProvider, QueryModelPoolEntry } from "../../types";
import { POOL_PROVIDER_PRESETS, findPreset } from "../../types";

const STALE_MS = 30_000;

/** Index map for registry-declaration-order tiebreak. Keeps Ollama first,
 * Custom last, and cloud providers clustered in the order the registry
 * declares them. Unknown providers sort after "custom" in alpha order. */
const REGISTRY_ORDER: Record<string, number> = Object.fromEntries(
  POOL_PROVIDER_PRESETS.map((p, i) => [p.id, i]),
);

/** Sort providers using the registry's declaration order: ollama first,
 * cloud providers next (registry order), custom last, unknowns alpha
 * after custom. */
function providerOrder(a: string, b: string): number {
  if (a === b) return 0;
  if (a === "ollama") return -1;
  if (b === "ollama") return 1;
  if (a === "custom") return 1;
  if (b === "custom") return -1;
  const ra = REGISTRY_ORDER[a];
  const rb = REGISTRY_ORDER[b];
  if (ra !== undefined && rb !== undefined) return ra - rb;
  if (ra !== undefined) return -1;
  if (rb !== undefined) return 1;
  return a.localeCompare(b);
}

function providerIcon(provider: PoolProvider) {
  if (provider === "ollama") {
    return <Cpu className="w-2.5 h-2.5 text-cyan-300" />;
  }
  if (provider === "custom") {
    return <CloudOff className="w-2.5 h-2.5 text-content-secondary" />;
  }
  return <Cloud className="w-2.5 h-2.5 text-emerald-400/80" />;
}

function entryAccessIcon(entry: QueryModelPoolEntry) {
  if (entry.source === "ollama") {
    return <Cpu className="w-2.5 h-2.5 text-cyan-300" />;
  }
  if (entry.api_key_ciphertext) {
    return <KeyRound className="w-2.5 h-2.5 text-emerald-400" />;
  }
  return <KeyRound className="w-2.5 h-2.5 text-red-400" />;
}

export function ModelSelector() {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const lastLoadedAt = useRef<number>(0);

  const { selectedModel, setSelectedModel } = useSettingsStore();
  const { config, load } = useQueryModelPoolStore();

  // Load on mount; re-load when dropdown opens if the cached config is stale.
  useEffect(() => {
    void load();
    lastLoadedAt.current = Date.now();
  }, [load]);

  const openDropdown = () => {
    const now = Date.now();
    if (now - lastLoadedAt.current > STALE_MS) {
      void load();
      lastLoadedAt.current = now;
    }
    setIsOpen(true);
  };

  // Click-outside dismisses.
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

  // Group enabled entries by provider, ollama first then alphabetical.
  const grouped = useMemo(() => {
    const enabled = config.query_model_pool.filter((e) => e.enabled);
    const byProvider = new Map<PoolProvider, QueryModelPoolEntry[]>();
    for (const e of enabled) {
      const list = byProvider.get(e.provider) ?? [];
      list.push(e);
      byProvider.set(e.provider, list);
    }
    const providers = Array.from(byProvider.keys()).sort(providerOrder);
    return providers.map((p) => ({
      provider: p,
      entries: (byProvider.get(p) ?? []).sort((a, b) =>
        a.model_name.localeCompare(b.model_name),
      ),
    }));
  }, [config.query_model_pool]);

  // Active-pill label. Four cases, all graceful:
  //   1. pool:<id> and id resolves → show pool entry label
  //   2. pool:<id> with missing id → show raw "pool:<id>" (entry was deleted)
  //   3. profile:<id>               → show raw "profile:<id>" (legacy)
  //   4. raw LiteLLM id             → show model.split('/').pop()
  const activePoolEntry =
    selectedModel?.startsWith("pool:")
      ? config.query_model_pool.find(
          (p) => `pool:${p.entry_id}` === selectedModel,
        )
      : undefined;
  const firstEnabledEntry = grouped[0]?.entries[0];

  useEffect(() => {
    if (!firstEnabledEntry) return;
    if (!selectedModel) {
      setSelectedModel(`pool:${firstEnabledEntry.entry_id}`);
      return;
    }
    if (selectedModel.startsWith("pool:") && !activePoolEntry) {
      setSelectedModel(`pool:${firstEnabledEntry.entry_id}`);
    }
  }, [activePoolEntry, firstEnabledEntry, selectedModel, setSelectedModel]);

  const displayLabel = activePoolEntry
    ? activePoolEntry.label
    : selectedModel
      ? selectedModel.startsWith("pool:") || selectedModel.startsWith("profile:")
        ? "[NO_MODEL]"
        : (selectedModel.split("/").pop() ?? selectedModel)
      : "[NO_MODEL]";

  const getModelColor = () => {
    if (!selectedModel) return "bg-content-tertiary";
    if (activePoolEntry) {
      if (activePoolEntry.provider === "ollama") return "bg-cyan-400";
      if (activePoolEntry.provider === "anthropic") return "bg-accent-main";
      if (activePoolEntry.provider === "openai") return "bg-success";
      return "bg-emerald-400";
    }
    if (selectedModel.startsWith("pool:")) return "bg-emerald-400";
    if (selectedModel.startsWith("profile:")) return "bg-purple-500";
    if (selectedModel.includes("ollama") || selectedModel.includes("local"))
      return "bg-accent-secondary";
    if (selectedModel.includes("anthropic") || selectedModel.includes("claude"))
      return "bg-accent-main";
    if (selectedModel.includes("openai") || selectedModel.includes("gpt"))
      return "bg-success";
    return "bg-content-primary";
  };

  const totalEnabled = grouped.reduce((n, g) => n + g.entries.length, 0);

  return (
    <div className="relative flex items-center gap-2" ref={dropdownRef}>
      <Sparkles className="w-3.5 h-3.5 text-content-secondary" />
      <button
        onClick={() => (isOpen ? setIsOpen(false) : openDropdown())}
        className="flex items-center gap-1.5 px-2 py-1 border border-border-minimal hover:border-accent-main bg-bg-surface transition-none rounded-none cursor-pointer group"
        title="Select chat model"
        data-testid="model-selector-toggle"
      >
        <div
          className={`w-1.5 h-1.5 rounded-none ${getModelColor()} animate-pulse`}
        />
        <span className="text-[10px] font-bold tracking-widest text-content-primary uppercase truncate max-w-[150px]">
          {displayLabel}
        </span>
        <ChevronDown
          className={`w-3 h-3 text-content-secondary group-hover:text-accent-main transition-transform duration-150 ${
            isOpen ? "rotate-180" : ""
          }`}
        />
      </button>

      {isOpen && (
        <div
          className="fixed left-2 right-2 bottom-36 z-[110] w-auto max-h-[calc(100dvh-11rem)] overflow-hidden border border-white/10 bg-[#2a2a2a] p-1 font-mono shadow-xl rounded origin-bottom sm:absolute sm:left-auto sm:right-0 sm:bottom-full sm:mb-2 sm:w-80 sm:max-w-[calc(100vw-1rem)] sm:max-h-[calc(100dvh-7rem)] sm:origin-bottom-right"
          data-testid="model-selector-dropdown"
        >
          <div className="text-[9px] font-bold tracking-widest uppercase text-content-secondary px-2 py-1.5 border-b border-border-minimal mb-1">
            Select Engine
          </div>

          <div className="max-h-[calc(100dvh-14rem)] sm:max-h-[calc(100dvh-11rem)] overflow-y-auto custom-scrollbar flex flex-col gap-1 pr-0.5">
            {totalEnabled === 0 ? (
              <div
                className="flex flex-col items-center gap-2 px-2 py-4 text-center"
                data-testid="model-selector-empty"
              >
                <div className="text-[10px] uppercase tracking-widest text-content-secondary">
                  [ No models configured ]
                </div>
                <button
                  onClick={() => {
                    setIsOpen(false);
                    window.dispatchEvent(new CustomEvent("open-settings"));
                  }}
                  className="flex items-center gap-1.5 px-2 py-1 text-[9px] font-bold tracking-widest uppercase border border-accent-main/60 bg-accent-main/10 text-accent-main hover:bg-accent-main/20"
                >
                  <Settings2 className="w-3 h-3" />
                  Open Models Settings
                </button>
                <div className="text-[8px] text-content-tertiary/80 tracking-wider max-w-[200px] leading-relaxed">
                  Settings → Models tab. Add a cloud entry or import
                  installed Ollama models.
                </div>
              </div>
            ) : (
              grouped.map((group) => (
                <div
                  key={group.provider}
                  data-testid={`model-group-${group.provider}`}
                >
                  <div
                    className="flex items-center gap-1 px-2 pt-1.5 pb-0.5 text-[8px] font-bold tracking-widest uppercase text-content-secondary"
                  >
                    {providerIcon(group.provider)}
                    {findPreset(group.provider)?.name ?? group.provider}
                  </div>
                  {group.entries.map((e) => {
                    const pid = `pool:${e.entry_id}`;
                    const isActive = selectedModel === pid;
                    return (
                      <button
                        key={pid}
                        onClick={() => {
                          setSelectedModel(pid);
                          setIsOpen(false);
                        }}
                        className={`group flex w-full flex-col items-stretch gap-1 rounded border px-2 py-2 text-left transition-none ${
                          isActive
                            ? "border-emerald-400 bg-emerald-500/15 text-emerald-200"
                            : "border-white/5 bg-[#0b0c10] text-content-primary hover:border-white/15 hover:bg-bg-base"
                        }`}
                        data-testid={`model-entry-${e.entry_id}`}
                        title={`${e.provider} · ${e.model_name}`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="min-w-0 flex-1 truncate text-[10px] font-bold tracking-wider text-white">
                            {e.label}
                          </span>
                          {isActive && (
                            <span className="shrink-0 rounded border border-emerald-400/40 bg-emerald-400/10 px-1.5 py-0.5 text-[7px] font-bold uppercase tracking-widest text-emerald-300">
                              Active
                            </span>
                          )}
                        </div>
                        <div className="flex min-w-0 items-center gap-1.5">
                          <span className="shrink-0 rounded border border-white/10 bg-[#16171d] px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-widest text-content-secondary">
                            {e.provider}
                          </span>
                          <span className="min-w-0 flex-1 truncate text-[9px] tracking-wide text-content-secondary normal-case">
                            {e.model_name}
                          </span>
                          <span className="shrink-0 opacity-80 group-hover:opacity-100">
                            {entryAccessIcon(e)}
                          </span>
                        </div>
                      </button>
                    );
                  })}
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
