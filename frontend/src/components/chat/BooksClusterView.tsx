/**
 * BooksClusterView — books-as-clusters graph mode.
 *
 * Each Document is a cluster, shared entities form bridges. Three states:
 *   1. Overview  — cluster cards (no nodes/edges loaded). Default.
 *   2. Drill     — clicked a cluster: load that book's entities + bridges
 *                  to other books. Sigma-rendered.
 *   3. Empty     — no corpora selected.
 *
 * Phase 1 ships the overview grid + a drill panel. The full WebGL
 * atom-shape rendering with cluster hulls is layered on top in a
 * follow-up commit; the data flow + selection state are already wired.
 */

import { useEffect, useMemo, useState } from "react";

import {
  getGraphByDocument,
  type ByDocumentCluster,
  type ByDocumentGraphResponse,
} from "../../lib/api";

interface BooksClusterViewProps {
  corpusIds: string[];
  onClose?: () => void;
}

// ── color: deterministic per-doc hue for cluster identity ──────────
function clusterHue(docId: string): number {
  let h = 0;
  for (let i = 0; i < docId.length; i++) {
    h = (h * 31 + docId.charCodeAt(i)) >>> 0;
  }
  return h % 360;
}

function clusterColor(docId: string, opts: { lightness?: number; alpha?: number } = {}): string {
  const { lightness = 55, alpha = 1 } = opts;
  return `hsla(${clusterHue(docId)}, 65%, ${lightness}%, ${alpha})`;
}

// ── overview card ───────────────────────────────────────────────────
function ClusterCard({
  cluster,
  active,
  onClick,
}: {
  cluster: ByDocumentCluster;
  active: boolean;
  onClick: () => void;
}) {
  const success = cluster.ghost_b_success_rate;
  const successPct = success != null ? Math.round(success * 1000) / 10 : null;
  const accent = clusterColor(cluster.cluster_id);
  const halo = clusterColor(cluster.cluster_id, { lightness: 25, alpha: 0.4 });

  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "relative text-left rounded-lg p-3 border transition-all",
        "bg-zinc-900/80 hover:bg-zinc-800/90",
        active
          ? "border-amber-400/80 ring-1 ring-amber-400/50"
          : "border-zinc-800 hover:border-zinc-700",
      ].join(" ")}
      style={{
        boxShadow: active ? `0 0 20px ${halo}` : undefined,
      }}
    >
      <span
        className="absolute left-0 top-0 bottom-0 w-1 rounded-l-lg"
        style={{ background: accent }}
        aria-hidden="true"
      />
      <div className="flex items-start gap-2 mb-1.5 pl-1">
        <div className="text-[11px] uppercase tracking-wider text-zinc-500">book</div>
        {successPct != null && (
          <div
            className={[
              "ml-auto text-[10px] font-mono px-1.5 py-0.5 rounded",
              successPct >= 99
                ? "bg-emerald-900/40 text-emerald-300"
                : successPct >= 90
                  ? "bg-amber-900/40 text-amber-300"
                  : "bg-rose-900/40 text-rose-300",
            ].join(" ")}
          >
            {successPct}%
          </div>
        )}
      </div>
      <div className="text-sm text-zinc-100 font-medium pl-1 line-clamp-2 mb-2">
        {cluster.label || cluster.cluster_id.slice(0, 12)}
      </div>
      <div className="flex items-center gap-3 text-[11px] text-zinc-400 pl-1">
        <span title="Distinct entities mentioned in this book">
          <span className="font-mono text-zinc-200">{cluster.entity_count.toLocaleString()}</span> entities
        </span>
        {cluster.total_mentions != null && (
          <span title="Total mention occurrences">
            <span className="font-mono text-zinc-200">{cluster.total_mentions.toLocaleString()}</span> mentions
          </span>
        )}
      </div>
      {cluster.top_entity_names && cluster.top_entity_names.length > 0 && (
        <div className="mt-2 pl-1 flex flex-wrap gap-1">
          {cluster.top_entity_names.slice(0, 5).map((name, i) => (
            <span
              key={i}
              className="text-[10px] px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-300"
              title={name}
            >
              {name.length > 22 ? name.slice(0, 22) + "…" : name}
            </span>
          ))}
        </div>
      )}
    </button>
  );
}

