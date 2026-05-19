// MessageBubble.tsx - Message display component with trimming indicators
import { useEffect, useRef, useState, type DependencyList } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  User,
  AlertTriangle,
  Clock,
  Copy,
  Check,
  Brain,
  Loader2,
  Search,
  TerminalSquare,
} from "lucide-react";
import type { ChatMessage } from "../../types";
import type { ProcessTimelineItem } from "../../types";
import type { TraceEvent } from "../../types";
import type { StreamingToolActivity } from "../../stores/chatStore";
import { RetrievalBadge } from "./RetrievalBadge";

interface MessageBubbleProps {
  message: ChatMessage;
  isStreaming?: boolean;
  toolActivity?: StreamingToolActivity[];
  processTimeline?: ProcessTimelineItem[];
}

export function MessageBubble({
  message,
  isStreaming = false,
  toolActivity = [],
  processTimeline,
}: MessageBubbleProps) {
  const [copied, setCopied] = useState(false);
  const [thinkingOpen, setThinkingOpen] = useState(isStreaming);
  const [traceOpen, setTraceOpen] = useState(isStreaming);
  const [toolOpen, setToolOpen] = useState(false);
  const previousThinkingLengthRef = useRef(0);
  const isUser = message.role === "user";
  const visibleToolActivity = isUser
    ? []
    : toolActivity.length > 0
      ? toolActivity
      : deriveToolActivityFromTraceEvents(message.trace_events);
  const visibleProcessTimeline = processTimeline || message.process_timeline || [];
  const hasProcessTimeline = !isUser && visibleProcessTimeline.length > 0;
  const hasRunningTool = visibleToolActivity.some(
    (activity) => activity.status === "running",
  );

  useEffect(() => {
    if (isUser) return;
    if (isStreaming && message.trace_events && message.trace_events.length > 0) {
      setTraceOpen(true);
      return;
    }
    if (!isStreaming) {
      setTraceOpen(false);
    }
  }, [isStreaming, isUser, message.trace_events?.length]);

  useEffect(() => {
    const thinkingLength = message.thinking?.length ?? 0;
    if (isUser || thinkingLength === 0) {
      previousThinkingLengthRef.current = thinkingLength;
      return;
    }
    const thinkingGrew = thinkingLength > previousThinkingLengthRef.current;

    if (!isStreaming || hasRunningTool || message.content.length > 0) {
      setThinkingOpen(false);
    } else if (thinkingGrew) {
      setThinkingOpen(true);
    }

    previousThinkingLengthRef.current = thinkingLength;
  }, [
    hasRunningTool,
    isStreaming,
    isUser,
    message.content.length,
    message.thinking?.length,
  ]);

  useEffect(() => {
    if (isUser || visibleToolActivity.length === 0) return;
    setToolOpen(Boolean(isStreaming && hasRunningTool));
  }, [hasRunningTool, isStreaming, isUser, visibleToolActivity.length]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  };

  const formatTime = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  };

  // Trimming warning component
  if (message.trimming_applied) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 mx-4 my-2 bg-error/10 border border-error transition-none rounded-none">
        <AlertTriangle className="w-3.5 h-3.5 text-error flex-shrink-0" />
        <span className="text-[10px] font-bold tracking-widest uppercase text-error">
          [SYSTEM_WARN] Context trimmed to fit token constraint
        </span>
      </div>
    );
  }

  return (
    <div
      className={`
        group flex gap-3 px-4 py-4
        ${isUser ? "flex-row-reverse" : "flex-row"}
        animate-fade-in
      `}
    >
      {/* Avatar */}
      <div
        className={`
          flex-shrink-0 w-8 h-8 border flex items-center justify-center transition-none rounded-none
          ${isUser ? "bg-accent-main/10 border-accent-main text-accent-main" : "bg-bg-surface border-border-minimal text-content-secondary"}
        `}
      >
        {isUser ? (
          <User className="w-4 h-4" />
        ) : (
          <TerminalSquare className="w-4 h-4" />
        )}
      </div>

      {/* Message Content */}
      <div
        className={`flex flex-col flex-1 min-w-0 ${
          isUser ? "max-w-[85%] items-end" : "max-w-full items-start"
        }`}
      >
        {hasProcessTimeline && (
          <ProcessTimeline
            items={visibleProcessTimeline}
            isStreaming={isStreaming}
          />
        )}

        {!hasProcessTimeline &&
          !isUser &&
          message.trace_events &&
          message.trace_events.length > 0 && (
          <TracePanel
            events={message.trace_events}
            open={traceOpen}
            isStreaming={isStreaming}
            onToggle={setTraceOpen}
          />
        )}

        {/* Thinking Block */}
        {!hasProcessTimeline && message.thinking && (
          <ReasoningPanel
            thinking={message.thinking}
            open={thinkingOpen}
            active={Boolean(
              thinkingOpen && isStreaming && !hasRunningTool && !message.content,
            )}
            onToggle={setThinkingOpen}
          />
        )}

        {!hasProcessTimeline && !isUser && visibleToolActivity.length > 0 && (
          <ToolActivityPanel
            activities={visibleToolActivity}
            open={toolOpen}
            onToggle={setToolOpen}
          />
        )}

        {/* Bubble */}
        <div
          className={`
            relative group/bubble
            ${isUser ? "message-user" : "message-assistant w-full max-w-4xl"}
          `}
        >
          {/* Content with Markdown and brutalist formatting */}
          <div
            className={
              isUser ? "break-words" : "synthesis-body break-words"
            }
          >
            {isUser ? (
              <span className="whitespace-pre-wrap">{message.content}</span>
            ) : (
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={markdownComponents}
              >
                {message.content}
              </ReactMarkdown>
            )}
            {isStreaming && (
              <span className="inline-block w-2 h-3 ml-1 bg-accent-main animate-pulse" />
            )}
          </div>

          {/* Copy Button (hover) */}
          {!isUser && !isStreaming && (
            <button
              onClick={handleCopy}
              className="
                absolute -right-10 top-2 p-1 border border-border-minimal bg-bg-surface
                text-content-tertiary hover:text-accent-main hover:border-accent-main
                opacity-0 group-hover/bubble:opacity-100 transition-none rounded-none
              "
              title="Copy message"
            >
              {copied ? (
                <Check className="w-4 h-4 text-success" />
              ) : (
                <Copy className="w-4 h-4" />
              )}
            </button>
          )}
        </div>

        {/* Metadata */}
        <div
          className={`
            flex items-center gap-3 mt-1 text-[9px] font-bold tracking-widest uppercase text-content-tertiary
            ${isUser ? "justify-end" : "justify-start"}
          `}
        >
          <span className="flex items-center gap-1">
            <Clock className="w-2.5 h-2.5" />
            {formatTime(message.created_at)}
          </span>

          {message.model_used && (
            <span className="hidden sm:inline">
              {message.model_used.split("/").pop()}
            </span>
          )}

          {message.token_count && <span>[{message.token_count} TOKENS]</span>}

          {/* Trust signal — always rendered for assistant messages so the
              ⚪ "training data only" state is visible even when no corpus
              was scoped. data-testid preserved for existing Playwright. */}
          {!isUser && <RetrievalBadge message={message} />}
        </div>
      </div>
    </div>
  );
}

