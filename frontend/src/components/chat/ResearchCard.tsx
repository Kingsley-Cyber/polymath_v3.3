// ResearchCard.tsx — in-chat Deep Research progress / artifact card.
// Rendered by MessageBubble for assistant messages whose metadata marks them
// as `message_kind === "research_job"`. Polls the durable research job every
// 3s, surfaces stage transitions + elapsed time, and resolves to authenticated
// artifact download links when the report is ready.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  Download,
  FileJson,
  FileText,
  Loader2,
  Telescope,
  XCircle,
} from "lucide-react";
import {
  getResearchJob,
  getResearchJobEvents,
  listResearchArtifacts,
  triggerResearchArtifactDownload,
  type ResearchArtifact,
  type ResearchJob,
  type ResearchTraceEvent,
} from "../../lib/api";

const TERMINAL_STATUSES = new Set(["done", "failed", "cancelled"]);

/** Ordered pipeline stages emitted by the backend worker (see
 *  services/research/worker.py). Used to render the checklist. */
const STAGE_ORDER: { key: string; label: string }[] = [
  { key: "planner", label: "Planning subquestions" },
  { key: "retrieval", label: "Retrieving evidence" },
  { key: "graph", label: "Expanding knowledge graph" },
  { key: "context", label: "Packing context window" },
  { key: "rendering", label: "Rendering report" },
];

function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function artifactIcon(format: ResearchArtifact["format"]) {
  return format === "json" ? (
    <FileJson className="h-3.5 w-3.5" />
  ) : (
    <FileText className="h-3.5 w-3.5" />
  );
}

interface ResearchCardProps {
  jobId: string;
}

