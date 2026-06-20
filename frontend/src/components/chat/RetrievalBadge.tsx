// RetrievalBadge.tsx — assistant-message trust signal.
//
// Three states (strict-zero detection per spec):
//   NO_RAG        — collections_queried empty / undefined
//   RAG_EMPTY     — chunks_returned === 0 (retrieval fired, zero hits)
//   RAG_GROUNDED  — chunks_returned > 0  OR  legacy: collections_queried
//                   non-empty + chunks_returned undefined (unknown count)
//
// Click to expand inline panel. Metadata first, chunks list last so the
// labels stay above the fold even when chunk text is long.

import { useState } from "react";
import type { ChatMessage, SourceChunk } from "../../types";
import { useChatStore } from "../../stores/chatStore";

interface RetrievalBadgeProps {
  message: ChatMessage;
}

type BadgeState = "NO_RAG" | "RAG_EMPTY" | "RAG_GROUNDED";

const TIER_LABELS: Record<string, string> = {
  qdrant_only: "Fast Search",
  qdrant_mongo: "Hybrid Search",
  qdrant_mongo_graph: "Graph Augmentation",
};

const SPEED_LABELS: Record<string, string> = {
  fast: "Fast",
  balanced: "Balanced",
  thorough: "Thorough",
  custom: "Custom",
};

const REASON_LABELS: Record<string, string> = {
  none: "Off",
  meta: "Meta",
  atomic: "Atomic",
  self_correct: "Self-Correct",
};

function deriveState(message: ChatMessage): BadgeState {
  const corpora = message.collections_queried ?? [];
  const hasSources = (message.sources ?? []).length > 0;
  const usedWeb = (message.tools_used ?? []).includes("web_search");
  if (corpora.length === 0 && !hasSources && !usedWeb) return "NO_RAG";
  // Strict zero — retrieval fired but returned nothing.
  if (message.chunks_returned === 0 && !hasSources) return "RAG_EMPTY";
  // chunks_returned > 0 OR undefined (legacy message): both → grounded.
  return "RAG_GROUNDED";
}

function humanizeStrategy(raw: string | undefined): string | null {
  if (!raw) return null;
  return TIER_LABELS[raw] ?? raw;
}

function humanizeSpeed(raw: string | undefined): string | null {
  if (!raw) return null;
  return SPEED_LABELS[raw] ?? raw;
}

function humanizeReason(raw: string | undefined): string | null {
  if (!raw) return null;
  return REASON_LABELS[raw] ?? raw;
}

function sourceMetadata(source: SourceChunk): Record<string, unknown> {
  return source.metadata && typeof source.metadata === "object"
    ? source.metadata
    : {};
}

function isWebSource(source: SourceChunk): boolean {
  const metadata = sourceMetadata(source);
  return source.source_tier === "web_search" || typeof metadata.url === "string";
}

function asText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function webEvidenceLabel(source: SourceChunk): string | null {
  const metadata = sourceMetadata(source);
  const mode = asText(metadata.evidence_mode);
  if (mode === "full_page") return "full page";
  if (mode === "snippet_fetch_failed") return "snippet, fetch failed";
  if (mode === "snippet_only") return "snippet";
  if (metadata.full_page_fetched === true) return "full page";
  return isWebSource(source) ? "web" : null;
}

function summarizeWebSources(sources: SourceChunk[]): {
  total: number;
  fullPage: number;
  snippetOnly: number;
  rendered: number;
  cacheHits: number;
} {
  const web = sources.filter(isWebSource);
  return {
    total: web.length,
    fullPage: web.filter((s) => sourceMetadata(s).full_page_fetched === true).length,
    snippetOnly: web.filter((s) => sourceMetadata(s).evidence_mode === "snippet_only").length,
    rendered: web.filter((s) => sourceMetadata(s).js_rendered === true).length,
    cacheHits: web.filter((s) => sourceMetadata(s).cache_hit === true).length,
  };
}

function StatusTag({
  tag,
  tone,
}: {
  tag: string;
  tone: "inf" | "wrn" | "res" | "www";
}) {
  return (
    <span className={`status-badge status-badge-${tone}`}>
      {`<${tag}>`}
    </span>
  );
}