// ── drill panel ─────────────────────────────────────────────────────
function DrillPanel({
  data,
  drillDocId,
  onClose,
}: {
  data: ByDocumentGraphResponse | null;
  drillDocId: string | null;
  onClose: () => void;
}) {
  if (!drillDocId || !data) return null;
  const drilled = data.clusters.find((c) => c.cluster_id === drillDocId);
  const bridgeNodes = data.nodes.filter((n) => n.bridge_doc_ids.length > 0);
  const exclusiveNodes = data.nodes.filter((n) => n.bridge_doc_ids.length === 0);
  const crossEdges = data.edges.filter((e) => e.cross_cluster);
  const accent = drillDocId ? clusterColor(drillDocId, { lightness: 60 }) : "#888";

  return (
    <div className="border-t border-zinc-800 bg-zinc-950/80 p-4">
      <div className="flex items-center gap-3 mb-3">
        <span
          className="w-3 h-3 rounded-full"
          style={{ background: accent }}
          aria-hidden="true"
        />
        <h3 className="text-sm font-semibold text-zinc-100">
          {drilled?.label || drillDocId.slice(0, 12)}
        </h3>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto text-[11px] text-zinc-400 hover:text-zinc-100 px-2 py-1 rounded hover:bg-zinc-800"
        >
          close drill
        </button>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
        <Stat label="entities" value={data.nodes.length} />
        <Stat label="bridges" value={bridgeNodes.length} hint="entities also in other books" />
        <Stat label="exclusive" value={exclusiveNodes.length} hint="entities unique to this book" />
        <Stat label="cross-cluster edges" value={crossEdges.length} />
      </div>

      <div className="grid sm:grid-cols-2 gap-4">
        <div>
          <h4 className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
            top bridge entities
          </h4>
          <div className="flex flex-col gap-1.5">
            {bridgeNodes
              .sort((a, b) => b.bridge_doc_ids.length - a.bridge_doc_ids.length)
              .slice(0, 12)
              .map((n) => (
                <div
                  key={n.id}
                  className="flex items-center gap-2 text-[12px] py-1 px-2 rounded bg-zinc-900/60"
                >
                  <span className="text-zinc-100 truncate flex-1" title={n.display_name}>
                    {n.display_name}
                  </span>
                  <span
                    className="text-[10px] uppercase tracking-wider text-zinc-500"
                    title="entity type"
                  >
                    {n.entity_type}
                  </span>
                  <span
                    className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-300"
                    title={`bridges to ${n.bridge_doc_ids.length} other book(s)`}
                  >
                    +{n.bridge_doc_ids.length}
                  </span>
                </div>
              ))}
          </div>
        </div>

        <div>
          <h4 className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
            connected books (bridge clusters)
          </h4>
          <div className="flex flex-col gap-1.5">
            {data.clusters
              .filter((c) => c.cluster_id !== drillDocId)
              .map((c) => {
                const sharedCount = bridgeNodes.filter((n) =>
                  n.bridge_doc_ids.includes(c.cluster_id),
                ).length;
                return { cluster: c, shared: sharedCount };
              })
              .sort((a, b) => b.shared - a.shared)
              .slice(0, 12)
              .map(({ cluster, shared }) => (
                <div
                  key={cluster.cluster_id}
                  className="flex items-center gap-2 text-[12px] py-1 px-2 rounded bg-zinc-900/60"
                >
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ background: clusterColor(cluster.cluster_id) }}
                  />
                  <span className="text-zinc-100 truncate flex-1" title={cluster.label}>
                    {cluster.label || cluster.cluster_id.slice(0, 12)}
                  </span>
                  <span className="text-[10px] font-mono text-zinc-400">
                    {shared} shared
                  </span>
                </div>
              ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: number;
  hint?: string;
}) {
  return (
    <div className="rounded bg-zinc-900/60 px-3 py-2" title={hint}>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div className="text-base font-mono text-zinc-100">{value.toLocaleString()}</div>
    </div>
  );
}

// ── main view ──────────────────────────────────────────────────────
export function BooksClusterView({ corpusIds, onClose }: BooksClusterViewProps) {
  const [overview, setOverview] = useState<ByDocumentGraphResponse | null>(null);
  const [drilled, setDrilled] = useState<ByDocumentGraphResponse | null>(null);
  const [drillDocId, setDrillDocId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Stable key so we only refetch overview when the actual corpus
  // selection changes, not on every render.
  const corpusKey = useMemo(() => [...corpusIds].sort().join(","), [corpusIds]);

  // Fetch the overview when corpus selection changes.
  useEffect(() => {
    if (corpusIds.length === 0) {
      setOverview(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    getGraphByDocument({ corpusIds, mode: "overview" })
      .then((res) => {
        if (!cancelled) {
          setOverview(res);
          // Reset drill if the previously-drilled doc is no longer in scope.
          if (drillDocId && !res.clusters.find((c) => c.cluster_id === drillDocId)) {
            setDrillDocId(null);
            setDrilled(null);
          }
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [corpusKey]);

  // Fetch drill subgraph when a cluster is selected.
  useEffect(() => {
    if (!drillDocId || corpusIds.length === 0) {
      setDrilled(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    getGraphByDocument({
      corpusIds,
      mode: "drill",
      drillDocId,
      maxNodes: 1500,
    })
      .then((res) => {
        if (!cancelled) setDrilled(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drillDocId, corpusKey]);

  if (corpusIds.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-zinc-500 text-sm">
        Select one or more corpora to see books-as-clusters.
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-3 px-4 py-3 border-b border-zinc-800">
        <h2 className="text-sm font-semibold text-zinc-100">Books as clusters</h2>
        <span className="text-[11px] text-zinc-500">
          {overview?.clusters.length ?? 0} books
          {overview?.clusters && overview.clusters.length > 0 && (
            <>
              {" · "}
              {overview.clusters
                .reduce((s, c) => s + (c.entity_count || 0), 0)
                .toLocaleString()}
              {" entities"}
            </>
          )}
        </span>
        {loading && <span className="text-[11px] text-amber-400 ml-auto">loading…</span>}
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="text-[11px] text-zinc-400 hover:text-zinc-100 px-2 py-1 rounded hover:bg-zinc-800 ml-auto"
          >
            close
          </button>
        )}
      </div>

      {error && (
        <div className="px-4 py-2 text-xs text-rose-300 bg-rose-950/30 border-b border-rose-900">
          {error}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-4 grid gap-3 grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 content-start">
        {(overview?.clusters || []).map((cluster) => (
          <ClusterCard
            key={cluster.cluster_id}
            cluster={cluster}
            active={cluster.cluster_id === drillDocId}
            onClick={() =>
              setDrillDocId(cluster.cluster_id === drillDocId ? null : cluster.cluster_id)
            }
          />
        ))}
        {!loading && overview && overview.clusters.length === 0 && (
          <div className="col-span-full text-zinc-500 text-sm py-8 text-center">
            No documents with extracted entities in the selected corpora.
            Run an ingest first.
          </div>
        )}
      </div>

      {drillDocId && (
        <DrillPanel
          data={drilled}
          drillDocId={drillDocId}
          onClose={() => setDrillDocId(null)}
        />
      )}
    </div>
  );
}
