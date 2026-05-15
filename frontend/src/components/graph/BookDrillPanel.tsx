/**
 * Phase 4.C2 — Book drill-down sidebar.
 *
 * Renders one cluster's entities + cross-doc bridges when the user clicks
 * a book dot on the Constellation canvas. Calls
 * POST /api/graph/by-document {mode: "drill", drill_doc_id: <id>}.
 *
 * Pure presentational — fetch + state lives in the parent.
 */

import type {
  ByDocumentCluster,
  ByDocumentEdge,
  ByDocumentNode,
} from "../../lib/api";

type Props = {
  cluster: ByDocumentCluster;
  nodes: ByDocumentNode[];
  edges: ByDocumentEdge[];
  loading?: boolean;
  onClose: () => void;
};

export default function BookDrillPanel({
  cluster,
  nodes,
  edges,
  loading,
  onClose,
}: Props) {
  const bridges = edges.filter((e) => e.cross_cluster);

  return (
    <aside className="absolute top-0 right-0 h-full w-96 bg-white border-l border-slate-200 shadow-xl overflow-auto p-4">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-lg font-semibold text-slate-900">
            {cluster.label || "Document"}
          </h3>
          <p className="text-xs text-slate-500 mt-1">
            {cluster.entity_count} entities · {cluster.total_mentions ?? 0}{" "}
            mentions
          </p>
        </div>
        <button
          className="text-slate-400 hover:text-slate-700"
          onClick={onClose}
          aria-label="Close"
        >
          ✕
        </button>
      </div>

      {loading ? (
        <p className="mt-4 text-sm text-slate-500">Loading…</p>
      ) : (
        <>
          <h4 className="mt-5 mb-2 text-sm font-medium text-slate-700">
            Entities in this document
          </h4>
          <ul className="text-sm text-slate-800 space-y-1">
            {nodes.slice(0, 40).map((n) => (
              <li key={n.id} className="flex justify-between gap-2">
                <span className="truncate">{n.display_name}</span>
                <span className="text-slate-400 text-xs">
                  {n.total_mentions}
                </span>
              </li>
            ))}
            {nodes.length === 0 && (
              <li className="text-slate-400 italic">
                No entities extracted yet
              </li>
            )}
          </ul>

          {bridges.length > 0 && (
            <>
              <h4 className="mt-5 mb-2 text-sm font-medium text-slate-700">
                Bridges to other documents
              </h4>
              <ul className="text-xs text-slate-700 space-y-1">
                {bridges.slice(0, 30).map((e, i) => (
                  <li key={i} className="font-mono">
                    {e.source} → {e.target}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </aside>
  );
}
