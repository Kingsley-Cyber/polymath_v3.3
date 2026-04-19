// CorpusDetail.tsx - Document browser for a single corpus
import { useState, useEffect, useCallback } from "react";
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
} from "lucide-react";
import * as api from "../../lib/api";
import type {
  CorpusResponse,
  DocumentResponse,
  IngestJobResponse,
} from "../../types";

interface CorpusDetailProps {
  corpus: CorpusResponse;
  onBack: () => void;
  onCorpusUpdated: (corpus: CorpusResponse) => void;
  /** When provided, renders an "Edit Models & Schema" button in the header
   *  that hands control back to CorpusManager's edit panel. */
  onEditConfig?: (corpus: CorpusResponse) => void;
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

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    setIsUploading(true);
    setError(null);

    try {
      for (const file of Array.from(files)) {
        setUploadProgress(`Ingesting: ${file.name}...`);
        const result: IngestJobResponse = await api.uploadDocumentToCorpus(
          corpus.corpus_id,
          file,
        );
        if (result.status === "failed") {
          setError(
            `Failed to ingest ${file.name}: ${result.error || "Unknown error"}`,
          );
        }
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

  const handleDeleteDoc = async (_docId: string) => {
    setError(null);
    try {
      // TODO: Backend endpoint for single doc delete not yet in router.
      // For now, reload to reflect state.
      await loadDocuments();
      setDeleteConfirmId(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to delete document",
      );
    }
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };

  const getWriteStateColor = (state: {
    mongo_written: boolean;
    qdrant_written: boolean;
    neo4j_written: boolean;
  }) => {
    if (state.mongo_written && state.qdrant_written && state.neo4j_written)
      return "text-accent-main";
    if (state.mongo_written && state.qdrant_written)
      return "text-accent-secondary";
    if (state.mongo_written) return "text-content-secondary";
    return "text-error";
  };

  const getWriteStateLabel = (state: {
    mongo_written: boolean;
    qdrant_written: boolean;
    neo4j_written: boolean;
  }) => {
    if (state.mongo_written && state.qdrant_written && state.neo4j_written)
      return "COMPLETE";
    if (state.mongo_written && state.qdrant_written) return "PARTIAL";
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
      <div className="flex items-center justify-between px-4 py-2 border-b border-border-minimal bg-bg-surface/50 shrink-0">
        <div data-testid="upload-status" className="text-[9px] font-bold tracking-widest text-content-tertiary uppercase">
          {isUploading ? uploadProgress || "UPLOADING..." : "DOCUMENTS"}
        </div>
        <label className="flex items-center gap-1.5 px-2 py-1 text-[9px] font-bold tracking-widest text-accent-main border border-accent-main hover:bg-accent-main hover:text-bg-base transition-none uppercase cursor-pointer">
          {isUploading ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : (
            <Upload className="w-3 h-3" />
          )}
          <span>{isUploading ? "Processing..." : "+ Ingest"}</span>
          <input
            data-testid="corpus-file-input"
            type="file"
            multiple
            onChange={handleUpload}
            disabled={isUploading}
            className="hidden"
          />
        </label>
      </div>

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

      {/* Document List */}
      <div className="flex-1 overflow-y-auto custom-scrollbar">
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

                    {/* Stats */}
                    <div className="flex items-center gap-3 text-[9px] text-content-tertiary tracking-wider shrink-0">
                      <span className="flex items-center gap-1">
                        <Layers className="w-3 h-3" />
                        {doc.parent_chunks?.length || 0}
                      </span>
                      <span
                        data-testid="pipeline-status"
                        className={`font-bold ${getWriteStateColor(doc.write_state)}`}
                      >
                        {getWriteStateLabel(doc.write_state)}
                      </span>
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
