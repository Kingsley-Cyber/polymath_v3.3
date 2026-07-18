// ModelSelector.tsx — Sprint 4C + T4 redesign.
// One grouped list sourced from the unified query_model_pool. Selecting any
// entry writes `pool:<entry_id>` into settingsStore. Legacy selected values
// (`profile:<id>`, raw LiteLLM ids) keep rendering — the backend resolver
// handles them on the chat-send path.
// T4: cli-shim__* entries surface as their own "CLI Subscriptions" group
// ($0 flat lanes through the host CLI shim), search filters across every
// group, and the active model is pinned in a summary card.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Sparkles,
  ChevronDown,
  Cloud,
  CloudOff,
  Cpu,
  KeyRound,
  Search,
  Settings2,
  SquareTerminal,
  X,
} from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import { useQueryModelPoolStore } from "../../stores/queryModelPoolStore";
import type { PoolProvider, QueryModelPoolEntry } from "../../types";
import { POOL_PROVIDER_PRESETS, findPreset } from "../../types";

const STALE_MS = 30_000;
const CLI_GROUP = "cli";
const CLI_PREFIX = "cli-shim__";

/** Index map for registry-declaration-order tiebreak. Keeps Ollama first,
 * Custom last, and cloud providers clustered in the order the registry
 * declares them. Unknown providers sort after "custom" in alpha order. */
const REGISTRY_ORDER: Record<string, number> = Object.fromEntries(
  POOL_PROVIDER_PRESETS.map((p, i) => [p.id, i]),
);

/** Sort group keys: ollama first, CLI subscriptions second, cloud providers
 * in registry order, custom last, unknowns alpha after custom. */
function groupOrder(a: string, b: string): number {
  if (a === b) return 0;
  if (a === "ollama") return -1;
  if (b === "ollama") return 1;
  if (a === CLI_GROUP) return -1;
  if (b === CLI_GROUP) return 1;
  if (a === "custom") return 1;
  if (b === "custom") return -1;
  const ra = REGISTRY_ORDER[a];
  const rb = REGISTRY_ORDER[b];
  if (ra !== undefined && rb !== undefined) return ra - rb;
  if (ra !== undefined) return -1;
  if (rb !== undefined) return 1;
  return a.localeCompare(b);
}

function isCliEntry(entry: QueryModelPoolEntry): boolean {
  return entry.entry_id.startsWith(CLI_PREFIX);
}

function groupKeyFor(entry: QueryModelPoolEntry): string {
  return isCliEntry(entry) ? CLI_GROUP : entry.provider;
}

function groupDisplayName(key: string): string {
  if (key === CLI_GROUP) return "CLI Subscriptions";
  return findPreset(key as PoolProvider)?.name ?? key;
}

function groupIcon(key: string) {
  if (key === "ollama") {
    return <Cpu className="w-3 h-3 text-cyan-300" />;
  }
  if (key === CLI_GROUP) {
    return <SquareTerminal className="w-3 h-3 text-amber-300" />;
  }
  if (key === "custom") {
    return <CloudOff className="w-3 h-3 text-content-secondary" />;
  }
  return <Cloud className="w-3 h-3 text-emerald-400/80" />;
}

/** CLI lane (which agent binary) an entry rides, from model_name
 * "cursor-cli:gpt-5.3-codex" → "cursor-cli"; bare "cursor-cli" (account
 * default) maps to the same lane. */
const CLI_LANE_NAMES: Record<string, string> = {
  "chatgpt-cli": "ChatGPT (codex)",
  "cursor-cli": "Cursor",
  "antigravity-cli": "Antigravity (gemini)",
};
const CLI_LANE_ORDER = ["chatgpt-cli", "cursor-cli", "antigravity-cli"];

function cliLaneOf(entry: QueryModelPoolEntry): string {
  const lane = entry.model_name.split(":")[0];
  return CLI_LANE_NAMES[lane] ? lane : "other";
}

/** Family/variant split for CLI model labels: trailing effort/speed words
 * ("Low", "Extra High Fast", "Thinking", …) become the variant pill, the
 * rest is the family row. "Codex 5.3 Extra High Fast" → family "Codex 5.3",
 * variant "Extra High Fast". Account-default entries get their own family. */
