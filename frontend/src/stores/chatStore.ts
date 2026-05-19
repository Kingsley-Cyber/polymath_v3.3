// Chat Store - Zustand state management for conversations and messages
import { create } from "zustand";
import type {
  Conversation,
  ChatMessage,
  CorpusResponse,
  ProcessTimelineItem,
  SourceChunk,
  TraceEvent,
} from "../types";

export interface StreamingToolActivity {
  id: string;
  name: string;
  status: "running" | "done";
  detail?: string;
}

interface ChatState {
  // Conversations
  conversations: Conversation[];
  activeConversationId: string | null;

  // Messages (keyed by conversationId)
  messages: Record<string, ChatMessage[]>;

  // Corpus selection
  selectedCorpusIds: string[];
  corpora: CorpusResponse[];

  // UI State
  isStreaming: boolean;
  isLoading: boolean;
  error: string | null;
  streamingContent: string;
  streamingThinking: string;
  streamingTraceEvents: TraceEvent[];
  streamingToolActivity: StreamingToolActivity[];
  streamingProcessTimeline: ProcessTimelineItem[];
  /** Sources captured from the SSE `sources` frame during a stream.
   *  Reset on each `startStreaming`; consumed when finalizing the
   *  assistant message so the RetrievalBadge expand panel has chunks. */
  streamingSources: SourceChunk[];

  // Token budget telemetry — updated from SSE `budget` frame on every send
  tokensUsed: number | null;
  tokensMax: number | null;

  // Actions
  setConversations: (conversations: Conversation[]) => void;
  addConversation: (conversation: Conversation) => void;
  updateConversation: (id: string, updates: Partial<Conversation>) => void;
  deleteConversation: (id: string) => void;
  setActiveConversation: (id: string | null) => void;

  // Message actions
  setMessages: (conversationId: string, messages: ChatMessage[]) => void;
  addMessage: (conversationId: string, message: ChatMessage) => void;
  updateStreamingContent: (content: string) => void;
  updateStreamingThinking: (thinking: string) => void;
  addStreamingTraceEvent: (event: TraceEvent) => void;
  addStreamingToolActivity: (activity: StreamingToolActivity) => void;
  completeStreamingToolActivity: (name: string, detail?: string) => void;
  setStreamingSources: (sources: SourceChunk[]) => void;
  finalizeStreamingMessage: (
    conversationId: string,
    message: ChatMessage,
  ) => void;

  // Corpus actions
  setCorpora: (corpora: CorpusResponse[]) => void;
  setSelectedCorpusIds: (ids: string[]) => void;
  toggleCorpusId: (id: string) => void;

  // Streaming state
  startStreaming: () => void;
  stopStreaming: () => void;
  clearStreamingContent: () => void;
  setTokenBudget: (used: number, max: number) => void;

  // Error handling
  setError: (error: string | null) => void;
  clearError: () => void;

  // Pending prompt — populated by GraphView "→ Ask Chat" handoff;
  // ChatInput consumes it on mount/update, then calls clearPendingPrompt().
  pendingPrompt: string | null;
  setPendingPrompt: (text: string) => void;
  clearPendingPrompt: () => void;
}

