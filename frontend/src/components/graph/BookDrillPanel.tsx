/**
 * Phase 4.C2 — Book drill-down sidebar.
 *
 * Renders one cluster's entities + cross-doc bridges when the user clicks
 * a book dot on the Constellation canvas. Calls
 * POST /api/graph/by-document {mode: "drill", drill_doc_id: <id>}.
 *
 * Pure presentational — fetch + state lives in the parent.
 *
 * Premium redesign (Pt 7d): typographic hierarchy tightened, lane tags
 * make corpus vs graph evidence obvious, top-entity cloud is bounded
 * so a 200-entity cluster doesn't blow the panel out, cross-doc
 * bridges sort by strength.
 */

import { ArrowRight, BookOpen, ChevronRight, Layers, X } from "lucide-react";

import type {
  ByDocumentCluster,
  ByDocumentEdge,
  ByDocumentNode,
} from "../../lib/api";
import { graphColors } from "../../lib/graph-colors";

type Props = {
  cluster: ByDocumentCluster;
  nodes: ByDocumentNode[];
  edges: ByDocumentEdge[];
  loading?: boolean;
  onClose: () => void;
};

function entityTypeAccent(type: string | undefined): string {
  if (!type) return graphColors.entity.Other;
  return (
    (graphColors.entity as Record<string, string>)[type] ||
    graphColors.entity.Other
  );
}

export default function BookDrillPanel({
  cluster,
  nodes,
  edges,
  loading,
  onClose,
}: Props) {
  const bridges = edges
    .filter((e) => e.cross_cluster)
    .slice();
  const internalEdges = edges.filter((e) => !e.cross_cluster);
  const topEntities = nodes
    .slice()
    .sort(
      (a, b) => (b.total_mentions ?? 0) - (a.total_mentions ?? 0),
    )
    .slice(0, 40);

  return (
    <aside
      className="absolute top-0 right-0 z-20 flex h-full w-[min(28rem,calc(100%-2rem))] flex-col overflow-hidden border-l border-border-minimal bg-[var(--bg-raised)]/95 shadow-[0_24px_60px_-24px_rgba(0,0,0,0.65)] backdrop-blur"
      aria-label="Book drill-down"
    >
      {/* Header */}
      <header className="flex shrink-0 items-start justify-between gap-3 border-b border-border-minimal px-4 py-3.5">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[9px] font-mono uppercase tracking-[0.18em] text-content-tertiary">
            <BookOpen className="h-3 w-3 text-accent-main" />
            <span>Drill down · {cluster.entity_count ?? 0} entities</span>
          </div>
          <h3 className="mt-1.5 truncate text-base font-semibold text-content-primary">
            {cluster.label || "Document"}
          </h3>
          <p className="mt-0.5 text-[11px] text-content-tertiary font-mono">
            {cluster.total_mentions ?? 0} mentions
            <span className="mx-1.5 text-content-tertiary/60">·</span>
            {bridges.length} cross-doc bridges
          </p>
        </div>
        <button
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded border border-border-minimal bg-[var(--bg-base)] text-content-secondary transition-colors hover:border-content-secondary hover:text-content-primary"
          onClick={onClose}
          aria-label="Close drill panel"
          title="Close"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </header>

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 custom-scroll">
        {loading && nodes.length === 0 ? (
          <div className="flex items-center gap-2 py-6 text-[11px] font-mono text-content-tertiary">
            <div className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent-main" />
            Loading cluster members…
          </div>
        ) : (
          <>
            {/* Entities section */}
            <section>
              <div className="mb-2 flex items-center justify-between gap-2">
                <h4 className="text-[10px] font-mono uppercase tracking-[0.18em] text-content-tertiary">
                  Entities
                </h4>
                <span className="text-[10px] font-mono text-content-tertiary">
                  {nodes.length} total · top {topEntities.length}
                </span>
              </div>
              <ul className="divide-y divide-border-minimal overflow-hidden rounded border border-border-minimal bg-[var(--bg-base)]">
                {topEntities.map((n) => {
                  const accent = entityTypeAccent(n.entity_type);
                  return (
                    <li
                      key={n.id}
                      className="flex items-center justify-between gap-2 px-2.5 py-1.5 text-[12px]"
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <span
                          className="h-2 w-2 shrink-0 rounded-full"
                          style={{ backgroundColor: accent }}
                          aria-hidden
                        />
                        <span className="truncate text-content-primary">
                          {n.display_name}
                        </span>
                      </div>
                      <div className="flex shrink-0 items-center gap-1.5">
                        {n.entity_type && (
                          <span className="rounded border border-border-minimal px-1 py-0.5 text-[9px] font-mono uppercase tracking-wider text-content-tertiary">
                            {n.entity_type}
                          </span>
                        )}
                        <span className="font-mono text-[10px] tabular-nums text-content-tertiary">
                          {n.total_mentions ?? 0}
                        </span>
                      </div>
                    </li>
                  );
                })}
                {nodes.length === 0 && (
                  <li className="px-2.5 py-2 text-[11px] italic text-content-tertiary">
                    No entities extracted yet
                  </li>
                )}
              </ul>
            </section>

            {/* Cross-doc bridges section */}
            {bridges.length > 0 && (
              <section className="mt-5">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <h4 className="text-[10px] font-mono uppercase tracking-[0.18em] text-content-tertiary">
                    Cross-doc bridges
                  </h4>
                  <span className="rounded border border-border-minimal bg-[var(--bg-base)] px-1.5 py-0.5 text-[9px] font-mono text-content-tertiary">
                    graph lane
                  </span>
                </div>
                <ul className="space-y-1.5">
                  {bridges.slice(0, 24).map((e, i) => {
                    return (
                      <li
                        key={i}
                        className="flex items-start gap-2 rounded border border-border-minimal bg-[var(--bg-base)] px-2.5 py-1.5"
                      >
                        <ArrowRight className="mt-0.5 h-3 w-3 shrink-0 text-content-tertiary" />
                        <div className="min-w-0 flex-1 text-[11px] leading-snug">
                          <div className="truncate text-content-primary">
                            {e.source}
                          </div>
                          <div className="truncate font-mono text-[10px] text-content-tertiary">
                            {e.target}
                          </div>
                        </div>
                        {typeof e.confidence === "number" && (
                          <span className="shrink-0 rounded border border-border-minimal px-1 py-0.5 font-mono text-[9px] tabular-nums text-content-tertiary">
                            {Math.round(e.confidence * 100)}%
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            )}

            {/* Internal relations */}
            {internalEdges.length > 0 && (
              <section className="mt-5">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <h4 className="text-[10px] font-mono uppercase tracking-[0.18em] text-content-tertiary">
                    Internal relations
                  </h4>
                  <span className="text-[10px] font-mono text-content-tertiary">
                    {internalEdges.length}
                  </span>
                </div>
                <ul className="space-y-1 text-[10px] font-mono text-content-secondary">
                  {internalEdges.slice(0, 12).map((e, i) => (
                    <li
                      key={i}
                      className="flex items-center gap-1.5 truncate rounded border border-border-minimal bg-[var(--bg-base)] px-2 py-1"
                    >
                      <Layers className="h-2.5 w-2.5 shrink-0 text-content-tertiary" />
                      <span className="truncate text-content-primary">
                        {e.source}
                      </span>
                      <ChevronRight className="h-2.5 w-2.5 shrink-0 text-content-tertiary" />
                      <span className="truncate text-content-primary">
                        {e.target}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
      </div>
    </aside>
  );
}