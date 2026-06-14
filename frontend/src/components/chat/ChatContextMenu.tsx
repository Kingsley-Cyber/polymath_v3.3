import { useEffect, useMemo, useRef, useState } from "react";
import {
  Brain,
  ChevronDown,
  Database,
  FolderGit2,
  Gauge,
  Layers3,
  Network,
  SlidersHorizontal,
  Telescope,
} from "lucide-react";

import { CollectionSelector } from "./CollectionSelector";
import { CorpusMultiSelect } from "./CorpusMultiSelect";
import { QueryProfileSelector } from "./QueryProfileSelector";
import { ReasoningModeSelector } from "./ReasoningModeSelector";
import { RetrievalTierSelector } from "./RetrievalTierSelector";
import { SearchModeSelector } from "./SearchModeSelector";
import { useSettingsStore } from "../../stores/settingsStore";
import type { Collection } from "../../types";

interface ChatContextMenuProps {
  collections: Collection[];
  onOpenGraph: () => void;
}

function retrievalLabel(tier: string) {
  if (tier === "qdrant_only") return "Vector";
  if (tier === "qdrant_mongo_graph") return "Graph";
  return "Hybrid";
}

export function ChatContextMenu({
  collections,
  onOpenGraph,
}: ChatContextMenuProps) {
  const [isOpen, setIsOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const {
    selectedCorpusIds,
    selectedCollectionIds,
    reasoningMode,
    searchMode,
    queryProfile,
    retrievalTier,
  } = useSettingsStore();

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (
        menuRef.current &&
        !menuRef.current.contains(event.target as Node)
      ) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const chips = useMemo(() => {
    const next: Array<{ label: string; tone: "source" | "mode" | "graph" }> = [
      {
        label:
          selectedCorpusIds.length === 0
            ? "All corpora"
            : `${selectedCorpusIds.length} corpora`,
        tone: "source",
      },
      {
        label: retrievalLabel(retrievalTier),
        tone: retrievalTier === "qdrant_mongo_graph" ? "graph" : "mode",
      },
      { label: searchMode, tone: "mode" },
    ];
    if (selectedCollectionIds.length > 0) {
      next.push({
        label: `${selectedCollectionIds.length} collections`,
        tone: "source",
      });
    }
    if (queryProfile !== "balanced") {
      next.push({ label: queryProfile, tone: "mode" });
    }
    if (reasoningMode && reasoningMode !== "none") {
      next.push({ label: "reason", tone: "graph" });
    }
    return next;
  }, [
    queryProfile,
    reasoningMode,
    retrievalTier,
    searchMode,
    selectedCollectionIds.length,
    selectedCorpusIds.length,
  ]);

  return (
    <div className="relative min-w-0" ref={menuRef}>
      <button
        type="button"
        data-testid="chat-context-toggle"
        onClick={() => setIsOpen((open) => !open)}
        className="pm-soft-control group flex h-9 max-w-full items-center gap-2 rounded-full border border-border-minimal bg-bg-surface/95 px-2.5 text-[10px] font-bold uppercase tracking-widest text-content-secondary hover:text-content-primary"
        aria-expanded={isOpen}
        aria-label="Open chat context controls"
        title="Chat context"
      >
        <SlidersHorizontal className="h-3.5 w-3.5 text-accent-main" />
        <span className="hidden sm:inline">Context</span>
        <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-accent-main/15 px-1.5 text-[9px] text-accent-main">
          {chips.length}
        </span>
        <ChevronDown
          className={`h-3 w-3 text-content-tertiary !transition-transform !duration-150 ${
            isOpen ? "rotate-180" : ""
          }`}
        />
      </button>

      <div className="mt-2 hidden min-w-0 items-center gap-1.5 lg:flex">
        {chips.slice(0, 4).map((chip) => (
          <span
            key={`${chip.tone}-${chip.label}`}
            className={`rounded-full border px-2 py-0.5 text-[9px] font-bold uppercase tracking-widest ${
              chip.tone === "source"
                ? "border-cyan-400/20 bg-cyan-400/10 text-cyan-200"
                : chip.tone === "graph"
                  ? "border-violet-400/25 bg-violet-400/10 text-violet-200"
                  : "border-emerald-400/20 bg-emerald-400/10 text-emerald-200"
            }`}
          >
            {chip.label}
          </span>
        ))}
      </div>

      {isOpen && (
        <div
          className="pm-context-panel fixed left-2 right-2 top-20 z-[95] max-h-[calc(100dvh-5.5rem)] overflow-y-auto overflow-x-visible rounded-2xl border border-white/10 bg-[#15171d] p-2 font-mono shadow-2xl sm:absolute sm:left-auto sm:right-0 sm:top-full sm:mt-3 sm:w-[34rem] sm:max-w-[calc(100vw-1rem)]"
          data-testid="chat-context-panel"
        >
          <div className="mb-2 flex items-center justify-between gap-2 border-b border-white/10 px-2 pb-2">
            <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.22em] text-content-primary">
              <Layers3 className="h-3.5 w-3.5 text-accent-main" />
              Query Context
            </div>
            <button
              type="button"
              onClick={onOpenGraph}
              className="pm-soft-control flex h-8 items-center gap-2 rounded-full border border-violet-400/25 bg-violet-400/10 px-3 text-[9px] font-bold uppercase tracking-widest text-violet-200 hover:bg-violet-400/15"
            >
              <Network className="h-3.5 w-3.5" />
              Graph
            </button>
          </div>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <section className="rounded-xl border border-white/10 bg-black/15 p-2">
              <div className="mb-2 flex items-center gap-2 text-[9px] font-bold uppercase tracking-widest text-cyan-200/80">
                <FolderGit2 className="h-3 w-3" />
                Sources
              </div>
              <div className="flex flex-col items-start gap-1.5">
                <CorpusMultiSelect />
                <CollectionSelector collections={collections} />
              </div>
            </section>

            <section className="rounded-xl border border-white/10 bg-black/15 p-2">
              <div className="mb-2 flex items-center gap-2 text-[9px] font-bold uppercase tracking-widest text-emerald-200/80">
                <Gauge className="h-3 w-3" />
                Pace
              </div>
              <div className="flex flex-col items-start gap-1.5">
                <QueryProfileSelector />
                <SearchModeSelector />
              </div>
            </section>

            <section className="rounded-xl border border-white/10 bg-black/15 p-2">
              <div className="mb-2 flex items-center gap-2 text-[9px] font-bold uppercase tracking-widest text-violet-200/80">
                <Database className="h-3 w-3" />
                Retrieval
              </div>
              <div className="flex flex-col items-start gap-1.5">
                <RetrievalTierSelector />
              </div>
            </section>

            <section className="rounded-xl border border-white/10 bg-black/15 p-2">
              <div className="mb-2 flex items-center gap-2 text-[9px] font-bold uppercase tracking-widest text-amber-200/80">
                <Brain className="h-3 w-3" />
                Reasoning
              </div>
              <div className="flex flex-col items-start gap-1.5">
                <ReasoningModeSelector />
                <Telescope className="h-3.5 w-3.5 text-content-tertiary" />
              </div>
            </section>
          </div>
        </div>
      )}
    </div>
  );
}
