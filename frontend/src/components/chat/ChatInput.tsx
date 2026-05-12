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
  FolderUp,
  CornerDownLeft,
  X,
  FileText,
  Loader2,
  TerminalSquare,
  Wrench,
  Sparkles,
} from "lucide-react";
import { ToggleBar } from "./ToggleBar";
import { useChatStore } from "../../stores/chatStore";
import { useSettingsStore } from "../../stores/settingsStore";

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
  onFileUpload?: (files: File[]) => Promise<void>;
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

export function ChatInput({
  onSend,
  onFileUpload,
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
  const [uploading, setUploading] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  // Phase 24 — slash command popover state.
  // Detects `/<query>` token at the end of the input (typed by user) and
  // surfaces matching skills + tools. Picking one toggles its active state
  // and strips the slash token from the input.
  const {
    availableTools,
    availableSkills,
    selectedToolIds,
    selectedSkillIds,
    toggleTool,
    toggleSkill,
  } = useSettingsStore();

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

  const handleFileSelect = async (files: FileList | null) => {
    if (!files || files.length === 0) return;

    const newFiles = Array.from(files);
    setAttachments((prev) => [...prev, ...newFiles]);

    // Upload files if handler provided
    if (onFileUpload) {
      setUploading(true);
      try {
        await onFileUpload(newFiles);
      } catch (error) {
        console.error("File upload failed:", error);
      } finally {
        setUploading(false);
      }
    }
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
      handleFileSelect(e.dataTransfer.files);
      return;
    }

    const collected: File[] = [];
    await Promise.all(entries.map((en) => walkEntry(en, collected)));

    // Convert to a FileList-shaped object for handleFileSelect.
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

  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
  };

  const hasContent = input.trim().length > 0 || attachments.length > 0;

  return (
    <div className="w-full font-mono flex flex-col relative chat-input-container border border-border-minimal">
      {/* Active Scanline Indicator (from index.css) */}
      {(hasContent || isLoading) && <div className="pulse-indicator" />}

      {/* Orchestration Header - Per-query toggles */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-bg-surface border-b border-border-minimal">
        <ToggleBar />
        <div className="flex items-center gap-1.5 opacity-70">
          <TerminalSquare className="w-3 h-3 text-content-secondary" />
          <span className="text-[9px] uppercase tracking-[0.2em] text-content-secondary font-bold">
            I/O Panel
          </span>
        </div>
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

        {/* Attachments Preview - Flat Block Styling */}
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 px-3 pt-3 pb-1">
            {attachments.map((file, index) => (
              <div
                key={`${file.name}-${index}`}
                className="flex items-center gap-2 px-2 py-1 bg-bg-surface border border-border-minimal group transition-none hover:border-accent-main"
              >
                <FileText className="w-3 h-3 text-accent-secondary" />
                <span className="text-[10px] text-content-primary truncate max-w-[150px] font-bold">
                  {file.name}
                </span>
                <span className="text-[9px] text-content-tertiary tracking-wider">
                  [{formatFileSize(file.size)}]
                </span>
                <button
                  onClick={() => removeAttachment(index)}
                  className="p-0.5 hover:bg-error/20 hover:text-error text-content-tertiary transition-none rounded-none"
                  disabled={isLoading}
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Input Area */}
        <div className="flex items-end gap-2 p-3">
          {/* File Attach Button */}
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={isLoading || uploading}
            className={`
              flex-shrink-0 p-2 border transition-none rounded-none
              ${isDragging
                ? "bg-accent-main text-bg-base border-accent-main"
                : "border-transparent text-content-tertiary hover:border-border-minimal hover:text-accent-main bg-bg-surface"
              }
              ${isLoading || uploading ? "opacity-50 cursor-not-allowed" : ""}
            `}
            title="Attach files (or drag & drop; dropping a folder walks it recursively)"
          >
            {uploading ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Paperclip className="w-4 h-4" />
            )}
          </button>

          {/* Folder Attach Button — walks every file in the picked folder */}
          <button
            onClick={() => folderInputRef.current?.click()}
            disabled={isLoading || uploading}
            className={`
              flex-shrink-0 p-2 border transition-none rounded-none
              border-transparent text-content-tertiary hover:border-border-minimal hover:text-accent-main bg-bg-surface
              ${isLoading || uploading ? "opacity-50 cursor-not-allowed" : ""}
            `}
            title="Attach an entire folder (recursive)"
          >
            <FolderUp className="w-4 h-4" />
          </button>
          <input
            ref={folderInputRef}
            type="file"
            multiple
            // @ts-expect-error — webkitdirectory is non-standard but supported
            webkitdirectory=""
            directory=""
            onChange={(e) => handleFileSelect(e.target.files)}
            className="hidden"
          />
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={(e: ChangeEvent<HTMLInputElement>) =>
              handleFileSelect(e.target.files)
            }
            className="hidden"
          />

          <input
            type="file"
            multiple
            className="hidden"
            onChange={handleFileInputChange}
          />
          {/* Prompt Textarea + Slash Popover wrapper */}
          <div className="flex-1 relative">
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
            {activeChips.length > 0 && (
              <div className="flex flex-wrap gap-1 mb-1">
                {activeChips.map((chip) => (
                  <span
                    key={`${chip.kind}-${chip.id}`}
                    className={`flex items-center gap-1 px-1.5 py-0.5 text-[9px] font-bold tracking-widest uppercase border rounded ${
                      chip.kind === "tool"
                        ? "border-content-tertiary/40 text-content-secondary bg-bg-base/50"
                        : "border-accent-secondary/50 text-accent-secondary bg-accent-secondary/10"
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
                w-full min-h-[38px] max-h-[250px] py-2 px-3
                bg-transparent border border-border-minimal border-l-2 border-l-accent-main/50
                focus:border-l-accent-main
                resize-none text-sm text-content-primary placeholder:text-content-tertiary
                focus:outline-none focus:ring-0
                disabled:opacity-50 disabled:cursor-not-allowed
                custom-scrollbar
              "
            />
          </div>

          {/* Execution Controls */}
          <div className="flex flex-col items-end gap-2">
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
                flex-shrink-0 flex items-center justify-center gap-2 px-4 py-1.5 min-w-[120px]
                text-[10px] font-bold tracking-widest uppercase border transition-none
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
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span>PROCESS...</span>
                </>
              ) : (
                <>
                  <CornerDownLeft className="w-3.5 h-3.5" />
                  <span>EXECUTE</span>
                </>
              )}
            </button>
          </div>
        </div>
      </div>

      {/* Helper Console Text */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-bg-surface border-t border-border-minimal text-[9px] font-bold tracking-widest text-content-tertiary uppercase">
        <div className="flex gap-4">
          <span>&gt; [SHIFT+ENTER] = NEWLINE</span>
          <span className="hidden sm:inline">
            &gt; [DRAG+DROP] = INJECT_FILE
          </span>
        </div>
        <div>
          <span>STATE: {isLoading ? "EXECUTING" : "IDLE"}</span>
        </div>
      </div>
    </div>
  );
}