function deriveToolActivityFromTraceEvents(
  events?: TraceEvent[],
): StreamingToolActivity[] {
  if (!events || events.length === 0) return [];

  const activityByName = new Map<string, StreamingToolActivity>();

  for (const event of events) {
    if (event.lane !== "tool_call" && event.lane !== "tool_result") {
      continue;
    }

    const metadataToolName =
      typeof event.metadata?.tool_name === "string"
        ? event.metadata.tool_name
        : undefined;
    const name = metadataToolName || toolNameFromTraceTitle(event.title);
    if (!name) continue;

    const current = activityByName.get(name);
    const isResult = event.lane === "tool_result";
    const status =
      isResult || event.status === "done" ? "done" : "running";
    const detail = event.content || current?.detail;

    activityByName.set(name, {
      id: current?.id || `${name}-${event.id}`,
      name,
      status: current?.status === "done" ? "done" : status,
      detail,
    });
  }

  return Array.from(activityByName.values());
}

const markdownComponents: Components = {
  code({ className, children, ...props }) {
    const raw = String(children).replace(/\n$/, "");
    const language = getCodeLanguage(className);
    const block = raw.includes("\n") || Boolean(className);

    if (block && isCommandCode(raw, language)) {
      return <CommandCard command={raw} language={language || "shell"} />;
    }

    if (block) {
      return (
        <pre className="pm-code-block">
          <code className={className} {...props}>
            {children}
          </code>
        </pre>
      );
    }

    return (
      <code
        className={isCommandLine(raw) ? "pm-command-chip" : "pm-inline-code"}
        {...props}
      >
        {children}
      </code>
    );
  },
  pre({ children }) {
    return <>{children}</>;
  },
};

