// CorpusMultiSelect.tsx - UI for selecting the active corpora (tenant scopes)
import { useState, useRef, useEffect } from "react";
import { FolderGit2, Check, ChevronDown } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import * as api from "../../lib/api";
import type { CorpusResponse } from "../../types";

export function CorpusMultiSelect() {
  const [isOpen, setIsOpen] = useState(false);
  const [corpora, setCorpora] = useState<CorpusResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const { selectedCorpusIds, toggleCorpus, purgeStaleCorpusIds } =
    useSettingsStore();

  useEffect(() => {
    const fetchCorpora = async () => {
      try {
        setLoading(true);
        const data = await api.listCorpora();
        setCorpora(data);
        purgeStaleCorpusIds(data.map((c) => c.corpus_id));
      } catch (err) {
        console.error("Failed to fetch corpora:", err);
      } finally {
        setLoading(false);
      }
    };

    fetchCorpora();
  }, [purgeStaleCorpusIds]);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(event.target as Node)
      ) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const selectedCount = selectedCorpusIds.length;

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        data-testid="corpus-multi-select"
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-1.5 px-2 py-1 text-[10px] font-bold tracking-widest uppercase border border-transparent hover:border-border-minimal transition-none rounded-none group"
        title="Select Target Corpora"
      >
        <FolderGit2 className="w-3.5 h-3.5 text-content-tertiary group-hover:text-accent-main" />
        <span className="text-content-secondary hidden sm:inline-block">
          {selectedCount === 0
            ? "[ALL CORPORA]"
            : `[CORPORA: ${selectedCount}]`}
        </span>
        <ChevronDown className="w-3 h-3 text-content-tertiary group-hover:text-accent-main" />
      </button>

      {isOpen && (
        <div className="absolute top-full right-0 mt-1 w-64 border border-white/10 bg-[#2a2a2a] z-[60] p-1 shadow-xl rounded">
          <div className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary px-2 py-1.5 border-b border-border-minimal mb-1 flex justify-between">
            <span>Corpus Scoping</span>
            {selectedCount > 0 && (
              <span className="text-accent-main">({selectedCount} SELECTED)</span>
            )}
          </div>

          <div className="max-h-60 overflow-y-auto custom-scrollbar flex flex-col gap-0.5">
            {loading ? (
              <div className="px-2 py-4 text-center text-[10px] text-content-tertiary uppercase tracking-widest animate-pulse">
                [LOADING...]
              </div>
            ) : corpora.length > 0 ? (
              corpora.map((corpus) => {
                const isSelected = selectedCorpusIds.includes(corpus.corpus_id);

                return (
                  <button
                    key={corpus.corpus_id}
                    onClick={() => toggleCorpus(corpus.corpus_id)}
                    className={`flex items-center gap-2 px-2 py-1.5 text-left border transition-none rounded-none ${
                      isSelected
                        ? "bg-accent-main/10 border-accent-main text-content-primary"
                        : "border-transparent hover:bg-bg-base text-content-secondary hover:text-content-primary"
                    }`}
                  >
                    <div className="w-4 h-4 border border-border-minimal flex-shrink-0 flex items-center justify-center bg-bg-base">
                      {isSelected && (
                        <Check className="w-3 h-3 text-accent-main" />
                      )}
                    </div>
                    <div className="flex-1 overflow-hidden">
                      <div className="text-[10px] font-bold tracking-widest uppercase truncate">
                        {corpus.name}
                      </div>
                      <div className="text-[9px] text-content-tertiary">
                        DOCS: {corpus.doc_count}
                      </div>
                    </div>
                  </button>
                );
              })
            ) : (
              <div className="px-2 py-4 text-center text-[10px] text-content-tertiary uppercase tracking-widest">
                [NO_CORPORA_FOUND]
              </div>
            )}
          </div>

          <div className="px-2 py-1.5 mt-1 border-t border-border-minimal text-[8px] text-content-tertiary uppercase tracking-wider text-center">
            Leave unselected to search globally
          </div>
        </div>
      )}
    </div>
  );
}
