// MessageBubble.tsx - Message display component with trimming indicators
import { useEffect, useRef, useState, type DependencyList } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Clock,
  Copy,
  Check,
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
  const [traceOpen, setTraceOpen] = useState(
    isStreaming || Boolean(message.trace_events?.length),
  );
  const [toolOpen, setToolOpen] = useState(false);
  const previousThinkingLengthRef = useRef(0);
  const isUser = message.role === "user";
  const visibleToolActivity = isUser
    ? []
    : toolActivity.length > 0
      ? toolActivity
      : deriveToolActivityFromTraceEvents(message.trace_events);
  const visibleProcessTimeline = (processTimeline || message.process_timeline || []).filter(
    (item) => item.kind !== "reasoning",
  );
  const hasProcessTimeline = !isUser && visibleProcessTimeline.length > 0;
  const hasRunningTool = visibleToolActivity.some(
    (activity) => activity.status === "running",
  );
  const hasAssistantContent = !isUser && message.content.trim().length > 0;
  const showLiveDraftPlaceholder =
    !isUser && isStreaming && !hasAssistantContent;

  useEffect(() => {
    if (isUser) return;
    if (isStreaming && message.trace_events && message.trace_events.length > 0) {
      setTraceOpen(true);
      return;
    }
  }, [isStreaming, isUser, message.trace_events?.length]);

  useEffect(() => {
    const thinkingLength = message.thinking?.length ?? 0;
    if (isUser || thinkingLength === 0) {
      previousThinkingLengthRef.current = thinkingLength;
      return;
    }
    const thinkingGrew = thinkingLength > previousThinkingLengthRef.current;

    if (!isStreaming || hasRunningTool) {
      setThinkingOpen(false);
    } else if (thinkingGrew) {
      setThinkingOpen(true);
    }

    previousThinkingLengthRef.current = thinkingLength;
  }, [
    hasRunningTool,
    isStreaming,
    isUser,
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
      <div className="flex items-center gap-2 px-3 py-1.5 mx-4 my-2 bg-error/10 transition-none rounded-none">
        <StatusBadge tag="WRN" tone="wrn" />
        <span className="text-[10px] font-bold tracking-widest uppercase text-error">
          [SYSTEM_WARN] Context trimmed to fit token constraint
        </span>
      </div>
    );
  }

  return (
    <div
      className={`
        group flex w-full
        ${isUser ? "justify-end" : "justify-start"}
        animate-fade-in
      `}
    >
      {/* Message Content */}
      <div
        className={`flex flex-col flex-1 min-w-0 ${
          isUser ? "items-end" : "items-start"
        }`}
      >
        {/* Bubble */}
        <div
          data-role={message.role}
          className={`
            relative group/bubble
            ${
              isUser
                ? "message-user"
                : `message-assistant w-full max-w-[82ch] ${
                    isStreaming ? "message-assistant-streaming" : ""
                  }`
            }
          `}
        >
          {/* Content with Markdown and brutalist formatting */}
          <div
            className={
              isUser
                ? "message-text whitespace-pre-wrap break-words"
                : "message-text synthesis-body break-words"
            }
          >
            {isUser ? (
              message.content
            ) : showLiveDraftPlaceholder ? (
              <LiveAnswerDraft
                hasThinking={Boolean(message.thinking)}
                hasProcess={hasProcessTimeline || Boolean(message.trace_events?.length)}
                thinking={message.thinking || ""}
              />
            ) : (
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={markdownComponents}
              >
                {message.content}
              </ReactMarkdown>
            )}
            {isStreaming && (
              <span className="pm-stream-caret" aria-hidden="true" />
            )}
          </div>

          {/* Copy Button (hover) */}
          {!isUser && !isStreaming && (
            <button
              onClick={handleCopy}
              className="
                absolute -right-8 top-0 p-1 text-content-tertiary
                opacity-0 transition-opacity hover:text-accent-main group-hover/bubble:opacity-100
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

        {hasProcessTimeline && (
          <ProcessTimeline
            items={visibleProcessTimeline}
            isStreaming={isStreaming}
            defaultOpen={true}
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
        {message.thinking && (
          <ReasoningPanel
            thinking={message.thinking}
            open={thinkingOpen}
            active={Boolean(
              thinkingOpen && isStreaming && !hasRunningTool,
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
              training-data-only state is visible even when no corpus was
              scoped. data-testid preserved for existing Playwright. */}
          {!isUser && <RetrievalBadge message={message} />}
        </div>
      </div>
    </div>
  );
}

function LiveAnswerDraft({
  hasThinking,
  hasProcess,
  thinking,
}: {
  hasThinking: boolean;
  hasProcess: boolean;
  thinking: string;
}) {
  const label = hasThinking
    ? "Reading the model's reasoning stream"
    : hasProcess
      ? "Tracing retrieval and model steps live"
      : "Starting the answer stream";
  const thinkingPreview = formatLiveThinkingPreview(thinking);

  return (
    <div className="pm-live-answer-draft" aria-live="polite">
      <div className="pm-live-answer-draft-head">
        <span className="pm-live-answer-dot" />
        <span>{label}</span>
      </div>
      {thinkingPreview ? (
        <div className="pm-live-reasoning-preview">
          <div className="pm-live-reasoning-label">
            <StatusBadge tag="GEN" tone="gen" />
            <span>live reasoning</span>
          </div>
          <div className="pm-live-reasoning-text custom-scrollbar">
            {thinkingPreview}
          </div>
        </div>
      ) : (
        <div className="pm-live-answer-lines" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
      )}
    </div>
  );
}

function formatLiveThinkingPreview(thinking: string): string {
  const cleaned = thinking.replace(/\r\n/g, "\n").trim();
  if (!cleaned) return "";
  const maxChars = 900;
  if (cleaned.length <= maxChars) return cleaned;
  return `...${cleaned.slice(cleaned.length - maxChars)}`;
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
    const block = raw.includes("\n") || Boolean(className);

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
      <code className="pm-inline-code" {...props}>
        {children}
      </code>
    );
  },
  pre({ children }) {
    return <>{children}</>;
  },
  table({ children }) {
    return (
      <div className="pm-table-scroll custom-scrollbar">
        <table>{children}</table>
      </div>
    );
  },
};

function toolNameFromTraceTitle(title: string): string | undefined {
  const match = title.match(/\b([a-zA-Z][a-zA-Z0-9_-]*)\s+tool\b/);
  return match?.[1];
}

function ProcessTimeline({
  items,
  isStreaming,
  defaultOpen = false,
}: {
  items: ProcessTimelineItem[];
  isStreaming: boolean;
  defaultOpen?: boolean;
}) {
  const groups = compactProcessTimeline(items);
  const [manualOpenIds, setManualOpenIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [manualClosedIds, setManualClosedIds] = useState<Set<string>>(
    () => new Set(),
  );
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
    setManualClosedIds((previous) => {
      const next = new Set(previous);
      if (open) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  return (
    <div className="mb-2 w-full max-w-[82ch]">
      {groups.map((group, index) => {
        const active = group.id === activeId;
        const open =
          active ||
          manualOpenIds.has(group.id) ||
          ((isStreaming || defaultOpen) && !manualClosedIds.has(group.id));
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
  const statusBadge = badgeForStatus(active ? "running" : group.status);

  useEffect(() => {
    if (!open || active) return;
    const node = bodyRef.current;
    if (!node) return;
    requestAnimationFrame(() => {
      node.scrollTop = 0;
    });
  }, [active, bodyRef, group.id, open]);

  return (
    <div
      className={`process-group w-full ${open ? "expanded" : ""} ${
        active ? "process-group-active" : ""
      }`}
    >
      <button
        type="button"
        className="process-group-header"
        aria-expanded={open}
        onClick={() => {
          if (!active) onToggle(!open);
        }}
      >
        <span className="disclosure-caret" aria-hidden="true" />
        <StatusBadge tag={group.kindLabel} tone={toneForTag(group.kindLabel)} />
        <span className="pm-process-title">{group.title}</span>
        {active ? <GeneratingIndicator label={group.kind === "gen" ? "THINKING" : "RUNNING"} /> : <StatusBadge {...statusBadge} />}
        <span className="pm-process-count">
          {String(index + 1).padStart(2, "0")} / {group.items.length}
        </span>
      </button>
      <div className={`collapsible ${open ? "expanded" : ""}`}>
        <div className="content">
          <div
            ref={bodyRef}
            className={`pm-process-body custom-scrollbar ${
              active ? "pm-process-body-live" : "pm-process-body-review"
            }`}
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
        </div>
      </div>
    </div>
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
  const modelCall = parseModelCallBlock(content);

  return (
    <div className="pm-process-row">
      <span className="pm-process-row-index">
        {String(index + 1).padStart(2, "0")}
      </span>
      <StatusBadge tag={rowLabel} tone={toneForTag(rowLabel)} />
      <div className="min-w-0 flex-1">
        <div className="pm-process-row-title">{item.title}</div>
        {modelCall ? (
          <ModelCallSummary data={modelCall} />
        ) : content && (
          <div className="pm-process-row-content custom-scrollbar">
            {content}
          </div>
        )}
      </div>
    </div>
  );
}

interface ModelCallSummaryData {
  name?: string;
  model?: string;
  status?: string;
  purpose?: string;
  duration?: string;
  detail: Record<string, string>;
}

function ModelCallSummary({ data }: { data: ModelCallSummaryData }) {
  const thinkingChars = data.detail.thinking_chars;
  const noReasoning = thinkingChars === "0";
  const entries = [
    ["Model", data.model],
    ["Status", data.status],
    ["Duration", data.duration ? `${data.duration}s` : undefined],
    ["Output", data.detail.content_chars ? `${data.detail.content_chars} chars` : undefined],
    ["Thinking", thinkingChars ? `${thinkingChars} chars` : undefined],
    ["Tools", data.detail.tool_calls],
  ].filter((entry): entry is [string, string] => Boolean(entry[1]));

  return (
    <div className="pm-model-call-summary">
      {entries.length > 0 && (
        <div className="pm-model-call-grid">
          {entries.map(([label, value]) => (
            <div key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </div>
      )}
      {data.purpose && <p>{data.purpose}</p>}
      {noReasoning && (
        <div className="pm-model-call-note">
          No separate reasoning text was emitted by this model for this step.
        </div>
      )}
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
        <StatusBadge
          tag={transcript.kind === "WEB" ? "WWW" : "EXE"}
          tone={transcript.kind === "WEB" ? "www" : "exe"}
        />
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

function parseModelCallBlock(text: string): ModelCallSummaryData | null {
  if (!text.includes("[Model API call]")) return null;
  const fields = parseKeyValueBlock(text);
  return {
    name: fields.name,
    model: fields.model,
    status: fields.status,
    purpose: fields.purpose,
    duration: fields.duration_s,
    detail: parseInlineKeyValueBlock(fields.detail || ""),
  };
}

function parseInlineKeyValueBlock(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of text.split(/\s+/)) {
    const match = /^([a-zA-Z_]+)=([^=]+)$/.exec(part.trim());
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
      kindLabel: kind === "setup" ? "USE" : kind === "gen" ? "GEN" : kind === "warn" ? "WRN" : "EXE",
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

    if (item.kind === "reasoning") {
      ensure("gen", "Reasoning trace").items.push(item);
      continue;
    }

    if (isModelTrace(item)) {
      ensure(
        "gen",
        item.title.includes("final") ? "Final synthesis" : "Model activity",
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
    <div
      className={`process-group mb-2 w-full max-w-[82ch] ${
        open ? "expanded" : ""
      } ${active ? "process-group-active" : ""}`}
    >
      <button
        type="button"
        className="process-group-header"
        aria-expanded={open}
        onClick={() => onToggle(!open)}
      >
        <span className="disclosure-caret" aria-hidden="true" />
        <StatusBadge tag="INF" tone="inf" />
        <span className="pm-process-title">Trace log</span>
        {active ? <GeneratingIndicator /> : <StatusBadge tag="RES" tone="res" />}
        <span className="ml-auto text-content-tertiary/60">
          {open ? "Collapse" : "Expand"}
        </span>
      </button>
      <div className={`collapsible ${open ? "expanded" : ""}`}>
        <div className="content">
          <div
            ref={bodyRef}
            className={`pm-live-scroll-panel mt-2 overflow-y-auto custom-scrollbar bg-bg-base p-3 font-mono text-[12px] leading-6 text-content-secondary transition-none rounded-none ${
              active ? "pm-live-scroll-panel-live" : "pm-live-scroll-panel-review"
            }`}
          >
            <div className="space-y-2">
              {active && (
                <div className="flex items-center gap-2 text-[9px] font-bold uppercase tracking-widest text-accent-main">
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
        </div>
      </div>
    </div>
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
    <div
      className={`process-group mb-2 w-full max-w-[82ch] ${
        open ? "expanded" : ""
      } ${active ? "process-group-active" : ""}`}
    >
      <button
        type="button"
        className="process-group-header"
        aria-expanded={open}
        onClick={() => onToggle(!open)}
      >
        <span className="disclosure-caret" aria-hidden="true" />
        <StatusBadge tag="GEN" tone="gen" />
        <span className="pm-process-title">Reasoning trace</span>
        {active ? <GeneratingIndicator label="THINKING" /> : <StatusBadge tag="RES" tone="res" />}
        <span className="ml-auto text-content-tertiary/60">
          {open ? "Collapse" : "Expand"}
        </span>
      </button>
      <div className={`collapsible ${open ? "expanded" : ""}`}>
        <div className="content">
          <div
            ref={bodyRef}
            className={`pm-live-scroll-panel mt-2 overflow-y-auto custom-scrollbar bg-bg-base px-3 py-2 font-mono text-[12px] leading-6 text-content-secondary transition-none rounded-none whitespace-pre-wrap break-words ${
              active ? "pm-live-scroll-panel-live" : "pm-live-scroll-panel-review"
            }`}
          >
            {thinking}
          </div>
        </div>
      </div>
    </div>
  );
}

type StatusTone =
  | "gen"
  | "use"
  | "exe"
  | "www"
  | "res"
  | "wrn"
  | "err"
  | "inf";

function StatusBadge({ tag, tone }: { tag: string; tone: StatusTone }) {
  return (
    <span className={`status-badge status-badge-${tone}`}>
      {`<${tag}>`}
    </span>
  );
}

function toneForTag(tag: string): StatusTone {
  const normalized = tag.toLowerCase();
  if (normalized === "gen") return "gen";
  if (normalized === "use") return "use";
  if (normalized === "exe") return "exe";
  if (normalized === "www" || normalized === "web") return "www";
  if (normalized === "res" || normalized === "done") return "res";
  if (normalized === "wrn" || normalized === "warn") return "wrn";
  if (normalized === "err" || normalized === "error") return "err";
  return "inf";
}

function badgeForStatus(status: string | undefined): {
  tag: string;
  tone: StatusTone;
} {
  if (status === "error") return { tag: "ERR", tone: "err" };
  if (status === "skipped") return { tag: "WRN", tone: "wrn" };
  if (status === "running") return { tag: "GEN", tone: "gen" };
  return { tag: "RES", tone: "res" };
}

function GeneratingIndicator({ label = "GENERATING" }: { label?: string }) {
  const upper = label.toUpperCase();
  const tag = upper.includes("RUN") ? "EXE" : "GEN";
  return <StatusBadge tag={tag} tone={tag === "EXE" ? "exe" : "gen"} />;
}

function TraceEventRow({ event }: { event: TraceEvent }) {
  const statusBadge = badgeForStatus(event.status);

  return (
    <div className="pl-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[9px] font-bold uppercase tracking-widest">
        <StatusBadge {...statusBadge} />
        <span className="text-content-primary">{event.title}</span>
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
    <div
      className={`process-group mb-2 w-full max-w-[82ch] ${
        open ? "expanded" : ""
      } ${running ? "process-group-active" : ""}`}
    >
      <button
        type="button"
        className="process-group-header"
        aria-expanded={open}
        onClick={() => onToggle(!open)}
      >
        <span className="disclosure-caret" aria-hidden="true" />
        <StatusBadge tag="EXE" tone="exe" />
        <span className="pm-process-title">Tool activity</span>
        {running ? <GeneratingIndicator label="RUNNING" /> : <StatusBadge tag="RES" tone="res" />}
        <span className="ml-auto text-content-tertiary/60">
          {open ? "Collapse" : "Expand"}
        </span>
      </button>
      <div className={`collapsible ${open ? "expanded" : ""}`}>
        <div className="content">
          <div
            ref={bodyRef}
            className={`pm-live-scroll-panel mt-2 overflow-y-auto custom-scrollbar bg-bg-base px-3 py-2 font-mono text-[12px] leading-6 text-content-secondary transition-none rounded-none ${
              running ? "pm-live-scroll-panel-live" : "pm-live-scroll-panel-review"
            }`}
          >
            <div className="space-y-1">
              {activities.map((activity) => {
                const isRunning = activity.status === "running";
                const toolTone = activity.name === "web_search" ? "www" : "exe";
                const toolTag = activity.name === "web_search" ? "WWW" : "EXE";
                return (
                  <div
                    key={activity.id}
                    className="text-content-secondary"
                  >
                    <div className="flex items-center gap-2">
                      <StatusBadge tag={toolTag} tone={toolTone} />
                      <StatusBadge
                        tag={isRunning ? "GEN" : "RES"}
                        tone={isRunning ? "gen" : "res"}
                      />
                      <span className="font-bold uppercase tracking-wider text-content-primary">
                        {formatToolName(activity.name)}
                      </span>
                      <span className="text-content-tertiary">
                        {isRunning ? "running" : "complete"}
                      </span>
                    </div>
                    {activity.detail && (
                      <div className="pm-tool-detail ml-10 mt-1 overflow-y-auto custom-scrollbar break-all bg-bg-surface p-2 text-[11px] leading-5 text-content-tertiary">
                        {activity.detail}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </div>
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