function CommandCard({
  command,
  language,
}: {
  command: string;
  language: string;
}) {
  const [copied, setCopied] = useState(false);

  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch (err) {
      console.error("Failed to copy command:", err);
    }
  };

  return (
    <div className="pm-command-card">
      <div className="pm-command-card-header">
        <span className="pm-command-card-dot" />
        <span className="pm-command-card-title">{language || "command"}</span>
        <button
          type="button"
          onClick={copyCommand}
          className="pm-command-card-copy"
          title="Copy command"
        >
          {copied ? (
            <Check className="h-3.5 w-3.5" />
          ) : (
            <Copy className="h-3.5 w-3.5" />
          )}
          <span>{copied ? "COPIED" : "COPY"}</span>
        </button>
      </div>
      <pre className="pm-command-card-body">
        <code>{command}</code>
      </pre>
    </div>
  );
}

function getCodeLanguage(className: string | undefined): string {
  const match = /language-([\w-]+)/.exec(className || "");
  return match?.[1]?.toLowerCase() || "";
}

function isCommandCode(raw: string, language: string): boolean {
  const shellLanguages = new Set([
    "bash",
    "sh",
    "shell",
    "zsh",
    "powershell",
    "ps1",
    "console",
    "terminal",
  ]);
  return shellLanguages.has(language) || isCommandLine(raw);
}

function isCommandLine(raw: string): boolean {
  const line = raw.trim().split(/\r?\n/)[0] || "";
  return /^(?:[$>]\s*)?(?:npm|pnpm|yarn|bun|npx|node|python|py|pip|uv|git|docker(?:\s+compose)?|curl|wget|cargo|rustup|go|make|cmake|pytest|ruff|black|mypy|poetry|pipx|ollama|llama-server)\b/i.test(
    line,
  );
}

function toolNameFromTraceTitle(title: string): string | undefined {
  const match = title.match(/\b([a-zA-Z][a-zA-Z0-9_-]*)\s+tool\b/);
  return match?.[1];
}