export const useChatStore = create<ChatState>()((set) => ({
  // Initial state
  conversations: [],
  activeConversationId: null,
  messages: {},
  selectedCorpusIds: [],
  corpora: [],
  isStreaming: false,
  isLoading: false,
  error: null,
  streamingContent: "",
  streamingThinking: "",
  streamingTraceEvents: [],
  streamingToolActivity: [],
  streamingProcessTimeline: [],
  streamingSources: [],
  tokensUsed: null,
  tokensMax: null,

  // Conversation actions
  setConversations: (conversations) => set({ conversations }),

  addConversation: (conversation) =>
    set((state) => ({
      conversations: [conversation, ...state.conversations],
      activeConversationId: conversation.id,
      messages: { ...state.messages, [conversation.id]: [] },
    })),

  updateConversation: (id, updates) =>
    set((state) => ({
      conversations: state.conversations.map((conv) =>
        conv.id === id ? { ...conv, ...updates } : conv,
      ),
    })),

  deleteConversation: (id) =>
    set((state) => {
      const newMessages = { ...state.messages };
      delete newMessages[id];
      return {
        conversations: state.conversations.filter((conv) => conv.id !== id),
        messages: newMessages,
        activeConversationId:
          state.activeConversationId === id
            ? state.conversations.find((c) => c.id !== id)?.id || null
            : state.activeConversationId,
      };
    }),

  setActiveConversation: (id) => set({ activeConversationId: id }),

  // Message actions
  setMessages: (conversationId, messages) =>
    set((state) => ({
      messages: { ...state.messages, [conversationId]: messages },
    })),

  addMessage: (conversationId, message) =>
    set((state) => ({
      messages: {
        ...state.messages,
        [conversationId]: [...(state.messages[conversationId] || []), message],
      },
    })),

  updateStreamingContent: (content) =>
    set((state) => ({
      streamingContent: state.streamingContent + content,
    })),

  updateStreamingThinking: (thinking) =>
    set((state) => {
      const timeline = [...state.streamingProcessTimeline];
      const last = timeline[timeline.length - 1];
      if (last?.kind === "reasoning" && last.status === "running") {
        timeline[timeline.length - 1] = {
          ...last,
          content: `${last.content || ""}${thinking}`,
        };
      } else {
        for (let i = 0; i < timeline.length; i += 1) {
          if (timeline[i].status === "running") {
            timeline[i] = { ...timeline[i], status: "done" };
          }
        }
        timeline.push({
          id: `reasoning-${Date.now()}-${timeline.length}`,
          kind: "reasoning",
          title: "Reasoning trace",
          status: "running",
          content: thinking,
          timestamp: new Date().toISOString(),
        });
      }
      return {
        streamingThinking: state.streamingThinking + thinking,
        streamingProcessTimeline: timeline,
      };
    }),

  addStreamingTraceEvent: (event) =>
    set((state) => {
      const timeline = state.streamingProcessTimeline.map((item) =>
        item.status === "running"
          ? { ...item, status: "done" }
          : item,
      );
      timeline.push({
        id: `trace-${event.id}`,
        kind: "trace",
        title: event.title,
        status: event.status,
        content: event.content,
        timestamp: event.timestamp,
        metadata: event.metadata,
      });
      return {
        streamingTraceEvents: [...state.streamingTraceEvents, event],
        streamingProcessTimeline: timeline,
      };
    }),

  addStreamingToolActivity: (activity) =>
    set((state) => {
      const timeline = state.streamingProcessTimeline.map((item) =>
        item.status === "running" ? { ...item, status: "done" } : item,
      );
      timeline.push({
        id: activity.id,
        kind: "tool",
        title: formatToolActivityTitle(activity.name),
        status: "running",
        detail: activity.detail,
        timestamp: new Date().toISOString(),
      });
      return {
        streamingToolActivity: [...state.streamingToolActivity, activity],
        streamingProcessTimeline: timeline,
      };
    }),

  completeStreamingToolActivity: (name, detail) =>
    set((state) => {
      const targetIndex = state.streamingToolActivity.findIndex(
        (activity) => activity.name === name && activity.status === "running",
      );

      if (targetIndex === -1) {
        const id = `${name}-${Date.now()}`;
        return {
          streamingToolActivity: [
            ...state.streamingToolActivity,
            {
              id,
              name,
              status: "done",
              detail,
            },
          ],
          streamingProcessTimeline: [
            ...state.streamingProcessTimeline,
            {
              id,
              kind: "tool",
              title: formatToolActivityTitle(name),
              status: "done",
              detail,
              timestamp: new Date().toISOString(),
            },
          ],
        };
      }

      return {
        streamingToolActivity: state.streamingToolActivity.map(
          (activity, index) =>
            index === targetIndex
              ? {
                  ...activity,
                  status: "done",
                  detail: mergeToolDetails(activity.detail, detail),
                }
              : activity,
        ),
        streamingProcessTimeline: state.streamingProcessTimeline.map((item) =>
          item.id === state.streamingToolActivity[targetIndex].id
            ? { ...item, status: "done", detail: mergeToolDetails(item.detail, detail) }
            : item,
        ),
      };
    }),

  setStreamingSources: (sources) => set({ streamingSources: sources }),

  finalizeStreamingMessage: (conversationId, message) =>
    set((state) => ({
      isStreaming: false,
      streamingContent: "",
      streamingThinking: "",
      streamingTraceEvents: [],
      streamingToolActivity: [],
      streamingProcessTimeline: [],
      streamingSources: [],
      messages: {
        ...state.messages,
        [conversationId]: [...(state.messages[conversationId] || []), message],
      },
    })),

  // Corpus actions
  setCorpora: (corpora) => set({ corpora }),

  setSelectedCorpusIds: (ids) => set({ selectedCorpusIds: ids }),

  toggleCorpusId: (id) =>
    set((state) => ({
      selectedCorpusIds: state.selectedCorpusIds.includes(id)
        ? state.selectedCorpusIds.filter((cid) => cid !== id)
        : [...state.selectedCorpusIds, id],
    })),

  // Streaming state
  startStreaming: () =>
    set({
      isStreaming: true,
      isLoading: true,
      streamingContent: "",
      streamingThinking: "",
      streamingTraceEvents: [],
      streamingToolActivity: [],
      streamingProcessTimeline: [],
      streamingSources: [],
    }),

  stopStreaming: () =>
    set({
      isStreaming: false,
      isLoading: false,
      streamingContent: "",
      streamingThinking: "",
      streamingTraceEvents: [],
      streamingToolActivity: [],
      streamingProcessTimeline: [],
      streamingSources: [],
    }),

  clearStreamingContent: () => set({ streamingContent: "" }),

  setTokenBudget: (used, max) => set({ tokensUsed: used, tokensMax: max }),

  // Error handling
  setError: (error) => set({ error }),
  clearError: () => set({ error: null }),

  // Pending prompt (graph handoff)
  pendingPrompt: null,
  setPendingPrompt: (text) => set({ pendingPrompt: text }),
  clearPendingPrompt: () => set({ pendingPrompt: null }),
}));

function formatToolActivityTitle(name: string): string {
  if (name === "web_search") return "Web search";
  if (name === "fetch_page") return "Fetch page";
  return name.replace(/_/g, " ");
}

function mergeToolDetails(
  before: string | undefined,
  after: string | undefined,
): string | undefined {
  const parts = [
    before ? `request\n${before}` : "",
    after ? `result\n${after}` : "",
  ].filter(Boolean);
  return parts.join("\n\n") || undefined;
}
