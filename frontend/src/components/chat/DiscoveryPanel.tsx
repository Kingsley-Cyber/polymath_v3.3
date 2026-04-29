// DiscoveryPanel.tsx (Phase 17 Waves 1 + 2)
// Renders structural findings below the ForceGraph canvas in the
// "Agent Query" tab.
//
// Modes:
//   - "knowledge" (default) — Phase 17.1 entity discovery:
//       [BRIDGES] [HUBS] [GAPS]  (entity-level)
//   - "discourse" — Phase 17.2 lexeme co-occurrence:
//       [CLUSTERS] [BRIDGES] [GAPS] [SHAPE]  (term-level)
//
// Each row is clickable — in knowledge mode it highlights an entity node by
// id; in discourse mode it highlights a lexeme node by term.

import { useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  GitBranch,
  Target,
  AlertTriangle,
  Shapes,
  Activity,
  Sparkles,
  ArrowRightCircle,
  Loader2,
  Layers,
} from "lucide-react";
import type {
  GraphBridge,
  GraphHub,
  GraphGap,
  DiscourseBridge,
  DiscourseCluster,
  DiscourseGap,
  DiscourseShape,
  SplitOverlayAlignment,
} from "../../types";

export type DiscoveryMode = "knowledge" | "discourse" | "split";

// Shared analysis (LLM narrative) slot — appears in all three modes.
interface AnalysisSlotProps {
  analysisMarkdown?: string | null;
  isAnalyzing?: boolean;
  onAnalyze?: () => void;
  onAskChat?: () => void;
  canAnalyze?: boolean;
}

interface KnowledgeProps extends AnalysisSlotProps {
  mode?: "knowledge";
  bridges: GraphBridge[];
  hubs: GraphHub[];
  gaps: GraphGap[];
  onSelectEntity?: (entityId: string) => void;
  isLoading?: boolean;
  hasQueried?: boolean;
}

interface DiscourseProps extends AnalysisSlotProps {
  mode: "discourse";
  clusters: DiscourseCluster[];
  bridges: DiscourseBridge[];
  gaps: DiscourseGap[];
  shape: DiscourseShape;
  onSelectLexeme?: (term: string) => void;
  isLoading?: boolean;
  hasQueried?: boolean;
}

interface SplitProps extends AnalysisSlotProps {
  mode: "split";
  alignment: SplitOverlayAlignment | null;
  onSelectEntity?: (entityId: string) => void;
  isLoading?: boolean;
  hasQueried?: boolean;
}

type DiscoveryPanelProps = KnowledgeProps | DiscourseProps | SplitProps;

export function DiscoveryPanel(props: DiscoveryPanelProps) {
  if (props.mode === "discourse") return <DiscourseView {...props} />;
  if (props.mode === "split") return <SplitView {...props} />;
  return <KnowledgeView {...props} />;
}

// ── Knowledge mode (Wave 1) ────────────────────────────────────────────────

function KnowledgeView({
  bridges,
  hubs,
  gaps,
  onSelectEntity,
  isLoading = false,
  hasQueried = false,
  ...rest
}: KnowledgeProps) {
  const [bridgesOpen, setBridgesOpen] = useState(true);
  const [hubsOpen, setHubsOpen] = useState(true);
  const [gapsOpen, setGapsOpen] = useState(true);

  if (isLoading) return <StatusLine text="[ANALYZING TOPOLOGY…]" />;
  if (!hasQueried)
    return (
      <StatusLine text="[IDLE — enter a query above to analyze bridges, hubs, and gaps]" />
    );

  const total = bridges.length + hubs.length + gaps.length;
  if (total === 0)
    return (
      <StatusLine text="[NO STRUCTURAL SIGNAL — query matched entities but found no bridges, hubs, or gaps. Try a broader question.]" />
    );

  return (
    <div className="border-t border-border-minimal bg-bg-surface overflow-y-auto custom-scrollbar text-[11px]">
      <Section
        label="BRIDGES"
        count={bridges.length}
        icon={<GitBranch className="w-3 h-3 text-accent-main" />}
        isOpen={bridgesOpen}
        onToggle={() => setBridgesOpen((v) => !v)}
        emptyCopy="No entity bridges ≥2 seed entities"
      >
        {bridges.map((b) => (
          <Row
            key={b.entity_id}
            primary={b.display_name || b.entity_id}
            secondary={`${b.entity_type} · connects ${b.connected_seed_count} seeds`}
            onClick={() => onSelectEntity?.(b.entity_id)}
          />
        ))}
      </Section>

      <Section
        label="HUBS"
        count={hubs.length}
        icon={<Target className="w-3 h-3 text-accent-secondary" />}
        isOpen={hubsOpen}
        onToggle={() => setHubsOpen((v) => !v)}
        emptyCopy="No high-degree nodes in subgraph"
      >
        {hubs.map((h) => (
          <Row
            key={h.entity_id}
            primary={h.display_name || h.entity_id}
            secondary={`${h.entity_type} · degree ${h.degree}${h.is_seed ? " · seed" : ""}`}
            onClick={() => onSelectEntity?.(h.entity_id)}
          />
        ))}
      </Section>

      <Section
        label="GAPS"
        count={gaps.length}
        icon={<AlertTriangle className="w-3 h-3 text-amber-400" />}
        isOpen={gapsOpen}
        onToggle={() => setGapsOpen((v) => !v)}
        emptyCopy="All seed pairs directly connected"
      >
        {gaps.map((g, i) => (
          <Row
            key={`${g.entity_a_id}-${g.entity_b_id}-${i}`}
            primary={`${g.entity_a_name} ↔ ${g.entity_b_name}`}
            secondary="no direct RELATES_TO edge"
            onClick={() => onSelectEntity?.(g.entity_a_id)}
          />
        ))}
      </Section>

      <AnalysisSection
        analysisMarkdown={rest.analysisMarkdown}
        isAnalyzing={rest.isAnalyzing}
        onAnalyze={rest.onAnalyze}
        onAskChat={rest.onAskChat}
        canAnalyze={rest.canAnalyze ?? bridges.length + hubs.length + gaps.length > 0}
      />
    </div>
  );
}

