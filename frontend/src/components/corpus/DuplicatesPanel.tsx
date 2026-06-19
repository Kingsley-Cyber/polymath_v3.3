// DuplicatesPanel.tsx — near-duplicate document detection + resolution, shown
// inside CorpusDetail. Runs an exact containment scan, then lets the user remove
// redundant copies one cluster at a time (keeping a chosen canonical), or bulk-
// remove only the near-identical ("certain") copies. The C++/Java-style traps
// surface as `review` and are never part of the safe bulk action.
import { useState, useEffect, useCallback } from "react";
import {
  Loader2,
  RefreshCw,
  Trash2,
  ShieldCheck,
  AlertTriangle,
  FileText,
} from "lucide-react";
import * as api from "../../lib/api";
import type {
  DuplicatesResponse,
  DuplicateCluster,
  DuplicateConfidence,
} from "../../lib/api";

const CONF_STYLE: Record<string, string> = {
  certain: "bg-green-500/15 text-green-400 border-green-500/40",
  likely: "bg-amber-400/15 text-amber-300 border-amber-400/40",
  review: "bg-error/15 text-error border-error/40",
};

const CONF_HINT: Record<DuplicateConfidence, string> = {
  certain: "near-identical — the smaller file is fully inside the kept one",
  likely: "same work, different format/edition — quick check advised",
  review: "may be DISTINCT works that share text — verify before removing",
};

function Badge({ c }: { c: string }) {
  return (
    <span
      className={`inline-flex items-center px-1.5 py-0.5 text-[8px] font-bold tracking-widest uppercase border ${
        CONF_STYLE[c] || CONF_STYLE.review
      }`}
    >
      {c}
    </span>
  );
}

function summarize(
  clusters: DuplicateCluster[],
  base: DuplicatesResponse,
): DuplicatesResponse {
  const redundant = clusters.flatMap((c) =>
    c.members.filter((m) => !m.is_canonical),
  );
  return {
    ...base,
    cluster_count: clusters.length,
    duplicate_document_count: redundant.length,
    redundant_chunk_count: redundant.reduce((s, m) => s + m.chunk_count, 0),
    by_confidence: {
      certain: redundant.filter((m) => m.confidence === "certain").length,
      likely: redundant.filter((m) => m.confidence === "likely").length,
      review: redundant.filter((m) => m.confidence === "review").length,
    },
    clusters,
  };
}

interface Props {
  corpusId: string;
  /** Called after any deletion so the parent can refresh its doc list/counts. */
  onResolved?: () => void;
}

