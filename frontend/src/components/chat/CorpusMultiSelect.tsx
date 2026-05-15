// CorpusMultiSelect.tsx - UI for selecting the active corpora (tenant scopes)
import { useState, useRef, useEffect } from "react";
import { FolderGit2, Check, ChevronDown, AlertTriangle } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import * as api from "../../lib/api";
import type { CorpusResponse } from "../../types";

// Hard cap from the backend's ChatRequest validator
// (backend/models/schemas.py — `Maximum 3 corpora per query.`).
// Graph endpoints (/query, /discover, /by-document) have no such limit.
// We show the asymmetry in the dropdown rather than hard-capping the
// component so brain/constellation/atom views can still scan N corpora.
const CHAT_CORPUS_LIMIT = 3;

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
  const exceedsChatLimit = selectedCount > CHAT_CORPUS_LIMIT;

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        data-testid="corpus-multi-select"
        onClick={() => setIsOpen(!isOpen)}
        className={
          "flex items-center gap-1.5 px-2 py-1 text-[10px] font-bold tracking-widest uppercase border transition-none rounded-none group " +
          (exceedsChatLimit
            ? "border-amber-700/60 hover:border-amber-500"
            : "border-transparent hover:border-border-minimal")
        }
        title={
          exceedsChatLimit
            ? `Chat is capped at ${CHAT_CORPUS_LIMIT} corpora — current selection (${selectedCount}) exceeds that. Graph views still work; chat requests will be rejected.`
            : "Select Target Corpora"
        }
      >
        <FolderGit2 className="w-3.5 h-3.5 text-content-tertiary group-hover:text-accent-main" />
        <span className="text-content-secondary hidden sm:inline-block">
          {selectedCount === 0
            ? "[ALL CORPORA]"
            : `[CORPORA: ${selectedCount}]`}
        </span>
        {exceedsChatLimit && (
          <AlertTriangle
            className="w-3 h-3 text-amber-500"
            aria-label="Chat cap exceeded"
          />
        )}
        <ChevronDown className="w-3 h-3 text-content-tertiary group-hover:text-accent-main" />
      </button>

      {isOpen && (
        <div className="absolute top-full right-0 mt-1 w-64 border border-white/10 bg-[#2a2a2a] z-[60] p-1 shadow-xl rounded">
          <div className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary px-2 py-1.5 border-b border-border-minimal mb-1 flex justify-between">
            <span>Corpus Scoping</span>
            {selectedCount > 0 && (
              <span
                className={
                  exceedsChatLimit ? "text-amber-400" : "text-accent-main"
                }
              >
                ({selectedCount} / {CHAT_CORPUS_LIMIT} chat)
              </span>
            )}
          </div>

          {exceedsChatLimit && (
            <div className="mx-1 mb-1 px-2 py-1.5 rounded border border-amber-700/40 bg-amber-900/20 text-[9px] tracking-wider text-amber-300 leading-snug">
              <div className="flex items-start gap-1.5">
                <AlertTriangle className="w-3 h-3 mt-0.5 flex-shrink-0" />
                <div>
                  <div className="font-bold uppercase mb-0.5">
                    Chat cap exceeded
                  </div>
                  <div className="text-amber-200/80 normal-case font-normal">
                    Chat will reject requests with &gt; {CHAT_CORPUS_LIMIT}{" "}
                    corpora. Graph views (Brain / Constellation / Atom) still
                    work without limit.
                  </div>
                </div>
              </div>
            </div>
          )}

          <div className="max-h-60 overflow-y-auto custom-scrollbar flex flex-col gap-0.5">
            {loading ? (
              <div className="px-2 py-4 text-center text-[10px] text-content-tertiary uppercase tracking-widest animate-pulse">
                [LOADING...]
              </div>
            ) : corpora.length > 0 ? (
              corpora.map((corpus) => {
                const isSelected = selectedCorpusIds.includes(corpus.corpus_id);
                // Adding this corpus would push the chat over the cap.
                // Highlight unselected rows in amber so the user sees
                // the cost of clicking BEFORE they click.
                const wouldExceedChatCap =
                  !isSelected && selectedCount >= CHAT_CORPUS_LIMIT;

                return (
                  <button
                    key={corpus.corpus_id}
                    onClick={() => toggleCorpus(corpus.corpus_id)}
                    className={`flex items-center gap-2 px-2 py-1.5 text-left border transition-none rounded-none ${
                      isSelected
                        ? exceedsChatLimit
                          ? "bg-amber-900/15 border-amber-700/50 text-content-primary"
                          : "bg-accent-main/10 border-accent-main text-content-primary"
                        : wouldExceedChatCap
                          ? "border-transparent hover:bg-amber-900/15 hover:border-amber-700/30 text-content-tertiary"
                          : "border-transparent hover:bg-bg-base text-content-secondary hover:text-content-primary"
                    }`}
                    title={
                      wouldExceedChatCap
                        ? `Adding this would put you at ${selectedCount + 1} corpora — chat caps at ${CHAT_CORPUS_LIMIT}. Graph views are unaffected.`
                        : undefined
                    }
                  >
                    <div
                      className={`w-4 h-4 border flex-shrink-0 flex items-center justify-center bg-bg-base ${
                        wouldExceedChatCap
                          ? "border-amber-700/40"
                          : "border-border-minimal"
                      }`}
                    >
                      {isSelected && (
                        <Check
                          className={
                            exceedsChatLimit
                              ? "w-3 h-3 text-amber-400"
                              : "w-3 h-3 text-accent-main"
                          }
                        />
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
            {selectedCount === 0
              ? "Leave unselected to search globally"
              : `Chat caps at ${CHAT_CORPUS_LIMIT} · graph views unlimited`}
          </div>
        </div>
      )}
    </div>
  );
}
