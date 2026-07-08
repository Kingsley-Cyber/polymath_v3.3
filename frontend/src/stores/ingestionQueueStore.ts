// ingestionQueueStore.ts — Phase F
// Tracks every in-flight ingestion job. Each entry opens one SSE connection
// to /api/ingestion/jobs/{doc_id}/stream and updates its row as events arrive.
// State persists to localStorage so refresh keeps the dashboard populated.
import { create } from "zustand";
import { persist } from "zustand/middleware";
import * as api from "../lib/api";
import type { IngestJobStage, IngestJobStatus } from "../types/corpus";

export type IngestionStage = IngestJobStage;

export interface IngestionJob {
  doc_id: string;
  filename: string;
  corpus_id: string;
  corpus_name: string;
  stage: IngestionStage;
  status: IngestJobStatus;
  chunk_count: number;
  parent_count: number;
  verified: boolean | null;
  warnings: string[];
  verify_errors: string[];
  error: string | null;
  started_at: number;
  updated_at: number;
}

interface IngestionQueueStore {
  jobs: Record<string, IngestionJob>;
  // Transient — not persisted
  _streams: Record<string, AbortController>;

  enqueue: (init: {
    doc_id: string;
    filename: string;
    corpus_id: string;
    corpus_name: string;
  }) => void;
  updateJob: (doc_id: string, patch: Partial<IngestionJob>) => void;
  removeJob: (doc_id: string) => void;
  clearFinished: () => void;

  /** Start (or resume) an SSE connection for a job. No-op if one is live. */
  watchJob: (doc_id: string) => void;
  stopWatch: (doc_id: string) => void;

  /** Call on app mount to re-open streams for jobs still marked processing. */
  resumeAllStreams: () => void;

  /** Derived helpers */
  activeJobs: () => IngestionJob[];
  recentCompleted: () => IngestionJob[];
}

const MAX_RETAINED = 50;

export const useIngestionQueueStore = create<IngestionQueueStore>()(
  persist(
    (set, get) => ({
      jobs: {},
      _streams: {},

      enqueue: ({ doc_id, filename, corpus_id, corpus_name }) => {
        const now = Date.now();
        set((state) => {
          const next = { ...state.jobs };
          next[doc_id] = {
            doc_id,
            filename,
            corpus_id,
            corpus_name,
            stage: "uploading",
            status: "processing",
            chunk_count: 0,
            parent_count: 0,
            verified: null,
            warnings: [],
            verify_errors: [],
            error: null,
            started_at: now,
            updated_at: now,
          };
          // Retain cap — drop oldest finished/failed
          const entries = Object.values(next);
          if (entries.length > MAX_RETAINED) {
            const sorted = entries
              .filter((j) => j.status !== "processing")
              .sort((a, b) => a.updated_at - b.updated_at);
            for (const old of sorted.slice(0, entries.length - MAX_RETAINED)) {
              delete next[old.doc_id];
            }
          }
          return { jobs: next };
        });
        // Kick off the stream immediately
        get().watchJob(doc_id);
      },

      updateJob: (doc_id, patch) =>
        set((state) => {
          const existing = state.jobs[doc_id];
          if (!existing) return {};
          return {
            jobs: {
              ...state.jobs,
              [doc_id]: { ...existing, ...patch, updated_at: Date.now() },
            },
          };
        }),

      removeJob: (doc_id) => {
        get().stopWatch(doc_id);
        set((state) => {
          const { [doc_id]: _, ...rest } = state.jobs;
          return { jobs: rest };
        });
      },

      clearFinished: () =>
        set((state) => {
          const next: Record<string, IngestionJob> = {};
          for (const [k, v] of Object.entries(state.jobs)) {
            if (v.status === "processing") next[k] = v;
          }
          return { jobs: next };
        }),

      watchJob: (doc_id) => {
        const store = get();
        if (store._streams[doc_id]) return; // already watching
        const ctrl = new AbortController();
        set((state) => ({ _streams: { ...state._streams, [doc_id]: ctrl } }));

        // Auto-reconnect loop: SSE streams get aborted by the browser when
        // the tab backgrounds. We MUST NOT mark the job as failed in that
        // case — the backend worker is still running fine. Instead we
        // reconnect with a small backoff. Only an explicit stopWatch()
        // (user removed the row) or a terminal status from the server
        // ends the loop.
        let reconnectAttempt = 0;
        const MAX_BACKOFF_MS = 10_000;

        (async () => {
          try {
            while (!ctrl.signal.aborted) {
              let sawTerminal = false;
              try {
                const activeJob = get().jobs[doc_id];
                for await (const evt of api.streamIngestionJob(doc_id, activeJob?.corpus_id)) {
                  if (ctrl.signal.aborted) return;
                  reconnectAttempt = 0; // reset on each successful event
                  store.updateJob(doc_id, {
                    stage: evt.stage,
                    status: evt.status,
                    chunk_count: evt.chunk_count,
                    parent_count: evt.parent_count,
                    verified: evt.write_state.verified,
                    warnings: evt.write_state.warnings ?? [],
                    verify_errors: evt.write_state.verify_errors,
                    error: evt.error,
                  });
                  if (
                    evt.status === "done" ||
                    evt.status === "failed" ||
                    evt.status === "skipped_duplicate" ||
                    evt.status === "awaiting_summary" ||
                    String(evt.status).startsWith("queryable_with_pending_")
                  ) {
                    sawTerminal = true;
                    break;
                  }
                }
              } catch (err) {
                if (ctrl.signal.aborted) return;
                // Network error / browser-throttled abort — treat as
                // transient disconnect, NOT as a pipeline failure.
                console.warn(
                  `SSE disconnect for ${doc_id} (attempt ${reconnectAttempt + 1}):`,
                  err,
                );
              }
              if (sawTerminal || ctrl.signal.aborted) return;
              // Exponential backoff: 500ms, 1s, 2s, 4s, capped at 10s.
              const wait = Math.min(
                500 * Math.pow(2, reconnectAttempt),
                MAX_BACKOFF_MS,
              );
              reconnectAttempt += 1;
              await new Promise((r) => setTimeout(r, wait));
            }
          } finally {
            set((state) => {
              const { [doc_id]: _, ...rest } = state._streams;
              return { _streams: rest };
            });
          }
        })();
      },

      stopWatch: (doc_id) => {
        const ctrl = get()._streams[doc_id];
        if (ctrl) ctrl.abort();
        set((state) => {
          const { [doc_id]: _, ...rest } = state._streams;
          return { _streams: rest };
        });
      },

      resumeAllStreams: () => {
        const { jobs, watchJob } = get();
        for (const job of Object.values(jobs)) {
          if (job.status === "processing") watchJob(job.doc_id);
        }
      },

      activeJobs: () =>
        Object.values(get().jobs)
          .filter((j) => j.status === "processing")
          .sort((a, b) => a.started_at - b.started_at),

      recentCompleted: () =>
        Object.values(get().jobs)
          .filter((j) => j.status !== "processing")
          .sort((a, b) => b.updated_at - a.updated_at),
    }),
    {
      name: "polymath-ingestion-queue",
      partialize: (state) => ({ jobs: state.jobs }),
    },
  ),
);