export function RetrievalBadge({ message }: RetrievalBadgeProps) {
  const [open, setOpen] = useState(false);
  const corpora = useChatStore((s) => s.corpora);

  const state = deriveState(message);
  const strategy = humanizeStrategy(message.strategy_used);
  const speed = humanizeSpeed(message.query_profile_used);
  const reasonMode = humanizeReason(message.reasoning_mode_used);
  const hyde = !!message.hyde_applied;
  const chunkCountKnown = typeof message.chunks_returned === "number";
  const factsSeededKnown = typeof message.facts_seeded === "number";
  const corpusIds = message.collections_queried ?? [];
  const corpusNames = corpusIds.map((id) => {
    const found = corpora.find((c) => c.corpus_id === id);
    return found?.name ?? id.slice(0, 8);
  });
  // Phase 24 — skill / tool / reasoning trust signals
  const skills = message.skills_used ?? [];
  const tools = message.tools_used ?? [];
  const agentic = !!message.agentic_mode_used && tools.length > 0;
  const reasoning = !!message.reasoning_cascade_applied;
  const sources = message.sources ?? [];
  const webSummary = summarizeWebSources(sources);
  const webUsed = tools.includes("web_search") || webSummary.total > 0;

  // ── Compose visible label ───────────────────────────────────────────
  let label: string;
  let tag: "INF" | "WRN" | "RES";
  let tone: "inf" | "wrn" | "res";
  if (state === "NO_RAG") {
    label = "Training data only";
    tag = "INF";
    tone = "inf";
  } else if (state === "RAG_EMPTY") {
    const tail = strategy ? ` · ${strategy}` : "";
    label = `RAG · 0 chunks${tail} · training fallback`;
    tag = "WRN";
    tone = "wrn";
  } else {
    const parts: string[] = ["RAG"];
    if (chunkCountKnown) parts.push(`${message.chunks_returned} chunks`);
    if (strategy) parts.push(strategy);
    if (speed) parts.push(speed);
    if (reasonMode && reasonMode !== "Off") parts.push(reasonMode);
    if (hyde) parts.push("HyDE");
    if (webUsed) parts.push("Web");
    if (agentic) parts.push("Agentic");
    label = parts.join(" · ");
    tag = "RES";
    tone = "res";
  }

  // Color palette per state (per spec)
  const palette =
    state === "RAG_GROUNDED"
      ? "text-emerald-400 bg-emerald-400/5 hover:bg-emerald-400/10"
      : state === "RAG_EMPTY"
        ? "text-amber-400 bg-amber-400/5 hover:bg-amber-400/10"
        : "text-content-tertiary";

  // Tooltip = full label so narrow viewports that truncate still surface it
  const tooltip = open ? "Click to collapse" : label;

  return (
    <span
      data-testid="source-citations"
      className="inline-flex flex-col items-start"
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        title={tooltip}
        aria-expanded={open}
        className={`inline-flex items-center gap-1 px-1.5 py-0.5 text-[9px] font-bold tracking-widest uppercase transition-colors ${palette} ${open ? "expanded" : ""}`}
      >
        <span className="disclosure-caret" aria-hidden="true" />
        <StatusTag tag={tag} tone={tone} />
        <span className="truncate max-w-[280px] sm:max-w-none">{label}</span>
      </button>

      {open && (
        <div className="process-group mt-1 w-full max-w-full sm:max-w-[528px] bg-bg-base text-[10.5px] font-mono text-content-secondary normal-case tracking-normal">
          {/* ── Metadata top (small + dense) ─────────────────────── */}
          <div className="px-2.5 py-2 grid grid-cols-1 sm:grid-cols-[88px_1fr] gap-x-2.5 gap-y-1">
            <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
              corpora
            </span>
            <span className="text-content-primary break-words">
              {corpusNames.length > 0 ? corpusNames.join(", ") : "—"}
            </span>

            <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
              retrieval
            </span>
            <span className="text-content-primary">{strategy ?? "—"}</span>

            <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
              speed
            </span>
            <span className="text-content-primary">{speed ?? "—"}</span>

            <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
              hyde
            </span>
            <span className={hyde ? "text-emerald-400" : "text-content-tertiary"}>
              {hyde ? "applied" : "not applied"}
            </span>

            <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
              reason mode
            </span>
            <span className={reasonMode && reasonMode !== "Off" ? "text-content-primary" : "text-content-tertiary"}>
              {reasonMode ?? "unknown"}
            </span>

            {/* Deterministic graph-fact counter — the real number of facts seeded
                into the answer context, straight from retrieval. Never an
                LLM-authored value, so it cannot lie. (Replaces the tool-loop row.) */}
            <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
              facts
            </span>
            <span
              className={
                factsSeededKnown && (message.facts_seeded ?? 0) > 0
                  ? "text-accent-secondary"
                  : "text-content-tertiary"
              }
              title="Graph facts seeded into the answer context — deterministic count from retrieval"
            >
              {factsSeededKnown ? message.facts_seeded : "—"}
            </span>

            {/* Phase 24 — Skills row (always shown, even when empty) */}
            <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
              skills
            </span>
            <span
              className={
                skills.length > 0 ? "text-accent-secondary break-words" : "text-content-tertiary"
              }
            >
              {skills.length > 0 ? skills.join(", ") : "none"}
            </span>

            {/* Phase 24 — Tools row (always shown, even when empty) */}
            <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
              tools
            </span>
            <span
              className={
                tools.length > 0 ? "text-accent-main break-words" : "text-content-tertiary"
              }
            >
              {tools.length > 0 ? tools.join(", ") : "none"}
            </span>

            {webUsed && (
              <>
                <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
                  web
                </span>
                <span className="text-content-primary break-words">
                  {webSummary.total > 0
                    ? `${webSummary.total} sources · ${webSummary.fullPage} full page · ${webSummary.snippetOnly} snippet · ${webSummary.rendered} rendered · ${webSummary.cacheHits} cached`
                    : "tool used"}
                </span>
              </>
            )}

            {/* Phase 24 — Reasoning cascade row */}
            <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
              cascade
            </span>
            <span
              className={reasoning ? "text-emerald-400" : "text-content-tertiary"}
            >
              {reasoning ? "cascade applied" : "off"}
            </span>

            <span className="text-content-tertiary tracking-widest uppercase text-[9.5px]">
              chunks
            </span>
            <span className="text-content-primary">
              {chunkCountKnown ? message.chunks_returned : "unknown (legacy)"}
            </span>

            {message.downgrade_reason && (
              <>
                <span className="text-amber-400 tracking-widest uppercase text-[9.5px]">
                  downgrade
                </span>
                <span className="text-amber-400 break-words">
                  {message.downgrade_reason}
                </span>
              </>
            )}
          </div>

          {/* ── Chunks list LAST (largest visual, scrollable) ────── */}
          {state === "RAG_GROUNDED" && (
            <ChunksList sources={sources} />
          )}
        </div>
      )}
    </span>
  );
}

