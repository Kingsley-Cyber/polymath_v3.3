// Phase 24 — ActivatorSelector
// Single dropdown in the ToggleBar that activates BOTH skills and tools.
// Two-tab UI (TOOLS / SKILLS) keeps the surface clean as the catalog grows.
// Same store actions used by the slash popover in ChatInput, so the two
// surfaces (browse vs slash) stay in sync.
import { useState, useEffect, useRef } from "react";
import { Wrench, Sparkles, ChevronDown, Check } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import * as api from "../../lib/api";

type Tab = "tools" | "skills";

export function ActivatorSelector() {
  const [isOpen, setIsOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("tools");
  const dropdownRef = useRef<HTMLDivElement>(null);

  const {
    availableTools,
    selectedToolIds,
    toggleTool,
    availableSkills,
    selectedSkillIds,
    toggleSkill,
    loadTools,
    loadSkills,
  } = useSettingsStore();

  // Fetch both on mount + prune stale ids (defensive — same pattern as ToolSelector).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [tools, skills] = await Promise.all([
          api.listTools(),
          api.listSkills(),
        ]);
        if (cancelled) return;
        loadTools(tools);
        loadSkills(skills);
        const validToolIds = new Set(tools.map((t) => t.id));
        const validSkillIds = new Set(skills.map((s) => s.id));
        const { selectedToolIds: curT, selectedSkillIds: curS, updateSettings } =
          useSettingsStore.getState();
        const prunedT = curT.filter((id) => id && validToolIds.has(id));
        const prunedS = curS.filter((id) => id && validSkillIds.has(id));
        if (
          prunedT.length !== curT.length ||
          prunedS.length !== curS.length
        ) {
          updateSettings({ selectedToolIds: prunedT, selectedSkillIds: prunedS });
        }
      } catch (e) {
        console.warn("Failed to load tools/skills:", e);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadTools, loadSkills]);

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

  const totalActive = selectedToolIds.length + selectedSkillIds.length;

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-1.5 px-2 py-1 text-[10px] font-bold tracking-widest uppercase border border-transparent hover:border-border-minimal transition-none rounded-none group"
        title="Activate skills + tools for the next send"
      >
        <Sparkles className="w-3.5 h-3.5 text-content-tertiary group-hover:text-accent-main" />
        <span className="text-content-secondary">
          [ITEMS: {totalActive}]
        </span>
        <ChevronDown className="w-3 h-3 text-content-tertiary group-hover:text-accent-main" />
      </button>

      {isOpen && (
        // `bottom-full` pops the panel ABOVE the trigger so it stays visible
        // when ChatInput is at the bottom of the viewport. With `top-full`
        // the dropdown opened DOWNWARD and got clipped by the page edge.
        <div className="fixed left-2 right-2 bottom-36 z-[100] w-auto max-h-[calc(100dvh-11rem)] overflow-hidden border border-white/10 bg-[#2a2a2a] shadow-xl rounded sm:absolute sm:left-auto sm:right-0 sm:bottom-full sm:mb-1 sm:w-72 sm:max-w-[calc(100vw-1rem)] sm:max-h-[calc(100dvh-8rem)]">
          {/* Tabs */}
          <div className="flex border-b border-border-minimal">
            <button
              onClick={() => setTab("tools")}
              className={`flex-1 flex items-center justify-center gap-1.5 px-2 py-2 text-[10px] font-bold tracking-widest uppercase transition-none ${
                tab === "tools"
                  ? "text-accent-main border-b-2 border-accent-main bg-bg-base/40"
                  : "text-content-tertiary hover:text-content-secondary"
              }`}
            >
              <Wrench className="w-3 h-3" />
              Tools ({selectedToolIds.length})
            </button>
            <button
              onClick={() => setTab("skills")}
              className={`flex-1 flex items-center justify-center gap-1.5 px-2 py-2 text-[10px] font-bold tracking-widest uppercase transition-none ${
                tab === "skills"
                  ? "text-accent-main border-b-2 border-accent-main bg-bg-base/40"
                  : "text-content-tertiary hover:text-content-secondary"
              }`}
            >
              <Sparkles className="w-3 h-3" />
              Skills ({selectedSkillIds.length})
            </button>
          </div>

          {/* List */}
          <div className="max-h-[min(18rem,calc(100dvh-12rem))] overflow-y-auto custom-scrollbar p-1">
            {tab === "tools" && (
              availableTools.length > 0 ? (
                availableTools.map((tool) => {
                  const selected = selectedToolIds.includes(tool.id);
                  return (
                    <button
                      key={tool.id}
                      onClick={() => toggleTool(tool.id)}
                      className={`w-full flex items-start gap-2 px-2 py-1.5 text-left border transition-none rounded-none text-content-secondary hover:text-content-primary ${
                        selected
                          ? "bg-accent-main/10 border-accent-main"
                          : "border-transparent hover:bg-bg-base"
                      }`}
                    >
                      <div className="w-4 h-4 border border-border-minimal flex-shrink-0 flex items-center justify-center bg-bg-base mt-0.5">
                        {selected && (
                          <Check className="w-3 h-3 text-accent-main" />
                        )}
                      </div>
                      <div className="flex-1 overflow-hidden">
                        <div className="flex items-center gap-2">
                          <div className="text-[10px] font-bold tracking-widest uppercase truncate">
                            {tool.name}
                          </div>
                          {tool.slash_command && (
                            <span className="text-[9px] font-mono text-accent-main shrink-0">
                              {tool.slash_command}
                            </span>
                          )}
                        </div>
                        <div className="text-[9px] text-content-tertiary truncate">
                          {tool.description}
                        </div>
                      </div>
                    </button>
                  );
                })
              ) : (
                <div className="px-3 py-6 text-center text-[10px] tracking-widest uppercase text-content-tertiary">
                  [NO_TOOLS_CREATED]
                </div>
              )
            )}

            {tab === "skills" && (
              availableSkills.length > 0 ? (
                availableSkills.map((skill) => {
                  const selected = selectedSkillIds.includes(skill.id);
                  return (
                    <button
                      key={skill.id}
                      onClick={() => toggleSkill(skill.id)}
                      className={`w-full flex items-start gap-2 px-2 py-1.5 text-left border transition-none rounded-none text-content-secondary hover:text-content-primary ${
                        selected
                          ? "bg-accent-main/10 border-accent-main"
                          : "border-transparent hover:bg-bg-base"
                      }`}
                    >
                      <div className="w-4 h-4 border border-border-minimal flex-shrink-0 flex items-center justify-center bg-bg-base mt-0.5">
                        {selected && (
                          <Check className="w-3 h-3 text-accent-main" />
                        )}
                      </div>
                      <div className="flex-1 overflow-hidden">
                        <div className="flex items-center gap-2">
                          <div className="text-[10px] font-bold tracking-widest uppercase truncate">
                            {skill.name}
                          </div>
                          {skill.slash_command && (
                            <span className="text-[9px] font-mono text-accent-main shrink-0">
                              {skill.slash_command}
                            </span>
                          )}
                        </div>
                        <div className="text-[9px] text-content-tertiary truncate">
                          {skill.description}
                        </div>
                      </div>
                    </button>
                  );
                })
              ) : (
                <div className="px-3 py-6 text-center text-[10px] tracking-widest uppercase text-content-tertiary">
                  [NO_SKILLS_CREATED]
                </div>
              )
            )}
          </div>

          <div className="text-[9px] text-content-tertiary px-2 py-1.5 border-t border-border-minimal text-center leading-snug">
            Or type <span className="text-accent-main">/</span> in the chat to
            activate by command.
          </div>
        </div>
      )}
    </div>
  );
}
