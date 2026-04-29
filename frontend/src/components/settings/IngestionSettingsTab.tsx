// IngestionSettingsTab.tsx — Read-only display of global ingestion/chunking defaults
// These values serve as defaults when creating new corpora. Per-corpus overrides
// are configured in CorpusManager at creation time and frozen after first ingest.

import { Layers, Info, Copy, Check } from "lucide-react";
import { useState } from "react";
import type { IngestionConfig, TokenBudget } from "../../types";
import { DEFAULT_INGESTION_CONFIG } from "../../types";

// ── Helpers ──────────────────────────────────────────────────────────────

function formatTokenBudget(budget: TokenBudget): string {
  return `${budget.min_tokens} / ${budget.target_tokens} / ${budget.max_tokens}`;
}

function copyToClipboard(text: string): Promise<void> {
  return navigator.clipboard.writeText(text);
}

// ── Read-only field row ──────────────────────────────────────────────────

function ReadOnlyField({
  label,
  value,
  hint,
  mono = false,
}: {
  label: string;
  value: string | number | boolean;
  hint?: string;
  mono?: boolean;
}) {
  const [copied, setCopied] = useState(false);

  const displayValue =
    typeof value === "boolean" ? (value ? "ON" : "OFF") : String(value);

  const handleCopy = async () => {
    await copyToClipboard(displayValue);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="flex items-start justify-between gap-3 py-2.5 border-b border-white/5 last:border-b-0">
      <div className="flex-1 min-w-0">
        <div className="text-[12px] text-gray-400">{label}</div>
        {hint && <div className="text-[10px] text-gray-600 mt-0.5">{hint}</div>}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <span
          className={`text-[13px] ${
            typeof value === "boolean"
              ? value
                ? "text-green-400"
                : "text-gray-500"
              : "text-white"
          } ${mono ? "font-mono" : ""}`}
        >
          {displayValue}
        </span>
        <button
          onClick={handleCopy}
          className="p-0.5 text-gray-600 hover:text-gray-300 transition-colors"
          title="Copy value"
        >
          {copied ? (
            <Check className="w-3 h-3 text-green-400" />
          ) : (
            <Copy className="w-3 h-3" />
          )}
        </button>
      </div>
    </div>
  );
}

// ── Section card ─────────────────────────────────────────────────────────

function SectionCard({
  title,
  icon: Icon,
  iconColor,
  children,
}: {
  title: string;
  icon: typeof Layers;
  iconColor: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
      <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
        <Icon size={16} className={iconColor} /> {title}
      </h3>
      <div className="space-y-0">{children}</div>
    </div>
  );
}

// ── Main component ───────────────────────────────────────────────────────

