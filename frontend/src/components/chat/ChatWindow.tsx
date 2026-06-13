// ChatWindow.tsx - Main chat display area with message list
import { useEffect, useMemo, useRef, useState } from "react";
import { MessageBubble } from "./MessageBubble";
import { useChatStore } from "../../stores/chatStore";
import { Sparkles, Shuffle } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore";
import * as api from "../../lib/api";
import type { GraphSuggestionItem } from "../../types/discover";

type PromptSuggestion = {
  text: string;
  kind: string;
  entities?: string[];
};

export function ChatWindow() {
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const selectedCorpusIds = useSettingsStore((s) => s.selectedCorpusIds);
  const setPendingPrompt = useChatStore((s) => s.setPendingPrompt);
  const [graphSuggestions, setGraphSuggestions] = useState<GraphSuggestionItem[]>([]);
  const [suggestionsLoading, setSuggestionsLoading] = useState(false);
  const [suggestionOffset, setSuggestionOffset] = useState(0);

  const {
    activeConversationId,
    messages,
    isStreaming,
    streamingContent,
    streamingThinking,
    streamingTraceEvents,
    streamingToolActivity,
    streamingProcessTimeline,
    isLoading,
  } = useChatStore();

  const conversationMessages = activeConversationId
    ? messages[activeConversationId] || []
    : [];
  const isEmptyState = conversationMessages.length === 0 && !isLoading;
  const corpusKey = selectedCorpusIds.slice().sort().join(",");

  // Has the streaming response produced anything renderable yet?
  const hasStreamingOutput =
    !!streamingContent ||
    !!streamingThinking ||
    streamingTraceEvents.length > 0 ||
    streamingToolActivity.length > 0 ||
    streamingProcessTimeline.length > 0;
  // The request is in flight but nothing has rendered yet — covers BOTH the
  // pre-stream retrieval phase (isLoading) AND the gap after the stream opens
  // but before the first token (isStreaming && no output). Without this second
  // case the UI went blank for several seconds, then the answer "spawned".
  const showWorkingIndicator = (isLoading || isStreaming) && !hasStreamingOutput;
  const workingLabel = isStreaming
    ? "Generating answer…"
    : "Searching your library…";

  useEffect(() => {
    let cancelled = false;
    setGraphSuggestions([]);
    if (!corpusKey) {
      setSuggestionsLoading(false);
      return;
    }
    setSuggestionsLoading(true);
    api
      .getGraphSuggestions(selectedCorpusIds)
      .then((res) => {
        if (cancelled) return;
        setGraphSuggestions(res.suggestions || []);
      })
      .catch(() => {
        if (cancelled) return;
        setGraphSuggestions([]);
      })
      .finally(() => {
        if (!cancelled) setSuggestionsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [corpusKey, selectedCorpusIds]);

  const promptPool = useMemo<PromptSuggestion[]>(() => {
    const fromGraph = graphSuggestions
      .map((s) => ({
        text: String(s.text || "").trim(),
        kind: String(s.kind || "graph"),
        entities: s.entities || [],
      }))
      .filter((s) => s.text.length > 12);

    if (fromGraph.length > 0) return fromGraph;

    if (selectedCorpusIds.length > 0) {
      return [
        {
          kind: "research",
          text: "What are the strongest claims across this corpus, and what evidence supports them?",
        },
        {
          kind: "nuance",
          text: "Where do these documents disagree, create tension, or define the same idea differently?",
        },
        {
          kind: "ideation",
          text: "What original project or research direction could be built from the patterns in this corpus?",
        },
        {
          kind: "graph",
          text: "Which concepts act as bridges between otherwise separate parts of this corpus?",
        },
        {
          kind: "gap",
          text: "What should this corpus connect but doesn't — which related ideas are never linked?",
        },
        {
          kind: "audit",
          text: "What important question is this corpus prepared to answer better than a normal web search?",
        },
        {
          kind: "synthesis",
          text: "Build a compact mental model of the corpus using its key entities, tensions, and examples.",
        },
      ];
    }

    return [
      { kind: "research", text: "Summarize the key findings from my research papers." },
      { kind: "graph", text: "What are the dependencies in the codebase?" },
      { kind: "compare", text: "Compare the April and May reports." },
      { kind: "search", text: "Find all references to authentication logic." },
      { kind: "ideation", text: "Give me three research directions worth exploring next." },
      { kind: "nuance", text: "What is the strongest counterargument to the main claim?" },
    ];
  }, [graphSuggestions, selectedCorpusIds.length]);

  const visiblePrompts = useMemo(() => {
    return promptPool
      .map((item) => ({
        item,
        score: stablePromptScore(item.text, suggestionOffset),
      }))
      .sort((a, b) => a.score - b.score)
      .slice(0, 4)
      .map((x) => x.item);
  }, [promptPool, suggestionOffset]);

  useEffect(() => {
    if (!isEmptyState || promptPool.length <= 4) return;
    const id = window.setInterval(() => {
      setSuggestionOffset((n) => n + 1);
    }, 18000);
    return () => window.clearInterval(id);
  }, [isEmptyState, promptPool.length]);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    requestAnimationFrame(() => {
      messagesEndRef.current?.scrollIntoView({
        behavior: isStreaming ? "auto" : "smooth",
        block: "end",
      });
    });
  }, [
    conversationMessages,
    isStreaming,
    streamingContent,
    streamingThinking,
    streamingTraceEvents,
    streamingToolActivity,
    streamingProcessTimeline,
  ]);

  // Empty state
  if (isEmptyState) {
    return (
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="text-center max-w-2xl">
          <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-primary/10 flex items-center justify-center">
            <Sparkles className="w-8 h-8 text-primary" />
          </div>
          <h2 className="text-xl font-semibold text-text-primary mb-2">
            Welcome to Polymath
          </h2>
          <p className="text-sm text-text-secondary mb-6">
            Your hierarchical RAG assistant. Select knowledge collections above,
            toggle HyDE reasoning or Graph traversal, and start asking questions.
          </p>
          <div className="mb-3 flex items-center justify-center gap-2 text-[10px] uppercase tracking-widest text-content-tertiary font-mono">
            <span>
              {selectedCorpusIds.length > 0
                ? suggestionsLoading
                  ? "building corpus questions"
                  : "corpus-aware questions"
                : "starter questions"}
            </span>
            <button
              type="button"
              onClick={() => setSuggestionOffset((n) => n + 1)}
              className="inline-flex items-center gap-1 rounded border border-border-minimal px-2 py-1 text-content-secondary hover:border-accent-main hover:text-accent-main"
              title="Shuffle questions"
            >
              <Shuffle className="h-3 w-3" />
              shuffle
            </button>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-left">
            {visiblePrompts.map((item) => (
              <ExamplePrompt
                key={`${item.kind}:${item.text}`}
                item={item}
                onPick={(text) => setPendingPrompt(text)}
              />
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      data-testid="response-panel"
      className="flex-1 overflow-y-auto custom-scrollbar bg-[var(--color-chat-background)]"
    >
      <div className="message-list w-full py-4">
        {conversationMessages.map((message, index) => (
          <MessageBubble
            key={message.id || index}
            message={message}
            isStreaming={false}
          />
        ))}

        {/* Streaming message */}
        {isStreaming && hasStreamingOutput && (
          <MessageBubble
            message={{
              id: "streaming",
              role: "assistant",
              content: streamingContent,
              thinking: streamingThinking || undefined,
              trace_events: streamingTraceEvents,
              process_timeline: streamingProcessTimeline,
              created_at: new Date().toISOString(),
            }}
            isStreaming={true}
            toolActivity={streamingToolActivity}
          />
        )}

        {/* Working indicator — stays visible from submit through the
            time-to-first-token gap so the UI is never blank mid-query. */}
        {showWorkingIndicator && (
          <div className="flex items-center gap-2.5 px-4 py-6" aria-live="polite">
            <div className="flex items-center gap-1">
              <div className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
              <div className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
              <div className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
            </div>
            <span className="text-[12px] text-content-tertiary">{workingLabel}</span>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>
    </div>
  );
}

function stablePromptScore(text: string, offset: number): number {
  let h = 2166136261 ^ offset;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function ExamplePrompt({
  item,
  onPick,
}: {
  item: PromptSuggestion;
  onPick: (text: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onPick(item.text)}
      className="group p-3 text-sm text-content-secondary bg-bg-surface border border-border-minimal rounded-xl hover:border-accent-main hover:text-accent-main transition-colors text-left"
    >
      <span className="mb-1 block text-[9px] uppercase tracking-widest text-content-tertiary font-mono group-hover:text-accent-main">
        {item.kind}
      </span>
      &ldquo;{item.text}&rdquo;
    </button>
  );
}
