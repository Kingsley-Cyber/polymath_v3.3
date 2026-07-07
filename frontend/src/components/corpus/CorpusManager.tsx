// CorpusManager.tsx - Corpus CRUD management interface
import { useState, useEffect, useCallback } from "react";
import {
  Plus,
  Trash2,
  Edit3,
  FolderOpen,
  FolderClosed,
  FileText,
  ChevronRight,
  ChevronDown,
  X,
  Check,
  Database,
  Loader2,
  AlertTriangle,
  ExternalLink,
} from "lucide-react";
import { useChatStore } from "../../stores/chatStore";
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
import { composeModelString, findPreset } from "../../types";
import { CorpusDetail } from "./CorpusDetail";
import { IngestionModelPool } from "../settings/IngestionModelPool";
import { Button } from "../ui/Button";

interface CorpusManagerProps {
  isOpen: boolean;
  onClose: () => void;
}

// DEFAULT_INGESTION_CONFIG imported from ../../types (complete version with all IngestionConfig fields)

// ── Preset selector ───────────────────────────────────────────────────────
// Four-way radio group. Fast / Balanced / Deep write both the `preset` field
// AND the underlying toggles so the outbound payload matches what the
// backend would apply anyway. Custom reveals the use_neo4j +
// chunk_summarization checkboxes for manual override.

const PRESET_META: {
  key: IngestionPreset;
  label: string;
  tooltip: string;
}[] = [
  {
    key: "fast",
    label: "Fast",
    tooltip: "Fastest ingest; vector/hybrid search only, no knowledge graph.",
  },
  {
    key: "balanced",
    label: "Balanced",
    tooltip: "Adds knowledge graph. No per-chunk summaries.",
  },
  {
    key: "deep",
    label: "Deep",
    tooltip:
      "Adds chunk summaries for hierarchical retrieval. Slowest, best recall.",
  },
  {
    key: "custom",
    label: "Custom",
    tooltip: "Reveal the underlying toggles and set them by hand.",
  },
];

type IngestionWorkflowId =
  | "local_only"
  | "rtx_only"
  | "cloud_only"
  | "local_cloud"
  | "local_rtx"
  | "cloud_rtx"
  | "all_lanes"
  | "fast_local_graph_rtx"
  | "vectors_only"
  | "custom";