export function DuplicatesPanel({ corpusId, onResolved }: Props) {
  const [data, setData] = useState<DuplicatesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyCluster, setBusyCluster] = useState<string | null>(null);
  const [confirmCluster, setConfirmCluster] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  // Per-cluster override of which copy to keep (canonical_doc_id -> keep doc_id).
  const [keepOverride, setKeepOverride] = useState<Record<string, string>>({});

  const scan = useCallback(async () => {
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      setData(await api.getDuplicates(corpusId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Scan failed");
    } finally {
      setLoading(false);
    }
  }, [corpusId]);

  useEffect(() => {
    void scan();
  }, [scan]);

  const removeCluster = async (cluster: DuplicateCluster) => {
    setBusyCluster(cluster.canonical_doc_id);
    setError(null);
    setNotice(null);
    try {
      const keep = keepOverride[cluster.canonical_doc_id];
      const res = await api.resolveDuplicates(corpusId, {
        apply: true,
        // Explicit per-cluster action → all tiers eligible for THIS cluster.
        min_confidence: "review",
        only_canonicals: [cluster.canonical_doc_id],
        keep_overrides: keep
          ? { [cluster.canonical_doc_id]: keep }
          : undefined,
      });
      setNotice(
        `Removed ${res.documents_deleted ?? 0} file(s), freed ${
          res.chunks_freed ?? 0
        } chunks (backed up).`,
      );
      setConfirmCluster(null);
      // Optimistic: drop the resolved cluster without a full re-scan.
      setData((prev) =>
        prev
          ? summarize(
              prev.clusters.filter(
                (c) => c.canonical_doc_id !== cluster.canonical_doc_id,
              ),
              prev,
            )
          : prev,
      );
      onResolved?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Remove failed");
    } finally {
      setBusyCluster(null);
    }
  };

  const removeAllCertain = async () => {
    setBulkBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.resolveDuplicates(corpusId, {
        apply: true,
        min_confidence: "certain",
      });
      setNotice(
        `Removed ${res.documents_deleted ?? 0} near-identical file(s), freed ${
          res.chunks_freed ?? 0
        } chunks (backed up).`,
      );
      // Optimistic: drop only the `certain` redundant copies; keep the rest.
      setData((prev) => {
        if (!prev) return prev;
        const clusters = prev.clusters
          .map((c) => ({
            ...c,
            members: c.members.filter(
              (m) => m.is_canonical || m.confidence !== "certain",
            ),
          }))
          .filter((c) => c.members.length > 1);
        return summarize(clusters, prev);
      });
      onResolved?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Bulk remove failed");
    } finally {
      setBulkBusy(false);
    }
  };

  const wrap =
    "border-b border-border-minimal bg-bg-base/70 px-4 py-3 shrink-0 space-y-3";

  if (loading) {
    return (
      <div className={wrap}>
        <div className="flex items-center gap-2 text-[10px] text-content-tertiary">
          <Loader2 className="w-3.5 h-3.5 animate-spin text-accent-main" />
          <span>
            Scanning for near-duplicate documents… this can take up to a minute
            on large corpora.
          </span>
        </div>
      </div>
    );
  }

  const bc = data?.by_confidence ?? { certain: 0, likely: 0, review: 0 };

  return (
    <div className={wrap}>
      {/* Header */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-[9px] font-bold tracking-widest text-content-tertiary uppercase">
            Near-Duplicate Documents
          </span>
          {data && (
            <span className="text-[9px] text-content-tertiary tracking-wider">
              {data.cluster_count} cluster
              {data.cluster_count === 1 ? "" : "s"} ·{" "}
              {data.duplicate_document_count} redundant ·{" "}
              {data.redundant_chunk_count.toLocaleString()} chunks
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {bc.certain > 0 && (
            <button
              onClick={removeAllCertain}
              disabled={bulkBusy || busyCluster !== null}
              className="flex items-center gap-1 px-2 py-1 text-[9px] font-bold tracking-widest text-green-400 border border-green-500/50 hover:bg-green-500/10 disabled:opacity-40 transition-none uppercase"
              title="Remove every near-identical (certain) redundant copy. Each keeps the fuller copy and is backed up."
            >
              {bulkBusy ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                <ShieldCheck className="w-3 h-3" />
              )}
              <span>Remove {bc.certain} safe</span>
            </button>
          )}
          <button
            onClick={scan}
            disabled={bulkBusy || busyCluster !== null}
            className="flex items-center gap-1 px-2 py-1 text-[9px] font-bold tracking-widest border border-border-minimal text-content-tertiary hover:border-content-secondary hover:text-content-secondary disabled:opacity-40 transition-none uppercase"
            title="Re-scan the corpus"
          >
            <RefreshCw className="w-3 h-3" />
            <span>Rescan</span>
          </button>
        </div>
      </div>

      {/* Banners */}
      {notice && (
        <div className="px-2 py-1 bg-green-500/10 border border-green-500/30 text-[10px] text-green-400">
          {notice}
        </div>
      )}
      {error && (
        <div className="flex items-center gap-2 px-2 py-1 bg-error/10 border border-error/30 text-[10px] text-error">
          <AlertTriangle className="w-3 h-3 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Empty state */}
      {data && data.cluster_count === 0 && (
        <div className="text-[10px] text-content-tertiary py-2">
          No near-duplicate documents found. This corpus is clean.
        </div>
      )}

      {/* Clusters */}
      {data && data.clusters.length > 0 && (
        <div className="space-y-2">
          {data.clusters.map((cluster) => {
            const keepId =
              keepOverride[cluster.canonical_doc_id] ??
              cluster.canonical_doc_id;
            const removeCount = cluster.members.filter(
              (m) => m.doc_id !== keepId,
            ).length;
            const isBusy = busyCluster === cluster.canonical_doc_id;
            const confirming = confirmCluster === cluster.canonical_doc_id;
            return (
              <div
                key={cluster.canonical_doc_id}
                className="border border-border-minimal bg-bg-base/50"
              >
                {/* Cluster header */}
                <div className="flex items-center justify-between gap-2 px-2.5 py-1.5 border-b border-border-minimal/60">
                  <div className="flex items-center gap-2">
                    <Badge c={cluster.confidence} />
                    <span className="text-[9px] text-content-tertiary tracking-wider">
                      max overlap {(cluster.max_similarity * 100).toFixed(0)}%
                    </span>
                  </div>
                  {confirming ? (
                    <div className="flex items-center gap-1">
                      <span className="text-[9px] text-content-tertiary">
                        Remove {removeCount} file{removeCount === 1 ? "" : "s"}?
                      </span>
                      <button
                        onClick={() => removeCluster(cluster)}
                        disabled={isBusy}
                        className="px-1.5 py-0.5 text-[9px] font-bold tracking-widest text-error border border-error hover:bg-error hover:text-bg-base disabled:opacity-40 transition-none uppercase"
                      >
                        {isBusy ? (
                          <Loader2 className="w-3 h-3 animate-spin" />
                        ) : (
                          "Yes"
                        )}
                      </button>
                      <button
                        onClick={() => setConfirmCluster(null)}
                        disabled={isBusy}
                        className="px-1.5 py-0.5 text-[9px] font-bold tracking-widest text-content-tertiary border border-border-minimal hover:border-content-secondary transition-none uppercase"
                      >
                        No
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() =>
                        setConfirmCluster(cluster.canonical_doc_id)
                      }
                      disabled={busyCluster !== null || bulkBusy}
                      className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-content-tertiary border border-border-minimal hover:border-error hover:text-error disabled:opacity-40 transition-none uppercase"
                      title="Remove the redundant copies, keep the selected one"
                    >
                      <Trash2 className="w-3 h-3" />
                      <span>Remove {removeCount}</span>
                    </button>
                  )}
                </div>

                {/* Members — radio picks which copy to KEEP */}
                <div className="divide-y divide-border-minimal/40">
                  {cluster.members.map((m) => {
                    const keep = m.doc_id === keepId;
                    return (
                      <label
                        key={m.doc_id}
                        className={`flex items-start gap-2 px-2.5 py-1.5 cursor-pointer ${
                          keep ? "bg-accent-main/5" : "hover:bg-bg-surface/40"
                        }`}
                      >
                        <input
                          type="radio"
                          name={`keep-${cluster.canonical_doc_id}`}
                          checked={keep}
                          onChange={() =>
                            setKeepOverride((prev) => ({
                              ...prev,
                              [cluster.canonical_doc_id]: m.doc_id,
                            }))
                          }
                          className="mt-0.5 accent-accent-main"
                        />
                        <FileText className="w-3 h-3 mt-0.5 shrink-0 text-content-tertiary" />
                        <div className="min-w-0 flex-1">
                          <div
                            className="text-[10px] text-content-primary truncate"
                            title={m.filename}
                          >
                            {m.filename}
                          </div>
                          <div className="text-[9px] text-content-tertiary tracking-wider">
                            {m.chunk_count.toLocaleString()} chunks
                            {!m.is_canonical &&
                              ` · ${(
                                m.containment_to_canonical * 100
                              ).toFixed(0)}% inside kept`}
                          </div>
                        </div>
                        <span
                          className={`shrink-0 text-[8px] font-bold tracking-widest uppercase mt-0.5 ${
                            keep ? "text-accent-main" : "text-content-tertiary/60"
                          }`}
                        >
                          {keep ? "keep" : "remove"}
                        </span>
                      </label>
                    );
                  })}
                </div>

                {/* Per-tier hint */}
                <div className="px-2.5 py-1 text-[8px] text-content-tertiary/70 border-t border-border-minimal/40">
                  {CONF_HINT[cluster.confidence]}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Footer legend */}
      {data && data.cluster_count > 0 && (
        <div className="flex items-center gap-3 text-[8px] text-content-tertiary/70 pt-1">
          <span className="flex items-center gap-1">
            <Badge c="certain" /> {bc.certain}
          </span>
          <span className="flex items-center gap-1">
            <Badge c="likely" /> {bc.likely}
          </span>
          <span className="flex items-center gap-1">
            <Badge c="review" /> {bc.review}
          </span>
          <span className="ml-auto">
            Removals keep one copy per cluster, cascade across stores, and are
            backed up (recoverable).
          </span>
        </div>
      )}
    </div>
  );
}
