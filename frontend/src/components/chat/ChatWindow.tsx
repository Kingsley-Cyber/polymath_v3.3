// ChatWindow.tsx - Main chat display area with message list
import { useEffect, useRef } from "react";
import { MessageBubble } from "./MessageBubble";
import { useChatStore } from "../../stores/chatStore";
import { Sparkles } from "lucide-react";

export function ChatWindow() {
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

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
  if (conversationMessages.length === 0 && !isLoading) {
    return (
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="text-center max-w-md">
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
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-left">
            <ExamplePrompt text="Summarize the key findings from my research papers" />
            <ExamplePrompt text="What are the dependencies in the codebase?" />
            <ExamplePrompt text="Compare the April and May reports" />
            <ExamplePrompt text="Find all references to authentication logic" />
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
        {isStreaming &&
          (streamingContent ||
            streamingThinking ||
            streamingTraceEvents.length > 0 ||
            streamingToolActivity.length > 0 ||
            streamingProcessTimeline.length > 0) && (
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

        {/* Loading indicator */}
        {isLoading && !isStreaming && (
          <div className="flex items-center gap-2 px-4 py-6">
            <div className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
            <div className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
            <div className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>
    </div>
  );
}

function ExamplePrompt({ text }: { text: string }) {
  return (
    <button className="p-3 text-sm text-content-secondary bg-bg-surface border border-border-minimal rounded-xl hover:border-accent-main hover:text-accent-main transition-colors text-left">
      &ldquo;{text}&rdquo;
    </button>
  );
}
