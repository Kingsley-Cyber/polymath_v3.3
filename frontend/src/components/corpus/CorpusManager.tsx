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
import { useSettingsStore } from "../../stores/settingsStore";
import * as api from "../../lib/api";
import type {
  CorpusResponse,
  CorpusCreate,
  IngestionConfig,
  EmbedMode,
  ChildChunkAlgorithm,
  SchemaStrictMode,
} from "../../types";
import { PRESET_MODES, DEFAULT_INGESTION_CONFIG } from "../../types";
import { CorpusDetail } from "./CorpusDetail";
import { IngestionModelPool } from "../settings/IngestionModelPool";
import { ModelsTab } from "../settings/ModelsTab";

interface CorpusManagerProps {
  isOpen: boolean;
  onClose: () => void;
}

// DEFAULT_INGESTION_CONFIG imported from ../../types (complete version with all IngestionConfig fields)

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

  // Tab: "corpora" (list + create) or "models" (Ollama + Modal deploy)
  const [activeTab, setActiveTab] = useState<"corpora" | "models">("corpora");

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
    setEditConfig(
      corpus.default_ingestion_config
        ? (JSON.parse(JSON.stringify(corpus.default_ingestion_config)) as IngestionConfig)
        : null,
    );
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
            {activeTab === "corpora" && (
              <button
                data-testid="create-corpus-btn"
                onClick={() => setShowCreateForm(!showCreateForm)}
                className="flex items-center gap-2 px-3 py-1.5 text-[12px] font-medium text-accent-main border border-accent-main hover:bg-accent-main hover:text-bg-base transition-colors"
              >
                <Plus className="w-4 h-4" />
                <span>New Corpus</span>
              </button>
            )}
            <button
              onClick={onClose}
              className="p-1.5 text-gray-400 hover:text-white transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Tab bar */}
        <div className="flex items-center gap-1 px-4 border-b border-white/5 shrink-0 bg-[#1f1f1f]">
          {(
            [
              { id: "corpora" as const, label: "Corpora" },
              { id: "models" as const, label: "Models" },
            ]
          ).map((t) => (
            <button
              key={t.id}
              onClick={() => setActiveTab(t.id)}
              className={`px-3 py-2 text-[11px] font-bold tracking-widest uppercase border-b-2 transition-colors ${
                activeTab === t.id
                  ? "border-accent-main text-accent-main"
                  : "border-transparent text-content-tertiary hover:text-content-secondary"
              }`}
            >
              {t.label}
            </button>
          ))}
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

        {/* Models tab body — Ollama manager + Modal deploy panel. */}
        {activeTab === "models" && (
          <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar p-4">
            <ModelsTab />
          </div>
        )}

        {/* Create Form — claims the full remaining modal body when open.
            Phase 19.3: the form grew large (IngestionModelsSection + schema
            tooltips) so it takes the whole body via flex-1 and the corpus
            list is hidden to avoid a squished dual-scroll region. */}
        {activeTab === "corpora" && showCreateForm && (
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
            {/* Ingestion Config Toggles */}
            <div className="flex flex-wrap gap-3">
              <label className="flex items-center gap-1.5 text-[11px] text-content-secondary tracking-wider">
                <input
                  type="checkbox"
                  checked={newConfig.use_neo4j}
                  onChange={(e) =>
                    setNewConfig({ ...newConfig, use_neo4j: e.target.checked })
                  }
                  className="accent-accent-main"
                />
                use_neo4j
              </label>
              <label className="flex items-center gap-1.5 text-[11px] text-content-secondary tracking-wider">
                <input
                  type="checkbox"
                  checked={newConfig.chunk_summarization}
                  onChange={(e) =>
                    setNewConfig({
                      ...newConfig,
                      chunk_summarization: e.target.checked,
                    })
                  }
                  className="accent-accent-main"
                />
                chunk_summarization
              </label>
            </div>

            {/* Embed mode radio (Phase 14.8) — Modal is primary, local is fallback */}
            <EmbedModeRadio
              value={newConfig.embed_mode ?? "modal_tei"}
              onChange={(m) => setNewConfig({ ...newConfig, embed_mode: m })}
            />

            {/* Preset Modes */}
            <div>
              <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase mb-1.5">
                Preset Mode
              </div>
              <div className="flex gap-1.5">
                {Object.entries(PRESET_MODES).map(([key, preset]) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() =>
                      setNewConfig((prev) => ({
                        ...prev,
                        use_neo4j: preset.use_neo4j,
                        chunk_summarization: preset.chunk_summarization,
                        target_qdrant_collections: [
                          ...preset.target_qdrant_collections,
                        ],
                      }))
                    }
                    className="flex-1 px-2 py-1.5 text-[9px] font-bold tracking-widest uppercase border border-border-minimal text-content-secondary hover:border-accent-main hover:text-accent-main transition-none"
                  >
                    {preset.label}
                  </button>
                ))}
              </div>
            </div>

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
                  ALGORITHM
                </label>
                <select
                  value={newConfig.child_chunk_algorithm}
                  onChange={(e) =>
                    setNewConfig((prev) => ({
                      ...prev,
                      child_chunk_algorithm: e.target
                        .value as ChildChunkAlgorithm,
                    }))
                  }
                  className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main"
                >
                  <option value="sentence_merge">sentence_merge</option>
                  <option value="semantic_split">semantic_split</option>
                </select>
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

            {/* Schema (Ontology-Lite) — Phase 14 */}
            <div>
              <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase mb-1.5">
                Schema (Ontology-Lite)
              </div>
              <div className="text-[9px] text-content-tertiary/70 mb-1.5 leading-relaxed space-y-1">
                <p>
                  Optional. Leave both empty for open extraction. Populate to
                  constrain GHOST B to your vocabulary (e.g. military units,
                  legal concepts). LLM creates instances freely under your
                  types.
                </p>
                <p className="text-amber-400/70">
                  ⓘ{" "}
                  <span className="font-semibold">'other'</span> /{" "}
                  <span className="font-semibold">'related_to'</span> are
                  always implicit fallbacks — cannot be removed.
                </p>
                <p className="text-amber-400/70">
                  ⓘ Schema changes do NOT backfill old documents. Only future
                  ingests use the new pool. Re-ingest a doc (clear
                  write_state.neo4j_written) to re-extract.
                </p>
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                <div>
                  <label className="text-[9px] text-content-tertiary tracking-wider">
                    ENTITY TYPES (one per line)
                  </label>
                  <textarea
                    rows={4}
                    value={(newConfig.entity_schema ?? []).join("\n")}
                    placeholder={"Person\nUnit\nEquipment"}
                    onChange={(e) => {
                      const lines = e.target.value
                        .split("\n")
                        .map((s) => s.trim())
                        .filter(Boolean);
                      setNewConfig((prev) => ({
                        ...prev,
                        entity_schema: lines.length > 0 ? lines : null,
                      }));
                    }}
                    className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary font-mono focus:outline-none focus:border-accent-main resize-y"
                  />
                </div>
                <div>
                  <label className="text-[9px] text-content-tertiary tracking-wider">
                    RELATION PREDICATES (one per line)
                  </label>
                  <textarea
                    rows={4}
                    value={(newConfig.relation_schema ?? []).join("\n")}
                    placeholder={"enhances\ndepends_on\nassigned_to"}
                    onChange={(e) => {
                      const lines = e.target.value
                        .split("\n")
                        .map((s) => s.trim())
                        .filter(Boolean);
                      setNewConfig((prev) => ({
                        ...prev,
                        relation_schema: lines.length > 0 ? lines : null,
                      }));
                    }}
                    className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary font-mono focus:outline-none focus:border-accent-main resize-y"
                  />
                </div>
              </div>
              <div className="mt-1.5">
                <label className="text-[9px] text-content-tertiary tracking-wider">
                  STRICT MODE
                </label>
                <select
                  value={newConfig.schema_strict}
                  disabled={
                    !newConfig.entity_schema && !newConfig.relation_schema
                  }
                  onChange={(e) =>
                    setNewConfig((prev) => ({
                      ...prev,
                      schema_strict: e.target.value as SchemaStrictMode,
                    }))
                  }
                  className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <option value="off">
                    off — schema is a hint, no enforcement
                  </option>
                  <option value="soft">
                    soft — remap unknowns to sentinels (recommended)
                  </option>
                  <option value="hard">hard — drop unknowns entirely</option>
                </select>
                <div className="text-[9px] text-content-tertiary/70 mt-0.5 leading-relaxed">
                  {!newConfig.entity_schema && !newConfig.relation_schema
                    ? "Disabled — populate at least one schema above to enable."
                    : "soft preserves edges (vague but not lost). hard drops them."}
                </div>
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
            can use the full body height. Also hidden on the Models tab. */}
        <div
          className={`${activeTab !== "corpora" || showCreateForm ? "hidden" : "flex-1"} overflow-y-auto custom-scrollbar`}
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

                        {/* Ingestion Models pools (Phase 19.3) */}
                        <IngestionModelsSection
                          config={editConfig}
                          onPatch={(patch) =>
                            setEditConfig((prev) =>
                              prev ? { ...prev, ...patch } : prev,
                            )
                          }
                          editing={true}
                        />

                        {/* Schema (Ontology-Lite) */}
                        <div>
                          <div className="text-[11px] font-bold tracking-widest text-content-tertiary uppercase mb-1.5">
                            Schema (Ontology-Lite)
                          </div>
                          <div className="grid grid-cols-2 gap-1.5">
                            <div>
                              <label className="text-[9px] text-content-tertiary tracking-wider">
                                ENTITY TYPES (one per line)
                              </label>
                              <textarea
                                rows={4}
                                value={(editConfig.entity_schema ?? []).join("\n")}
                                placeholder={"Person\nUnit\nEquipment"}
                                onChange={(e) => {
                                  const lines = e.target.value
                                    .split("\n")
                                    .map((s) => s.trim())
                                    .filter(Boolean);
                                  setEditConfig((prev) =>
                                    prev
                                      ? {
                                          ...prev,
                                          entity_schema:
                                            lines.length > 0 ? lines : null,
                                        }
                                      : prev,
                                  );
                                }}
                                className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary font-mono focus:outline-none focus:border-accent-main resize-y"
                              />
                            </div>
                            <div>
                              <label className="text-[9px] text-content-tertiary tracking-wider">
                                RELATION PREDICATES (one per line)
                              </label>
                              <textarea
                                rows={4}
                                value={(editConfig.relation_schema ?? []).join("\n")}
                                placeholder={"enhances\ndepends_on\nassigned_to"}
                                onChange={(e) => {
                                  const lines = e.target.value
                                    .split("\n")
                                    .map((s) => s.trim())
                                    .filter(Boolean);
                                  setEditConfig((prev) =>
                                    prev
                                      ? {
                                          ...prev,
                                          relation_schema:
                                            lines.length > 0 ? lines : null,
                                        }
                                      : prev,
                                  );
                                }}
                                className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary font-mono focus:outline-none focus:border-accent-main resize-y"
                              />
                            </div>
                          </div>
                          <div className="mt-1.5">
                            <label className="text-[9px] text-content-tertiary tracking-wider">
                              STRICT MODE
                            </label>
                            <select
                              value={editConfig.schema_strict}
                              disabled={
                                !editConfig.entity_schema &&
                                !editConfig.relation_schema
                              }
                              onChange={(e) =>
                                setEditConfig((prev) =>
                                  prev
                                    ? {
                                        ...prev,
                                        schema_strict: e.target
                                          .value as SchemaStrictMode,
                                      }
                                    : prev,
                                )
                              }
                              className="w-full px-2 py-1 bg-bg-base border border-border-minimal text-[12px] text-content-primary focus:outline-none focus:border-accent-main disabled:opacity-40 disabled:cursor-not-allowed"
                            >
                              <option value="off">
                                off — schema is a hint, no enforcement
                              </option>
                              <option value="soft">
                                soft — remap unknowns to sentinels (recommended)
                              </option>
                              <option value="hard">
                                hard — drop unknowns entirely
                              </option>
                            </select>
                          </div>
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

        {/* Footer — corpora tab only */}
        {activeTab === "corpora" && (
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
        )}
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

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-[12px] font-bold tracking-widest text-content-tertiary uppercase">
          Ingestion Models
        </div>
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
    </div>
  );
}

