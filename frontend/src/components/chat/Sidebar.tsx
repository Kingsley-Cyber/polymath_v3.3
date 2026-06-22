// Sidebar.tsx - Obsidian File Explorer / Deterministic Style
import { useState, useEffect } from "react";
import {
  Plus,
  Trash2,
  X,
  Search,
  FolderOpen,
  FileCode2,
  Settings,
  Settings2,
  ChevronRight,
  ChevronDown,
  Database,
  CheckSquare,
  Square,
  Share2,
} from "lucide-react";
import { useChatStore } from "../../stores/chatStore";
import { useSettingsStore } from "../../stores/settingsStore";
import * as api from "../../lib/api";
import { CorpusManager } from "../corpus/CorpusManager";
import type { Theme } from "../../types";

/**
 * UI Protocol theme picker entries — single source of truth for the sidebar.
 * `accent` controls which accent token highlights the active button (so
 * Gruvbox/Solar pop in their secondary color while the dark themes use main).
 */
const THEMES: { id: Theme; label: string; accent: "main" | "secondary" }[] = [
  { id: "ayu-mirage",  label: "Obsidian Graph",  accent: "main" },
  { id: "gruvbox",     label: "Polymath Onto.",  accent: "secondary" },
  { id: "serendipity", label: "Deterministic",   accent: "main" },
  { id: "nord",        label: "Arctic Ice",      accent: "main" },
  { id: "dracula",     label: "Neon Spectral",   accent: "main" },
  { id: "solar",       label: "Solar Focus",     accent: "secondary" },
  { id: "claude",      label: "???",             accent: "main" },
];

interface SidebarProps {
  isOpen: boolean;
  onToggle: () => void;
}

