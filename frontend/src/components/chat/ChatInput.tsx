// ChatInput.tsx - Deterministic Protocol Input Interface
import {
  useState,
  useRef,
  useCallback,
  useEffect,
  type ChangeEvent,
  type KeyboardEvent,
  type DragEvent,
} from "react";
import {
  Paperclip,
  CornerDownLeft,
  X,
  Wrench,
  Sparkles,
  SlidersHorizontal,
} from "lucide-react";
import { ToggleBar } from "./ToggleBar";
import { ModelSelector } from "./ModelSelector";
import { ThinkingEffortSelector } from "./ThinkingEffortSelector";
import { FileAttachment } from "./FileAttachment";
import { useChatStore } from "../../stores/chatStore";
import { useSettingsStore } from "../../stores/settingsStore";
import { useQueryModelPoolStore } from "../../stores/queryModelPoolStore";
import {
  supportsVision,
  visionCapableModelsHint,
} from "../../lib/modelCapabilities";

/** Recursively walks a FileSystemEntry tree collecting every File it finds.
 *  Needed because `dataTransfer.files` is flat — dropping a folder yields
 *  only top-level entries, not the subdirectory contents. */
async function walkEntry(entry: FileSystemEntry, out: File[]): Promise<void> {
  if (entry.isFile) {
    return new Promise((resolve) => {
      (entry as FileSystemFileEntry).file(
        (file) => {
          out.push(file);
          resolve();
        },
        () => resolve(),
      );
    });
  }
  if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    // readEntries returns up to ~100 at a time — loop until empty.
    const children: FileSystemEntry[] = [];
    const readBatch = (): Promise<void> =>
      new Promise((resolve) => {
        reader.readEntries(
          (batch) => {
            if (batch.length === 0) {
              resolve();
            } else {
              children.push(...batch);
              readBatch().then(resolve);
            }
          },
          () => resolve(),
        );
      });
    await readBatch();
    await Promise.all(children.map((child) => walkEntry(child, out)));
  }
}

interface ChatInputProps {
  onSend: (message: string, attachments?: File[]) => void;
  isLoading?: boolean;
  placeholder?: string;
  tokenCount?: { current: number; max: number };
  /** Pt 7: when the parent wants to seed the input with text (e.g. the
   *  Graph Query tab's "send to chat" callback), it bumps this value.
   *  ChatInput watches it and replaces its internal state on change.
   *  Each call should use a fresh object so equality changes even when
   *  the text is identical to the last prefill — { text, nonce }. */
  prefill?: { text: string; nonce: number };
}

function StatusTag({
  tag,
  tone,
}: {
  tag: string;
  tone: "gen" | "use" | "wrn" | "inf";
}) {
  return (
    <span className={`status-badge status-badge-${tone}`}>
      {`<${tag}>`}
    </span>
  );
}