// ── Discourse mode (Wave 2) ────────────────────────────────────────────────

function DiscourseView({
  clusters,
  bridges,
  gaps,
  shape,
  onSelectLexeme,
  isLoading = false,
  hasQueried = false,
  ...rest
}: DiscourseProps) {
  const [clustersOpen, setClustersOpen] = useState(true);
  const [bridgesOpen, setBridgesOpen] = useState(true);
  const [gapsOpen, setGapsOpen] = useState(true);
  const [shapeOpen, setShapeOpen] = useState(true);

  if (isLoading) return <StatusLine text="[BUILDING DISCOURSE GRAPH…]" />;
  if (!hasQueried)
    return (
      <StatusLine text="[IDLE — switch to Discourse mode to analyze this corpus's vocabulary themes]" />
    );

  const isEmpty =
    shape.shape === "EMPTY" ||
    (clusters.length === 0 && bridges.length === 0 && gaps.length === 0);
  if (isEmpty)
    return (
      <StatusLine text="[NO DISCOURSE SIGNAL — corpus too small or terms too sparse. Try lowering min_cooccur.]" />
    );

  return (
    <div className="border-t border-border-minimal bg-bg-surface overflow-y-auto custom-scrollbar text-[11px]">
      <Section
        label="SHAPE"
        count={1}
        icon={<Shapes className="w-3 h-3 text-accent-main" />}
        isOpen={shapeOpen}
        onToggle={() => setShapeOpen((v) => !v)}
        emptyCopy=""
      >
        <div className="px-2 py-1 flex flex-col gap-1">
          <div className="text-[10px] font-bold tracking-widest text-accent-main uppercase">
            [{shape.shape}]
          </div>
          <div className="text-[10px] text-content-secondary leading-snug">
            {shape.shape_description}
          </div>
          <div className="text-[9px] text-content-tertiary tracking-wider mt-1">
            Gini: {shape.gini_coefficient.toFixed(3)} · Dominant cluster:{" "}
            {shape.dominant_cluster ?? "—"} ·{" "}
            {(shape.dominant_percentage * 100).toFixed(0)}% of edges
          </div>
        </div>
      </Section>

      <Section
        label="CLUSTERS"
        count={clusters.length}
        icon={<Activity className="w-3 h-3 text-accent-secondary" />}
        isOpen={clustersOpen}
        onToggle={() => setClustersOpen((v) => !v)}
        emptyCopy="No discernible clusters — graph too sparse"
      >
        {clusters.map((c) => (
          <Row
            key={c.cluster_id}
            primary={`Cluster ${c.cluster_id} · ${c.size} terms`}
            secondary={c.top_terms.join(", ") || "—"}
            onClick={() => c.top_terms[0] && onSelectLexeme?.(c.top_terms[0])}
          />
        ))}
      </Section>

      <Section
        label="BRIDGES"
        count={bridges.length}
        icon={<GitBranch className="w-3 h-3 text-accent-main" />}
        isOpen={bridgesOpen}
        onToggle={() => setBridgesOpen((v) => !v)}
        emptyCopy="No cross-cluster bridging terms"
      >
        {bridges.map((b) => (
          <Row
            key={b.term}
            primary={b.term}
            secondary={`centrality ${b.centrality.toFixed(3)} · spans clusters ${b.connects_clusters.join(", ")} · deg ${b.degree}`}
            onClick={() => onSelectLexeme?.(b.term)}
          />
        ))}
      </Section>

      <Section
        label="GAPS"
        count={gaps.length}
        icon={<AlertTriangle className="w-3 h-3 text-amber-400" />}
        isOpen={gapsOpen}
        onToggle={() => setGapsOpen((v) => !v)}
        emptyCopy="All cluster pairs healthily bridged"
      >
        {gaps.map((g, i) => (
          <Row
            key={`${g.cluster_a}-${g.cluster_b}-${i}`}
            primary={`Cluster ${g.cluster_a} ↔ Cluster ${g.cluster_b} · ${g.severity}`}
            secondary={g.interpretation}
            onClick={() =>
              g.bridging_words[0] && onSelectLexeme?.(g.bridging_words[0])
            }
          />
        ))}
      </Section>

      <AnalysisSection
        analysisMarkdown={rest.analysisMarkdown}
        isAnalyzing={rest.isAnalyzing}
        onAnalyze={rest.onAnalyze}
        onAskChat={rest.onAskChat}
        canAnalyze={rest.canAnalyze ?? clusters.length > 0}
      />
    </div>
  );
}

