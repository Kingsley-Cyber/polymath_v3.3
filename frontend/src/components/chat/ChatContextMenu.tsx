import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Boxes,
  Brain,
  Check,
  ChevronDown,
  Database,
  FolderGit2,
  Gauge,
  Layers3,
  Network,
  SlidersHorizontal,
  Zap,
} from "lucide-react";

import * as api from "../../lib/api";
import { useSettingsStore } from "../../stores/settingsStore";
import {
  QUERY_PROFILES,
  REASONING_MODES,
  type Collection,
  type CorpusResponse,
} from "../../types";
import type { RetrievalTier } from "../../types/chat";

interface ChatContextMenuProps {
  collections: Collection[];
  onOpenGraph: () => void;
}

const SEARCH_MODES: Array<{
  id: "auto" | "local" | "global";
  label: string;
  hint: string;
}> = [
  {
    id: "auto",
    label: "Auto",
    hint: "Infer local vs global from the query.",
  },
  {
    id: "local",
    label: "Local",
    hint: "Full retrieval with parent text for specific questions.",
  },
  {
    id: "global",
    label: "Global",
    hint: "Summary breadth for corpus-wide questions.",
  },
];

const RETRIEVAL_TIERS: Array<{
  id: RetrievalTier;
  label: string;
  short: string;
  description: string;
  icon: typeof Database;
}> = [
  {
    id: "qdrant_only",
    label: "Vector Base",
    short: "Vector",
    description: "Qdrant vectors only.",
    icon: Database,
  },
  {
    id: "qdrant_mongo",
    label: "Hybrid",
    short: "Hybrid",
    description: "Vector + lexical + Mongo parent hydration.",
    icon: Boxes,
  },
  {
    id: "qdrant_mongo_graph",
    label: "Graph Augmented",
    short: "Graph",
    description: "Hybrid retrieval with Neo4j fact and graph expansion.",
    icon: Network,
  },
];

function retrievalLabel(tier: string) {
  return RETRIEVAL_TIERS.find((item) => item.id === tier)?.short ?? "Hybrid";
}

function compactName(value: string, fallback = "Untitled") {
  const trimmed = value.trim();
  return trimmed || fallback;
}

function Section({
  icon,
  title,
  tone,
  children,
}: {
  icon: ReactNode;
  title: string;
  tone: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-white/10 bg-black/20 p-2.5">
      <div
        className={`mb-2 flex items-center gap-2 text-[9px] font-bold uppercase tracking-widest ${tone}`}
      >
        {icon}
        {title}
      </div>
      {children}
    </section>
  );
}

