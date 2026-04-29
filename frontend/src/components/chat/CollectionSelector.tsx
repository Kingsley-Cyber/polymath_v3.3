// CollectionSelector.tsx - Multi-select dropdown for knowledge collections
import { useState, useEffect, useRef } from "react";
import { Database, Check, ChevronDown, X } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import type { Collection } from "../../types";

interface CollectionSelectorProps {
  collections?: Collection[];
  onChange?: (selectedIds: string[]) => void;
}

export function CollectionSelector({
  collections = [],
  onChange,
}: CollectionSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [localCollections, setLocalCollections] =
    useState<Collection[]>(collections);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const {
    selectedCollectionIds,
    toggleCollection,
    selectAllCollections,
    clearCollections,
  } = useSettingsStore();

  // Close dropdown when clicking outside
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

  // Sync with parent collections prop
  useEffect(() => {
    if (collections.length > 0) {
      setLocalCollections(collections);
    }
  }, [collections]);

  // Notify parent of changes
  useEffect(() => {
    onChange?.(selectedCollectionIds);
  }, [selectedCollectionIds, onChange]);

  const selectedCount = selectedCollectionIds.length;
  const totalCount = localCollections.length;

  const getSelectionLabel = () => {
    if (selectedCount === 0) return "ALL_COLLS";
    if (selectedCount === 1) {
      const collection = localCollections.find(
        (c) => c.id === selectedCollectionIds[0],
      );
      return collection?.name || "1_SEL";
    }
    if (selectedCount === totalCount) return "ALL_SEL";
    return `${selectedCount}_SEL`;
  };

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        data-testid="collection-selector"
        onClick={() => setIsOpen(!isOpen)}
        className={`
          flex items-center gap-2 px-2 py-1 text-[10px] font-bold tracking-widest uppercase
          transition-none border rounded-none
          ${selectedCount > 0
            ? "bg-accent-main/25 text-accent-main border-accent-main"
            : "bg-bg-surface text-content-secondary border-border-minimal hover:border-accent-main hover:text-content-primary"
          }
        `}
        title="Select knowledge collections to query"
      >
        <Database className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">{getSelectionLabel()}</span>
        <span className="sm:hidden">
          {selectedCount > 0 ? `${selectedCount}` : "ALL"}
        </span>
        <ChevronDown
          className={`w-3.5 h-3.5 transition-transform duration-150 ${isOpen ? "rotate-180" : ""
            }`}
        />
      </button>

      {isOpen && (
        <div className="absolute top-full z-[60] mt-1 w-64 border border-white/10 bg-[#2a2a2a] animate-fade-in font-mono shadow-xl rounded">
          {/* Header */}
          <div className="flex items-center justify-between px-3 py-2 border-b border-border-minimal bg-bg-base">
            <span className="text-[10px] font-bold tracking-widest uppercase text-content-primary">
              Sources
            </span>
            <div className="flex items-center gap-1">
              <button
                onClick={() =>
                  selectAllCollections(localCollections.map((c) => c.id))
                }
                className="px-1.5 py-0.5 text-[9px] font-bold tracking-widest uppercase text-accent-main border border-transparent hover:border-accent-main transition-none rounded-none"
              >
                [ALL]
              </button>
              <button
                onClick={clearCollections}
                className="px-1.5 py-0.5 text-[9px] font-bold tracking-widest uppercase text-content-tertiary border border-transparent hover:border-content-tertiary hover:text-content-secondary transition-none rounded-none"
              >
                [NONE]
              </button>
            </div>
          </div>

          {/* Collection List */}
          <div className="max-h-60 overflow-y-auto custom-scrollbar">
            {localCollections.length === 0 ? (
              <div className="px-3 py-4 text-center text-[10px] tracking-widest uppercase text-content-tertiary">
                [EMPTY_DIR]
              </div>
            ) : (
              localCollections.map((collection) => {
                const isSelected = selectedCollectionIds.includes(
                  collection.id,
                );
                return (
                  <button
                    key={collection.id}
                    onClick={() => toggleCollection(collection.id)}
                    className={`
                      w-full flex items-center gap-2 px-3 py-2 text-left
                      transition-none border-l-2
                      ${isSelected ? "bg-bg-base border-accent-main" : "border-transparent hover:bg-bg-base hover:border-content-tertiary"}
                    `}
                  >
                    <div
                      className={`
                        flex-shrink-0 w-3.5 h-3.5 border flex items-center justify-center transition-none rounded-none
                        ${isSelected
                          ? "bg-accent-main border-accent-main"
                          : "border-border-minimal bg-bg-base"
                        }
                      `}
                    >
                      {isSelected && <Check className="w-3 h-3 text-bg-base" />}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-[11px] font-bold text-content-primary truncate uppercase tracking-tight">
                        {collection.name}
                      </div>
                    </div>
                    <span className="text-[9px] font-bold tracking-widest text-content-tertiary">
                      [{collection.document_count}]
                    </span>
                  </button>
                );
              })
            )}
          </div>

          {/* Footer */}
          {selectedCount > 0 && (
            <div className="flex items-center justify-between px-3 py-1.5 border-t border-border-minimal bg-bg-base">
              <span className="text-[9px] font-bold tracking-widest uppercase text-content-secondary">
                SELECTED: {selectedCount}/{totalCount}
              </span>
              <button
                onClick={clearCollections}
                className="flex items-center gap-1 text-[9px] font-bold tracking-widest uppercase text-error hover:text-error/80 transition-none"
              >
                <X className="w-3 h-3" />
                CLR
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
