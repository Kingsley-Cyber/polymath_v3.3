import { useEffect, useState } from "react";
import { getEntityRelations } from "../../lib/api";
import type { RelationEdge } from "../../types";

interface Props {
  corpusId: string;
  entityId: string;
  entityName?: string;
  limit?: number;
}

export default function RelationGraph({
  corpusId,
  entityId,
  entityName,
  limit = 20,
}: Props) {
  const [edges, setEdges] = useState<RelationEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    getEntityRelations(corpusId, entityId, limit)
      .then(setEdges)
      .catch((e) =>
        setError(e instanceof Error ? e.message : "Failed to load relations"),
      )
      .finally(() => setLoading(false));
  }, [corpusId, entityId, limit]);

  if (loading) return <p className="text-sm text-gray-400">Loading relations…</p>;
  if (error) return <p className="text-sm text-red-500">{error}</p>;

  return (
    <div className="flex flex-col gap-2">
      {entityName && (
        <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-100">
          Relations — {entityName}
        </h4>
      )}

      {edges.length === 0 ? (
        <p className="text-xs text-gray-400">No relations found for this entity.</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-gray-400">
              <th className="pb-1 pr-2 font-medium">Subject</th>
              <th className="pb-1 pr-2 font-medium">Predicate</th>
              <th className="pb-1 pr-2 font-medium">Object</th>
              <th className="pb-1 font-medium">Conf</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
            {edges.map((r, i) => {
              const isOutgoing = r.subject_id === entityId;
              return (
                <tr
                  key={i}
                  className={`text-gray-700 dark:text-gray-300 ${isOutgoing ? "" : "opacity-70"}`}
                >
                  <td className="py-1 pr-2 font-medium">{r.subject_name}</td>
                  <td className="py-1 pr-2 italic text-gray-400">{r.predicate}</td>
                  <td className="py-1 pr-2">{r.object_name}</td>
                  <td className="py-1">{r.confidence.toFixed(2)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
