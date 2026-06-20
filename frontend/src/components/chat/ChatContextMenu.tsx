import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Boxes,
  Brain,
  Check,
  ChevronDown,
  Database,
  FolderGit2,
  Gauge,
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
}

type OpenPanel = "context" | "sources" | "reasoning" | null;

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
    label: "Fast Search",
    short: "Fast",
    description: "Qdrant vectors only.",
    icon: Database,
  },
  {
    id: "qdrant_mongo",
    label: "Hybrid Search",
    short: "Hybrid",
    description: "Vector + lexical + Mongo parent hydration.",
    icon: Boxes,
  },
  {
    id: "qdrant_mongo_graph",
    label: "Graph Augmentation",
    short: "Graph",
    description: "Highest quality: Hybrid Search plus Neo4j facts, graph expansion, and final rerank.",
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
    <section className="flex min-h-0 flex-col rounded-xl border border-white/10 bg-black/20 p-2.5">
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

function ScrollPane({
  children,
  testId,
  className = "max-h-44 sm:max-h-64",
}: {
  children: ReactNode;
  testId?: string;
  className?: string;
}) {
  return (
    <div
      data-testid={testId}
      className={`custom-scrollbar min-h-0 space-y-1.5 overflow-y-auto overflow-x-hidden pr-1 ${className}`}
    >
      {children}
    </div>
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
}: ChatContextMenuProps) {
  const [openPanel, setOpenPanel] = useState<OpenPanel>(null);
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
        setOpenPanel(null);
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

  const contextCount =
    2 + (queryProfile !== "balanced" ? 1 : 0) + (searchMode !== "auto" ? 1 : 0);
  const sourceCount =
    (selectedCorpusIds.length === 0 ? 1 : selectedCorpusIds.length) +
    selectedCollectionIds.length;
  const reasoningCount = reasoningMode && reasoningMode !== "none" ? 1 : 0;

  const togglePanel = (panel: Exclude<OpenPanel, null>) => {
    setOpenPanel((current) => (current === panel ? null : panel));
  };

  return (
    <div className="relative min-w-0" ref={menuRef}>
      <div className="flex min-w-0 items-center gap-1.5">
        <button
          type="button"
          data-testid="chat-sources-toggle"
          onClick={() => togglePanel("sources")}
          className="pm-soft-control group flex h-9 max-w-full items-center gap-2 rounded-full border border-border-minimal bg-bg-surface/95 px-2.5 text-[10px] font-bold uppercase tracking-widest text-content-secondary hover:text-content-primary"
          aria-expanded={openPanel === "sources"}
          aria-label="Open source controls"
          title="Sources"
        >
          <FolderGit2 className="h-3.5 w-3.5 text-cyan-200" />
          <span className="hidden md:inline">Sources</span>
          <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-cyan-400/12 px-1.5 text-[9px] text-cyan-200">
            {sourceCount}
          </span>
        </button>

        <button
          type="button"
          data-testid="chat-context-toggle"
          onClick={() => togglePanel("context")}
          className="pm-soft-control group flex h-9 max-w-full items-center gap-2 rounded-full border border-border-minimal bg-bg-surface/95 px-2.5 text-[10px] font-bold uppercase tracking-widest text-content-secondary hover:text-content-primary"
          aria-expanded={openPanel === "context"}
          aria-label="Open pace and retrieval controls"
          title="Pace and retrieval"
        >
          <SlidersHorizontal className="h-3.5 w-3.5 text-accent-main" />
          <span className="hidden sm:inline">Context</span>
          <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-accent-main/15 px-1.5 text-[9px] text-accent-main">
            {contextCount}
          </span>
        </button>

        <button
          type="button"
          data-testid="chat-reasoning-toggle"
          onClick={() => togglePanel("reasoning")}
          className="pm-soft-control group flex h-9 max-w-full items-center gap-2 rounded-full border border-border-minimal bg-bg-surface/95 px-2.5 text-[10px] font-bold uppercase tracking-widest text-content-secondary hover:text-content-primary"
          aria-expanded={openPanel === "reasoning"}
          aria-label="Open reasoning controls"
          title="Reasoning"
        >
          <Brain className="h-3.5 w-3.5 text-amber-200" />
          <span className="hidden lg:inline">Reason</span>
          <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-amber-400/12 px-1.5 text-[9px] text-amber-200">
            {reasoningCount}
          </span>
        </button>
      </div>

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

      {openPanel === "sources" && (
        <div
          className="pm-context-panel fixed left-2 right-2 top-16 z-[95] max-h-[calc(100dvh-5rem)] overflow-hidden overscroll-contain rounded-2xl border border-white/10 bg-[#15171d] p-2 font-mono shadow-2xl sm:absolute sm:left-auto sm:right-0 sm:top-full sm:mt-3 sm:w-[32rem] sm:max-w-[calc(100vw-1rem)]"
          data-testid="chat-sources-panel"
        >
          <div className="mb-2 flex items-center justify-between gap-2 border-b border-white/10 px-2 pb-2 pt-0.5">
            <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.22em] text-content-primary">
              <FolderGit2 className="h-3.5 w-3.5 text-cyan-200" />
              Sources
            </div>
            <button
              type="button"
              onClick={() => setOpenPanel(null)}
              className="rounded-full border border-white/10 px-2 py-0.5 text-[8px] font-bold uppercase tracking-widest text-content-secondary hover:text-content-primary"
            >
              Close
            </button>
          </div>

          <div className="grid min-h-0 grid-cols-1 gap-2 sm:grid-cols-2">
            <Section
              icon={<FolderGit2 className="h-3 w-3" />}
              title="Corpora"
              tone="text-cyan-200/80"
            >
              <ScrollPane testId="context-corpus-list">
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
                        <span className="block break-words text-[10px] font-bold uppercase tracking-widest">
                          {compactName(corpus.name, corpus.corpus_id)}
                        </span>
                        <span className="block text-[9px] text-content-tertiary">
                          {corpus.doc_count ?? 0} docs
                        </span>
                      </InlineChoice>
                    );
                  })
                )}
              </ScrollPane>
            </Section>

            <Section
              icon={<Boxes className="h-3 w-3" />}
              title="Collections"
              tone="text-cyan-100/70"
            >
              <div className="mb-2 flex items-center justify-end gap-1">
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
              <ScrollPane testId="context-collection-list">
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
                        <span className="block break-words text-[10px] font-bold uppercase tracking-widest">
                          {compactName(collection.name, collection.id)}
                        </span>
                      </InlineChoice>
                    );
                  })
                )}
              </ScrollPane>
            </Section>
          </div>
        </div>
      )}

      {openPanel === "context" && (
        <div
          className="pm-context-panel fixed left-2 right-2 top-16 z-[95] max-h-[calc(100dvh-5rem)] overflow-hidden overscroll-contain rounded-2xl border border-white/10 bg-[#15171d] p-2 font-mono shadow-2xl sm:absolute sm:left-auto sm:right-0 sm:top-full sm:mt-3 sm:w-[30rem] sm:max-w-[calc(100vw-1rem)]"
          data-testid="chat-context-panel"
        >
          <div className="mb-2 flex items-center justify-between gap-2 border-b border-white/10 px-2 pb-2 pt-0.5">
            <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.22em] text-content-primary">
              <SlidersHorizontal className="h-3.5 w-3.5 text-accent-main" />
              Query Context
            </div>
            <ChevronDown className="h-3 w-3 rotate-180 text-content-tertiary" />
          </div>

          <div className="grid min-h-0 grid-cols-1 gap-2 sm:grid-cols-2">
            <Section
              icon={<Gauge className="h-3 w-3" />}
              title="Pace"
              tone="text-emerald-200/80"
            >
              <ScrollPane
                testId="context-query-speed"
                className="max-h-52 sm:max-h-72"
              >
                {QUERY_PROFILES.map((profile) => (
                  <InlineChoice
                    key={profile.key}
                    active={profile.key === queryProfile}
                    onClick={() => setQueryProfile(profile.key)}
                    title={profile.description}
                  >
                    <span className="flex min-w-0 items-center justify-between gap-2">
                      <span className="break-words text-[10px] font-bold uppercase tracking-widest">
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
              </ScrollPane>

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
              <ScrollPane
                testId="context-retrieval-tier"
                className="max-h-52 sm:max-h-72"
              >
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
                        <span className="break-words text-[10px] font-bold uppercase tracking-widest">
                          {tier.label}
                        </span>
                      </span>
                      <span className="block text-[9px] text-content-tertiary">
                        {tier.description}
                      </span>
                    </InlineChoice>
                  );
                })}
              </ScrollPane>
            </Section>
          </div>
        </div>
      )}

      {openPanel === "reasoning" && (
        <div
          className="pm-context-panel fixed left-2 right-2 top-16 z-[95] max-h-[calc(100dvh-5rem)] overflow-hidden overscroll-contain rounded-2xl border border-white/10 bg-[#15171d] p-2 font-mono shadow-2xl sm:absolute sm:left-auto sm:right-0 sm:top-full sm:mt-3 sm:w-[28rem] sm:max-w-[calc(100vw-1rem)]"
          data-testid="chat-reasoning-panel"
        >
          <div className="mb-2 flex items-center justify-between gap-2 border-b border-white/10 px-2 pb-2 pt-0.5">
            <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.22em] text-content-primary">
              <Brain className="h-3.5 w-3.5 text-amber-200" />
              Reasoning
            </div>
            <span className="rounded-full border border-amber-400/20 bg-amber-400/10 px-2 py-0.5 text-[8px] font-bold uppercase tracking-widest text-amber-200">
              {activeReasoning?.label ?? "Off"}
            </span>
          </div>

          <Section
            icon={<Brain className="h-3 w-3" />}
            title="Modes"
            tone="text-amber-200/80"
          >
            <ScrollPane
              testId="context-reasoning-list"
              className="max-h-[calc(100dvh-12rem)] sm:max-h-56 lg:max-h-80"
            >
              {REASONING_MODES.map((mode) => (
                <InlineChoice
                  key={mode.key}
                  active={mode.key === reasoningMode}
                  onClick={() => setReasoningMode(mode.key)}
                  title={mode.description}
                >
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="break-words text-[10px] font-bold uppercase tracking-widest">
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
            </ScrollPane>
          </Section>
        </div>
      )}
    </div>
  );
}