export function Sidebar({ isOpen, onToggle }: SidebarProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isCreatingChat, setIsCreatingChat] = useState(false);
  const [folderOpen, setFolderOpen] = useState(true);
  const [isCorpusManagerOpen, setIsCorpusManagerOpen] = useState(false);
  // Mass-delete selection mode
  const [isSelectMode, setIsSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [isBulkDeleting, setIsBulkDeleting] = useState(false);

  const {
    conversations,
    setConversations,
    activeConversationId,
    setActiveConversation,
    addConversation,
    deleteConversation,
    setMessages,
  } = useChatStore();

  const { theme, setTheme } = useSettingsStore();

  useEffect(() => {
    loadConversations();
  }, []);

  const loadConversations = async () => {
    setIsLoading(true);
    try {
      const data = await api.listConversations();
      setConversations(data as import("../../types").Conversation[]);
    } catch (error) {
      console.error("Failed to load conversations:", error);
    } finally {
      setIsLoading(false);
    }
  };

  const handleNewChat = async () => {
    if (isCreatingChat) return;
    setIsCreatingChat(true);
    try {
      const { id } = await api.createConversation({
        title: "untitled_node.md",
      });
      const newConversation = {
        id,
        title: "untitled_node.md",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        message_count: 0,
      };
      addConversation(newConversation as import("../../types").Conversation);
      setActiveConversation(id);
    } catch (error) {
      console.error("Failed to create conversation:", error);
    } finally {
      setIsCreatingChat(false);
    }
  };

  const handleDelete = async (e: React.MouseEvent, id: string) => {
    e.preventDefault();
    e.stopPropagation();

    // Prevent delete if this is the currently active conversation being viewed
    if (activeConversationId === id) {
      if (
        !confirm("WARN: This conversation is currently active. Delete anyway?")
      )
        return;
    } else {
      if (!confirm("WARN: Execute rm -rf on this node?")) return;
    }

    try {
      await api.deleteConversation(id);
      deleteConversation(id);
    } catch (error) {
      console.error("Failed to delete:", error);
    }
  };

  const handleSelectConversation = async (id: string) => {
    // Don't reload if already active
    if (activeConversationId === id) return;

    setActiveConversation(id);
    try {
      // Phase 24 fix: the legacy `/conversations/:id/messages` endpoint never
      // existed on the backend (every refresh produced 404 → empty history).
      // The canonical `/conversations/:id` already returns messages embedded.
      const conv = await api.getConversation(id);
      setMessages(id, conv.messages || []);
    } catch (error) {
      console.error("Failed to load conversation:", error);
    }
  };

  const activateConversationRow = (id: string) => {
    if (isSelectMode) {
      toggleSelected(id);
      return;
    }
    void handleSelectConversation(id);
  };

  const handleConversationKeyDown = (
    e: React.KeyboardEvent<HTMLDivElement>,
    id: string,
  ) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    activateConversationRow(id);
  };

  const toggleSelectMode = () => {
    setIsSelectMode((prev) => {
      const next = !prev;
      if (!next) setSelectedIds(new Set());
      return next;
    });
  };

  const toggleSelected = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAll = () => {
    setSelectedIds(new Set(filteredConversations.map((c) => c.id)));
  };

  const clearSelection = () => setSelectedIds(new Set());

  const handleMassDelete = async () => {
    if (selectedIds.size === 0) return;
    if (!confirm(`WARN: Execute rm -rf on ${selectedIds.size} node(s)?`))
      return;
    setIsBulkDeleting(true);
    try {
      const ids = Array.from(selectedIds);
      // Parallel deletes — small N, network-bound
      const results = await Promise.allSettled(
        ids.map((id) => api.deleteConversation(id)),
      );
      results.forEach((r, i) => {
        if (r.status === "fulfilled") {
          deleteConversation(ids[i]);
        } else {
          console.error(`Failed to delete ${ids[i]}:`, r.reason);
        }
      });
      setSelectedIds(new Set());
      setIsSelectMode(false);
    } finally {
      setIsBulkDeleting(false);
    }
  };

  const filteredConversations = conversations.filter((conv) =>
    conv.title.toLowerCase().includes(searchQuery.toLowerCase()),
  );

  const formatTechDate = (dateString: string) => {
    const date = new Date(dateString);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  };

  return (
    <>
      {isOpen && (
        // Mobile backdrop. /80 alpha + backdrop-blur so the page behind
        // dims visibly when the sidebar is open — without the dim, the
        // sidebar reads as semi-transparent because the page content
        // bleeds through at the edge of the viewport on some themes.
        <div
          className="fixed inset-0 bg-bg-base/80 backdrop-blur-sm z-40 lg:hidden"
          onClick={onToggle}
        />
      )}
      <aside
        // bg-bg-base is solid in every theme (#0f0f11 / #141416 / etc.).
        // The shadow-xl + active-state ring make the panel visually
        // distinct from the canvas behind it when the sidebar is open,
        // so it reads as opaque even on themes where bg-base is close
        // to the page-content color.
        className={`fixed lg:static inset-y-0 left-0 z-50 w-[min(20rem,calc(100vw-1rem))] sm:w-72 lg:w-64 lg:max-w-64 bg-bg-base border-r border-border-minimal flex flex-col transition-transform duration-150 select-none shadow-xl lg:shadow-none will-change-transform touch-manipulation ${
          isOpen
            ? "translate-x-0 ring-1 ring-accent-main/10 lg:ring-0"
            : "-translate-x-full lg:translate-x-0"
        }`}
      >
        {/* Header / Vault Title */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border-minimal shrink-0 bg-bg-surface">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded-full bg-[#1c4e80] flex items-center justify-center border border-[#2a6baf] shrink-0">
              <Share2 className="w-3 h-3 text-white" />
            </div>
            <div className="flex flex-col leading-none">
              <span className="text-content-primary text-[13px] font-semibold tracking-wide">Polymath</span>
              <span className="text-content-tertiary text-[8px] font-mono tracking-widest uppercase mt-0.5">Knowledge Graph</span>
            </div>
          </div>
          <div className="flex items-center gap-1">
            <button
              data-testid="sidebar-db-btn"
              onClick={() => setIsCorpusManagerOpen(true)}
              className="p-1 text-content-tertiary hover:text-accent-main transition-none"
              title="Corpus Manager"
            >
              <Database className="w-4 h-4" />
            </button>
            <button
              onClick={() =>
                window.dispatchEvent(new CustomEvent("open-settings"))
              }
              className="p-1 text-content-tertiary hover:text-accent-main transition-none"
              title="Settings"
            >
              <Settings className="w-4 h-4" />
            </button>
            <button
              onClick={onToggle}
              className="lg:hidden p-1 text-content-tertiary hover:text-accent-main transition-none"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Action: New Node */}
        <div className="p-2 border-b border-border-minimal shrink-0">
          <button
            type="button"
            onClick={handleNewChat}
            disabled={isCreatingChat}
            aria-label="Create new chat"
            title="Create new chat"
            className="group w-full min-h-11 rounded-[6px] border border-accent-main/40 bg-accent-main/10 px-2.5 py-2 text-left text-accent-main shadow-[inset_0_0_0_1px_rgba(255,255,255,0.02)] transition-colors duration-150 hover:border-accent-main hover:bg-accent-main hover:text-bg-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-main/60 focus-visible:ring-offset-2 focus-visible:ring-offset-bg-base active:scale-[0.99] disabled:cursor-wait disabled:opacity-70 disabled:hover:bg-accent-main/10 disabled:hover:text-accent-main touch-manipulation"
          >
            <span className="pointer-events-none flex items-center gap-2">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[5px] border border-current/35 bg-bg-base/45 transition-colors group-hover:bg-bg-base/15">
                <Plus className="h-4 w-4" />
              </span>
              <span className="flex min-w-0 flex-col">
                <span className="text-[12px] font-black uppercase leading-tight tracking-[0.14em]">
                  {isCreatingChat ? "Creating..." : "New Chat"}
                </span>
                <span className="font-mono text-[9px] uppercase tracking-wider opacity-75">
                  Blank conversation
                </span>
              </span>
            </span>
          </button>
        </div>

        {/* Search / Filter */}
        <div className="px-2 py-2 border-b border-border-minimal shrink-0 bg-bg-base relative">
          <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-3 h-3 text-content-tertiary" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder='grep -i "query"'
            className="w-full pl-7 pr-2 py-1 bg-bg-surface border border-border-minimal text-[11px] text-content-primary placeholder:text-content-tertiary focus:outline-none focus:border-accent-main transition-none rounded-none font-mono"
          />
        </div>

        {/* File Explorer Tree */}
        <div className="flex-1 overflow-y-auto custom-scrollbar py-2">
          {/* Root Folder structure simulation */}
          <div className="px-2">
            <div className="flex items-center justify-between gap-1">
              <button
                onClick={() => setFolderOpen(!folderOpen)}
                className="flex-1 flex items-center gap-1.5 px-1 py-1 text-left text-[11px] text-content-secondary hover:text-content-primary transition-none uppercase font-bold tracking-wider"
              >
                {folderOpen ? (
                  <ChevronDown className="w-3 h-3" />
                ) : (
                  <ChevronRight className="w-3 h-3" />
                )}
                <FolderOpen className="w-3.5 h-3.5 text-accent-secondary" />
                <span>conversations/</span>
              </button>
              {filteredConversations.length > 0 && (
                <button
                  onClick={toggleSelectMode}
                  title={isSelectMode ? "Cancel selection" : "Select multiple"}
                  className={`px-1.5 py-1 text-[9px] font-bold tracking-widest uppercase border transition-none ${
                    isSelectMode
                      ? "border-accent-main text-bg-base bg-accent-main"
                      : "border-border-minimal text-content-secondary hover:text-bg-base hover:bg-accent-main hover:border-accent-main"
                  }`}
                >
                  {isSelectMode ? "Cancel" : "Select"}
                </button>
              )}
            </div>

            {/* Mass-delete toolbar */}
            {isSelectMode && (
              <div className="flex items-center justify-between gap-1 mt-1 px-1 py-1 border border-border-minimal bg-bg-surface">
                <div className="text-[10px] font-bold tracking-widest text-content-secondary uppercase">
                  {selectedIds.size}/{filteredConversations.length} selected
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={
                      selectedIds.size === filteredConversations.length
                        ? clearSelection
                        : selectAll
                    }
                    className="px-1.5 py-0.5 text-[9px] font-bold tracking-widest uppercase text-content-tertiary hover:text-accent-secondary"
                    title="Toggle select all"
                  >
                    {selectedIds.size === filteredConversations.length
                      ? "None"
                      : "All"}
                  </button>
                  <button
                    onClick={handleMassDelete}
                    disabled={selectedIds.size === 0 || isBulkDeleting}
                    className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest uppercase border border-accent-primary text-accent-primary hover:bg-accent-primary hover:text-bg-base disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-accent-primary"
                  >
                    <Trash2 className="w-3 h-3" />
                    {isBulkDeleting ? "..." : `Del ${selectedIds.size}`}
                  </button>
                </div>
              </div>
            )}

            {/* Folder Contents */}
            {folderOpen && (
              <div className="mt-0.5 ml-3 pl-2 border-l border-border-minimal space-y-[1px]">
                {isLoading ? (
                  <div className="py-2 text-[10px] text-content-tertiary pl-4">
                    [LOADING_NODES...]
                  </div>
                ) : filteredConversations.length === 0 ? (
                  <div className="py-2 text-[10px] text-content-tertiary pl-4">
                    [EMPTY_DIR]
                  </div>
                ) : (
                  filteredConversations.map((conversation) => {
                    const isActive = activeConversationId === conversation.id;
                    const isSelected = selectedIds.has(conversation.id);
                    return (
                      <div
                        key={conversation.id}
                        role="button"
                        tabIndex={0}
                        onClick={() => activateConversationRow(conversation.id)}
                        onKeyDown={(e) =>
                          handleConversationKeyDown(e, conversation.id)
                        }
                        className={`w-full flex flex-col gap-0.5 px-2 py-1.5 text-left transition-colors duration-100 group border-l-4 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-main/50 focus-visible:ring-offset-1 focus-visible:ring-offset-bg-base ${
                          isSelectMode && isSelected
                            ? "bg-accent-main border-accent-secondary text-bg-base font-bold"
                            : isActive
                              ? "bg-bg-raised border-accent-main text-content-primary"
                              : "border-transparent text-content-secondary hover:bg-bg-surface hover:text-content-primary"
                        }`}
                      >
                        <div className="flex items-center gap-1.5 w-full">
                          {isSelectMode ? (
                            isSelected ? (
                              <CheckSquare className="w-3.5 h-3.5 shrink-0 text-bg-base" />
                            ) : (
                              <Square className="w-3.5 h-3.5 shrink-0 text-content-secondary" />
                            )
                          ) : (
                            <FileCode2
                              className={`w-3 h-3 shrink-0 ${isActive ? "text-accent-main" : "text-content-tertiary group-hover:text-content-secondary"}`}
                            />
                          )}
                          <span className="text-[11px] truncate flex-1 leading-none tracking-tight">
                            {conversation.title
                              .toLowerCase()
                              .replace(/\s+/g, "_") + ".md"}
                          </span>
                          {!isSelectMode && (
                            <button
                              type="button"
                              onClick={(e) => handleDelete(e, conversation.id)}
                              onMouseDown={(e) => e.stopPropagation()} // Prevent focus/click events
                              title="Delete conversation"
                              className="shrink-0 p-0.5 text-content-tertiary hover:text-error hover:bg-error/10 rounded transition-colors"
                            >
                              <Trash2 className="w-3 h-3" />
                            </button>
                          )}
                        </div>
                        <div
                          className={`pl-4.5 text-[9px] tracking-widest ${
                            isSelectMode && isSelected
                              ? "text-bg-base font-bold"
                              : "text-content-tertiary"
                          }`}
                        >
                          {formatTechDate(conversation.updated_at)}
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            )}
          </div>
        </div>

        {/* Footer / Theme Protocol Selector */}
        <div className="p-3 border-t border-border-minimal mt-auto bg-bg-surface shrink-0">
          <div className="flex items-center gap-2 mb-2 text-[10px] font-bold text-content-secondary uppercase tracking-widest">
            <Settings2 className="w-3.5 h-3.5" />
            UI Protocol
          </div>
          <div className="flex flex-col gap-1 max-h-[160px] overflow-y-auto custom-scrollbar pr-1">
            {THEMES.map((t, i) => {
              const active = theme === t.id;
              const accentClasses = active
                ? t.accent === "secondary"
                  ? "border-accent-secondary text-accent-secondary bg-accent-secondary/10"
                  : "border-accent-main text-accent-main bg-accent-main/10"
                : "border-border-minimal text-content-tertiary hover:border-content-secondary";
              return (
                <button
                  key={t.id}
                  onClick={() => setTheme(t.id)}
                  className={`text-left px-2 py-1 text-[10px] font-bold tracking-widest border transition-none uppercase ${accentClasses}`}
                >
                  [{i + 1}] {t.label}
                </button>
              );
            })}
          </div>
        </div>
      </aside>

      <CorpusManager
        isOpen={isCorpusManagerOpen}
        onClose={() => setIsCorpusManagerOpen(false)}
      />
    </>
  );
}