// ── Split mode (Wave 3) ────────────────────────────────────────────────────

function SplitView({
  alignment,
  onSelectEntity,
  isLoading = false,
  hasQueried = false,
  ...rest
}: SplitProps) {
  const [alignOpen, setAlignOpen] = useState(true);
  const [absentOpen, setAbsentOpen] = useState(false);

  if (isLoading) return <StatusLine text="[BUILDING SPLIT OVERLAY…]" />;
  if (!hasQueried || !alignment)
    return (
      <StatusLine text="[IDLE — switch to Split mode to align the entity graph with the discourse graph]" />
    );

  const scorePct = (alignment.score * 100).toFixed(1);

  return (
    <div className="border-t border-border-minimal bg-bg-surface overflow-y-auto custom-scrollbar text-[11px]">
      <Section
        label="ALIGNMENT"
        count={1}
        icon={<Layers className="w-3 h-3 text-accent-main" />}
        isOpen={alignOpen}
        onToggle={() => setAlignOpen((v) => !v)}
        emptyCopy=""
      >
        <div className="px-2 py-1 flex flex-col gap-1">
          <div className="text-[10px] font-bold tracking-widest text-accent-main uppercase">
            [{scorePct}% aligned]
          </div>
          <div className="text-[10px] text-content-secondary leading-snug">
            {alignment.intersection_size} of {alignment.union_size} names
            overlap between entity graph and discourse vocabulary.
          </div>
        </div>
      </Section>

      <Section
        label="ENTITY ↔ LEXEME MATCHES"
        count={alignment.entities_present_as_lexemes.length}
        icon={<GitBranch className="w-3 h-3 text-accent-secondary" />}
        isOpen={alignOpen}
        onToggle={() => setAlignOpen((v) => !v)}
        emptyCopy="No entities surface as lexemes in this corpus"
      >
        {alignment.entities_present_as_lexemes.slice(0, 20).map((name) => (
          <Row
            key={`match-${name}`}
            primary={name}
            secondary="entity name present in discourse vocabulary"
            onClick={() => onSelectEntity?.(name)}
          />
        ))}
      </Section>

      <Section
        label="ENTITIES ABSENT FROM DISCOURSE"
        count={alignment.entities_absent_from_lexemes.length}
        icon={<AlertTriangle className="w-3 h-3 text-amber-400" />}
        isOpen={absentOpen}
        onToggle={() => setAbsentOpen((v) => !v)}
        emptyCopy="All entities surface as lexemes — full alignment"
      >
        {alignment.entities_absent_from_lexemes.slice(0, 20).map((name) => (
          <Row
            key={`absent-${name}`}
            primary={name}
            secondary="entity appears in the graph, but not in the discourse vocabulary"
            onClick={() => onSelectEntity?.(name)}
          />
        ))}
      </Section>

      <AnalysisSection
        analysisMarkdown={rest.analysisMarkdown}
        isAnalyzing={rest.isAnalyzing}
        onAnalyze={rest.onAnalyze}
        onAskChat={rest.onAskChat}
        canAnalyze={rest.canAnalyze ?? alignment.union_size > 0}
      />
    </div>
  );
}

