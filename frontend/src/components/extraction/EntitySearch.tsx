import { useCallback, useEffect, useRef, useState } from "react";
import { getExtractionEntities } from "../../lib/api";
import type { EntityResult } from "../../types";

const TYPE_COLORS: Record<string, string> = {
  person: "bg-blue-100 text-blue-800",
  org: "bg-purple-100 text-purple-800",
  concept: "bg-green-100 text-green-800",
  other: "bg-gray-100 text-gray-600",
};

interface Props {
  corpusId: string;
  docId?: string;
  onSelect?: (entity: EntityResult) => void;
}

export default function EntitySearch({ corpusId, docId, onSelect }: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<EntityResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [minConfidence, setMinConfidence] = useState(0);
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const search = useCallback(
    async (q: string) => {
      setLoading(true);
      setError(null);
      try {
        const data = await getExtractionEntities(corpusId, {
          q,
          limit: 30,
          doc_id: docId,
        });
        setResults(data);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Search failed");
        setResults([]);
      } finally {
        setLoading(false);
      }
    },
    [corpusId, docId],
  );

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(query), 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query, search]);

  const typeOptions = Array.from(
    new Set(results.map((r) => r.entity_type).filter(Boolean)),
  ).sort();

  const filtered = results.filter(
    (e) =>
      e.confidence >= minConfidence &&
      (typeFilter === "all" || e.entity_type === typeFilter),
  );

  return (
    <div className="flex flex-col gap-3">
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search entities…"
        className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
      />

      <div className="flex items-center gap-2">
        <label className="flex items-center gap-2 flex-1 text-[10px] uppercase tracking-widest text-gray-500">
          <span>Min conf</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={minConfidence}
            onChange={(e) => setMinConfidence(Number(e.target.value))}
            className="flex-1 accent-amber-500"
          />
          <span className="w-8 font-mono text-gray-400">
            {minConfidence.toFixed(2)}
          </span>
        </label>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="text-[10px] px-1.5 py-0.5 rounded border border-gray-300 bg-white dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
        >
          <option value="all">All types</option>
          {typeOptions.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>

      {loading && (
        <p className="text-xs text-gray-500">Searching…</p>
      )}

      {error && (
        <p className="text-xs text-red-500">{error}</p>
      )}

      {!loading && filtered.length === 0 && results.length > 0 && (
        <p className="text-xs text-gray-400">No entities match current filters.</p>
      )}

      {!loading && results.length === 0 && query.length > 0 && (
        <p className="text-xs text-gray-400">No entities found.</p>
      )}

      <ul className="divide-y divide-gray-100 dark:divide-gray-700">
        {filtered.map((e) => (
          <li
            key={e.entity_id}
            className={`flex items-center justify-between gap-2 py-2 ${onSelect ? "cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800" : ""}`}
            onClick={() => onSelect?.(e)}
          >
            <div className="flex flex-col">
              <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                {e.display_name}
              </span>
              <span className="text-xs text-gray-400">{e.normalized_name}</span>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <span
                className={`rounded px-1.5 py-0.5 text-xs font-medium ${TYPE_COLORS[e.entity_type] ?? TYPE_COLORS.other}`}
              >
                {e.entity_type}
              </span>
              <span className="text-xs text-gray-400">
                ×{e.mention_count}
              </span>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
