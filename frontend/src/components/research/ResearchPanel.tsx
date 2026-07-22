import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Download,
  FileJson,
  FileText,
  Loader2,
  Play,
  Search,
  X,
} from "lucide-react";

import {
  createResearchJob,
  downloadResearchArtifact,
  getResearchJob,
  getResearchJobEvents,
  listResearchArtifacts,
  listResearchJobs,
  runResearchJob,
  type ResearchArtifact,
  type ResearchJob,
  type ResearchMode,
  type ResearchTraceEvent,
} from "../../lib/api";

interface ResearchPanelProps {
  isOpen: boolean;
  onClose: () => void;
  defaultCorpusIds: string[];
}

const LIVE_STATUSES: ResearchJob["status"][] = [
  "queued",
  "running",
  "waiting_for_input",
  "rendering",
];

function isLiveStatus(status: ResearchJob["status"]): boolean {
  return LIVE_STATUSES.includes(status);
}

function statusTone(status: ResearchJob["status"]): string {
  switch (status) {
    case "done":
      return "border-emerald-500/30 bg-emerald-500/10 text-emerald-200";
    case "failed":
    case "cancelled":
      return "border-rose-500/30 bg-rose-500/10 text-rose-200";
    case "rendering":
    case "running":
      return "border-sky-500/30 bg-sky-500/10 text-sky-200";
    default:
      return "border-amber-500/30 bg-amber-500/10 text-amber-200";
  }
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function artifactIcon(artifact: ResearchArtifact) {
  return artifact.format === "json" ? (
    <FileJson className="h-4 w-4" />
  ) : (
    <FileText className="h-4 w-4" />
  );
}

export function ResearchPanel({
  isOpen,
  onClose,
  defaultCorpusIds,
}: ResearchPanelProps) {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<ResearchMode>("standard");
  const [jobs, setJobs] = useState<ResearchJob[]>([]);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [activeJob, setActiveJob] = useState<ResearchJob | null>(null);
  const [events, setEvents] = useState<ResearchTraceEvent[]>([]);
  const [artifacts, setArtifacts] = useState<ResearchArtifact[]>([]);
  const [isLoadingJobs, setIsLoadingJobs] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [downloadingArtifactId, setDownloadingArtifactId] = useState<string | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  const scopeLabel = useMemo(() => {
    if (defaultCorpusIds.length === 0) return "Backend default scope";
    if (defaultCorpusIds.length === 1) return "1 corpus selected";
    return `${defaultCorpusIds.length} corpora selected`;
  }, [defaultCorpusIds.length]);

  const refreshJobs = useCallback(async () => {
    if (!isOpen) return;
    setIsLoadingJobs(true);
    try {
      const response = await listResearchJobs({ limit: 12 });
      setJobs(response.items);
      setError(null);
      if (!activeJobId && response.items.length > 0) {
        setActiveJobId(response.items[0].job_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load research jobs");
    } finally {
      setIsLoadingJobs(false);
    }
  }, [activeJobId, isOpen]);

  const loadJobDetails = useCallback(async (jobId: string) => {
    try {
      const [job, jobEvents, jobArtifacts] = await Promise.all([
        getResearchJob(jobId),
        getResearchJobEvents(jobId),
        listResearchArtifacts(jobId),
      ]);
      setActiveJob(job);
      setEvents(jobEvents.items);
      setArtifacts(jobArtifacts.items);
      setJobs((current) => {
        const rest = current.filter((item) => item.job_id !== job.job_id);
        return [job, ...rest].sort(
          (a, b) =>
            new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
        );
      });
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load research job");
    }
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    void refreshJobs();
  }, [isOpen, refreshJobs]);

  useEffect(() => {
    if (!isOpen || !activeJobId) return;
    void loadJobDetails(activeJobId);
  }, [activeJobId, isOpen, loadJobDetails]);

  useEffect(() => {
    if (!isOpen || !activeJob || !isLiveStatus(activeJob.status)) return;
    const pollId = window.setInterval(() => {
      void loadJobDetails(activeJob.job_id);
    }, 2500);
    return () => window.clearInterval(pollId);
  }, [activeJob, isOpen, loadJobDetails]);

  if (!isOpen) return null;

  const startResearch = async () => {
    const trimmed = question.trim();
    if (!trimmed) {
      setError("Add a research question first.");
      return;
    }

    setIsCreating(true);
    try {
      const job = await createResearchJob({
        question: trimmed,
        corpus_ids: defaultCorpusIds,
        mode,
        run: true,
        metadata: { source: "frontend_research_panel" },
      });
      setJobs((current) => [
        job,
        ...current.filter((item) => item.job_id !== job.job_id),
      ]);
      setActiveJobId(job.job_id);
      setActiveJob(job);
      setEvents([]);
      setArtifacts([]);
      setQuestion("");
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start research");
    } finally {
      setIsCreating(false);
    }
  };

  const rerunResearch = async () => {
    if (!activeJob) return;
    setIsCreating(true);
    try {
      const job = await runResearchJob(activeJob.job_id, true);
      setActiveJob(job);
      setJobs((current) => [
        job,
        ...current.filter((item) => item.job_id !== job.job_id),
      ]);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to run research job");
    } finally {
      setIsCreating(false);
    }
  };

  const downloadArtifact = async (artifact: ResearchArtifact) => {
    setDownloadingArtifactId(artifact.artifact_id);
    try {
      const blob = await downloadResearchArtifact(artifact.artifact_id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifact.filename;
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to download artifact");
    } finally {
      setDownloadingArtifactId(null);
    }
  };

  return (
    <div className="fixed inset-0 z-[120] flex justify-end bg-black/55 backdrop-blur-sm">
      <aside className="flex h-full w-full max-w-5xl flex-col border-l border-border-minimal bg-[var(--bg-base)] shadow-2xl">
        <header className="flex shrink-0 items-center justify-between border-b border-border-minimal px-4 py-3 sm:px-6">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.24em] text-content-tertiary">
              <Search className="h-3.5 w-3.5" />
              Autoresearch
            </div>
            <h2 className="mt-1 text-lg font-semibold text-content-primary">
              Research workspace
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-9 w-9 items-center justify-center rounded-full border border-border-minimal text-content-secondary hover:border-rose-500/50 hover:text-rose-200"
            aria-label="Close research workspace"
            title="Close research workspace"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[360px_1fr]">
          <section className="flex min-h-0 flex-col border-b border-border-minimal lg:border-b-0 lg:border-r">
            <div className="border-b border-border-minimal p-4">
              <label className="text-[10px] font-mono uppercase tracking-[0.18em] text-content-tertiary">
                Research question
              </label>
              <textarea
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                className="mt-2 h-28 w-full resize-none rounded-xl border border-border-minimal bg-[var(--bg-surface)] p-3 text-sm text-content-primary outline-none placeholder:text-content-tertiary focus:border-accent-main/70"
                placeholder="Ask for a cited corpus-level report..."
              />

              <div className="mt-3 flex flex-wrap gap-2">
                {(["quick", "standard", "deep"] as ResearchMode[]).map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => setMode(item)}
                    className={`rounded-full border px-3 py-1.5 text-[10px] font-bold uppercase tracking-[0.16em] ${
                      mode === item
                        ? "border-accent-main bg-accent-main/15 text-accent-main"
                        : "border-border-minimal text-content-tertiary hover:text-content-primary"
                    }`}
                  >
                    {item}
                  </button>
                ))}
              </div>

              <div className="mt-3 flex items-center justify-between gap-3">
                <div className="text-xs text-content-tertiary">{scopeLabel}</div>
                <button
                  type="button"
                  onClick={() => void startResearch()}
                  disabled={isCreating}
                  className="inline-flex items-center gap-2 rounded-full bg-accent-main px-4 py-2 text-xs font-bold uppercase tracking-[0.14em] text-black disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {isCreating ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Play className="h-3.5 w-3.5" />
                  )}
                  Start
                </button>
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto p-3">
              <div className="mb-2 flex items-center justify-between px-1">
                <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-content-tertiary">
                  Recent jobs
                </div>
                {isLoadingJobs && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              </div>

              <div className="space-y-2">
                {jobs.length === 0 && !isLoadingJobs && (
                  <div className="rounded-xl border border-dashed border-border-minimal p-4 text-sm text-content-tertiary">
                    No research jobs yet. Start one above and artifacts will appear here.
                  </div>
                )}
                {jobs.map((job) => (
                  <button
                    key={job.job_id}
                    type="button"
                    onClick={() => setActiveJobId(job.job_id)}
                    className={`w-full rounded-xl border p-3 text-left transition-none ${
                      activeJobId === job.job_id
                        ? "border-accent-main/70 bg-accent-main/10"
                        : "border-border-minimal bg-[var(--bg-surface)] hover:border-accent-main/35"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span
                        className={`rounded-full border px-2 py-0.5 text-[9px] font-bold uppercase tracking-[0.14em] ${statusTone(job.status)}`}
                      >
                        {job.status}
                      </span>
                      <span className="text-[10px] text-content-tertiary">
                        {formatTime(job.updated_at)}
                      </span>
                    </div>
                    <div className="mt-2 line-clamp-2 text-sm text-content-primary">
                      {job.question}
                    </div>
                    <div className="mt-2 text-[10px] uppercase tracking-[0.14em] text-content-tertiary">
                      {job.mode} / {job.artifact_ids.length} artifacts
                    </div>
                  </button>
                ))}
              </div>
            </div>
          </section>

          <section className="min-h-0 overflow-y-auto p-4 sm:p-6">
            {error && (
              <div className="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-100">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            {!activeJob ? (
              <div className="flex min-h-[420px] items-center justify-center rounded-2xl border border-dashed border-border-minimal text-center">
                <div>
                  <Search className="mx-auto h-8 w-8 text-content-tertiary" />
                  <p className="mt-3 text-sm text-content-tertiary">
                    Pick a job or start a new research run.
                  </p>
                </div>
              </div>
            ) : (
              <div className="space-y-5">
                <div className="rounded-2xl border border-border-minimal bg-[var(--bg-surface)] p-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <span
                      className={`rounded-full border px-2.5 py-1 text-[10px] font-bold uppercase tracking-[0.16em] ${statusTone(activeJob.status)}`}
                    >
                      {activeJob.status}
                    </span>
                    <button
                      type="button"
                      onClick={() => void rerunResearch()}
                      disabled={isCreating || isLiveStatus(activeJob.status)}
                      className="inline-flex items-center gap-2 rounded-full border border-border-minimal px-3 py-1.5 text-[10px] font-bold uppercase tracking-[0.14em] text-content-secondary hover:border-accent-main/50 hover:text-accent-main disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <Play className="h-3.5 w-3.5" />
                      Run
                    </button>
                  </div>
                  <h3 className="mt-3 text-xl font-semibold text-content-primary">
                    {activeJob.question}
                  </h3>
                  <div className="mt-3 grid gap-2 text-xs text-content-tertiary sm:grid-cols-3">
                    <div>Mode: {activeJob.mode}</div>
                    <div>Corpora: {activeJob.corpus_ids.length || "default"}</div>
                    <div>Updated: {formatTime(activeJob.updated_at)}</div>
                  </div>
                </div>

                <div className="rounded-2xl border border-border-minimal bg-[var(--bg-surface)] p-4">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <h4 className="text-sm font-semibold text-content-primary">
                      Download artifacts
                    </h4>
                    <span className="text-[10px] uppercase tracking-[0.16em] text-content-tertiary">
                      {artifacts.length} files
                    </span>
                  </div>
                  {artifacts.length === 0 ? (
                    <div className="rounded-xl border border-dashed border-border-minimal p-4 text-sm text-content-tertiary">
                      Artifacts appear after the renderer finishes.
                    </div>
                  ) : (
                    <div className="grid gap-2 sm:grid-cols-2">
                      {artifacts.map((artifact) => (
                        <button
                          key={artifact.artifact_id}
                          type="button"
                          onClick={() => void downloadArtifact(artifact)}
                          className="flex items-center justify-between gap-3 rounded-xl border border-border-minimal bg-[var(--bg-base)] p-3 text-left hover:border-accent-main/50"
                        >
                          <span className="flex min-w-0 items-center gap-2">
                            <span className="text-accent-main">
                              {artifactIcon(artifact)}
                            </span>
                            <span className="min-w-0">
                              <span className="block truncate text-sm text-content-primary">
                                {artifact.filename}
                              </span>
                              <span className="text-[10px] uppercase tracking-[0.14em] text-content-tertiary">
                                {artifact.format} / {formatBytes(artifact.size_bytes)}
                              </span>
                            </span>
                          </span>
                          {downloadingArtifactId === artifact.artifact_id ? (
                            <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                          ) : (
                            <Download className="h-4 w-4 shrink-0" />
                          )}
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                <div className="rounded-2xl border border-border-minimal bg-[var(--bg-surface)] p-4">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <h4 className="text-sm font-semibold text-content-primary">
                      Trace
                    </h4>
                    {isLiveStatus(activeJob.status) ? (
                      <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.16em] text-sky-200">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        polling
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.16em] text-emerald-200">
                        <CheckCircle2 className="h-3.5 w-3.5" />
                        settled
                      </span>
                    )}
                  </div>
                  <div className="space-y-2">
                    {events.length === 0 && (
                      <div className="rounded-xl border border-dashed border-border-minimal p-4 text-sm text-content-tertiary">
                        No trace events have been recorded yet.
                      </div>
                    )}
                    {events.map((event) => (
                      <div
                        key={event.event_id}
                        className="rounded-xl border border-border-minimal bg-[var(--bg-base)] p-3"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="text-[10px] font-bold uppercase tracking-[0.16em] text-content-tertiary">
                            {event.stage} / {event.status}
                          </span>
                          <span className="inline-flex items-center gap-1 text-[10px] text-content-tertiary">
                            <Clock3 className="h-3 w-3" />
                            {formatTime(event.created_at)}
                          </span>
                        </div>
                        <p className="mt-1 text-sm text-content-primary">
                          {event.message}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </section>
        </div>
      </aside>
    </div>
  );
}
