// CorpusDetail.tsx - Document browser for a single corpus
import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  ChevronLeft,
  Trash2,
  FileText,
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
} from "lucide-react";
import * as api from "../../lib/api";
import type {
  CorpusResponse,
  DocumentResponse,
  IngestionBatchResponse,
  IngestJobResponse,
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

const INGEST_BATCH_SIZE = 25;

function describeBatchQueued(batch: IngestionBatchResponse) {
  const status =
    batch.status === "completed_with_errors"
      ? "completed with errors"
      : batch.status.replace(/_/g, " ");
  const vectorReady = batch.vector_ready_count || 0;
  const graphReady = batch.graph_ready_count || 0;
  const graphFailed = batch.graph_failed_count || 0;
  const backfill = batch.needs_backfill_count || 0;
  const failed = batch.failed_count || 0;
  const parts = [
    `Batch ${batch.batch_id.slice(0, 8)} ${status}`,
    `${batch.total_files} files`,
    `${vectorReady} vector-ready`,
    `${graphReady} graph-ready`,
  ];
  if (backfill) parts.push(`${backfill} graph backfill`);
  if (graphFailed) parts.push(`${graphFailed} graph token-budget failed`);
  if (failed) parts.push(`${failed} failed`);
  return parts.join(" · ");
}

function batchStatusSignature(batch: IngestionBatchResponse) {
  return [
    batch.status,
    batch.queued_count,
    batch.processing_count,
    batch.vector_ready_count,
    batch.graph_ready_count,
    batch.graph_partial_count,
    batch.needs_backfill_count || 0,
    batch.graph_failed_count || 0,
    batch.failed_count,
    batch.cancelled_count,
  ].join("|");
}

function getWriteStateMessages(
  state: Pick<WriteState, "warnings" | "verify_errors">,
) {
  return [...(state.warnings ?? []), ...(state.verify_errors ?? [])].filter(Boolean);
}

function humanizeStrategy(value?: string | null) {
  return (value || "auto").replace(/_/g, " ");
}

function getDecisionSummary(doc: DocumentResponse) {
  if (doc.decision_trace_summary) return doc.decision_trace_summary;
  const trace = doc.decision_trace;
  if (!trace) return "auto ingestion policy";
  const parts = [
    humanizeStrategy(trace.chunking_strategy),
    humanizeStrategy(trace.graph_strategy),
  ];
  const skipped = trace.low_value_chunk_count ?? 0;
  if (skipped > 0) parts.push(`${skipped} low-value chunks skipped`);
  return parts.join(" - ");
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

  // Upload state
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<string | null>(null);

  // Per-batch overrides (Sprint 2B). Empty object = use corpus defaults.
  const [overrides, setOverrides] = useState<IngestOverrides>({});
  const [showOverrides, setShowOverrides] = useState(false);

  // Modal global status — used to warn when corpus default is embed_mode='modal'
  // but Modal isn't deployed.
  const [modalStatus, setModalStatus] = useState<ModalStatus | null>(null);
  useEffect(() => {
    api
      .getModalStatus()
      .then(setModalStatus)
      .catch(() => setModalStatus(null));
  }, []);

  // Retry UX — the bytes of uploaded files aren't cached server-side, so
  // "retry" on a failed ingest is really "re-pick the file from disk". The
  // library panel's retry button clicks this hidden input to trigger the
  // browser file picker.
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [retryHint, setRetryHint] = useState<string | null>(null);
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null);
  const batchStatusRef = useRef<string>("");
  const [backfillingDocs, setBackfillingDocs] = useState<Set<string>>(new Set());
  const [recoveringVectors, setRecoveringVectors] = useState<Set<string>>(new Set());

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

  useEffect(() => {
    if (!activeBatchId) return;
    let cancelled = false;
    const terminal = new Set([
      "completed",
      "completed_with_errors",
      "failed",
      "cancelled",
    ]);

    const pollBatch = async () => {
      try {
        const batch = await api.getIngestionBatch(activeBatchId);
        if (cancelled) return;
        const signature = batchStatusSignature(batch);
        setRetryHint(
          `${describeBatchQueued(batch)} · vector RAG becomes available per document before graph completion.`,
        );
        if (signature !== batchStatusRef.current) {
          batchStatusRef.current = signature;
          await loadDocuments();
          const updated = await api.getCorpus(corpus.corpus_id);
          if (!cancelled) onCorpusUpdated(updated);
        }
        if (terminal.has(batch.status)) {
          setActiveBatchId(null);
        }
      } catch (err) {
        if (!cancelled) {
          setRetryHint(
            `Batch ${activeBatchId.slice(0, 8)} status unavailable: ${
              err instanceof Error ? err.message : "unknown error"
            }`,
          );
        }
      }
    };

    pollBatch();
    const timer = window.setInterval(pollBatch, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeBatchId, corpus.corpus_id, loadDocuments, onCorpusUpdated]);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    setIsUploading(true);
    setError(null);

    try {
      const fileList = Array.from(files);
      if (fileList.length > 1) {
        setUploadProgress(`Spooling ${fileList.length} files to durable batch queue...`);
        const batch = await api.batchUploadDocumentsToCorpus(
          corpus.corpus_id,
          fileList,
          Object.keys(overrides).length > 0 ? overrides : undefined,
        );
        batchStatusRef.current = batchStatusSignature(batch);
        setActiveBatchId(batch.batch_id);
        setRetryHint(`${describeBatchQueued(batch)} · vector RAG becomes available per document before graph completion.`);
        await loadDocuments();
        const updated = await api.getCorpus(corpus.corpus_id);
        onCorpusUpdated(updated);
        return;
      }
      for (let idx = 0; idx < fileList.length; idx += 1) {
        const file = fileList[idx];
        const batchNo = Math.floor(idx / INGEST_BATCH_SIZE) + 1;
        const batchTotal = Math.ceil(fileList.length / INGEST_BATCH_SIZE);
        setUploadProgress(
          `Batch ${batchNo}/${batchTotal} · ${idx + 1}/${fileList.length}: ${file.name}`,
        );
        const result: IngestJobResponse = await api.uploadDocumentToCorpus(
          corpus.corpus_id,
          file,
          Object.keys(overrides).length > 0 ? overrides : undefined,
        );
        if (result.status === "failed") {
          setError(
            `Failed to ingest ${file.name}: ${result.error || "Unknown error"}`,
          );
        }
        if ((idx + 1) % INGEST_BATCH_SIZE === 0 && corpus.default_ingestion_config.use_neo4j) {
          await api.warmGraphCache(corpus.corpus_id).catch(() => undefined);
          await loadDocuments();
        }
      }
      if (corpus.default_ingestion_config.use_neo4j) {
        await api.warmGraphCache(corpus.corpus_id).catch(() => undefined);
      }
      // Refresh documents after upload
      await loadDocuments();
      // Refresh corpus doc_count
      const updated = await api.getCorpus(corpus.corpus_id);
      onCorpusUpdated(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setIsUploading(false);
      setUploadProgress(null);
      // Reset file input
      e.target.value = "";
    }
  };

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

  const handleVectorRecovery = async (doc: DocumentResponse) => {
    setError(null);
    setRecoveringVectors((prev) => new Set(prev).add(doc.doc_id));
    try {
      await api.recoverDocumentVectors(corpus.corpus_id, doc.doc_id);
      await loadDocuments();
      const updated = await api.getCorpus(corpus.corpus_id);
      onCorpusUpdated(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Vector recovery failed");
    } finally {
      setRecoveringVectors((prev) => {
        const next = new Set(prev);
        next.delete(doc.doc_id);
        return next;
      });
    }
  };

  const handleRetryDoc = (doc: DocumentResponse) => {
    const name = doc.filename || doc.source_path?.split("/").pop() || "this file";
    setRetryHint(
      `Re-select "${name}" in the file picker — original bytes weren't cached. The ingest is idempotent on content hash, so re-uploading resumes the same doc.`,
    );
    fileInputRef.current?.click();
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };

  const getWriteStateColor = (state: WriteState) => {
    if (state.verified === false) return "text-error";
    if (
      state.graph_status === "needs_backfill" ||
      state.graph_status === "graph_partial" ||
      state.graph_status === "graph_failed_token_budget"
    )
      return "text-amber-300";
    if (getWriteStateMessages(state).length > 0) return "text-amber-300";
    if (state.mongo_written && state.qdrant_written && state.neo4j_written)
      return "text-accent-main";
    if (state.vector_ready || (state.mongo_written && state.qdrant_written))
      return "text-accent-secondary";
    if (state.mongo_written) return "text-content-secondary";
    return "text-error";
  };

  const getWriteStateLabel = (state: WriteState) => {
    const hasWarnings = getWriteStateMessages(state).length > 0;
    const vectorReady = state.vector_ready || (state.mongo_written && state.qdrant_written);
    if (state.verified === false) return "VERIFY_FAIL";
    if (state.graph_status === "graph_failed_token_budget") return "GRAPH_TOKEN_BUDGET";
    if (state.graph_status === "needs_backfill") return "NEEDS_BACKFILL";
    if (state.graph_status === "graph_partial") return "GRAPH_PARTIAL";
    if (state.graph_status === "graph_extracting") return "GRAPH_EXTRACTING";
    if (state.graph_status === "graph_pending" && vectorReady) return "GRAPH_PENDING";
    if (state.mongo_written && state.qdrant_written && state.neo4j_written)
      return hasWarnings ? "COMPLETE_WARN" : "COMPLETE_GRAPH";
    if (vectorReady) return "COMPLETE_VECTOR";
    if (state.mongo_written) return "MONGO_ONLY";
    return "PENDING";
  };

  return (
    <div className="flex flex-col h-full relative">
      {/* Full-panel upload overlay — shown while ingesting so the user isn't
          staring at a black screen during the 1-3 min DOCX pipeline */}
      {isUploading && (
        <div
          data-testid="upload-overlay"
          className="absolute inset-0 z-50 flex flex-col items-center justify-center bg-bg-base/95 backdrop-blur-sm"
        >
          <Loader2 className="w-10 h-10 animate-spin text-accent-main mb-4" />
          <div className="text-[13px] font-bold tracking-widest text-accent-main uppercase mb-2">
            INGESTING DOCUMENT
          </div>
          <div className="text-[11px] text-content-secondary font-mono text-center max-w-[500px] px-4 mb-4">
            {uploadProgress || "Uploading..."}
          </div>
          <div className="flex flex-col gap-1 text-[10px] font-bold tracking-widest text-content-tertiary uppercase">
            <span>&gt; extract → chunk → embed → commit</span>
            <span className="text-content-tertiary/60">this may take 1–3 min for large documents</span>
          </div>
        </div>
      )}
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

      {/* Upload Bar */}
      <div className="flex flex-col border-b border-border-minimal bg-bg-surface/50 shrink-0">
      <div className="flex items-center justify-between px-4 py-2">
        <div data-testid="upload-status" className="text-[9px] font-bold tracking-widest text-content-tertiary uppercase">
          {isUploading
            ? uploadProgress || "UPLOADING..."
            : `DOCUMENTS · ${documents.length}`}
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
          <label className="flex items-center gap-1.5 px-2 py-1 text-[9px] font-bold tracking-widest text-accent-main border border-accent-main hover:bg-accent-main hover:text-bg-base transition-none uppercase cursor-pointer">
            {isUploading ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <Upload className="w-3 h-3" />
            )}
            <span>{isUploading ? "Processing..." : "+ Ingest"}</span>
            <input
              ref={fileInputRef}
              data-testid="corpus-file-input"
              type="file"
              multiple
              onChange={handleUpload}
              disabled={isUploading}
              className="hidden"
            />
          </label>
        </div>
      </div>
        <IngestionProgressBar documents={documents} isUploading={isUploading} />
      </div>

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
      <div ref={splitRowRef} className="flex-1 flex overflow-hidden">
      <div
        style={{ width: `${leftPct}%` }}
        className="overflow-y-auto custom-scrollbar shrink-0"
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
              &gt; Upload a file to begin ingestion
            </span>
          </div>
        ) : (
          <div className="divide-y divide-border-minimal">
            {documents.map((doc) => {
              const isExpanded = expandedDocId === doc.doc_id;
              const isPendingDelete = deleteConfirmId === doc.doc_id;
              const stateMessages = getWriteStateMessages(doc.write_state);
              const decisionTrace = doc.decision_trace;
              const decisionSummary = getDecisionSummary(doc);

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

                    {/* Filename — prefer doc.filename, fall back to source_path */}
                    <div className="flex-1 min-w-0">
                      <div className="text-[11px] font-bold text-content-primary truncate">
                        {doc.filename || (doc.source_path ? doc.source_path.split("/").pop() : doc.doc_id?.slice(0, 12) || "unknown")}
                      </div>
                      <div className="text-[9px] text-content-tertiary truncate">
                        {doc.source_path || doc.source_mime || doc.source_tier || ""}
                      </div>
                    </div>

                    {/* Stats — child chunk count matches corpus header;
                         parent count in parens for context. */}
                    <div className="flex items-center gap-3 text-[9px] text-content-tertiary tracking-wider shrink-0">
                      <span
                        className="flex items-center gap-1"
                        title={`${doc.chunk_count ?? 0} child chunks (retrieval unit) · ${doc.parent_chunks?.length ?? 0} parent chunks (context unit)`}
                      >
                        <Layers className="w-3 h-3" />
                        {doc.chunk_count ?? 0}
                        <span className="text-content-tertiary/60">
                          ({doc.parent_chunks?.length ?? 0}p)
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
                      <div className="grid grid-cols-2 gap-2 text-[10px]">
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
                        <div className="pl-2 grid grid-cols-2 gap-1">
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
                      <div className="text-[10px] space-y-1 border border-border-minimal bg-bg-base/50 px-2 py-2">
                        <div className="flex items-center justify-between gap-2">
                          <div className="text-content-tertiary tracking-wider uppercase font-bold">
                            ingestion decision:
                          </div>
                          <span className="text-[9px] text-content-tertiary">
                            {decisionTrace?.structure_quality || "auto"}
                          </span>
                        </div>
                        <div className="text-content-secondary leading-snug">
                          {decisionSummary}
                        </div>
                        {decisionTrace && (
                          <div className="grid grid-cols-2 gap-1 pt-1 text-content-tertiary">
                            <span>
                              parser:{" "}
                              <span className="text-content-secondary">
                                {humanizeStrategy(decisionTrace.parser_strategy)}
                              </span>
                            </span>
                            <span>
                              chunks:{" "}
                              <span className="text-content-secondary">
                                {decisionTrace.child_count ?? doc.chunk_count ?? 0}c /{" "}
                                {decisionTrace.parent_count ?? doc.parent_chunks?.length ?? 0}p
                              </span>
                            </span>
                            <span>
                              child:{" "}
                              <span className="text-content-secondary">
                                {humanizeStrategy(decisionTrace.child_strategy)}
                              </span>
                            </span>
                            <span>
                              graph:{" "}
                              <span className="text-content-secondary">
                                {humanizeStrategy(decisionTrace.graph_mode)}
                              </span>
                            </span>
                          </div>
                        )}
                        {(decisionTrace?.reasons?.length ?? 0) > 0 && (
                          <div className="pt-1 space-y-0.5 text-[9px] text-content-tertiary">
                            {decisionTrace?.reasons?.slice(0, 3).map((reason, idx) => (
                              <div key={`${doc.doc_id}-decision-${idx}`}>
                                - {reason}
                              </div>
                            ))}
                          </div>
                        )}
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
                          <div className="pl-2 grid grid-cols-2 gap-1 text-content-secondary">
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
                        {doc.write_state?.mongo_written && !doc.write_state?.qdrant_written && (
                          <div className="pl-2 flex items-center justify-between gap-2 border border-cyan-400/30 bg-cyan-400/5 px-2 py-1.5">
                            <div className="text-cyan-300 leading-snug">
                              Mongo-ready only. Recover vectors without reparsing or rerunning summaries.
                            </div>
                            <button
                              onClick={() => handleVectorRecovery(doc)}
                              disabled={recoveringVectors.has(doc.doc_id)}
                              className="flex items-center gap-1 px-2 py-1 text-[9px] font-bold tracking-widest text-cyan-300 border border-cyan-400/50 hover:bg-cyan-400 hover:text-bg-base transition-none uppercase disabled:opacity-50"
                            >
                              {recoveringVectors.has(doc.doc_id) ? (
                                <Loader2 className="w-3 h-3 animate-spin" />
                              ) : (
                                <RotateCcw className="w-3 h-3" />
                              )}
                              Recover Vectors
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
          className="w-1 shrink-0 bg-border-minimal hover:bg-accent-main cursor-col-resize transition-none"
          title="Drag to resize"
        />
        <LibraryPanel
          widthPct={100 - leftPct}
          documents={documents}
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

function IngestionProgressBar({
  documents,
  isUploading,
}: {
  documents: DocumentResponse[];
  isUploading: boolean;
}) {
  const total = documents.length;

  if (total === 0) {
    return (
      <div className="flex items-center gap-3 px-4 py-1.5 border-t border-border-minimal text-[9px] font-bold tracking-widest uppercase text-content-tertiary">
        <span>[NO_INGEST_ACTIVITY]</span>
        {isUploading && (
          <span className="text-accent-main animate-pulse">
            · UPLOAD_IN_FLIGHT
          </span>
        )}
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
    totalParents += d.parent_chunks?.length ?? 0;
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
        {isUploading && (
          <div className="absolute top-0 right-0 h-full w-8 bg-gradient-to-l from-accent-main/40 to-transparent animate-pulse" />
        )}
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
//   COMPLETED = vector-ready for chat/RAG; graph readiness is shown separately
//   FAILED    = verified === false  OR  (partial state AND stale > STALE_MS)
// Anything else (fresh, actively ingesting) is hidden here; the left list
// shows it as PARTIAL / MONGO_ONLY / PENDING while it's still moving.

const STALE_MS = 5 * 60 * 1000;

type DocStatus = "completed" | "failed" | "in_progress";

function classifyDoc(doc: DocumentResponse): DocStatus {
  const ws = doc.write_state;
  if (ws.verified === false) return "failed";
  if (ws.vector_ready || (ws.mongo_written && ws.qdrant_written)) {
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
  onDeleteOne,
  onBulkDelete,
  onRetry,
}: {
  widthPct: number;
  documents: DocumentResponse[];
  onDeleteOne: (docId: string) => void | Promise<void>;
  onBulkDelete: (docIds: string[]) => void | Promise<void>;
  onRetry: (doc: DocumentResponse) => void;
}) {
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);

  const { completed, failed } = useMemo(() => {
    const c: DocumentResponse[] = [];
    const f: DocumentResponse[] = [];
    for (const d of documents) {
      const s = classifyDoc(d);
      if (s === "completed") c.push(d);
      else if (s === "failed") f.push(d);
    }
    return { completed: c, failed: f };
  }, [documents]);

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

  const total = completed.length + failed.length;

  return (
    <div
      style={{ width: `${widthPct}%` }}
      className="flex flex-col overflow-hidden shrink-0"
    >
      <div className="flex items-center justify-between px-4 py-2 border-b border-border-minimal bg-bg-surface/50 shrink-0">
        <div className="text-[9px] font-bold tracking-widest text-content-tertiary uppercase">
          LIBRARY · {total}
        </div>
        <div className="flex items-center gap-1.5">
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

      <div className="flex-1 overflow-y-auto custom-scrollbar">
        {total === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-[10px] text-content-tertiary tracking-widest">
            <FileText className="w-8 h-8 mb-3 text-content-tertiary/50" />
            <span>[EMPTY_LIBRARY]</span>
          </div>
        ) : (
          <>
            {/* Completed section */}
            <LibrarySection
              label="COMPLETED"
              icon={<CheckCircle2 className="w-3 h-3 text-accent-main" />}
              accentColor="text-accent-main"
              count={completed.length}
            />
            <div className="divide-y divide-border-minimal">
              {completed.map((doc) => (
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

            {/* Failed section */}
            <LibrarySection
              label="FAILED"
              icon={<XCircle className="w-3 h-3 text-error" />}
              accentColor="text-error"
              count={failed.length}
            />
            {failed.length === 0 ? (
              <div className="px-4 py-3 text-[9px] text-content-tertiary tracking-widest uppercase">
                [NO_FAILURES]
              </div>
            ) : (
              <div className="divide-y divide-border-minimal">
                {failed.map((doc) => (
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
}: {
  label: string;
  icon: React.ReactNode;
  accentColor: string;
  count: number;
}) {
  return (
    <div className="flex items-center gap-2 px-4 py-1.5 bg-bg-base/50 border-y border-border-minimal text-[9px] font-bold tracking-widest uppercase sticky top-0 z-10">
      {icon}
      <span className={accentColor}>{label}</span>
      <span className="text-content-tertiary">({count})</span>
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
  status: "completed" | "failed";
  selectMode: boolean;
  checked: boolean;
  onToggle: () => void;
  onDelete: () => void;
  onRetry: () => void;
}) {
  const name =
    doc.filename || doc.source_path?.split("/").pop() || doc.doc_id.slice(0, 12);
  const chunks = doc.chunk_count ?? 0;
  const parents = doc.parent_chunks?.length ?? 0;
  const failedStage = deriveFailedStage(doc);
  const stateMessages = getWriteStateMessages(doc.write_state);

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

      <FileText className="w-3.5 h-3.5 text-content-tertiary shrink-0" />

      <div className="flex-1 min-w-0">
        <div className="text-[11px] font-bold text-content-primary truncate">
          {name}
        </div>
        {status === "completed" ? (
          <div className="flex items-center gap-2 text-[9px] text-content-tertiary">
            <span>{chunks}c / {parents}p</span>
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
        ) : (
          <div className="text-[9px] text-error truncate">
            killed at {failedStage}
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

function deriveFailedStage(doc: DocumentResponse): string {
  const ws = doc.write_state;
  if (ws.verified === false) return "verify";
  if (!ws.mongo_written) return "parse/ghosts";
  if (!ws.qdrant_written) return "embed/qdrant";
  if (ws.graph_status === "graph_pending") return "graph pending";
  if (ws.graph_status === "graph_extracting") return "graph extracting";
  if (ws.graph_status === "graph_partial") return "graph partial";
  if (ws.graph_status === "needs_backfill") return "graph backfill";
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
  const extractionDefault = cfg.models_linked
    ? summaryDefault
    : cfg.extraction_models?.[0]?.model ?? "(none)";

  const [editEmbed, setEditEmbed] = useState(false);
  const [editSummary, setEditSummary] = useState(false);
  const [editExtraction, setEditExtraction] = useState(false);

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
            <div className="grid grid-cols-2 gap-2">
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
        <div className="grid grid-cols-3 gap-2">
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

      {/* Extraction row */}
      <OverrideRow
        label="Extraction (GHOST B)"
        defaultValue={extractionDefault}
        overrideValue={overrides.extraction_model}
        editing={editExtraction}
        onToggle={() => setEditExtraction(!editExtraction)}
        onClear={() =>
          clearKeys([
            "extraction_model",
            "extraction_base_url",
            "extraction_api_key",
          ])
        }
      >
        <div className="grid grid-cols-3 gap-2">
          <input
            type="text"
            placeholder="model"
            value={overrides.extraction_model ?? ""}
            onChange={(e) => patch({ extraction_model: e.target.value })}
            className="px-2 py-1 bg-bg-base border border-border-minimal text-[11px] text-content-primary font-mono"
          />
          <input
            type="text"
            placeholder="base_url (optional)"
            value={overrides.extraction_base_url ?? ""}
            onChange={(e) => patch({ extraction_base_url: e.target.value })}
            className="px-2 py-1 bg-bg-base border border-border-minimal text-[11px] text-content-primary font-mono"
          />
          <input
            type="password"
            placeholder="api_key (optional, ephemeral)"
            value={overrides.extraction_api_key ?? ""}
            onChange={(e) => patch({ extraction_api_key: e.target.value })}
            className="px-2 py-1 bg-bg-base border border-border-minimal text-[11px] text-content-primary font-mono"
          />
        </div>
      </OverrideRow>
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
