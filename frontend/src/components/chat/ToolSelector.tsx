import { useState, useRef, useEffect } from "react";
import { Wrench, ChevronDown, Check } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import * as api from "../../lib/api";

export function ToolSelector() {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const { availableTools, selectedToolIds, toggleTool, loadTools } =
    useSettingsStore();

  // Fetch tools on mount. Previously only the Settings → Tools editor did
  // this, so opening chat cold showed [NO_TOOLS_CREATED] even when the
  // backend had tools. Prune any stale selections whose IDs no longer exist.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const tools = await api.listTools();
        if (cancelled) return;
        loadTools(tools);
        const validIds = new Set(tools.map((t) => t.id));
        const { selectedToolIds: curSel, updateSettings } =
          useSettingsStore.getState();
        const pruned = curSel.filter((id) => id && validIds.has(id));
        if (pruned.length !== curSel.length) {
          updateSettings({ selectedToolIds: pruned });
        }
      } catch (e) {
        console.warn("Failed to load tools:", e);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadTools]);

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

  const selectedCount = selectedToolIds.length;

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-1.5 px-2 py-1 text-[10px] font-bold tracking-widest uppercase border border-transparent hover:border-border-minimal transition-none rounded-none group"
      >
        <Wrench className="w-3.5 h-3.5 text-content-tertiary group-hover:text-accent-main" />
        <span className="text-content-secondary">
          {selectedCount === 0 ? "[TOOLS: OFF]" : `[TOOLS: ${selectedCount}]`}
        </span>
        <ChevronDown className="w-3 h-3 text-content-tertiary group-hover:text-accent-main" />
      </button>

      {isOpen && (
        <div className="absolute bottom-full right-0 mb-1 w-72 max-w-[calc(100vw-1rem)] max-h-[calc(100dvh-8rem)] overflow-hidden border border-white/10 bg-[#2a2a2a] z-[60] p-1 shadow-xl rounded">
          <div className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary px-2 py-1.5 border-b border-border-minimal mb-1">
            Enable Agent Tools
          </div>
          <div className="max-h-[min(15rem,calc(100dvh-12rem))] overflow-y-auto custom-scrollbar flex flex-col gap-0.5">
            {availableTools.length > 0 ? (
              availableTools.map((tool) => (
                <button
                  key={tool.id}
                  onClick={() => toggleTool(tool.id)}
                  title={tool.description}
                  className={`flex items-center gap-2 px-2 py-1.5 text-left border transition-none rounded-none text-content-secondary hover:text-content-primary ${
                    selectedToolIds.includes(tool.id)
                      ? "bg-accent-main/10 border-accent-main"
                      : "border-transparent hover:bg-bg-base"
                  }`}
                >
                  <div className="w-4 h-4 border border-border-minimal flex-shrink-0 flex items-center justify-center bg-bg-base">
                    {selectedToolIds.includes(tool.id) && (
                      <Check className="w-3 h-3 text-accent-main" />
                    )}
                  </div>
                  <div className="flex-1 overflow-hidden">
                    <div className="text-[10px] font-bold tracking-widest uppercase truncate">
                      {tool.name}
                    </div>
                    <div className="text-[9px] text-content-tertiary truncate">
                      {tool.description}
                    </div>
                  </div>
                </button>
              ))
            ) : (
              <div className="px-2 py-4 text-center text-[10px] text-content-tertiary uppercase tracking-widest">
                [NO_TOOLS_CREATED]
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