const VARIANT_WORDS = new Set([
  "low",
  "medium",
  "high",
  "extra",
  "fast",
  "none",
  "thinking",
]);

function cliFamilySplit(entry: QueryModelPoolEntry): {
  family: string;
  variant: string;
} {
  if (!entry.model_name.includes(":")) {
    return { family: "Account default", variant: "Auto" };
  }
  const label = entry.label.includes(" — ")
    ? entry.label.slice(entry.label.indexOf(" — ") + 3)
    : entry.label;
  const clean = label.replace(/\s*\(.*?\)\s*/g, " ").trim();
  const words = clean.split(/\s+/);
  let i = words.length;
  while (i > 1 && VARIANT_WORDS.has(words[i - 1].toLowerCase())) i--;
  const family = words.slice(0, i).join(" ") || clean;
  const variant = words.slice(i).join(" ") || "Std";
  return { family, variant };
}

/** Order pills by effort then speed: None < Low < Std < High < XHigh;
 * Fast variants sort right after their base. */
function variantRank(variant: string): number {
  const v = variant.toLowerCase();
  let rank = 2;
  if (v.startsWith("none")) rank = 0;
  else if (v.startsWith("low")) rank = 1;
  else if (v.startsWith("high")) rank = 3;
  else if (v.startsWith("extra")) rank = 4;
  else if (v.startsWith("thinking")) rank = 5;
  return rank * 2 + (v.includes("fast") ? 1 : 0);
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

const OPEN_GROUPS_KEY = "polymath.modelSelector.openGroups";

function loadOpenGroups(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(OPEN_GROUPS_KEY) ?? "{}");
  } catch {
    return {};
  }
}

function saveOpenGroups(next: Record<string, boolean>) {
  try {
    localStorage.setItem(OPEN_GROUPS_KEY, JSON.stringify(next));
  } catch {
    /* persistence is best-effort */
  }
}