function ProcessTimeline({
  items,
  isStreaming,
}: {
  items: ProcessTimelineItem[];
  isStreaming: boolean;
}) {
  const [manualOpenIds, setManualOpenIds] = useState<Set<string>>(new Set());
  const groups = compactProcessTimeline(items);
  const activeId = isStreaming
    ? [...groups].reverse().find((group) => group.status === "running")?.id
    : undefined;

  const handleToggle = (id: string, open: boolean) => {
    setManualOpenIds((previous) => {
      const next = new Set(previous);
      if (open) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
  };

  return (
    <div className="pm-process-list mb-2 w-full max-w-3xl">
      {groups.map((group, index) => {
        const active = group.id === activeId;
        const open = active || manualOpenIds.has(group.id);
        return (
          <ProcessTimelineCard
            key={group.id}
            group={group}
            index={index}
            open={open}
            active={active}
            onToggle={(nextOpen) => handleToggle(group.id, nextOpen)}
          />
        );
      })}
    </div>
  );
}

function ProcessTimelineCard({
  group,
  index,
  open,
  active,
  onToggle,
}: {
  group: ProcessGroup;
  index: number;
  open: boolean;
  active: boolean;
  onToggle: (open: boolean) => void;
}) {
  const bodyRef = useAutoScroll<HTMLDivElement>([
    group.items.length,
    group.items[group.items.length - 1]?.content,
    group.items[group.items.length - 1]?.detail,
    open,
  ], active);
  const statusClass =
    group.status === "error"
      ? "text-error"
      : active
        ? "text-accent-main"
        : group.status === "skipped"
          ? "text-content-tertiary"
          : "text-emerald-400";

  useEffect(() => {
    if (!open || active) return;
    const node = bodyRef.current;
    if (!node) return;
    requestAnimationFrame(() => {
      node.scrollTop = 0;
    });
  }, [active, bodyRef, group.id, open]);

  return (
    <details
      open={open}
      onToggle={(e) => {
        if (active) return;
        onToggle((e.target as HTMLDetailsElement).open);
      }}
      className="group/process w-full"
    >
      <summary className="pm-process-summary">
        <span className="pm-process-caret">{open ? "▾" : "▸"}</span>
        <span className={`pm-process-kind pm-process-kind-${group.kind}`}>
          {group.kindLabel}
        </span>
        <span className="pm-process-title">{group.title}</span>
        {active ? (
          <GeneratingIndicator
            label={group.kind === "gen" ? "THINKING" : "RUNNING"}
          />
        ) : (
          <span className={statusClass}>{group.status || "done"}</span>
        )}
        <span className="pm-process-count">
          {String(index + 1).padStart(2, "0")} · {group.items.length}
        </span>
      </summary>
      <div
        ref={bodyRef}
        className="pm-process-body custom-scrollbar"
      >
        {group.kind === "exe" ? (
          <ToolTranscript group={group} />
        ) : (
          group.items.map((item, itemIndex) => (
            <ProcessTimelineRow
              key={item.id}
              item={item}
              index={itemIndex}
            />
          ))
        )}
      </div>
    </details>
  );
}

type ProcessGroupKind = "setup" | "gen" | "exe" | "warn";

interface ProcessGroup {
  id: string;
  kind: ProcessGroupKind;
  kindLabel: string;
  title: string;
  status: string;
  items: ProcessTimelineItem[];
}

function ProcessTimelineRow({
  item,
  index,
}: {
  item: ProcessTimelineItem;
  index: number;
}) {
  const rowLabel =
    item.kind === "reasoning"
      ? "GEN"
      : item.kind === "tool"
        ? "EXE"
        : item.status === "error"
          ? "ERR"
          : item.status === "skipped"
            ? "WRN"
            : "LOG";
  const content = [item.content, item.detail].filter(Boolean).join("\n\n");

  return (
    <div className="pm-process-row">
      <span className="pm-process-row-index">
        {String(index + 1).padStart(2, "0")}
      </span>
      <span className={`pm-process-row-label pm-process-row-label-${rowLabel.toLowerCase()}`}>
        {rowLabel}
      </span>
      <div className="min-w-0 flex-1">
        <div className="pm-process-row-title">{item.title}</div>
        {content && (
          <div className="pm-process-row-content custom-scrollbar">
            {content}
          </div>
        )}
      </div>
    </div>
  );
}

function ToolTranscript({ group }: { group: ProcessGroup }) {
  const transcript = buildToolTranscript(group.items);

  if (!transcript) {
    return (
      <>
        {group.items.map((item, itemIndex) => (
          <ProcessTimelineRow
            key={item.id}
            item={item}
            index={itemIndex}
          />
        ))}
      </>
    );
  }

  return (
    <div className={`pm-tool-transcript pm-tool-transcript-${transcript.kind.toLowerCase()}`}>
      <div className="pm-tool-transcript-head">
        <span className="pm-tool-transcript-kind">{transcript.kind}</span>
        <span className="pm-tool-transcript-title">{transcript.title}</span>
      </div>
      {transcript.document && (
        <TranscriptTextBlock label="Document" lines={[transcript.document]} />
      )}
      {transcript.queries.length > 0 && (
        <TranscriptTextBlock label="Queries" lines={transcript.queries} />
      )}
      {transcript.reason && (
        <div className="pm-tool-kv">
          <span>Reason</span>
          <p>{transcript.reason}</p>
        </div>
      )}
      <div className="pm-tool-grid">
        {transcript.toolName && (
          <div>
            <span>Tool</span>
            <strong>{transcript.toolName}</strong>
          </div>
        )}
        {transcript.status && (
          <div>
            <span>Status</span>
            <strong>{transcript.status}</strong>
          </div>
        )}
        {transcript.method && (
          <div>
            <span>Method</span>
            <strong>{transcript.method}</strong>
          </div>
        )}
        {transcript.chars && (
          <div>
            <span>Chars</span>
            <strong>{transcript.chars}</strong>
          </div>
        )}
        {transcript.candidates && (
          <div>
            <span>Candidates</span>
            <strong>{transcript.candidates}</strong>
          </div>
        )}
        {transcript.selection && (
          <div>
            <span>Selection</span>
            <strong>{transcript.selection}</strong>
          </div>
        )}
      </div>
      {transcript.metrics.length > 0 && (
        <div className="pm-tool-metrics">
          {transcript.metrics.map((metric) => (
            <div
              key={`${metric.label}-${metric.value}`}
              className={`pm-tool-metric pm-tool-metric-${metric.tone || "default"}`}
            >
              <span>{metric.label}</span>
              <code>{metric.value}</code>
            </div>
          ))}
        </div>
      )}
      {transcript.sources.length > 0 && (
        <div className="pm-tool-sources">
          <div className="pm-tool-sources-title">Sources</div>
          {transcript.sources.map((source) => (
            <div key={`${source.title}-${source.url}`} className="pm-tool-source">
              <span>{source.title}</span>
              <a href={source.url} target="_blank" rel="noreferrer">
                {source.url}
              </a>
            </div>
          ))}
        </div>
      )}
      {transcript.fallbackLines.length > 0 && (
        <pre className="pm-tool-decision custom-scrollbar">
          {transcript.fallbackLines.join("\n")}
        </pre>
      )}
    </div>
  );
}

function TranscriptTextBlock({
  label,
  lines,
}: {
  label: string;
  lines: string[];
}) {
  return (
    <div className="pm-tool-text-block">
      <div className="pm-tool-text-label">{label}</div>
      <div className="pm-tool-text-lines">
        {lines.map((line) => (
          <code key={line}>{line}</code>
        ))}
      </div>
    </div>
  );
}

interface ToolTranscriptData {
  kind: "WEB" | "EXE";
  title: string;
  toolName?: string;
  document?: string;
  queries: string[];
  reason?: string;
  status?: string;
  method?: string;
  chars?: string;
  candidates?: string;
  selection?: string;
  metrics: Array<{
    label: string;
    value: string;
    tone?: "decision" | "evidence" | "obscura" | "default";
  }>;
  sources: Array<{ title: string; url: string }>;
  fallbackLines: string[];
}

function buildToolTranscript(items: ProcessTimelineItem[]): ToolTranscriptData | null {
  const content = items.map((item) => [item.content, item.detail].filter(Boolean).join("\n")).join("\n\n");
  const firstTool = items.find((item) => item.kind === "tool");
  const nativeCall = items.find((item) => /native tool call/i.test(item.title));
  const parsedCall = nativeCall ? parseNativeToolCall(nativeCall.content || "") : null;
  const { request, result } = splitToolDetail(firstTool?.detail || "");
  const decision = parseWebDecisionTrace(content);
  const toolName = parsedCall?.name || inferToolName(firstTool?.title || content);
  const query =
    valueAsString(request.query) ||
    valueAsString(parsedCall?.args.query) ||
    decision.query ||
    extractLineValue(content, "query");
  const url =
    valueAsString(request.url) ||
    valueAsString(parsedCall?.args.url) ||
    valueAsString(result.url) ||
    extractLineValue(content, "url");
  const queries = uniqueTruthy([
    query,
    ...decision.searchQueries,
  ]);
  const document = url || undefined;

  if (!toolName && !decision.present && queries.length === 0 && !document) return null;

  return {
    kind: toolName === "web_search" ? "WEB" : "EXE",
    title:
      toolName === "web_search"
        ? "Using tool `web_search`"
        : toolName === "fetch_page"
          ? "Using tool `fetch_page`"
          : `Using tool \`${toolName || "tool"}\``,
    toolName,
    document: toolName === "web_search" ? undefined : document,
    queries,
    reason: valueAsString(request.reason) || valueAsString(parsedCall?.args.reason),
    status: valueAsString(result.status),
    method: valueAsString(result.method),
    chars: valueAsString(result.chars),
    candidates: decision.candidates,
    selection: decision.selection,
    metrics: decision.metrics,
    sources: decision.sources,
    fallbackLines: decision.fallbackLines,
  };
}

function parseNativeToolCall(content: string): { name?: string; args: Record<string, unknown> } | null {
  try {
    const parsed = JSON.parse(content) as
      | Array<{ name?: string; args?: string | Record<string, unknown> }>
      | { name?: string; args?: string | Record<string, unknown> };
    const first = Array.isArray(parsed) ? parsed[0] : parsed;
    if (!first) return null;
    const rawArgs = first.args;
    const args =
      typeof rawArgs === "string"
        ? JSON.parse(rawArgs) as Record<string, unknown>
        : rawArgs || {};
    return {
      name: first.name,
      args,
    };
  } catch {
    return null;
  }
}

function splitToolDetail(detail: string): {
  request: Record<string, string>;
  result: Record<string, string>;
} {
  if (!detail) return { request: {}, result: {} };
  const parts = detail.split(/\n\nresult\n/i);
  const requestText = (parts[0] || "").replace(/^request\n/i, "");
  const resultText = parts[1] || parts[0] || "";
  return {
    request: parseKeyValueBlock(requestText),
    result: parseKeyValueBlock(resultText),
  };
}

function parseKeyValueBlock(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split(/\r?\n/)) {
    const match = /^([a-zA-Z_]+):\s*(.*)$/.exec(line.trim());
    if (match) out[match[1]] = match[2];
  }
  return out;
}

function extractLineValue(text: string, key: string): string | undefined {
  const match = new RegExp(`^${key}:\\s*(.+)$`, "im").exec(text);
  return match?.[1]?.trim();
}

interface ParsedWebDecisionTrace {
  present: boolean;
  query?: string;
  searchQueries: string[];
  candidates?: string;
  selection?: string;
  metrics: ToolTranscriptData["metrics"];
  sources: Array<{ title: string; url: string }>;
  fallbackLines: string[];
}

function parseWebDecisionTrace(text: string): ParsedWebDecisionTrace {
  const fallback: ParsedWebDecisionTrace = {
    present: false,
    searchQueries: [],
    metrics: [],
    sources: [],
    fallbackLines: [],
  };
  const marker = "[Web retrieval decision trace]";
  const idx = text.indexOf(marker);
  if (idx < 0) return fallback;
  const block = text.slice(idx).trim();
  const map = parseKeyValueBlock(block);
  const sources = extractTopSources(block);
  const metrics: ToolTranscriptData["metrics"] = [];
  const metricKeys: Array<[string, ToolTranscriptData["metrics"][number]["tone"]]> = [
    ["snippet_decision", "decision"],
    ["snippet_evidence", "evidence"],
    ["page_fetch", "default"],
    ["fetch_methods", "default"],
    ["obscura", "obscura"],
    ["reranker", "default"],
  ];

  for (const [key, tone] of metricKeys) {
    if (map[key]) {
      metrics.push({ label: key, value: map[key], tone });
    }
  }

  return {
    present: true,
    query: map.query,
    searchQueries: (map.search_queries || "")
      .split(";")
      .map((query) => query.trim())
      .filter(Boolean),
    candidates: map.candidates,
    selection: selectionFromReranker(map.reranker),
    metrics,
    sources,
    fallbackLines: metrics.length === 0 ? block.split(/\r?\n/).slice(1) : [],
  };
}

function extractTopSources(text: string): Array<{ title: string; url: string }> {
  const sources: Array<{ title: string; url: string }> = [];
  const section = text.split("top_selected_sources:")[1] || "";
  for (const line of section.split(/\r?\n/)) {
    const match = /^-\s*(.*?)\s*\[[^\]]+\]\s*(https?:\/\/\S+)/.exec(line.trim());
    if (match) {
      sources.push({ title: match[1], url: match[2] });
    }
    if (sources.length >= 5) break;
  }
  return sources;
}

