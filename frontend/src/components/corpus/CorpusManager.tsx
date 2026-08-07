// CorpusManager.tsx - Corpus CRUD management interface
import { useState, useEffect, useCallback, useRef } from "react";
import {
  Plus,
  Trash2,
  Edit3,
  FolderOpen,
  FolderClosed,
  ChevronRight,
  ChevronDown,
  X,
  Check,
  Database,
  Server,
  Ban,
  Loader2,
  AlertTriangle,
  ExternalLink,
  RefreshCw,
} from "lucide-react";
import { useChatStore } from "../../stores/chatStore";
import { useSettingsStore } from "../../stores/settingsStore";
import * as api from "../../lib/api";
import type {
  CorpusResponse,
  CorpusCreate,
  IngestionConfig,
  EmbedMode,
  IngestionPreset,
  ModalStatus,
  GlobalIngestionSettings,
  ExtractionEngine,
  ExtractionContractResponse,
  ModelProfileRef,
} from "../../types";
import { DEFAULT_INGESTION_CONFIG, inferPreset } from "../../types";
import { CorpusDetail } from "./CorpusDetail";
import { IngestionModelPool } from "../settings/IngestionModelPool";
import { IngestionProviderSelector } from "../settings/IngestionProviderSelector";
import { Button } from "../ui/Button";

interface CorpusManagerProps {
  isOpen: boolean;
  onClose: () => void;
}

function readinessLabel(status?: string | null): string {
  return (status || "unknown").replace(/_/g, " ");
}

