import { useCallback, useState } from "react";
import { Search, Loader2 } from "lucide-react";
import { entitySearch } from "../../lib/api";
import type { SourceChunk } from "../../types";

interface Props {
  corpusId: string;
  className?: string;
}

export default function EntityChunkSearch({ corpusId, className = "" }: Props) {
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(20);
  const [results, setResults] = useState<SourceChunk[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ran, setRan] = useState(false);

  const run = useCallback(async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await entitySearch(query.trim(), {
        corpusIds: [corpusId],
        limit,
        hydrate: true,
      });
      setResults(resp.chunks);
      setRan(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Search failed");
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, [query, corpusId, limit]);

  return (
    <div className={`flex flex-col gap-3 ${className}`}>
      <div className="flex items-center gap-2">
        <div className="flex-1 flex items-center gap-2 bg-[#121418] border border-white/5 rounded px-2 py-1.5">
          <Search className="w-3.5 h-3.5 text-gray-500" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run()}
            placeholder="Entity name or substring…"
            className="flex-1 bg-transparent text-[12px] text-white outline-none placeholder:text-gray-600"
          />
        </div>
        <input
          type="number"
          min={1}
          max={100}
          value={limit}
          onChange={(e) =>
            setLimit(Math.max(1, Math.min(100, Number(e.target.value) || 20)))
          }
          className="w-14 bg-[#121418] border border-white/5 rounded px-2 py-1.5 text-[11px] text-white text-center outline-none"
          title="Max chunks returned"
        />
        <button
          onClick={run}
          disabled={loading || !query.trim()}
          className="px-3 py-1.5 text-[11px] font-bold uppercase tracking-widest border border-white/10 bg-[#1a1d24] hover:bg-[#22262f] disabled:opacity-40 text-white rounded"
        >
          {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "Search"}
        </button>
      </div>

      {error && (
        <div className="text-[11px] text-red-400 font-mono">[ERROR] {error}</div>
      )}

      {ran && !loading && results.length === 0 && !error && (
        <div className="text-[11px] text-gray-500 italic">
          No chunks mention entities matching "{query}".
        </div>
      )}

      <div className="flex flex-col gap-2">
        {results.map((c) => (
          <div
            key={c.chunk_id}
            className="bg-[#121418] border border-white/5 rounded p-3"
          >
            <div className="flex items-center justify-between mb-1.5">
              <div className="text-[10px] font-mono text-gray-500 truncate">
                {c.doc_name || c.doc_id}
              </div>
              <div className="text-[10px] text-amber-400 font-mono">
                score {c.score.toFixed(3)}
              </div>
            </div>
            {c.provenance && c.provenance.length > 0 && (
              <div className="flex flex-wrap gap-1 mb-2">
                {c.provenance.slice(0, 5).map((p, i) => (
                  <span
                    key={i}
                    className="px-1.5 py-0.5 text-[9px] font-bold rounded bg-amber-500/10 text-amber-300 uppercase tracking-wider"
                    title={`confidence ${p.confidence.toFixed(2)}`}
                  >
                    {p.entity}
                  </span>
                ))}
              </div>
            )}
            <div className="text-[12px] text-gray-300 whitespace-pre-wrap leading-relaxed line-clamp-6">
              {c.text || c.summary || "(no text hydrated)"}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
