// IngestionDashboard.tsx — Phase G
// Floating bottom-right panel showing live per-file ingestion progress.
// Data source: useIngestionQueueStore (zustand, SSE-fed, persisted).
import { useEffect, useMemo, useState } from "react";
import { FixedSizeList as List, type ListChildComponentProps } from "react-window";
import {
  ChevronDown,
  ChevronUp,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  X,
  Activity,
} from "lucide-react";
import {
  useIngestionQueueStore,
  type IngestionJob,
  type IngestionStage,
} from "../../stores/ingestionQueueStore";

const STAGE_ORDER: IngestionStage[] = [
  "uploading",
  "ingesting",
  "embedding",
  "graph_extracting",
  "verifying",
  "verified",
];

const STAGE_LABEL: Record<IngestionStage, string> = {
  uploading: "uploading",
  ingesting: "parsing + chunking",
  embedding: "embedding",
  graph_extracting: "extracting graph",
  verifying: "verifying",
  verified: "verified",
  verify_failed: "verify failed",
  finalized: "finalized",
  failed: "failed",
};

function JobRow({ job }: { job: IngestionJob }) {
  const removeJob = useIngestionQueueStore((s) => s.removeJob);
  const warnings = job.warnings ?? [];

  const [icon, tone] = useMemo(() => {
    if (job.status === "failed" || job.stage === "verify_failed") {
      return [<XCircle key="i" className="w-3.5 h-3.5 text-red-500" />, "red"];
    }
    if (job.stage === "verified" || job.stage === "finalized") {
      return [
        <CheckCircle2 key="i" className="w-3.5 h-3.5 text-green-500" />,
        "green",
      ];
    }
    return [
      <Activity
        key="i"
        className="w-3.5 h-3.5 text-accent-main animate-pulse"
      />,
      "accent",
    ];
  }, [job.status, job.stage]);

  const currentIdx = STAGE_ORDER.indexOf(job.stage);
  const progressPct =
    currentIdx >= 0
      ? Math.round(((currentIdx + 1) / STAGE_ORDER.length) * 100)
      : job.stage === "verify_failed" || job.stage === "failed"
        ? 100
        : 0;

  const elapsed = Math.floor((Date.now() - job.started_at) / 1000);

  return (
    <div className="px-3 py-2 border-b border-border-minimal last:border-b-0 font-mono">
      <div className="flex items-center gap-2 mb-1">
        {icon}
        <div className="flex-1 min-w-0">
          <div className="text-[11px] text-content-primary truncate">
            {job.filename}
          </div>
          <div className="text-[9px] text-content-tertiary truncate">
            {job.corpus_name} · {elapsed}s
          </div>
        </div>
        {(job.status !== "processing") && (
          <button
            onClick={() => removeJob(job.doc_id)}
            className="text-content-tertiary hover:text-accent-main p-0.5"
            aria-label="Dismiss"
          >
            <X className="w-3 h-3" />
          </button>
        )}
      </div>

      {/* Stage bar */}
      <div className="h-1 bg-bg-base rounded-sm overflow-hidden mb-1">
        <div
          className={`h-full transition-all duration-300 ${
            tone === "red"
              ? "bg-red-500"
              : tone === "green"
                ? "bg-green-500"
                : "bg-accent-main"
          }`}
          style={{ width: `${progressPct}%` }}
        />
      </div>

      <div className="flex items-center justify-between text-[9px]">
        <span
          className={`${
            tone === "red"
              ? "text-red-500"
              : tone === "green"
                ? "text-green-500"
                : "text-content-secondary"
          } tracking-wider uppercase`}
        >
          {STAGE_LABEL[job.stage] ?? job.stage}
        </span>
        <span className="text-content-tertiary">
          {job.chunk_count} chunks · {job.parent_count} parents
        </span>
      </div>

      {/* Error / warning / verify_errors surface */}
      {job.error && (
        <div className="mt-1 text-[9px] text-red-500 break-all">
          {job.error}
        </div>
      )}
      {warnings.length > 0 && (
        <div className="mt-1 text-[9px] text-amber-500 space-y-0.5">
          {warnings.slice(0, 2).map((e, i) => (
            <div key={i} className="flex gap-1">
              <AlertTriangle className="w-2.5 h-2.5 shrink-0 mt-0.5" />
              <span className="break-all">{e}</span>
            </div>
          ))}
        </div>
      )}
      {job.verify_errors.length > 0 && (
        <div className="mt-1 text-[9px] text-amber-500 space-y-0.5">
          {job.verify_errors.slice(0, 3).map((e, i) => (
            <div key={i} className="flex gap-1">
              <AlertTriangle className="w-2.5 h-2.5 shrink-0 mt-0.5" />
              <span className="break-all">{e}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function IngestionDashboard() {
  const { jobs, resumeAllStreams, clearFinished } = useIngestionQueueStore();
  const [collapsed, setCollapsed] = useState(false);

  // Re-open SSE streams on mount for anything still processing
  // (e.g. after a page refresh mid-ingest).
  useEffect(() => {
    resumeAllStreams();
  }, [resumeAllStreams]);

  // Tab-refocus resume — browsers throttle backgrounded tabs and abort
  // long-lived fetch streams. On visibilitychange → visible we re-trigger
  // resumeAllStreams; the store's watchJob checks for an existing live
  // stream and no-ops if still connected, otherwise reopens with backoff.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        resumeAllStreams();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [resumeAllStreams]);

  const allJobs = Object.values(jobs).sort(
    (a, b) => b.started_at - a.started_at,
  );
  const active = allJobs.filter((j) => j.status === "processing").length;
  const completed = allJobs.filter(
    (j) => j.status === "done" && j.stage === "verified",
  ).length;
  const failed = allJobs.filter(
    (j) => j.status === "failed" || j.stage === "verify_failed",
  ).length;

  if (allJobs.length === 0) return null;

  return (
    <div className="fixed bottom-2 left-2 right-2 z-40 max-h-[45dvh] overflow-hidden bg-bg-surface border border-border-minimal shadow-lg font-mono sm:left-auto sm:bottom-4 sm:right-4 sm:w-80">
      {/* Header */}
      <div
        className="flex items-center justify-between px-3 py-2 border-b border-border-minimal bg-bg-base cursor-pointer select-none"
        onClick={() => setCollapsed((c) => !c)}
      >
        <div className="flex items-center gap-2">
          <Activity
            className={`w-3 h-3 ${active > 0 ? "text-accent-main animate-pulse" : "text-content-tertiary"}`}
          />
          <span className="text-[11px] text-content-primary tracking-wide">
            INGESTION
          </span>
          <span className="text-[9px] text-content-tertiary">
            {active > 0 ? `${active} running` : "idle"}
            {completed > 0 && ` · ${completed} ✓`}
            {failed > 0 && ` · ${failed} ✗`}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {active === 0 && allJobs.length > 0 && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                clearFinished();
              }}
              className="text-[9px] text-content-tertiary hover:text-accent-main px-1.5 py-0.5 border border-border-minimal"
            >
              clear
            </button>
          )}
          {collapsed ? (
            <ChevronUp className="w-3 h-3 text-content-tertiary" />
          ) : (
            <ChevronDown className="w-3 h-3 text-content-tertiary" />
          )}
        </div>
      </div>

      {!collapsed && (
        <VirtualJobList jobs={allJobs} />
      )}
    </div>
  );
}

// Virtualized list — renders only visible rows, so the dashboard stays at 60
// FPS even at 500+ concurrent jobs. Fixed row height keeps scroll math cheap.
// Row height set empirically — 80px is enough for filename + stage bar +
// 2-line error surface without clipping.
const ROW_HEIGHT = 92;
const LIST_HEIGHT = 320; // matches the prior max-h-80 (80 × 4px)

function VirtualJobList({ jobs }: { jobs: IngestionJob[] }) {
  // Under 20 rows the overhead of virtualization isn't worth it; render flat.
  if (jobs.length < 20) {
    return (
      <div style={{ maxHeight: LIST_HEIGHT, overflowY: "auto" }}>
        {jobs.map((job) => (
          <JobRow key={job.doc_id} job={job} />
        ))}
      </div>
    );
  }
  const Row = ({ index, style }: ListChildComponentProps) => (
    <div style={style}>
      <JobRow job={jobs[index]} />
    </div>
  );
  return (
    <List
      height={LIST_HEIGHT}
      width="100%"
      itemCount={jobs.length}
      itemSize={ROW_HEIGHT}
      itemKey={(i) => jobs[i].doc_id}
      overscanCount={4}
    >
      {Row}
    </List>
  );
}