function selectionFromReranker(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const match = /\bselected=([^\s]+)/.exec(value);
  return match?.[1] || undefined;
}

function valueAsString(value: unknown): string | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return undefined;
}

function uniqueTruthy(values: Array<string | undefined>): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const normalized = value?.trim();
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    result.push(normalized);
  }
  return result;
}

function inferToolName(text: string): string | undefined {
  if (/web search|web_search/i.test(text)) return "web_search";
  if (/fetch page|fetch_page/i.test(text)) return "fetch_page";
  return undefined;
}

function compactProcessTimeline(items: ProcessTimelineItem[]): ProcessGroup[] {
  const groups: ProcessGroup[] = [];
  let current: ProcessGroup | null = null;

  const close = () => {
    if (current) {
      current.status = summarizeGroupStatus(current.items);
      groups.push(current);
      current = null;
    }
  };

  const ensure = (kind: ProcessGroupKind, title: string): ProcessGroup => {
    if (current && current.kind === kind) return current;
    close();
    current = {
      id: `${kind}-${groups.length}-${items.length}`,
      kind,
      kindLabel: kind === "setup" ? "SYS" : kind === "gen" ? "GEN" : kind === "warn" ? "WRN" : "EXE",
      title,
      status: "done",
      items: [],
    };
    return current;
  };

  for (const item of items) {
    if (/assistant final answer/i.test(item.title)) {
      continue;
    }

    if (isSetupTrace(item)) {
      ensure("setup", "Setup, RAG, and route checks").items.push(item);
      continue;
    }

    if (isModelTrace(item) || item.kind === "reasoning") {
      ensure(
        "gen",
        item.title.includes("final") ? "Final synthesis" : "Model reasoning",
      ).items.push(item);
      continue;
    }

    if (item.kind === "tool" || isToolTrace(item)) {
      ensure("exe", titleForToolGroup(item)).items.push(item);
      continue;
    }

    if (item.status === "error" || item.status === "skipped") {
      ensure("warn", "Warnings and recoveries").items.push(item);
      continue;
    }

    ensure("setup", "Setup, RAG, and route checks").items.push(item);
  }

  close();
  return groups.map((group, index) => ({
    ...group,
    id: `${group.kind}-${index}-${group.items[0]?.id || index}`,
    status: summarizeGroupStatus(group.items),
  }));
}