const WORKFLOW_META: {
  key: IngestionWorkflowId;
  label: string;
  detail: string;
  engine: ExtractionEngine;
  needsCloudPool: boolean;
  needsRtx: boolean;
  needsCloudApi: boolean;
}[] = [
  {
    key: "rtx_only",
    label: "Private RTX LLM",
    detail:
      "Local desktop vLLM receives the same strict provider-card extraction contract as a cloud model.",
    engine: "local",
    needsCloudPool: true,
    needsRtx: true,
    needsCloudApi: false,
  },
  {
    key: "cloud_only",
    label: "Cloud/API LLM",
    detail:
      "Extraction goes to configured provider-card cloud chips with strict promotion gates.",
    engine: "cloud",
    needsCloudPool: true,
    needsRtx: false,
    needsCloudApi: true,
  },
  {
    key: "cloud_rtx",
    label: "Cloud + private RTX",
    detail: "Cloud/API and RTX provider-card chips share the extraction pool.",
    engine: "cloud",
    needsCloudPool: true,
    needsRtx: true,
    needsCloudApi: true,
  },
  {
    key: "local_only",
    label: "Legacy Mac sidecar",
    detail:
      "Deprecated GLiNER/GLiREL sidecar path. Use only for compatibility or controlled backfills.",
    engine: "legacy_local",
    needsCloudPool: false,
    needsRtx: false,
    needsCloudApi: false,
  },
  {
    key: "local_cloud",
    label: "Legacy sidecar + cloud",
    detail:
      "Transition mode: deprecated Mac sidecars and cloud chips split chunks deterministically.",
    engine: "dual",
    needsCloudPool: true,
    needsRtx: false,
    needsCloudApi: true,
  },
  {
    key: "local_rtx",
    label: "Legacy sidecar + RTX",
    detail: "Transition mode: deprecated Mac sidecars and RTX vLLM split extraction chunks.",
    engine: "dual",
    needsCloudPool: true,
    needsRtx: true,
    needsCloudApi: false,
  },
  {
    key: "all_lanes",
    label: "Legacy sidecar + cloud + RTX",
    detail:
      "Transition mode: deprecated Mac sidecars plus all configured cloud/RTX extraction chips.",
    engine: "dual",
    needsCloudPool: true,
    needsRtx: true,
    needsCloudApi: true,
  },
  {
    key: "fast_local_graph_rtx",
    label: "Deprecated local graph + RTX enrich",
    detail:
      "Legacy GLiNER/GLiREL skeleton first, then RTX re-extracts quality-gated gaps. Kept for migration only.",
    engine: "local_then_enrich",
    needsCloudPool: true,
    needsRtx: true,
    needsCloudApi: false,
  },
  {
    key: "vectors_only",
    label: "Vectors only",
    detail: "Skip graph extraction; vector/hybrid retrieval only.",
    engine: "off",
    needsCloudPool: false,
    needsRtx: false,
    needsCloudApi: false,
  },
  {
    key: "custom",
    label: "Custom",
    detail: "Keep the current engine and pools exactly as configured.",
    engine: "inherit",
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
  if ((next.extraction_engine ?? "local") === "local") {
    next.extraction_models = ensureRtxExtractionModel(next.extraction_models ?? []);
  }
  if (usesProviderEngine(draftEngine(next.extraction_engine, "cloud")) && hasFactoryChunkShape(next)) {
    next = {
      ...next,
      child_chunk_algorithm: "sentence_merge",
      child_chunk_tokens: { min_tokens: 128, target_tokens: 512, max_tokens: 700 },
    };
  }
  return next;
}

function isRtxModel(entry: ModelProfileRef): boolean {
  const provider = (entry.provider_preset || "").toLowerCase();
  const model = (entry.model || "").toLowerCase();
  const base = (entry.base_url || "").toLowerCase();
  const lifecycle = (entry.lifecycle_base_url || "").toLowerCase();
  const extra = entry.extra_params || {};
  return (
    provider === "vllm-rtx" ||
    provider === "vllm" ||
    Boolean(extra.managed_vllm) ||
    extra.resource_class === "rtx" ||
    model.includes("polymath-extract") ||
    model.includes("vllm") ||
    base.includes(":8000") ||
    lifecycle.includes(":8085")
  );
}

function hasNonRtxCloudModel(entries: ModelProfileRef[]): boolean {
  return entries.some((entry) => !isRtxModel(entry));
}

function makeRtxExtractionModel(): ModelProfileRef {
  const preset = findPreset("vllm-rtx");
  return {
    provider_preset: "vllm-rtx",
    model: composeModelString("vllm-rtx", preset?.example_model ?? "polymath-extract"),
    base_url: preset?.base_url ?? "http://192.168.1.83:8000/v1",
    api_key: null,
    max_concurrent: preset?.default_max_concurrent ?? 60,
    lifecycle_base_url: preset?.lifecycle?.base_url ?? "http://192.168.1.83:8085",
    lifecycle_api_key: null,
    lifecycle_auto_start: preset?.lifecycle?.auto_start ?? true,
    lifecycle_auto_stop: preset?.lifecycle?.auto_stop ?? false,
    lifecycle_up_path: "/up",
    lifecycle_status_path: "/status",
    lifecycle_down_path: "/down",
    lifecycle_ready_timeout_seconds:
      preset?.lifecycle?.ready_timeout_seconds ?? 360,
    extra_params: {
      ...(preset?.kwargs ?? {}),
      managed_vllm: true,
      resource_class: "rtx",
      supports_json_schema: true,
      schema_mode: "json_schema",
      json_repair_mode: "provider_native",
      semantic_verifier_mode: "strict_with_direction_repair",
      concurrency_policy: "adaptive_vram_85",
      failure_backfill_policy: "retry_then_stage",
      adaptive_vram: true,
      vram_safety_ratio: 0.85,
    },
  };
}

function ensureRtxExtractionModel(entries: ModelProfileRef[]): ModelProfileRef[] {
  if (entries.some(isRtxModel)) return entries;
  return [makeRtxExtractionModel(), ...entries];
}

function inferWorkflow(config: IngestionConfig): IngestionWorkflowId {
  const engine = draftEngine(config.extraction_engine, "local");
  const pool = config.extraction_models ?? [];
  const hasRtx = pool.some(isRtxModel);
  const hasCloud = hasNonRtxCloudModel(pool);
  if (engine === "off") return "vectors_only";
  if (engine === "legacy_local") return "local_only";
  if (engine === "local") return hasRtx ? "rtx_only" : "custom";
  if (engine === "cloud") {
    if (hasRtx && hasCloud) return "cloud_rtx";
    if (hasRtx) return "rtx_only";
    return "cloud_only";
  }
  if (engine === "local_then_enrich") {
    return hasRtx ? "fast_local_graph_rtx" : "custom";
  }
  if (engine === "dual" || engine === "local_then_cloud") {
    if (hasRtx && hasCloud) return "all_lanes";
    if (hasRtx) return "local_rtx";
    return "local_cloud";
  }
  return "custom";
}

// GLiNER-era factory chunk shape: tiny single-idea children sized for the
// local classifier. LLM extraction lanes want LLM-sized windows instead.
function hasFactoryChunkShape(cfg: IngestionConfig): boolean {
  const t = cfg.child_chunk_tokens;
  return (
    cfg.child_chunk_algorithm === "semantic_split" &&
    t?.target_tokens === 128 &&
    t?.max_tokens === 256
  );
}

function applyWorkflowToConfig(
  cfg: IngestionConfig,
  workflowId: IngestionWorkflowId,
): IngestionConfig {
  const workflow = WORKFLOW_META.find((item) => item.key === workflowId);
  if (!workflow || workflowId === "custom") return cfg;

  let next: IngestionConfig = {
    ...cfg,
    extraction_engine: workflow.engine,
    // New workflow choices are explicit. Ghost B never silently borrows
    // Summary chips unless a legacy corpus still carries models_linked=true.
    models_linked: false,
  };
  if (workflowId === "vectors_only") {
    next = applyPresetToConfig(next, "fast");
  } else if (next.preset === "fast" || inferPreset(next) === "fast") {
    next = applyPresetToConfig(next, "balanced");
  }
  if (workflow.needsRtx) {
    next.extraction_models = ensureRtxExtractionModel(next.extraction_models ?? []);
  }
  // Cloud-LLM extraction (RTX vLLM / API) reads context, not GLiNER spans:
  // 512-token children ≈ 4× fewer extraction calls per file at equal
  // coverage (measured on polymath_v2, 2026-07-05: 128-tok chunks made a
  // 508KB book cost 1,201 calls). Only applied when the corpus still has the
  // factory shape — user-customized chunking is never overridden, and the
  // backend freeze keeps already-populated corpora unchanged.
  if (
    (workflow.engine === "cloud" ||
      workflow.engine === "local" ||
      workflow.engine === "dual" ||
      // §13-H: storage chunks at LLM shape; the local GLiREL lane derives
      // its own sentence windows from parent text at extraction time.
      workflow.engine === "local_then_enrich") &&
    hasFactoryChunkShape(next)
  ) {
    next = {
      ...next,
      child_chunk_algorithm: "sentence_merge",
      child_chunk_tokens: { min_tokens: 128, target_tokens: 512, max_tokens: 700 },
    };
  }
  return next;
}

function poolLabel(entries: ModelProfileRef[]): string {
  if (!entries.length) return "empty";
  return entries.map(formatExtractionPoolEntry).join(" | ");
}

interface PresetSelectorProps {
  config: IngestionConfig;
  onChange: (next: IngestionConfig) => void;
  idPrefix: string; // unique per form (create vs edit) for radio grouping
}

function PresetModeSelector({ config, onChange, idPrefix }: PresetSelectorProps) {
  const current: IngestionPreset = config.preset ?? inferPreset(config);
  const isCustom = current === "custom";
  return (
    <div>
      <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase mb-1.5">
        Retrieval Depth
      </div>
      <div
        role="radiogroup"
        aria-label="Retrieval depth"
        className="grid grid-cols-2 sm:grid-cols-4 gap-1.5"
      >
        {PRESET_META.map((p) => {
          const checked = current === p.key;
          const id = `${idPrefix}-preset-${p.key}`;
          return (
            <label
              key={p.key}
              htmlFor={id}
              title={p.tooltip}
              className={`flex items-center justify-center gap-1.5 px-2 py-1.5 text-[9px] font-bold tracking-widest uppercase border cursor-pointer transition-none ${
                checked
                  ? "border-accent-main text-accent-main bg-accent-main/10"
                  : "border-border-minimal text-content-secondary hover:border-accent-main hover:text-accent-main"
              }`}
            >
              <input
                id={id}
                type="radio"
                name={`${idPrefix}-preset`}
                value={p.key}
                checked={checked}
                onChange={() => onChange(applyPresetToConfig(config, p.key))}
                className="accent-accent-main"
              />
              {p.label}
            </label>
          );
        })}
      </div>
      <div
        className="text-[9px] text-content-tertiary/70 leading-relaxed mt-1"
        data-testid={`${idPrefix}-preset-hint`}
      >
        {PRESET_META.find((p) => p.key === current)?.tooltip}
      </div>

      {isCustom && (
        <div
          className="flex flex-wrap gap-3 mt-2"
          data-testid={`${idPrefix}-custom-toggles`}
        >
          <label className="flex items-center gap-1.5 text-[11px] text-content-secondary tracking-wider">
            <input
              type="checkbox"
              checked={config.use_neo4j}
              onChange={(e) =>
                onChange({
                  ...config,
                  preset: "custom",
                  use_neo4j: e.target.checked,
                })
              }
              className="accent-accent-main"
            />
            use_neo4j
          </label>
          <label className="flex items-center gap-1.5 text-[11px] text-content-secondary tracking-wider">
            <input
              type="checkbox"
              checked={config.chunk_summarization}
              onChange={(e) =>
                onChange({
                  ...config,
                  preset: "custom",
                  chunk_summarization: e.target.checked,
                })
              }
              className="accent-accent-main"
            />
            chunk_summarization
          </label>
        </div>
      )}

    </div>
  );
}

function IngestionWorkflowSelector({
  config,
  onChange,
  idPrefix,
}: {
  config: IngestionConfig;
  onChange: (next: IngestionConfig) => void;
  idPrefix: string;
}) {
  const current = inferWorkflow(config);
  const currentMeta = WORKFLOW_META.find((item) => item.key === current) ?? WORKFLOW_META[0];
  const extractionPool = config.extraction_models ?? [];
  const summaryPool = config.summary_models ?? [];
  const providerActive = usesProviderEngine(draftEngine(config.extraction_engine, "local"));

  return (
    <div className="border border-accent-main/25 bg-bg-base/50 px-3 py-2 space-y-2">
      <div>
        <label
          htmlFor={`${idPrefix}-ingestion-workflow`}
          className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase"
        >
          Ingestion workflow
        </label>
        <select
          id={`${idPrefix}-ingestion-workflow`}
          data-testid={`${idPrefix}-ingestion-workflow`}
          value={current}
          onChange={(event) =>
            onChange(applyWorkflowToConfig(config, event.target.value as IngestionWorkflowId))
          }
          className="mt-1 w-full px-2 py-1.5 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
        >
          {WORKFLOW_META.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label}
            </option>
          ))}
        </select>
        <div className="mt-1 text-[10px] text-content-tertiary leading-snug">
          {currentMeta.detail}
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-1.5 text-[10px]">
        <div className="border border-border-minimal bg-bg-surface/50 px-2 py-1.5">
          <div className="text-content-tertiary uppercase tracking-widest text-[8px]">
            Extraction
          </div>
          <div className="text-content-primary font-bold uppercase mt-0.5">
            {draftEngine(config.extraction_engine, "local").replace(/_/g, " ")}
          </div>
          <div className="text-content-tertiary mt-0.5">
            {providerActive ? poolLabel(extractionPool) : "provider pool inactive"}
          </div>
        </div>
        <div className="border border-border-minimal bg-bg-surface/50 px-2 py-1.5">
          <div className="text-content-tertiary uppercase tracking-widest text-[8px]">
            Summary
          </div>
          <div className="text-content-primary font-bold uppercase mt-0.5">
            {config.chunk_summarization ? "enabled" : "off"}
          </div>
          <div className="text-content-tertiary mt-0.5">
            {summaryPool.length ? poolLabel(summaryPool) : "configure below"}
          </div>
        </div>
        <div className="border border-border-minimal bg-bg-surface/50 px-2 py-1.5">
          <div className="text-content-tertiary uppercase tracking-widest text-[8px]">
            Embedding
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

  const loadCorpora = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.listCorpora();
      setCorpora(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load corpora");
    } finally {
      setIsLoading(false);
    }
  }, [setCorpora]);

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
          className="absolute inset-0 bg-bg-base animate-overlay-in opacity-100"
          onClick={() => setSelectedCorpus(null)}
        />
        <div
          className="relative w-full h-dvh sm:h-[85vh] sm:max-h-[800px] sm:min-h-[500px] sm:max-w-[1200px] bg-[#242424] rounded-none sm:rounded-2xl shadow-2xl flex flex-col overflow-hidden border border-white/5"
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
        className="absolute inset-0 bg-bg-base animate-overlay-in opacity-100"
        onClick={onClose}
      />
      <div
        className="relative w-full h-dvh sm:h-[85vh] sm:max-h-[800px] sm:min-h-[500px] sm:max-w-[1200px] bg-[#242424] rounded-none sm:rounded-2xl shadow-2xl flex flex-col overflow-hidden border border-white/5"
        style={{ fontFamily: "Inter, -apple-system, sans-serif" }}
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-3 px-4 sm:px-6 py-3 sm:py-4 border-b border-white/5 shrink-0">
          <div className="flex items-center gap-2">
            <Database className="w-5 h-5 text-accent-main" />
            <span className="text-[13px] font-semibold text-white">
              Corpus Manager
            </span>
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
            <div className="text-[11px] font-bold tracking-widest text-content-secondary uppercase">
              &gt; Create New Corpus
            </div>
            <input
              data-testid="corpus-name-input"
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="corpus_name"
              className="w-full px-2 py-1.5 bg-bg-base border border-border-minimal text-[12px] text-content-primary placeholder:text-content-tertiary focus:outline-none focus:border-accent-main"
            />
            <IngestionWorkflowSelector
              config={newConfig}
              onChange={setNewConfig}
              idPrefix="create"
            />
            <input
              type="text"
              value={newDescription}
              onChange={(e) => setNewDescription(e.target.value)}
              placeholder="description (optional)"
              className="w-full px-2 py-1.5 bg-bg-base border border-border-minimal text-[12px] text-content-primary placeholder:text-content-tertiary focus:outline-none focus:border-accent-main"
            />

            {/* Parent/child token budgets are AUTO-TUNED (validated defaults
                sent on create). Overrides remain available via the API only —
                deliberately absent from the UI (owner decision 2026-07-03). */}

            {/* Chunking is AUTO-tuned end to end — token budgets, overlap and
                summary caps all ship validated defaults on create; overrides
                are deliberately API-only (owner decision 2026-07-03). */}
            <div>
              <label className="text-[9px] text-content-tertiary tracking-wider">
                CHUNKING
              </label>
              <div
                className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary"
                title="Resolved per file after parsing: prose → semantic_split (one idea per child); lists/lines/code/tables/transcripts auto-route; SaT sentence engine; topic-fused paragraphs escalate via embeddings; structureless docs get semantic parents."
              >
                AUTO
              </div>
              <div className="mt-1 text-[8px] text-content-tertiary leading-tight">
                file-type routers · child: semantic_split · budgets, overlap &amp; summaries auto-tuned
              </div>
            </div>

            {/* GHOST A + GHOST B Model Profiles (Phase 19.3) */}
            <IngestionModelsSection
              config={newConfig}
              onPatch={(patch) =>
                setNewConfig((prev) => ({ ...prev, ...patch }))
              }
              editing={true}
            />

            {/* Embed dispatch — three-way selector + per-mode credentials */}
            <EmbedSection
              config={newConfig}
              onPatch={(patch) =>
                setNewConfig((prev) => ({ ...prev, ...patch }))
              }
              modalStatus={modalStatus}
            />

            <PresetModeSelector
              config={newConfig}
              onChange={setNewConfig}
              idPrefix="create"
            />

            {/* Confidence threshold sits on its own because it's GHOST-B-specific
                and orthogonal to the model identity. */}
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
            <div>
              <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase mb-1.5">
                Extraction Schema
              </div>
              <div className="text-[9px] text-content-tertiary/70 leading-relaxed">
                Extraction uses a universal 12-type / 17-predicate schema
                (baked backend-side). No per-corpus tuning.
              </div>
            </div>

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
            <div className="divide-y divide-border-minimal">
              {corpora.map((corpus) => {
                const isSelected = selectedCorpusIds.includes(corpus.corpus_id);
                const isExpanded = expandedId === corpus.corpus_id;
                const isEditing = editingId === corpus.corpus_id;
                const isPendingDelete = deleteConfirmId === corpus.corpus_id;
                const isDeleting = deletingId === corpus.corpus_id;

                return (
                  <div
                    key={corpus.corpus_id}
                    className={`group transition-none ${
                      isSelected
                        ? "bg-accent-main/5 border-l-2 border-l-accent-main"
                        : "border-l-2 border-l-transparent hover:bg-bg-surface/50"
                    }`}
                  >
                    {/* Corpus Row */}
                    <div className="flex items-center gap-2 px-4 py-3">
                      {/* Expand Toggle */}
                      <button
                        onClick={() =>
                          setExpandedId(isExpanded ? null : corpus.corpus_id)
                        }
                        className="p-0.5 text-content-tertiary hover:text-content-primary transition-none"
                      >
                        {isExpanded ? (
                          <ChevronDown className="w-3 h-3" />
                        ) : (
                          <ChevronRight className="w-3 h-3" />
                        )}
                      </button>

                      {/* Selection Checkbox */}
                      <button
                        onClick={() => toggleCorpusId(corpus.corpus_id)}
                        className={`w-4 h-4 border flex items-center justify-center transition-none ${
                          isSelected
                            ? "bg-accent-main border-accent-main text-bg-base"
                            : "border-border-minimal text-transparent hover:border-content-secondary"
                        }`}
                      >
                        <Check className="w-3 h-3" />
                      </button>

                      {/* Folder Icon */}
                      {isSelected ? (
                        <FolderOpen className="w-3.5 h-3.5 text-accent-secondary shrink-0" />
                      ) : (
                        <FolderClosed className="w-3.5 h-3.5 text-content-tertiary shrink-0" />
                      )}

                      {/* Name / Edit */}
                      {isEditing ? (
                        <div className="flex-1 flex items-center gap-2">
                          <input
                            type="text"
                            value={editName}
                            onChange={(e) => setEditName(e.target.value)}
                            className="flex-1 px-1 py-0.5 bg-bg-base border border-accent-main text-[12px] text-content-primary focus:outline-none"
                            autoFocus
                          />
                          <button
                            onClick={() => handleUpdate(corpus.corpus_id)}
                            className="p-0.5 text-accent-main hover:text-accent-hover"
                          >
                            <Check className="w-3 h-3" />
                          </button>
                          <button
                            onClick={cancelEdit}
                            className="p-0.5 text-content-tertiary hover:text-content-primary"
                          >
                            <X className="w-3 h-3" />
                          </button>
                        </div>
                      ) : (
                        <div className="flex-1 min-w-0">
                          <div className="text-[12px] font-bold text-content-primary truncate">
                            {corpus.name}
                          </div>
                          {corpus.description && (
                            <div className="text-[9px] text-content-tertiary truncate mt-0.5">
                              {corpus.description}
                            </div>
                          )}
                        </div>
                      )}

                      {/* Stats */}
                      {!isEditing && (
                        <div className="flex items-center gap-3 text-[9px] text-content-tertiary tracking-wider shrink-0">
                          <span
                            className="flex items-center gap-1"
                            title={`${corpus.ready_doc_count ?? 0} fully verified of ${corpus.doc_count} document rows (in-flight and failed rows included in the total)`}
                          >
                            <FileText className="w-3 h-3" />
                            {corpus.ready_doc_count != null
                              ? `${corpus.ready_doc_count}/${corpus.doc_count} ready`
                              : corpus.doc_count}
                          </span>
                          <span>{corpus.chunk_count} chunks</span>
                        </div>
                      )}

                      {/* Actions */}
                      {!isEditing && (
                        <div className="flex items-center gap-1 shrink-0">
                          <Button
                            data-testid="corpus-browse-btn"
                            variant="secondary"
                            size="sm"
                            onClick={() => setSelectedCorpus(corpus)}
                            className="font-bold tracking-widest uppercase text-[10px]"
                            title="Browse documents and start backend folder ingest"
                          >
                            <ExternalLink className="w-3 h-3" />
                            <span>Open</span>
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
                            Mutable — provider / credentials
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
          <div className="text-[9px] text-content-tertiary tracking-widest">
            {corpora.length} CORPUS // {selectedCorpusIds.length} SELECTED
          </div>
          <button
            onClick={loadCorpora}
            disabled={isLoading}
            className="text-[9px] text-content-tertiary hover:text-accent-main tracking-widest uppercase transition-none disabled:opacity-50"
          >
            [REFRESH]
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

type ResolvedDraftEngine = Exclude<ExtractionEngine, "inherit">;

function draftEngine(
  engine: ExtractionEngine | undefined,
  inheritedEngine?: ResolvedDraftEngine,
): ResolvedDraftEngine {
  return engine && engine !== "inherit" ? engine : (inheritedEngine ?? "local");
}

function usesLegacyLocalEngine(engine: ResolvedDraftEngine): boolean {
  return (
    engine === "legacy_local" ||
    engine === "dual" ||
    engine === "local_then_cloud" ||
    engine === "local_then_enrich"
  );
}

function usesProviderEngine(engine: ResolvedDraftEngine): boolean {
  return (
    engine === "local" ||
    engine === "cloud" ||
    engine === "dual" ||
    engine === "local_then_cloud" ||
    engine === "local_then_enrich"
  );
}

type ContractPoolEntry = ExtractionContractResponse["pool"][number];
type ProviderCard = NonNullable<ContractPoolEntry["provider_card"]>;

function isContractPoolEntry(
  entry: ModelProfileRef | ContractPoolEntry,
): entry is ContractPoolEntry {
  return "provider_card" in entry || "lifecycle_status" in entry;
}

function inferDraftProviderCard(entry: ModelProfileRef): ProviderCard {
  const provider = (entry.provider_preset || "custom").toLowerCase();
  const model = (entry.model || "").toLowerCase();
  const base = (entry.base_url || "").toLowerCase();
  const extra = entry.extra_params || {};
  const rtx = isRtxModel(entry);
  const longcat = provider === "longcat" || base.includes("longcat") || model.includes("longcat");
  const siliconflow =
    provider === "siliconflow" || base.includes("siliconflow") || model.includes("hy3");
  const mimo = provider === "mimo" || base.includes("xiaomimimo") || model.includes("mimo");
  const openrouterNemo = provider === "openrouter" && model.includes("mistral-nemo");
  const nativeSchema =
    Boolean(extra.supports_json_schema) ||
    rtx ||
    provider === "openai" ||
    provider === "deepseek" ||
    openrouterNemo;
  const compilerGated = longcat || siliconflow || mimo || !nativeSchema;
  return {
    provider: rtx ? "local_private_vllm" : provider,
    model: entry.model,
    endpoint: entry.base_url || "litellm_default",
    auth_mode: entry.api_key ? "bearer_api_key" : rtx ? "none_or_lan_bearer" : "bearer_api_key",
    schema_mode: nativeSchema ? "json_schema" : "json_object_prompt",
    json_repair_mode: compilerGated ? "deterministic_compiler" : "provider_native",
    semantic_verifier_mode: "strict_with_direction_repair",
    concurrency_policy: rtx ? "adaptive_vram_85" : "static_lane_cap",
    failure_backfill_policy: "retry_then_stage",
    supports_json_schema: nativeSchema,
    supports_json_object: !compilerGated,
    disable_thinking: longcat || mimo || provider === "deepseek",
    local_private: rtx,
    managed_vllm: rtx || Boolean(entry.lifecycle_base_url),
    lifecycle_base_url: entry.lifecycle_base_url || "",
    promotion_gate: [
      "json_parse",
      "pydantic_extraction_response",
      "allowed_predicate",
      "required_evidence_phrase",
      "sane_endpoints",
      "semantic_direction_check",
    ],
    notes: [],
  };
}

function providerLabel(card: ProviderCard | null | undefined, entry?: ModelProfileRef | ContractPoolEntry): string {
  const provider = (card?.provider || entry?.provider_preset || "custom").toLowerCase();
  if (provider === "local_private_vllm" || provider === "vllm-rtx" || provider === "vllm") {
    return "Local RTX vLLM";
  }
  if (provider === "siliconflow") return "SiliconFlow";
  if (provider === "openrouter") return "OpenRouter";
  if (provider === "longcat") return "LongCat";
  if (provider === "deepseek") return "DeepSeek";
  if (provider === "openai") return "OpenAI";
  if (provider === "mimo") return "MiMo";
  return provider || "custom";
}

function schemaModeLabel(card: ProviderCard | null | undefined): string {
  if (!card) return "schema unknown";
  if (card.schema_mode === "json_schema") return "json_schema";
  if (card.schema_mode === "json_object_prompt") return "compiler-gated JSON";
  if (card.schema_mode === "json_object") return "json_object";
  return "JSONL repair";
}

function concurrencyLabel(card: ProviderCard | null | undefined, maxConcurrent?: number | null): string {
  if (card?.concurrency_policy === "adaptive_vram_85") {
    return `adaptive 85% VRAM · cap ${maxConcurrent ?? 1}`;
  }
  return `static cap ${maxConcurrent ?? 1}`;
}

function lifecycleStatusLabel(entry: ContractPoolEntry): string | null {
  const status = entry.lifecycle_status;
  if (!status) return null;
  if (!status.ok) return `control DOWN${status.error ? ` · ${status.error}` : ""}`;
  const free =
    typeof status.gpu_vram_free_gb === "number"
      ? ` · ${status.gpu_vram_free_gb.toFixed(1)}GB free`
      : "";
  const rec =
    typeof status.recommended_concurrency === "number"
      ? ` · rec ${status.recommended_concurrency}`
      : "";
  return `${status.ready ? "READY" : "NOT READY"}${free}${rec}`;
}

function formatExtractionPoolEntry(
  entry:
    | ModelProfileRef
    | ExtractionContractResponse["pool"][number],
): string {
  const card = isContractPoolEntry(entry)
    ? entry.provider_card
    : inferDraftProviderCard(entry);
  const lifecycle = isContractPoolEntry(entry) ? lifecycleStatusLabel(entry) : null;
  const pieces = [
    `${providerLabel(card, entry)}: ${entry.model} @${entry.max_concurrent ?? 1}`,
    `schema ${schemaModeLabel(card)}`,
    `repair ${card?.json_repair_mode ?? "unknown"}`,
    `verifier ${card?.semantic_verifier_mode ?? "strict"}`,
    concurrencyLabel(card, entry.max_concurrent),
  ];
  if (lifecycle) pieces.push(lifecycle);
  return pieces.join(" · ");
}

function IngestionModelsSection({
  config,
  onPatch,
  editing,
  corpusId,
}: {
  config: IngestionConfig;
  onPatch: (patch: Partial<IngestionConfig>) => void;
  editing: boolean;
  corpusId?: string;
}) {
  const linked = config.models_linked !== false;
  const summaryPool = config.summary_models ?? [];
  const extractionPool = linked ? summaryPool : (config.extraction_models ?? []);

  // Resolved contract (SAVED state) from the backend truth endpoint.
  const [contract, setContract] = useState<ExtractionContractResponse | null>(null);
  const [contractDown, setContractDown] = useState(false);

  const engine = config.extraction_engine;
  const draft = draftEngine(engine, contract?.engine);
  const draftUsesLegacyLocal = usesLegacyLocalEngine(draft);
  const draftUsesProvider = usesProviderEngine(draft);
  const draftPoolSource = draftUsesProvider
    ? linked
      ? "summary_models"
      : "extraction_models"
    : "none";
  const draftPool =
    draftPoolSource === "summary_models"
      ? summaryPool
      : draftPoolSource === "extraction_models"
        ? (config.extraction_models ?? [])
        : [];
  const draftErrors =
    draftUsesProvider && draftPool.length === 0
      ? [
          linked
            ? "Provider extraction needs at least one Summary model chip."
            : "Provider extraction needs at least one Extraction model chip.",
        ]
      : draft === "local" && !draftPool.some(isRtxModel)
        ? [
            "Local private extraction requires at least one RTX/vLLM extraction chip.",
          ]
      : [];
  const draftChanged =
    !!contract &&
    (contract.engine !== draft ||
      contract.models_linked !== linked ||
      (draftUsesProvider &&
        contract.pool.map(formatExtractionPoolEntry).join("|") !==
          draftPool.map(formatExtractionPoolEntry).join("|")));

  useEffect(() => {
    if (!corpusId) return;
    let gone = false;
    api
      .getExtractionContract(corpusId)
      .then((c) => {
        if (!gone) {
          setContract(c);
          setContractDown(false);
        }
      })
      .catch(() => {
        if (!gone) setContractDown(true);
      });
    return () => {
      gone = true;
    };
  }, [corpusId, config.extraction_engine, config.models_linked]);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-[12px] font-bold tracking-widest text-content-tertiary uppercase">
          Ingestion Models
        </div>
        <div className="text-[10px] text-content-tertiary tracking-wider uppercase">
          {draftUsesProvider ? "provider extraction pool active" : "provider pool hidden"}
        </div>
      </div>

      {/* ── Extraction contract — the deterministic workflow switch ── */}
      <div
        className="border border-border-minimal bg-bg-base/50 px-3 py-2 space-y-1.5"
        data-testid="extraction-contract-block"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[10px] font-bold tracking-widest text-content-tertiary uppercase">
            Extraction workflow
          </span>
          <span className="text-[10px] font-bold tracking-widest text-accent-secondary uppercase">
            {engine === "local_then_enrich"
              ? "LEGACY LOCAL FIRST - RTX fills gaps"
              : draftUsesLegacyLocal && draftUsesProvider
                ? "TRANSITION - legacy sidecar + provider LLM"
                : draftUsesLegacyLocal
                  ? "LEGACY LOCAL SIDECAR"
                : draftUsesProvider
                    ? draft === "local"
                      ? "PRIVATE PROVIDER LLM"
                      : "PROVIDER LLM"
                    : "OFF - vectors only"}
            {engine === "inherit" || engine === undefined ? " (inherited)" : ""}
            {engine === "local_then_cloud" ? " (local->cloud rescue)" : ""}
          </span>
          <span className="ml-auto text-[10px] text-content-tertiary">
            Change via Ingestion workflow above.
          </span>
        </div>
        {linked && draftUsesProvider && (
          <div className="flex flex-wrap items-center gap-2 border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[10px] text-amber-300">
            <span>
              Legacy summary reuse is active: provider extraction borrows Summary
              Models. New workflows use a dedicated Extraction pool.
            </span>
            {editing && (
              <button
                type="button"
                onClick={() => onPatch({ models_linked: false })}
                className="px-2 py-0.5 border border-amber-300/50 text-amber-200 uppercase tracking-widest"
              >
                Detach
              </button>
            )}
          </div>
        )}

        {/* Resolved truth line — what the worker will actually run NOW */}
        <div className="text-[10px] leading-relaxed" data-testid="extraction-contract-truth">
          {!corpusId ? (
            <>
              <span className="text-content-tertiary tracking-wider">
                NEW CONTRACT:{" "}
              </span>
              <span className="text-content-primary font-bold uppercase">{draft}</span>
              {draftUsesLegacyLocal && (
                <span className="text-content-secondary">
                  {" · legacy GLiNER/GLiREL sidecars from Settings"}
                </span>
              )}
              {draftUsesProvider && (
                <span className="text-content-secondary">
                  {" · pool ("}
                  {draftPoolSource === "summary_models"
                    ? "summary, linked"
                    : "extraction"}
                  {"): "}
                  {draftPool.length === 0
                    ? "EMPTY"
                    : draftPool
                        .map(formatExtractionPoolEntry)
                        .join(", ")}
                </span>
              )}
              {draftErrors.map((e, i) => (
                <div key={`de-${i}`} className="text-error">
                  ERROR: {e}
                </div>
              ))}
            </>
          ) : contractDown ? (
            <span className="text-content-tertiary">
              [SAVED_CONTRACT_UNAVAILABLE] — backend build without the contract
              endpoint; showing config only
            </span>
          ) : !contract ? (
            <span className="text-content-tertiary">resolving saved contract…</span>
          ) : (
            <>
              <span className="text-content-tertiary tracking-wider">SAVED CONTRACT: </span>
              <span className="text-content-primary font-bold uppercase">{contract.engine}</span>
              <span className="text-content-tertiary"> ({contract.source})</span>
              {(contract.engine === "legacy_local" ||
                contract.engine === "dual" ||
                contract.engine === "local_then_cloud" ||
                contract.engine === "local_then_enrich") && (
                <span className="text-content-secondary">
                  {" · legacy sidecars: "}
                  {contract.endpoints.filter((e) => e.enabled).length === 0
                    ? "none enabled (env floor)"
                    : contract.endpoints
                        .filter((e) => e.enabled)
                        .map(
                          (e) =>
                            `${e.label || e.url}${
                              e.alive === null ? "" : e.alive ? " UP" : " DOWN"
                            }`,
                        )
                        .join(" · ")}
                </span>
              )}
              {(contract.engine === "local" ||
                contract.engine === "cloud" ||
                contract.engine === "dual" ||
                contract.engine === "local_then_cloud" ||
                contract.engine === "local_then_enrich") && (
                <span className="text-content-secondary">
                  {" · pool ("}
                  {contract.pool_source === "summary_models"
                    ? contract.models_linked
                      ? "summary, linked"
                      : "summary"
                    : "extraction"}
                  {"): "}
                  {contract.pool.length === 0
                    ? "EMPTY"
                    : contract.pool
                        .map(formatExtractionPoolEntry)
                        .join(", ")}
                </span>
              )}
              {contract.errors.map((e, i) => (
                <div key={`ce-${i}`} className="text-error">
                  ERROR: {e}
                </div>
              ))}
              {contract.warnings.map((w, i) => (
                <div key={`cw-${i}`} className="text-amber-300">
                  WARN: {w}
                </div>
              ))}
              {editing && draftChanged && (
                <div className="text-accent-secondary">
                  PENDING AFTER SAVE: {draft.toUpperCase()}
                  {draftUsesProvider
                    ? ` · ${draftPoolSource === "summary_models" ? "summary" : "extraction"} pool: ${
                        draftPool.length === 0
                          ? "EMPTY"
                          : draftPool.map(formatExtractionPoolEntry).join(", ")
                      }`
                    : ""}
                </div>
              )}
              {editing &&
                draftErrors.map((e, i) => (
                  <div key={`pe-${i}`} className="text-error">
                    PENDING ERROR: {e}
                  </div>
                ))}
            </>
          )}
        </div>
      </div>

      <IngestionModelPool
        title="Summary Models (GHOST A)"
        subtitle="Parent-chunk summarization · tasks round-robined across chips"
        value={summaryPool}
        onChange={(next) => onPatch({ summary_models: next })}
        editing={editing}
        testKind="chat"
        testContext={{ corpusId, poolField: "summary_models" }}
      />

      {draftUsesProvider ? (
        <IngestionModelPool
          title="Extraction Models (GHOST B)"
          subtitle={
            linked
              ? "Legacy mode: Summary pool is currently reused for provider extraction"
              : "Provider-card extraction lane · private RTX and API chips share this pool"
          }
          value={extractionPool}
          onChange={(next) => onPatch({ extraction_models: next })}
          editing={editing}
          readOnly={linked}
          readOnlyHint="Detach legacy summary reuse to configure a dedicated extraction pool."
          testKind="chat"
          testContext={{
            corpusId,
            poolField: linked ? "summary_models" : "extraction_models",
          }}
        />
      ) : (
        <div className="border border-border-minimal bg-bg-base/40 px-3 py-2 text-[10px] text-content-tertiary leading-snug">
          Extraction model pool hidden because this workflow does not send Ghost
          B to provider-card LLM chips. Legacy sidecars are configured globally in Settings.
        </div>
      )}
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
