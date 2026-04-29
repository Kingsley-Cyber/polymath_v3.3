import { useEffect, useState } from "react";
import { getChunkExtraction } from "../../lib/api";
import type { ChunkExtractionResponse } from "../../types";

const TYPE_COLORS: Record<string, string> = {
  person: "bg-blue-100 text-blue-800",
  org: "bg-purple-100 text-purple-800",
  concept: "bg-green-100 text-green-800",
  other: "bg-gray-100 text-gray-600",
};

interface Props {
  corpusId: string;
  chunkId: string;
}

export default function ChunkExtraction({ corpusId, chunkId }: Props) {
  const [data, setData] = useState<ChunkExtractionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    getChunkExtraction(corpusId, chunkId)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }, [corpusId, chunkId]);

  if (loading) return <p className="text-sm text-gray-400">Loading extraction…</p>;
  if (error) return <p className="text-sm text-red-500">{error}</p>;
  if (!data) return null;

  return (
    <div className="flex flex-col gap-4">
      {/* Entity badges */}
      <div>
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
          Entities ({data.entities.length})
        </h4>
        {data.entities.length === 0 ? (
          <p className="text-xs text-gray-400">No entities extracted.</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {data.entities.map((e) => (
              <span
                key={e.entity_id}
                title={`${e.normalized_name} · confidence ${e.confidence.toFixed(2)}`}
                className={`rounded px-2 py-0.5 text-xs font-medium ${TYPE_COLORS[e.entity_type] ?? TYPE_COLORS.other}`}
              >
                {e.display_name}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Relation rows */}
      <div>
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
          Relations ({data.relations.length})
        </h4>
        {data.relations.length === 0 ? (
          <p className="text-xs text-gray-400">No relations extracted.</p>
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
              {data.relations.map((r, i) => (
                <tr key={i} className="text-gray-700 dark:text-gray-300">
                  <td className="py-1 pr-2">{r.subject_name}</td>
                  <td className="py-1 pr-2 italic text-gray-400">{r.predicate}</td>
                  <td className="py-1 pr-2">{r.object_name}</td>
                  <td className="py-1">{r.confidence.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