function ChunksList({ sources }: { sources: SourceChunk[] }) {
  if (sources.length === 0) {
    return (
      <div className="px-2 py-1.5 text-content-tertiary italic">
        Source previews are not available for this older message.
      </div>
    );
  }
  return (
    <div className="max-h-[240px] overflow-y-auto custom-scrollbar divide-y divide-border-minimal">
      {sources.map((s, i) => (
        <div
          key={s.chunk_id ?? i}
          className="px-2.5 py-2 hover:bg-bg-surface/40 transition-colors"
        >
          {isWebSource(s) && (
            <WebSourceLine source={s} />
          )}
          <div className="flex items-center justify-between gap-2 text-[9.5px] tracking-widest uppercase text-content-tertiary">
            <span className="truncate">
              {s.doc_name ?? s.doc_id.slice(0, 12)}
            </span>
            <span className="text-accent-secondary shrink-0">
              {s.score.toFixed(3)}
            </span>
          </div>
          <div className="mt-0.5 text-[10.5px] text-content-secondary leading-snug break-words">
            {s.text.slice(0, 120)}
            {s.text.length > 120 ? "…" : ""}
          </div>
        </div>
      ))}
    </div>
  );
}

function WebSourceLine({ source }: { source: SourceChunk }) {
  const metadata = sourceMetadata(source);
  const url = asText(metadata.url) ?? source.doc_id;
  const evidence = webEvidenceLabel(source);
  const status = asText(metadata.fetch_status);
  const cache = metadata.cache_hit === true ? "cached" : null;
  const rendered = metadata.js_rendered === true ? "rendered" : null;
  const pieces = [evidence, status, cache, rendered].filter(Boolean);
  return (
    <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[9.5px]">
      <StatusTag tag="WWW" tone="www" />
      {pieces.length > 0 && (
        <span className="text-content-tertiary">{pieces.join(" · ")}</span>
      )}
      {url && (
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          className="text-accent-secondary hover:underline break-all"
        >
          {url}
        </a>
      )}
    </div>
  );
}