// ── Analysis (LLM narrative) ──────────────────────────────────────────────

function AnalysisSection({
  analysisMarkdown,
  isAnalyzing,
  onAnalyze,
  onAskChat,
  canAnalyze = true,
}: AnalysisSlotProps) {
  const [open, setOpen] = useState(true);

  return (
    <div className="border-t border-border-minimal bg-bg-base">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-[9px] font-bold tracking-widest uppercase text-content-secondary hover:bg-bg-surface"
      >
        {open ? (
          <ChevronDown className="w-3 h-3 text-content-tertiary" />
        ) : (
          <ChevronRight className="w-3 h-3 text-content-tertiary" />
        )}
        <Sparkles className="w-3 h-3 text-violet-400" />
        <span>[ANALYSIS]</span>
      </button>
      {open && (
        <div className="px-3 pb-3 flex flex-col gap-2">
          {isAnalyzing ? (
            <div className="text-[10px] text-content-tertiary tracking-widest uppercase flex items-center gap-2 py-2">
              <Loader2 className="w-3 h-3 animate-spin" />
              Narrating topology…
            </div>
          ) : analysisMarkdown ? (
            <div className="text-[11px] text-content-primary leading-relaxed whitespace-pre-wrap">
              {analysisMarkdown}
            </div>
          ) : (
            <div className="text-[10px] text-content-tertiary italic leading-snug py-1">
              Click <span className="text-violet-300">Analyze</span> to generate
              an LLM narrative of this graph's structure. The LLM reads topology
              (hubs, bridges, gaps) — never raw document text.
            </div>
          )}

          <div className="flex items-center gap-2">
            <button
              onClick={onAnalyze}
              disabled={isAnalyzing || !canAnalyze || !onAnalyze}
              className="px-3 py-1 text-[10px] font-bold tracking-widest uppercase border border-violet-500/40 text-violet-200 hover:bg-violet-500/20 disabled:opacity-40 disabled:cursor-not-allowed rounded"
            >
              {analysisMarkdown ? "Re-Analyze" : "Analyze"}
            </button>
            {analysisMarkdown && onAskChat && (
              <button
                onClick={onAskChat}
                className="flex items-center gap-1 px-3 py-1 text-[10px] font-bold tracking-widest uppercase border border-accent-main/60 text-accent-main hover:bg-accent-main/10 rounded"
              >
                <ArrowRightCircle className="w-3 h-3" />
                Ask Chat
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── internals ────────────────────────────────────────────────────────────

function StatusLine({ text }: { text: string }) {
  return (
    <div className="border-t border-border-minimal px-3 py-3 text-[10px] tracking-widest uppercase text-content-tertiary">
      {text}
    </div>
  );
}

interface SectionProps {
  label: string;
  count: number;
  icon: React.ReactNode;
  isOpen: boolean;
  onToggle: () => void;
  emptyCopy: string;
  children: React.ReactNode;
}

function Section({
  label,
  count,
  icon,
  isOpen,
  onToggle,
  emptyCopy,
  children,
}: SectionProps) {
  return (
    <div className="border-b border-border-minimal last:border-b-0">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-[9px] font-bold tracking-widest uppercase text-content-secondary hover:bg-bg-base"
      >
        {isOpen ? (
          <ChevronDown className="w-3 h-3 text-content-tertiary" />
        ) : (
          <ChevronRight className="w-3 h-3 text-content-tertiary" />
        )}
        {icon}
        <span>[{label}: {count}]</span>
      </button>
      {isOpen && (
        <div className="px-2 pb-2">
          {count === 0 ? (
            <div className="px-2 py-1 text-[10px] text-content-tertiary italic">
              {emptyCopy}
            </div>
          ) : (
            children
          )}
        </div>
      )}
    </div>
  );
}

interface RowProps {
  primary: string;
  secondary: string;
  onClick: () => void;
}

function Row({ primary, secondary, onClick }: RowProps) {
  return (
    <button
      onClick={onClick}
      className="w-full flex flex-col items-start gap-0.5 px-2 py-1 text-left border border-transparent hover:border-border-minimal hover:bg-bg-base transition-none"
    >
      <div className="text-[10px] font-bold tracking-wider text-content-primary truncate w-full">
        {primary}
      </div>
      <div className="text-[9px] text-content-tertiary tracking-wider truncate w-full">
        {secondary}
      </div>
    </button>
  );
}