export function ChatInput({
  onSend,
  isLoading = false,
  placeholder = "EXECUTE QUERY // INJECT CONTEXT...",
  tokenCount,
  prefill,
}: ChatInputProps) {
  const tokensUsed = useChatStore((s) => s.tokensUsed);
  const tokensMax = useChatStore((s) => s.tokensMax);

  // Prefer explicit prop; otherwise fall back to SSE-driven store telemetry.
  const effectiveTokenCount =
    tokenCount ??
    (tokensUsed !== null && tokensMax !== null
      ? { current: tokensUsed, max: tokensMax }
      : undefined);

  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);

  // Phase 29 — vision-model guardrail. If the user has attached at least
  // one image and the selected model can't process images, we surface a
  // warning right above the input so they catch the mismatch BEFORE
  // hitting send. The backend pre-flight is the source of truth (it
  // emits an SSE error), but the UX is much nicer when we catch it
  // client-side. Resolves pool:<id> references through the pool store
  // — the heuristic needs the raw model_name to pattern-match.
  const selectedModelRaw = useSettingsStore((s) => s.selectedModel);
  const queryPool = useQueryModelPoolStore((s) => s.config.query_model_pool);
  const resolvedModelName = (() => {
    if (!selectedModelRaw) return "";
    if (selectedModelRaw.startsWith("pool:")) {
      const id = selectedModelRaw.slice("pool:".length);
      return queryPool.find((e) => e.entry_id === id)?.model_name ?? "";
    }
    if (selectedModelRaw.startsWith("profile:")) return "";
    return selectedModelRaw;
  })();
  const hasImageAttachment = attachments.some(
    (f) => (f.type || "").toLowerCase().startsWith("image/"),
  );
  const visionMismatch =
    hasImageAttachment && !supportsVision(resolvedModelName);

  // Pt 7: parent-driven prefill. Bumping prefill.nonce on the parent
  // replaces the input with the new text. Focuses the textarea so the
  // user can edit/extend the suggestion before sending.
  useEffect(() => {
    if (!prefill) return;
    setInput(prefill.text);
    // Defer focus to after React commits the new value.
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (ta) {
        ta.focus();
        const end = ta.value.length;
        ta.setSelectionRange(end, end);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- nonce IS the trigger
  }, [prefill?.nonce]);
  const [isDragging, setIsDragging] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const optionsRef = useRef<HTMLDivElement>(null);

  // Phase 24 — slash command popover state.
  // Detects `/<query>` token at the end of the input (typed by user) and
  // surfaces matching skills + tools. Picking one toggles its active state
  // and strips the slash token from the input.
  const {
    availableTools,
    availableSkills,
    selectedToolIds,
    selectedSkillIds,
    hydeEnabled,
    webSearchEnabled,
    reasoningCascadeEnabled,
    toggleTool,
    toggleSkill,
  } = useSettingsStore();

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (
        optionsRef.current &&
        !optionsRef.current.contains(event.target as Node)
      ) {
        setOptionsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  type SlashItem = {
    kind: "tool" | "skill";
    id: string;
    name: string;
    description: string;
    slash: string;
  };

  const slashCatalog: SlashItem[] = [
    ...availableTools
      .filter((t) => t.slash_command)
      .map((t) => ({
        kind: "tool" as const,
        id: t.id,
        name: t.name,
        description: t.description,
        slash: t.slash_command as string,
      })),
    ...availableSkills
      .filter((s) => s.slash_command)
      .map((s) => ({
        kind: "skill" as const,
        id: s.id,
        name: s.name,
        description: s.description,
        slash: s.slash_command as string,
      })),
  ];

  // Detect /token at end of input (after whitespace or at start)
  const slashMatch = input.match(/(?:^|\s)(\/[a-z0-9_-]*)$/i);
  const slashQuery = slashMatch ? slashMatch[1].toLowerCase() : null;
  const slashOpen = slashQuery !== null;
  const slashFiltered = slashOpen
    ? slashCatalog.filter((item) =>
        item.slash.toLowerCase().startsWith(slashQuery as string),
      )
    : [];
  const [slashCursor, setSlashCursor] = useState(0);
  useEffect(() => {
    setSlashCursor(0);
  }, [slashQuery]);

  const activateSlashItem = useCallback(
    (item: SlashItem) => {
      // Strip the slash token from the input
      setInput((prev) =>
        prev.replace(/(?:^|\s)(\/[a-z0-9_-]*)$/i, (m) =>
          m.startsWith(" ") ? " " : "",
        ),
      );
      // Toggle activation
      if (item.kind === "tool") toggleTool(item.id);
      else toggleSkill(item.id);
      textareaRef.current?.focus();
    },
    [toggleTool, toggleSkill],
  );

  // Active chips (above textarea)
  const activeChips: { kind: "tool" | "skill"; id: string; name: string; slash?: string }[] = [
    ...selectedToolIds
      .map((id) => availableTools.find((t) => t.id === id))
      .filter(Boolean)
      .map((t) => ({ kind: "tool" as const, id: t!.id, name: t!.name, slash: t!.slash_command || undefined })),
    ...selectedSkillIds
      .map((id) => availableSkills.find((s) => s.id === id))
      .filter(Boolean)
      .map((s) => ({ kind: "skill" as const, id: s!.id, name: s!.name, slash: s!.slash_command || undefined })),
  ];

  const activeModeChips = [
    hydeEnabled ? "HyDE" : null,
    reasoningCascadeEnabled ? "Reason" : null,
    webSearchEnabled ? "Web" : null,
  ].filter((chip): chip is string => Boolean(chip));
  const activeFeatureCount =
    activeModeChips.length + selectedToolIds.length + selectedSkillIds.length;

  // Auto-resize textarea
  useEffect(() => {
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = "auto";
      textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
    }
  }, [input]);

  // Focus textarea on mount
  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  // Consume a pending prompt handed off from GraphView "→ Ask Chat".
  // Pattern: caller sets `pendingPrompt` on chatStore, then closes the
  // overlay; we hydrate the input and clear the store so it fires once.
  const pendingPrompt = useChatStore((s) => s.pendingPrompt);
  const clearPendingPrompt = useChatStore((s) => s.clearPendingPrompt);
  useEffect(() => {
    if (pendingPrompt) {
      setInput(pendingPrompt);
      clearPendingPrompt();
      textareaRef.current?.focus();
    }
  }, [pendingPrompt, clearPendingPrompt]);

  // Calculate token percentage
  const tokenPercentage = effectiveTokenCount
    ? (effectiveTokenCount.current / effectiveTokenCount.max) * 100
    : 0;

  const getTokenStatus = () => {
    if (tokenPercentage > 90) return "text-error animate-pulse";
    if (tokenPercentage > 75) return "text-accent-main";
    return "text-content-tertiary";
  };

  const handleSubmit = useCallback(() => {
    if (!input.trim() && attachments.length === 0) return;
    if (isLoading) return;

    onSend(input.trim(), attachments.length > 0 ? attachments : undefined);
    setInput("");
    setAttachments([]);

    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }, [input, attachments, isLoading, onSend]);

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Phase 24 — slash popover keyboard control. When the popover is open,
    // intercept Up/Down/Enter/Tab/Escape before falling through to send.
    if (slashOpen && slashFiltered.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashCursor((c) => (c + 1) % slashFiltered.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashCursor(
          (c) => (c - 1 + slashFiltered.length) % slashFiltered.length,
        );
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        activateSlashItem(slashFiltered[slashCursor]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        // Strip the slash query so popover closes
        setInput((prev) =>
          prev.replace(/(?:^|\s)(\/[a-z0-9_-]*)$/i, (m) =>
            m.startsWith(" ") ? " " : "",
          ),
        );
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  /**
   * Phase 29 — paperclip path is now PER-TURN ATTACHMENTS, not corpus
   * ingestion. Files added via the paperclip stay in component state
   * until the user hits Send, at which point they ride on the chat
   * request as multimodal content. They are NOT uploaded to the
   * corpus. Corpus ingest is backend-folder only from Corpus Detail.
   */
  const handleFileSelect = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const newFiles = Array.from(files);
    setAttachments((prev) => {
      // Hard cap mirrors backend ATTACHMENT_MAX_COUNT=4. We accept up
      // to the cap; any overflow gets dropped silently — the user
      // sees their pill row stop growing at 4.
      const combined = [...prev, ...newFiles];
      return combined.slice(0, 4);
    });
  };

  const handleFileInputChange = (e: ChangeEvent<HTMLInputElement>) => {
    handleFileSelect(e.target.files);
  };

  const handleDrop = async (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);

    // `dataTransfer.files` is NOT recursive — dropping a folder yields only
    // top-level entries. Walk the FileSystemEntry tree via webkitGetAsEntry()
    // to collect every file in every nested directory.
    const items = Array.from(e.dataTransfer.items || []);
    const entries = items
      .map((it) => (it as any).webkitGetAsEntry?.() as FileSystemEntry | null)
      .filter((en): en is FileSystemEntry => !!en);

    if (entries.length === 0) {
      // Older browsers / non-dnd sources — fall back to flat file list.
      // Treat as per-turn attachments (the safer default for raw files).
      handleFileSelect(e.dataTransfer.files);
      return;
    }

    const collected: File[] = [];
    await Promise.all(entries.map((en) => walkEntry(en, collected)));

    const dt = new DataTransfer();
    collected.forEach((f) => dt.items.add(f));
    handleFileSelect(dt.files);
  };

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
  };

  const removeAttachment = (index: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  };

  const hasContent = input.trim().length > 0 || attachments.length > 0;

  return (
    <div className="pm-chat-composer w-full min-w-0 font-mono flex flex-col relative chat-input-container">
      {/* Active Scanline Indicator (from index.css) */}
      {(hasContent || isLoading) && <div className="pulse-indicator" />}

      <div className="relative px-2.5 pt-2.5 sm:px-3" ref={optionsRef}>
        <div className="flex min-w-0 items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <ModelSelector />
            <ThinkingEffortSelector />
          </div>
          <button
            type="button"
            data-testid="composer-features-toggle"
            onClick={() => setOptionsOpen((open) => !open)}
            className={`pm-soft-control flex h-8 shrink-0 items-center gap-2 rounded-full border px-2.5 text-[10px] font-bold uppercase tracking-widest !transition-colors !duration-150 ${
              activeFeatureCount > 0
                ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
                : "border-border-minimal bg-bg-surface text-content-secondary hover:text-content-primary"
            }`}
            aria-expanded={optionsOpen}
            title="Tools, skills, web, HyDE, and reasoning"
          >
            <SlidersHorizontal className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Features</span>
            <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-black/25 px-1.5 text-[9px]">
              {activeFeatureCount}
            </span>
          </button>
        </div>

        {optionsOpen && (
          <div className="pm-composer-options absolute left-2 right-2 bottom-[calc(100%+0.5rem)] z-[105] rounded-2xl border border-white/10 bg-[#15171d] p-2 shadow-2xl sm:left-auto sm:right-2 sm:w-[30rem] sm:max-w-[calc(100vw-1rem)]">
            <div className="mb-2 flex items-center justify-between gap-2 border-b border-white/10 px-2 pb-2">
              <div className="text-[10px] font-bold uppercase tracking-[0.22em] text-content-primary">
                Features
              </div>
              <span className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-2 py-0.5 text-[9px] font-bold uppercase tracking-widest text-emerald-200">
                {activeFeatureCount} active
              </span>
            </div>
            <ToggleBar className="rounded-xl bg-black/15 p-2" />
          </div>
        )}
      </div>

      {/* Token Budget Bar — driven by SSE `budget` frame from chat_orchestrator */}
      {effectiveTokenCount && (
        <div className="h-1 w-full bg-bg-surface">
          <div
            className={`h-full transition-[width] duration-300 ${tokenPercentage >= 90
                ? "bg-error"
                : tokenPercentage >= 70
                  ? "bg-amber-500"
                  : "bg-accent-main/70"
              }`}
            style={{ width: `${Math.min(100, tokenPercentage)}%` }}
            title={`${effectiveTokenCount.current} / ${effectiveTokenCount.max} tokens used`}
          />
        </div>
      )}

      {/* Input Container */}
      <div
        data-testid="upload-zone"
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        className={`
          relative bg-bg-base transition-none
          ${isDragging ? "border border-accent-main" : "border border-transparent"}
          ${isLoading ? "opacity-70 pointer-events-none" : ""}
        `}
      >
        {/* Drag Overlay */}
        {isDragging && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-bg-base/90 backdrop-blur-sm border-2 border-dashed border-accent-main">
            <div className="text-center flex flex-col items-center">
              <Paperclip className="w-6 h-6 mb-2 text-accent-main" />
              <p className="text-[10px] font-bold tracking-widest text-accent-main uppercase">
                [ INJECT_CONTEXT_FILES ]
              </p>
            </div>
          </div>
        )}

        {/* Phase 29 — vision-model warning. Renders ONLY when the
            user has attached at least one image and the selected
            model can't process images. Surfaces the mismatch BEFORE
            send so the user fixes it without a server round-trip. */}
        {visionMismatch && (
          <div className="mx-3 mt-2 flex items-start gap-2 px-3 py-2 rounded-none border border-amber-700/50 bg-amber-900/15 text-amber-300">
            <StatusTag tag="WRN" tone="wrn" />
            <div className="text-[10px] font-mono tracking-wider leading-snug">
              <div className="font-bold uppercase mb-0.5">
                Selected model has no vision support
              </div>
              <div className="normal-case font-normal text-amber-200/80">
                {visionCapableModelsHint()} The request will be rejected
                server-side if you send it as-is.
              </div>
            </div>
          </div>
        )}

        {/* Attachments Preview - Flat Block Styling */}
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 px-3 pt-3 pb-1">
            {attachments.map((file, index) => (
              <FileAttachment
                key={`${file.name}-${index}`}
                file={file}
                onRemove={isLoading ? undefined : () => removeAttachment(index)}
              />
            ))}
          </div>
        )}

        {/* Input Area */}
        <div className="flex items-end gap-2 p-2.5 sm:p-3">
          {/* Paperclip — PER-TURN multimodal attachments. Images go
              into the LLM call as image_url blocks; text files inline
              into the augmented prompt. Capped at 4 files / 20 MB each.
              Does NOT persist anything into a corpus. */}
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={isLoading}
            className={`
              pm-composer-icon flex-shrink-0 p-2 border transition-none rounded-full
              ${isDragging
                ? "bg-accent-main text-bg-base border-accent-main"
                : "border-transparent text-content-tertiary hover:border-border-minimal hover:text-accent-main bg-bg-surface"
              }
              ${isLoading ? "opacity-50 cursor-not-allowed" : ""}
            `}
            title="Per-turn attachment — images + text files inlined into THIS message only. Not saved to any corpus. Max 4 files / 20 MB each."
          >
            <Paperclip className="w-4 h-4" />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={(e: ChangeEvent<HTMLInputElement>) => {
              handleFileSelect(e.target.files);
              e.currentTarget.value = "";
            }}
            className="hidden"
          />

          <input
            type="file"
            multiple
            className="hidden"
            onChange={handleFileInputChange}
          />
          {/* Prompt Textarea + Slash Popover wrapper */}
          <div className="flex-1 min-w-0 relative">
            {/* Phase 24 — Slash popover (skills + tools by /command) */}
            {slashOpen && slashFiltered.length > 0 && (
              <div className="absolute bottom-full left-0 right-0 mb-1 max-h-72 overflow-y-auto custom-scrollbar bg-[#2a2a2a] border border-white/10 rounded shadow-xl z-50">
                <div className="px-3 py-1.5 text-[9px] font-bold tracking-widest uppercase text-content-tertiary border-b border-border-minimal">
                  Activate by command — ↑/↓ select · Enter/Tab activate · Esc cancel
                </div>
                {slashFiltered.map((item, idx) => (
                  <button
                    key={`${item.kind}-${item.id}`}
                    type="button"
                    onMouseDown={(e) => {
                      e.preventDefault();
                      activateSlashItem(item);
                    }}
                    onMouseEnter={() => setSlashCursor(idx)}
                    className={`w-full flex items-start gap-2 px-3 py-2 text-left transition-none ${
                      idx === slashCursor
                        ? "bg-accent-main/15 text-content-primary"
                        : "hover:bg-bg-base text-content-secondary"
                    }`}
                  >
                    {item.kind === "tool" ? (
                      <Wrench className="w-3 h-3 text-content-tertiary mt-0.5 shrink-0" />
                    ) : (
                      <Sparkles className="w-3 h-3 text-accent-secondary mt-0.5 shrink-0" />
                    )}
                    <div className="flex-1 overflow-hidden">
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] font-mono font-bold text-accent-main shrink-0">
                          {item.slash}
                        </span>
                        <span className="text-[10px] font-bold tracking-widest uppercase truncate">
                          {item.name}
                        </span>
                      </div>
                      <div className="text-[9px] text-content-tertiary truncate">
                        {item.description}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}

            {/* Active chips above textarea */}
            {(activeModeChips.length > 0 || activeChips.length > 0) && (
              <div className="flex flex-wrap gap-1 mb-1.5">
                {activeModeChips.map((chip) => (
                  <span
                    key={chip}
                    className="flex items-center gap-1 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-2 py-0.5 text-[9px] font-bold uppercase tracking-widest text-emerald-200"
                  >
                    <Sparkles className="h-2.5 w-2.5" />
                    {chip}
                  </span>
                ))}
                {activeChips.map((chip) => (
                  <span
                    key={`${chip.kind}-${chip.id}`}
                    className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-bold tracking-widest uppercase ${
                      chip.kind === "tool"
                        ? "border-cyan-400/30 text-cyan-200 bg-cyan-400/10"
                        : "border-violet-400/30 text-violet-200 bg-violet-400/10"
                    }`}
                  >
                    {chip.kind === "tool" ? (
                      <Wrench className="w-2.5 h-2.5" />
                    ) : (
                      <Sparkles className="w-2.5 h-2.5" />
                    )}
                    {chip.slash || chip.name}
                    <button
                      type="button"
                      onClick={() =>
                        chip.kind === "tool"
                          ? toggleTool(chip.id)
                          : toggleSkill(chip.id)
                      }
                      className="ml-0.5 hover:text-error transition-none"
                      title={`Deactivate ${chip.name}`}
                    >
                      <X className="w-2.5 h-2.5" />
                    </button>
                  </span>
                ))}
              </div>
            )}

            <textarea
              data-testid="query-input"
              ref={textareaRef}
              value={input}
              onChange={(e: ChangeEvent<HTMLTextAreaElement>) =>
                setInput(e.target.value)
              }
              onKeyDown={handleKeyDown}
              placeholder={placeholder}
              disabled={isLoading}
              rows={1}
              className="
                pm-query-textarea w-full min-h-[42px] max-h-[42dvh] sm:max-h-[250px] py-2.5 px-3
                bg-transparent border border-border-minimal
                resize-none text-sm text-content-primary placeholder:text-content-tertiary
                focus:outline-none focus:ring-0
                disabled:opacity-50 disabled:cursor-not-allowed
                custom-scrollbar
              "
            />
          </div>

          {/* Execution Controls */}
          <div className="flex flex-col items-end gap-2 shrink-0">
            {/* Token Tracker */}
            {effectiveTokenCount && (
              <div
                className={`hidden sm:flex items-center gap-1 text-[9px] font-bold tracking-widest uppercase ${getTokenStatus()}`}
              >
                <span>TOKENS:</span>
                <span>{effectiveTokenCount.current}</span>
                <span className="text-content-tertiary mx-0.5">/</span>
                <span className="text-content-tertiary">
                  {effectiveTokenCount.max}
                </span>
              </div>
            )}

            {/* Execute Button */}
            <button
              data-testid="query-submit"
              onClick={handleSubmit}
              disabled={!hasContent || isLoading}
              className={`
                pm-composer-action flex-shrink-0 flex h-10 w-10 sm:w-auto items-center justify-center gap-2 rounded-full px-3 sm:px-4 py-2 sm:py-1.5 min-w-0 sm:min-w-[118px]
                text-[10px] font-bold tracking-widest uppercase border !transition-colors !duration-150
                ${hasContent && !isLoading
                  ? "bg-accent-main text-bg-base border-accent-main hover:bg-accent-hover hover:border-accent-hover"
                  : "bg-bg-surface text-content-tertiary border-border-minimal"
                }
                disabled:opacity-50 disabled:cursor-not-allowed
              `}
              title={hasContent ? "Execute Query" : "Awaiting Input..."}
            >
              {isLoading ? (
                <>
                  <StatusTag tag="GEN" tone="gen" />
                  <span className="hidden sm:inline">PROCESS...</span>
                </>
              ) : (
                <>
                  <CornerDownLeft className="w-3.5 h-3.5" />
                  <span className="hidden sm:inline">EXECUTE</span>
                </>
              )}
            </button>
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between gap-2 px-3 pb-2 text-[9px] font-bold uppercase tracking-widest text-content-tertiary">
        <div className="min-w-0 truncate">
          {activeFeatureCount > 0
            ? activeModeChips
                .concat(activeChips.map((chip) => chip.name))
                .join(" / ")
            : "Ready"}
        </div>
        <div
          className={isLoading ? "text-accent-main" : "text-content-tertiary"}
        >
          {isLoading ? "Executing" : "Idle"}
        </div>
      </div>
    </div>
  );
}
