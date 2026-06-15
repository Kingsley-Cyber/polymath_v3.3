/**
 * GraphViewer — orchestration layer over `useSigma` (hook port of
 * GitNexus's useSigma.ts) and `polymath-graph-adapter` (Polymath payload
 * → graphology). All rendering / physics / reducer logic lives in those
 * two modules; this file owns:
 *
 *   • Multi-corpus data fetch (Brain View domains/books, Query View)
 *   • Cache-warming poll
 *   • UI chrome: corpus pill stats, color/view toggles, breadcrumb,
 *     hover tooltip, selection bar, controls cluster
 *   • Drill stack management (concept community drill, book drill)
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Loader2,
  Maximize2,
  Pause,
  Play,
  ZoomIn,
  ZoomOut,
} from "lucide-react";

import * as api from "../../lib/api";
import { useSigma } from "../../hooks/useSigma";
import { useSettingsStore } from "../../stores/settingsStore";
import {
  polymathToGraphology,
  type ColorMode,
} from "../../lib/polymath-graph-adapter";
import { fingerprintGraphQuery } from "../../lib/query-fingerprint";
import { cleanBookLabel } from "../../lib/label-utils";
import {
  BrainViewDashboard,
  type DashboardTab,
  type GraphProgressStep,
} from "./BrainViewDashboard";
import { GalaxyBackground } from "./GalaxyBackground";
import type { GraphSynthesisMode } from "../../types/discover";

// Pt 4 polish: adaptive top-N forceLabel. At 16 books, hardcoded N=20
// meant every label rendered every frame → overlap storm. The new
// formula scales N with the corpus size so the canvas always has
// breathing room: ~30% of books get the forced label, clamped to
// [3, 24]. A 16-book brain view labels ~5 anchors; 100 books → 24;
// 1000+ → 24 too.
function adaptiveTopN(total: number): number {
  return Math.min(24, Math.max(3, Math.ceil(total * 0.3)));
}

// ─── Types ────────────────────────────────────────────────────────────────

export type GraphViewerMode = "brain" | "query";

interface GraphViewerProps {
  mode: GraphViewerMode;
  corpusIds: string[];
  query?: string;
  onRerun?: () => void;
  onQueryPhaseChange?: (phase: "idle" | "loading" | "ready" | "error") => void;
  /** Pt 7: callback fired when the user picks a refined chip / entity in
   *  the Graph Query tab. Parent typically closes the modal and loads
   *  the text into the chat input. */
  onSendToChat?: (text: string) => void;
}

// Books-as-clusters drill: we only ever drill into a book (no concept-
// community drill anymore — that was the domains mode that's been retired).
type DrillFrame = {
  docId: string;
  label: string;
};

type GraphPayload = { nodes: any[]; links: any[] };

type GraphRunMode = "new" | "followup";

type GraphTurnContext = {
  query: string;
  coreIdea: string;
  seedNames: string[];
  fileNames: string[];
  evidenceFacets: string[];
};

type GraphQueryProgressStage =
  | "idle"
  | "querying"
  | "following"
  | "analyzing"
  | "packing"
  | "synthesizing"
  | "done"
  | "subgraph_error"
  | "synthesis_error_after_map";

type QuestionGraphProgressStage =
  | "idle"
  | "querying"
  | "done"
  | "error";

const GRAPH_QUERY_PROGRESS_FOLLOWING_MS = 3000;
const GRAPH_QUERY_PROGRESS_ANALYZING_MS = 6500;
const GRAPH_QUERY_PROGRESS_PACKING_MS = 10000;
const GRAPH_QUERY_PROGRESS_SYNTHESIS_MS = 1800;

const wait = (ms: number) =>
  new Promise<void>((resolve) => window.setTimeout(resolve, Math.max(0, ms)));