function readinessTone(status?: string | null): string {
  if (status === "fully_enriched") return "text-accent-main border-accent-main/50";
  if (
    status === "summaries_pending" ||
    status === "lexicon_pending" ||
    status === "graph_pending" ||
    status === "ingestion_pending" ||
    status === "extraction_pending" ||
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

function compactCount(value?: number | null): string {
  const count = value ?? 0;
  return new Intl.NumberFormat("en", {
    notation: count >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(count);
}

// DEFAULT_INGESTION_CONFIG imported from ../../types (complete version with all IngestionConfig fields)

type IngestionWorkflowId = "graphify_cpu" | "vectors_only";

const WORKFLOW_META: {
  key: IngestionWorkflowId;
  label: string;
  detail: string;
  execution: string;
  outcome: string;
  badge: string;
  kind: "private" | "off";
  engine: ExtractionEngine;
  needsCloudPool: false;
  needsRtx: false;
  needsCloudApi: false;
}[] = [
  {
    key: "graphify_cpu",
    label: "Graphify CPU",
    detail: "Pinned GLiNER2 entity census with deterministic document completion and relation extraction.",
    execution: "Runs in-process on CPU with no provider endpoint or fallback",
    outcome: "Qualified entities and relations with durable stage receipts",
    badge: "Canonical",
    kind: "private",
    engine: "graphify_cpu",
    needsCloudPool: false,
    needsRtx: false,
    needsCloudApi: false,
  },
  {
    key: "vectors_only",
    label: "Vectors only",
    detail: "Skip graph extraction; vector and hybrid retrieval only.",
    execution: "No Graphify extraction",
    outcome: "Documents become searchable while graph extraction stays off",
    badge: "No graph",
    kind: "off",
    engine: "off",
    needsCloudPool: false,
    needsRtx: false,
    needsCloudApi: false,
  },
];

function applyPresetToConfig(
  cfg: IngestionConfig,
  preset: IngestionPreset,
): IngestionConfig {
  if (preset === "custom") {
    return { ...cfg, preset };
  }
  const map = {
    fast: {
      use_neo4j: false,
      chunk_summarization: false,
      target_qdrant_collections: ["naive", "hrag"],
    },
    balanced: {
      use_neo4j: true,
      chunk_summarization: false,
      target_qdrant_collections: ["naive", "hrag", "graph"],
    },
    deep: {
      use_neo4j: true,
      chunk_summarization: true,
      target_qdrant_collections: ["naive", "hrag", "graph"],
    },
  }[preset];
  return { ...cfg, preset, ...map };
}

function createDefaultIngestionConfig(
  globalIngestion?: GlobalIngestionSettings | null,
): IngestionConfig {
  const summary = globalIngestion?.summary;
  const patch: Partial<IngestionConfig> = {};
  if (summary) {
    patch.max_summary_tokens =
      summary.max_summary_tokens || DEFAULT_INGESTION_CONFIG.max_summary_tokens;
    if (summary.summary_models?.length) {
      patch.summary_models = [...summary.summary_models];
    }
    if (summary.enabled) {
      patch.chunk_summarization = true;
      patch.preset = "deep";
      patch.target_qdrant_collections = ["naive", "hrag", "graph"];
      patch.use_neo4j = true;
    }
  }
  let next: IngestionConfig = {
    ...DEFAULT_INGESTION_CONFIG,
    ...patch,
    parent_chunk_tokens: { ...DEFAULT_INGESTION_CONFIG.parent_chunk_tokens },
    child_chunk_tokens: { ...DEFAULT_INGESTION_CONFIG.child_chunk_tokens },
    target_qdrant_collections: [
      ...(patch.target_qdrant_collections ??
        DEFAULT_INGESTION_CONFIG.target_qdrant_collections),
    ],
    summary_models: [...(patch.summary_models ?? DEFAULT_INGESTION_CONFIG.summary_models)],
    extraction_models: [...DEFAULT_INGESTION_CONFIG.extraction_models],
    embedding_models: [...DEFAULT_INGESTION_CONFIG.embedding_models],
  };
  return next;
}

function inferWorkflow(config: IngestionConfig): IngestionWorkflowId {
  return config.extraction_engine === "off" ? "vectors_only" : "graphify_cpu";
}

function applyWorkflowToConfig(
  cfg: IngestionConfig,
  workflowId: IngestionWorkflowId,
  _providerProfiles: ModelProfileRef[] = [],
): IngestionConfig {
  const workflow = WORKFLOW_META.find((item) => item.key === workflowId);
  if (!workflow) return cfg;
  let next: IngestionConfig = {
    ...cfg,
    extraction_engine: workflow.engine,
    models_linked: false,
    extraction_models: [],
  };
  if (workflowId === "vectors_only") {
    next = applyPresetToConfig(next, "fast");
  } else if (next.preset === "fast" || inferPreset(next) === "fast") {
    next = applyPresetToConfig(next, "balanced");
  }
  return next;
}

function poolLabel(entries: ModelProfileRef[]): string {
  if (!entries.length) return "empty";
  return entries.map((entry) => entry.model || entry.provider_preset || "model").join(" | ");
}

type WorkflowMeta = (typeof WORKFLOW_META)[number];

function workflowKindClass(kind: WorkflowMeta["kind"], selected = false): string {
  const base = {
    private: selected
      ? "border-cyan-300 bg-cyan-300/10 text-cyan-100"
      : "border-cyan-300/30 bg-bg-surface text-cyan-100 hover:border-cyan-300",
    cloud: selected
      ? "border-sky-300 bg-sky-300/10 text-sky-100"
      : "border-sky-300/30 bg-bg-surface text-sky-100 hover:border-sky-300",
    hybrid: selected
      ? "border-violet-300 bg-violet-300/10 text-violet-100"
      : "border-violet-300/30 bg-bg-surface text-violet-100 hover:border-violet-300",
    legacy: selected
      ? "border-amber-300 bg-amber-300/10 text-amber-100"
      : "border-amber-300/30 bg-bg-surface text-amber-100 hover:border-amber-300",
    off: selected
      ? "border-zinc-300 bg-zinc-300/10 text-zinc-100"
      : "border-zinc-400/25 bg-bg-surface text-zinc-200 hover:border-zinc-300",
    custom: selected
      ? "border-content-secondary bg-content-secondary/10 text-content-primary"
      : "border-border-minimal bg-bg-surface text-content-secondary hover:border-content-secondary",
  }[kind];
  return base;
}

function workflowBadgeClass(kind: WorkflowMeta["kind"]): string {
  return {
    private: "border-cyan-300/40 bg-cyan-300/10 text-cyan-100",
    cloud: "border-sky-300/40 bg-sky-300/10 text-sky-100",
    hybrid: "border-violet-300/40 bg-violet-300/10 text-violet-100",
    legacy: "border-amber-300/40 bg-amber-300/10 text-amber-100",
    off: "border-zinc-300/30 bg-zinc-300/10 text-zinc-200",
    custom: "border-border-minimal bg-bg-base text-content-secondary",
  }[kind];
}

function WorkflowIcon({ kind }: { kind: WorkflowMeta["kind"] }) {
  const className = "w-4 h-4 shrink-0";
  if (kind === "private") return <Server className={className} />;
  return <Ban className={className} />;
}

function humanEngineLabel(engine: ExtractionEngine | string | undefined): string {
  if (!engine || engine === "graphify_cpu") return "Graphify CPU (canonical)";
  if (engine === "off") return "Off";
  return String(engine).replace(/_/g, " ");
}

function IngestionWorkflowSelector({
  config,
  onChange,
  idPrefix,
  providerProfiles,
}: {
  config: IngestionConfig;
  onChange: (next: IngestionConfig) => void;
  idPrefix: string;
  providerProfiles: ModelProfileRef[];
}) {
  const current = inferWorkflow(config);
  const currentMeta = WORKFLOW_META.find((item) => item.key === current) ?? WORKFLOW_META[0];
  const summaryPool = config.summary_models ?? [];
  const primaryWorkflowIds: IngestionWorkflowId[] = [
    "graphify_cpu",
    "vectors_only",
  ];
  const visibleWorkflowIds = primaryWorkflowIds.includes(current)
    ? primaryWorkflowIds
    : [current, ...primaryWorkflowIds];
  const visibleWorkflows = visibleWorkflowIds
    .map((key) => WORKFLOW_META.find((item) => item.key === key))
    .filter(Boolean) as typeof WORKFLOW_META;

  return (
    <div className="border border-accent-main/25 bg-bg-base px-3 py-3 space-y-3">
      <div>
        <div className="flex items-center justify-between gap-2">
          <div>
            <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase">
              Extraction Profile
            </div>
            <div className="mt-0.5 text-[10px] text-content-tertiary leading-snug">
              Pick the execution route, then assign saved provider profiles below.
              Credentials are managed once under Settings → Ingestion.
            </div>
          </div>
          <div
            className={`hidden sm:flex items-center gap-1.5 border px-2 py-1 text-[9px] font-bold tracking-widest uppercase ${workflowBadgeClass(
              currentMeta.kind,
            )}`}
          >
            <WorkflowIcon kind={currentMeta.kind} />
            {currentMeta.badge}
          </div>
        </div>
        <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-2">
          {visibleWorkflows.map((item) => {
            const selected = item.key === current;
            return (
              <button
                key={item.key}
                id={`${idPrefix}-ingestion-workflow-${item.key}`}
                data-testid={`${idPrefix}-ingestion-workflow-${item.key}`}
                type="button"
                onClick={() =>
                  onChange(applyWorkflowToConfig(config, item.key, providerProfiles))
                }
                className={`min-h-[138px] text-left border px-3 py-2.5 transition-colors ${workflowKindClass(
                  item.kind,
                  selected,
                )}`}
              >
                <div className="flex items-start justify-between gap-2">
                  <span
                    className={`inline-flex items-center gap-1.5 border px-1.5 py-0.5 text-[8px] font-bold tracking-widest uppercase ${workflowBadgeClass(
                      item.kind,
                    )}`}
                  >
                    <WorkflowIcon kind={item.kind} />
                    {item.badge}
                  </span>
                  {selected && <Check className="w-4 h-4 shrink-0" />}
                </div>
                <div className="mt-2 text-[12px] font-bold tracking-wider uppercase text-content-primary">
                  {item.label}
                </div>
                <div className="mt-1 text-[10px] leading-snug text-content-secondary">
                  {item.detail}
                </div>
                <div className="mt-2 grid gap-1 text-[9px] leading-snug">
                  <div>
                    <span className="font-bold uppercase tracking-widest text-content-tertiary">
                      Runs:
                    </span>{" "}
                    <span className="text-content-primary">{item.execution}</span>
                  </div>
                  <div>
                    <span className="font-bold uppercase tracking-widest text-content-tertiary">
                      Output:
                    </span>{" "}
                    <span className="text-content-primary">{item.outcome}</span>
                  </div>
                </div>
              </button>
            );
          })}
        </div>
        <details className="mt-2">
          <summary className="cursor-pointer text-[9px] font-bold tracking-widest uppercase text-content-tertiary hover:text-content-secondary">
            Legacy / migration routes
          </summary>
          <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-2">
            {WORKFLOW_META.filter(
              (item) =>
                !primaryWorkflowIds.includes(item.key) && item.key !== current,
            ).map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() =>
                  onChange(applyWorkflowToConfig(config, item.key, providerProfiles))
                }
                className={`text-left border px-2.5 py-2 transition-colors ${workflowKindClass(
                  item.kind,
                  false,
                )}`}
              >
                <div className="flex items-center gap-1.5 text-[10px] font-bold tracking-widest uppercase">
                  <WorkflowIcon kind={item.kind} />
                  {item.label}
                </div>
                <div className="mt-1 text-[9px] leading-snug text-content-tertiary">
                  {item.detail}
                </div>
              </button>
            ))}
          </div>
        </details>
        <div className="mt-2 text-[10px] text-content-tertiary leading-snug">
          {currentMeta.detail}
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-1.5 text-[10px]">
        <div className="border border-border-minimal bg-bg-surface px-2 py-1.5">
          <div className="flex items-center justify-between gap-2">
            <div className="text-content-tertiary uppercase tracking-widest text-[8px]">
              Extraction route
            </div>
            <span className={`border px-1 py-0.5 text-[8px] uppercase ${workflowBadgeClass(currentMeta.kind)}`}>
              {currentMeta.badge}
            </span>
          </div>
          <div className="text-content-primary font-bold uppercase mt-0.5">
            {humanEngineLabel(config.extraction_engine)}
          </div>
          <div className="text-content-tertiary mt-0.5">
            {config.extraction_engine === "off" ? "disabled" : "in-process CPU"}
          </div>
        </div>
        <div className="border border-border-minimal bg-bg-surface px-2 py-1.5">
          <div className="text-content-tertiary uppercase tracking-widest text-[8px]">
            Summary route
          </div>
          <div className="text-content-primary font-bold uppercase mt-0.5">
            {config.chunk_summarization ? "enabled" : "off"}
          </div>
          <div className="text-content-tertiary mt-0.5">
            {summaryPool.length ? poolLabel(summaryPool) : "configure below"}
          </div>
        </div>
        <div className="border border-border-minimal bg-bg-surface px-2 py-1.5">
          <div className="text-content-tertiary uppercase tracking-widest text-[8px]">
            Embedding route
          </div>
          <div className="text-content-primary font-bold uppercase mt-0.5">
            {config.embed_mode ?? "local"}
          </div>
          <div className="text-content-tertiary mt-0.5">
            {config.embedding_model} ({config.embedding_dimension}d)
          </div>
        </div>
      </div>
    </div>
  );
}

export function CorpusManager({ isOpen, onClose }: CorpusManagerProps) {
  const {
    corpora,
    setCorpora,
    selectedCorpusIds,
    setSelectedCorpusIds,
    toggleCorpusId,
  } = useChatStore();

  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const corpusLoadInFlight = useRef(false);
  const corpusLoadController = useRef<AbortController | null>(null);
  const corpusLoadRequestId = useRef(0);

  // Sprint 2B — Modal global status. Fetched once on open; gates the
  // embed_mode='modal' option in the per-corpus form.
  const [modalStatus, setModalStatus] = useState<ModalStatus | null>(null);
  const [globalIngestionDefaults, setGlobalIngestionDefaults] =
    useState<GlobalIngestionSettings | null>(null);

  // Create form state
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [newConfig, setNewConfig] = useState<IngestionConfig>(
    createDefaultIngestionConfig(null),
  );
  const [isCreating, setIsCreating] = useState(false);

  // Edit state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [editConfig, setEditConfig] = useState<IngestionConfig | null>(null);
  const [editError, setEditError] = useState<string | null>(null);

  // Delete confirmation
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  // Id whose delete request is in flight — drives the "Deleting…" state and
  // blocks double-fires (a slow cascade used to invite repeat clicks).
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // ESC closes the modal — matches SettingsModal behavior
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  // Expanded corpus for details
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Drill-down: selected corpus for document browser
  const [selectedCorpus, setSelectedCorpus] = useState<CorpusResponse | null>(
    null,
  );

  const loadCorpora = useCallback(async (opts?: { silent?: boolean }) => {
    if (corpusLoadInFlight.current && opts?.silent) return;
    corpusLoadController.current?.abort();
    const controller = new AbortController();
    corpusLoadController.current = controller;
    const requestId = ++corpusLoadRequestId.current;
    corpusLoadInFlight.current = true;
    const silent = Boolean(opts?.silent);
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 10000);
    if (!silent) {
      setIsLoading(true);
      setError(null);
    }
    try {
      const data = await api.listCorpora(controller.signal);
      if (requestId !== corpusLoadRequestId.current) return;
      setCorpora(data);
      const validIds = data.map((corpus) => corpus.corpus_id);
      const validSet = new Set(validIds);
      const currentSelectedIds = useChatStore.getState().selectedCorpusIds;
      const reconciledSelectedIds = currentSelectedIds.filter((id) => validSet.has(id));
      if (
        reconciledSelectedIds.length !== currentSelectedIds.length ||
        reconciledSelectedIds.some((id, index) => id !== currentSelectedIds[index])
      ) {
        setSelectedCorpusIds(reconciledSelectedIds);
      }
      useSettingsStore.getState().purgeStaleCorpusIds(validIds);
      setSelectedCorpus((prev) => {
        if (!prev) return prev;
        return data.find((c) => c.corpus_id === prev.corpus_id) ?? null;
      });
      setExpandedId((prev) => (prev && validSet.has(prev) ? prev : null));
      setEditingId((prev) => (prev && validSet.has(prev) ? prev : null));
    } catch (err) {
      if (!silent && requestId === corpusLoadRequestId.current) {
        setError(
          timedOut
            ? "Corpus readiness timed out after 10 seconds. Retry when storage pressure settles."
            : err instanceof Error && err.name !== "AbortError"
              ? err.message
              : "Corpus load was cancelled.",
        );
      }
    } finally {
      window.clearTimeout(timeout);
      if (requestId === corpusLoadRequestId.current) {
        corpusLoadInFlight.current = false;
        corpusLoadController.current = null;
        if (!silent) {
          setIsLoading(false);
        }
      }
    }
  }, [setCorpora, setSelectedCorpusIds]);

  useEffect(() => {
    if (isOpen) return;
    corpusLoadController.current?.abort();
    corpusLoadController.current = null;
    corpusLoadInFlight.current = false;
    setIsLoading(false);
  }, [isOpen]);

  useEffect(() => () => corpusLoadController.current?.abort(), []);

  useEffect(() => {
    if (isOpen) {
      loadCorpora();
      // Modal status is best-effort — backend may not have shipped the
      // endpoint yet (Terminal 1 in flight). Falsy status = treat as
      // "not deployed" in the EmbedSection gate.
      api
        .getModalStatus()
        .then(setModalStatus)
        .catch(() => setModalStatus(null));
      api
        .getGlobalSettings()
        .then((resp) => {
          const defaults = resp.settings.ingestion ?? null;
          setGlobalIngestionDefaults(defaults);
          if (!showCreateForm) {
            setNewConfig(createDefaultIngestionConfig(defaults));
          }
        })
        .catch(() => setGlobalIngestionDefaults(null));
    }
  }, [isOpen, loadCorpora, showCreateForm]);

  useEffect(() => {
    if (!isOpen) return;
    const timer = window.setInterval(() => {
      void loadCorpora({ silent: true });
    }, 30000);
    return () => window.clearInterval(timer);
  }, [isOpen, loadCorpora]);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setIsCreating(true);
    setError(null);
    try {
      const payload: CorpusCreate = {
        name: newName.trim(),
        description: newDescription.trim() || null,
        default_ingestion_config: newConfig,
      };
      const created = await api.createCorpus(payload);
      setCorpora([created, ...corpora]);
      setNewName("");
      setNewDescription("");
      setNewConfig(createDefaultIngestionConfig(globalIngestionDefaults));
      setShowCreateForm(false);
      // UX: auto-drill into the new corpus so the user lands on the ingest screen
      setSelectedCorpus(created);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create corpus");
    } finally {
      setIsCreating(false);
    }
  };

  const handleUpdate = async (corpusId: string) => {
    if (!editName.trim()) return;
    setError(null);
    setEditError(null);
    try {
      const payload: {
        name: string;
        description: string | null;
        default_ingestion_config?: IngestionConfig;
      } = {
        name: editName.trim(),
        description: editDescription.trim() || null,
      };
      if (editConfig) {
        payload.default_ingestion_config = editConfig;
      }
      const updated = await api.updateCorpus(corpusId, payload);
      setCorpora(corpora.map((c) => (c.corpus_id === corpusId ? updated : c)));
      setEditingId(null);
      setEditConfig(null);
      setEditError(null);
    } catch (err) {
      // Preserve in-progress editConfig so user can fix the offending field
      // (e.g. locked embedding fields → backend returns 409).
      const msg = err instanceof Error ? err.message : "Failed to update corpus";
      setEditError(msg);
    }
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditConfig(null);
    setEditError(null);
  };

  const handleDelete = async (corpusId: string) => {
    if (deletingId) return; // a delete is already in flight — ignore re-clicks
    setError(null);
    setDeletingId(corpusId);
    try {
      await api.deleteCorpus(corpusId);
      setCorpora(corpora.filter((c) => c.corpus_id !== corpusId));
      setSelectedCorpusIds(selectedCorpusIds.filter((id) => id !== corpusId));
      setDeleteConfirmId(null);
      if (expandedId === corpusId) setExpandedId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete corpus");
    } finally {
      setDeletingId(null);
    }
  };

  const startEdit = (corpus: CorpusResponse) => {
    setEditingId(corpus.corpus_id);
    setEditName(corpus.name);
    setEditDescription(corpus.description || "");
    // Clone so in-progress edits don't mutate the corpus list state.
    // Stamp the inferred preset so the radio group pre-selects correctly:
    // legacy corpora lack a stored preset, and even stored "balanced"
    // defaults can disagree with the toggles (pre-feature rows). Trust
    // the toggles over the Pydantic default.
    const cloned = corpus.default_ingestion_config
      ? (JSON.parse(JSON.stringify(corpus.default_ingestion_config)) as IngestionConfig)
      : null;
    if (cloned) {
      cloned.preset = inferPreset(cloned);
    }
    setEditConfig(cloned);
    setEditError(null);
    setExpandedId(corpus.corpus_id);
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };

  // Drill-down: show corpus detail with document browser
  if (isOpen && selectedCorpus) {
    return (
      <div className="fixed inset-0 z-[100] flex items-stretch sm:items-center justify-center sm:p-4">
        <div
          data-testid="corpus-manager-dialog"
          className="absolute inset-0 bg-bg-base animate-overlay-in opacity-100"
          onClick={() => setSelectedCorpus(null)}
        />
        <div
          className="relative w-full h-dvh sm:h-[85vh] sm:max-h-[800px] sm:min-h-[500px] sm:max-w-[1200px] bg-[#242424] rounded-none sm:rounded-lg shadow-2xl flex flex-col overflow-hidden border border-white/5"
          style={{ fontFamily: "Inter, -apple-system, sans-serif" }}
        >
          <CorpusDetail
            corpus={selectedCorpus}
            onBack={() => setSelectedCorpus(null)}
            onCorpusUpdated={(updated) => {
              setCorpora(
                corpora.map((c) =>
                  c.corpus_id === updated.corpus_id ? updated : c,
                ),
              );
              setSelectedCorpus((prev) =>
                prev?.corpus_id === updated.corpus_id ? updated : prev,
              );
            }}
            onEditConfig={(c) => {
              setSelectedCorpus(null);
              startEdit(c);
            }}
          />
        </div>
      </div>
    );
  }

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-stretch sm:items-center justify-center sm:p-4">
      <div
        data-testid="corpus-manager-dialog"
        className="absolute inset-0 bg-bg-base animate-overlay-in opacity-100"
        onClick={onClose}
      />
      <div
        className="relative w-full h-dvh sm:h-[85vh] sm:max-h-[800px] sm:min-h-[500px] sm:max-w-[1200px] bg-[#242424] rounded-none sm:rounded-lg shadow-2xl flex flex-col overflow-hidden border border-white/5"
        style={{ fontFamily: "Inter, -apple-system, sans-serif" }}
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-3 px-4 sm:px-6 py-3 sm:py-4 border-b border-white/5 shrink-0">
          <div className="flex min-w-0 items-center gap-3">
            <Database className="w-5 h-5 text-accent-main" />
            <div className="min-w-0">
              <div className="text-[13px] font-semibold text-white">Corpus Manager</div>
              <div className="mt-0.5 truncate text-[10px] text-content-tertiary">
                {corpora.length} corpora · {corpora.filter((item) => item.readiness?.status !== "fully_enriched").length} need attention
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              data-testid="create-corpus-btn"
              onClick={() => {
                const next = !showCreateForm;
                if (next) {
                  setNewConfig(createDefaultIngestionConfig(globalIngestionDefaults));
                }
                setShowCreateForm(next);
              }}
              className="flex items-center gap-2 px-3 py-1.5 text-[12px] font-medium text-accent-main border border-accent-main hover:bg-accent-main hover:text-bg-base transition-colors"
            >
              <Plus className="w-4 h-4" />
              <span>New Corpus</span>
            </button>
            <button
              data-testid="corpus-manager-close"
              onClick={onClose}
              className="p-1.5 text-gray-400 hover:text-white transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Error Banner */}
        {error && (
          <div className="flex items-center gap-2 px-4 py-2 bg-error/10 border-b border-error/30 text-[11px] text-error">
            <AlertTriangle className="w-3 h-3 shrink-0" />
            <span className="flex-1">{error}</span>
            <button
              type="button"
              onClick={() => void loadCorpora()}
              className="inline-flex items-center gap-1 hover:text-content-primary"
              title="Retry corpus readiness"
            >
              <RefreshCw className="w-3 h-3" />
              Retry
            </button>
            <button
              onClick={() => setError(null)}
              className="hover:text-content-primary"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        )}

        {/* Create Form — claims the full remaining modal body when open.
            Phase 19.3: the form grew large (IngestionModelsSection + schema
            tooltips) so it takes the whole body via flex-1 and the corpus
            list is hidden to avoid a squished dual-scroll region. */}
        {showCreateForm && (
          <div className="flex-1 min-h-0 px-4 py-3 border-b border-border-minimal bg-bg-surface/50 space-y-3 overflow-y-auto custom-scrollbar">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-[11px] font-bold tracking-widest text-content-secondary uppercase">
                  &gt; New Ingestion Control Plane
                </div>
                <div className="mt-0.5 text-[10px] text-content-tertiary">
                  Corpus setup → extraction profile → model routing → validation → ready
                </div>
              </div>
              <div className="text-[9px] font-bold tracking-widest uppercase text-accent-secondary">
                new runs use this contract
              </div>
            </div>

            <section className="border border-border-minimal bg-bg-base/40 px-3 py-2 space-y-2">
              <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase">
                1. Corpus / Document Setup
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)] gap-2">
                <input
                  data-testid="corpus-name-input"
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="corpus_name"
                  className="w-full px-2 py-1.5 bg-bg-base border border-border-minimal text-[12px] text-content-primary placeholder:text-content-tertiary focus:outline-none focus:border-accent-main"
                />
                <input
                  type="text"
                  value={newDescription}
                  onChange={(e) => setNewDescription(e.target.value)}
                  placeholder="description (optional)"
                  className="w-full px-2 py-1.5 bg-bg-base border border-border-minimal text-[12px] text-content-primary placeholder:text-content-tertiary focus:outline-none focus:border-accent-main"
                />
              </div>
            </section>

            <section className="space-y-2">
              <IngestionWorkflowSelector
                config={newConfig}
                onChange={setNewConfig}
                idPrefix="create"
                providerProfiles={globalIngestionDefaults?.provider_models ?? []}
              />
            </section>

            {/* Parent/child token budgets are AUTO-TUNED (validated defaults
                sent on create). Overrides remain available via the API only —
                deliberately absent from the UI (owner decision 2026-07-03). */}

            {/* Chunking is AUTO-tuned end to end — token budgets, overlap and
                summary caps all ship validated defaults on create; overrides
                are deliberately API-only (owner decision 2026-07-03). */}
            <section className="border border-border-minimal bg-bg-base/40 px-3 py-2">
              <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase mb-1.5">
                2. Clean / Chunk
              </div>
              <div
                className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary"
                title="Resolved per file after parsing: prose → semantic_split (one idea per child); lists/lines/code/tables/transcripts auto-route; SaT sentence engine; topic-fused paragraphs escalate via embeddings; structureless docs get semantic parents."
              >
                AUTO
              </div>
              <div className="mt-1 text-[8px] text-content-tertiary leading-tight">
                file-type routers · child: semantic_split · budgets, overlap &amp; summaries auto-tuned
              </div>
            </section>

            {/* GHOST A + GHOST B Model Profiles (Phase 19.3) */}
            <section className="space-y-2">
              <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase">
                3. Model Routing
              </div>
              <IngestionModelsSection
                config={newConfig}
                onPatch={(patch) =>
                  setNewConfig((prev) => ({ ...prev, ...patch }))
                }
                editing={true}
                providerProfiles={globalIngestionDefaults?.provider_models ?? []}
              />
            </section>

            {/* Embed dispatch — three-way selector + per-mode credentials */}
            <section className="space-y-2">
              <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase">
                4. Indexing Route
              </div>
              <EmbedSection
                config={newConfig}
                onPatch={(patch) =>
                  setNewConfig((prev) => ({ ...prev, ...patch }))
                }
                modalStatus={modalStatus}
              />
            </section>

            {/* Confidence threshold sits on its own because it's GHOST-B-specific
                and orthogonal to the model identity. */}
            <section className="border border-border-minimal bg-bg-base/40 px-3 py-2 space-y-2">
              <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase">
                5. Validation &amp; Promotion
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-1.5">
              <div>
                <label className="text-[9px] text-content-tertiary tracking-wider">
                  ENTITY CONFIDENCE
                </label>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={newConfig.entity_confidence_threshold}
                  onChange={(e) =>
                    setNewConfig((prev) => ({
                      ...prev,
                      entity_confidence_threshold:
                        parseFloat(e.target.value) || 0.5,
                    }))
                  }
                  className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                  title="Extraction rows below this confidence are dropped before schema enforcement."
                />
              </div>
              <div className="col-span-2 flex items-center">
                <div className="text-[9px] text-content-tertiary leading-snug">
                  Rows below this confidence are dropped before schema
                  enforcement. 0.5 is a balanced default.
                </div>
              </div>
              </div>

            {/* Universal Extraction Schema — read-only notice.
                Entity types / relation predicates / strict mode are now
                baked into ghost_b.UNIVERSAL_*_SCHEMA and applied to every
                corpus. See GOTCHAS.md §66. */}
              <div className="text-[9px] text-content-tertiary/70 leading-relaxed">
                Extraction uses the universal schema backend-side. Structured
                output, required evidence, semantic direction checks, and graph
                promotion gates decide what can become durable graph data.
              </div>
            </section>

            <div className="flex gap-2">
              <Button
                data-testid="corpus-create-submit"
                variant="primary"
                onClick={handleCreate}
                disabled={!newName.trim() || isCreating}
                className="font-bold tracking-widest uppercase text-[11px] disabled:cursor-not-allowed"
              >
                {isCreating ? (
                  <Loader2 className="w-3 h-3 animate-spin" />
                ) : (
                  <Check className="w-3 h-3" />
                )}
                {isCreating ? "Creating..." : "Create"}
              </Button>
              <Button
                variant="secondary"
                onClick={() => {
                  setShowCreateForm(false);
                  setNewName("");
                  setNewDescription("");
                  setNewConfig(createDefaultIngestionConfig(globalIngestionDefaults));
                }}
                className="font-bold tracking-widest uppercase text-[11px]"
              >
                Cancel
              </Button>
            </div>
          </div>
        )}

        {/* Corpus List — hidden while the Create form is open so the form
            can use the full body height. */}
        <div
          className={`${showCreateForm ? "hidden" : "flex-1"} overflow-y-auto custom-scrollbar`}
        >
          {isLoading ? (
            <div className="flex items-center justify-center py-12 text-[11px] text-content-tertiary tracking-widest">
              <Loader2 className="w-4 h-4 animate-spin mr-2" />
              LOADING_CORPORA...
            </div>
          ) : corpora.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-[11px] text-content-tertiary tracking-widest">
              <FolderClosed className="w-8 h-8 mb-3 text-content-tertiary/50" />
              <span>[NO_CORPORA_FOUND]</span>
              <span className="mt-1 opacity-60">
                &gt; Create a corpus to begin ingestion
              </span>
            </div>
          ) : (
            <div className="space-y-2 p-3 sm:p-4">
              {corpora.map((corpus) => {
                const isSelected = selectedCorpusIds.includes(corpus.corpus_id);
                const isExpanded = expandedId === corpus.corpus_id;
                const isEditing = editingId === corpus.corpus_id;
                const isPendingDelete = deleteConfirmId === corpus.corpus_id;
                const isDeleting = deletingId === corpus.corpus_id;
                const readinessDocs = corpus.readiness?.documents;
                const queryableDocs = readinessDocs?.queryable ?? corpus.ready_doc_count ?? 0;
                const totalDocs = readinessDocs?.total ?? corpus.doc_count;
                const excludedDocs = readinessDocs?.excluded_total ?? 0;
                const lexiconReady = readinessDocs?.lexicon_ready;
                const readinessStatus = corpus.readiness?.status;
                const readinessGraph = corpus.readiness?.graph;
                const graphRequired = readinessGraph?.required !== false;
                const graphPromotionPending = graphRequired
                  ? readinessGraph?.pending ?? 0
                  : 0;
                const graphMetadataMarkRows = graphRequired
                  ? readinessGraph?.unmarked_promoted_extraction_rows ??
                    readinessGraph?.unpromoted_extraction_rows ??
                    0
                  : 0;
                const graphFailedChunks = graphRequired
                  ? readinessGraph?.failed_chunks ?? 0
                  : 0;
                const graphStaleFailureRows = graphRequired
                  ? readinessGraph?.stale_failure_rows ?? 0
                  : 0;
                const summaries = corpus.readiness?.summaries;
                const retrievalSummaryDone =
                  summaries?.retrieval_parent_done ?? summaries?.body_parent_done ?? 0;
                const retrievalSummaryTotal =
                  summaries?.retrieval_parent_total ?? summaries?.body_parent_total ?? 0;
                const documentSummaryDone =
                  summaries?.document_synced_done ?? summaries?.document_done ?? 0;
                const documentSummaryTotal = summaries?.document_total ?? totalDocs;
                const primaryAction = corpus.readiness?.next_actions?.[0];

                return (
                  <div
                    key={corpus.corpus_id}
                    className={`group border border-border-minimal bg-bg-base/40 transition-none ${
                      isSelected
                        ? "bg-accent-main/5 border-l-2 border-l-accent-main"
                        : "border-l-2 border-l-transparent hover:bg-bg-surface/50"
                    }`}
                  >
                    <div className="px-4 py-3">
                      <div className="flex items-start gap-3">
                        <div className="flex items-center gap-2 pt-0.5 shrink-0">
                          <button
                            onClick={() => setExpandedId(isExpanded ? null : corpus.corpus_id)}
                            className="p-1 text-content-tertiary hover:text-content-primary"
                            title={isExpanded ? "Hide corpus configuration" : "Show corpus configuration"}
                            aria-label={isExpanded ? "Collapse corpus" : "Expand corpus"}
                          >
                            {isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                          </button>
                          <button
                            onClick={() => toggleCorpusId(corpus.corpus_id)}
                            className={`w-5 h-5 border flex items-center justify-center ${
                              isSelected
                                ? "bg-accent-main border-accent-main text-bg-base"
                                : "border-border-minimal text-transparent hover:border-content-secondary"
                            }`}
                            title="Include this corpus in retrieval"
                            aria-label={`Include ${corpus.name} in retrieval`}
                          >
                            <Check className="w-3.5 h-3.5" />
                          </button>
                          {isSelected ? (
                            <FolderOpen className="w-4 h-4 text-accent-secondary" />
                          ) : (
                            <FolderClosed className="w-4 h-4 text-content-tertiary" />
                          )}
                        </div>

                        <div className="min-w-0 flex-1">
                          {isEditing ? (
                            <div className="flex items-center gap-2">
                              <input
                                type="text"
                                value={editName}
                                onChange={(e) => setEditName(e.target.value)}
                                className="min-w-0 flex-1 px-2 py-1 bg-bg-base border border-accent-main text-[13px] text-content-primary focus:outline-none"
                                autoFocus
                              />
                              <Button variant="primary" size="icon" onClick={() => handleUpdate(corpus.corpus_id)} title="Save corpus name">
                                <Check className="w-3.5 h-3.5" />
                              </Button>
                              <Button variant="ghost" size="icon" onClick={cancelEdit} title="Cancel editing">
                                <X className="w-3.5 h-3.5" />
                              </Button>
                            </div>
                          ) : (
                            <>
                              <div className="flex flex-wrap items-center gap-2">
                                <h3 className="min-w-0 truncate text-[13px] font-bold text-content-primary">
                                  {corpus.name}
                                </h3>
                                {readinessStatus && (
                                  <span
                                    className={`px-2 py-0.5 border text-[9px] font-bold uppercase ${readinessTone(readinessStatus)}`}
                                    title={(corpus.readiness?.blocking ?? []).join(" · ") || "Durable corpus readiness"}
                                  >
                                    {readinessLabel(readinessStatus)}
                                  </span>
                                )}
                              </div>
                              {corpus.description && (
                                <p className="mt-1 max-w-3xl truncate text-[10px] text-content-tertiary">
                                  {corpus.description}
                                </p>
                              )}
                            </>
                          )}
                        </div>

                        {!isEditing && (
                          <div className="flex items-center gap-1 shrink-0">
                          <Button
                            data-testid="corpus-browse-btn"
                            aria-label={`Open ${corpus.name}`}
                            variant="secondary"
                            size="sm"
                            onClick={() => setSelectedCorpus(corpus)}
                            className="font-bold tracking-widest uppercase text-[10px]"
                            title="View corpus documents and ingestion controls"
                          >
                            <ExternalLink className="w-3 h-3" />
                            <span className="hidden sm:inline">Open</span>
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => startEdit(corpus)}
                            title="Edit"
                          >
                            <Edit3 className="w-3 h-3" />
                          </Button>
                          {isPendingDelete ? (
                            <div className="flex items-center gap-1">
                              <Button
                                variant="danger"
                                size="sm"
                                onClick={() => handleDelete(corpus.corpus_id)}
                                disabled={isDeleting}
                                className="font-bold tracking-widest uppercase text-[9px] disabled:cursor-wait"
                              >
                                {isDeleting ? "Deleting…" : "Confirm"}
                              </Button>
                              <Button
                                variant="secondary"
                                size="sm"
                                onClick={() => setDeleteConfirmId(null)}
                                disabled={isDeleting}
                                className="font-bold tracking-widest uppercase text-[9px]"
                              >
                                No
                              </Button>
                            </div>
                          ) : (
                            <Button
                              variant="ghost"
                              size="icon"
                              onClick={() =>
                                setDeleteConfirmId(corpus.corpus_id)
                              }
                              className="hover:text-red-400 hover:bg-red-500/10"
                              title="Delete corpus"
                              aria-label="Delete corpus"
                            >
                              <Trash2 className="w-3 h-3" />
                            </Button>
                          )}
                        </div>
                      )}
                      </div>

                      {!isEditing && (
                        <>
                          <div className="mt-3 grid grid-cols-2 gap-px bg-border-minimal sm:grid-cols-3 lg:grid-cols-6">
                            {[
                              ["Queryable", `${queryableDocs}/${totalDocs}`, excludedDocs ? `${excludedDocs} excluded` : "documents"],
                              ["Chunks", compactCount(corpus.chunk_count), "indexed units"],
                              ["Retrieval summaries", `${retrievalSummaryDone}/${retrievalSummaryTotal}`, retrievalSummaryTotal > 0 ? `${Math.round((retrievalSummaryDone / retrievalSummaryTotal) * 100)}% complete` : "not required"],
                              ["Document summaries", `${documentSummaryDone}/${documentSummaryTotal}`, `${summaries?.document_mismatch ?? 0} drift`],
                              ["Vocabulary", lexiconReady === undefined ? "—" : `${lexiconReady}/${totalDocs}`, (readinessDocs?.lexicon_pending ?? 0) > 0 ? `${readinessDocs?.lexicon_pending} pending` : "ready"],
                              ["Graph", graphRequired ? `${readinessGraph?.promoted ?? 0}/${totalDocs}` : "Off", graphRequired ? `${graphPromotionPending} pending` : "not required"],
                            ].map(([label, value, detail]) => (
                              <div key={label} className="min-w-0 bg-bg-base px-3 py-2">
                                <div className="truncate text-[9px] font-bold uppercase text-content-tertiary">{label}</div>
                                <div className="mt-0.5 truncate text-[12px] font-semibold text-content-primary">{value}</div>
                                <div className="truncate text-[9px] text-content-tertiary">{detail}</div>
                              </div>
                            ))}
                          </div>

                          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[9px]">
                            <div className="min-w-0 flex-1">
                              {primaryAction ? (
                                <div className={primaryAction.blocked_by_pressure ? "text-content-tertiary" : "text-amber-300"} title={primaryAction.reason}>
                                  <span className="font-bold uppercase">Next: </span>
                                  {primaryAction.label}{primaryAction.count > 0 ? ` (${primaryAction.count})` : ""}
                                  <span className="ml-1 text-content-tertiary">{primaryAction.reason}</span>
                                </div>
                              ) : graphFailedChunks > 0 || graphMetadataMarkRows > 0 || graphStaleFailureRows > 0 ? (
                                <div className="text-amber-300">Graph audit has unresolved extraction metadata.</div>
                              ) : (
                                <div className="text-content-tertiary">No blocking action reported.</div>
                              )}
                            </div>
                            {corpus.readiness?.computed_at && (
                              <span className={corpus.readiness.stale ? "text-amber-300" : "text-content-tertiary"}>
                                Updated {formatDate(corpus.readiness.computed_at)}
                              </span>
                            )}
                          </div>
                        </>
                      )}
                    </div>

                    {/* Edit Panel — name/description/full IngestionConfig.
                        Backend silently locks embedding_model/dimension/model_id
                        once doc_count > 0 (returns 409). Everything else is fair
                        game and applies to NEW ingests only — existing docs keep
                        their snapshotted ingestion_config (per GOTCHA #48). */}
                    {isEditing && editConfig && (
                      <div className="px-4 pb-3 pl-12 space-y-3">
                        <div className="flex items-start gap-2 px-3 py-2 bg-amber-500/10 border border-amber-500/30 text-[11px] text-amber-300 leading-snug">
                          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                          <span>
                            Changes apply to <strong>NEW ingests only</strong>.
                            Existing documents are NOT re-processed. To re-extract
                            an old doc with the new schema/models, clear its
                            write_state.neo4j_written and re-ingest.
                          </span>
                        </div>

                        {editError && (
                          <div className="flex items-start gap-2 px-3 py-2 bg-error/10 border border-error/30 text-[11px] text-error leading-snug">
                            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                            <span className="flex-1">{editError}</span>
                            <button
                              onClick={() => setEditError(null)}
                              className="text-content-tertiary hover:text-content-primary"
                            >
                              <X className="w-3 h-3" />
                            </button>
                          </div>
                        )}

                        <IngestionWorkflowSelector
                          config={editConfig}
                          onChange={(next) => setEditConfig(next)}
                          idPrefix={`edit-${corpus.corpus_id}`}
                          providerProfiles={globalIngestionDefaults?.provider_models ?? []}
                        />

                        <div>
                          <label className="text-[9px] text-content-tertiary tracking-wider">
                            DESCRIPTION
                          </label>
                          <input
                            type="text"
                            value={editDescription}
                            onChange={(e) => setEditDescription(e.target.value)}
                            placeholder="Optional description"
                            className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                          />
                        </div>

                        {/* ─── LOCKED — Structural ──────────────────────────
                            Cannot be changed once a corpus exists. Backend
                            enforces (FROZEN_CONFIG_FIELDS in
                            services/ingestion_service.py); UI mirrors. */}
                        <LockedStructuralSection config={editConfig} />

                        {/* ─── MUTABLE — Provider / Credentials ────────────
                            Apply to NEW ingests only. Existing docs keep
                            their snapshotted ingestion_config (GOTCHA #48). */}
                        <div className="border-t border-accent-main/20 pt-3 space-y-3">
                          <div className="flex items-center gap-2 text-[10px] font-bold tracking-widest uppercase text-accent-main">
                            <span className="w-1 h-3 bg-accent-main" />
                            Mutable — provider mappings
                          </div>

                          <EmbedSection
                            config={editConfig}
                            onPatch={(patch) =>
                              setEditConfig((prev) =>
                                prev ? { ...prev, ...patch } : prev,
                              )
                            }
                            modalStatus={modalStatus}
                            corpusId={corpus.corpus_id}
                          />

                          <IngestionModelsSection
                            config={editConfig}
                            onPatch={(patch) =>
                              setEditConfig((prev) =>
                                prev ? { ...prev, ...patch } : prev,
                              )
                            }
                            editing={true}
                            corpusId={corpus.corpus_id}
                            providerProfiles={globalIngestionDefaults?.provider_models ?? []}
                          />

                          <div className="grid grid-cols-1 sm:grid-cols-3 gap-1.5">
                            <div>
                              <label className="text-[9px] text-content-tertiary tracking-wider uppercase">
                                Entity confidence
                              </label>
                              <input
                                type="number"
                                min={0}
                                max={1}
                                step={0.05}
                                value={editConfig.entity_confidence_threshold}
                                onChange={(e) =>
                                  setEditConfig((prev) =>
                                    prev
                                      ? {
                                          ...prev,
                                          entity_confidence_threshold:
                                            parseFloat(e.target.value) || 0.5,
                                        }
                                      : prev,
                                  )
                                }
                                className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                              />
                            </div>
                            <div className="col-span-2 flex items-center text-[9px] text-content-tertiary leading-snug">
                              Rows below this confidence are dropped before
                              schema enforcement. 0.5 is a balanced default.
                            </div>
                          </div>
                        </div>

                        {/* Save / Cancel */}
                        <div className="flex gap-2 pt-2 border-t border-border-minimal">
                          <button
                            onClick={() => handleUpdate(corpus.corpus_id)}
                            className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-bold tracking-widest bg-accent-main text-bg-base border border-accent-main hover:bg-accent-hover transition-none uppercase"
                          >
                            <Check className="w-3 h-3" />
                            Save changes
                          </button>
                          <button
                            onClick={cancelEdit}
                            className="px-3 py-1.5 text-[11px] font-bold tracking-widest text-content-tertiary border border-border-minimal hover:border-content-secondary transition-none uppercase"
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    )}

                    {/* Expanded Details */}
                    {isExpanded && !isEditing && (
                      <div className="px-4 pb-3 pl-12 space-y-2">
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-[11px]">
                          <div>
                            <span className="text-content-tertiary tracking-wider">
                              corpus_id:
                            </span>
                            <span className="ml-1 text-content-secondary font-bold">
                              {corpus.corpus_id.slice(0, 8)}...
                            </span>
                          </div>
                          <div>
                            <span className="text-content-tertiary tracking-wider">
                              embedding:
                            </span>
                            <span className="ml-1 text-content-secondary">
                              {corpus.embedding_model_id || "default"}
                            </span>
                          </div>
                          <div>
                            <span className="text-content-tertiary tracking-wider">
                              created:
                            </span>
                            <span className="ml-1 text-content-secondary">
                              {formatDate(corpus.created_at)}
                            </span>
                          </div>
                          <div>
                            <span className="text-content-tertiary tracking-wider">
                              updated:
                            </span>
                            <span className="ml-1 text-content-secondary">
                              {formatDate(corpus.updated_at)}
                            </span>
                          </div>
                        </div>
                        {/* Ingestion Config */}
                        <div className="text-[11px] space-y-1">
                          <div className="text-content-tertiary tracking-wider uppercase font-bold">
                            ingestion_config:
                          </div>
                          <div className="pl-2 grid grid-cols-1 sm:grid-cols-2 gap-1">
                            <span className="text-content-secondary">
                              use_neo4j:{" "}
                              <span
                                className={
                                  corpus.default_ingestion_config.use_neo4j
                                    ? "text-accent-main"
                                    : "text-content-tertiary"
                                }
                              >
                                {String(
                                  corpus.default_ingestion_config.use_neo4j,
                                )}
                              </span>
                            </span>
                            <span className="text-content-secondary">
                              summarize:{" "}
                              <span
                                className={
                                  corpus.default_ingestion_config
                                    .chunk_summarization
                                    ? "text-accent-main"
                                    : "text-content-tertiary"
                                }
                              >
                                {String(
                                  corpus.default_ingestion_config
                                    .chunk_summarization,
                                )}
                              </span>
                            </span>
                            <span className="text-content-secondary">
                              max_summary_tokens:{" "}
                              <span className="text-accent-secondary">
                                {
                                  corpus.default_ingestion_config
                                    .max_summary_tokens
                                }
                              </span>
                            </span>
                            <span className="text-content-secondary">
                              targets:{" "}
                              <span className="text-accent-secondary">
                                {corpus.default_ingestion_config.target_qdrant_collections.join(
                                  ", ",
                                )}
                              </span>
                            </span>
                          </div>
                        </div>
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
          <div className="text-[9px] text-content-tertiary">
            {corpora.length} corpora · {selectedCorpusIds.length} selected
          </div>
          <button
            onClick={() => void loadCorpora()}
            disabled={isLoading}
            className="flex items-center gap-1 text-[9px] font-bold text-content-tertiary hover:text-accent-main uppercase disabled:opacity-50"
          >
            <RefreshCw className="h-3 w-3" />
            Refresh
          </button>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// IngestionModelsSection — multi-model chip pools for GHOST A / GHOST B
// ============================================================================
//
// Renders two IngestionModelPool blocks (Summary / Extraction). Each chip is
// a ModelProfileRef with its own base_url / api_key / max_concurrent. When
// `models_linked` is true, the Extraction pool is rendered read-only and
// mirrors the Summary pool's chips (since the worker reuses summary_models
// for GHOST B in that mode).

function IngestionModelsSection({
  config,
  onPatch,
  editing,
  corpusId,
  providerProfiles,
}: {
  config: IngestionConfig;
  onPatch: (patch: Partial<IngestionConfig>) => void;
  editing: boolean;
  corpusId?: string;
  providerProfiles: ModelProfileRef[];
}) {
  const summaryPool = config.summary_models ?? [];
  const [contract, setContract] = useState<ExtractionContractResponse | null>(null);
  const [contractDown, setContractDown] = useState(false);

  useEffect(() => {
    if (!corpusId) return;
    let gone = false;
    api.getExtractionContract(corpusId)
      .then((value) => {
        if (!gone) {
          setContract(value);
          setContractDown(false);
        }
      })
      .catch(() => {
        if (!gone) setContractDown(true);
      });
    return () => { gone = true; };
  }, [corpusId, config.extraction_engine]);

  const engine = config.extraction_engine ?? "graphify_cpu";
  return (
    <div className="space-y-2">
      <div className="border border-border-minimal bg-bg-base px-3 py-2 space-y-1">
        <div className="text-[10px] font-bold tracking-widest text-content-tertiary uppercase">
          Extraction contract
        </div>
        <div className="text-[11px] text-content-primary font-bold uppercase">
          {humanEngineLabel(contract?.engine ?? engine)}
        </div>
        <div className="text-[10px] text-content-tertiary">
          {engine === "off"
            ? "Vectors-only opt-out. No graph extraction runs."
            : "Pinned GLiNER2 census and deterministic relation stages run in-process on CPU. No provider pool or fallback."}
        </div>
        {contractDown && (
          <div className="text-[10px] text-error">Saved contract endpoint unavailable.</div>
        )}
        {contract?.errors.map((error, index) => (
          <div key={index} className="text-[10px] text-error">ERROR: {error}</div>
        ))}
        {contract?.warnings.map((warning, index) => (
          <div key={index} className="text-[10px] text-amber-300">WARN: {warning}</div>
        ))}
      </div>

      <IngestionProviderSelector
        title="Summary routes"
        subtitle="Select saved Settings routes for optional parent-chunk summarization. Extraction never uses these providers."
        role="summary"
        profiles={providerProfiles}
        value={summaryPool}
        onChange={(next) => onPatch({ summary_models: next })}
        editing={editing}
      />
    </div>
  );
}

// ============================================================================
// EmbedSection — three-way embed_mode (local | api | modal)
// Sprint 2B.frontend
// ============================================================================
//
// MUTABLE section. Embedding *identity* (model + dimension) is FROZEN; only
// the dispatch path (which embedder, with what credentials) is editable.
//
// modal mode reads global Modal status to decide whether the radio is
// available — if Modal is not deployed, picking "modal" reveals a dead-end
// pointing to Settings → Infrastructure → Modal.

const EMBED_MODE_OPTIONS: { value: EmbedMode; label: string; hint: string }[] = [
  { value: "local", label: "Local", hint: "in-cluster sentence-transformers sidecar" },
  { value: "api", label: "API", hint: "OpenAI-compatible /embeddings endpoint" },
  { value: "modal", label: "Modal Cloud", hint: "your deployed Modal GPU app" },
];

const EMBEDDING_PROVIDER_PRESETS = [
  {
    id: "openai",
    name: "OpenAI",
    base_url: "https://api.openai.com/v1",
    example_model: "text-embedding-3-large",
  },
  {
    id: "siliconflow",
    name: "SiliconFlow",
    base_url: "https://api.siliconflow.com/v1",
    example_model: "Qwen/Qwen3-Embedding-0.6B",
  },
  {
    id: "together",
    name: "Together",
    base_url: "https://api.together.xyz/v1",
    example_model: "BAAI/bge-large-en-v1.5",
  },
  {
    id: "custom",
    name: "Custom (OpenAI-compat)",
    base_url: "",
    example_model: "",
  },
];

function EmbedSection({
  config,
  onPatch,
  modalStatus,
  corpusId,
}: {
  config: IngestionConfig;
  onPatch: (patch: Partial<IngestionConfig>) => void;
  modalStatus: ModalStatus | null;
  corpusId?: string;
}) {
  const mode = config.embed_mode ?? "local";
  const modalDeployed = !!modalStatus?.deployed;
  const modalContainersGlobal = modalStatus?.container_count ?? 10;

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    message: string;
  } | null>(null);

  const testApi = async () => {
    if (!config.embed_base_url || !config.embed_api_key) {
      setTestResult({ ok: false, message: "base_url and api_key required" });
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      // Probe by hitting `${base_url}/models` — most OpenAI-compatible
      // providers expose it cheaply. Per CLAUDE.md §13, all HTTP through
      // api.ts — but this endpoint isn't on our backend; it's a 3rd-party
      // provider, so a direct fetch is appropriate (and intentional).
      const url = config.embed_base_url.replace(/\/$/, "") + "/models";
      const resp = await fetch(url, {
        headers: { Authorization: `Bearer ${config.embed_api_key}` },
      });
      if (resp.ok) {
        setTestResult({ ok: true, message: `OK — ${resp.status}` });
      } else {
        setTestResult({
          ok: false,
          message: `${resp.status} ${resp.statusText}`,
        });
      }
    } catch (err) {
      setTestResult({
        ok: false,
        message: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase">
        Embedding
      </div>

      {/* Model label — locked, identical across all three modes */}
      <div className="text-[11px] text-content-secondary font-mono bg-bg-base border border-border-minimal px-2 py-1.5">
        {config.embedding_model} ({config.embedding_dimension}d)
        <span className="ml-2 text-[9px] text-content-tertiary tracking-widest uppercase">
          [locked]
        </span>
      </div>

      {/* Three-way radio */}
      <div role="radiogroup" aria-label="embed_mode" className="grid grid-cols-1 sm:grid-cols-3 gap-1.5">
        {EMBED_MODE_OPTIONS.map((opt) => {
          const checked = mode === opt.value;
          return (
            <label
              key={opt.value}
              title={opt.hint}
              className={`flex items-center justify-center gap-1.5 px-2 py-1.5 text-[10px] font-bold tracking-widest uppercase border cursor-pointer transition-none ${
                checked
                  ? "border-accent-main text-accent-main bg-accent-main/10"
                  : "border-border-minimal text-content-secondary hover:border-accent-main hover:text-accent-main"
              }`}
            >
              <input
                type="radio"
                name="embed_mode"
                value={opt.value}
                checked={checked}
                onChange={() => onPatch({ embed_mode: opt.value })}
                className="accent-accent-main"
              />
              {opt.label}
            </label>
          );
        })}
      </div>

      {/* Mode-specific body */}
      {mode === "local" && (
        <div className="text-[11px] text-content-tertiary leading-snug px-2 py-1.5 bg-bg-base border border-border-minimal">
          Uses the local <code className="bg-bg-surface px-1">embedder</code>{" "}
          sidecar — no cloud cost, GPU-bound.
        </div>
      )}

      {mode === "api" && (
        <div className="space-y-2">
          <IngestionModelPool
            title="Embedding APIs"
            subtitle="OpenAI-compatible /embeddings · batches round-robined across chips"
            value={config.embedding_models ?? []}
            onChange={(next) => onPatch({ embedding_models: next })}
            editing={true}
            presets={EMBEDDING_PROVIDER_PRESETS}
            composeModel={(_presetId, model) => model.trim()}
            modelPlaceholder="embedding model (required)"
            testKind="embedding"
            testContext={{ corpusId, poolField: "embedding_models" }}
          />
          <div className="text-[10px] text-content-tertiary leading-snug px-2 py-1.5 bg-bg-base border border-border-minimal">
            Add one or more embedding API chips. Each chip carries its own
            base URL, model, API key, and max in-flight calls. If this pool is
            empty, backend falls back to the legacy single API/global settings.
          </div>
          {config.embed_base_url && (
            <div className="flex items-center gap-2 px-2 py-1.5 bg-bg-base border border-border-minimal">
              <button
                onClick={testApi}
                disabled={testing || !config.embed_base_url}
                className="flex items-center gap-1 px-2 py-1 text-[10px] font-bold tracking-widest uppercase border border-accent-main text-accent-main hover:bg-accent-main hover:text-bg-base disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {testing ? (
                  <Loader2 className="w-3 h-3 animate-spin" />
                ) : (
                  <Check className="w-3 h-3" />
                )}
                Test legacy endpoint
              </button>
              {testResult && (
                <span
                  className={`text-[10px] font-mono ${testResult.ok ? "text-accent-main" : "text-error"}`}
                >
                  {testResult.message}
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {mode === "modal" && (
        <div className="space-y-2 px-2 py-2 bg-bg-base border border-border-minimal">
          {!modalDeployed ? (
            <div className="flex items-start gap-2 text-[11px] text-amber-300 leading-snug">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>
                Modal is not deployed. Configure tokens and deploy under{" "}
                <strong>Settings → Infrastructure → Modal</strong> first.
              </span>
            </div>
          ) : (
            <>
              <div className="text-[10px] text-content-tertiary leading-snug">
                Routes ingest embeds to your deployed Modal app. GPU tier and
                fleet size are configured globally under{" "}
                <strong>Settings → Infrastructure → Modal</strong>.
              </div>
              <div>
                <label className="flex items-center justify-between text-[9px] text-content-tertiary tracking-wider uppercase mb-0.5">
                  <span>Per-corpus container cap</span>
                  <span className="font-mono text-accent-main">
                    {config.modal_containers ?? modalContainersGlobal}
                  </span>
                </label>
                <input
                  type="range"
                  min={1}
                  max={Math.max(1, modalContainersGlobal)}
                  value={Math.min(
                    config.modal_containers ?? modalContainersGlobal,
                    modalContainersGlobal,
                  )}
                  onChange={(e) =>
                    onPatch({ modal_containers: Number(e.target.value) })
                  }
                  className="w-full accent-accent-main"
                />
                <div className="flex justify-between text-[9px] text-content-tertiary font-mono">
                  <span>1</span>
                  <span>global cap: {modalContainersGlobal}</span>
                </div>
              </div>
              <div className="flex items-start gap-2 text-[10px] text-amber-300/80 leading-snug">
                <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
                <span>
                  Modal embed mode incurs cloud GPU cost. Phase-gate: confirm
                  your Modal billing limits before enabling for large corpora.
                </span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// LockedStructuralSection — read-only mirror of frozen IngestionConfig fields
// ============================================================================
//
// These fields are FROZEN once a corpus exists (backend
// services/ingestion_service.FROZEN_CONFIG_FIELDS). Showing them disabled in
// the edit form prevents the user from filling out a value the API will
// reject with 409. To change any of these, the user creates a new corpus.

function LockedStructuralSection({ config }: { config: IngestionConfig }) {
  const preset = config.preset ?? inferPreset(config);
  const ents = config.entity_schema ?? [];
  const rels = config.relation_schema ?? [];
  const isLegacyCustom =
    (ents.length > 0 && ents.length !== 12) ||
    (rels.length > 0 && rels.length !== 17);

  const Row = ({ label, value }: { label: string; value: React.ReactNode }) => (
    <div className="flex items-center justify-between text-[11px] py-0.5">
      <span className="text-content-tertiary tracking-wider uppercase text-[9px]">
        {label}
      </span>
      <span className="text-content-secondary font-mono">{value}</span>
    </div>
  );

  return (
    <div className="space-y-2 border border-border-minimal bg-bg-base/40 px-3 py-2 opacity-90">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-[10px] font-bold tracking-widest uppercase text-content-tertiary">
          <span className="w-1 h-3 bg-content-tertiary" />
          Locked — structural
        </div>
        <span className="text-[9px] text-content-tertiary/70 italic">
          create a new corpus to alter these
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-0.5">
        <Row label="preset" value={preset} />
        <Row
          label="use_neo4j"
          value={String(config.use_neo4j)}
        />
        <Row
          label="chunk_summarization"
          value={String(config.chunk_summarization)}
        />
        <Row
          label="qdrant_targets"
          value={config.target_qdrant_collections.join(", ")}
        />
        <Row
          label="parent_tokens (min/tgt/max)"
          value={`${config.parent_chunk_tokens.min_tokens} / ${config.parent_chunk_tokens.target_tokens} / ${config.parent_chunk_tokens.max_tokens}`}
        />
        <Row
          label="child_tokens (min/tgt/max)"
          value={`${config.child_chunk_tokens.min_tokens} / ${config.child_chunk_tokens.target_tokens} / ${config.child_chunk_tokens.max_tokens}`}
        />
        <Row label="chunk_overlap" value={config.chunk_overlap} />
        <Row label="chunking_mode" value="auto per file" />
        <Row label="child_splitter" value="auto → sentence_merge" />
        <Row label="max_summary_tokens" value={config.max_summary_tokens} />
        <Row
          label="embedding"
          value={`${config.embedding_model_id} (${config.embedding_dimension}d)`}
        />
      </div>

      <div className="pt-1 border-t border-border-minimal/60">
        <div className="text-[9px] text-content-tertiary tracking-widest uppercase mb-0.5">
          extraction schema
        </div>
        {isLegacyCustom ? (
          <div className="text-[9px] text-amber-400/80 leading-relaxed">
            Custom schema in use ({ents.length} entity types, {rels.length}{" "}
            relations) — contact admin to reset to universal.
          </div>
        ) : (
          <div className="text-[9px] text-content-tertiary/70 leading-relaxed">
            Universal 12-type / 17-predicate schema (baked backend-side).
          </div>
        )}
      </div>
    </div>
  );
}