export function IngestionSettingsTab() {
  const config: IngestionConfig = DEFAULT_INGESTION_CONFIG;

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold text-white mb-2">Ingestion</h2>
        <p className="text-[13px] text-gray-500">
          Global defaults for document ingestion pipeline. These values
          pre-fill the corpus creation form. Per-corpus overrides are frozen
          after first document ingest.
        </p>
      </div>

      {/* Info banner */}
      <div className="flex items-start gap-3 bg-blue-950/20 border border-blue-700/30 rounded-lg px-4 py-3">
        <Info className="w-4 h-4 text-blue-400 mt-0.5 shrink-0" />
        <p className="text-[12px] text-blue-300/80">
          These defaults are read-only. Edit per-corpus settings when creating
          a corpus in Corpus Manager. Changes here require backend API support
          (planned).
        </p>
      </div>

      {/* Embedding */}
      <SectionCard title="Embedding" icon={Layers} iconColor="text-purple-400">
        <ReadOnlyField
          label="Embedding Model"
          value={config.embedding_model}
          mono
        />
        <ReadOnlyField
          label="Dimension"
          value={config.embedding_dimension}
          hint="Changing requires full Qdrant re-index"
        />
        <ReadOnlyField
          label="Model ID"
          value={config.embedding_model_id}
          mono
        />
        <ReadOnlyField
          label="Embed Mode"
          value={config.embed_mode}
          hint="local = sentence-transformers GPU | api = OpenAI-compatible | modal = Modal cloud"
          mono
        />
      </SectionCard>

      {/* Chunking */}
      <SectionCard
        title="Chunking — Auto Policy"
        icon={Layers}
        iconColor="text-cyan-400"
      >
        <ReadOnlyField
          label="Chunking Mode"
          value="AUTO"
          hint="Resolved per file after parsing: headings, token windows, or PDF page groups"
          mono
        />
        <ReadOnlyField
          label="Parent Chunk Tokens (min / target / max)"
          value={formatTokenBudget(config.parent_chunk_tokens)}
          hint="Used by Auto when sections/pages need token-sized parents"
          mono
        />
        <ReadOnlyField
          label="Child Chunk Tokens (min / target / max)"
          value={formatTokenBudget(config.child_chunk_tokens)}
          hint="Auto currently resolves children to sentence-merged passages"
          mono
        />
        <ReadOnlyField
          label="Chunk Overlap"
          value={`${config.chunk_overlap} tokens`}
          hint="Trailing sentences carried to next parent"
        />
        <ReadOnlyField
          label="Max Summary Tokens"
          value={config.max_summary_tokens}
          hint="Token cap per parent summary (GHOST A output)"
        />
        <ReadOnlyField
          label="Child Splitter"
          value="AUTO → sentence_merge"
          hint="semantic_split remains disabled until the backend splitter is fully implemented"
          mono
        />
      </SectionCard>

      {/* GHOST A — Summary Pool */}
      <SectionCard
        title="GHOST A — Summary Pool"
        icon={Layers}
        iconColor="text-amber-400"
      >
        <ReadOnlyField
          label="Summary Models"
          value={
            (config.summary_models ?? [])
              .map((m) => m.model)
              .join(", ") || "—"
          }
          hint="Round-robin pool. Edit per-corpus in Corpus Manager."
          mono
        />
        <ReadOnlyField
          label="Total Concurrency (sum)"
          value={(config.summary_models ?? [])
            .reduce((sum, m) => sum + (m.max_concurrent || 1), 0)}
          hint="Sum of each entry's max_concurrent"
        />
        <ReadOnlyField
          label="Max Tokens per Summary"
          value={config.max_summary_tokens}
        />
        <ReadOnlyField
          label="Summarization Enabled"
          value={config.chunk_summarization}
          hint="Run GHOST A: summarize parents + embed summaries"
        />
      </SectionCard>

      {/* GHOST B — Extraction Pool */}
      <SectionCard
        title="GHOST B — Extraction Pool"
        icon={Layers}
        iconColor="text-rose-400"
      >
        <ReadOnlyField
          label="Extraction Models"
          value={
            config.models_linked
              ? "(using Summary pool — models_linked=true)"
              : (config.extraction_models ?? [])
                  .map((m) => m.model)
                  .join(", ") || "—"
          }
          hint="Round-robin pool. Edit per-corpus in Corpus Manager."
          mono
        />
        <ReadOnlyField
          label="Total Concurrency (sum)"
          value={(config.models_linked
            ? config.summary_models ?? []
            : config.extraction_models ?? []
          ).reduce((sum, m) => sum + (m.max_concurrent || 1), 0)}
          hint="Sum of each entry's max_concurrent"
        />
        <ReadOnlyField
          label="Entity Confidence Threshold"
          value={config.entity_confidence_threshold}
          hint="Min confidence to keep extracted entity/relation"
        />
        <ReadOnlyField
          label="Neo4j Enabled"
          value={config.use_neo4j}
          hint="Run GHOST B: entity extraction + Neo4j graph"
        />
      </SectionCard>

      {/* Schema (Ontology-Lite) — Phase 14 */}
      <SectionCard
        title="Schema (Ontology-Lite)"
        icon={Layers}
        iconColor="text-fuchsia-400"
      >
        <ReadOnlyField
          label="Entity Types"
          value={
            config.entity_schema && config.entity_schema.length > 0
              ? config.entity_schema.join(", ")
              : "(open — default 4-bucket enum)"
          }
          hint="LLM creates instances freely under these types. 'other' is implicit fallback."
          mono
        />
        <ReadOnlyField
          label="Relation Predicates"
          value={
            config.relation_schema && config.relation_schema.length > 0
              ? config.relation_schema.join(", ")
              : "(open — free-form predicates)"
          }
          hint="'related_to' is implicit fallback."
          mono
        />
        <ReadOnlyField
          label="Strict Mode"
          value={config.schema_strict ?? "soft"}
          hint="soft = out-of-schema entries remap to sentinels (universal schema is always 'soft')."
          mono
        />
      </SectionCard>

      {/* Qdrant Targets */}
      <SectionCard
        title="Qdrant Targets"
        icon={Layers}
        iconColor="text-green-400"
      >
        <ReadOnlyField
          label="Target Collections"
          value={config.target_qdrant_collections.join(", ")}
          hint="Collections written during ingest: naive | hrag | graph"
          mono
        />
      </SectionCard>
    </div>
  );
}