function cleanContextText(value: string, max = 260): string {
  return value
    .replace(/[`*_#[\]()]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, max);
}

function uniqueCompact(values: string[], maxItems: number, maxChars = 80): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of values) {
    const cleaned = cleanContextText(String(raw || ""), maxChars);
    if (!cleaned) continue;
    const key = cleaned.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(cleaned);
    if (out.length >= maxItems) break;
  }
  return out;
}

function extractCoreIdea(markdown: string, fallback = ""): string {
  const coreMatch = markdown.match(/\*\*\s*Core idea:\s*\*\*\s*([^\n]+)/i);
  if (coreMatch?.[1]) return cleanContextText(coreMatch[1], 320);

  const paragraphs = markdown
    .split(/\n{2,}/)
    .map((p) => p.replace(/^#+\s+/gm, "").trim())
    .filter((p) => p && !/^[-*]\s/.test(p));
  const firstParagraph = paragraphs.find((p) => p.length > 80) || paragraphs[0] || "";
  return cleanContextText(firstParagraph || fallback, 320);
}

function buildFollowUpSynthesisQuery(rawQuery: string, context: GraphTurnContext): string {
  const lines = [
    `Follow-up question: ${cleanContextText(rawQuery, 420)}`,
    `Prior question: ${cleanContextText(context.query, 360)}`,
  ];
  if (context.coreIdea) lines.push(`Prior core idea: ${context.coreIdea}`);
  if (context.seedNames.length) lines.push(`Seed concepts: ${context.seedNames.join(", ")}`);
  if (context.evidenceFacets.length) {
    lines.push(`Evidence facets: ${context.evidenceFacets.join(", ")}`);
  }
  if (context.fileNames.length) {
    lines.push(`Prior files, for continuity only: ${context.fileNames.join("; ")}`);
  }
  lines.push(
    "Resolve words like this, that idea, and the previous direction against the prior turn, then build a fresh source-grounded answer for the follow-up.",
  );
  return lines.join("\n").slice(0, 1800);
}

function graphQueryStepLabels(mode: GraphSynthesisMode): string[] {
  if (mode === "nuance") {
    return [
      "Reading your question and finding the concepts under tension.",
      "Following the nearby relationships that complicate the simple answer.",
      "Looking for bridges, gaps, contradictions, and edge cases.",
      "Packing the evidence that shows tradeoffs and alternate framings.",
      "Synthesizing the nuanced interpretation.",
    ];
  }
  if (mode === "ideation") {
    return [
      "Reading your question and finding the buildable ingredients.",
      "Following nearby concepts that could combine into something useful.",
      "Looking for bridges, gaps, and unexpected pairings.",
      "Packing the strongest material for idea generation.",
      "Synthesizing build ideas from the graph.",
    ];
  }
  if (mode === "gap") {
    return [
      "Reading your question and finding the concepts that should connect.",
      "Tracing which neighbors are linked — and which are conspicuously not.",
      "Ranking candidate gaps, fragile bridges, and weak links by structural signal.",
      "Packing the absence map plus the evidence that grounds it.",
      "Synthesizing what the corpus does not yet connect.",
    ];
  }
  return [
    "Reading your question and spotting the main ideas.",
    "Following the evidence neighborhood around those ideas.",
    "Looking for bridges, hubs, and missing links that may affect the answer.",
    "Packing the strongest source-backed evidence.",
    "Synthesizing a grounded research answer.",
  ];
}

function graphQueryProgressSteps(
  mode: GraphSynthesisMode,
  stage: GraphQueryProgressStage,
  detail?: string | null,
): GraphProgressStep[] {
  const labels = graphQueryStepLabels(mode);
  if (stage === "idle") {
    return labels.map((label, index) => ({
      id: `graph-${index}`,
      label,
      status: "pending",
    }));
  }

  const doneThrough =
    stage === "querying" || stage === "subgraph_error"
      ? -1
      : stage === "following"
        ? 0
      : stage === "analyzing"
        ? 1
      : stage === "packing"
        ? 2
      : stage === "synthesizing" || stage === "synthesis_error_after_map"
        ? 3
        : 4;
  const runningIndex =
    stage === "querying"
      ? 0
      : stage === "following"
        ? 1
        : stage === "analyzing"
          ? 2
      : stage === "packing"
        ? 3
        : stage === "synthesizing"
          ? 4
          : -1;
  const errorIndex =
    stage === "subgraph_error"
      ? 0
      : stage === "synthesis_error_after_map"
        ? 4
        : -1;

  return labels.map((label, index) => ({
    id: `graph-${index}`,
    label:
      stage === "synthesis_error_after_map" && index === 4
        ? "The map loaded, but the synthesis model could not be reached."
        : label,
    status:
      index === errorIndex
        ? "error"
        : index === runningIndex
          ? "running"
          : index <= doneThrough
            ? "done"
            : "pending",
    detail: index === errorIndex ? detail || undefined : undefined,
  }));
}

function questionGraphProgressSteps(
  stage: QuestionGraphProgressStage,
  detail?: string | null,
): GraphProgressStep[] {
  const labels = [
    "Sketching a quick map from your question.",
    "Matching your words to concepts in the corpus.",
  ];
  if (stage === "idle") {
    return labels.map((label, index) => ({
      id: `question-graph-${index}`,
      label,
      status: "pending",
    }));
  }

  return labels.map((label, index) => ({
    id: `question-graph-${index}`,
    label,
    status:
      stage === "error" && index === 0
        ? "error"
        : stage === "querying" && index === 0
          ? "running"
          : stage === "done"
            ? "done"
            : "pending",
    detail: stage === "error" && index === 0 ? detail || undefined : undefined,
  }));
}

const QUERY_VISIBLE_NODE_LIMIT = 420;
const QUERY_VISIBLE_EDGE_LIMIT = 950;
const QUERY_MIN_LINK_DENSITY = 0.55;
const QUERY_SCAFFOLD_EDGE_LIMIT = 90;

function graphEndpointId(value: unknown): string {
  if (value && typeof value === "object") {
    const obj = value as { id?: unknown; key?: unknown };
    return String(obj.id ?? obj.key ?? "");
  }
  return String(value ?? "");
}

function graphLinkWeight(link: any): number {
  const weight = Number(link?.weight ?? link?.shared_entities ?? 1);
  const confidence = Number(link?.confidence ?? 0.5);
  return (Number.isFinite(weight) ? weight : 1) +
    (Number.isFinite(confidence) ? confidence : 0.5);
}

function graphPairKey(source: string, target: string): string {
  return source < target ? `${source}::${target}` : `${target}::${source}`;
}

function nodeDisplayText(node: any): string {
  return String(
    node?.display_name ||
      node?.label ||
      node?.name ||
      node?.id ||
      "",
  );
}

function nodeTerms(node: any): Set<string> {
  const text = nodeDisplayText(node).toLowerCase();
  const terms = new Set<string>();
  for (const match of text.matchAll(/[a-z0-9][a-z0-9.+#-]*/g)) {
    const value = match[0];
    if (value.length >= 3) terms.add(value);
  }
  return terms;
}

function queryNodeScore(
  node: any,
  seedIds: Set<string>,
  hubIds: Set<string>,
  bridgeIds: Set<string>,
): number {
  const id = String(node.id);
  const mentions = Number(node.mention_count ?? node.total_mentions ?? 1);
  let score = 0;
  if (seedIds.has(id)) score += 900;
  if (hubIds.has(id)) score += 650;
  if (bridgeIds.has(id)) score += 600;
  if (node.is_working_entity) score += 180;
  if (node.pagerank_score) score += Number(node.pagerank_score) * 250;
  if (Number.isFinite(mentions)) score += Math.log2(mentions + 1) * 24;
  return score;
}

function ensureQueryScaffoldLinks(
  payload: GraphPayload,
  seedIds: Set<string>,
  hubIds: Set<string>,
  bridgeIds: Set<string>,
): GraphPayload {
  const nodes = payload.nodes || [];
  if (nodes.length < 2) return payload;

  const nodesById = new Map<string, any>();
  for (const node of nodes) nodesById.set(String(node.id), node);

  const usableLinks = (payload.links || []).filter((link) => {
    const s = graphEndpointId(link.source);
    const t = graphEndpointId(link.target);
    return s && t && s !== t && nodesById.has(s) && nodesById.has(t);
  });

  const targetLinkCount = Math.min(
    nodes.length - 1,
    Math.floor(nodes.length * QUERY_MIN_LINK_DENSITY),
  );
  if (usableLinks.length >= targetLinkCount) {
    return { nodes, links: usableLinks };
  }

  const existing = new Set<string>();
  for (const link of usableLinks) {
    existing.add(graphPairKey(graphEndpointId(link.source), graphEndpointId(link.target)));
  }

  const ranked = [...nodes].sort(
    (a, b) =>
      queryNodeScore(b, seedIds, hubIds, bridgeIds) -
        queryNodeScore(a, seedIds, hubIds, bridgeIds) ||
      nodeDisplayText(a).localeCompare(nodeDisplayText(b)),
  );

  const anchorIds = ranked
    .filter((node) => {
      const id = String(node.id);
      return seedIds.has(id) || hubIds.has(id) || bridgeIds.has(id);
    })
    .map((node) => String(node.id));
  const anchors = (anchorIds.length ? anchorIds : ranked.slice(0, 6).map((n) => String(n.id)))
    .filter((id, index, arr) => Boolean(id) && arr.indexOf(id) === index);
  if (anchors.length === 0) return { nodes, links: usableLinks };

  const termCache = new Map<string, Set<string>>();
  const termsFor = (id: string) => {
    if (!termCache.has(id)) termCache.set(id, nodeTerms(nodesById.get(id)));
    return termCache.get(id) || new Set<string>();
  };
  const overlapScore = (a: string, b: string) => {
    const aTerms = termsFor(a);
    const bTerms = termsFor(b);
    let overlap = 0;
    for (const term of aTerms) {
      if (bTerms.has(term)) overlap += 1;
    }
    return overlap;
  };

  const scaffold: any[] = [];
  const addScaffold = (source: string, target: string, weight: number) => {
    if (source === target || !nodesById.has(source) || !nodesById.has(target)) return;
    const key = graphPairKey(source, target);
    if (existing.has(key)) return;
    if (scaffold.length >= QUERY_SCAFFOLD_EDGE_LIMIT) return;
    existing.add(key);
    scaffold.push({
      source,
      target,
      predicate: "related_to",
      relation_family: "WeakAssociation",
      confidence: 0.25,
      weight,
      visual_scaffold: true,
    });
  };

  for (let i = 0; i < anchors.length; i += 1) {
    addScaffold(anchors[i], anchors[(i + 1) % anchors.length], 0.55);
  }

  for (const node of ranked) {
    if (usableLinks.length + scaffold.length >= targetLinkCount) break;
    const id = String(node.id);
    if (!id || anchors.includes(id)) continue;
    const bestAnchor = [...anchors].sort((a, b) => {
      const aNode = nodesById.get(a);
      const bNode = nodesById.get(b);
      return (
        overlapScore(b, id) - overlapScore(a, id) ||
        queryNodeScore(bNode, seedIds, hubIds, bridgeIds) -
          queryNodeScore(aNode, seedIds, hubIds, bridgeIds) ||
        a.localeCompare(b)
      );
    })[0];
    addScaffold(bestAnchor, id, 0.42);
  }

  return {
    nodes,
    links: [...usableLinks, ...scaffold],
  };
}

function curateQueryGraphForCanvas(
  payload: GraphPayload | null,
  seedIds: Set<string>,
  hubIds: Set<string>,
  bridgeIds: Set<string>,
): GraphPayload | null {
  if (!payload) return payload;
  const scaffolded = ensureQueryScaffoldLinks(payload, seedIds, hubIds, bridgeIds);
  if (scaffolded.nodes.length <= QUERY_VISIBLE_NODE_LIMIT) return scaffolded;

  const nodesById = new Map<string, any>();
  for (const node of scaffolded.nodes) nodesById.set(String(node.id), node);

  const usableLinks = scaffolded.links.filter((link) => {
    const s = graphEndpointId(link.source);
    const t = graphEndpointId(link.target);
    return s && t && s !== t && nodesById.has(s) && nodesById.has(t);
  });

  const degreeById = new Map<string, number>();
  const incidentById = new Map<string, any[]>();
  for (const link of usableLinks) {
    const s = graphEndpointId(link.source);
    const t = graphEndpointId(link.target);
    degreeById.set(s, (degreeById.get(s) ?? 0) + 1);
    degreeById.set(t, (degreeById.get(t) ?? 0) + 1);
    if (!incidentById.has(s)) incidentById.set(s, []);
    if (!incidentById.has(t)) incidentById.set(t, []);
    incidentById.get(s)?.push(link);
    incidentById.get(t)?.push(link);
  }

  const isAnchor = (id: string) =>
    seedIds.has(id) || hubIds.has(id) || bridgeIds.has(id);
  const nodeScore = (node: any) => {
    const id = String(node.id);
    const kind = String(node.kind || node.entity_type || node.supernode_type || "");
    const mentions = Number(node.mention_count ?? node.total_mentions ?? 1);
    let score = (degreeById.get(id) ?? 0) * 14;
    if (seedIds.has(id)) score += 1200;
    if (hubIds.has(id)) score += 700;
    if (bridgeIds.has(id)) score += 650;
    if (kind.toLowerCase() === "book" || node.is_cluster_anchor) score += 120;
    if (kind.toLowerCase() === "concept") score += 70;
    if (Number.isFinite(mentions)) score += Math.log2(mentions + 1) * 12;
    return score;
  };

  const rankedNodes = [...scaffolded.nodes].sort((a, b) => nodeScore(b) - nodeScore(a));
  const selected = new Set<string>();
  const addNode = (id: string) => {
    if (selected.size >= QUERY_VISIBLE_NODE_LIMIT) return false;
    if (!nodesById.has(id)) return false;
    selected.add(id);
    return true;
  };

  for (const node of rankedNodes) {
    const id = String(node.id);
    if (isAnchor(id)) addNode(id);
  }

  const anchors = [...selected];
  for (const id of anchors) {
    const incident = [...(incidentById.get(id) || [])].sort(
      (a, b) => graphLinkWeight(b) - graphLinkWeight(a),
    );
    for (const link of incident) {
      if (selected.size >= Math.floor(QUERY_VISIBLE_NODE_LIMIT * 0.72)) break;
      const s = graphEndpointId(link.source);
      const t = graphEndpointId(link.target);
      addNode(s === id ? t : s);
    }
  }

  for (const node of rankedNodes) {
    if (selected.size >= QUERY_VISIBLE_NODE_LIMIT) break;
    addNode(String(node.id));
  }

  const selectedLinks = usableLinks
    .filter((link) => selected.has(graphEndpointId(link.source)) &&
      selected.has(graphEndpointId(link.target)))
    .sort((a, b) => {
      const aAnchor =
        Number(isAnchor(graphEndpointId(a.source))) +
        Number(isAnchor(graphEndpointId(a.target)));
      const bAnchor =
        Number(isAnchor(graphEndpointId(b.source))) +
        Number(isAnchor(graphEndpointId(b.target)));
      return bAnchor - aAnchor || graphLinkWeight(b) - graphLinkWeight(a);
    })
    .slice(0, QUERY_VISIBLE_EDGE_LIMIT);

  return {
    nodes: scaffolded.nodes.filter((node) => selected.has(String(node.id))),
    links: selectedLinks,
  };
}

// ─── Brain mode data hook ─────────────────────────────────────────────────

// Note: client-side bridge synthesis from `top_entities` intersection used to
// live here as `computeClusterBridges`. Replaced by /api/graph/brain-view
// which computes ground-truth bridge strengths in Cypher (shared MENTIONS
// + RELATES_TO traversal) so we no longer approximate.

function useBrainGraph(
  corpusIds: string[],
  drill: DrillFrame | null,
) {
  const [data, setData] = useState<{
    nodes: any[];
    links: any[];
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cacheWarming, setCacheWarming] = useState<string[]>([]);
  // Per-corpus cache classification: "missing" (never built — needs manual
  // rebuild) vs "warming" (stale signature — rebuild already needed) vs
  // "ready". Populated by polling /api/corpora/{cid}/cache-status whenever
  // cacheWarming is non-empty.
  const [cacheStatuses, setCacheStatuses] = useState<
    Record<string, api.CacheStatus>
  >({});
  const [rebuildingIds, setRebuildingIds] = useState<Set<string>>(new Set());

  const reload = useCallback(async () => {
    if (corpusIds.length === 0) {
      setData(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      // ── DRILL: one Document anchor + its local entities + cross-book bridges ──
      // Powered by the rich :Document anchor schema — Cypher returns the
      // anchor's local_entities + intra-book relations + cross_book_bridges
      // in one query. No MongoDB enrichment, no Python aggregation.
      if (drill) {
        const drillRes = await api.getBookDrilldown(
          drill.docId,
          corpusIds,
        );

        const anchorRaw =
          drillRes.anchor?.label ||
          drillRes.anchor?.filename ||
          drillRes.anchor?.doc_id?.slice(0, 8) ||
          "";
        const anchorNode: any = drillRes.anchor
          ? {
              id: `book:${drillRes.anchor.doc_id}`,
              display_name: anchorRaw,
              // Sigma renders `label`; tooltip / selection bar reads
              // `display_name` for full context.
              label: cleanBookLabel(anchorRaw) || anchorRaw,
              mention_count: drillRes.anchor.chunk_count || 1,
              kind: "book" as const,
              source_corpora: [drillRes.anchor.corpus_id],
              source_corpus: drillRes.anchor.corpus_id,
              is_cluster_anchor: true,
              forceLabel: true,
            }
          : null;

        const entityNodes = drillRes.local_entities.map((e) => ({
          id: e.entity_id,
          display_name: e.display_name,
          entity_type: e.entity_type,
          primary_entity_type: e.primary_entity_type,
          definitional_phrase: e.definitional_phrase,
          observed_entity_types: e.observed_entity_types,
          confidence: e.confidence,
          object_kind: e.object_kind,
          canonical_family: e.canonical_family,
          primary_doc_id: drill.docId,
          mention_count: 1,
        }));

        // Bridge entities (from cross-book results) become their own nodes
        // so the user sees the literal entity that connects two books.
        const bridgeNodes = drillRes.cross_book_bridges.map((b) => ({
          id: b.bridge_entity_id,
          display_name: b.bridge_entity_name || b.bridge_entity_id,
          entity_type: b.bridge_entity_type || "",
          mention_count: b.strength || 1,
        }));

        // Edges: book→entity (membership), entity→entity (intra-book), entity→other-book (bridge).
        const memberEdges = entityNodes.map((e) => ({
          source: e.id,
          target: `book:${drill.docId}`,
          predicate: "in_book",
          confidence: 1,
          weight: 0.4,
        }));
        const intraEdges = drillRes.local_relations.map((r) => ({
          source: r.source_id,
          target: r.target_id,
          predicate: r.predicate,
          relation_family: r.relation_family,
          confidence: r.confidence,
        }));
        const bridgeEdges = drillRes.cross_book_bridges.flatMap((b) => [
          {
            source: b.via_entity_id,
            target: b.bridge_entity_id,
            predicate: "bridges_to",
            weight: b.strength,
            confidence: 0.7,
          },
          {
            source: b.bridge_entity_id,
            target: `book:${b.target_doc_id}`,
            predicate: "in_book",
            weight: 0.3,
            confidence: 0.6,
          },
        ]);
        // Synthetic anchors for the bridge target books so the canvas has
        // a node to land the bridge edge on.
        const targetAnchorNodes = drillRes.cross_book_bridges.map((b) => {
          const rawTarget = b.target_filename || b.target_doc_id.slice(0, 8);
          return {
            id: `book:${b.target_doc_id}`,
            display_name: rawTarget,
            label: cleanBookLabel(rawTarget) || rawTarget,
            mention_count: 1,
            kind: "book" as const,
            source_corpora: [b.target_corpus_id || ""],
            source_corpus: b.target_corpus_id || "",
            is_cluster_anchor: true,
          };
        });

        const nodes = [
          ...(anchorNode ? [anchorNode] : []),
          ...entityNodes,
          ...bridgeNodes,
          ...targetAnchorNodes,
        ];
        // Dedup by id (target anchors may collide with the drilled anchor on
        // self-bridges; entity nodes can repeat across local + bridge).
        const seen = new Set<string>();
        const dedupedNodes = nodes.filter((n) => {
          if (seen.has(n.id)) return false;
          seen.add(n.id);
          return true;
        });

        setData({
          nodes: dedupedNodes,
          links: [...memberEdges, ...intraEdges, ...bridgeEdges],
        });
        setCacheWarming([]);
        return;
      }

      // ── TOP-LEVEL: Brain View v2 — pure-Cypher anchors + bridge strengths ──
      // POST /api/graph/brain-view keys off :Document {is_cluster_anchor: true}
      // and computes pairwise bridge strength on the Neo4j side. Anchor
      // metadata (filename, chunk_count, ghost_b_success_rate) lives on the
      // Document node so no MongoDB round-trip is needed.
      const bv = await api.getBrainView(corpusIds);
      // Backend computes the pairwise-bridge view ONCE per corpus signature
      // (too heavy for request time at 500-book scale — it used to 504) and
      // serves `meta.warming` until the background build lands. Poll.
      if ((bv.meta as Record<string, unknown> | undefined)?.warming) {
        window.setTimeout(() => {
          void reload();
        }, 20000);
      }
      // Sort by bridge_count desc so we can tag the top-N anchors with
      // forceLabel — those are the most-connected books and worth always
      // labelling, the long tail relies on semantic zoom.
      const sortedDocs = [...bv.documents].sort(
        (a, b) => (b.bridge_count || 0) - (a.bridge_count || 0),
      );
      const topN = adaptiveTopN(sortedDocs.length);

      // Octopus / spotlight mode. The top SPOTLIGHT_COUNT docs by bridge
      // count grow satellites (orbiting Entity nodes). The long tail stays
      // as plain head-only anchors so the canvas doesn't drown in dots.
      // 100 docs × 8 satellites = 800 satellite nodes max — well within
      // sigma's smooth-render budget.
      const SPOTLIGHT_COUNT = 100;
      const SAT_ORBIT_R = 28; // initial orbit radius (FA2 will adjust)

      const anchorNodes: any[] = [];
      const satelliteNodes: any[] = [];
      const satelliteEdges: any[] = [];

      sortedDocs.forEach((d, idx) => {
        const rawLabel = d.label || d.filename || d.doc_id.slice(0, 8);
        const bookId = `book:${d.doc_id}`;
        anchorNodes.push({
          id: bookId,
          // Full text kept on display_name for tooltip; sigma renders `label`.
          display_name: rawLabel,
          label: cleanBookLabel(rawLabel) || rawLabel,
          // mention_count drives node size in the adapter — use chunk_count
          // so a 5000-chunk book is visually larger than a 50-chunk one.
          mention_count: Math.max(1, d.chunk_count || d.actual_chunk_count || 1),
          kind: "book" as const,
          source_corpora: [d.corpus_id],
          source_corpus: d.corpus_id,
          is_cluster_anchor: true,
          // Top-N strongest anchors keep their label visible at all zoom
          // levels; the long tail relies on semantic-zoom logic in useSigma.
          forceLabel: idx < topN,
          // Pt 5: extraction-schema facets drive deterministic node color
          // in polymath-graph-adapter::pickNodeColor.
          dominant_family: d.dominant_family,
          dominant_entity_type: d.dominant_entity_type,
          // Pass anchor metadata through so the selection bar can render it.
          ghost_b_success_rate: d.ghost_b_success_rate,
          ghost_b_extracted: d.ghost_b_extracted,
          ghost_b_total: d.ghost_b_total,
          chunk_count: d.chunk_count,
          parent_count: d.parent_count,
          bridge_count: d.bridge_count,
          filename: d.filename,
          entity_count: d.entity_count,
        });

        // Only the spotlight (top SPOTLIGHT_COUNT by bridge_count) gets
        // satellites. Long-tail books read as solo dots, keeping the
        // canvas legible at 1000+ books.
        if (idx < SPOTLIGHT_COUNT) {
          const topEntities = d.top_entity_records?.length
            ? d.top_entity_records
            : (d.top_entities || []).map((name) => ({
                name,
                entity_id: null,
                entity_type: "",
                primary_entity_type: null,
                definitional_phrase: null,
                observed_entity_types: null,
                canonical_family: null,
                confidence: null,
                mention_count: null,
              }));
          const satCount = topEntities.length;
          topEntities.forEach((entity, i) => {
            const name = entity.name || entity.entity_id || "";
            if (!name) return;
            // Pre-bake polar position around (0,0) — the adapter resolves
            // the book's anchor position and adds these as offsets, so
            // satellites start in a ring instead of being dragged into
            // orbit by FA2.
            const angle =
              (i / Math.max(satCount, 1)) * Math.PI * 2 + idx * 0.7;
            const entityId = `ent:${d.doc_id}:${i}`;
            satelliteNodes.push({
              id: entityId,
              display_name: name,
              label: name.length > 18 ? name.slice(0, 17) + "…" : name,
              entity_type: entity.entity_type || "",
              primary_entity_type: entity.primary_entity_type ?? null,
              definitional_phrase: entity.definitional_phrase ?? null,
              observed_entity_types: entity.observed_entity_types ?? null,
              canonical_family: entity.canonical_family ?? null,
              confidence: entity.confidence ?? null,
              source_corpus: d.corpus_id,
              source_corpora: [d.corpus_id],
              // primary_doc_id wires the adapter's "orbit your book"
              // positioning. Without this, FA2 would scatter satellites.
              primary_doc_id: d.doc_id,
              mention_count: entity.mention_count ?? 1,
              // Pre-baked polar — adapter adds anchor position.
              x: Math.cos(angle) * SAT_ORBIT_R,
              y: Math.sin(angle) * SAT_ORBIT_R,
            });
            satelliteEdges.push({
              source: bookId,
              target: entityId,
              predicate: "contains",
              // Structural containment, not a weak semantic relation — keeps the
              // Evidence Inspector from labeling every satellite "WeakAssociation".
              relation_family: "Structural",
              weight: 0.2,
            });
          });
        }
      });

      const bridgeLinks = bv.bridges.map((b) => ({
        source: `book:${b.source}`,
        target: `book:${b.target}`,
        predicate: "bridges_to",
        // Use strength on weight so the adapter / sigma reducer thickens
        // bridges by how many cross-book entity pairs they represent.
        weight: b.strength,
        confidence: Math.min(1, (b.shared_entities || 0) / 12),
        // Pt 5: passes through to the adapter, which uses
        // EDGE_COLORS_BY_FAMILY[dominant_relation_family] for the edge color.
        dominant_relation_family: b.dominant_relation_family,
        // Pt 7c: top shared concept names + total count. The adapter
        // builds an on-edge label string like "Bayes' theorem +5" so
        // users see what links two books without clicking.
        top_shared_entities: b.top_shared_entities || [],
        shared_entities: b.shared_entities,
      }));
      setData({
        nodes: [...anchorNodes, ...satelliteNodes],
        links: [...bridgeLinks, ...satelliteEdges],
      });
      setCacheWarming([]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [corpusIds, drill]);

  useEffect(() => {
    reload();
  }, [reload]);

  // Whenever cacheWarming changes, fetch per-corpus statuses ONCE so the
  // chip + Build button can show the real state (missing vs warming vs
  // running rebuild). Then poll every 15s if any are still not ready.
  useEffect(() => {
    if (!cacheWarming.length) {
      setCacheStatuses({});
      return;
    }
    let cancelled = false;
    const refresh = async () => {
      try {
        const [statuses, rebuildState] = await Promise.all([
          Promise.all(cacheWarming.map((cid) => api.getCorpusCacheStatus(cid))),
          api.getGraphCacheRebuildStatus().catch(() => ({
            in_flight: [],
            finished: [],
          })),
        ]);
        if (cancelled) return;
        setCacheStatuses(
          Object.fromEntries(statuses.map((s) => [s.corpus_id, s])),
        );
        setRebuildingIds(new Set(rebuildState.in_flight));
        const stillNotReady = statuses
          .filter(
            (s) => s.metrics_cache !== "ready" || s.domain_cache !== "ready",
          )
          .map((s) => s.corpus_id);
        if (stillNotReady.length === 0) reload();
        else setCacheWarming(stillNotReady);
      } catch {
        /* swallow */
      }
    };
    refresh();
    const t = setInterval(refresh, 15000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [cacheWarming, reload]);

  const triggerRebuild = useCallback(
    async (ids: string[]) => {
      if (ids.length === 0) return;
      try {
        const res = await api.rebuildGraphCache(ids);
        const triggered = new Set([...res.rebuilding, ...res.already_running]);
        setRebuildingIds((prev) => {
          const next = new Set(prev);
          for (const cid of triggered) next.add(cid);
          return next;
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [],
  );

  return {
    data,
    loading,
    error,
    cacheWarming,
    cacheStatuses,
    rebuildingIds,
    triggerRebuild,
    reload,
  };
}

// ─── Query mode data hook ─────────────────────────────────────────────────

function useQueryGraph(
  corpusIds: string[],
  query: string | undefined,
  synthesisQuery: string | undefined,
  synthesisMode: GraphSynthesisMode = "research",
  validateSynthesis: boolean = false,
) {
  const graphQuerySeedEntities = useSettingsStore(
    (state) => state.graphQuerySeedEntities,
  );
  const graphQueryMaxHops = useSettingsStore((state) => state.graphQueryMaxHops);
  const graphQueryNodeLimit = useSettingsStore(
    (state) => state.graphQueryNodeLimit,
  );
  const [phase, setPhase] = useState<"idle" | "loading" | "ready" | "error">(
    "idle",
  );
  const [data, setData] = useState<{ nodes: any[]; links: any[] } | null>(null);
  const [seedIds, setSeedIds] = useState<Set<string>>(new Set());
  const [hubIds, setHubIds] = useState<Set<string>>(new Set());
  const [bridgeIds, setBridgeIds] = useState<Set<string>>(new Set());
  const [gaps, setGaps] = useState<any[]>([]);
  const [synthesis, setSynthesis] = useState<{
    markdown: string;
    sources: any[];
    files?: any[];
    sessionId?: string;
    responseQuery?: string;
    graphQuery?: string;
    headline?: string;
    perCorpus?: Array<{ corpus_id: string; markdown: string }>;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [progressStage, setProgressStage] =
    useState<GraphQueryProgressStage>("idle");
  const [progressError, setProgressError] = useState<string | null>(null);

  useEffect(() => {
    let cancel = false;
    let synthesisStageTimer: ReturnType<typeof setTimeout> | null = null;
    const subgraphStageTimers: ReturnType<typeof setTimeout>[] = [];
    const clearSubgraphStageTimers = () => {
      while (subgraphStageTimers.length) {
        const timer = subgraphStageTimers.pop();
        if (timer) clearTimeout(timer);
      }
    };
    const run = async () => {
      if (!corpusIds.length || !query?.trim()) {
        setPhase("idle");
        setData(null);
        setSeedIds(new Set());
        setHubIds(new Set());
        setBridgeIds(new Set());
        setGaps([]);
        setSynthesis(null);
        setError(null);
        setProgressStage("idle");
        setProgressError(null);
        return;
      }
      setPhase("loading");
      setError(null);
      setSynthesis(null);
      setProgressStage("querying");
      setProgressError(null);
      const graphProgressStartedAt = Date.now();
      let subgraphLoaded = false;
      try {
        subgraphStageTimers.push(
          setTimeout(() => {
            if (!cancel) setProgressStage("following");
          }, GRAPH_QUERY_PROGRESS_FOLLOWING_MS),
          setTimeout(() => {
            if (!cancel) setProgressStage("analyzing");
          }, GRAPH_QUERY_PROGRESS_ANALYZING_MS),
        );
        const subgraphP = api.queryGraph(
          corpusIds,
          query,
          graphQueryMaxHops,
          graphQueryNodeLimit,
          { seedLimitPerToken: graphQuerySeedEntities },
        );
        const synthP = api.discoverGraph({
          corpus_ids: corpusIds as any,
          query: synthesisQuery?.trim() || query,
          mode: "auto",
          synthesis_mode: synthesisMode,
          validate_synthesis: validateSynthesis,
        } as any);
        // The map and synthesis run in parallel so the graph can appear first.
        // If the map request fails, this prevents the still-running synthesis
        // promise from surfacing as an unhandled rejection.
        void synthP.catch(() => undefined);
        const sub = await subgraphP;
        if (cancel) return;
        setSeedIds(
          new Set<string>((sub.seed_entities || []).map((s: any) => String(s.id))),
        );
        setHubIds(
          new Set<string>((sub.hubs || []).map((h: any) => String(h.entity_id))),
        );
        setBridgeIds(
          new Set<string>((sub.bridges || []).map((b: any) => String(b.entity_id))),
        );
        setGaps(sub.gaps || []);
        setData({ nodes: sub.nodes || [], links: sub.links || [] });
        subgraphLoaded = true;
        await wait(GRAPH_QUERY_PROGRESS_PACKING_MS - (Date.now() - graphProgressStartedAt));
        if (cancel) return;
        clearSubgraphStageTimers();
        setProgressStage("packing");
        synthesisStageTimer = setTimeout(() => {
          if (!cancel) setProgressStage("synthesizing");
        }, GRAPH_QUERY_PROGRESS_SYNTHESIS_MS);
        const synth = await synthP;
        if (cancel) return;
        if (synthesisStageTimer) {
          clearTimeout(synthesisStageTimer);
          synthesisStageTimer = null;
        }
        const auto = (synth as any).auto_synthesis || {};
        setSynthesis({
          markdown:
            auto.markdown ||
            (synth as any).interpretation ||
            "(no synthesis generated)",
          sources: auto.sources || [],
          files: (synth as any).trace?.llm_context?.files || [],
          sessionId: (synth as any).session_id || "",
          responseQuery: (synth as any).query || "",
          graphQuery: query,
          headline:
            auto.headline ||
            (synth as any).headline?.headline ||
            (synth as any).headline?.kicker ||
            "",
          perCorpus: auto.per_corpus_synthesis || undefined,
        });
        setProgressStage("done");
        setPhase("ready");
      } catch (e) {
        if (cancel) return;
        clearSubgraphStageTimers();
        const message = e instanceof Error ? e.message : String(e);
        setError(message);
        setProgressError(message);
        setProgressStage(
          subgraphLoaded ? "synthesis_error_after_map" : "subgraph_error",
        );
        setPhase(subgraphLoaded ? "ready" : "error");
      }
    };
    run();
    return () => {
      cancel = true;
      clearSubgraphStageTimers();
      if (synthesisStageTimer) clearTimeout(synthesisStageTimer);
    };
  }, [
    corpusIds,
    query,
    synthesisQuery,
    synthesisMode,
    validateSynthesis,
    graphQuerySeedEntities,
    graphQueryMaxHops,
    graphQueryNodeLimit,
  ]);

  return {
    phase,
    data,
    seedIds,
    hubIds,
    bridgeIds,
    gaps,
    synthesis,
    error,
    progressSteps: graphQueryProgressSteps(
      synthesisMode,
      progressStage,
      progressError,
    ),
  };
}

function useQuestionGraph(
  corpusIds: string[],
  query: string | undefined,
) {
  const graphQuerySeedEntities = useSettingsStore(
    (state) => state.graphQuerySeedEntities,
  );
  const graphQueryMaxHops = useSettingsStore((state) => state.graphQueryMaxHops);
  const graphQueryNodeLimit = useSettingsStore(
    (state) => state.graphQueryNodeLimit,
  );
  const [phase, setPhase] = useState<"idle" | "loading" | "ready" | "error">(
    "idle",
  );
  const [data, setData] = useState<{ nodes: any[]; links: any[] } | null>(null);
  const [seedIds, setSeedIds] = useState<Set<string>>(new Set());
  const [hubIds, setHubIds] = useState<Set<string>>(new Set());
  const [bridgeIds, setBridgeIds] = useState<Set<string>>(new Set());
  const [gaps, setGaps] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [progressStage, setProgressStage] =
    useState<QuestionGraphProgressStage>("idle");
  const [progressError, setProgressError] = useState<string | null>(null);

  useEffect(() => {
    let cancel = false;
    const run = async () => {
      if (!corpusIds.length || !query?.trim()) {
        setPhase("idle");
        setData(null);
        setSeedIds(new Set());
        setHubIds(new Set());
        setBridgeIds(new Set());
        setGaps([]);
        setError(null);
        setProgressStage("idle");
        setProgressError(null);
        return;
      }
      setPhase("loading");
      setError(null);
      setProgressStage("querying");
      setProgressError(null);
      try {
        const sub = await api.queryGraph(
          corpusIds,
          query,
          graphQueryMaxHops,
          graphQueryNodeLimit,
          { seedLimitPerToken: graphQuerySeedEntities },
        );
        if (cancel) return;
        setSeedIds(
          new Set<string>((sub.seed_entities || []).map((s: any) => String(s.id))),
        );
        setHubIds(
          new Set<string>((sub.hubs || []).map((h: any) => String(h.entity_id))),
        );
        setBridgeIds(
          new Set<string>((sub.bridges || []).map((b: any) => String(b.entity_id))),
        );
        setGaps(sub.gaps || []);
        setData({ nodes: sub.nodes || [], links: sub.links || [] });
        setPhase("ready");
        setProgressStage("done");
      } catch (e) {
        if (cancel) return;
        const message = e instanceof Error ? e.message : String(e);
        setError(message);
        setProgressError(message);
        setProgressStage("error");
        setPhase("error");
      }
    };
    run();
    return () => {
      cancel = true;
    };
  }, [
    corpusIds,
    query,
    graphQuerySeedEntities,
    graphQueryMaxHops,
    graphQueryNodeLimit,
  ]);

  return {
    phase,
    data,
    seedIds,
    hubIds,
    bridgeIds,
    gaps,
    error,
    progressSteps: questionGraphProgressSteps(progressStage, progressError),
  };
}

// ─── Component ────────────────────────────────────────────────────────────

export function GraphViewer({
  mode,
  corpusIds,
  query,
  onRerun,
  onQueryPhaseChange,
  onSendToChat,
}: GraphViewerProps) {
  // Default to entity_type so nodes are colored by their GLiNER type on first
  // load (Person/Concept/Software/...), decoupled from relation strength.
  const [colorMode, setColorMode] = useState<ColorMode>("entity_type");
  const [drillStack, setDrillStack] = useState<DrillFrame[]>([]);
  const [hoveredName, setHoveredName] = useState<string | null>(null);
  // Pt 5: right sidebar dashboard collapse state.
  const [dashboardCollapsed, setDashboardCollapsed] = useState(false);
  // Pt 6: bridge filter knobs (driven by dashboard sliders). Defaults match
  // the Pt 5 hardcoded behavior so the canvas looks the same on load.
  const [minBridgeStrength, setMinBridgeStrength] = useState(2);
  const [maxBridgesPerBook, setMaxBridgesPerBook] = useState(3);
  // Pt 6: re-settle FA2 layout briefly after the user releases a dragged
  // node. Off by default; user toggles in the Layout section of the dashboard.
  const [settleAfterDrag, setSettleAfterDrag] = useState(false);
  // Pt 7: tab selection in the sidebar + Graph Query tab's local input.
  // `agentInput` is what the user types; `agentQuery` is what useQueryGraph
  // actually consumes (only promoted when the user hits Run). This way
  // every keystroke doesn't refire the query.
  const [activeTab, setActiveTab] = useState<DashboardTab>("brain");
  const [agentInput, setAgentInput] = useState<string>(query ?? "");
  const [agentQuery, setAgentQuery] = useState<string | undefined>(query);
  const [agentSynthesisQuery, setAgentSynthesisQuery] = useState<string | undefined>(
    query,
  );
  const [questionGraphQuery, setQuestionGraphQuery] = useState<string | undefined>(
    undefined,
  );
  const [lastGraphContext, setLastGraphContext] =
    useState<GraphTurnContext | null>(null);
  // Phase 3 — synthesis-mode toggle. "research" (default) gives concrete
  // claims; "ideation" produces [BUILD IDEA] blocks; "nuance" explores gap
  // typology, analogies, transfers, and bridges.
  const [draftSynthesisMode, setDraftSynthesisMode] =
    useState<GraphSynthesisMode>("research");
  const [executedSynthesisMode, setExecutedSynthesisMode] =
    useState<GraphSynthesisMode>("research");
  // Sprint #2 — opt-in critique + revise loop. When true, the backend
  // runs auditor + editor passes after the draft (2-3× LLM cost).
  // Off by default so the common case stays single-call.
  const [draftValidateSynthesis, setDraftValidateSynthesis] =
    useState<boolean>(false);
  const [executedValidateSynthesis, setExecutedValidateSynthesis] =
    useState<boolean>(false);
  const drillStackRef = useRef(drillStack);
  drillStackRef.current = drillStack;

  const drill = drillStack.length ? drillStack[drillStack.length - 1] : null;
  const brain = useBrainGraph(
    mode === "brain" ? corpusIds : [],
    drill,
  );
  // Pt 7: useQueryGraph now reads `agentQuery` state instead of the props.query.
  // For mode==="query" callers, props.query seeds agentQuery on mount. For
  // mode==="brain", the Agent Search tab inside the dashboard can drive
  // queries by promoting agentInput → agentQuery via onAgentRun.
  // Corpora pool is always available (regardless of mode) so users can
  // run an agent query from the brain canvas without switching modes.
  const q = useQueryGraph(
    corpusIds,
    agentQuery,
    agentSynthesisQuery,
    executedSynthesisMode,
    executedValidateSynthesis,
  );
  const lightQ = useQuestionGraph(corpusIds, questionGraphQuery);
  const heavyQueryActive = Boolean(agentQuery?.trim()) && q.phase !== "idle";
  const lightQueryActive =
    !heavyQueryActive && Boolean(questionGraphQuery?.trim()) && lightQ.phase !== "idle";
  const activeQ = heavyQueryActive ? q : lightQ;
  const queryLensActive = heavyQueryActive || lightQueryActive;
  const effectiveMode: GraphViewerMode =
    mode === "query" || queryLensActive ? "query" : "brain";
  const queryFingerprint = useMemo(
    () =>
      (heavyQueryActive ? agentQuery : questionGraphQuery)?.trim()
        ? fingerprintGraphQuery((heavyQueryActive ? agentQuery : questionGraphQuery) || "")
        : undefined,
    [agentQuery, questionGraphQuery, heavyQueryActive],
  );

  const agentSeedNames = useMemo(
    () =>
      (q.data?.nodes || [])
        .filter((n: any) => q.seedIds.has(String(n.id)))
        .map((n: any) => String(n.display_name || n.id)),
    [q.data, q.seedIds],
  );
  const agentBridgeNames = useMemo(
    () =>
      (q.data?.nodes || [])
        .filter((n: any) => q.bridgeIds.has(String(n.id)))
        .map((n: any) => String(n.display_name || n.id)),
    [q.data, q.bridgeIds],
  );
  const agentHubNames = useMemo(
    () =>
      (q.data?.nodes || [])
        .filter((n: any) => q.hubIds.has(String(n.id)))
        .map((n: any) => String(n.display_name || n.id)),
    [q.data, q.hubIds],
  );
  const agentSourceNames = useMemo(
    () =>
      Array.from(
        new Set(
          ((q.synthesis?.files?.length ? q.synthesis.files : q.synthesis?.sources) || [])
            .map((s: any) => String(s.source_label || s.label || s.doc_id || "").trim())
            .filter(Boolean),
        ),
      ),
    [q.synthesis],
  );

  const runGraphQuery = useCallback(
    (nextQuery?: string, requestedMode?: GraphRunMode) => {
      const normalized = (nextQuery ?? agentInput).trim();
      if (!normalized) return;
      const runMode = requestedMode || "new";
      const shouldContinue = runMode === "followup" && lastGraphContext !== null;
      const synthesisQuery = shouldContinue
        ? buildFollowUpSynthesisQuery(normalized, lastGraphContext)
        : normalized;
      setAgentInput(normalized);
      setExecutedSynthesisMode(draftSynthesisMode);
      setExecutedValidateSynthesis(draftValidateSynthesis);
      setAgentQuery(normalized);
      setAgentSynthesisQuery(synthesisQuery);
      setQuestionGraphQuery(undefined);
      if (!shouldContinue) {
        setLastGraphContext(null);
      }
      setActiveTab("agent");
    },
    [
      agentInput,
      draftSynthesisMode,
      draftValidateSynthesis,
      lastGraphContext,
    ],
  );

  const runQuestionGraph = useCallback((nextQuery: string) => {
    const normalized = nextQuery.trim();
    if (!normalized) return;
    setQuestionGraphQuery(normalized);
    setAgentQuery(undefined);
    setAgentSynthesisQuery(undefined);
    setAgentInput(normalized);
    setLastGraphContext(null);
    setActiveTab("brain");
  }, []);

  const clearGraphQuery = useCallback(() => {
    setAgentQuery(undefined);
    setAgentSynthesisQuery(undefined);
    setQuestionGraphQuery(undefined);
    setAgentInput("");
    setLastGraphContext(null);
    setActiveTab("brain");
  }, []);

  const corpusKey = corpusIds.join("|");
  const previousCorpusKey = useRef(corpusKey);
  useEffect(() => {
    if (previousCorpusKey.current === corpusKey) return;
    previousCorpusKey.current = corpusKey;
    setAgentQuery(undefined);
    setAgentSynthesisQuery(undefined);
    setQuestionGraphQuery(undefined);
    setLastGraphContext(null);
    setActiveTab("brain");
  }, [corpusKey]);

  useEffect(() => {
    if (q.phase !== "ready" || !agentQuery?.trim() || !q.synthesis?.markdown) return;
    if (q.synthesis.graphQuery !== agentQuery) return;
    const seedNames = uniqueCompact(agentSeedNames, 8);
    const fileNames = uniqueCompact(agentSourceNames, 4, 120);
    const evidenceFacets = uniqueCompact(
      [...agentBridgeNames, ...agentHubNames, ...seedNames],
      8,
      80,
    );
    setLastGraphContext({
      query: agentQuery,
      coreIdea: extractCoreIdea(q.synthesis.markdown, q.synthesis.headline || ""),
      seedNames,
      fileNames,
      evidenceFacets,
    });
  }, [
    q.phase,
    q.synthesis,
    agentQuery,
    agentSeedNames,
    agentSourceNames,
    agentBridgeNames,
    agentHubNames,
  ]);

  useEffect(() => {
    onQueryPhaseChange?.(activeQ.phase);
  }, [onQueryPhaseChange, activeQ.phase]);

  const rawData = effectiveMode === "brain" ? brain.data : activeQ.data;
  const data = useMemo(
    () =>
      effectiveMode === "query"
        ? curateQueryGraphForCanvas(rawData, activeQ.seedIds, activeQ.hubIds, activeQ.bridgeIds)
        : rawData,
    [effectiveMode, rawData, activeQ.seedIds, activeQ.hubIds, activeQ.bridgeIds],
  );
  const loading = effectiveMode === "brain" ? brain.loading : activeQ.phase === "loading";
  const error = effectiveMode === "brain" ? brain.error : activeQ.error;

  // Double-click handler — drills into a book anchor (single-click selects
  // the node + neighbors as usual). Books-as-clusters is the only Brain
  // View now, so the only drill target is `book:<doc_id>`.
  const handleDoubleClickNode = useCallback(
    (nodeId: string) => {
      if (effectiveMode !== "brain") return;
      if (!nodeId.startsWith("book:")) return;
      const docId = nodeId.slice(5);
      const found = (data?.nodes || []).find((n: any) => String(n.id) === nodeId);
      const label =
        (found && (found.display_name as string)) || docId.slice(0, 8);
      setDrillStack([...drillStackRef.current, { docId, label }]);
    },
    [effectiveMode, data],
  );

  const sigma = useSigma({
    onNodeHover: (id) => {
      if (!id || !data) {
        setHoveredName(null);
        return;
      }
      const found = (data.nodes || []).find((n: any) => String(n.id) === id);
      setHoveredName(found ? String(found.display_name || id) : id);
    },
    onDoubleClickNode: handleDoubleClickNode,
    layoutMode: effectiveMode,
    queryFingerprint: effectiveMode === "query" ? queryFingerprint : undefined,
    settleAfterDrag,
  });

  // Push new data into sigma when it lands.
  useEffect(() => {
    if (!data) return;
    const seedIds = effectiveMode === "query" ? activeQ.seedIds : new Set<string>();
    const hubIds = effectiveMode === "query" ? activeQ.hubIds : new Set<string>();
    const bridgeIds = effectiveMode === "query" ? activeQ.bridgeIds : new Set<string>();
    const newGraph = polymathToGraphology(data.nodes, data.links, {
      colorMode,
      layoutMode: effectiveMode,
      queryFingerprint: effectiveMode === "query" ? queryFingerprint : undefined,
      seedIds,
      hubIds,
      bridgeIds,
      minBridgeStrength,
      maxBridgesPerBook,
    });
    sigma.setGraph(newGraph);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-run on data change
  }, [data, effectiveMode, queryFingerprint, activeQ.seedIds, activeQ.hubIds, activeQ.bridgeIds, minBridgeStrength, maxBridgesPerBook]);

  // Apply colorMode toggle without rebuilding graph.
  useEffect(() => {
    const sigmaInst = sigma.sigmaRef.current;
    const g = (sigmaInst as any)?.getGraph?.();
    if (!sigmaInst || !g || g.order === 0 || !data) return;
    // Easiest correct path: rebuild the graph with new colorMode. The
    // adapter is fast enough that this stays under a few ms even for
    // overview payloads (~80 nodes).
    const seedIds = effectiveMode === "query" ? activeQ.seedIds : new Set<string>();
    const hubIds = effectiveMode === "query" ? activeQ.hubIds : new Set<string>();
    const bridgeIds = effectiveMode === "query" ? activeQ.bridgeIds : new Set<string>();
    const newGraph = polymathToGraphology(data.nodes, data.links, {
      colorMode,
      layoutMode: effectiveMode,
      queryFingerprint: effectiveMode === "query" ? queryFingerprint : undefined,
      seedIds,
      hubIds,
      bridgeIds,
      minBridgeStrength,
      maxBridgesPerBook,
    });
    sigma.setGraph(newGraph);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- triggered only on colorMode flip
  }, [colorMode]);

  const selectedId = sigma.selectedNode;
  const selectedDisplay = useMemo(() => {
    if (!selectedId || !data) return null;
    const found = (data.nodes || []).find((n: any) => String(n.id) === selectedId);
    if (!found) return null;
    return {
      id: selectedId,
      display_name: String(
        (found as any).display_name || (found as any).label || selectedId,
      ),
      source_corpora: (Array.isArray((found as any).source_corpora)
        ? (found as any).source_corpora
        : (found as any).source_corpus
          ? [(found as any).source_corpus]
          : []) as string[],
      source_corpus: (found as any).source_corpus,
      nodeKind: (found as any).nodeKind,
      kind: (found as any).kind,
      entity_type: (found as any).entity_type,
      // Classification surfaced in the inspector ("what is this").
      primary_entity_type: (found as any).primary_entity_type,
      definitional_phrase: (found as any).definitional_phrase,
      observed_entity_types: (found as any).observed_entity_types,
      canonical_family: (found as any).canonical_family,
      confidence: (found as any).confidence,
      mention_count: (found as any).mention_count,
      dominant_entity_type: (found as any).dominant_entity_type,
      dominant_relation_family: (found as any).dominant_relation_family,
      primary_doc_id: (found as any).primary_doc_id,
      bridge_count: (found as any).bridge_count,
      chunk_count: (found as any).chunk_count,
      parent_count: (found as any).parent_count,
      entity_count: (found as any).entity_count,
      top_entities: Array.isArray((found as any).top_entities)
        ? (found as any).top_entities
        : [],
    };
  }, [selectedId, data]);

  // ── Render ──

  if (corpusIds.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-[#06060a] text-zinc-400">
        <div className="text-center">
          <div className="text-base mb-2 font-mono">
            Select a corpus from the sidebar to begin
          </div>
          <div className="text-xs text-zinc-600 font-mono">
            Tip: select multiple corpora to see cross-book connections
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="relative flex h-full w-full flex-col md:flex-row bg-[#06060a]">
      {/* Canvas column (fills remaining width) */}
      <div className="relative flex-1 min-h-0 min-w-0">
        {/* Background gradient — same recipe as GitNexus GraphCanvas */}
        <div className="pointer-events-none absolute inset-0">
          <div
            className="absolute inset-0"
            style={{
              background: `
                radial-gradient(circle at 50% 50%, rgba(124, 58, 237, 0.05) 0%, transparent 65%),
                linear-gradient(to bottom, #06060a, #0a0a14)
              `,
            }}
          />
        </div>

      {/* Hovered node tooltip — only when nothing is selected. Stays on
          canvas (cursor-following pill) rather than in the dashboard. */}
      {hoveredName && !selectedId && (
        <div className="pointer-events-none absolute top-3 left-1/2 z-20 -translate-x-1/2 rounded-lg border border-zinc-800 bg-[#0d0d14]/95 px-3 py-1.5 backdrop-blur">
          <span className="font-mono text-sm text-zinc-100">{hoveredName}</span>
        </div>
      )}

      {/* Loading / error overlay */}
      {(loading || error) && (
        <div className="absolute inset-0 z-10 flex items-center justify-center pointer-events-none">
          {loading && !error && (
            <div className="flex items-center gap-2 text-zinc-400 text-xs font-mono">
              <Loader2 className="w-4 h-4 animate-spin" />
              {effectiveMode === "query"
                ? heavyQueryActive
                  ? "synthesizing across corpora..."
                  : "building question graph..."
                : "loading graph..."}
            </div>
          )}
          {error && (
            <div className="text-rose-300 text-xs font-mono pointer-events-auto bg-[#0d0d14] border border-rose-900/50 rounded px-3 py-2 max-w-md">
              {error}
            </div>
          )}
        </div>
      )}

      {/* Empty data fallback */}
      {data && data.nodes.length === 0 && !loading && (
        <div className="absolute inset-0 z-10 flex items-center justify-center">
          <EmptyDataState
            mode={effectiveMode}
            cacheWarming={effectiveMode === "brain" ? brain.cacheWarming : []}
            statuses={effectiveMode === "brain" ? brain.cacheStatuses : {}}
            rebuildingIds={effectiveMode === "brain" ? brain.rebuildingIds : new Set()}
            onRebuild={effectiveMode === "brain" ? brain.triggerRebuild : async () => {}}
          />
        </div>
      )}

      {/* Sigma canvas */}
      <div className="absolute inset-0 flex">
        <div className="relative flex-1 min-w-0">
          {/* Pt 6: Galaxy background canvas — dust particles + family
              nebulae + Book-anchor glow halos, all painted in lockstep
              with sigma via its `afterRender` event. Sits BEHIND sigma's
              own canvas (z-0) with pointer-events:none so it never
              swallows clicks. */}
          <GalaxyBackground
            sigmaRef={sigma.sigmaRef as any}
            isLayoutRunning={sigma.isLayoutRunning}
          />
          <div
            ref={sigma.containerRef}
            className="absolute inset-0 z-10 cursor-grab active:cursor-grabbing"
          />
        </div>
      </div>{/* end absolute inset-0 flex */}

      {/* Bottom-right control cluster — same layout as GitNexus */}
      <div className="absolute right-3 bottom-[calc(52dvh+0.75rem)] z-20 flex flex-col gap-1 pointer-events-auto md:right-4 md:bottom-4">
        <button
          onClick={sigma.zoomIn}
          className="flex h-9 w-9 items-center justify-center rounded-md border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          title="Zoom in"
        >
          <ZoomIn className="h-4 w-4" />
        </button>
        <button
          onClick={sigma.zoomOut}
          className="flex h-9 w-9 items-center justify-center rounded-md border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          title="Zoom out"
        >
          <ZoomOut className="h-4 w-4" />
        </button>
        <button
          onClick={sigma.resetZoom}
          className="flex h-9 w-9 items-center justify-center rounded-md border border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          title="Fit to screen"
        >
          <Maximize2 className="h-4 w-4" />
        </button>
        <div className="h-px bg-zinc-800 my-1" />
        <button
          onClick={sigma.isLayoutRunning ? sigma.stopLayout : sigma.startLayout}
          className={`flex h-9 w-9 items-center justify-center rounded-md border transition-all ${
            sigma.isLayoutRunning
              ? "animate-pulse border-violet-500 bg-violet-500/30 text-white"
              : "border-zinc-800 bg-[#0d0d14] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
          }`}
          title={sigma.isLayoutRunning ? "Pause layout" : "Resume layout"}
        >
          {sigma.isLayoutRunning ? (
            <Pause className="h-4 w-4" />
          ) : (
            <Play className="h-4 w-4" />
          )}
        </button>
      </div>

      {/* Layout running indicator */}
      {sigma.isLayoutRunning && (
        <div className="absolute bottom-[calc(52dvh+0.75rem)] left-1/2 z-10 flex -translate-x-1/2 items-center gap-2 rounded-full border border-emerald-500/30 bg-emerald-500/20 px-3 py-1.5 backdrop-blur md:bottom-4">
          <div className="h-2 w-2 animate-ping rounded-full bg-emerald-400" />
          <span className="text-xs font-medium text-emerald-200 font-mono">
            layout optimizing…
          </span>
        </div>
      )}
      </div>{/* end canvas column */}

      {/* Right sidebar dashboard (Pt 5 — flex layout). Holds breadcrumb,
          stats, cache health, color mode, selection info, layout controls.
          Collapses to a 36px strip via the panel toggle. */}
      <BrainViewDashboard
        collapsed={dashboardCollapsed}
        onToggle={() => setDashboardCollapsed((v) => !v)}
        mode={effectiveMode}
        drillStack={drillStack}
        setDrillStack={setDrillStack}
        corpusIds={corpusIds}
        data={data as any}
        cacheWarming={effectiveMode === "brain" ? brain.cacheWarming : []}
        cacheStatuses={effectiveMode === "brain" ? brain.cacheStatuses : {}}
        rebuildingIds={effectiveMode === "brain" ? brain.rebuildingIds : new Set()}
        onRebuild={effectiveMode === "brain" ? brain.triggerRebuild : async () => {}}
        colorMode={colorMode}
        onColorModeToggle={() =>
          setColorMode((m) =>
            m === "entity_type"
              ? "community"
              : m === "community"
                ? "corpus"
                : "entity_type",
          )
        }
        minBridgeStrength={minBridgeStrength}
        onMinBridgeStrengthChange={setMinBridgeStrength}
        maxBridgesPerBook={maxBridgesPerBook}
        onMaxBridgesPerBookChange={setMaxBridgesPerBook}
        settleAfterDrag={settleAfterDrag}
        onSettleAfterDragToggle={() => setSettleAfterDrag((v) => !v)}
        activeTab={activeTab}
        onActiveTabChange={setActiveTab}
        agentQuery={agentInput}
        onAgentQueryChange={setAgentInput}
        onAgentRun={(runMode) => runGraphQuery(undefined, runMode)}
        synthesisMode={draftSynthesisMode}
        onSynthesisModeChange={setDraftSynthesisMode}
        validateSynthesis={draftValidateSynthesis}
        onValidateSynthesisChange={setDraftValidateSynthesis}
        followUpAvailable={lastGraphContext !== null}
        followUpPreview={lastGraphContext?.coreIdea || lastGraphContext?.query || ""}
        onBuildQuestionGraph={runQuestionGraph}
        agentPhase={q.phase}
        agentError={q.error}
        agentSynthesisMarkdown={q.synthesis?.markdown ?? null}
        agentProgressSteps={q.progressSteps}
        questionProgressSteps={lightQ.progressSteps}
        agentSeedNames={agentSeedNames}
        agentSourceNames={agentSourceNames}
        agentBridgeNames={agentBridgeNames}
        agentHubNames={agentHubNames}
        agentGaps={q.gaps as any}
        onSendToChat={onSendToChat}
        onRerun={onRerun}
        showQueryRerun={heavyQueryActive}
        onClearGraphQuery={clearGraphQuery}
        selectedDisplay={selectedDisplay}
        onClearSelection={() => sigma.setSelectedNode(null)}
        isLayoutRunning={sigma.isLayoutRunning}
        startLayout={sigma.startLayout}
        stopLayout={sigma.stopLayout}
      />
    </div>
  );
}

export default GraphViewer;


// ─── Cache-warming sub-components ─────────────────────────────────────────
// Pt 5: CacheWarmingChip + CacheChipProps removed. BrainViewDashboard
// renders per-corpus cache health inline as a list with individual
// build buttons, which is more informative than the aggregate chip.

interface EmptyStateProps {
  mode: GraphViewerMode;
  cacheWarming: string[];
  statuses: Record<string, api.CacheStatus>;
  rebuildingIds: Set<string>;
  onRebuild: (ids: string[]) => Promise<void>;
}

function EmptyDataState({
  mode,
  cacheWarming,
  statuses,
  rebuildingIds,
  onRebuild,
}: EmptyStateProps) {
  if (mode !== "brain") {
    return (
      <div className="text-center text-zinc-500 text-xs font-mono pointer-events-none">
        <div>no query result yet</div>
        <div className="text-zinc-700 mt-1">type a question below</div>
      </div>
    );
  }
  if (cacheWarming.length === 0) {
    return (
      <div className="text-center text-zinc-500 text-xs font-mono pointer-events-none">
        <div>no graph data</div>
        <div className="text-zinc-700 mt-1">
          the selected corpora have empty supernode overviews
        </div>
      </div>
    );
  }
  const missingIds = cacheWarming.filter((cid) => {
    const s = statuses[cid];
    return s && (s.metrics_cache === "missing" || s.domain_cache === "missing");
  });
  const warmingIds = cacheWarming.filter((cid) => {
    const s = statuses[cid];
    return s && s.metrics_cache !== "missing" && s.domain_cache !== "missing";
  });
  const rebuildingHere = cacheWarming.filter((cid) => rebuildingIds.has(cid));
  const buildable = cacheWarming.filter((cid) => !rebuildingIds.has(cid));
  return (
    <div className="text-center text-zinc-300 text-xs font-mono max-w-xl px-6 space-y-2 pointer-events-auto">
      <div className="text-zinc-200 text-sm mb-3">
        Analytics cache not ready for{" "}
        <span className="text-amber-300">{cacheWarming.length}</span> corpor
        {cacheWarming.length === 1 ? "us" : "a"}
      </div>
      {missingIds.length > 0 && (
        <div className="text-zinc-400">
          <span className="text-rose-300">{missingIds.length} never built</span>{" "}
          — Polymath warms the analytics cache automatically at the end of
          ingestion, but these corpora were ingested before that hook landed
          (or never finished). Rebuild manually below.
        </div>
      )}
      {warmingIds.length > 0 && (
        <div className="text-zinc-400">
          <span className="text-amber-300">{warmingIds.length} stale</span> —
          new docs were ingested since the last cache build. Rebuild to refresh.
        </div>
      )}
      {rebuildingHere.length > 0 && (
        <div className="text-violet-300">
          {rebuildingHere.length} currently rebuilding (this can take seconds
          for tiny corpora to several minutes for thousands of entities).
          Frontend polls every 15s; the canvas will populate when each
          finishes.
        </div>
      )}
      {buildable.length > 0 && (
        <button
          className="mt-3 text-[11px] uppercase tracking-widest text-amber-100 hover:text-amber-50 border border-amber-500/50 bg-amber-500/10 rounded px-3 py-1.5 font-mono"
          onClick={() => onRebuild(buildable)}
        >
          Build cache for {buildable.length} corpor{buildable.length === 1 ? "us" : "a"}
        </button>
      )}
      <div className="text-zinc-600 text-[10px] mt-2">
        Behind the scenes: services/graph/analytics.py:emerge_domains runs
        Louvain on document embeddings, computes PageRank + concept
        communities, then writes the result to graph_domain_cache and
        graph_metrics_cache.
      </div>
    </div>
  );
}