function summarizeGroupStatus(items: ProcessTimelineItem[]): string {
  if (items.some((item) => item.status === "running")) return "running";
  if (items.some((item) => item.status === "error")) return "error";
  if (items.every((item) => item.status === "skipped")) return "skipped";
  return "done";
}

function isSetupTrace(item: ProcessTimelineItem): boolean {
  return (
    item.kind === "trace" &&
    /routed|hyde|rag retrieval|agentic web loop|retrieval finished/i.test(item.title)
  );
}

function isModelTrace(item: ProcessTimelineItem): boolean {
  return item.kind === "trace" && /chat model.*stream/i.test(item.title);
}

function isToolTrace(item: ProcessTimelineItem): boolean {
  return (
    item.kind === "trace" &&
    /native tool|web retrieval decision/i.test(item.title)
  );
}

function titleForToolGroup(item: ProcessTimelineItem): string {
  if (/fetch/i.test(item.title)) return "Fetch and inspect source";
  if (/search/i.test(item.title)) return "Search and retrieve web evidence";
  return "Tool execution";
}

function TracePanel({
  events,
  open,
  isStreaming,
  onToggle,
}: {
  events: TraceEvent[];
  open: boolean;
  isStreaming: boolean;
  onToggle: (open: boolean) => void;
}) {
  const active = isStreaming;
  const bodyRef = useAutoScroll<HTMLDivElement>([
    events.length,
    events[events.length - 1]?.content,
  ]);

  return (
    <details
      open={open}
      onToggle={(e) => onToggle((e.target as HTMLDetailsElement).open)}
      className="mb-2 w-full max-w-3xl group/trace"
    >
      <summary className="flex items-center gap-2 px-3 py-2 text-[11px] font-bold tracking-widest uppercase text-content-tertiary bg-bg-surface border border-border-minimal transition-none rounded-none cursor-pointer hover:text-content-primary hover:border-content-tertiary select-none list-none">
        <TerminalSquare className="w-3.5 h-3.5" />
        <span>[TRACE_LOG]</span>
        {active ? (
          <GeneratingIndicator />
        ) : (
          <span className="text-content-tertiary/70">DONE</span>
        )}
        <span className="ml-auto text-content-tertiary/60 group-hover/trace:text-content-secondary">
          {open ? "COLLAPSE [-]" : "EXPAND [+]"}
        </span>
      </summary>
      <div
        ref={bodyRef}
        className="mt-1 max-h-72 overflow-y-auto custom-scrollbar border border-border-minimal bg-bg-base p-3 font-mono text-[12px] leading-6 text-content-secondary transition-none rounded-none"
      >
        <div className="space-y-2">
          {active && (
            <div className="flex items-center gap-2 border-l border-accent-main pl-2 text-[9px] font-bold uppercase tracking-widest text-accent-main">
              <GeneratingIndicator label="STREAMING" />
              <span className="text-content-tertiary">
                model and tool trace is updating live
              </span>
            </div>
          )}
          {events.map((event) => (
            <TraceEventRow key={event.id} event={event} />
          ))}
        </div>
      </div>
    </details>
  );
}