// ============================================================================
// EmbedModeRadio (Phase 14.8)
// ============================================================================

function EmbedModeRadio({
  value,
  onChange,
}: {
  value: EmbedMode;
  onChange: (m: EmbedMode) => void;
}) {
  const modalEnabled = useSettingsStore((s) => s.modalEnabled);

  return (
    <div className="flex flex-col gap-1.5">
      <div className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary">
        embed_mode
      </div>
      <div className="flex gap-3">
        <label
          className={`flex items-center gap-1.5 text-[11px] tracking-wider cursor-pointer ${
            !modalEnabled ? "opacity-50 cursor-not-allowed" : ""
          }`}
          title={
            modalEnabled
              ? "Primary production path — cloud GPU ingestion"
              : "Modal disabled — set MODAL_ENABLED=true in .env"
          }
        >
          <input
            type="radio"
            name="embed_mode"
            value="modal_tei"
            checked={value === "modal_tei"}
            disabled={!modalEnabled}
            onChange={() => onChange("modal_tei")}
            className="accent-accent-main"
          />
          <span className="text-content-secondary">
            modal_tei (cloud, primary)
          </span>
        </label>
        <label className="flex items-center gap-1.5 text-[11px] tracking-wider cursor-pointer">
          <input
            type="radio"
            name="embed_mode"
            value="local_st"
            checked={value === "local_st"}
            onChange={() => onChange("local_st")}
            className="accent-accent-main"
          />
          <span className="text-content-secondary">local_st (fallback)</span>
        </label>
      </div>
      {!modalEnabled && value === "modal_tei" && (
        <div className="text-[11px] text-amber-400">
          Modal not configured — ingestion will auto-fallback to local_st.
        </div>
      )}
    </div>
  );
}
