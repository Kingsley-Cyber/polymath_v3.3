// CorpusDetail.tsx - Document browser for a single corpus
import { useState, useEffect, useCallback, useMemo, useRef, type CSSProperties } from "react";
import {
  ChevronLeft,
  Trash2,
  FileText,
  FolderOpen,
  Upload,
  Loader2,
  Check,
  X,
  AlertTriangle,
  Hash,
  Layers,
  ChevronRight,
  ChevronDown,
  Settings2,
  SlidersHorizontal,
  CheckCircle2,
  XCircle,
  RotateCcw,
  Copy,
  BookOpen,
  Search,
  Zap,
} from "lucide-react";
import * as api from "../../lib/api";
import { parseBookMeta } from "../../lib/label-utils";
import { DuplicatesPanel } from "./DuplicatesPanel";
import type {
  CorpusResponse,
  DocumentResponse,
  IngestBatchItemResponse,
  IngestBatchResponse,
  IngestProfileName,
  ModalStatus,
  WriteState,
} from "../../types";
import type { IngestOverrides } from "../../lib/api";

interface CorpusDetailProps {
  corpus: CorpusResponse;
  onBack: () => void;
  onCorpusUpdated: (corpus: CorpusResponse) => void;
  /** When provided, renders an "Edit Models & Schema" button in the header
   *  that hands control back to CorpusManager's edit panel. */
  onEditConfig?: (corpus: CorpusResponse) => void;
}

const LOCAL_BATCH_DEFAULT_PATH = "/ingest-source/authentic_files";