function ReasoningPanel({
  thinking,
  open,
  active,
  onToggle,
}: {
  thinking: string;
  open: boolean;
  active: boolean;
  onToggle: (open: boolean) => void;
}) {
  const bodyRef = useAutoScroll<HTMLDivElement>([thinking]);

  return (
    <details
      open={open}
      onToggle={(e) => onToggle((e.target as HTMLDetailsElement).open)}
      className="mb-2 w-full max-w-3xl group/thinking"
    >
      <summary className="flex items-center gap-2 px-3 py-2 text-[11px] font-bold tracking-widest uppercase text-content-tertiary bg-bg-surface border border-border-minimal transition-none rounded-none cursor-pointer hover:text-content-primary hover:border-content-tertiary select-none list-none">
        <Brain className="w-3.5 h-3.5" />
        <span>[REASONING_TRACE]</span>
        {active ? (
          <GeneratingIndicator label="THINKING" />
        ) : (
          <span className="text-content-tertiary/70">DONE</span>
        )}
        <span className="ml-auto text-content-tertiary/60 group-hover/thinking:text-content-secondary">
          {open ? "COLLAPSE [-]" : "EXPAND [+]"}
        </span>
      </summary>
      <div
        ref={bodyRef}
        className="mt-1 max-h-56 overflow-y-auto custom-scrollbar border border-border-minimal bg-bg-base px-3 py-2 font-mono text-[12px] leading-6 text-content-secondary transition-none rounded-none whitespace-pre-wrap break-words"
      >
        {thinking}
      </div>
    </details>
  );
}