export function ResearchCard({ jobId }: ResearchCardProps) {
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [events, setEvents] = useState<ResearchTraceEvent[]>([]);
  const [artifacts, setArtifacts] = useState<ResearchArtifact[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [jobData, eventsData] = await Promise.all([
        getResearchJob(jobId),
        getResearchJobEvents(jobId),
      ]);
      setJob(jobData);
      setEvents(eventsData.items);
      setLoadError(null);
      if (jobData.status === "done") {
        const artifactData = await listResearchArtifacts(jobId);
        setArtifacts(artifactData.items);
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Failed to load research job");
    }
  }, [jobId]);

  // Poll while the job is live; a 1s ticker drives the elapsed-time readout.
  useEffect(() => {
    let cancelled = false;

    const tick = async () => {
      if (cancelled) return;
      await refresh();
    };

    void tick();
    intervalRef.current = setInterval(() => {
      setNow(Date.now());
      void tick();
    }, 3000);

    return () => {
      cancelled = true;
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [refresh]);

  // Stop polling once the job reaches a terminal status.
  useEffect(() => {
    if (job && TERMINAL_STATUSES.has(job.status) && intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, [job]);

  const handleDownload = useCallback(
    async (artifact: ResearchArtifact) => {
      setDownloadingId(artifact.artifact_id);
      try {
        await triggerResearchArtifactDownload(
          artifact.artifact_id,
          artifact.filename,
        );
      } catch (err) {
        setLoadError(
          err instanceof Error ? err.message : "Download failed",
        );
      } finally {
        setDownloadingId(null);
      }
    },
    [],
  );

  const status = job?.status ?? "queued";
  const isDone = status === "done";
  const isFailed = status === "failed" || status === "cancelled";
  const isLive = !isDone && !isFailed;

  const startedAt = job ? new Date(job.created_at).getTime() : now;
  const finishedAt = job && !isLive ? new Date(job.updated_at).getTime() : now;
  const elapsedMs = (isLive ? now : finishedAt) - startedAt;

  // Stages that have emitted at least one event are "reached"; the last
  // reached stage is the current one while the job is still running.
  const reachedStages = new Set(events.map((e) => e.stage));
  const currentStage =
    events.length > 0 ? events[events.length - 1].stage : "planner";
  const latestMessage =
    events.length > 0 ? events[events.length - 1].message : "Submitting research question…";

  const headerTone = isDone
    ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
    : isFailed
      ? "border-rose-400/30 bg-rose-400/10 text-rose-200"
      : "border-sky-400/30 bg-sky-400/10 text-sky-200";

  return (
    <div className="message-assistant w-full max-w-[82ch]">
      <div className="overflow-hidden border border-white/10 bg-black/20">
        {/* Header */}
        <div className="flex items-center justify-between gap-2 border-b border-white/10 px-3 py-2">
          <div className="flex items-center gap-2">
            <Telescope className="h-4 w-4 text-sky-300" />
            <span className="text-[10px] font-bold uppercase tracking-[0.22em] text-content-primary">
              Deep Research
            </span>
            <span
              className={`rounded-full border px-2 py-0.5 text-[9px] font-bold uppercase tracking-widest ${headerTone}`}
            >
              {isDone ? "Complete" : isFailed ? status : "Running"}
            </span>
          </div>
          <span className="font-mono text-[10px] tabular-nums text-content-tertiary">
            {formatElapsed(elapsedMs)}
          </span>
        </div>

        <div className="px-3 py-2.5">
          {loadError && (
            <div className="mb-2 border border-rose-400/20 bg-rose-400/10 px-2 py-1 text-[10px] text-rose-200">
              {loadError}
            </div>
          )}

          {/* Stage checklist */}
          {isLive && (
            <ul className="space-y-1.5">
              {STAGE_ORDER.map((stage) => {
                const reached = reachedStages.has(stage.key);
                const isCurrent = stage.key === currentStage;
                return (
                  <li key={stage.key} className="flex items-center gap-2">
                    {reached && !isCurrent ? (
                      <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
                    ) : isCurrent ? (
                      <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-sky-300" />
                    ) : (
                      <span className="inline-block h-3.5 w-3.5 shrink-0 rounded-full border border-white/15" />
                    )}
                    <span
                      className={`text-[11px] ${
                        isCurrent
                          ? "text-content-primary"
                          : reached
                            ? "text-content-secondary"
                            : "text-content-tertiary"
                      }`}
                    >
                      {stage.label}
                      {isCurrent && (
                        <span className="ml-1 text-content-tertiary">…</span>
                      )}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}

          {/* Live status line */}
          {isLive && (
            <div className="mt-2 border-t border-white/10 pt-2 text-[10px] text-content-tertiary">
              {latestMessage}
            </div>
          )}

          {/* Failure detail */}
          {isFailed && (
            <div className="flex items-start gap-2">
              <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-rose-400" />
              <div className="text-[11px] text-rose-200">
                {status === "cancelled"
                  ? "Research was cancelled before it finished."
                  : job?.error || "Research failed. Check the research workspace for details."}
              </div>
            </div>
          )}

          {/* Artifact download links */}
          {isDone && (
            <div className="space-y-1.5">
              {artifacts.length === 0 ? (
                <div className="text-[11px] text-content-tertiary">
                  Report ready — no artifacts were stored.
                </div>
              ) : (
                artifacts.map((artifact) => (
                  <button
                    key={artifact.artifact_id}
                    type="button"
                    onClick={() => void handleDownload(artifact)}
                    disabled={downloadingId === artifact.artifact_id}
                    className="group/dl flex w-full items-center gap-2 border border-white/10 bg-white/[0.03] px-2.5 py-1.5 text-left transition-colors hover:border-sky-400/40 hover:bg-sky-400/10 disabled:opacity-50"
                  >
                    <span className="text-sky-300">{artifactIcon(artifact.format)}</span>
                    <span className="flex-1 truncate text-[11px] text-content-primary group-hover/dl:text-sky-200">
                      {artifact.filename}
                    </span>
                    <span className="text-[9px] uppercase tracking-widest text-content-tertiary">
                      {artifact.format}
                    </span>
                    {downloadingId === artifact.artifact_id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-300" />
                    ) : (
                      <Download className="h-3.5 w-3.5 text-content-tertiary group-hover/dl:text-sky-300" />
                    )}
                  </button>
                ))
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