function InlineChoice({
  active,
  onClick,
  title,
  children,
}: {
  active: boolean;
  onClick: () => void;
  title?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={`flex min-h-9 w-full items-center gap-2 rounded-lg border px-2 py-1.5 text-left !transition-colors !duration-150 ${
        active
          ? "border-accent-main/60 bg-accent-main/12 text-content-primary"
          : "border-white/5 bg-[#0f1117] text-content-secondary hover:border-white/15 hover:bg-[#131720] hover:text-content-primary"
      }`}
    >
      <span
        className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
          active
            ? "border-accent-main bg-accent-main text-bg-base"
            : "border-white/15 bg-black/20"
        }`}
      >
        {active && <Check className="h-3 w-3" />}
      </span>
      <span className="min-w-0 flex-1">{children}</span>
    </button>
  );
}

export function ChatContextMenu({
  collections,
  onOpenGraph,
}: ChatContextMenuProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [corpora, setCorpora] = useState<CorpusResponse[]>([]);
  const [corporaLoading, setCorporaLoading] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const {
    selectedCorpusIds,
    selectedCollectionIds,
    reasoningMode,
    searchMode,
    queryProfile,
    retrievalTier,
    toggleCorpus,
    purgeStaleCorpusIds,
    updateSettings,
    toggleCollection,
    selectAllCollections,
    clearCollections,
    setQueryProfile,
    setSearchMode,
    setRetrievalTier,
    setReasoningMode,
  } = useSettingsStore();

  useEffect(() => {
    let cancelled = false;
    async function fetchCorpora() {
      try {
        setCorporaLoading(true);
        const data = await api.listCorpora();
        if (cancelled) return;
        setCorpora(data);
        purgeStaleCorpusIds(data.map((corpus) => corpus.corpus_id));
      } catch (err) {
        console.error("Failed to fetch corpora:", err);
      } finally {
        if (!cancelled) setCorporaLoading(false);
      }
    }
    void fetchCorpora();
    return () => {
      cancelled = true;
    };
  }, [purgeStaleCorpusIds]);

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

  const activeReasoning =
    REASONING_MODES.find((mode) => mode.key === reasoningMode) ??
    REASONING_MODES[0];

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
          className="pm-context-panel custom-scrollbar fixed left-2 right-2 top-20 z-[95] max-h-[calc(100dvh-5.5rem)] overflow-y-auto overflow-x-hidden overscroll-contain rounded-2xl border border-white/10 bg-[#15171d] p-2 font-mono shadow-2xl sm:absolute sm:left-auto sm:right-0 sm:top-full sm:mt-3 sm:w-[36rem] sm:max-w-[calc(100vw-1rem)] sm:max-h-[calc(100dvh-7rem)]"
          data-testid="chat-context-panel"
        >
          <div className="sticky top-0 z-10 mb-2 flex items-center justify-between gap-2 border-b border-white/10 bg-[#15171d] px-2 pb-2 pt-0.5">
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
            <Section
              icon={<FolderGit2 className="h-3 w-3" />}
              title="Sources"
              tone="text-cyan-200/80"
            >
              <div className="space-y-1.5" data-testid="context-corpus-list">
                <InlineChoice
                  active={selectedCorpusIds.length === 0}
                  onClick={() => updateSettings({ selectedCorpusIds: [] })}
                >
                  <span className="block text-[10px] font-bold uppercase tracking-widest">
                    All corpora
                  </span>
                  <span className="block text-[9px] text-content-tertiary">
                    Search every available corpus
                  </span>
                </InlineChoice>
                {corporaLoading ? (
                  <div className="px-2 py-3 text-[10px] uppercase tracking-widest text-content-tertiary">
                    Loading corpora...
                  </div>
                ) : corpora.length === 0 ? (
                  <div className="px-2 py-3 text-[10px] uppercase tracking-widest text-content-tertiary">
                    No corpora found
                  </div>
                ) : (
                  corpora.map((corpus) => {
                    const active = selectedCorpusIds.includes(corpus.corpus_id);
                    return (
                      <InlineChoice
                        key={corpus.corpus_id}
                        active={active}
                        onClick={() => toggleCorpus(corpus.corpus_id)}
                      >
                        <span className="block truncate text-[10px] font-bold uppercase tracking-widest">
                          {compactName(corpus.name, corpus.corpus_id)}
                        </span>
                        <span className="block text-[9px] text-content-tertiary">
                          {corpus.doc_count ?? 0} docs
                        </span>
                      </InlineChoice>
                    );
                  })
                )}
              </div>

              <div className="mt-3 border-t border-white/10 pt-2">
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <span className="text-[9px] font-bold uppercase tracking-widest text-content-tertiary">
                    Collections
                  </span>
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() =>
                        selectAllCollections(collections.map((c) => c.id))
                      }
                      className="rounded-full border border-white/10 px-2 py-0.5 text-[8px] font-bold uppercase tracking-widest text-content-secondary hover:text-content-primary"
                    >
                      All
                    </button>
                    <button
                      type="button"
                      onClick={clearCollections}
                      className="rounded-full border border-white/10 px-2 py-0.5 text-[8px] font-bold uppercase tracking-widest text-content-secondary hover:text-content-primary"
                    >
                      Clear
                    </button>
                  </div>
                </div>
                <div className="space-y-1" data-testid="context-collection-list">
                  {collections.length === 0 ? (
                    <div className="px-2 py-2 text-[9px] uppercase tracking-widest text-content-tertiary">
                      No collections
                    </div>
                  ) : (
                    collections.map((collection) => {
                      const active = selectedCollectionIds.includes(
                        collection.id,
                      );
                      return (
                        <InlineChoice
                          key={collection.id}
                          active={active}
                          onClick={() => toggleCollection(collection.id)}
                        >
                          <span className="block truncate text-[10px] font-bold uppercase tracking-widest">
                            {compactName(collection.name, collection.id)}
                          </span>
                        </InlineChoice>
                      );
                    })
                  )}
                </div>
              </div>
            </Section>

            <Section
              icon={<Gauge className="h-3 w-3" />}
              title="Pace"
              tone="text-emerald-200/80"
            >
              <div className="space-y-1.5" data-testid="context-query-speed">
                {QUERY_PROFILES.map((profile) => (
                  <InlineChoice
                    key={profile.key}
                    active={profile.key === queryProfile}
                    onClick={() => setQueryProfile(profile.key)}
                    title={profile.description}
                  >
                    <span className="flex min-w-0 items-center justify-between gap-2">
                      <span className="truncate text-[10px] font-bold uppercase tracking-widest">
                        {profile.label}
                      </span>
                      <span className="shrink-0 text-[8px] uppercase tracking-widest text-content-tertiary">
                        {profile.approxLatency}
                      </span>
                    </span>
                    <span className="block text-[9px] text-content-tertiary">
                      {profile.key === "custom"
                        ? "Saved retrieval settings"
                        : `k=${profile.retrieval_k} · rerank=${
                            profile.rerank_enabled ? "on" : "off"
                          }`}
                    </span>
                  </InlineChoice>
                ))}
              </div>

              <div className="mt-3 grid grid-cols-3 gap-1">
                {SEARCH_MODES.map((mode) => (
                  <button
                    key={mode.id}
                    type="button"
                    onClick={() => setSearchMode(mode.id)}
                    title={mode.hint}
                    className={`min-h-8 rounded-lg border px-2 text-[9px] font-bold uppercase tracking-widest !transition-colors !duration-150 ${
                      searchMode === mode.id
                        ? "border-emerald-400/40 bg-emerald-400/12 text-emerald-200"
                        : "border-white/5 bg-[#0f1117] text-content-tertiary hover:text-content-primary"
                    }`}
                  >
                    {mode.label}
                  </button>
                ))}
              </div>
            </Section>

            <Section
              icon={<Database className="h-3 w-3" />}
              title="Retrieval"
              tone="text-violet-200/80"
            >
              <div className="space-y-1.5" data-testid="context-retrieval-tier">
                {RETRIEVAL_TIERS.map((tier) => {
                  const Icon = tier.icon;
                  return (
                    <InlineChoice
                      key={tier.id}
                      active={tier.id === retrievalTier}
                      onClick={() => setRetrievalTier(tier.id)}
                      title={tier.description}
                    >
                      <span className="flex min-w-0 items-center gap-1.5">
                        <Icon className="h-3 w-3 shrink-0 text-violet-200/70" />
                        <span className="truncate text-[10px] font-bold uppercase tracking-widest">
                          {tier.label}
                        </span>
                      </span>
                      <span className="block text-[9px] text-content-tertiary">
                        {tier.description}
                      </span>
                    </InlineChoice>
                  );
                })}
              </div>
            </Section>

            <Section
              icon={<Brain className="h-3 w-3" />}
              title="Reasoning"
              tone="text-amber-200/80"
            >
              <div className="mb-2 rounded-lg border border-white/5 bg-[#0f1117] px-2 py-1.5">
                <span className="block text-[9px] uppercase tracking-widest text-content-tertiary">
                  Active
                </span>
                <span className="block truncate text-[10px] font-bold uppercase tracking-widest text-content-primary">
                  {activeReasoning?.label ?? "Off"}
                </span>
              </div>
              <div className="grid grid-cols-1 gap-1.5" data-testid="context-reasoning-list">
                {REASONING_MODES.map((mode) => (
                  <InlineChoice
                    key={mode.key}
                    active={mode.key === reasoningMode}
                    onClick={() => setReasoningMode(mode.key)}
                    title={mode.description}
                  >
                    <span className="flex min-w-0 items-center gap-1.5">
                      <span className="truncate text-[10px] font-bold uppercase tracking-widest">
                        {mode.label}
                      </span>
                      {mode.retrievalLevel && (
                        <Zap className="h-3 w-3 shrink-0 text-amber-300" />
                      )}
                    </span>
                    <span className="block text-[9px] text-content-tertiary">
                      {mode.description}
                    </span>
                  </InlineChoice>
                ))}
              </div>
            </Section>
          </div>
        </div>
      )}
    </div>
  );
}