function GeneratingIndicator({ label = "GENERATING" }: { label?: string }) {
  return (
    <span
      className="inline-flex items-center gap-1 text-accent-main"
      aria-label={label.toLowerCase()}
    >
      <span>{label}</span>
      <span className="inline-flex items-center gap-0.5" aria-hidden="true">
        <span className="h-1 w-1 animate-bounce rounded-full bg-current [animation-duration:900ms]" />
        <span
          className="h-1 w-1 animate-bounce rounded-full bg-current [animation-duration:900ms]"
          style={{ animationDelay: "120ms" }}
        />
        <span
          className="h-1 w-1 animate-bounce rounded-full bg-current [animation-duration:900ms]"
          style={{ animationDelay: "240ms" }}
        />
      </span>
    </span>
  );
}

function TraceEventRow({ event }: { event: TraceEvent }) {
  const statusClass =
    event.status === "error"
      ? "text-error"
      : event.status === "running"
        ? "text-accent-main"
        : event.status === "skipped"
          ? "text-content-tertiary"
          : "text-emerald-400";

  return (
    <div className="border-l border-border-minimal pl-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[9px] font-bold uppercase tracking-widest">
        <span className="text-content-primary">{event.title}</span>
        <span className={statusClass}>{event.status || "event"}</span>
        <span className="text-content-tertiary">{event.lane}</span>
        <span className="text-content-tertiary/70">
          {formatTraceTime(event.timestamp)}
        </span>
      </div>
      {event.content && (
        <div className="mt-1 whitespace-pre-wrap break-words text-content-secondary">
          {event.content}
        </div>
      )}
    </div>
  );
}

function ToolActivityPanel({
  activities,
  open,
  onToggle,
}: {
  activities: StreamingToolActivity[];
  open: boolean;
  onToggle: (open: boolean) => void;
}) {
  const bodyRef = useAutoScroll<HTMLDivElement>([
    activities.length,
    activities.map((activity) => `${activity.name}:${activity.status}`).join("|"),
  ]);
  const running = activities.some((activity) => activity.status === "running");

  return (
    <details
      open={open}
      onToggle={(e) => onToggle((e.target as HTMLDetailsElement).open)}
      className="mb-2 w-full max-w-3xl group/tool"
    >
      <summary className="flex items-center gap-2 border border-border-minimal bg-bg-surface px-3 py-2 text-[11px] font-bold uppercase tracking-widest text-content-tertiary transition-none rounded-none cursor-pointer hover:text-content-primary hover:border-content-tertiary select-none list-none">
        <span>[TOOL_ACTIVITY]</span>
        {running ? (
          <GeneratingIndicator label="RUNNING" />
        ) : (
          <span className="text-content-tertiary/70">DONE</span>
        )}
        <span className="ml-auto text-content-tertiary/60 group-hover/tool:text-content-secondary">
          {open ? "COLLAPSE [-]" : "EXPAND [+]"}
        </span>
      </summary>
      <div
        ref={bodyRef}
        className="mt-1 max-h-48 overflow-y-auto custom-scrollbar border border-border-minimal bg-bg-base px-3 py-2 font-mono text-[12px] leading-6 text-content-secondary transition-none rounded-none"
      >
        <div className="space-y-1">
        {activities.map((activity) => {
          const isRunning = activity.status === "running";
          return (
            <div
              key={activity.id}
              className="text-content-secondary"
            >
              <div className="flex items-center gap-2">
                {isRunning ? (
                  <Loader2 className="h-3 w-3 shrink-0 animate-spin text-accent-main" />
                ) : (
                  <Check className="h-3 w-3 shrink-0 text-emerald-400" />
                )}
                <Search className="h-3 w-3 shrink-0 text-content-tertiary" />
                <span className="font-bold uppercase tracking-wider text-content-primary">
                  {formatToolName(activity.name)}
                </span>
                <span className="text-content-tertiary">
                  {isRunning ? "searching" : "complete"}
                </span>
              </div>
              {activity.detail && (
                <div className="ml-10 mt-1 max-h-28 overflow-y-auto custom-scrollbar break-all border border-border-minimal bg-bg-surface p-2 text-[11px] leading-5 text-content-tertiary">
                  {activity.detail}
                </div>
              )}
            </div>
          );
        })}
        </div>
      </div>
    </details>
  );
}

function useAutoScroll<T extends HTMLElement>(
  dependencies: DependencyList,
  enabled = true,
) {
  const ref = useRef<T | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const node = ref.current;
    if (!node) return;
    requestAnimationFrame(() => {
      node.scrollTop = node.scrollHeight;
    });
  }, [...dependencies, enabled]);

  return ref;
}

function formatToolName(name: string): string {
  if (name === "web_search") return "WEB SEARCH";
  return name.replace(/_/g, " ").toUpperCase();
}

function formatTraceTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