function formatBytes(bytes?: number | null): string {
  if (!bytes || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIdx = 0;
  while (value >= 1024 && unitIdx < units.length - 1) {
    value /= 1024;
    unitIdx += 1;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[unitIdx]}`;
}

function formatRate(value?: number | null): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "n/a";
  return `${Math.round(value * 100)}%`;
}

function formatCoverage(done?: number | null, total?: number | null): string {
  const safeDone = done ?? 0;
  const safeTotal = total ?? 0;
  if (safeTotal <= 0) return `${safeDone}/0`;
  return `${safeDone}/${safeTotal} · ${Math.round((safeDone / safeTotal) * 100)}%`;
}

function readinessLabel(status?: string | null): string {
  return (status || "unknown").replace(/_/g, " ");
}

function readinessTone(status?: string | null): string {
  if (status === "fully_enriched") return "text-accent-main border-accent-main/50";
  if (
    status === "summaries_pending" ||
    status === "graph_pending" ||
    status === "ingestion_pending" ||
    status === "queryable_partial" ||
    status === "needs_review"
  ) {
    return "text-amber-300 border-amber-400/40";
  }
  if (status === "needs_repair" || status === "needs_reconciliation" || status === "not_ready") {
    return "text-error border-error/40";
  }
  return "text-content-tertiary border-border-minimal";
}

function providerLabel(provider: string): string {
  const key = provider.toLowerCase();
  if (key.includes("vllm") || key.includes("rtx")) return "RTX";
  if (key.includes("silicon")) return "SiliconFlow";
  if (key.includes("longcat")) return "LongCat";
  if (key.includes("deepseek")) return "DeepSeek";
  return provider.replace(/^openai\//, "");
}

function topCountEntries(counts?: Record<string, number>, limit = 4) {
  return Object.entries(counts ?? {})
    .filter(([, value]) => Number.isFinite(value) && value > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit);
}

type SummaryScopeValue =
  | string
  | {
      label?: string;
      description?: string;
      readiness_gate?: boolean;
      includes_chunk_kinds?: string[];
      includes_missing_chunk_kind?: boolean;
    };

function summaryScope(
  scopes: Record<string, SummaryScopeValue> | undefined,
  key: string,
): { label?: string; description?: string } {
  const value = scopes?.[key];
  if (!value || typeof value === "string") return {};
  return {
    label: value.label,
    description: value.description,
  };
}

type RepairCycleSummary = {
  readiness_status?: string;
  pressure_status?: string;
  pressure_recommendations?: string[];
  queryable_docs?: number;
  total_docs?: number;
  main_summary_missing?: number;
  document_summary_missing?: number;
  document_summaries_built?: number;
  graph_pending?: number;
  failed_chunks?: number;
  stale_failure_rows?: number;
  promoted_extraction_marks_planned?: number;
  promoted_extraction_marks_backfilled?: number;
  graph_jobs_queued?: number;
  graph_jobs_running?: number;
  graph_jobs_blocked?: number;
  source_parse_jobs_queued?: number;
  source_parse_jobs_running?: number;
  source_parse_jobs_blocked?: number;
  source_parse_jobs_started?: number;
  document_pipeline_jobs_queued?: number;
  document_pipeline_jobs_running?: number;
  document_pipeline_jobs_blocked?: number;
  document_pipeline_jobs_ran?: number;
  document_pipeline_jobs_succeeded?: number;
  extraction_jobs_queued?: number;
  extraction_jobs_failed?: number;
  extraction_jobs_blocked?: number;
  summary_jobs_queued?: number;
  summary_jobs_running?: number;
  summary_jobs_pending?: number;
  summary_jobs_waiting_dependencies?: number;
  summary_jobs_blocked?: number;
  summary_jobs_ran?: number;
  summary_jobs_succeeded?: number;
};

type RepairCycleResult = {
  status?: string;
  apply?: boolean;
  background?: boolean;
  run_id?: string;
  summary?: RepairCycleSummary;
};

type SummaryBackfillResult = {
  status?: string;
  run_id?: string;
  generated?: number;
  attempted?: number;
  indexed?: number;
  limit?: number | null;
  index_scope?: string;
  index_requested?: boolean;
  index_deferred_by_pressure?: boolean;
};

type ExtractionJobPlanResult = {
  status?: string;
  apply?: boolean;
  planned?: number;
  counts?: Record<string, number>;
  kind_counts?: Record<string, number>;
};

type SummaryJobPlanResult = {
  status?: string;
  apply?: boolean;
  planned?: number;
  counts?: Record<string, number>;
  kind_counts?: Record<string, number>;
};

type SummaryJobRunResult = {
  status?: string;
  claimed?: number;
  reclaimed?: number;
  parent_claimed?: number;
  document_claimed?: number;
  counts?: Record<string, number>;
  runner_results?: Record<
    string,
    {
      index_scope?: string;
      index_requested?: boolean;
      index_deferred_by_pressure?: boolean;
      generated?: number;
      indexed?: number;
    }
  >;
};

type SourceParseJobPlanResult = {
  status?: string;
  apply?: boolean;
  planned?: number;
  counts?: Record<string, number>;
  kind_counts?: Record<string, number>;
};

type SourceParseJobRunResult = {
  status?: string;
  requested?: number;
  eligible_items?: number;
  batch_count?: number;
  runners_started?: number;
  runner_deferred?: boolean;
  counts?: Record<string, number>;
};

type DocumentPipelineJobPlanResult = {
  status?: string;
  apply?: boolean;
  planned?: number;
  counts?: Record<string, number>;
  kind_counts?: Record<string, number>;
};

type DocumentPipelineJobRunResult = {
  status?: string;
  claimed?: number;
  reclaimed?: number;
  source_claimed?: number;
  source_requested?: boolean;
  executor_missing_kinds?: string[];
  runner_results?: Record<string, unknown>;
  counts?: Record<string, number>;
};

type ExtractionJobRunResult = {
  status?: string;
  claimed?: number;
  reclaimed?: number;
  counts?: Record<string, number>;
};

type IdentityAuditDocCard = {
  doc_id?: string;
  filename?: string;
  title?: string;
  ingest_stage?: string;
  source_key?: string;
  source_kind?: string;
  content_sha256?: string;
  identity_version?: string;
  write_state?: Partial<WriteState>;
};

type IdentityAuditDuplicateGroup = {
  source_key?: string;
  content_sha256?: string;
  doc_count?: number;
  canonical_doc_id?: string | null;
  canonical_doc?: IdentityAuditDocCard | null;
  duplicate_doc_ids?: string[];
  recommended_action?: string;
  docs?: IdentityAuditDocCard[];
};

type IdentityAuditResult = {
  status?: string;
  doc_total?: number;
  source_keyed_documents?: number;
  content_hash_documents?: number;
  missing_source_identity_count?: number;
  duplicate_source_key_group_count?: number;
  duplicate_source_key_doc_count?: number;
  source_key_collision_group_count?: number;
  source_key_collision_doc_count?: number;
  duplicate_content_hash_group_count?: number;
  duplicate_content_hash_doc_count?: number;
  duplicate_source_key_groups?: IdentityAuditDuplicateGroup[];
  duplicate_content_hash_groups?: IdentityAuditDuplicateGroup[];
};

function getWriteStateMessages(
  state: Pick<WriteState, "warnings" | "verify_errors">,
) {
  return [...(state.warnings ?? []), ...(state.verify_errors ?? [])].filter(Boolean);
}

function getParentCount(doc: DocumentResponse): number {
  return doc.parent_count ?? doc.parent_chunks?.length ?? 0;
}

function getIdentityAuditAction(result: IdentityAuditResult | null): string | null {
  if (!result) return null;
  const group =
    result.duplicate_source_key_groups?.[0] ??
    result.duplicate_content_hash_groups?.[0];
  if (group?.recommended_action === "repair_source_identity_collision") {
    return "Repair source identity collision before reusing artifacts; matching source_key rows have different content hashes";
  }
  if (!group?.canonical_doc_id) return null;
  const duplicateCount = group.duplicate_doc_ids?.length ?? 0;
  if (duplicateCount <= 0) return null;
  const canonicalLabel =
    group.canonical_doc?.filename || group.canonical_doc_id || "canonical doc";
  return `Keep ${canonicalLabel}; reuse artifacts for ${duplicateCount} exact duplicate${duplicateCount === 1 ? "" : "s"}`;
}

function getBatchItemStatusLabel(item: IngestBatchItemResponse): string {
  if (item.status === "staged") {
    return item.stage ? `staged · ${item.stage}` : "staged";
  }
  if (item.status === "failed_recoverable") {
    return item.phase === "stale" ? "stale" : "recoverable";
  }
  return item.status;
}

function defaultBatchProfile(corpus: CorpusResponse): IngestProfileName {
  const cfg = corpus.default_ingestion_config;
  const engine = cfg.extraction_engine ?? "local";
  const hasRemotePool =
    (cfg.extraction_models ?? []).some((m) => {
      const url = (m.base_url ?? "").toLowerCase();
      const provider = (m.provider_preset ?? "").toLowerCase();
      const model = (m.model ?? "").toLowerCase();
      const extras = m.extra_params ?? {};
      return (
        provider === "vllm-rtx" ||
        provider === "vllm" ||
        url.includes("/v1") ||
        url.includes("192.168.") ||
        model.includes("polymath-extract") ||
        model.includes("vllm") ||
        extras.resource_class === "rtx" ||
        extras.resource_class === "remote_vllm" ||
        extras.managed_vllm === true
      );
    }) || ["cloud", "dual", "local_then_cloud", "local_then_enrich"].includes(engine);
  return hasRemotePool ? "rtx_assisted" : "mac_queryable_first";
}

const PROFILE_LABELS: Record<IngestProfileName, string> = {
  rtx_assisted: "RTX assisted",
  mac_queryable_first: "Mac queryable-first",
  mac_safe: "Mac optimized",
};

// Pipeline phase → words a human can read. Raw phase stays in tooltips.
const PHASE_LABELS: Record<string, string> = {
  parse: "parsing",
  chunk: "chunking",
  chunking: "chunking",
  ghosts: "extracting entities",
  summary: "summarizing",
  mongo: "writing text",
  embed: "embedding",
  qdrant: "writing vectors",
  neo4j: "writing graph",
  verify: "verifying",
};

const INGEST_STAGE_LABELS = [
  ["registered", "Registered"],
  ["parsed", "Parsed"],
  ["chunked", "Chunked"],
  ["indexed", "Indexed"],
  ["queryable", "Queryable"],
  ["summary_pending", "Summary pending"],
  ["summary_complete", "Summary complete"],
  ["graph_pending", "Graph pending"],
  ["graph_extracted", "Graph extracted"],
  ["graph_promoted", "Graph promoted"],
  ["fully_enriched", "Fully enriched"],
] as const;

function hasUsefulBatchProgress(batch: IngestBatchResponse): boolean {
  if (batch.status === "cancelled") return false;

  const counts = batch.counts ?? {};
  if (
    (counts.running ?? 0) > 0 ||
    (counts.queued ?? 0) > 0 ||
    (counts.done ?? 0) > 0 ||
    (counts.staged ?? 0) > 0 ||
    (counts.failed_recoverable ?? 0) > 0
  ) {
    return true;
  }

  const progress = batch.progress;
  const ladder = progress?.ladder ?? {};
  return (
    (progress?.files_done ?? 0) > 0 ||
    (progress?.files_queryable ?? 0) > 0 ||
    (progress?.files_graph_extracted ?? 0) > 0 ||
    (ladder.queryable ?? 0) > 0 ||
    (ladder.indexed ?? 0) > 0 ||
    (ladder.extracted ?? 0) > 0 ||
    (ladder.graph_extracted ?? 0) > 0 ||
    (ladder.graph_promoted ?? 0) > 0
  );
}

function isArchivedBatch(batch: IngestBatchResponse): boolean {
  return batch.status === "cancelled" || batch.status === "failed";
}

function selectDisplayBatch(
  batches: IngestBatchResponse[],
): IngestBatchResponse | null {
  const active = batches.find((batch) =>
    batch.status === "queued" || batch.status === "running"
  );
  if (active) return active;

  return (
    batches.find(
      (batch) =>
        (batch.status === "done" || batch.status === "partial") &&
        hasUsefulBatchProgress(batch),
    ) ?? null
  );
}

function phaseLabel(phase?: string | null): string {
  if (!phase) return "queued";
  return PHASE_LABELS[phase] ?? phase;
}

// Graph-lane coverage from ghost_b_metrics: extracted/requested chunks.
// §13 correction — a doc can be mongo/qdrant/verified GREEN while its knowledge
// graph is ~empty (the Qwen2.5-7B collapse left 110/113 docs graph-dead reading
// as clean "done"). Returns null when the corpus doesn't use the graph, when
// extraction was explicitly OFF, or when metrics are absent.
function graphCoverage(
  doc: DocumentResponse,
): { pct: number; extracted: number; requested: number } | null {
  const m = doc.ghost_b_metrics;
  if (!m || m.engine === "off") return null;
  const requested = m.requested_chunks ?? 0;
  if (!requested) return null;
  const extracted = m.extracted_chunks ?? 0;
  return { pct: Math.round((extracted / requested) * 100), extracted, requested };
}

/** Plain-English reason for a failed ingest. Raw error text → tooltip. */
function humanizeIngestFailure(
  rawError: string | null | undefined,
  stage?: string,
): string {
  const e = (rawError || "").toLowerCase();
  if (e.includes("tier_chunker exceeded"))
    return "chunking timed out — pathological layout (huge index / table / code blocks)";
  if (e.includes("has_chunk") || (e.includes("neo4j") && e.includes("expected")))
    return "knowledge-graph links missing — try Backfill on the document";
  if (e.includes("child vectors") || e.includes("vectors but"))
    return "vector store incomplete — some embeddings missing (re-ingest to repair)";
  if (e.includes("timeout") || e.includes("timed out")) return "a pipeline stage timed out";
  if (e) return e.length > 100 ? `${e.slice(0, 100)}…` : e;
  switch (stage) {
    case "parse/ghosts":
      return "failed while parsing / extracting";
    case "embed/qdrant":
      return "failed while embedding";
    case "neo4j":
      return "failed while writing the knowledge graph";
    default:
      return "failed final verification";
  }
}

export function CorpusDetail({
  corpus,
  onBack,
  onCorpusUpdated,
  onEditConfig,
}: CorpusDetailProps) {
  const [documents, setDocuments] = useState<DocumentResponse[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Expanded document for details
  const [expandedDocId, setExpandedDocId] = useState<string | null>(null);

  // Delete confirmation
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  // Per-batch overrides (Sprint 2B). Empty object = use corpus defaults.
  const [overrides, setOverrides] = useState<IngestOverrides>({});
  const [showOverrides, setShowOverrides] = useState(false);
  const [showLocalBatch, setShowLocalBatch] = useState(true);
  const [showDuplicates, setShowDuplicates] = useState(false);
  const [localBatchPath, setLocalBatchPath] = useState(LOCAL_BATCH_DEFAULT_PATH);
  const [localBatchProfile, setLocalBatchProfile] = useState<IngestProfileName>(
    () => defaultBatchProfile(corpus),
  );
  const localBatchConcurrency = 1;
  const [localBatch, setLocalBatch] = useState<IngestBatchResponse | null>(null);
  const [isStartingLocalBatch, setIsStartingLocalBatch] = useState(false);
  const [isQuickUploading, setIsQuickUploading] = useState(false);
  const [isRunningRepairCycle, setIsRunningRepairCycle] = useState(false);
  const [repairCycleResult, setRepairCycleResult] = useState<RepairCycleResult | null>(null);
  const [isQueueingSummaryBackfill, setIsQueueingSummaryBackfill] = useState(false);
  const [summaryBackfillResult, setSummaryBackfillResult] = useState<SummaryBackfillResult | null>(null);
  const [isPlanningExtractionJobs, setIsPlanningExtractionJobs] = useState(false);
  const [extractionJobPlanResult, setExtractionJobPlanResult] = useState<ExtractionJobPlanResult | null>(null);
  const [isPlanningSummaryJobs, setIsPlanningSummaryJobs] = useState(false);
  const [summaryJobPlanResult, setSummaryJobPlanResult] = useState<SummaryJobPlanResult | null>(null);
  const [isRunningSummaryJobs, setIsRunningSummaryJobs] = useState(false);
  const [summaryJobRunResult, setSummaryJobRunResult] = useState<SummaryJobRunResult | null>(null);
  const [isPlanningSourceParseJobs, setIsPlanningSourceParseJobs] = useState(false);
  const [sourceParseJobPlanResult, setSourceParseJobPlanResult] =
    useState<SourceParseJobPlanResult | null>(null);
  const [isRunningSourceParseJobs, setIsRunningSourceParseJobs] = useState(false);
  const [sourceParseJobRunResult, setSourceParseJobRunResult] =
    useState<SourceParseJobRunResult | null>(null);
  const [isPlanningDocumentPipelineJobs, setIsPlanningDocumentPipelineJobs] = useState(false);
  const [documentPipelineJobPlanResult, setDocumentPipelineJobPlanResult] =
    useState<DocumentPipelineJobPlanResult | null>(null);
  const [isRunningDocumentPipelineJobs, setIsRunningDocumentPipelineJobs] = useState(false);
  const [documentPipelineJobRunResult, setDocumentPipelineJobRunResult] =
    useState<DocumentPipelineJobRunResult | null>(null);
  const [isRunningExtractionJobs, setIsRunningExtractionJobs] = useState(false);
  const [extractionJobRunResult, setExtractionJobRunResult] = useState<ExtractionJobRunResult | null>(null);
  const [isAuditingIdentity, setIsAuditingIdentity] = useState(false);
  const [identityAuditResult, setIdentityAuditResult] = useState<IdentityAuditResult | null>(null);
  const [durableJobs, setDurableJobs] = useState<api.DurableIngestionJob[]>([]);
  const [isLoadingDurableJobs, setIsLoadingDurableJobs] = useState(false);
  const [controllingJobId, setControllingJobId] = useState<string | null>(null);
  const identityAuditAction = getIdentityAuditAction(identityAuditResult);
  const quickUploadInputRef = useRef<HTMLInputElement | null>(null);

  // Modal global status — used to warn when corpus default is embed_mode='modal'
  // but Modal isn't deployed.
  const [modalStatus, setModalStatus] = useState<ModalStatus | null>(null);
  useEffect(() => {
    setLocalBatchProfile(defaultBatchProfile(corpus));
  }, [corpus.corpus_id, corpus.default_ingestion_config.extraction_engine]);

  useEffect(() => {
    api
      .getModalStatus()
      .then(setModalStatus)
      .catch(() => setModalStatus(null));
  }, []);

  const refreshDisplayBatch = useCallback(
    async (hydrateItems = false, isCurrent: () => boolean = () => true) => {
      const batches = await api.listIngestBatches(corpus.corpus_id, 10);
      if (!isCurrent()) return;
      const selected = selectDisplayBatch(batches);
      setLocalBatch((prev) => {
        if (!selected) return null;
        return prev && prev.batch_id === selected.batch_id
          ? { ...selected, items: selected.items ?? prev.items }
          : selected;
      });
      if (selected && ["queued", "running"].includes(selected.status)) {
        setShowLocalBatch(true);
      }

      if (hydrateItems && selected && !selected.items?.length) {
        const full = await api.getIngestBatch(selected.batch_id);
        if (!isCurrent()) return;
        setLocalBatch((prev) =>
          prev && prev.batch_id === full.batch_id
            ? { ...prev, items: full.items ?? prev.items }
            : prev,
        );
      }
    },
    [corpus.corpus_id],
  );

  useEffect(() => {
    if (!localBatch?.batch_id) return;
    if (!["queued", "running"].includes(localBatch.status)) return;
    const timer = window.setInterval(() => {
      api
        // Summary mode: the 3s progress poll needs counts/status, not the
        // ~585 KB item list. Keep previously-loaded items so any expanded
        // item view doesn't blank out between polls.
        .getIngestBatch(localBatch.batch_id, { includeItems: false })
        .then((next) =>
          setLocalBatch((prev) => {
            if (isArchivedBatch(next) && !hasUsefulBatchProgress(next)) {
              return null;
            }
            return prev ? { ...next, items: next.items ?? prev.items } : next;
          }),
        )
        .catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [localBatch?.batch_id, localBatch?.status]);

  useEffect(() => {
    setLocalBatch(null);
    setShowLocalBatch(true);

    let cancelled = false;
    // The library's PROCESSING / FAILED sections need the item list; hydrate
    // once, then poll the lightweight list endpoint below so cancelled repair
    // batches cannot remain pinned as the live corpus status.
    refreshDisplayBatch(true, () => !cancelled)
      .catch(() => undefined);

    const timer = window.setInterval(() => {
      if (cancelled) return;
      refreshDisplayBatch(false, () => !cancelled).catch(() => undefined);
    }, 10000);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [corpus.corpus_id, refreshDisplayBatch]);

  const [retryHint, setRetryHint] = useState<string | null>(null);
  const [backfillingDocs, setBackfillingDocs] = useState<Set<string>>(new Set());

  // Splitter between the left (doc list) and right (library) panels.
  // leftPct is the left side's width as a % of the flex row container;
  // clamped 20–80 so neither side can collapse to nothing.
  const [leftPct, setLeftPct] = useState(50);
  const splitRowRef = useRef<HTMLDivElement | null>(null);

  const startSplitDrag = (e: React.MouseEvent) => {
    e.preventDefault();
    const row = splitRowRef.current;
    if (!row) return;
    const onMove = (ev: MouseEvent) => {
      const rect = row.getBoundingClientRect();
      const pct = ((ev.clientX - rect.left) / rect.width) * 100;
      setLeftPct(Math.max(20, Math.min(80, pct)));
    };
    const onUp = () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const loadDocuments = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.listDocuments(corpus.corpus_id);
      setDocuments(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load documents");
    } finally {
      setIsLoading(false);
    }
  }, [corpus.corpus_id]);

  useEffect(() => {
    loadDocuments();
  }, [loadDocuments]);

  const handleDeleteDoc = async (docId: string) => {
    setError(null);
    try {
      await api.deleteDocument(corpus.corpus_id, docId);
      await loadDocuments();
      const updated = await api.getCorpus(corpus.corpus_id);
      onCorpusUpdated(updated);
      setDeleteConfirmId(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to delete document",
      );
    }
  };

  const handleBulkDelete = async (docIds: string[]) => {
    setError(null);
    const failures: string[] = [];
    for (const id of docIds) {
      try {
        await api.deleteDocument(corpus.corpus_id, id);
      } catch (err) {
        failures.push(id.slice(0, 12));
        console.error("delete failed for", id, err);
      }
    }
    await loadDocuments();
    try {
      const updated = await api.getCorpus(corpus.corpus_id);
      onCorpusUpdated(updated);
    } catch {
      /* non-fatal */
    }
    if (failures.length > 0) {
      setError(
        `Failed to delete ${failures.length} of ${docIds.length} documents: ${failures.join(", ")}`,
      );
    }
  };

  const handleGraphBackfill = async (doc: DocumentResponse) => {
    setError(null);
    setBackfillingDocs((prev) => new Set(prev).add(doc.doc_id));
    try {
      const result = await api.backfillDocumentGraph(corpus.corpus_id, doc.doc_id);
      setRetryHint(
        result.status === "noop"
          ? "No failed graph chunks to backfill for this document."
          : `Graph backfill queued for ${result.failed_chunks} failed chunk(s). Refresh in a moment to see recovery status.`,
      );
      await loadDocuments();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to queue graph backfill");
    } finally {
      setBackfillingDocs((prev) => {
        const next = new Set(prev);
        next.delete(doc.doc_id);
        return next;
      });
    }
  };

  const handleRetryDoc = (doc: DocumentResponse) => {
    const name = doc.filename || doc.source_path?.split("/").pop() || "this file";
    setShowLocalBatch(true);
    setRetryHint(
      `Retry "${name}" through Backend Folder. Browser upload is disabled so ingest state stays durable and resumable.`,
    );
  };

  const handleStartLocalBatch = async () => {
    const rootPath = localBatchPath.trim();
    if (!rootPath) {
      setError("Enter a backend-visible folder path such as /ingest-source/books");
      return;
    }
    setIsStartingLocalBatch(true);
    setError(null);
    try {
      const batch = await api.createLocalIngestBatch(corpus.corpus_id, {
        root_path: rootPath,
        profile: localBatchProfile,
        recursive: true,
        store_files: true,
        max_total_bytes: 2 * 1024 * 1024 * 1024,
        concurrency: localBatchConcurrency,
        start: true,
      });
      setLocalBatch(batch);
      setRetryHint(
        `Durable backend batch ${batch.batch_id.slice(0, 8)} started from ${rootPath}. ` +
          `${batch.total} file(s), ${formatBytes(batch.stored_bytes)} stored in the backend spool.`,
      );
      await loadDocuments();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start local ingest batch");
    } finally {
      setIsStartingLocalBatch(false);
    }
  };

  const handleResumeLocalBatch = async () => {
    if (!localBatch?.batch_id) return;
    setIsStartingLocalBatch(true);
    setError(null);
    try {
      const batch = await api.resumeIngestBatch(localBatch.batch_id);
      setLocalBatch(batch);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to resume local ingest batch");
    } finally {
      setIsStartingLocalBatch(false);
    }
  };

  const handleRescanLocalBatch = async () => {
    if (!localBatch?.batch_id) return;
    setIsStartingLocalBatch(true);
    setError(null);
    try {
      const batch = await api.rescanIngestBatch(localBatch.batch_id, { start: true });
      setLocalBatch(batch);
      setRetryHint(
        `Folder sync found ${batch.appended_items ?? 0} new file(s) from ${batch.discovered_files ?? 0} discovered file(s).`,
      );
      await loadDocuments();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to sync local ingest folder");
    } finally {
      setIsStartingLocalBatch(false);
    }
  };

  const handleQuickUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(e.target.files ?? []);
    if (selected.length === 0) return;
    setIsQuickUploading(true);
    setError(null);
    try {
      const batch = await api.createUploadIngestBatch(corpus.corpus_id, selected, {
        use_neo4j: overrides.use_neo4j,
        chunk_summarization: overrides.chunk_summarization,
        model: overrides.model,
        concurrency: Math.max(1, Math.min(4, selected.length)),
        profile: localBatchProfile,
        start: true,
      });
      setLocalBatch(batch);
      setShowLocalBatch(true);
      setRetryHint(
        `Quick upload batch ${batch.batch_id.slice(0, 8)} started for ${batch.total} file(s).`,
      );
      await loadDocuments();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start quick upload");
    } finally {
      setIsQuickUploading(false);
      e.target.value = "";
    }
  };

  const handleRepairCycle = async (
    apply: boolean,
    options: {
      background?: boolean;
      runGraphJobs?: boolean;
      runExtractionJobs?: boolean;
      runDocumentPipelineJobs?: boolean;
      runSummaryJobs?: boolean;
      runDocumentSummaries?: boolean;
    } = {},
  ) => {
    setIsRunningRepairCycle(true);
    setError(null);
    try {
      const result = (await api.runCorpusRepairCycle(corpus.corpus_id, {
        apply,
        background: options.background ?? false,
        reconcile_failures: true,
        failure_reconcile_limit: 5000,
        backfill_promoted_extraction_marks_rows: true,
        promoted_extraction_marks_backfill_limit: 100,
        plan_source_parse_jobs: true,
        source_parse_job_plan_limit: 1000,
        plan_document_pipeline_jobs: true,
        document_pipeline_job_plan_limit: 1000,
        run_document_pipeline_jobs: options.runDocumentPipelineJobs ?? false,
        document_pipeline_job_run_limit: 25,
        plan_graph_jobs: true,
        graph_plan_limit: 100,
        plan_extraction_jobs: true,
        extraction_job_plan_limit: 500,
        run_extraction_jobs: options.runExtractionJobs ?? false,
        extraction_job_run_limit: 25,
        run_summary_jobs: options.runSummaryJobs ?? false,
        summary_job_run_limit: 25,
        run_document_summaries: options.runDocumentSummaries ?? false,
        document_summary_limit: 10,
        run_graph_jobs: options.runGraphJobs ?? false,
        graph_run_limit: 3,
      })) as RepairCycleResult;
      setRepairCycleResult(result);
      const refreshed = await api.getCorpus(corpus.corpus_id);
      onCorpusUpdated(refreshed);
      if (apply) {
        await loadDocuments();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to run corpus repair cycle");
    } finally {
      setIsRunningRepairCycle(false);
    }
  };

  const handleSummaryBackfill = async () => {
    setIsQueueingSummaryBackfill(true);
    setError(null);
    try {
      const result = (await api.backfillCorpusSummaries(corpus.corpus_id, {
        generate: true,
        index: true,
        limit: 500,
        batch: 32,
        background: true,
      })) as SummaryBackfillResult;
      setSummaryBackfillResult(result);
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to queue summary backfill");
    } finally {
      setIsQueueingSummaryBackfill(false);
    }
  };

  const handlePlanExtractionJobs = async () => {
    setIsPlanningExtractionJobs(true);
    setError(null);
    try {
      const result = (await api.planCorpusExtractionJobs(corpus.corpus_id, {
        apply: true,
        limit: 1000,
        include_succeeded: false,
      })) as ExtractionJobPlanResult;
      setExtractionJobPlanResult(result);
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to plan extraction jobs");
    } finally {
      setIsPlanningExtractionJobs(false);
    }
  };

  const handlePlanSummaryJobs = async () => {
    setIsPlanningSummaryJobs(true);
    setError(null);
    try {
      const result = (await api.planCorpusSummaryJobs(corpus.corpus_id, {
        apply: true,
        limit: 1000,
      })) as SummaryJobPlanResult;
      setSummaryJobPlanResult(result);
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to plan summary jobs");
    } finally {
      setIsPlanningSummaryJobs(false);
    }
  };

  const handleRunSummaryJobs = async () => {
    setIsRunningSummaryJobs(true);
    setError(null);
    try {
      const result = (await api.runCorpusSummaryJobs(corpus.corpus_id, {
        limit: 25,
      })) as SummaryJobRunResult;
      setSummaryJobRunResult(result);
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to run summary jobs");
    } finally {
      setIsRunningSummaryJobs(false);
    }
  };

  const handlePlanDocumentPipelineJobs = async () => {
    setIsPlanningDocumentPipelineJobs(true);
    setError(null);
    try {
      const result = (await api.planCorpusDocumentPipelineJobs(corpus.corpus_id, {
        apply: true,
        limit: 1000,
      })) as DocumentPipelineJobPlanResult;
      setDocumentPipelineJobPlanResult(result);
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to plan document pipeline jobs");
    } finally {
      setIsPlanningDocumentPipelineJobs(false);
    }
  };

  const handleRunDocumentPipelineJobs = async () => {
    setIsRunningDocumentPipelineJobs(true);
    setError(null);
    try {
      const result = (await api.runCorpusDocumentPipelineJobs(corpus.corpus_id, {
        limit: 25,
      })) as DocumentPipelineJobRunResult;
      setDocumentPipelineJobRunResult(result);
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to run document pipeline jobs");
    } finally {
      setIsRunningDocumentPipelineJobs(false);
    }
  };

  const handlePlanSourceParseJobs = async () => {
    setIsPlanningSourceParseJobs(true);
    setError(null);
    try {
      const result = (await api.planCorpusSourceParseJobs(corpus.corpus_id, {
        apply: true,
        limit: 1000,
      })) as SourceParseJobPlanResult;
      setSourceParseJobPlanResult(result);
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to plan source parse jobs");
    } finally {
      setIsPlanningSourceParseJobs(false);
    }
  };

  const handleRunSourceParseJobs = async () => {
    setIsRunningSourceParseJobs(true);
    setError(null);
    try {
      const result = (await api.runCorpusSourceParseJobs(corpus.corpus_id, {
        limit: 25,
      })) as SourceParseJobRunResult;
      setSourceParseJobRunResult(result);
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to run source parse jobs");
    } finally {
      setIsRunningSourceParseJobs(false);
    }
  };

  const handleInspectDurableJobs = async () => {
    setIsLoadingDurableJobs(true);
    setError(null);
    try {
      const result = await api.listCorpusDurableJobs(corpus.corpus_id, {
        status: [
          "dead_letter",
          "failed",
          "provider_failed",
          "validation_failed",
          "blocked_empty_source",
          "blocked_provider_contract",
        ],
        limit: 100,
      });
      setDurableJobs(result.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to inspect durable jobs");
    } finally {
      setIsLoadingDurableJobs(false);
    }
  };

  const handleControlDurableJob = async (
    job: api.DurableIngestionJob,
    action: "retry" | "supersede" | "dead_letter",
  ) => {
    setControllingJobId(job.job_id);
    setError(null);
    try {
      await api.controlCorpusDurableJob(corpus.corpus_id, job.lane, job.job_id, {
        action,
        reason: `Operator ${action} from corpus artifact/queue audit`,
      });
      await handleInspectDurableJobs();
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} durable job`);
    } finally {
      setControllingJobId(null);
    }
  };

  const handleRunExtractionJobs = async () => {
    setIsRunningExtractionJobs(true);
    setError(null);
    try {
      const result = (await api.runCorpusExtractionJobs(corpus.corpus_id, {
        limit: 25,
      })) as ExtractionJobRunResult;
      setExtractionJobRunResult(result);
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to run extraction jobs");
    } finally {
      setIsRunningExtractionJobs(false);
    }
  };

  const handleIdentityAudit = async () => {
    setIsAuditingIdentity(true);
    setError(null);
    try {
      const result = (await api.auditCorpusIdempotency(corpus.corpus_id, {
        group_limit: 10,
        missing_limit: 10,
      })) as IdentityAuditResult;
      setIdentityAuditResult(result);
      onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to audit source identity");
    } finally {
      setIsAuditingIdentity(false);
    }
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };

  const getWriteStateColor = (state: WriteState) => {
    if (state.verified === false) return "text-error";
    if (getWriteStateMessages(state).length > 0) return "text-amber-300";
    if (state.mongo_written && state.qdrant_written && state.neo4j_written)
      return "text-accent-main";
    if (state.mongo_written && state.qdrant_written)
      return "text-accent-secondary";
    if (state.mongo_written) return "text-content-secondary";
    return "text-error";
  };

  const getWriteStateLabel = (state: WriteState) => {
    const hasWarnings = getWriteStateMessages(state).length > 0;
    if (state.verified === false) return "VERIFY_FAIL";
    if (state.mongo_written && state.qdrant_written && state.neo4j_written)
      return hasWarnings ? "COMPLETE_WARN" : "COMPLETE";
    if (state.mongo_written && state.qdrant_written) return "PARTIAL";
    if (state.mongo_written) return "MONGO_ONLY";
    return "PENDING";
  };

  const localBatchStaleCount =
    localBatch?.items?.filter((item) => item.phase === "stale").length ?? 0;
  const batchReport = localBatch?.report;
  const batchRouteCounts =
    batchReport?.ghost_b_provider_call_counts ??
    batchReport?.ghost_b_model_call_counts ??
    {};
  const batchRouteEntries = topCountEntries(batchRouteCounts);
  const readiness = corpus.readiness;
  const readinessDocs = readiness?.documents;
  const readinessGraph = readiness?.graph;
  const readinessSummaries = readiness?.summaries;
  const retrievalSummaryScope = summaryScope(
    readinessSummaries?.scopes as Record<string, SummaryScopeValue> | undefined,
    "retrieval_parent",
  );
  const allParentSummaryScope = summaryScope(
    readinessSummaries?.scopes as Record<string, SummaryScopeValue> | undefined,
    "all_parent",
  );
  const documentSummaryScope = summaryScope(
    readinessSummaries?.scopes as Record<string, SummaryScopeValue> | undefined,
    "document",
  );
  const documentSummaryDrift = readinessSummaries?.document_mismatch ?? 0;
  const documentSummarySyncedDone =
    readinessSummaries?.document_synced_done ?? readinessSummaries?.document_done;
  const summaryExcludedParentTotal = readinessSummaries?.summary_excluded_parent_total ?? 0;
  const graphMetadataMarkDocs =
    readinessGraph?.unmarked_promoted_extraction_docs ??
    readinessGraph?.unpromoted_extraction_docs ??
    0;
  const graphMetadataMarkRows =
    readinessGraph?.unmarked_promoted_extraction_rows ??
    readinessGraph?.unpromoted_extraction_rows ??
    0;
  const readinessIdempotency = readiness?.idempotency;
  const readinessRepair = readiness?.repair;
  const graphRunnableJobCount =
    (readinessRepair?.graph_promotion_jobs?.queued ?? 0) +
    (readinessRepair?.graph_promotion_jobs?.running ?? 0);
  const graphStaleJobCount =
    (readinessRepair?.graph_promotion_jobs?.queued_stale ?? 0) +
    (readinessRepair?.graph_promotion_jobs?.running_stale ?? 0);
  const graphPromotionWorkAvailable =
    readinessGraph?.required !== false &&
    (graphRunnableJobCount > 0 || (readinessGraph?.pending ?? 0) > 0);
  const providerCooldownLanes =
    readinessRepair?.provider_lane_health?.lanes?.filter(
      (lane) => lane.status === "cooldown",
    ) ?? [];
  const readinessPressure = readiness?.pressure;
  const summaryGenerationPaused =
    readinessPressure?.backpressure?.summary_generation_allowed === false ||
    readinessPressure?.backpressure?.summary_backfill_allowed === false;
  const summaryIndexingPaused =
    readinessPressure?.backpressure?.summary_indexing_allowed === false;
  const summaryIndexDeferredNotice =
    summaryIndexingPaused
      ? " Qdrant indexing is paused by pressure; generated summaries will be stored and vector indexing will be deferred."
      : "";
  const summaryBackfillTitle = summaryGenerationPaused
    ? "Summary generation is paused by ingestion pressure; wait for active repairs/write queues to drain."
    : `Queue a bounded background run for up to 500 missing retrieval parent summaries.${summaryIndexDeferredNotice}`;
  const summaryJobRunTitle = summaryGenerationPaused
    ? "Summary generation is paused by storage/provider pressure."
    : `Run up to 25 queued retrieval-parent/document summary jobs and reconcile against stored artifacts.${summaryIndexDeferredNotice}`;
  const readinessComputedAt = readiness?.computed_at;
  const readinessNextActions = (readiness?.next_actions ?? []).slice(0, 4);

  return (
    <div className="flex flex-col h-full relative">
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-3 border-b border-border-minimal bg-bg-surface shrink-0">
        <button
          onClick={onBack}
          className="p-1 text-content-tertiary hover:text-accent-main transition-none"
        >
          <ChevronLeft className="w-4 h-4" />
        </button>
        <div className="flex-1 min-w-0">
          <div className="text-[11px] font-bold text-content-primary truncate">
            {corpus.name}
          </div>
          {corpus.description && (
            <div className="text-[9px] text-content-tertiary truncate">
              {corpus.description}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2 text-[9px] text-content-tertiary tracking-wider shrink-0">
          <span>{corpus.doc_count} docs</span>
          <span>{corpus.chunk_count} chunks</span>
          {onEditConfig && (
            <button
              onClick={() => onEditConfig(corpus)}
              className="flex items-center gap-1 px-2 py-1 ml-1 text-[9px] font-bold tracking-widest text-accent-main border border-accent-main hover:bg-accent-main hover:text-bg-base transition-none uppercase"
              title="Edit ingestion models, API keys, and schema"
            >
              <Settings2 className="w-3 h-3" />
              <span>Edit Models</span>
            </button>
          )}
        </div>
      </div>

      {/* Ingest Bar */}
      <div className="flex flex-col border-b border-border-minimal bg-bg-surface/50 shrink-0">
      <div className="flex items-center justify-between px-4 py-2">
        <div data-testid="upload-status" className="text-[9px] font-bold tracking-widest text-content-tertiary uppercase">
          {`DOCUMENTS · ${documents.length}`}
        </div>
        <div className="flex items-center gap-1.5">
          <button
            onClick={() => setShowOverrides(!showOverrides)}
            className={`flex items-center gap-1.5 px-2 py-1 text-[9px] font-bold tracking-widest border transition-none uppercase ${
              Object.keys(overrides).length > 0
                ? "border-amber-400 text-amber-300 hover:bg-amber-400/10"
                : "border-border-minimal text-content-tertiary hover:border-content-secondary hover:text-content-secondary"
            }`}
            title="Override embed/ghost models for this batch only"
          >
            <SlidersHorizontal className="w-3 h-3" />
            <span>
              Overrides
              {Object.keys(overrides).length > 0 &&
                ` (${Object.keys(overrides).length})`}
            </span>
          </button>
          <button
            onClick={() => setShowLocalBatch((open) => !open)}
            className={`flex items-center gap-1.5 px-2 py-1 text-[9px] font-bold tracking-widest border transition-none uppercase ${
              showLocalBatch || localBatch
                ? "border-accent-main text-accent-main hover:bg-accent-main hover:text-bg-base"
                : "border-border-minimal text-content-tertiary hover:border-content-secondary hover:text-content-secondary"
            }`}
            title="Start a durable backend-owned folder ingest"
          >
            <FolderOpen className="w-3 h-3" />
            <span>Ingest Folder</span>
          </button>
          <button
            onClick={() => setShowDuplicates((open) => !open)}
            className={`flex items-center gap-1.5 px-2 py-1 text-[9px] font-bold tracking-widest border transition-none uppercase ${
              showDuplicates
                ? "border-accent-main text-accent-main hover:bg-accent-main hover:text-bg-base"
                : "border-border-minimal text-content-tertiary hover:border-content-secondary hover:text-content-secondary"
            }`}
            title="Detect and remove near-duplicate documents in this corpus"
          >
            <Copy className="w-3 h-3" />
            <span>Duplicates</span>
          </button>
          <button
            onClick={() => quickUploadInputRef.current?.click()}
            disabled={isQuickUploading}
            className="flex items-center gap-1.5 px-2 py-1 text-[9px] font-bold tracking-widest text-accent-main border border-accent-main hover:bg-accent-main hover:text-bg-base disabled:opacity-50 disabled:hover:bg-transparent disabled:hover:text-accent-main transition-none uppercase"
            title="Upload one or a few files as a durable resumable batch"
          >
            {isQuickUploading ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <Upload className="w-3 h-3" />
            )}
            <span>{isQuickUploading ? "Uploading" : "Quick Upload"}</span>
          </button>
          <input
            ref={quickUploadInputRef}
            type="file"
            multiple
            onChange={handleQuickUpload}
            className="hidden"
            accept=".pdf,.epub,.doc,.docx,.rtf,.odt,.txt,.text,.md,.markdown,.html,.htm,.xhtml"
          />
        </div>
      </div>
        <IngestionProgressBar documents={documents} />
      </div>

      {readiness && (
        <div className="border-b border-border-minimal bg-bg-base/80 px-4 py-2 shrink-0">
          <div className="flex flex-col gap-2 xl:flex-row xl:items-center xl:justify-between">
            <div className="flex items-center gap-2 min-w-0">
              <BookOpen className="w-3.5 h-3.5 text-accent-secondary shrink-0" />
              <div className="text-[9px] font-bold tracking-widest text-content-tertiary uppercase">
                Corpus truth
              </div>
              <span
                className={`px-2 py-0.5 border text-[9px] font-bold tracking-widest uppercase ${readinessTone(readiness.status)}`}
                title={[
                  readinessComputedAt ? `Computed ${formatDate(readinessComputedAt)}` : null,
                  readiness.source ? `source=${readiness.source}` : null,
                  readiness.stale ? "stale snapshot" : null,
                  readiness.refresh_error ? `refresh error: ${readiness.refresh_error}` : null,
                  (readiness.blocking ?? []).join(" · ") || "No blocking readiness reason reported",
                ]
                  .filter(Boolean)
                  .join(" · ")}
              >
                {readinessLabel(readiness.status)}
              </span>
              {readinessComputedAt && (
                <span
                  className={`text-[9px] ${readiness.stale ? "text-amber-300" : "text-content-tertiary"}`}
                  title={`Corpus truth snapshot computed at ${formatDate(readinessComputedAt)}`}
                >
                  truth {formatDate(readinessComputedAt)}
                </span>
              )}
              {(readinessRepair?.active_runs ?? 0) > 0 && (
                <span className="text-[9px] text-accent-secondary">
                  {readinessRepair?.active_runs} repair active
                </span>
              )}
              {readinessPressure && readinessPressure.status !== "normal" && (
                <span
                  className={`px-1.5 py-0.5 border text-[9px] font-bold tracking-widest uppercase ${
                    readinessPressure.status === "high"
                      ? "border-error/60 text-error"
                      : "border-amber-400/50 text-amber-300"
                  }`}
                  title={[
                    `RSS pressure: ${
                      typeof readinessPressure.resources?.rss_pressure === "number"
                        ? Math.round(readinessPressure.resources.rss_pressure * 100)
                        : "unknown"
                    }%`,
                    `Active repairs: ${readinessPressure.queues?.active_repairs ?? 0}`,
                    `Graph queue: ${readinessPressure.queues?.graph_pending ?? 0}`,
                    `Extraction queue: ${readinessPressure.queues?.extraction_pending ?? 0}`,
                    `Qdrant writer: ${readinessPressure.writers?.qdrant?.status ?? "unknown"}`,
                    `Qdrant queue: ${readinessPressure.writers?.qdrant?.queue_depth ?? 0}`,
                    `Qdrant write latency: ${
                      typeof readinessPressure.writers?.qdrant?.write_latency_ms === "number"
                        ? `${Math.round(readinessPressure.writers.qdrant.write_latency_ms)}ms`
                        : "unknown"
                    }`,
                    `Qdrant memory: ${
                      typeof readinessPressure.writers?.qdrant?.memory_pressure === "number"
                        ? `${Math.round(readinessPressure.writers.qdrant.memory_pressure * 100)}%`
                        : "unknown"
                    }`,
                    `Summary generation: ${
                      readinessPressure.backpressure?.summary_generation_allowed === false
                        ? "paused"
                        : "allowed"
                    }`,
                    `Summary indexing: ${
                      readinessPressure.backpressure?.summary_indexing_allowed === false
                        ? "paused"
                        : "allowed"
                    }`,
                    ...(readinessPressure.recommendations ?? []),
                  ].join(" · ")}
                >
                  pressure {readinessPressure.status}
                </span>
              )}
              {graphRunnableJobCount > 0 && (
                <span className="text-[9px] text-accent-secondary">
                  {graphRunnableJobCount} graph jobs
                </span>
              )}
              {graphStaleJobCount > 0 && graphRunnableJobCount <= 0 && (
                <span
                  className="text-[9px] text-content-tertiary"
                  title="Stale graph-promotion job rows are retained for audit but no longer count as runnable work because corpus artifacts already show those docs are promoted."
                >
                  {graphStaleJobCount} stale graph jobs
                </span>
              )}
              {(((readinessRepair?.document_pipeline_jobs?.queued ?? 0) +
                (readinessRepair?.document_pipeline_jobs?.blocked_no_source ?? 0) +
                (readinessRepair?.document_pipeline_jobs?.blocked_missing_chunks ?? 0) +
                (readinessRepair?.document_pipeline_jobs?.blocked_mongo_state ?? 0)) > 0) && (
                <span className="text-[9px] text-amber-300">
                  {(readinessRepair?.document_pipeline_jobs?.queued ?? 0) +
                    (readinessRepair?.document_pipeline_jobs?.blocked_no_source ?? 0) +
                    (readinessRepair?.document_pipeline_jobs?.blocked_missing_chunks ?? 0) +
                    (readinessRepair?.document_pipeline_jobs?.blocked_mongo_state ?? 0)} pipeline jobs
                </span>
              )}
              {(((readinessRepair?.extraction_jobs?.queued ?? 0) +
                (readinessRepair?.extraction_jobs?.provider_failed ?? 0) +
                (readinessRepair?.extraction_jobs?.validation_failed ?? 0)) > 0) && (
                <span className="text-[9px] text-amber-300">
                  {(readinessRepair?.extraction_jobs?.queued ?? 0) +
                    (readinessRepair?.extraction_jobs?.provider_failed ?? 0) +
                  (readinessRepair?.extraction_jobs?.validation_failed ?? 0)} extraction jobs
                </span>
              )}
              {providerCooldownLanes.length > 0 && (
                <span
                  className="text-[9px] text-amber-300"
                  title={providerCooldownLanes
                    .slice(0, 6)
                    .map((lane) =>
                      [
                        providerLabel(lane.provider ?? "provider"),
                        lane.model ?? "model",
                        lane.lane != null ? `lane ${lane.lane}` : null,
                        `${lane.rate_limited ?? 0} rate limited`,
                        `${lane.succeeded ?? 0} ok`,
                      ]
                        .filter(Boolean)
                        .join(" · "),
                    )
                    .join(" / ")}
                >
                  {providerCooldownLanes.length} provider cooldown
                </span>
              )}
              {(((readinessRepair?.summary_jobs?.queued ?? 0) +
                (readinessRepair?.summary_jobs?.blocked_no_parent_summaries ?? 0) +
                (readinessRepair?.summary_jobs?.blocked_parent_summaries_incomplete ?? 0)) > 0) && (
                <span className="text-[9px] text-accent-secondary">
                  {(readinessRepair?.summary_jobs?.queued ?? 0) +
                    (readinessRepair?.summary_jobs?.blocked_no_parent_summaries ?? 0) +
                    (readinessRepair?.summary_jobs?.blocked_parent_summaries_incomplete ?? 0)} summary jobs
                </span>
              )}
              {readiness.error && (
                <span className="text-[9px] text-error truncate">
                  readiness unavailable
                </span>
              )}
              {readinessNextActions.length > 0 && (
                <div
                  className="flex items-center gap-1 min-w-0"
                  title="Recommended next actions from durable corpus readiness"
                >
                  <span className="text-[9px] text-content-tertiary uppercase">
                    next
                  </span>
                  {readinessNextActions.map((action) => (
                    <span
                      key={action.id}
                      className={`px-1.5 py-0.5 border text-[9px] font-bold tracking-widest uppercase ${
                        action.blocked_by_pressure
                          ? "border-border-minimal text-content-tertiary"
                          : action.severity === "critical"
                            ? "border-error/60 text-error"
                            : action.severity === "review"
                              ? "border-amber-400/50 text-amber-300"
                              : "border-accent-secondary/50 text-accent-secondary"
                      }`}
                      title={`${action.reason}${action.count ? ` · ${action.count}` : ""}${
                        action.blocked_by_pressure ? " · paused by pressure" : ""
                      }`}
                    >
                      {action.label}
                      {action.count > 0 ? ` ${action.count}` : ""}
                    </span>
                  ))}
                </div>
              )}
              <div className="flex items-center gap-1 shrink-0">
                <button
                  onClick={() => handleRepairCycle(false)}
                  disabled={isRunningRepairCycle}
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-content-tertiary border border-border-minimal hover:border-content-secondary hover:text-content-secondary disabled:opacity-50 transition-none uppercase"
                  title="Dry-run bounded reconciliation and graph repair planning without writing changes"
                >
                  {isRunningRepairCycle ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <Search className="w-3 h-3" />
                  )}
                  <span>Analyze repair</span>
                </button>
                <button
                  onClick={() =>
                    handleRepairCycle(true, {
                      background: true,
                      runExtractionJobs: true,
                      runDocumentPipelineJobs:
                        (readinessRepair?.document_pipeline_jobs_pending ?? 0) > 0 &&
                        readinessPressure?.backpressure?.document_pipeline_allowed !== false,
                      runSummaryJobs:
                        ((readinessRepair?.summary_jobs_pending ?? 0) > 0 ||
                          ((readinessSummaries?.retrieval_parent_missing ??
                            readinessSummaries?.body_parent_missing ??
                            0) +
                            (readinessSummaries?.document_missing ?? 0)) > 0) &&
                        !summaryGenerationPaused,
                      runDocumentSummaries:
                        (readinessSummaries?.document_missing ?? 0) > 0 &&
                        !summaryGenerationPaused,
                      runGraphJobs:
                        graphPromotionWorkAvailable &&
                        readinessPressure?.backpressure?.graph_promotion_allowed !== false,
                    })
                  }
                  disabled={isRunningRepairCycle}
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-accent-main border border-accent-main hover:bg-accent-main hover:text-bg-base disabled:opacity-50 disabled:hover:bg-transparent disabled:hover:text-accent-main transition-none uppercase"
                  title="Queue a bounded background repair: reconcile stale failures, materialize document pipeline/extraction/graph/summary jobs, retry up to 25 extraction chunks, run up to 25 summary jobs, backfill up to 10 document summaries, and run graph jobs only when pressure allows."
                >
                  <RotateCcw className="w-3 h-3" />
                  <span>Auto repair</span>
                </button>
                <button
                  onClick={handleInspectDurableJobs}
                  disabled={isLoadingDurableJobs}
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-content-tertiary border border-border-minimal hover:border-content-secondary hover:text-content-secondary disabled:opacity-50 transition-none uppercase"
                  title="Inspect dead-letter and failed durable jobs without changing artifact truth"
                >
                  {isLoadingDurableJobs ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <FileText className="w-3 h-3" />
                  )}
                  <span>Inspect jobs</span>
                </button>
                <button
                  onClick={() => handleRepairCycle(true, { runGraphJobs: true })}
                  disabled={
                    isRunningRepairCycle ||
                    !graphPromotionWorkAvailable ||
                    readinessPressure?.backpressure?.graph_promotion_allowed === false
                  }
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-amber-300 border border-amber-400/50 hover:bg-amber-400/10 disabled:opacity-50 transition-none uppercase"
                  title={
                    readinessPressure?.backpressure?.graph_promotion_allowed === false
                      ? "Graph promotion is paused by ingestion pressure; queue repair metadata first and let writes drain."
                      : !graphPromotionWorkAvailable
                        ? "No actionable graph-promotion work is pending. Stale graph job rows are audit history, not runnable work."
                        : graphRunnableJobCount > 0
                          ? "Run up to 3 queued graph-promotion jobs."
                          : "Plan missing graph-promotion jobs for pending docs, then run up to 3."
                  }
                >
                  <CheckCircle2 className="w-3 h-3" />
                  <span>Run graph jobs</span>
                </button>
                <button
                  onClick={handleSummaryBackfill}
                  disabled={
                    isQueueingSummaryBackfill ||
                    (readinessSummaries?.retrieval_parent_missing ??
                      readinessSummaries?.body_parent_missing ??
                      0) <= 0 ||
                    summaryGenerationPaused
                  }
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-accent-secondary border border-accent-secondary/50 hover:bg-accent-secondary/10 disabled:opacity-50 transition-none uppercase"
                  title={
                    summaryBackfillTitle
                  }
                >
                  {isQueueingSummaryBackfill ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <BookOpen className="w-3 h-3" />
                  )}
                  <span>Backfill summaries</span>
                </button>
                <button
                  onClick={handlePlanSourceParseJobs}
                  disabled={isPlanningSourceParseJobs}
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-amber-300 border border-amber-400/50 hover:bg-amber-400/10 disabled:opacity-50 transition-none uppercase"
                  title="Materialize source-file and parse progress from durable ingest batch manifests"
                >
                  {isPlanningSourceParseJobs ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <FileText className="w-3 h-3" />
                  )}
                  <span>Plan source jobs</span>
                </button>
                <button
                  onClick={handleRunSourceParseJobs}
                  disabled={
                    isRunningSourceParseJobs ||
                    readinessPressure?.backpressure?.source_parse_allowed === false ||
                    ((readinessRepair?.source_parse_jobs_pending ?? 0) +
                      (readinessRepair?.source_parse_jobs?.failed_recoverable ?? 0)) <= 0
                  }
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-amber-300 border border-amber-400/50 hover:bg-amber-400/10 disabled:opacity-50 transition-none uppercase"
                  title={
                    readinessPressure?.backpressure?.source_parse_allowed === false
                      ? "Source parse jobs are paused by ingestion pressure; wait for Mongo/RSS pressure to drop."
                      : "Resume eligible source/parse jobs through the durable ingest batch runner"
                  }
                >
                  {isRunningSourceParseJobs ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <RotateCcw className="w-3 h-3" />
                  )}
                  <span>Run source jobs</span>
                </button>
                <button
                  onClick={handlePlanDocumentPipelineJobs}
                  disabled={isPlanningDocumentPipelineJobs}
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-amber-300 border border-amber-400/50 hover:bg-amber-400/10 disabled:opacity-50 transition-none uppercase"
                  title="Materialize missing chunk/persist/embed document-stage work as durable jobs without running heavy ingestion"
                >
                  {isPlanningDocumentPipelineJobs ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <Layers className="w-3 h-3" />
                  )}
                  <span>Plan pipeline jobs</span>
                </button>
                <button
                  onClick={handleRunDocumentPipelineJobs}
                  disabled={
                    isRunningDocumentPipelineJobs ||
                    (readinessRepair?.document_pipeline_jobs_pending ?? 0) <= 0 ||
                    readinessPressure?.backpressure?.document_pipeline_allowed === false
                  }
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-amber-300 border border-amber-400/50 hover:bg-amber-400/10 disabled:opacity-50 transition-none uppercase"
                  title={
                    readinessPressure?.backpressure?.document_pipeline_allowed === false
                      ? "Document pipeline repair is paused by ingestion pressure"
                      : "Run/reconcile up to 25 queued chunk/persist/embed document-stage jobs"
                  }
                >
                  {isRunningDocumentPipelineJobs ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <RotateCcw className="w-3 h-3" />
                  )}
                  <span>Run pipeline jobs</span>
                </button>
                <button
                  onClick={handlePlanSummaryJobs}
                  disabled={
                    isPlanningSummaryJobs ||
                    ((readinessSummaries?.retrieval_parent_missing ??
                      readinessSummaries?.body_parent_missing ??
                      0) +
                      (readinessSummaries?.document_missing ?? 0)) <= 0
                  }
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-accent-secondary border border-accent-secondary/50 hover:bg-accent-secondary/10 disabled:opacity-50 transition-none uppercase"
                  title="Materialize missing retrieval-parent and document-summary work as durable jobs without calling summary providers"
                >
                  {isPlanningSummaryJobs ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <Layers className="w-3 h-3" />
                  )}
                  <span>Plan summary jobs</span>
                </button>
                <button
                  onClick={handleRunSummaryJobs}
                  disabled={
                    isRunningSummaryJobs ||
                    (readinessRepair?.summary_jobs_pending ?? 0) <= 0 ||
                    summaryGenerationPaused
                  }
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-accent-secondary border border-accent-secondary/50 hover:bg-accent-secondary/10 disabled:opacity-50 transition-none uppercase"
                  title={
                    summaryJobRunTitle
                  }
                >
                  {isRunningSummaryJobs ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <Zap className="w-3 h-3" />
                  )}
                  <span>Run summary jobs</span>
                </button>
                <button
                  onClick={handlePlanExtractionJobs}
                  disabled={isPlanningExtractionJobs || readinessGraph?.required === false}
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-amber-300 border border-amber-400/50 hover:bg-amber-400/10 disabled:opacity-50 transition-none uppercase"
                  title="Materialize up to 1000 missing or failed chunk extraction jobs without calling providers"
                >
                  {isPlanningExtractionJobs ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <Hash className="w-3 h-3" />
                  )}
                  <span>Plan extraction jobs</span>
                </button>
                <button
                  onClick={handleRunExtractionJobs}
                  disabled={
                    isRunningExtractionJobs ||
                    readinessGraph?.required === false ||
                    readinessPressure?.backpressure?.extraction_backfill_allowed === false ||
                    (((readinessRepair?.extraction_jobs?.queued ?? 0) +
                      (readinessRepair?.extraction_jobs?.provider_failed ?? 0) +
                      (readinessRepair?.extraction_jobs?.validation_failed ?? 0) +
                      (readinessRepair?.extraction_jobs?.failed ?? 0)) <= 0)
                  }
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-amber-300 border border-amber-400/50 hover:bg-amber-400/10 disabled:opacity-50 transition-none uppercase"
                  title={
                    readinessPressure?.backpressure?.extraction_backfill_allowed === false
                      ? "Extraction job execution is paused by ingestion pressure."
                      : "Run up to 25 queued or failed chunk-level extraction jobs through the active Ghost B contract"
                  }
                >
                  {isRunningExtractionJobs ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <Zap className="w-3 h-3" />
                  )}
                  <span>Run extraction jobs</span>
                </button>
                <button
                  onClick={handleIdentityAudit}
                  disabled={isAuditingIdentity}
                  className="flex items-center gap-1 px-2 py-0.5 text-[9px] font-bold tracking-widest text-content-tertiary border border-border-minimal hover:border-content-secondary hover:text-content-secondary disabled:opacity-50 transition-none uppercase"
                  title="Fetch exact duplicate source-key/content-hash groups and documents missing source identity metadata"
                >
                  {isAuditingIdentity ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <Hash className="w-3 h-3" />
                  )}
                  <span>Audit identity</span>
                </button>
              </div>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-11 gap-2 text-[9px] font-mono">
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Queryable docs</div>
                <div className="text-content-primary">
                  {formatCoverage(readinessDocs?.queryable, readinessDocs?.total)}
                </div>
                {(readinessDocs?.excluded_total ?? 0) > 0 && (
                  <div className="text-content-tertiary">
                    {readinessDocs?.excluded_total} duplicates excluded
                  </div>
                )}
              </div>
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Fully enriched</div>
                <div className="text-content-primary">
                  {formatCoverage(readinessDocs?.fully_enriched, readinessDocs?.total)}
                </div>
              </div>
              <div
                className="border border-accent-secondary/40 px-2 py-1"
                title={
                  retrievalSummaryScope.description ??
                  "Retrieval summaries required for readiness. This includes eligible body/table and legacy parent rows, not body-only diagnostics or structural rows."
                }
              >
                <div className="text-content-tertiary uppercase">
                  {retrievalSummaryScope.label ?? "Retrieval summaries"}
                </div>
                <div className="text-accent-secondary">
                  {formatCoverage(
                    readinessSummaries?.retrieval_parent_done ??
                      readinessSummaries?.body_parent_done,
                    readinessSummaries?.retrieval_parent_total ??
                      readinessSummaries?.body_parent_total,
                  )}
                </div>
              </div>
              <div
                className="border border-border-minimal px-2 py-1"
                title={
                  allParentSummaryScope.description ??
                  "Diagnostic count across every parent row, including structural rows that are not required for retrieval-summary readiness."
                }
              >
                <div className="text-content-tertiary uppercase">
                  {allParentSummaryScope.label ?? "All parent rows"}
                </div>
                <div className="text-content-primary">
                  {formatCoverage(
                    readinessSummaries?.all_parent_done ?? readinessSummaries?.parent_done,
                    readinessSummaries?.all_parent_total ?? readinessSummaries?.parent_total,
                  )}
                </div>
                {summaryExcludedParentTotal > 0 && (
                  <div
                    className="mt-0.5 text-[8px] text-content-tertiary"
                    title="Structural parent rows excluded from the retrieval-summary gate."
                  >
                    {summaryExcludedParentTotal} excluded
                  </div>
                )}
              </div>
              <div
                className="border border-border-minimal px-2 py-1"
                title={
                  documentSummaryScope.description ??
                  "Document-level summaries from summary_tree/doc_profile. These summarize whole documents after parent summaries are available."
                }
              >
                <div className="text-content-tertiary uppercase">
                  {documentSummaryScope.label ?? "Document summaries"}
                </div>
                <div className="text-content-primary">
                  {formatCoverage(
                    documentSummarySyncedDone,
                    readinessSummaries?.document_total,
                  )}
                </div>
                {documentSummarySyncedDone !== readinessSummaries?.document_done && (
                  <div
                    className="mt-0.5 text-[8px] text-content-tertiary"
                    title="Usable document summaries present in either doc_profile or summary_tree."
                  >
                    {readinessSummaries?.document_done ?? 0} usable
                  </div>
                )}
                {documentSummaryDrift > 0 && (
                  <div
                    className="mt-0.5 text-[8px] text-warning"
                    title={`doc_profile only: ${readinessSummaries?.document_profile_only ?? 0}; summary_tree only: ${readinessSummaries?.document_tree_only ?? 0}`}
                  >
                    {documentSummaryDrift} artifact drift
                  </div>
                )}
              </div>
              <div
                className={`border px-2 py-1 ${
                  (readinessGraph?.failed_chunks ?? 0) > 0 ||
                  (readinessGraph?.stale_failure_rows ?? 0) > 0 ||
                  graphMetadataMarkRows > 0
                    ? "border-error/40"
                    : "border-border-minimal"
                }`}
              >
                <div className="text-content-tertiary uppercase">Graph state</div>
                <div className="text-content-primary">
                  {readinessGraph?.required === false ? (
                    <span className="text-content-tertiary">not required</span>
                  ) : (
                    <>
                      {readinessGraph?.promoted ?? 0} promoted
                      <span className="text-content-tertiary">
                        {" "}· {readinessGraph?.pending ?? 0} pending
                      </span>
                    </>
                  )}
                </div>
                {graphMetadataMarkRows > 0 && (
                  <div className="text-amber-300">
                    {graphMetadataMarkDocs} docs with
                    <span className="text-content-tertiary">
                      {" "}{graphMetadataMarkRows} graph metadata marks missing
                    </span>
                  </div>
                )}
                {((readinessGraph?.failed_chunks ?? 0) > 0 ||
                  (readinessGraph?.stale_failure_rows ?? 0) > 0) && (
                  <div className="text-error">
                    {readinessGraph?.failed_chunks ?? 0} failed chunks
                    <span className="text-content-tertiary">
                      {" "}· {readinessGraph?.stale_failure_rows ?? 0} stale refs
                    </span>
                  </div>
                )}
                {(readinessGraph?.reconciled_stale_failure_rows ?? 0) > 0 && (
                  <div className="text-content-tertiary">
                    {readinessGraph?.reconciled_stale_failure_rows} reconciled stale
                  </div>
                )}
              </div>
              <div
                className="border border-border-minimal px-2 py-1"
                title="Secret-free provider accounting over the last 24 hours. Artifact truth is not derived from these audit counters."
              >
                <div className="text-content-tertiary uppercase">Provider efficiency</div>
                <div className="text-content-primary">
                  {readinessRepair?.provider_efficiency?.calls ?? 0} calls
                  <span className="text-content-tertiary">
                    {" "}· {readinessRepair?.provider_efficiency?.accepted_artifacts ?? 0} accepted
                  </span>
                </div>
                <div className="text-content-tertiary">
                  {readinessRepair?.provider_efficiency?.calls_per_artifact ?? "n/a"} calls/artifact ·{" "}
                  {readinessRepair?.queue_telemetry?.dead_letter_total ?? 0} dead-letter
                </div>
              </div>
              <div
                className={`border px-2 py-1 ${
                  ((readinessRepair?.extraction_jobs_failed ?? 0) > 0 ||
                    (readinessRepair?.extraction_jobs_blocked ?? 0) > 0)
                    ? "border-error/40"
                    : (readinessRepair?.extraction_jobs_pending ?? 0) > 0
                      ? "border-accent-primary/40"
                      : "border-border-minimal"
                }`}
                title="Durable chunk-level extraction jobs still waiting, running, needing retry, or blocked by provider contract configuration."
              >
                <div className="text-content-tertiary uppercase">Extraction queue</div>
                <div className="text-content-primary">
                  {readinessRepair?.extraction_jobs_pending ?? 0} pending
                </div>
                {(readinessRepair?.extraction_jobs_failed ?? 0) > 0 && (
                  <div className="text-error">
                    {readinessRepair?.extraction_jobs_failed} retry
                  </div>
                )}
                {(readinessRepair?.extraction_jobs_blocked ?? 0) > 0 && (
                  <div className="text-error">
                    {readinessRepair?.extraction_jobs_blocked} contract blocked
                  </div>
                )}
              </div>
              <div
                className={`border px-2 py-1 ${
                  (readinessRepair?.source_parse_jobs_failed ?? 0) > 0
                    ? "border-error/40"
                    : (readinessRepair?.source_parse_jobs_pending ?? 0) > 0
                      ? "border-amber-400/40"
                      : "border-border-minimal"
                }`}
                title="Durable source-file and parse jobs derived from ingest batch manifests."
              >
                <div className="text-content-tertiary uppercase">Source queue</div>
                <div className="text-content-primary">
                  {readinessRepair?.source_parse_jobs_pending ?? 0} pending
                </div>
                {(readinessRepair?.source_parse_jobs_failed ?? 0) > 0 && (
                  <div className="text-error">
                    {readinessRepair?.source_parse_jobs_failed} blocked
                  </div>
                )}
              </div>
              <div
                className={`border px-2 py-1 ${
                  (readinessRepair?.document_pipeline_jobs_failed ?? 0) > 0
                    ? "border-error/40"
                    : (readinessRepair?.document_pipeline_jobs_pending ?? 0) > 0
                      ? "border-amber-400/40"
                      : "border-border-minimal"
                }`}
                title="Durable document-stage jobs for missing chunk, persist, or embedding/index work."
              >
                <div className="text-content-tertiary uppercase">Pipeline queue</div>
                <div className="text-content-primary">
                  {readinessRepair?.document_pipeline_jobs_pending ?? 0} pending
                </div>
                {(readinessRepair?.document_pipeline_jobs_failed ?? 0) > 0 && (
                  <div className="text-error">
                    {readinessRepair?.document_pipeline_jobs_failed} blocked
                  </div>
                )}
              </div>
              <div
                className={`border px-2 py-1 ${
                  (readinessRepair?.summary_jobs_failed ?? 0) > 0
                    ? "border-error/40"
                    : (readinessRepair?.summary_jobs_pending ?? 0) > 0
                      ? "border-accent-secondary/40"
                      : "border-border-minimal"
                }`}
                title="Durable parent/document summary jobs still waiting or blocked."
              >
                <div className="text-content-tertiary uppercase">Summary queue</div>
                <div className="text-content-primary">
                  {readinessRepair?.summary_jobs_pending ?? 0} pending
                </div>
                {(readinessRepair?.summary_jobs_failed ?? 0) > 0 && (
                  <div className="text-error">
                    {readinessRepair?.summary_jobs_failed} blocked
                  </div>
                )}
                {(readinessRepair?.summary_jobs_waiting_dependencies ?? 0) > 0 && (
                  <div className="text-content-tertiary">
                    {readinessRepair?.summary_jobs_waiting_dependencies} waiting
                  </div>
                )}
              </div>
              <div
                className={`border px-2 py-1 ${
                  ((readinessIdempotency?.duplicate_source_key_groups ?? 0) -
                    (readinessIdempotency?.source_key_collision_groups ?? 0)) > 0 ||
                  (readinessIdempotency?.source_key_collision_groups ?? 0) > 0 ||
                  (readinessIdempotency?.duplicate_content_hash_groups ?? 0) > 0 ||
                  (readinessIdempotency?.missing_source_identity ?? 0) > 0 ||
                  (readinessIdempotency?.stage_identity_blocking_total ??
                    readinessIdempotency?.stage_identity_missing_total ??
                    0) > 0
                    ? "border-amber-400/50"
                    : "border-border-minimal"
                }`}
                title={[
                  "Exact source identity, content-hash, and stage-identity audit for duplicate/idempotent ingestion.",
                  `Missing source identity: ${readinessIdempotency?.missing_source_identity ?? 0}`,
                  `Active stage identity gaps: ${readinessIdempotency?.stage_identity_blocking_total ?? readinessIdempotency?.stage_identity_missing_total ?? 0}`,
                  `Legacy successful extraction artifacts missing identity: ${readinessIdempotency?.ghost_b_extractions_missing_stage_identity_legacy_ok ?? 0}`,
                  `Source/parse stage gaps: ${readinessIdempotency?.source_parse_jobs_missing_stage_identity ?? 0}`,
                  `Source-key collision groups: ${readinessIdempotency?.source_key_collision_groups ?? 0}`,
                  `Exact source duplicate groups: ${Math.max(
                    (readinessIdempotency?.duplicate_source_key_groups ?? 0) -
                      (readinessIdempotency?.source_key_collision_groups ?? 0),
                    0,
                  )}`,
                  `Duplicate content groups: ${readinessIdempotency?.duplicate_content_hash_groups ?? 0}`,
                ].join(" · ")}
              >
                <div className="text-content-tertiary uppercase">Identity audit</div>
                <div className="text-content-primary">
                  {readinessIdempotency?.source_keyed_documents ?? 0} keyed
                </div>
                {((readinessIdempotency?.missing_source_identity ?? 0) > 0 ||
                  (readinessIdempotency?.stage_identity_blocking_total ??
                    readinessIdempotency?.stage_identity_missing_total ??
                    0) > 0) && (
                  <div className="text-amber-300">
                    {readinessIdempotency?.missing_source_identity ?? 0} source gap ·{" "}
                    {readinessIdempotency?.stage_identity_blocking_total ??
                      readinessIdempotency?.stage_identity_missing_total ??
                      0} active stage gap
                  </div>
                )}
                {((readinessIdempotency?.ghost_b_extractions_missing_stage_identity_legacy_ok ?? 0) > 0) && (
                  <div className="text-content-tertiary">
                    {readinessIdempotency?.ghost_b_extractions_missing_stage_identity_legacy_ok ?? 0} legacy artifact gaps
                  </div>
                )}
                {(((readinessIdempotency?.duplicate_source_key_groups ?? 0) -
                  (readinessIdempotency?.source_key_collision_groups ?? 0) > 0) ||
                  ((readinessIdempotency?.duplicate_content_hash_groups ?? 0) > 0)) && (
                  <div className="text-amber-300">
                    {Math.max(
                      (readinessIdempotency?.duplicate_source_key_groups ?? 0) -
                        (readinessIdempotency?.source_key_collision_groups ?? 0),
                      0,
                    ) +
                      (readinessIdempotency?.duplicate_content_hash_groups ?? 0)} dup groups
                  </div>
                )}
                {(readinessIdempotency?.source_key_collision_groups ?? 0) > 0 && (
                  <div className="text-amber-300">
                    {readinessIdempotency?.source_key_collision_groups ?? 0} source-key collisions
                  </div>
                )}
              </div>
            </div>
            {durableJobs.length > 0 && (
              <div className="xl:col-span-2 border-t border-border-minimal pt-2 space-y-1">
                <div className="flex items-center justify-between text-[9px] uppercase tracking-widest">
                  <span className="text-content-secondary">Queue audit history</span>
                  <span className="text-content-tertiary">{durableJobs.length} inspectable blockers</span>
                </div>
                {durableJobs.slice(0, 20).map((job) => (
                  <div
                    key={`${job.lane}:${job.job_id}`}
                    className="grid grid-cols-[90px_minmax(0,1fr)_auto] items-center gap-2 border border-border-minimal px-2 py-1 text-[9px]"
                  >
                    <span className="text-content-tertiary uppercase">{job.lane}</span>
                    <span className="min-w-0 truncate text-content-secondary" title={job.last_actionable_error ?? job.job_id}>
                      {job.failure_class ?? job.status} · attempts {job.attempt_count ?? 0} · {job.job_id}
                    </span>
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => handleControlDurableJob(job, "retry")}
                        disabled={controllingJobId === job.job_id}
                        className="p-1 text-accent-main hover:bg-accent-main hover:text-bg-base disabled:opacity-50"
                        title="Explicitly reset the attempt ceiling and retry this job"
                      >
                        <RotateCcw className="w-3 h-3" />
                      </button>
                      <button
                        onClick={() => handleControlDurableJob(job, "supersede")}
                        disabled={controllingJobId === job.job_id}
                        className="p-1 text-content-tertiary hover:text-content-primary disabled:opacity-50"
                        title="Keep this job as audit history but remove it from readiness"
                      >
                        <Check className="w-3 h-3" />
                      </button>
                      {job.status !== "dead_letter" && (
                        <button
                          onClick={() => handleControlDurableJob(job, "dead_letter")}
                          disabled={controllingJobId === job.job_id}
                          className="p-1 text-error hover:bg-error hover:text-bg-base disabled:opacity-50"
                          title="Stop automatic retries and move this job to dead-letter"
                        >
                          <X className="w-3 h-3" />
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
            {repairCycleResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Repair {repairCycleResult.background ? "queued" : repairCycleResult.apply ? "applied" : "planned"} ·
                {repairCycleResult.run_id ? ` ${repairCycleResult.run_id.slice(0, 18)} ·` : ""}
                {" "}{repairCycleResult.summary?.graph_jobs_queued ?? 0} graph queued ·
                {" "}{repairCycleResult.summary?.graph_jobs_blocked ?? 0} blocked ·
                {" "}{repairCycleResult.summary?.promoted_extraction_marks_backfilled ?? 0} graph metadata marks fixed ·
                {" "}{repairCycleResult.summary?.source_parse_jobs_queued ?? 0} source queued ·
                {" "}{repairCycleResult.summary?.source_parse_jobs_blocked ?? 0} source blocked ·
                {" "}{repairCycleResult.summary?.source_parse_jobs_started ?? 0} source runners ·
                {" "}{repairCycleResult.summary?.document_pipeline_jobs_queued ?? 0} pipeline queued ·
                {" "}{repairCycleResult.summary?.document_pipeline_jobs_blocked ?? 0} pipeline blocked ·
                {" "}{repairCycleResult.summary?.document_pipeline_jobs_ran ?? 0} pipeline ran ·
                {" "}{repairCycleResult.summary?.document_pipeline_jobs_succeeded ?? 0} pipeline succeeded ·
                {" "}{repairCycleResult.summary?.failed_chunks ?? 0} failed chunks ·
                {" "}{repairCycleResult.summary?.extraction_jobs_queued ?? 0} extraction queued ·
                {" "}{repairCycleResult.summary?.extraction_jobs_failed ?? 0} extraction failed ·
                {" "}{repairCycleResult.summary?.extraction_jobs_blocked ?? 0} extraction blocked ·
                {" "}{repairCycleResult.summary?.summary_jobs_queued ?? 0} summary queued ·
                {" "}{repairCycleResult.summary?.summary_jobs_waiting_dependencies ?? 0} summary waiting ·
                {" "}{repairCycleResult.summary?.summary_jobs_blocked ?? 0} summary blocked ·
                {" "}{repairCycleResult.summary?.summary_jobs_ran ?? 0} summary ran ·
                {" "}{repairCycleResult.summary?.summary_jobs_succeeded ?? 0} summary succeeded ·
                {" "}{repairCycleResult.summary?.main_summary_missing ?? 0} retrieval parent summaries missing ·
                {" "}{repairCycleResult.summary?.document_summaries_built ?? 0} doc summaries built
              </div>
            )}
            {summaryBackfillResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Summary repair {summaryBackfillResult.status ?? "queued"} ·
                {" "}limit {summaryBackfillResult.limit ?? 500}
                {typeof summaryBackfillResult.generated === "number" && (
                  <> · generated {summaryBackfillResult.generated}</>
                )}
                {typeof summaryBackfillResult.indexed === "number" && (
                  <> · indexed {summaryBackfillResult.indexed}</>
                )}
                {summaryBackfillResult.index_deferred_by_pressure && (
                  <> · indexing deferred by Qdrant pressure</>
                )}
                {summaryBackfillResult.run_id && (
                  <> · run {summaryBackfillResult.run_id.slice(0, 18)}</>
                )}
              </div>
            )}
            {identityAuditResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Identity audit {identityAuditResult.status ?? "unknown"} ·
                {" "}{identityAuditResult.source_keyed_documents ?? 0}/{identityAuditResult.doc_total ?? 0} keyed ·
                {" "}{identityAuditResult.missing_source_identity_count ?? 0} missing ·
                {" "}{(identityAuditResult.duplicate_source_key_group_count ?? 0) +
                  (identityAuditResult.duplicate_content_hash_group_count ?? 0)} dup groups
                {identityAuditAction && (
                  <div className="mt-1 text-amber-300">{identityAuditAction}</div>
                )}
              </div>
            )}
            {extractionJobPlanResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Extraction jobs {extractionJobPlanResult.status ?? "planned"} ·
                {" "}{extractionJobPlanResult.planned ?? 0} planned ·
                {" "}{extractionJobPlanResult.counts?.queued ?? 0} queued ·
                {" "}{extractionJobPlanResult.counts?.provider_failed ?? 0} provider failed ·
                {" "}{extractionJobPlanResult.counts?.validation_failed ?? 0} validation failed ·
                {" "}{extractionJobPlanResult.counts?.blocked_provider_contract ?? 0} contract blocked
              </div>
            )}
            {documentPipelineJobPlanResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Pipeline jobs {documentPipelineJobPlanResult.status ?? "planned"} ·
                {" "}{documentPipelineJobPlanResult.planned ?? 0} planned ·
                {" "}{documentPipelineJobPlanResult.counts?.queued ?? 0} queued ·
                {" "}{(documentPipelineJobPlanResult.counts?.blocked_no_source ?? 0) +
                  (documentPipelineJobPlanResult.counts?.blocked_missing_chunks ?? 0) +
                  (documentPipelineJobPlanResult.counts?.blocked_mongo_state ?? 0) +
                  (documentPipelineJobPlanResult.counts?.failed ?? 0)} blocked
              </div>
            )}
            {documentPipelineJobRunResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Pipeline run {documentPipelineJobRunResult.status ?? "complete"} ·
                {" "}{documentPipelineJobRunResult.claimed ?? 0} claimed ·
                {" "}{documentPipelineJobRunResult.reclaimed ?? 0} reclaimed ·
                {" "}{documentPipelineJobRunResult.source_claimed ?? 0} source-backed ·
                {" "}{documentPipelineJobRunResult.source_requested ? "source requested" : "source unchanged"} ·
                {" "}{documentPipelineJobRunResult.executor_missing_kinds?.length
                  ? `missing executor: ${documentPipelineJobRunResult.executor_missing_kinds.join(", ")} ·`
                  : ""}{" "}
                {" "}{documentPipelineJobRunResult.counts?.succeeded ?? 0} succeeded ·
                {" "}{documentPipelineJobRunResult.counts?.failed ?? 0} failed
              </div>
            )}
            {sourceParseJobPlanResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Source jobs {sourceParseJobPlanResult.status ?? "planned"} ·
                {" "}{sourceParseJobPlanResult.planned ?? 0} planned ·
                {" "}{sourceParseJobPlanResult.counts?.queued ?? 0} queued ·
                {" "}{sourceParseJobPlanResult.counts?.running ?? 0} running ·
                {" "}{(sourceParseJobPlanResult.counts?.blocked_source_missing ?? 0) +
                  (sourceParseJobPlanResult.counts?.failed_recoverable ?? 0) +
                  (sourceParseJobPlanResult.counts?.failed ?? 0)} blocked
              </div>
            )}
            {sourceParseJobRunResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Source run {sourceParseJobRunResult.status ?? "requested"} ·
                {" "}{sourceParseJobRunResult.eligible_items ?? 0}/{sourceParseJobRunResult.requested ?? 0} eligible ·
                {" "}{sourceParseJobRunResult.batch_count ?? 0} batches ·
                {" "}{sourceParseJobRunResult.runners_started ?? 0} runners
                {sourceParseJobRunResult.runner_deferred ? " · deferred to ingest worker" : ""}
              </div>
            )}
            {summaryJobPlanResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Summary jobs {summaryJobPlanResult.status ?? "planned"} ·
                {" "}{summaryJobPlanResult.planned ?? 0} planned ·
                {" "}{summaryJobPlanResult.counts?.queued ?? 0} queued ·
                {" "}{(summaryJobPlanResult.counts?.blocked_no_parent_summaries ?? 0) +
                  (summaryJobPlanResult.counts?.blocked_parent_summaries_incomplete ?? 0)} waiting ·
                {" "}{(summaryJobPlanResult.counts?.blocked_empty_source ?? 0) +
                  (summaryJobPlanResult.counts?.failed ?? 0)} blocked
              </div>
            )}
            {summaryJobRunResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Summary run {summaryJobRunResult.status ?? "complete"} ·
                {" "}{summaryJobRunResult.claimed ?? 0} claimed ·
                {" "}{summaryJobRunResult.reclaimed ?? 0} reclaimed ·
                {" "}{summaryJobRunResult.parent_claimed ?? 0} parent ·
                {" "}{summaryJobRunResult.document_claimed ?? 0} document ·
                {" "}{summaryJobRunResult.counts?.succeeded ?? 0} succeeded ·
                {" "}{summaryJobRunResult.counts?.failed ?? 0} failed
                {summaryJobRunResult.runner_results?.retrieval_parent_summary?.index_deferred_by_pressure
                  ? " · parent indexing deferred by Qdrant pressure"
                  : ""}
              </div>
            )}
            {extractionJobRunResult && (
              <div className="xl:col-span-2 text-[9px] font-mono text-content-tertiary">
                Extraction run {extractionJobRunResult.status ?? "complete"} ·
                {" "}{extractionJobRunResult.claimed ?? 0} claimed ·
                {" "}{extractionJobRunResult.reclaimed ?? 0} reclaimed ·
                {" "}{extractionJobRunResult.counts?.succeeded ?? 0} succeeded ·
                {" "}{extractionJobRunResult.counts?.provider_failed ?? 0} provider failed ·
                {" "}{extractionJobRunResult.counts?.validation_failed ?? 0} validation failed ·
                {" "}{extractionJobRunResult.counts?.blocked_provider_contract ?? 0} contract blocked
              </div>
            )}
          </div>
        </div>
      )}

      {showDuplicates && (
        <DuplicatesPanel
          corpusId={corpus.corpus_id}
          onResolved={async () => {
            await loadDocuments();
            try {
              onCorpusUpdated(await api.getCorpus(corpus.corpus_id));
            } catch {
              /* corpus count refresh is best-effort */
            }
          }}
        />
      )}

      {showLocalBatch && (
        <div className="border-b border-border-minimal bg-bg-base/70 px-4 py-3 shrink-0">
          <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(210px,0.45fr)_auto_auto_auto] gap-2 items-end">
            <label className="min-w-0">
              <span className="block text-[9px] font-bold tracking-widest text-content-tertiary uppercase mb-1">
                Source Folder
              </span>
              <input
                value={localBatchPath}
                onChange={(e) => setLocalBatchPath(e.target.value)}
                className="w-full h-8 px-2 bg-bg-surface border border-border-minimal text-[11px] text-content-primary font-mono outline-none focus:border-accent-main"
                placeholder="/ingest-source/authentic_files"
              />
            </label>
            <div
              className="h-8 flex items-center justify-between gap-2 px-2 bg-bg-surface border border-border-minimal"
              title="Derived from this corpus extraction and embedding contract. The backend owns resource planning."
            >
              <div className="min-w-0">
                <div className="text-[8px] font-bold tracking-widest uppercase text-content-tertiary">
                  Run Contract
                </div>
                <div className="text-[10px] text-content-primary truncate">
                  {PROFILE_LABELS[localBatchProfile]} · auto workers
                </div>
              </div>
              <CheckCircle2 className="w-3.5 h-3.5 text-accent-secondary shrink-0" />
            </div>
            <button
              onClick={handleStartLocalBatch}
              disabled={isStartingLocalBatch}
              className="h-8 flex items-center justify-center gap-1.5 px-3 text-[9px] font-bold tracking-widest text-accent-main border border-accent-main hover:bg-accent-main hover:text-bg-base disabled:opacity-50 disabled:hover:bg-transparent disabled:hover:text-accent-main transition-none uppercase"
            >
              {isStartingLocalBatch ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                <FolderOpen className="w-3 h-3" />
              )}
              <span>Run</span>
            </button>
            <button
              onClick={handleResumeLocalBatch}
              disabled={!localBatch || isStartingLocalBatch}
              className="h-8 flex items-center justify-center gap-1.5 px-3 text-[9px] font-bold tracking-widest text-content-secondary border border-border-minimal hover:border-content-secondary disabled:opacity-40 transition-none uppercase"
            >
              <RotateCcw className="w-3 h-3" />
              <span>Resume</span>
            </button>
            <button
              onClick={handleRescanLocalBatch}
              disabled={!localBatch || isStartingLocalBatch}
              className="h-8 flex items-center justify-center gap-1.5 px-3 text-[9px] font-bold tracking-widest text-content-secondary border border-border-minimal hover:border-content-secondary disabled:opacity-40 transition-none uppercase"
              title="Rescan the original backend folder and append new files to this batch"
            >
              <FolderOpen className="w-3 h-3" />
              <span>Sync</span>
            </button>
          </div>
          {localBatch && (
            <>
            <div
              className="mt-2 flex flex-wrap items-center gap-2 text-[9px]"
              title="Batch state is run history. Corpus truth above is authoritative for queryability, graph, and summaries."
            >
              <span className="font-bold tracking-widest uppercase text-content-tertiary">
                Latest run log
              </span>
              <span className="text-content-tertiary">
                Run history only. Use Corpus truth above for current query, graph, and summary readiness.
              </span>
            </div>
            <div className="mt-2 grid grid-cols-1 sm:grid-cols-4 xl:grid-cols-11 gap-2 text-[9px] font-mono">
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Batch status</div>
                <div className="text-content-primary">{localBatch.status}</div>
              </div>
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Run contract</div>
                <div className="text-content-primary">
                  {PROFILE_LABELS[
                    ((localBatch.options?.profile as IngestProfileName | undefined) ??
                      localBatchProfile)
                  ] ?? "Default"}
                </div>
              </div>
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Run files</div>
                <div className="text-content-primary">{localBatch.total}</div>
              </div>
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Run queued</div>
                <div className="text-content-primary">{localBatch.counts?.queued ?? 0}</div>
              </div>
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Run active</div>
                <div className="text-accent-secondary">{localBatch.counts?.running ?? 0}</div>
              </div>
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Run done</div>
                <div className="text-green-500">
                  {localBatch.counts?.done ?? 0}
                  {localBatch.progress && (
                    <span className="text-content-tertiary">
                      {" "}· {localBatch.progress.mb_done} MB
                    </span>
                  )}
                </div>
              </div>
              {localBatch.progress && (
                <div
                  className="border border-accent-secondary/40 px-2 py-1"
                  title="Graph extraction finished inside this batch run. Corpus truth above is authoritative for current graph readiness."
                >
                  <div className="text-content-tertiary uppercase">Run graph extract</div>
                  <div className="text-accent-secondary">
                    {localBatch.progress.ladder?.graph_extracted ??
                      localBatch.progress.files_graph_extracted ??
                      localBatch.progress.files_extracted}
                    <span className="text-content-tertiary">
                      {" "}· {localBatch.progress.mb_graph_extracted ??
                        localBatch.progress.mb_extracted}/
                      {localBatch.progress.mb_total} MB
                    </span>
                  </div>
                </div>
              )}
              {localBatch.progress?.ladder && (
                <div
                  className="border border-border-minimal px-2 py-1"
                  title="Files made queryable inside this batch run. Corpus truth above is authoritative for current query readiness."
                >
                  <div className="text-content-tertiary uppercase">Run queryable</div>
                  <div className="text-content-primary">
                    {localBatch.progress.ladder.queryable ??
                      localBatch.progress.files_queryable ??
                      0}
                    <span className="text-content-tertiary">
                      {" "}· {localBatch.progress.mb_queryable ??
                        localBatch.progress.mb_done} MB
                    </span>
                  </div>
                </div>
              )}
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Run failed</div>
                <div className="text-error">{localBatch.counts?.failed ?? 0}</div>
              </div>
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Run recoverable</div>
                <div className="text-amber-400">
                  {localBatch.counts?.failed_recoverable ?? 0}
                </div>
              </div>
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Run stale</div>
                <div className="text-amber-400">{localBatchStaleCount}</div>
              </div>
              <div className="border border-border-minimal px-2 py-1">
                <div className="text-content-tertiary uppercase">Run stored</div>
                <div className="text-content-primary">
                  {formatBytes(localBatch.stored_bytes)}
                </div>
              </div>
            </div>
            </>
          )}
	          {localBatch?.progress?.ladder && (
	            <div className="mt-2 grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-8 gap-2 text-[9px] font-mono">
	              {INGEST_STAGE_LABELS.map(([stage, label]) => {
                const value = localBatch.progress?.ladder?.[stage] ?? 0;
                const total = localBatch.progress?.files_total ?? localBatch.total ?? 0;
                const pct = total > 0 ? Math.round((value / total) * 100) : 0;
                return (
                  <div
                    key={stage}
                    className="border border-border-minimal px-2 py-1"
                    title={`${label}: ${value}/${total} files complete`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-content-tertiary uppercase truncate">
                        {label}
                      </span>
                      <span className="text-content-tertiary">{pct}%</span>
                    </div>
                    <div className="mt-1 h-1 bg-bg-surface border border-border-minimal overflow-hidden">
                      <div
                        className="h-full bg-accent-secondary"
                        style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
                      />
                    </div>
                    <div className="mt-1 text-content-primary">
                      {value}
                      <span className="text-content-tertiary">/{total}</span>
                    </div>
                  </div>
                );
	              })}
	            </div>
	          )}
	          {batchReport && (
	            <div className="mt-2 grid grid-cols-1 xl:grid-cols-[1.5fr_1fr] gap-2 text-[9px] font-mono">
	              <div className="border border-border-minimal px-2 py-2">
	                <div className="flex items-center justify-between gap-2">
	                  <div className="text-content-tertiary uppercase">
	                    Run quality
	                  </div>
	                  <div className="text-content-tertiary">
	                    verified {batchReport.docs_verified ?? 0}/{batchReport.docs ?? 0}
	                  </div>
	                </div>
	                <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-2">
	                  <div>
	                    <div className="text-content-tertiary uppercase">Summary</div>
	                    <div className="text-content-primary">
	                      {batchReport.parents_summary_required_summarized ??
	                        batchReport.parents_summarized ??
	                        0}
	                      /{batchReport.parents_summary_required ?? 0}
	                      <span className="text-content-tertiary">
	                        {" "}· {formatRate(batchReport.summary_coverage_rate)}
	                      </span>
	                    </div>
	                    <div className="text-content-tertiary">
	                      {batchReport.parents_summary_skipped ?? 0} skipped by design
	                    </div>
	                  </div>
	                  <div>
	                    <div className="text-content-tertiary uppercase">Structure</div>
	                    <div className="text-content-primary">
	                      {batchReport.parents_summary_required_structured ??
	                        batchReport.parents_structured ??
	                        0}
	                      /{batchReport.parents_summary_required ?? 0}
	                      <span className="text-content-tertiary">
	                        {" "}· {formatRate(batchReport.structure_rate)}
	                      </span>
	                    </div>
	                    <div className="text-content-tertiary">
	                      raw missing {formatRate(batchReport.summary_raw_missing_rate)}
	                    </div>
	                  </div>
	                  <div>
	                    <div className="text-content-tertiary uppercase">Graph extract</div>
	                    <div className="text-accent-secondary">
	                      {batchReport.ghost_b_extracted_chunks ?? 0}/
	                      {batchReport.ghost_b_requested_chunks ?? 0}
	                      <span className="text-content-tertiary">
	                        {" "}· {formatRate(batchReport.ghost_b_success_rate)}
	                      </span>
	                    </div>
	                    <div className="text-content-tertiary">
	                      failed {batchReport.ghost_b_failed_chunks ?? 0}
	                    </div>
	                  </div>
	                  <div>
	                    <div className="text-content-tertiary uppercase">Graph QA</div>
	                    <div className="text-content-primary">
	                      partial {batchReport.ghost_b_docs_partial ?? 0}
	                      <span className="text-content-tertiary">
	                        {" "}· dead {batchReport.ghost_b_docs_dead ?? 0}
	                      </span>
	                    </div>
	                    <div className="text-content-tertiary">
	                      related_to {formatRate(batchReport.ghost_b_related_to_ratio)}
	                      {" "}· rejects {batchReport.ghost_b_validation_rejection_count ?? 0}
	                    </div>
	                  </div>
	                </div>
	                {batchReport.alerts && batchReport.alerts.length > 0 ? (
	                  <div className="mt-2 space-y-1 text-amber-300">
	                    {batchReport.alerts.slice(0, 3).map((alert, idx) => (
	                      <div
	                        key={`batch-quality-alert-${idx}`}
	                        className="flex items-start gap-1.5 leading-snug"
	                      >
	                        <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
	                        <span>{alert}</span>
	                      </div>
	                    ))}
	                  </div>
	                ) : (
	                  <div className="mt-2 text-content-tertiary">
	                    No batch quality alerts.
	                  </div>
	                )}
	              </div>
	              <div className="border border-border-minimal px-2 py-2">
	                <div className="text-content-tertiary uppercase">Extraction route</div>
	                {batchRouteEntries.length > 0 ? (
	                  <div className="mt-2 flex flex-wrap gap-1.5">
	                    {batchRouteEntries.map(([provider, count]) => (
	                      <span
	                        key={provider}
	                        className="px-2 py-1 border border-border-minimal text-content-primary"
	                        title={provider}
	                      >
	                        {providerLabel(provider)} {count}
	                      </span>
	                    ))}
	                  </div>
	                ) : (
	                  <div className="mt-2 text-content-tertiary">
	                    No extraction route calls recorded yet.
	                  </div>
	                )}
	                {batchReport.ghost_b_model_call_counts &&
	                  Object.keys(batchReport.ghost_b_model_call_counts).length > 0 && (
	                    <div className="mt-2 text-content-tertiary leading-snug">
	                      {topCountEntries(batchReport.ghost_b_model_call_counts, 3)
	                        .map(([model, count]) => `${providerLabel(model)} ${count}`)
	                        .join(" · ")}
	                    </div>
	                  )}
	              </div>
	            </div>
	          )}
	          {localBatch?.items && localBatch.items.length > 0 && (
	            <div className="mt-2 border border-border-minimal">
              <div className="hidden md:grid grid-cols-[42px_minmax(0,1.4fr)_86px_92px_minmax(0,1fr)] gap-2 px-2 py-1 text-[9px] font-bold tracking-widest text-content-tertiary uppercase border-b border-border-minimal">
                <span>Size</span>
                <span>File</span>
                <span>Status</span>
                <span>Phase</span>
                <span>Reason</span>
              </div>
              <div className="max-h-40 overflow-y-auto custom-scrollbar">
                {localBatch.items.slice(0, 12).map((item) => (
                  <BatchItemRow key={item.item_id} item={item} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Per-batch overrides panel */}
      {showOverrides && (
        <IngestOverridesPanel
          corpus={corpus}
          modalStatus={modalStatus}
          overrides={overrides}
          onChange={setOverrides}
          onClose={() => setShowOverrides(false)}
        />
      )}

      {/* Error Banner */}
      {error && (
        <div className="flex items-center gap-2 px-4 py-2 bg-error/10 border-b border-error/30 text-[10px] text-error shrink-0">
          <AlertTriangle className="w-3 h-3 shrink-0" />
          <span className="flex-1">{error}</span>
          <button
            onClick={() => setError(null)}
            className="hover:text-content-primary"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      )}

      {/* Retry hint — shown after the user clicks retry on a failed doc */}
      {retryHint && (
        <div className="flex items-start gap-2 px-4 py-2 bg-amber-400/10 border-b border-amber-400/30 text-[10px] text-amber-300 shrink-0">
          <RotateCcw className="w-3 h-3 shrink-0 mt-0.5" />
          <span className="flex-1 leading-snug">{retryHint}</span>
          <button
            onClick={() => setRetryHint(null)}
            className="hover:text-content-primary"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      )}

      {/* Main area — resizable split: left = document list, right = library */}
      <div ref={splitRowRef} className="flex-1 flex flex-col md:flex-row overflow-hidden">
      <div
        style={{ "--left-panel-width": `${leftPct}%` } as CSSProperties}
        className="h-1/2 w-full overflow-y-auto custom-scrollbar shrink-0 md:h-auto md:w-[var(--left-panel-width)]"
      >
        {isLoading ? (
          <div className="flex items-center justify-center py-12 text-[10px] text-content-tertiary tracking-widest">
            <Loader2 className="w-4 h-4 animate-spin mr-2" />
            LOADING_DOCUMENTS...
          </div>
        ) : documents.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-[10px] text-content-tertiary tracking-widest">
            <FileText className="w-8 h-8 mb-3 text-content-tertiary/50" />
            <span>[NO_DOCUMENTS]</span>
            <span className="mt-1 opacity-60">
              &gt; Start a backend folder batch to begin ingestion
            </span>
          </div>
        ) : (
          <div className="divide-y divide-border-minimal">
            {documents.map((doc) => {
              const isExpanded = expandedDocId === doc.doc_id;
              const isPendingDelete = deleteConfirmId === doc.doc_id;
              const stateMessages = getWriteStateMessages(doc.write_state);

              return (
                <div
                  key={doc.doc_id}
                  className="group hover:bg-bg-surface/50 transition-none"
                >
                  {/* Document Row */}
                  <div className="flex items-center gap-2 px-4 py-2.5">
                    {/* Expand Toggle */}
                    <button
                      onClick={() =>
                        setExpandedDocId(isExpanded ? null : doc.doc_id)
                      }
                      className="p-0.5 text-content-tertiary hover:text-content-primary transition-none"
                    >
                      {isExpanded ? (
                        <ChevronDown className="w-3 h-3" />
                      ) : (
                        <ChevronRight className="w-3 h-3" />
                      )}
                    </button>

                    {/* Icon */}
                    <FileText className="w-3.5 h-3.5 text-content-tertiary shrink-0" />

                    {/* Parsed book title; author · year under it. Raw
                        filename + source path stay on the tooltip. */}
                    {(() => {
                      const rawName =
                        doc.filename ||
                        (doc.source_path
                          ? doc.source_path.split("/").pop()
                          : doc.doc_id?.slice(0, 12)) ||
                        "unknown";
                      const meta = parseBookMeta(rawName);
                      const subtitle = [meta.author, meta.year]
                        .filter(Boolean)
                        .join(" · ");
                      return (
                        <div
                          className="flex-1 min-w-0"
                          title={`${meta.raw}${doc.source_path ? `\n${doc.source_path}` : ""}`}
                        >
                          <div className="text-[11px] font-bold text-content-primary truncate">
                            {meta.title || rawName}
                          </div>
                          <div className="text-[9px] text-content-tertiary truncate">
                            {subtitle ||
                              doc.source_path ||
                              doc.source_mime ||
                              doc.source_tier ||
                              ""}
                          </div>
                        </div>
                      );
                    })()}

                    {/* Stats — child chunk count matches corpus header;
                         parent count in parens for context. */}
                    <div className="flex items-center gap-3 text-[9px] text-content-tertiary tracking-wider shrink-0">
                      <span
                        className="flex items-center gap-1"
                        title={`${doc.chunk_count ?? 0} child chunks (retrieval unit) · ${getParentCount(doc)} parent chunks (context unit)`}
                      >
                        <Layers className="w-3 h-3" />
                        {doc.chunk_count ?? 0}
                        <span className="text-content-tertiary/60">
                          ({getParentCount(doc)}p)
                        </span>
                      </span>
                      <span
                        data-testid="pipeline-status"
                        className={`font-bold ${getWriteStateColor(doc.write_state)}`}
                      >
                        {getWriteStateLabel(doc.write_state)}
                      </span>
                      {stateMessages.length > 0 && (
                        <span title={stateMessages.join("\n")}>
                          <AlertTriangle className="w-3 h-3 text-amber-300" />
                        </span>
                      )}
                    </div>

                    {/* Delete */}
                    <div className="opacity-0 group-hover:opacity-100 transition-none shrink-0">
                      {isPendingDelete ? (
                        <div className="flex items-center gap-1">
                          <button
                            onClick={() => handleDeleteDoc(doc.doc_id)}
                            className="px-1.5 py-0.5 text-[9px] font-bold tracking-widest text-error border border-error hover:bg-error hover:text-bg-base transition-none uppercase"
                          >
                            Yes
                          </button>
                          <button
                            onClick={() => setDeleteConfirmId(null)}
                            className="px-1.5 py-0.5 text-[9px] font-bold tracking-widest text-content-tertiary border border-border-minimal hover:border-content-secondary transition-none uppercase"
                          >
                            No
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => setDeleteConfirmId(doc.doc_id)}
                          className="p-1 text-content-tertiary hover:text-error transition-none"
                          title="Delete document"
                        >
                          <Trash2 className="w-3 h-3" />
                        </button>
                      )}
                    </div>
                  </div>

                  {/* Expanded Document Details */}
                  {isExpanded && (
                    <div className="px-4 pb-3 pl-12 space-y-3">
                      {/* Metadata Grid */}
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-[10px]">
                        <div>
                          <span className="text-content-tertiary tracking-wider">
                            doc_id:
                          </span>
                          <span className="ml-1 text-content-secondary font-bold">
                            {doc.doc_id.slice(0, 12)}...
                          </span>
                        </div>
                        <div>
                          <span className="text-content-tertiary tracking-wider">
                            source_tier:
                          </span>
                          <span className="ml-1 text-accent-secondary font-bold">
                            {doc.source_tier}
                          </span>
                        </div>
                        <div>
                          <span className="text-content-tertiary tracking-wider">
                            ingested:
                          </span>
                          <span className="ml-1 text-content-secondary">
                            {doc.ingested_at ? formatDate(doc.ingested_at) : (doc.created_at ? formatDate(doc.created_at) : "—")}
                          </span>
                        </div>
                        <div>
                          <span className="text-content-tertiary tracking-wider">
                            entities:
                          </span>
                          <span className="ml-1 text-content-secondary">
                            {doc.entities_extracted ? (
                              <span className="text-accent-main">Yes</span>
                            ) : (
                              <span className="text-content-tertiary">No</span>
                            )}
                          </span>
                        </div>
                      </div>

                      {/* Ingestion Config */}
                      <div className="text-[10px] space-y-1">
                        <div className="text-content-tertiary tracking-wider uppercase font-bold">
                          ingestion_config:
                        </div>
                        <div className="pl-2 grid grid-cols-1 sm:grid-cols-2 gap-1">
                          <span className="text-content-secondary">
                            use_neo4j:{" "}
                            <span
                              className={
                                doc.ingestion_config.use_neo4j
                                  ? "text-accent-main"
                                  : "text-content-tertiary"
                              }
                            >
                              {String(doc.ingestion_config.use_neo4j)}
                            </span>
                          </span>
                          <span className="text-content-secondary">
                            summarize:{" "}
                            <span
                              className={
                                doc.ingestion_config.chunk_summarization
                                  ? "text-accent-main"
                                  : "text-content-tertiary"
                              }
                            >
                              {String(doc.ingestion_config.chunk_summarization)}
                            </span>
                          </span>
                        </div>
                      </div>

                      {/* Write State */}
                      <div className="text-[10px] space-y-1">
                        <div className="text-content-tertiary tracking-wider uppercase font-bold">
                          write_state:
                        </div>
                        <div className="pl-2 flex gap-4">
                          {(
                            [
                              "mongo_written",
                              "qdrant_written",
                              "neo4j_written",
                            ] as const
                          ).map((key) => (
                            <span
                              key={key}
                              className="flex items-center gap-1 text-content-secondary"
                            >
                              {doc.write_state[key] ? (
                                <Check className="w-3 h-3 text-accent-main" />
                              ) : (
                                <X className="w-3 h-3 text-error" />
                              )}
                              {key.replace("_written", "")}
                            </span>
                          ))}
                        </div>
                        {stateMessages.length > 0 && (
                          <div className="pl-2 space-y-1 text-amber-300">
                            {stateMessages.map((message, idx) => (
                              <div
                                key={`${doc.doc_id}-warning-${idx}`}
                                className="flex items-start gap-1.5 leading-snug"
                              >
                                <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
                                <span>{message}</span>
                              </div>
                            ))}
                          </div>
                        )}
                        {doc.ghost_b_metrics && (
                          <div className="pl-2 grid grid-cols-1 sm:grid-cols-2 gap-1 text-content-secondary">
                            <span>
                              graph_extract:{" "}
                              <span className="text-accent-secondary">
                                {doc.ghost_b_metrics.extracted_chunks ?? 0}/
                                {doc.ghost_b_metrics.requested_chunks ?? doc.chunk_count ?? 0}
                              </span>
                            </span>
                            <span>
                              related_to:{" "}
                              <span className="text-amber-300">
                                {Math.round((doc.ghost_b_metrics.related_to_ratio ?? 0) * 100)}%
                              </span>
                            </span>
                          </div>
                        )}
                        {(doc.ghost_b_failures?.length ?? 0) > 0 && (
                          <div className="pl-2 flex items-center justify-between gap-2 border border-amber-400/30 bg-amber-400/5 px-2 py-1.5">
                            <div className="text-amber-300 leading-snug">
                              {doc.ghost_b_failures?.length} graph chunk(s) need backfill
                            </div>
                            <button
                              onClick={() => handleGraphBackfill(doc)}
                              disabled={backfillingDocs.has(doc.doc_id)}
                              className="flex items-center gap-1 px-2 py-1 text-[9px] font-bold tracking-widest text-amber-300 border border-amber-400/50 hover:bg-amber-400 hover:text-bg-base transition-none uppercase disabled:opacity-50"
                            >
                              {backfillingDocs.has(doc.doc_id) ? (
                                <Loader2 className="w-3 h-3 animate-spin" />
                              ) : (
                                <RotateCcw className="w-3 h-3" />
                              )}
                              Backfill
                            </button>
                          </div>
                        )}
                      </div>

                      {/* Parent Chunks Preview */}
                      {doc.parent_chunks && doc.parent_chunks.length > 0 && (
                        <div className="text-[10px] space-y-1">
                          <div className="text-content-tertiary tracking-wider uppercase font-bold">
                            parent_chunks ({doc.parent_chunks.length}):
                          </div>
                          <div className="pl-2 space-y-1 max-h-40 overflow-y-auto custom-scrollbar">
                            {doc.parent_chunks
                              .slice(0, 5)
                              .map((parent, idx) => (
                                <div
                                  key={parent.parent_id}
                                  className="flex items-start gap-2 p-1.5 bg-bg-base border border-border-minimal"
                                >
                                  <Hash className="w-3 h-3 text-content-tertiary shrink-0 mt-0.5" />
                                  <div className="flex-1 min-w-0">
                                    <div className="flex items-center gap-2">
                                      <span className="text-content-secondary font-bold">
                                        Parent {idx}
                                      </span>
                                      <span className="text-content-tertiary">
                                        {parent.token_count} tokens
                                      </span>
                                      {parent.summary && (
                                        <span className="text-accent-main text-[9px]">
                                          [SUMMARIZED]
                                        </span>
                                      )}
                                    </div>
                                    {parent.heading_path.length > 0 && (
                                      <div className="text-[9px] text-content-tertiary mt-0.5">
                                        {parent.heading_path.join(" > ")}
                                      </div>
                                    )}
                                    <div className="text-[9px] text-content-tertiary mt-0.5 truncate">
                                      {parent.text.slice(0, 120)}...
                                    </div>
                                  </div>
                                </div>
                              ))}
                            {doc.parent_chunks.length > 5 && (
                              <div className="text-[9px] text-content-tertiary pl-2">
                                ... and {doc.parent_chunks.length - 5} more
                              </div>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
        <div
          onMouseDown={startSplitDrag}
          className="hidden w-1 shrink-0 bg-border-minimal hover:bg-accent-main cursor-col-resize transition-none md:block"
          title="Drag to resize"
        />
        <LibraryPanel
          widthPct={100 - leftPct}
          documents={documents}
          batchItems={localBatch?.items ?? []}
          onDeleteOne={handleDeleteDoc}
          onBulkDelete={handleBulkDelete}
          onRetry={handleRetryDoc}
        />
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between px-4 py-2 border-t border-border-minimal bg-bg-surface shrink-0">
        <div className="text-[9px] text-content-tertiary tracking-widest">
          {documents.length} DOCUMENTS
        </div>
        <button
          onClick={loadDocuments}
          disabled={isLoading}
          className="text-[9px] text-content-tertiary hover:text-accent-main tracking-widest uppercase transition-none disabled:opacity-50"
        >
          [REFRESH]
        </button>
      </div>
    </div>
  );
}

// ============================================================================
// IngestionProgressBar — pipeline completion across this corpus's documents
// ============================================================================
//
// Derived entirely from the documents array already loaded by CorpusDetail.
// Stages reflect the Phase 20 locked pipeline order (GOTCHAS §67):
//   parse → chunk → ghosts → mongo → embed → qdrant → neo4j → verify
// So counts are monotonic: mongo ≥ qdrant ≥ neo4j ≥ verified. That's why
// the stacked bar paints left→right in stage order — each later layer is
// narrower, revealing the slower stage as a trailing stripe on the right.

function BatchItemRow({ item }: { item: IngestBatchItemResponse }) {
  const label = getBatchItemStatusLabel(item);
  const statusClass =
    label === "done"
      ? "text-green-500"
      : label === "failed"
        ? "text-error"
        : label === "stale" || label === "recoverable"
          ? "text-amber-400"
          : label === "queued"
            ? "text-content-secondary"
            : "text-content-primary";

  return (
    <div className="grid grid-cols-1 md:grid-cols-[42px_minmax(0,1.4fr)_86px_92px_minmax(0,1fr)] gap-1 md:gap-2 px-2 py-2 md:py-1 text-[10px] font-mono border-b border-border-minimal/60 last:border-b-0">
      <span className="text-content-tertiary">
        {typeof item.size_bytes === "number" ? formatBytes(item.size_bytes) : ""}
      </span>
      <span className="text-content-secondary truncate" title={item.relative_path || item.filename}>
        {item.relative_path || item.filename}
      </span>
      <span className={statusClass} title={item.status}>
        {label}
      </span>
      <span className="text-accent-secondary truncate" title={item.phase || ""}>
        {label === "stale" ? "recoverable" : item.phase || "queued"}
      </span>
      <span className="text-content-tertiary truncate" title={item.error || item.failure_stage || ""}>
        {item.error || item.failure_stage || ""}
      </span>
    </div>
  );
}

function IngestionProgressBar({
  documents,
}: {
  documents: DocumentResponse[];
}) {
  const total = documents.length;

  if (total === 0) {
    return (
      <div className="flex items-center gap-3 px-4 py-1.5 border-t border-border-minimal text-[9px] font-bold tracking-widest uppercase text-content-tertiary">
        <span>[NO_INGEST_ACTIVITY]</span>
      </div>
    );
  }

  let mongoDone = 0;
  let qdrantDone = 0;
  let neo4jDone = 0;
  let verifiedDone = 0;
  let totalChunks = 0;
  let totalParents = 0;
  for (const d of documents) {
    if (d.write_state.mongo_written) mongoDone += 1;
    if (d.write_state.qdrant_written) qdrantDone += 1;
    if (d.write_state.neo4j_written) neo4jDone += 1;
    if (d.write_state.verified === true) verifiedDone += 1;
    totalChunks += d.chunk_count ?? 0;
    totalParents += getParentCount(d);
  }
  const overall = Math.round(
    ((mongoDone + qdrantDone + neo4jDone) / (3 * total)) * 100,
  );

  const width = (done: number) => ({
    width: `${(done / total) * 100}%`,
  });

  const stageColor = (done: number) =>
    done === total
      ? "text-accent-main"
      : done > 0
        ? "text-content-secondary"
        : "text-content-tertiary";

  return (
    <div className="flex flex-col gap-1 px-4 py-1.5 border-t border-border-minimal">
      <div className="flex items-center justify-between text-[9px] font-bold tracking-widest uppercase">
        <div className="flex items-center gap-2">
          <span className={stageColor(mongoDone)}>
            MONGO {mongoDone}/{total}
          </span>
          <span className="text-content-tertiary/40">·</span>
          <span className={stageColor(qdrantDone)}>
            QDRANT {qdrantDone}/{total}
          </span>
          <span className="text-content-tertiary/40">·</span>
          <span className={stageColor(neo4jDone)}>
            NEO4J {neo4jDone}/{total}
          </span>
          <span className="text-content-tertiary/40">·</span>
          <span className={stageColor(verifiedDone)}>
            VERIFIED {verifiedDone}/{total}
          </span>
        </div>
        <div className="flex items-center gap-3 text-content-tertiary">
          <span>
            CHUNKS{" "}
            <span className="text-content-secondary">{totalChunks}</span>
          </span>
          <span>
            PARENTS{" "}
            <span className="text-content-secondary">{totalParents}</span>
          </span>
          <span
            className={
              overall === 100 ? "text-accent-main" : "text-accent-secondary"
            }
          >
            {overall}%
          </span>
        </div>
      </div>
      <div className="relative h-1 w-full bg-bg-base overflow-hidden">
        <div
          className="absolute top-0 left-0 h-full bg-content-tertiary/40 transition-none"
          style={width(mongoDone)}
        />
        <div
          className="absolute top-0 left-0 h-full bg-accent-secondary/70 transition-none"
          style={width(qdrantDone)}
        />
        <div
          className="absolute top-0 left-0 h-full bg-accent-main transition-none"
          style={width(neo4jDone)}
        />
      </div>
    </div>
  );
}

// ============================================================================
// LibraryPanel — right half of the corpus detail. Groups docs by outcome
// (COMPLETED vs FAILED), supports multi-select delete + retry for failures.
// ============================================================================
//
// Classification is pure frontend — no new backend flag. Uses the same
// write_state signals the left list reads:
//   COMPLETED = mongo_written && qdrant_written && neo4j_written
//   FAILED    = verified === false  OR  (partial state AND stale > STALE_MS)
// Anything else (fresh, actively ingesting) is hidden here; the left list
// shows it as PARTIAL / MONGO_ONLY / PENDING while it's still moving.

const STALE_MS = 5 * 60 * 1000;

type DocStatus = "completed" | "failed" | "in_progress";

function classifyDoc(doc: DocumentResponse): DocStatus {
  const ws = doc.write_state;
  if (ws.verified === false) return "failed";
  if (ws.mongo_written && ws.qdrant_written && ws.neo4j_written) {
    return "completed";
  }
  const ts = doc.updated_at || doc.created_at || doc.ingested_at;
  if (ts) {
    const age = Date.now() - new Date(ts).getTime();
    if (age > STALE_MS) return "failed";
  }
  return "in_progress";
}

function LibraryPanel({
  widthPct,
  documents,
  batchItems,
  onDeleteOne,
  onBulkDelete,
  onRetry,
}: {
  widthPct: number;
  documents: DocumentResponse[];
  batchItems: IngestBatchItemResponse[];
  onDeleteOne: (docId: string) => void | Promise<void>;
  onBulkDelete: (docIds: string[]) => void | Promise<void>;
  onRetry: (doc: DocumentResponse) => void;
}) {
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);
  const [query, setQuery] = useState("");

  const { ready, processingDocs, runningItems, failed, failedBatchOnly, queuedCount } =
    useMemo(() => {
      const readyDocs: DocumentResponse[] = [];
      const inProgress: DocumentResponse[] = [];
      const failedDocs: DocumentResponse[] = [];
      const docNames = new Set<string>();
      for (const d of documents) {
        if (d.filename) docNames.add(d.filename.toLowerCase());
        const s = classifyDoc(d);
        if (s === "completed") readyDocs.push(d);
        else if (s === "failed") failedDocs.push(d);
        else inProgress.push(d);
      }
      // Books currently moving through the pipeline (live phase per item).
      const running = batchItems.filter((i) => i.status === "running");
      const runningNames = new Set(
        running.map((i) => (i.filename || "").toLowerCase()),
      );
      // Docs mid-write not covered by a running batch item.
      const inProgressOnly = inProgress.filter(
        (d) => !runningNames.has((d.filename || "").toLowerCase()),
      );
      // Batch failures that never produced a document (e.g. chunker timeouts)
      // are invisible in `documents` — without these rows the library
      // undercounts failures vs the batch header.
      const batchOnly = batchItems.filter(
        (i) =>
          i.status === "failed" &&
          !docNames.has((i.filename || "").toLowerCase()),
      );
      const queued = batchItems.filter((i) => i.status === "queued").length;
      // Ready books alphabetized by parsed title — findable, not insertion order.
      readyDocs.sort((a, b) =>
        parseBookMeta(a.filename || "").title.localeCompare(
          parseBookMeta(b.filename || "").title,
        ),
      );
      return {
        ready: readyDocs,
        processingDocs: inProgressOnly,
        runningItems: running,
        failed: failedDocs,
        failedBatchOnly: batchOnly,
        queuedCount: queued,
      };
    }, [documents, batchItems]);

  // One filter across every section: parsed title + author + raw filename.
  const q = query.trim().toLowerCase();
  const nameMatches = useCallback(
    (name: string | null | undefined) => {
      if (!q) return true;
      const m = parseBookMeta(name || "");
      return `${m.title} ${m.author} ${name || ""}`.toLowerCase().includes(q);
    },
    [q],
  );
  const readyView = ready.filter((d) => nameMatches(d.filename));
  const processingDocsView = processingDocs.filter((d) => nameMatches(d.filename));
  const runningView = runningItems.filter((i) => nameMatches(i.filename));
  const failedView = failed.filter((d) => nameMatches(d.filename));
  const failedBatchView = failedBatchOnly.filter((i) => nameMatches(i.filename));

  const toggle = (docId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(docId)) next.delete(docId);
      else next.add(docId);
      return next;
    });
  };

  const enterSelectMode = () => {
    setSelectMode(true);
    setSelected(new Set());
  };

  const cancelSelect = () => {
    setSelectMode(false);
    setSelected(new Set());
  };

  const confirmDelete = async () => {
    if (selected.size === 0) {
      setSelectMode(false);
      return;
    }
    setDeleting(true);
    try {
      await onBulkDelete(Array.from(selected));
    } finally {
      setDeleting(false);
      setSelectMode(false);
      setSelected(new Set());
    }
  };

  const failedTotal = failedView.length + failedBatchView.length;
  const total =
    ready.length +
    processingDocs.length +
    runningItems.length +
    failed.length +
    failedBatchOnly.length;

  return (
    <div
      style={{ "--library-panel-width": `${widthPct}%` } as CSSProperties}
      className="h-1/2 w-full flex flex-col overflow-hidden shrink-0 md:h-auto md:w-[var(--library-panel-width)]"
    >
      <div className="flex items-center gap-2 px-4 py-2 border-b border-border-minimal bg-bg-surface/50 shrink-0">
        <div className="text-[9px] font-bold tracking-widest text-content-tertiary uppercase shrink-0">
          LIBRARY · {total}
        </div>
        <div className="flex-1 flex items-center gap-1.5 min-w-0 px-2 py-1 border border-border-minimal bg-bg-base focus-within:border-content-secondary">
          <Search className="w-3 h-3 text-content-tertiary shrink-0" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="filter by title or author…"
            data-testid="library-filter"
            className="w-full bg-transparent text-[10px] text-content-primary placeholder:text-content-tertiary/60 outline-none"
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              className="text-content-tertiary hover:text-content-primary"
            >
              <X className="w-3 h-3" />
            </button>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {selectMode ? (
            <>
              <button
                onClick={cancelSelect}
                disabled={deleting}
                className="flex items-center gap-1 px-2 py-1 text-[9px] font-bold tracking-widest border border-border-minimal text-content-tertiary hover:border-content-secondary hover:text-content-secondary transition-none uppercase disabled:opacity-50"
              >
                CANCEL
              </button>
              <button
                onClick={confirmDelete}
                disabled={deleting || selected.size === 0}
                className="flex items-center gap-1 px-2 py-1 text-[9px] font-bold tracking-widest border border-error text-error hover:bg-error hover:text-bg-base transition-none uppercase disabled:opacity-50"
              >
                {deleting ? (
                  <Loader2 className="w-3 h-3 animate-spin" />
                ) : (
                  <Trash2 className="w-3 h-3" />
                )}
                DELETE {selected.size > 0 ? selected.size : ""}
              </button>
            </>
          ) : (
            <button
              onClick={enterSelectMode}
              disabled={total === 0}
              className="flex items-center gap-1 px-2 py-1 text-[9px] font-bold tracking-widest border border-border-minimal text-content-tertiary hover:border-error hover:text-error transition-none uppercase disabled:opacity-50"
            >
              <Trash2 className="w-3 h-3" />
              DELETE
            </button>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar" data-testid="library-panel">
        {total === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 px-6 text-center text-[10px] text-content-tertiary">
            <FileText className="w-8 h-8 mb-3 text-content-tertiary/50" />
            <span className="tracking-widest">[EMPTY_LIBRARY]</span>
            <span className="mt-1 opacity-70">
              Ingested books and files appear here — start a folder batch or
              upload above.
            </span>
          </div>
        ) : (
          <>
            {/* Processing — live view of the current batch; previously these
                docs were hidden here entirely ("where did my book go?"). */}
            {(runningView.length > 0 ||
              processingDocsView.length > 0 ||
              queuedCount > 0) && (
              <>
                <LibrarySection
                  label="PROCESSING"
                  icon={
                    <Loader2 className="w-3 h-3 text-accent-secondary animate-spin" />
                  }
                  accentColor="text-accent-secondary"
                  count={runningView.length + processingDocsView.length}
                  hint="being ingested now"
                />
                <div className="divide-y divide-border-minimal">
                  {runningView.map((item) => (
                    <LibraryBatchRow
                      key={item.item_id}
                      item={item}
                      mode="processing"
                    />
                  ))}
                  {processingDocsView.map((doc) => (
                    <LibraryRow
                      key={doc.doc_id}
                      doc={doc}
                      status="processing"
                      selectMode={selectMode}
                      checked={selected.has(doc.doc_id)}
                      onToggle={() => toggle(doc.doc_id)}
                      onDelete={() => onDeleteOne(doc.doc_id)}
                      onRetry={() => onRetry(doc)}
                    />
                  ))}
                  {queuedCount > 0 && (
                    <div className="px-4 py-2 text-[9px] text-content-tertiary tracking-wider">
                      + {queuedCount} more queued in the current batch
                    </div>
                  )}
                </div>
              </>
            )}

            {/* Ready — alphabetized by parsed title */}
            <LibrarySection
              label="READY"
              icon={<CheckCircle2 className="w-3 h-3 text-accent-main" />}
              accentColor="text-accent-main"
              count={readyView.length}
              hint="indexed · searchable in chat"
            />
            {readyView.length === 0 ? (
              <div className="px-4 py-3 text-[9px] text-content-tertiary tracking-widest uppercase">
                {q ? "[NO_MATCHES]" : "[NONE_YET]"}
              </div>
            ) : (
              <div className="divide-y divide-border-minimal">
                {readyView.map((doc) => (
                  <LibraryRow
                    key={doc.doc_id}
                    doc={doc}
                    status="completed"
                    selectMode={selectMode}
                    checked={selected.has(doc.doc_id)}
                    onToggle={() => toggle(doc.doc_id)}
                    onDelete={() => onDeleteOne(doc.doc_id)}
                    onRetry={() => onRetry(doc)}
                  />
                ))}
              </div>
            )}

            {/* Failed — documents that failed AND batch files that never
                became documents (chunker timeouts et al). Reason on each row. */}
            <LibrarySection
              label="FAILED"
              icon={<XCircle className="w-3 h-3 text-error" />}
              accentColor="text-error"
              count={failedTotal}
              hint="reason on each row"
            />
            {failedTotal === 0 ? (
              <div className="px-4 py-3 text-[9px] text-content-tertiary tracking-widest uppercase">
                {q ? "[NO_MATCHES]" : "[NO_FAILURES] — every document ingested clean"}
              </div>
            ) : (
              <div className="divide-y divide-border-minimal">
                {failedView.map((doc) => (
                  <LibraryRow
                    key={doc.doc_id}
                    doc={doc}
                    status="failed"
                    selectMode={selectMode}
                    checked={selected.has(doc.doc_id)}
                    onToggle={() => toggle(doc.doc_id)}
                    onDelete={() => onDeleteOne(doc.doc_id)}
                    onRetry={() => onRetry(doc)}
                  />
                ))}
                {failedBatchView.map((item) => (
                  <LibraryBatchRow
                    key={item.item_id}
                    item={item}
                    mode="failed"
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function LibrarySection({
  label,
  icon,
  accentColor,
  count,
  hint,
}: {
  label: string;
  icon: React.ReactNode;
  accentColor: string;
  count: number;
  hint?: string;
}) {
  return (
    <div className="flex items-center gap-2 px-4 py-1.5 bg-bg-base/50 border-y border-border-minimal text-[9px] font-bold tracking-widest uppercase sticky top-0 z-10">
      {icon}
      <span className={accentColor}>{label}</span>
      <span className="text-content-tertiary">({count})</span>
      {hint && (
        <span className="ml-auto font-normal normal-case tracking-normal text-content-tertiary/70">
          {hint}
        </span>
      )}
    </div>
  );
}

function LibraryRow({
  doc,
  status,
  selectMode,
  checked,
  onToggle,
  onDelete,
  onRetry,
}: {
  doc: DocumentResponse;
  status: "completed" | "failed" | "processing";
  selectMode: boolean;
  checked: boolean;
  onToggle: () => void;
  onDelete: () => void;
  onRetry: () => void;
}) {
  const rawName =
    doc.filename || doc.source_path?.split("/").pop() || doc.doc_id.slice(0, 12);
  const meta = parseBookMeta(rawName);
  const title = meta.title || rawName;
  const chunks = doc.chunk_count ?? 0;
  const parents = getParentCount(doc);
  const stateMessages = getWriteStateMessages(doc.write_state);
  const failureReason = humanizeIngestFailure(
    stateMessages[0],
    deriveFailedStage(doc),
  );
  const Icon = meta.author ? BookOpen : FileText;

  return (
    <div
      className={`group flex items-center gap-2 px-4 py-2.5 transition-none ${
        selectMode ? "cursor-pointer hover:bg-bg-surface/50" : "hover:bg-bg-surface/50"
      } ${checked ? "bg-accent-main/5" : ""}`}
      onClick={selectMode ? onToggle : undefined}
    >
      {selectMode && (
        <div
          className={`w-3.5 h-3.5 border flex-shrink-0 flex items-center justify-center transition-none ${
            checked
              ? "bg-accent-main border-accent-main"
              : "border-border-minimal bg-bg-base"
          }`}
        >
          {checked && <Check className="w-3 h-3 text-bg-base" />}
        </div>
      )}

      <Icon className="w-3.5 h-3.5 text-content-tertiary shrink-0" />

      <div className="flex-1 min-w-0" title={meta.raw}>
        <div className="text-[11px] font-bold text-content-primary truncate">
          {title}
        </div>
        {status === "failed" ? (
          <div
            className="text-[9px] text-error truncate"
            title={stateMessages.join("\n") || failureReason}
          >
            {failureReason}
          </div>
        ) : (
          <div className="flex items-center gap-2 text-[9px] text-content-tertiary">
            {meta.author && <span className="truncate">{meta.author}</span>}
            {meta.year && <span className="shrink-0">· {meta.year}</span>}
            <span
              className="shrink-0"
              title={`${chunks} retrieval chunks · ${parents} parent sections`}
            >
              {meta.author || meta.year ? "· " : ""}
              {chunks} chunks
            </span>
            {status === "processing" && (
              <span className="shrink-0 text-accent-secondary">· writing…</span>
            )}
            {status === "completed" &&
              (() => {
                const g = graphCoverage(doc);
                if (g === null || g.pct >= 90) return null;
                const dead = g.pct < 10;
                return (
                  <span
                    className={`inline-flex items-center gap-1 shrink-0 ${
                      dead ? "text-error" : "text-amber-300"
                    }`}
                    title={`Knowledge-graph coverage: ${g.extracted}/${g.requested} chunks extracted (${g.pct}%). Text and vectors are complete; the graph lane ${
                      dead ? "is effectively empty" : "is partial"
                    }. Use Backfill (expand the doc in the left list) to re-run extraction on the missing chunks.`}
                  >
                    <AlertTriangle className="w-2.5 h-2.5" />
                    graph {dead ? "dead" : `${g.pct}%`}
                  </span>
                );
              })()}
            {doc.is_near_duplicate && (
              <span
                className="inline-flex items-center gap-1 text-amber-300"
                title={`Near duplicate${
                  doc.near_duplicate_candidates?.[0]?.filename
                    ? ` of ${doc.near_duplicate_candidates[0].filename}`
                    : ""
                }`}
              >
                <AlertTriangle className="w-2.5 h-2.5" />
                duplicate
              </span>
            )}
            {stateMessages.length > 0 && (
              <span
                className="inline-flex items-center gap-1 text-amber-300"
                title={stateMessages.join("\n")}
              >
                <AlertTriangle className="w-2.5 h-2.5" />
                warning
              </span>
            )}
          </div>
        )}
      </div>

      {!selectMode && (
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-none shrink-0">
          {status === "failed" && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onRetry();
              }}
              title="Re-upload the file to retry ingestion"
              className="p-1 text-content-tertiary hover:text-accent-main transition-none"
            >
              <RotateCcw className="w-3 h-3" />
            </button>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            title="Delete document"
            className="p-1 text-content-tertiary hover:text-error transition-none"
          >
            <Trash2 className="w-3 h-3" />
          </button>
        </div>
      )}
    </div>
  );
}

// A batch item with no document row yet: either mid-pipeline (processing) or
// failed before its first write (e.g. chunker timeout) — previously these
// were invisible here, so the library's FAILED count disagreed with the batch.
function LibraryBatchRow({
  item,
  mode,
}: {
  item: IngestBatchItemResponse;
  mode: "processing" | "failed";
}) {
  const rawName = item.filename || item.relative_path || item.item_id;
  const meta = parseBookMeta(rawName);
  const title = meta.title || rawName;
  const Icon = meta.author ? BookOpen : FileText;
  const size =
    typeof item.size_bytes === "number" ? formatBytes(item.size_bytes) : "";

  return (
    <div className="flex items-center gap-2 px-4 py-2.5 hover:bg-bg-surface/50 transition-none">
      <Icon className="w-3.5 h-3.5 text-content-tertiary shrink-0" />
      <div className="flex-1 min-w-0" title={meta.raw}>
        <div className="text-[11px] font-bold text-content-primary truncate">
          {title}
        </div>
        {mode === "processing" ? (
          <div className="flex items-center gap-2 text-[9px] text-content-tertiary">
            {meta.author && <span className="truncate">{meta.author}</span>}
            {meta.year && <span className="shrink-0">· {meta.year}</span>}
            <span className="shrink-0 text-accent-secondary" title={item.phase || ""}>
              {meta.author || meta.year ? "· " : ""}
              {phaseLabel(item.phase)}…
            </span>
            {size && <span className="shrink-0">· {size}</span>}
          </div>
        ) : (
          <div
            className="text-[9px] text-error truncate"
            title={item.error || item.failure_stage || ""}
          >
            {humanizeIngestFailure(item.error)}
            {size ? ` · ${size}` : ""}
          </div>
        )}
      </div>
    </div>
  );
}

function deriveFailedStage(doc: DocumentResponse): string {
  const ws = doc.write_state;
  if (ws.verified === false) return "verify";
  if (!ws.mongo_written) return "parse/ghosts";
  if (!ws.qdrant_written) return "embed/qdrant";
  if (!ws.neo4j_written) return "neo4j";
  return "verify";
}

// ============================================================================
// IngestOverridesPanel — per-batch overrides without persisting to corpus
// ============================================================================
//
// Sprint 2B. Backend supports flat-scalar overrides for embed_mode/base_url/
// api_key/max_concurrent + summary_model/base_url/api_key + extraction_*.
// See backend/routers/ingestion.py:217-301.
//
// Layout: three info rows (Embed / Summary / Extraction) showing the corpus
// default value. Each row has a [Change for this batch] toggle that reveals
// an inline editor for that family.

function IngestOverridesPanel({
  corpus,
  modalStatus,
  overrides,
  onChange,
  onClose,
}: {
  corpus: CorpusResponse;
  modalStatus: ModalStatus | null;
  overrides: IngestOverrides;
  onChange: (next: IngestOverrides) => void;
  onClose: () => void;
}) {
  const cfg = corpus.default_ingestion_config;
  const summaryDefault = cfg.summary_models?.[0]?.model ?? "(none)";
  const extractionEngine = cfg.extraction_engine ?? "cloud";
  const extractionUsesProvider = ["local", "cloud", "dual", "local_then_cloud", "local_then_enrich"].includes(
    extractionEngine,
  );
  const extractionPool = cfg.models_linked ? cfg.summary_models ?? [] : cfg.extraction_models ?? [];
  const extractionDefault = extractionUsesProvider
    ? extractionPool.length
      ? extractionPool
          .map((m) => `${m.provider_preset || "custom"}:${m.model} @${m.max_concurrent}`)
          .join(" | ")
      : "provider pool empty"
    : extractionEngine === "off"
      ? "off — vectors only"
      : "legacy GLiNER/GLiREL sidecar";

  const [editEmbed, setEditEmbed] = useState(false);
  const [editSummary, setEditSummary] = useState(false);

  const patch = (p: Partial<IngestOverrides>) => onChange({ ...overrides, ...p });
  const clearKeys = (keys: (keyof IngestOverrides)[]) => {
    const next = { ...overrides };
    keys.forEach((k) => delete next[k]);
    onChange(next);
  };

  const corpusModeUnavailable =
    cfg.embed_mode === "modal" && !modalStatus?.deployed;

  return (
    <div className="border-b border-amber-400/30 bg-amber-400/5 px-4 py-3 space-y-3 shrink-0">
      <div className="flex items-center justify-between">
        <div className="text-[10px] font-bold tracking-widest uppercase text-amber-300 flex items-center gap-2">
          <SlidersHorizontal className="w-3 h-3" />
          Per-batch overrides — not persisted
        </div>
        <div className="flex items-center gap-2">
          {Object.keys(overrides).length > 0 && (
            <button
              onClick={() => onChange({})}
              className="text-[9px] text-content-tertiary hover:text-content-primary tracking-widest uppercase"
            >
              Clear all
            </button>
          )}
          <button
            onClick={onClose}
            className="text-content-tertiary hover:text-content-primary"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      </div>

      {corpusModeUnavailable && (
        <div className="flex items-start gap-2 px-2 py-1.5 bg-error/10 border border-error/30 text-[10px] text-error leading-snug">
          <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
          <span>
            Corpus default is <code className="font-mono">embed_mode=modal</code>{" "}
            but Modal is not deployed. Override to{" "}
            <code className="font-mono">local</code> or deploy under Settings →
            Infrastructure → Modal first.
          </span>
        </div>
      )}

      {/* Embed row */}
      <OverrideRow
        label="Embed"
        defaultValue={cfg.embed_mode}
        overrideValue={overrides.embed_mode}
        editing={editEmbed}
        onToggle={() => setEditEmbed(!editEmbed)}
        onClear={() =>
          clearKeys([
            "embed_mode",
            "embed_base_url",
            "embed_api_key",
            "embed_max_concurrent",
          ])
        }
      >
        <div className="space-y-2">
          <div className="flex gap-2">
            {(["local", "api", "modal"] as const).map((m) => (
              <label
                key={m}
                className={`flex items-center gap-1 text-[10px] tracking-wider cursor-pointer px-2 py-1 border ${
                  (overrides.embed_mode ?? cfg.embed_mode) === m
                    ? "border-accent-main text-accent-main"
                    : "border-border-minimal text-content-secondary hover:border-content-secondary"
                }`}
              >
                <input
                  type="radio"
                  name="override-embed-mode"
                  checked={(overrides.embed_mode ?? cfg.embed_mode) === m}
                  onChange={() => patch({ embed_mode: m })}
                  className="accent-accent-main"
                />
                {m}
              </label>
            ))}
          </div>
          {(overrides.embed_mode ?? cfg.embed_mode) === "api" && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <input
                type="text"
                placeholder="base_url"
                value={overrides.embed_base_url ?? ""}
                onChange={(e) => patch({ embed_base_url: e.target.value })}
                className="px-2 py-1 bg-bg-base border border-border-minimal text-[11px] text-content-primary font-mono"
              />
              <input
                type="password"
                placeholder="api_key (plaintext, ephemeral)"
                value={overrides.embed_api_key ?? ""}
                onChange={(e) => patch({ embed_api_key: e.target.value })}
                className="px-2 py-1 bg-bg-base border border-border-minimal text-[11px] text-content-primary font-mono"
              />
            </div>
          )}
        </div>
      </OverrideRow>

      {/* Summary row */}
      <OverrideRow
        label="Summary (GHOST A)"
        defaultValue={summaryDefault}
        overrideValue={overrides.summary_model}
        editing={editSummary}
        onToggle={() => setEditSummary(!editSummary)}
        onClear={() =>
          clearKeys(["summary_model", "summary_base_url", "summary_api_key"])
        }
      >
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          <input
            type="text"
            placeholder="model (e.g. ollama/llama3.2:3b)"
            value={overrides.summary_model ?? ""}
            onChange={(e) => patch({ summary_model: e.target.value })}
            className="px-2 py-1 bg-bg-base border border-border-minimal text-[11px] text-content-primary font-mono"
          />
          <input
            type="text"
            placeholder="base_url (optional)"
            value={overrides.summary_base_url ?? ""}
            onChange={(e) => patch({ summary_base_url: e.target.value })}
            className="px-2 py-1 bg-bg-base border border-border-minimal text-[11px] text-content-primary font-mono"
          />
          <input
            type="password"
            placeholder="api_key (optional, ephemeral)"
            value={overrides.summary_api_key ?? ""}
            onChange={(e) => patch({ summary_api_key: e.target.value })}
            className="px-2 py-1 bg-bg-base border border-border-minimal text-[11px] text-content-primary font-mono"
          />
        </div>
      </OverrideRow>

      {/* Extraction row — per-batch overrides intentionally do not replace
          provider-card extraction routing. The corpus contract owns which
          provider/model pool Ghost B uses. */}
      <div className="flex items-start justify-between gap-3 py-2">
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-wide text-content-tertiary">
            Extraction (GHOST B)
          </div>
          <div className="text-[12px] text-content-primary font-mono truncate">
            {extractionDefault}
          </div>
          <div className="text-[11px] text-content-tertiary">
            {extractionUsesProvider
              ? "Provider-card LLM extraction is configured on the corpus; strict schema gates run before graph promotion."
              : extractionEngine === "off"
                ? "Graph extraction is disabled for this corpus."
                : "Legacy local sidecar mode. Configure provider-card RTX/cloud extraction in Corpus Manager."}
          </div>
        </div>
      </div>
    </div>
  );
}

function OverrideRow({
  label,
  defaultValue,
  overrideValue,
  editing,
  onToggle,
  onClear,
  children,
}: {
  label: string;
  defaultValue: React.ReactNode;
  overrideValue: React.ReactNode | undefined;
  editing: boolean;
  onToggle: () => void;
  onClear: () => void;
  children: React.ReactNode;
}) {
  const hasOverride = overrideValue != null && overrideValue !== "";
  return (
    <div className="border border-border-minimal bg-bg-base/40 px-2 py-1.5 space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary shrink-0">
            {label}:
          </span>
          {hasOverride ? (
            <span className="text-[10px] font-mono text-amber-300 truncate">
              <span className="text-content-tertiary line-through mr-1">
                {String(defaultValue)}
              </span>
              {String(overrideValue)}
            </span>
          ) : (
            <span className="text-[10px] font-mono text-content-secondary truncate">
              {String(defaultValue)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {hasOverride && (
            <button
              onClick={onClear}
              className="text-[9px] text-content-tertiary hover:text-content-primary tracking-widest uppercase px-1"
              title="Clear override"
            >
              reset
            </button>
          )}
          <button
            onClick={onToggle}
            className="text-[9px] text-accent-main hover:text-accent-hover tracking-widest uppercase px-1"
          >
            {editing ? "[hide]" : "[change for this batch]"}
          </button>
        </div>
      </div>
      {editing && children}
    </div>
  );
}