export function ModelSelector() {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState("");
  // Collapsed by default; persisted per group so the list stays the way the
  // owner left it. The active model's group is always forced open.
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>(
    loadOpenGroups,
  );
  const toggleGroup = (key: string) => {
    setOpenGroups((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      saveOpenGroups(next);
      return next;
    });
  };
  const dropdownRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
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
    setQuery("");
    setIsOpen(true);
  };

  // Focus search and reveal the active entry on open.
  useEffect(() => {
    if (!isOpen) return;
    requestAnimationFrame(() => {
      searchRef.current?.focus();
      dropdownRef.current
        ?.querySelector('[data-active="true"]')
        ?.scrollIntoView({ block: "nearest" });
    });
  }, [isOpen]);

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

  // Group enabled entries; CLI shim entries get their own group.
  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase();
    const enabled = config.query_model_pool.filter((e) => e.enabled);
    const byGroup = new Map<string, QueryModelPoolEntry[]>();
    for (const e of enabled) {
      const key = groupKeyFor(e);
      if (
        q &&
        ![e.label, e.model_name, e.provider, groupDisplayName(key)].some((s) =>
          s.toLowerCase().includes(q),
        )
      ) {
        continue;
      }
      const list = byGroup.get(key) ?? [];
      list.push(e);
      byGroup.set(key, list);
    }
    const keys = Array.from(byGroup.keys()).sort(groupOrder);
    return keys.map((key) => ({
      key,
      entries: (byGroup.get(key) ?? []).sort((a, b) =>
        a.model_name.localeCompare(b.model_name),
      ),
    }));
  }, [config.query_model_pool, query]);

  const searching = query.trim().length > 0;

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
    if (
      !searching &&
      selectedModel.startsWith("pool:") &&
      !activePoolEntry
    ) {
      setSelectedModel(`pool:${firstEnabledEntry.entry_id}`);
    }
  }, [
    activePoolEntry,
    firstEnabledEntry,
    searching,
    selectedModel,
    setSelectedModel,
  ]);

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
      if (isCliEntry(activePoolEntry)) return "bg-amber-300";
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

  const totalEnabled = config.query_model_pool.filter((e) => e.enabled).length;
  const totalShown = grouped.reduce((n, g) => n + g.entries.length, 0);
  const allOpen = grouped.every(
    (g) => openGroups[g.key] === true,
  );
  const setAllGroups = (open: boolean) => {
    const next: Record<string, boolean> = { ...openGroups };
    for (const g of grouped) next[g.key] = open;
    setOpenGroups(next);
    saveOpenGroups(next);
  };

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
          className="fixed left-2 right-2 bottom-36 z-[110] w-auto max-h-[calc(100dvh-11rem)] overflow-hidden border border-white/10 bg-[#232323] font-mono shadow-2xl rounded-md origin-bottom sm:absolute sm:left-0 sm:right-auto sm:bottom-full sm:mb-2 sm:w-[22rem] sm:max-w-[calc(100vw-1rem)] sm:max-h-[calc(100dvh-7rem)] sm:origin-bottom-left"
          data-testid="model-selector-dropdown"
        >
          {/* Header: title + counts + expand/collapse all */}
          <div className="flex items-center gap-2 px-3 py-2 border-b border-white/10">
            <span className="text-[9px] font-bold tracking-widest uppercase text-content-secondary">
              Select Model
            </span>
            <span className="rounded border border-white/10 px-1 text-[8px] text-content-tertiary">
              {searching ? `${totalShown}/${totalEnabled}` : totalEnabled}
            </span>
            <span className="flex-1" />
            <button
              onClick={() => setAllGroups(!allOpen)}
              className="text-[8px] font-bold uppercase tracking-widest text-content-tertiary hover:text-content-primary cursor-pointer"
              data-testid="model-selector-toggle-all"
            >
              {allOpen ? "Collapse all" : "Expand all"}
            </button>
          </div>

          {/* Search */}
          <div className="flex items-center gap-1.5 px-3 py-1.5 border-b border-white/10 bg-[#1d1d1d]">
            <Search className="w-3 h-3 shrink-0 text-content-tertiary" />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  if (query) setQuery("");
                  else setIsOpen(false);
                }
              }}
              placeholder="Search models or providers…"
              className="min-w-0 flex-1 bg-transparent text-[10px] tracking-wide text-content-primary placeholder:text-content-tertiary/70 focus:outline-none"
              data-testid="model-selector-search"
            />
            {query && (
              <button
                onClick={() => setQuery("")}
                className="shrink-0 text-content-tertiary hover:text-content-primary cursor-pointer"
                aria-label="Clear search"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>

          {/* Active model summary */}
          {activePoolEntry && (
            <div
              className="flex items-center gap-2 px-3 py-1.5 border-b border-white/10 bg-emerald-500/[0.07]"
              data-testid="model-selector-active-summary"
            >
              <div className="w-1.5 h-1.5 shrink-0 rounded-full bg-emerald-400 animate-pulse" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[9px] font-bold tracking-wider text-emerald-200">
                  {activePoolEntry.label}
                </div>
                <div className="truncate text-[8px] tracking-wide text-content-secondary">
                  {groupDisplayName(groupKeyFor(activePoolEntry))} ·{" "}
                  {activePoolEntry.model_name}
                </div>
              </div>
            </div>
          )}

          <div className="max-h-[calc(100dvh-19rem)] sm:max-h-[calc(100dvh-16rem)] overflow-y-auto custom-scrollbar flex flex-col gap-1 p-1 pr-1.5">
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
            ) : totalShown === 0 ? (
              <div
                className="px-2 py-4 text-center text-[10px] uppercase tracking-widest text-content-secondary"
                data-testid="model-selector-no-matches"
              >
                [ No models match “{query}” ]
              </div>
            ) : (
              grouped.map((group) => {
                const containsActive = group.entries.some(
                  (e) => `pool:${e.entry_id}` === selectedModel,
                );
                const isGroupOpen =
                  searching || openGroups[group.key] === true || containsActive;
                return (
                  <div
                    key={group.key}
                    className="rounded border border-white/[0.06] bg-[#1b1c21]"
                    data-testid={`model-group-${group.key}`}
                  >
                    <button
                      onClick={() => toggleGroup(group.key)}
                      className="flex w-full items-center gap-2 px-2 py-1.5 text-[9px] font-bold tracking-widest uppercase text-content-secondary hover:text-content-primary cursor-pointer"
                      data-testid={`model-group-toggle-${group.key}`}
                    >
                      {groupIcon(group.key)}
                      <span className="flex-1 truncate text-left">
                        {groupDisplayName(group.key)}
                      </span>
                      {containsActive && !isGroupOpen && (
                        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400" />
                      )}
                      {group.key === CLI_GROUP && (
                        <span className="shrink-0 rounded border border-amber-300/30 bg-amber-300/10 px-1 text-[7px] text-amber-200">
                          $0 FLAT
                        </span>
                      )}
                      <span className="shrink-0 rounded border border-white/10 px-1 text-[7px] text-content-tertiary">
                        {group.entries.length}
                      </span>
                      <ChevronDown
                        className={`w-3 h-3 shrink-0 transition-transform duration-100 ${
                          isGroupOpen ? "" : "-rotate-90"
                        }`}
                      />
                    </button>
                    {isGroupOpen && group.key !== CLI_GROUP && (
                      <div className="flex flex-col gap-1 p-1 pt-0">
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
                              data-active={isActive ? "true" : undefined}
                              className={`group flex w-full flex-col items-stretch gap-1 rounded border px-2 py-1.5 text-left transition-none ${
                                isActive
                                  ? "border-emerald-400 bg-emerald-500/15 text-emerald-200"
                                  : "border-white/5 bg-[#0b0c10] text-content-primary hover:border-white/20 hover:bg-bg-base"
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
                    )}
                    {isGroupOpen && group.key === CLI_GROUP && (
                      <div className="flex flex-col gap-1 p-1 pt-0">
                        {(() => {
                          const lanes = new Map<
                            string,
                            QueryModelPoolEntry[]
                          >();
                          for (const e of group.entries) {
                            const lane = cliLaneOf(e);
                            lanes.set(lane, [
                              ...(lanes.get(lane) ?? []),
                              e,
                            ]);
                          }
                          const laneKeys = Array.from(lanes.keys()).sort(
                            (a, b) =>
                              (CLI_LANE_ORDER.indexOf(a) + 99) -
                              (CLI_LANE_ORDER.indexOf(b) + 99),
                          );
                          return laneKeys.map((lane) => {
                            const laneEntries = lanes.get(lane) ?? [];
                            const laneKey = `cli:${lane}`;
                            const laneActive = laneEntries.some(
                              (e) => `pool:${e.entry_id}` === selectedModel,
                            );
                            const laneOpen =
                              searching ||
                              openGroups[laneKey] === true ||
                              laneActive;
                            // family → entries, families alphabetical
                            const families = new Map<
                              string,
                              { e: QueryModelPoolEntry; variant: string }[]
                            >();
                            for (const e of laneEntries) {
                              const { family, variant } = cliFamilySplit(e);
                              families.set(family, [
                                ...(families.get(family) ?? []),
                                { e, variant },
                              ]);
                            }
                            const familyKeys = Array.from(
                              families.keys(),
                            ).sort((a, b) => a.localeCompare(b));
                            return (
                              <div
                                key={laneKey}
                                className="rounded border border-white/[0.06] bg-[#17181d]"
                                data-testid={`cli-lane-${lane}`}
                              >
                                <button
                                  onClick={() => toggleGroup(laneKey)}
                                  className="flex w-full items-center gap-2 px-2 py-1.5 text-[9px] font-bold tracking-widest uppercase text-content-secondary hover:text-content-primary cursor-pointer"
                                  data-testid={`cli-lane-toggle-${lane}`}
                                >
                                  <SquareTerminal className="w-3 h-3 text-amber-300/80" />
                                  <span className="flex-1 truncate text-left">
                                    {CLI_LANE_NAMES[lane] ?? lane}
                                  </span>
                                  {laneActive && !laneOpen && (
                                    <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400" />
                                  )}
                                  <span className="shrink-0 rounded border border-white/10 px-1 text-[7px] text-content-tertiary">
                                    {laneEntries.length}
                                  </span>
                                  <ChevronDown
                                    className={`w-3 h-3 shrink-0 transition-transform duration-100 ${
                                      laneOpen ? "" : "-rotate-90"
                                    }`}
                                  />
                                </button>
                                {laneOpen && (
                                  <div className="flex flex-col gap-0.5 p-1 pt-0">
                                    {familyKeys.map((family) => {
                                      const variants = (
                                        families.get(family) ?? []
                                      ).sort(
                                        (a, b) =>
                                          variantRank(a.variant) -
                                          variantRank(b.variant),
                                      );
                                      const famKey = `cli:${lane}:${family}`;
                                      const famActive = variants.some(
                                        ({ e }) =>
                                          `pool:${e.entry_id}` ===
                                          selectedModel,
                                      );
                                      const famOpen =
                                        searching ||
                                        openGroups[famKey] === true ||
                                        famActive;
                                      const single = variants.length === 1;
                                      return (
                                        <div
                                          key={famKey}
                                          className="rounded border border-white/5 bg-[#0b0c10]"
                                        >
                                          <button
                                            onClick={() => {
                                              if (single) {
                                                setSelectedModel(
                                                  `pool:${variants[0].e.entry_id}`,
                                                );
                                                setIsOpen(false);
                                              } else {
                                                toggleGroup(famKey);
                                              }
                                            }}
                                            data-active={
                                              single && famActive
                                                ? "true"
                                                : undefined
                                            }
                                            className={`flex w-full items-center gap-1.5 px-2 py-1 text-left cursor-pointer ${
                                              single && famActive
                                                ? "text-emerald-200"
                                                : "text-content-primary hover:text-white"
                                            }`}
                                            data-testid={
                                              single
                                                ? `model-entry-${variants[0].e.entry_id}`
                                                : `cli-family-toggle-${lane}-${family}`
                                            }
                                            title={
                                              single
                                                ? variants[0].e.model_name
                                                : `${family} — ${variants.length} variants`
                                            }
                                          >
                                            <span className="min-w-0 flex-1 truncate text-[9.5px] font-bold tracking-wider">
                                              {family}
                                            </span>
                                            {famActive && (
                                              <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400" />
                                            )}
                                            {!single && (
                                              <>
                                                <span className="shrink-0 rounded border border-white/10 px-1 text-[7px] text-content-tertiary">
                                                  {variants.length}
                                                </span>
                                                <ChevronDown
                                                  className={`w-2.5 h-2.5 shrink-0 text-content-tertiary transition-transform duration-100 ${
                                                    famOpen
                                                      ? ""
                                                      : "-rotate-90"
                                                  }`}
                                                />
                                              </>
                                            )}
                                          </button>
                                          {!single && famOpen && (
                                            <div className="flex flex-wrap gap-1 px-2 pb-1.5">
                                              {variants.map(
                                                ({ e, variant }) => {
                                                  const pid = `pool:${e.entry_id}`;
                                                  const isActive =
                                                    selectedModel === pid;
                                                  return (
                                                    <button
                                                      key={pid}
                                                      onClick={() => {
                                                        setSelectedModel(
                                                          pid,
                                                        );
                                                        setIsOpen(false);
                                                      }}
                                                      data-active={
                                                        isActive
                                                          ? "true"
                                                          : undefined
                                                      }
                                                      className={`rounded border px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider cursor-pointer ${
                                                        isActive
                                                          ? "border-emerald-400 bg-emerald-500/15 text-emerald-200"
                                                          : "border-white/10 bg-[#16171d] text-content-secondary hover:border-white/25 hover:text-white"
                                                      }`}
                                                      data-testid={`model-entry-${e.entry_id}`}
                                                      title={e.model_name}
                                                    >
                                                      {variant}
                                                    </button>
                                                  );
                                                },
                                              )}
                                            </div>
                                          )}
                                        </div>
                                      );
                                    })}
                                  </div>
                                )}
                              </div>
                            );
                          });
                        })()}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>

          {/* Footer */}
          <button
            onClick={() => {
              setIsOpen(false);
              window.dispatchEvent(new CustomEvent("open-settings"));
            }}
            className="flex w-full items-center gap-1.5 px-3 py-1.5 border-t border-white/10 text-[8px] font-bold uppercase tracking-widest text-content-tertiary hover:text-content-primary cursor-pointer"
            data-testid="model-selector-manage"
          >
            <Settings2 className="w-3 h-3" />
            Manage models
          </button>
        </div>
      )}
    </div>
  );
}
