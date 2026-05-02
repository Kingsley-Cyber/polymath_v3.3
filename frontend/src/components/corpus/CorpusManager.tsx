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
} from "../../types";
import { DEFAULT_INGESTION_CONFIG, inferPreset } from "../../types";
import { CorpusDetail } from "./CorpusDetail";
import { IngestionModelPool } from "../settings/IngestionModelPool";

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
        Preset Mode
      </div>
      <div
        role="radiogroup"
        aria-label="Ingestion preset"
        className="grid grid-cols-4 gap-1.5"
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

  // Create form state
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [newConfig, setNewConfig] = useState<IngestionConfig>(
    DEFAULT_INGESTION_CONFIG,
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
    }
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
      setNewConfig(DEFAULT_INGESTION_CONFIG);
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
    setError(null);
    try {
      await api.deleteCorpus(corpusId);
      setCorpora(corpora.filter((c) => c.corpus_id !== corpusId));
      setSelectedCorpusIds(selectedCorpusIds.filter((id) => id !== corpusId));
      setDeleteConfirmId(null);
      if (expandedId === corpusId) setExpandedId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete corpus");
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
      <div className="fixed inset-0 z-[100] flex items-center justify-center">
        <div
          className="absolute inset-0 bg-bg-base animate-overlay-in opacity-100"
          onClick={() => setSelectedCorpus(null)}
        />
        <div
          className="relative w-full max-w-[1200px] h-[85vh] max-h-[800px] min-h-[500px] bg-[#242424] rounded-2xl shadow-2xl flex flex-col overflow-hidden border border-white/5"
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
    <div className="fixed inset-0 z-[100] flex items-center justify-center">
      <div
        className="absolute inset-0 bg-bg-base animate-overlay-in opacity-100"
        onClick={onClose}
      />
      <div
        className="relative w-full max-w-[1200px] h-[85vh] max-h-[800px] min-h-[500px] bg-[#242424] rounded-2xl shadow-2xl flex flex-col overflow-hidden border border-white/5"
        style={{ fontFamily: "Inter, -apple-system, sans-serif" }}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-white/5 shrink-0">
          <div className="flex items-center gap-2">
            <Database className="w-5 h-5 text-accent-main" />
            <span className="text-[13px] font-semibold text-white">
              Corpus Manager
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              data-testid="create-corpus-btn"
              onClick={() => setShowCreateForm(!showCreateForm)}
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
            <input
              type="text"
              value={newDescription}
              onChange={(e) => setNewDescription(e.target.value)}
              placeholder="description (optional)"
              className="w-full px-2 py-1.5 bg-bg-base border border-border-minimal text-[12px] text-content-primary placeholder:text-content-tertiary focus:outline-none focus:border-accent-main"
            />
            {/* Preset Mode — radio group. Custom reveals the raw toggles. */}
            <PresetModeSelector
              config={newConfig}
              onChange={setNewConfig}
              idPrefix="create"
            />

            {/* Embed dispatch — three-way selector + per-mode credentials */}
            <EmbedSection
              config={newConfig}
              onPatch={(patch) =>
                setNewConfig((prev) => ({ ...prev, ...patch }))
              }
              modalStatus={modalStatus}
            />

            {/* Token Budget — Parent */}
            <div>
              <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase mb-1.5">
                Parent Chunk Tokens
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                <div>
                  <label className="text-[9px] text-content-tertiary tracking-wider">
                    MIN
                  </label>
                  <input
                    type="number"
                    min={100}
                    value={newConfig.parent_chunk_tokens.min_tokens}
                    onChange={(e) =>
                      setNewConfig((prev) => ({
                        ...prev,
                        parent_chunk_tokens: {
                          ...prev.parent_chunk_tokens,
                          min_tokens: parseInt(e.target.value) || 500,
                        },
                      }))
                    }
                    className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                  />
                </div>
                <div>
                  <label className="text-[9px] text-content-tertiary tracking-wider">
                    TARGET
                  </label>
                  <input
                    type="number"
                    min={200}
                    value={newConfig.parent_chunk_tokens.target_tokens}
                    onChange={(e) =>
                      setNewConfig((prev) => ({
                        ...prev,
                        parent_chunk_tokens: {
                          ...prev.parent_chunk_tokens,
                          target_tokens: parseInt(e.target.value) || 1200,
                        },
                      }))
                    }
                    className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                  />
                </div>
                <div>
                  <label className="text-[9px] text-content-tertiary tracking-wider">
                    MAX
                  </label>
                  <input
                    type="number"
                    min={500}
                    value={newConfig.parent_chunk_tokens.max_tokens}
                    onChange={(e) =>
                      setNewConfig((prev) => ({
                        ...prev,
                        parent_chunk_tokens: {
                          ...prev.parent_chunk_tokens,
                          max_tokens: parseInt(e.target.value) || 2000,
                        },
                      }))
                    }
                    className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                  />
                </div>
              </div>
            </div>

            {/* Token Budget — Child */}
            <div>
              <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase mb-1.5">
                Child Chunk Tokens
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                <div>
                  <label className="text-[9px] text-content-tertiary tracking-wider">
                    MIN
                  </label>
                  <input
                    type="number"
                    min={50}
                    value={newConfig.child_chunk_tokens.min_tokens}
                    onChange={(e) =>
                      setNewConfig((prev) => ({
                        ...prev,
                        child_chunk_tokens: {
                          ...prev.child_chunk_tokens,
                          min_tokens: parseInt(e.target.value) || 128,
                        },
                      }))
                    }
                    className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                  />
                </div>
                <div>
                  <label className="text-[9px] text-content-tertiary tracking-wider">
                    TARGET
                  </label>
                  <input
                    type="number"
                    min={100}
                    value={newConfig.child_chunk_tokens.target_tokens}
                    onChange={(e) =>
                      setNewConfig((prev) => ({
                        ...prev,
                        child_chunk_tokens: {
                          ...prev.child_chunk_tokens,
                          target_tokens: parseInt(e.target.value) || 350,
                        },
                      }))
                    }
                    className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                  />
                </div>
                <div>
                  <label className="text-[9px] text-content-tertiary tracking-wider">
                    MAX
                  </label>
                  <input
                    type="number"
                    min={256}
                    value={newConfig.child_chunk_tokens.max_tokens}
                    onChange={(e) =>
                      setNewConfig((prev) => ({
                        ...prev,
                        child_chunk_tokens: {
                          ...prev.child_chunk_tokens,
                          max_tokens: parseInt(e.target.value) || 512,
                        },
                      }))
                    }
                    className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                  />
                </div>
              </div>
            </div>

            {/* Chunking extras */}
            <div className="grid grid-cols-3 gap-1.5">
              <div>
                <label className="text-[9px] text-content-tertiary tracking-wider">
                  OVERLAP
                </label>
                <input
                  type="number"
                  min={0}
                  value={newConfig.chunk_overlap}
                  onChange={(e) =>
                    setNewConfig((prev) => ({
                      ...prev,
                      chunk_overlap: parseInt(e.target.value) || 200,
                    }))
                  }
                  className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                />
              </div>
              <div>
                <label className="text-[9px] text-content-tertiary tracking-wider">
                  CHUNKING
                </label>
                <div
                  className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary"
                  title="Resolved per file after parsing. Children currently use sentence_merge; semantic_split is held back until implemented."
                >
                  AUTO
                </div>
                <div className="mt-1 text-[8px] text-content-tertiary leading-tight">
                  file-type policy · child: sentence_merge
                </div>
              </div>
              <div>
                <label className="text-[9px] text-content-tertiary tracking-wider">
                  MAX SUMMARY
                </label>
                <input
                  type="number"
                  min={50}
                  value={newConfig.max_summary_tokens}
                  onChange={(e) =>
                    setNewConfig((prev) => ({
                      ...prev,
                      max_summary_tokens: parseInt(e.target.value) || 175,
                    }))
                  }
                  className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                />
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

            {/* Confidence threshold sits on its own because it's GHOST-B-specific
                and orthogonal to the model identity. */}
            <div className="grid grid-cols-3 gap-1.5">
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
              <button
                data-testid="corpus-create-submit"
                onClick={handleCreate}
                disabled={!newName.trim() || isCreating}
                className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-bold tracking-widest bg-accent-main text-bg-base border border-accent-main hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-none uppercase"
              >
                {isCreating ? (
                  <Loader2 className="w-3 h-3 animate-spin" />
                ) : (
                  <Check className="w-3 h-3" />
                )}
                {isCreating ? "Creating..." : "Create"}
              </button>
              <button
                onClick={() => {
                  setShowCreateForm(false);
                  setNewName("");
                  setNewDescription("");
                  setNewConfig(DEFAULT_INGESTION_CONFIG);
                }}
                className="px-3 py-1.5 text-[11px] font-bold tracking-widest text-content-tertiary border border-border-minimal hover:border-content-secondary transition-none uppercase"
              >
                Cancel
              </button>
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
                          <span className="flex items-center gap-1">
                            <FileText className="w-3 h-3" />
                            {corpus.doc_count}
                          </span>
                          <span>{corpus.chunk_count} chunks</span>
                        </div>
                      )}

                      {/* Actions */}
                      {!isEditing && (
                        <div className="flex items-center gap-1 transition-none shrink-0">
                          <button
                            data-testid="corpus-browse-btn"
                            onClick={() => setSelectedCorpus(corpus)}
                            className="flex items-center gap-1 px-2 py-1 text-[10px] font-bold tracking-widest text-accent-main border border-accent-main hover:bg-accent-main hover:text-bg-base transition-none uppercase"
                            title="Browse Documents & Upload Files"
                          >
                            <ExternalLink className="w-3 h-3" />
                            <span>Open</span>
                          </button>
                          <button
                            onClick={() => startEdit(corpus)}
                            className="p-1 text-content-tertiary hover:text-accent-main transition-none"
                            title="Edit"
                          >
                            <Edit3 className="w-3 h-3" />
                          </button>
                          {isPendingDelete ? (
                            <div className="flex items-center gap-1">
                              <button
                                onClick={() => handleDelete(corpus.corpus_id)}
                                className="px-1.5 py-0.5 text-[9px] font-bold tracking-widest text-error border border-error hover:bg-error hover:text-bg-base transition-none uppercase"
                              >
                                Confirm
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
                              onClick={() =>
                                setDeleteConfirmId(corpus.corpus_id)
                              }
                              className="p-1 text-content-tertiary hover:text-error transition-none"
                              title="Delete"
                            >
                              <Trash2 className="w-3 h-3" />
                            </button>
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
                          />

                          <IngestionModelsSection
                            config={editConfig}
                            onPatch={(patch) =>
                              setEditConfig((prev) =>
                                prev ? { ...prev, ...patch } : prev,
                              )
                            }
                            editing={true}
                          />

                          <div className="grid grid-cols-3 gap-1.5">
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
                        <div className="grid grid-cols-2 gap-2 text-[11px]">
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
                          <div className="pl-2 grid grid-cols-2 gap-1">
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

function IngestionModelsSection({
  config,
  onPatch,
  editing,
}: {
  config: IngestionConfig;
  onPatch: (patch: Partial<IngestionConfig>) => void;
  editing: boolean;
}) {
  const linked = config.models_linked !== false;
  const summaryPool = config.summary_models ?? [];
  const extractionPool = linked ? summaryPool : (config.extraction_models ?? []);
  const repairPool = config.extraction_repair_models ?? [];

  const applyLocalIngestionStack = () => {
    onPatch({
      models_linked: false,
      graph_extraction_engine: "llm",
      summary_models: DEFAULT_INGESTION_CONFIG.summary_models,
      extraction_models: DEFAULT_INGESTION_CONFIG.extraction_models,
      extraction_repair_models: DEFAULT_INGESTION_CONFIG.extraction_repair_models,
    });
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-[12px] font-bold tracking-widest text-content-tertiary uppercase">
          Ingestion Models
        </div>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={applyLocalIngestionStack}
            disabled={!editing}
            className="text-[10px] px-2 py-1 rounded border border-accent-main/50 text-accent-main hover:bg-accent-main/10 disabled:opacity-40 disabled:cursor-not-allowed"
            title="Use local vLLM LFM2 extraction, LFM2-RAG document summaries, and Gemma repair."
          >
            Local LFM Stack
          </button>
          <label
            className="flex items-center gap-1.5 text-[11px] text-content-secondary tracking-wider cursor-pointer"
            title="ON = Extraction reuses the Summary pool. OFF = two independent pools."
          >
            <input
              type="checkbox"
              checked={linked}
              onChange={(e) => onPatch({ models_linked: e.target.checked })}
              className="accent-accent-main"
            />
            Reuse Summary pool for Extraction
          </label>
        </div>
      </div>

      <IngestionModelPool
        title="Summary Models (GHOST A)"
        subtitle="Parent-chunk summarization · tasks round-robined across chips"
        value={summaryPool}
        onChange={(next) => onPatch({ summary_models: next })}
        editing={editing}
      />

      <IngestionModelPool
        title="Extraction Models (GHOST B)"
        subtitle={
          linked
            ? "Using Summary pool (link toggle above)"
            : "Entity + relation extraction · tasks round-robined across chips"
        }
        value={extractionPool}
        onChange={(next) => onPatch({ extraction_models: next })}
        editing={editing}
        readOnly={linked}
        readOnlyHint="Uncheck 'Reuse Summary pool' to configure Extraction independently."
      />

      <IngestionModelPool
        title="Repair Models (GHOST B)"
        subtitle="JSON/schema recovery only · never used for primary extraction fan-out"
        value={repairPool}
        onChange={(next) => onPatch({ extraction_repair_models: next })}
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
}: {
  config: IngestionConfig;
  onPatch: (patch: Partial<IngestionConfig>) => void;
  modalStatus: ModalStatus | null;
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
      <div role="radiogroup" aria-label="embed_mode" className="grid grid-cols-3 gap-1.5">
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

      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
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
