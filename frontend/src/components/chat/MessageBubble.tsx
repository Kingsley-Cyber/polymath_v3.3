// MessageBubble.tsx - Message display component with trimming indicators
import { useState } from "react";
import ReactMarkdown from "react-markdown";
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
import type { TraceEvent } from "../../types";
import type { StreamingToolActivity } from "../../stores/chatStore";
import { RetrievalBadge } from "./RetrievalBadge";

interface MessageBubbleProps {
  message: ChatMessage;
  isStreaming?: boolean;
  toolActivity?: StreamingToolActivity[];
}

export function MessageBubble({
  message,
  isStreaming = false,
  toolActivity = [],
}: MessageBubbleProps) {
  const [copied, setCopied] = useState(false);
  const [thinkingOpen, setThinkingOpen] = useState(true);
  const [traceOpen, setTraceOpen] = useState(true);
  const isUser = message.role === "user";

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
        {!isUser && message.trace_events && message.trace_events.length > 0 && (
          <TracePanel
            events={message.trace_events}
            open={traceOpen}
            isStreaming={isStreaming}
            onToggle={setTraceOpen}
          />
        )}

        {/* Thinking Block */}
        {message.thinking && (
          <details
            open={thinkingOpen}
            onToggle={(e) =>
              setThinkingOpen((e.target as HTMLDetailsElement).open)
            }
            className="mb-2 group/thinking"
          >
            <summary className="flex items-center gap-2 px-3 py-1.5 text-[10px] font-bold tracking-widest uppercase text-content-tertiary bg-bg-surface border border-border-minimal transition-none rounded-none cursor-pointer hover:text-content-primary hover:border-content-tertiary select-none list-none">
              <Brain className="w-3.5 h-3.5" />
              <span>[REASONING_TRACE]</span>
              <span className="ml-auto text-content-tertiary/60 group-hover/thinking:text-content-secondary">
                {thinkingOpen ? "COLLAPSE [-]" : "EXPAND [+]"}
              </span>
            </summary>
            <div className="mt-1 px-3 py-2 text-[11px] font-mono text-content-secondary bg-bg-base border border-border-minimal transition-none rounded-none whitespace-pre-wrap break-words max-h-60 overflow-y-auto custom-scrollbar">
              {message.thinking}
            </div>
          </details>
        )}

        {!isUser && toolActivity.length > 0 && (
          <ToolActivityPanel activities={toolActivity} />
        )}

        {/* Bubble */}
        <div
          className={`
            relative group/bubble
            ${isUser ? "message-user" : "message-assistant"}
          `}
        >
          {/* Content with Markdown and brutalist formatting */}
          <div className="break-words [&_p]:my-2 [&_ul]:list-square [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_li]:my-1 [&_code]:bg-bg-base [&_code]:text-accent-main [&_code]:border [&_code]:border-border-minimal [&_code]:px-1 [&_code]:py-0.5 [&_pre]:p-3 [&_pre]:bg-bg-base [&_pre]:border [&_pre]:border-border-minimal [&_pre]:overflow-x-auto [&_pre_code]:bg-transparent [&_pre_code]:border-none [&_pre_code]:p-0 [&_pre_code]:text-content-secondary [&_a]:text-accent-secondary [&_a]:underline [&_h1]:text-lg [&_h1]:font-bold [&_h1]:uppercase [&_h2]:text-md [&_h2]:font-bold [&_h3]:font-bold">
            {isUser ? (
              <span className="whitespace-pre-wrap">{message.content}</span>
            ) : (
              <ReactMarkdown>{message.content}</ReactMarkdown>
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
  const active =
    isStreaming && events.some((event) => event.status === "running");

  return (
    <details
      open={open}
      onToggle={(e) => onToggle((e.target as HTMLDetailsElement).open)}
      className="mb-2 w-full max-w-3xl group/trace"
    >
      <summary className="flex items-center gap-2 px-3 py-1.5 text-[10px] font-bold tracking-widest uppercase text-content-tertiary bg-bg-surface border border-border-minimal transition-none rounded-none cursor-pointer hover:text-content-primary hover:border-content-tertiary select-none list-none">
        <TerminalSquare className="w-3.5 h-3.5" />
        <span>[TRACE_LOG]</span>
        <span className="text-content-tertiary/70">
          {active ? "RUNNING" : "DONE"}
        </span>
        <span className="ml-auto text-content-tertiary/60 group-hover/trace:text-content-secondary">
          {open ? "COLLAPSE [-]" : "EXPAND [+]"}
        </span>
      </summary>
      <div className="mt-1 max-h-72 overflow-y-auto custom-scrollbar border border-border-minimal bg-bg-base p-2 font-mono text-[10px] text-content-secondary transition-none rounded-none">
        <div className="space-y-2">
          {events.map((event) => (
            <TraceEventRow key={event.id} event={event} />
          ))}
        </div>
      </div>
    </details>
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
}: {
  activities: StreamingToolActivity[];
}) {
  return (
    <div className="mb-2 w-full max-w-xl border border-border-minimal bg-bg-base px-3 py-2 font-mono text-[10px] text-content-secondary transition-none rounded-none">
      <div className="mb-1.5 flex items-center justify-between gap-3 text-[9px] font-bold uppercase tracking-widest text-content-tertiary">
        <span>[TOOL_ACTIVITY]</span>
        <span>{activities.some((a) => a.status === "running") ? "RUNNING" : "DONE"}</span>
      </div>
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
                <div className="ml-10 mt-0.5 break-words text-[9px] text-content-tertiary">
                  {activity.detail}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
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
