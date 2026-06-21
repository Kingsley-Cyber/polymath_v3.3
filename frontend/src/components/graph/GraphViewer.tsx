/**
 * GraphViewer — premium redesign (Pt 7d).
 *
 * Three-zone layout:
 *   1) Top command strip (corpus pill, mode badge, live status, lane
 *      legend). One row above the canvas — the only piece of chrome
 *      that is NOT inside the right rail.
 *   2) Canvas (sigma) — research substrate with subtle grid + vignette,
 *      bottom-right control cluster (zoom/fit/pause-play), top-center
 *      hover pill. Always uses the right rail below.
 *   3) Right rail — Inspector/Composer/Output/Brain settings via
 *      BrainViewDashboard.
 *
 * The orchestration logic (multi-corpus fetch, cache warming, sigma
 * wiring, drill stack, follow-up context, agent queries) is unchanged
 * from the previous design — only the chrome around it is rebuilt.
 */

import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Activity,
  Loader2,
  Maximize2,
  Pause,
  Play,
  Search,
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
import {
  graphCategoryGenome,
  graphDocumentNodeColor,
  graphColors,
  graphGenomePropertyColor,
} from "../../lib/graph-colors";
import { LaneChip, RoleChip } from "../ui/Card";
import { Button } from "../ui/Button";
import {
  computeRoleContext,
  assignNodeRole,
} from "../../lib/role-adapter";

import type { GraphSynthesisMode } from "../../types/discover";

// Lazy-load the 3D atom so the default 2D bundle stays lean. The chunk
// is only fetched when the user toggles 3D or auto-transition kicks in.
const GraphAtom3D = lazy(() => import("./GraphAtom3D"));

// ─── Types ────────────────────────────────────────────────────────────────

export type GraphViewerMode = "brain" | "query";

interface GraphViewerProps {
  mode: GraphViewerMode;
  corpusIds: string[];
  query?: string;
  onRerun?: () => void;
  onQueryPhaseChange?: (phase: "idle" | "loading" | "ready" | "error") => void;
  /** Callback fired when the user picks a refined chip / entity in
   *  the Graph Query tab. Parent typically closes the modal and loads
   *  the text into the chat input. */
  onSendToChat?: (text: string) => void;
}

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

function softGraphLabel(value: string, max = 18): string {
  const cleaned = cleanContextText(value, max + 12)
    .replace(/\.(pdf|md|txt|docx?|pptx?|xlsx?)$/i, "")
    .replace(/[_-]+/g, " ")
    .trim();
  if (cleaned.length <= max) return cleaned;
  return `${cleaned.slice(0, Math.max(4, max - 3)).trim()}...`;
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
      if (drill) {
        const DRILL_ENTITY_LIMIT = 48;
        const DRILL_CHUNK_LIMIT = 18;
        const DRILL_BRIDGE_LIMIT = 32;
        const DRILL_RELATION_LIMIT = 90;
        const drillRes = await api.getBookDrilldown(
          drill.docId,
          corpusIds,
          DRILL_ENTITY_LIMIT,
          DRILL_CHUNK_LIMIT,
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
              label: cleanBookLabel(anchorRaw) || anchorRaw,
              mention_count: drillRes.anchor.chunk_count || 1,
              kind: "book" as const,
              source_corpora: [drillRes.anchor.corpus_id],
              source_corpus: drillRes.anchor.corpus_id,
              graph_cluster_key: `doc:${drill.docId}`,
              is_cluster_anchor: true,
              forceLabel: true,
            }
          : null;

        const chunkRecords = drillRes.local_chunks || [];
        const chunkOffsetById = new Map<string, { x: number; y: number }>();
        const entityAnchorOffsets = new Map<string, { x: number; y: number }>();
        const JELLY_CHUNK_R = 82;
        const JELLY_ENTITY_R = 38;
        const goldenAngle = Math.PI * (3 - Math.sqrt(5));
        const chunkNodes = chunkRecords.map((chunk, idx) => {
          const ring = Math.floor(idx / 12);
          const angle = idx * goldenAngle;
          const radius = JELLY_CHUNK_R + ring * 44;
          const x = Math.cos(angle) * radius;
          const y = Math.sin(angle) * radius * 0.82;
          const chunkNodeId = `chunk:${chunk.chunk_id}`;
          chunkOffsetById.set(chunk.chunk_id, { x, y });
          const topNames = (chunk.top_entity_names || []).filter(Boolean);
          const topIds = (chunk.top_entity_ids || []).filter(Boolean);
          topIds.forEach((entityId, entityIdx) => {
            if (entityAnchorOffsets.has(entityId)) return;
            const tipAngle =
              angle +
              ((entityIdx + 1) / Math.max(topIds.length + 1, 2)) * Math.PI * 0.78 -
              Math.PI * 0.39;
            entityAnchorOffsets.set(entityId, {
              x: x + Math.cos(tipAngle) * JELLY_ENTITY_R,
              y: y + Math.sin(tipAngle) * JELLY_ENTITY_R,
            });
          });
          const chunkLabel = `Chunk ${idx + 1}`;
          const chunkSummary = topNames.slice(0, 3).join(" / ");
          return {
            id: chunkNodeId,
            display_name: chunkSummary
              ? `${chunkLabel}: ${chunkSummary}`
              : chunkLabel,
            label: chunkLabel,
            entity_type: "Document",
            primary_entity_type: "Document",
            source_corpus: chunk.corpus_id || drillRes.anchor?.corpus_id || "",
            source_corpora: [chunk.corpus_id || drillRes.anchor?.corpus_id || ""],
            graph_cluster_key: `doc:${drill.docId}`,
            primary_doc_id: drill.docId,
            mention_count: Math.max(1, Number(chunk.entity_count || 1)),
            forceLabel: idx < 8,
            chunk_id: chunk.chunk_id,
            top_entities: topNames,
            x,
            y,
          };
        });
        const chunkEntityIds = new Set<string>();
        chunkRecords.forEach((chunk) => {
          (chunk.top_entity_ids || []).forEach((entityId) => {
            if (entityId) chunkEntityIds.add(entityId);
          });
        });

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
          graph_cluster_key: `doc:${drill.docId}`,
          primary_doc_id: drill.docId,
          mention_count: 1,
          ...(entityAnchorOffsets.get(e.entity_id) || {}),
        }));

        const bridgeNodes = drillRes.cross_book_bridges.map((b) => ({
          id: b.bridge_entity_id,
          display_name: b.bridge_entity_name || b.bridge_entity_id,
          entity_type: b.bridge_entity_type || "",
          mention_count: b.strength || 1,
        }));

        const chunkEdges = chunkNodes.map((chunk) => ({
          source: chunk.id,
          target: `book:${drill.docId}`,
          predicate: "has_chunk",
          relation_family: "Structural",
          confidence: 1,
          weight: 0.62,
        }));
        const entityChunkEdges = chunkRecords.flatMap((chunk) => {
          const chunkNodeId = `chunk:${chunk.chunk_id}`;
          return (chunk.top_entity_ids || [])
            .filter((entityId) => entityId && entityNodes.some((e) => e.id === entityId))
            .map((entityId) => ({
              source: entityId,
              target: chunkNodeId,
              predicate: "mentioned_in",
              relation_family: "Structural",
              confidence: 0.92,
              weight: 0.34,
            }));
        });
        const memberEdges = entityNodes
          .filter((e) => !chunkEntityIds.has(e.id))
          .map((e) => ({
          source: e.id,
          target: `book:${drill.docId}`,
          predicate: "in_book",
          confidence: 1,
          weight: 0.22,
        }));
        const intraEdges = drillRes.local_relations.slice(0, DRILL_RELATION_LIMIT).map((r) => ({
          source: r.source_id,
          target: r.target_id,
          predicate: r.predicate,
          relation_family: r.relation_family,
          confidence: r.confidence,
        }));
        const visibleCrossBookBridges = drillRes.cross_book_bridges.slice(
          0,
          DRILL_BRIDGE_LIMIT,
        );
        const bridgeEdges = visibleCrossBookBridges.flatMap((b) => [
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
        const targetAnchorNodes = visibleCrossBookBridges.map((b) => {
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
          ...chunkNodes,
          ...entityNodes,
          ...bridgeNodes,
          ...targetAnchorNodes,
        ];
        const seen = new Set<string>();
        const dedupedNodes = nodes.filter((n) => {
          if (seen.has(n.id)) return false;
          seen.add(n.id);
          return true;
        });

        setData({
          nodes: dedupedNodes,
          links: [
            ...chunkEdges,
            ...entityChunkEdges,
            ...memberEdges,
            ...intraEdges,
            ...bridgeEdges,
          ],
        });
        setCacheWarming([]);
        return;
      }

      const bv = await api.getBrainView(corpusIds);
      if ((bv.meta as Record<string, unknown> | undefined)?.warming) {
        window.setTimeout(() => {
          void reload();
        }, 20000);
      }
      const sortedDocs = [...bv.documents].sort(
        (a, b) => (b.bridge_count || 0) - (a.bridge_count || 0),
      );
      const SPOTLIGHT_COUNT = 220;
      const TENTACLE_CAP = 5;
      const SAT_ORBIT_R = 32;
      const SOFT_DOC_LABEL_COUNT = 3;
      const SOFT_ENTITY_LABEL_COUNT = 10;
      let softEntityLabelsUsed = 0;

      const anchorNodes: any[] = [];
      const satelliteNodes: any[] = [];
      const satelliteEdges: any[] = [];

      sortedDocs.forEach((d, idx) => {
        const rawLabel = d.label || d.filename || d.doc_id.slice(0, 8);
        const bookId = `book:${d.doc_id}`;
        // Corpus overview uses a jellyfish grammar: each document owns its
        // local visual family. New ingests automatically fall into this
        // structure because every document emitted by /graph/brain-view gets
        // a unique doc cluster and bounded entity tentacles.
        const graphClusterKey = `doc:${d.doc_id}`;
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
        const genome = graphCategoryGenome(
          topEntities.map((entity) => ({
            category:
              entity.primary_entity_type ||
              entity.entity_type ||
              entity.canonical_family ||
              "Other",
            weight: entity.mention_count || 1,
          })),
          `${d.corpus_id}:${d.doc_id}`,
        );
        const docLabel = softGraphLabel(cleanBookLabel(rawLabel) || rawLabel, 20);
        const shouldSoftLabelDoc = idx < SOFT_DOC_LABEL_COUNT;
        anchorNodes.push({
          id: bookId,
          display_name: rawLabel,
          label: docLabel || cleanBookLabel(rawLabel) || rawLabel,
          mention_count: Math.max(1, d.chunk_count || d.actual_chunk_count || 1),
          kind: "book" as const,
          source_corpora: [d.corpus_id],
          source_corpus: d.corpus_id,
          graph_cluster_key: graphClusterKey,
          is_cluster_anchor: true,
          visual_size: 1.38,
          visual_mass: 0.32,
          visual_color: graphDocumentNodeColor(),
          visual_glow: true,
          visual_glow_strength: 0.46,
          visual_category_genome: genome.signature,
          visual_dominant_category: genome.dominant,
          // Overview should read like Obsidian: all documents are visible
          // as dots, names appear through hover/selection/drill instead of
          // carpeting the atlas.
          forceLabel: shouldSoftLabelDoc,
          dominant_family: d.dominant_family,
          dominant_entity_type: d.dominant_entity_type,
          ghost_b_success_rate: d.ghost_b_success_rate,
          ghost_b_extracted: d.ghost_b_extracted,
          ghost_b_total: d.ghost_b_total,
          chunk_count: d.chunk_count,
          parent_count: d.parent_count,
          bridge_count: d.bridge_count,
          filename: d.filename,
          entity_count: d.entity_count,
        });

        if (idx < SPOTLIGHT_COUNT) {
          const satCount = topEntities.length;
          topEntities.slice(0, TENTACLE_CAP).forEach((entity, i) => {
            const name = entity.name || entity.entity_id || "";
            if (!name) return;
            const angle =
              (i / Math.max(Math.min(satCount, TENTACLE_CAP), 1)) * Math.PI * 2 +
              idx * 0.7;
            const entityId = `ent:${d.doc_id}:${i}`;
            const shouldSoftLabelEntity =
              softEntityLabelsUsed < SOFT_ENTITY_LABEL_COUNT &&
              idx < SPOTLIGHT_COUNT &&
              (i === 0 || (idx < 18 && i === 1));
            if (shouldSoftLabelEntity) softEntityLabelsUsed += 1;
            satelliteNodes.push({
              id: entityId,
              display_name: name,
              label: softGraphLabel(name, 18),
              entity_type: entity.entity_type || "",
              primary_entity_type: entity.primary_entity_type ?? null,
              definitional_phrase: entity.definitional_phrase ?? null,
              observed_entity_types: entity.observed_entity_types ?? null,
              canonical_family: entity.canonical_family ?? null,
              confidence: entity.confidence ?? null,
              source_corpus: d.corpus_id,
              source_corpora: [d.corpus_id],
              graph_cluster_key: graphClusterKey,
              primary_doc_id: d.doc_id,
              mention_count: entity.mention_count ?? 1,
              visual_size: 2.1,
              visual_mass: 0.2,
              visual_color: graphGenomePropertyColor(
                entity.primary_entity_type || entity.entity_type || entity.canonical_family,
                genome,
                `${d.corpus_id}:${d.doc_id}:${name}:${i}`,
              ),
              forceLabel: shouldSoftLabelEntity,
              x: Math.cos(angle) * SAT_ORBIT_R,
              y: Math.sin(angle) * SAT_ORBIT_R,
            });
            satelliteEdges.push({
              source: bookId,
              target: entityId,
              predicate: "contains",
              relation_family: "Structural",
              weight: 0.02,
              visual_scaffold: true,
            });
          });
        }
      });

      const bridgeLinks = bv.bridges.map((b) => ({
        source: `book:${b.source}`,
        target: `book:${b.target}`,
        predicate: "bridges_to",
        weight: b.strength,
        confidence: Math.min(1, (b.shared_entities || 0) / 12),
        dominant_relation_family: b.dominant_relation_family,
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
  webGroundingEnabled: boolean = false,
) {
  const graphQuerySeedEntities = useSettingsStore(
    (state) => state.graphQuerySeedEntities,
  );
  const graphQueryMaxHops = useSettingsStore((state) => state.graphQueryMaxHops);
  const graphQueryNodeLimit = useSettingsStore(
    (state) => state.graphQueryNodeLimit,
  );
  const webFetchDepth = useSettingsStore((state) => state.webFetchDepth);
  const webMaxSources = useSettingsStore((state) => state.webMaxSources);
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
          web_search_enabled: webGroundingEnabled,
          web_fetch_depth: webGroundingEnabled ? webFetchDepth : undefined,
          web_max_results: webGroundingEnabled
            ? Math.max(1, Math.min(10, webMaxSources || 5))
            : undefined,
        } as any);
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
    webGroundingEnabled,
    webFetchDepth,
    webMaxSources,
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
  const [colorMode, setColorMode] = useState<ColorMode>("entity_type");
  const [drillStack, setDrillStack] = useState<DrillFrame[]>([]);
  const [hoveredName, setHoveredName] = useState<string | null>(null);
  const [dashboardCollapsed, setDashboardCollapsed] = useState(false);
  const [minBridgeStrength, setMinBridgeStrength] = useState(2);
  const [maxBridgesPerBook, setMaxBridgesPerBook] = useState(0);
  const [settleAfterDrag, setSettleAfterDrag] = useState(false);
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
  const [draftSynthesisMode, setDraftSynthesisMode] =
    useState<GraphSynthesisMode>("research");
  const [executedSynthesisMode, setExecutedSynthesisMode] =
    useState<GraphSynthesisMode>("research");
  const [draftValidateSynthesis, setDraftValidateSynthesis] =
    useState<boolean>(false);
  const [executedValidateSynthesis, setExecutedValidateSynthesis] =
    useState<boolean>(false);
  const [draftWebGrounding, setDraftWebGrounding] = useState<boolean>(false);
  const [executedWebGrounding, setExecutedWebGrounding] = useState<boolean>(false);
  // 3D atom view mode — "2d" is the default sigma canvas, "3d" swaps in
  // the lazy-loaded GraphAtom3D scene consuming the same `data` payload.
  const [viewMode, setViewMode] = useState<"2d" | "3d">("2d");
  const lastAutoTransitionFingerprintRef = useRef<string | null>(null);
  const drillStackRef = useRef(drillStack);
  drillStackRef.current = drillStack;

  const drill = drillStack.length ? drillStack[drillStack.length - 1] : null;
  const brain = useBrainGraph(
    mode === "brain" ? corpusIds : [],
    drill,
  );
  const q = useQueryGraph(
    corpusIds,
    agentQuery,
    agentSynthesisQuery,
    executedSynthesisMode,
    executedValidateSynthesis,
    executedWebGrounding,
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
      setExecutedWebGrounding(draftWebGrounding);
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
      draftWebGrounding,
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

  // Auto-transition 2D → 3D when a heavy query completes. Keyed on a
  // stable fingerprint of the query so re-renders without new data
  // don't re-fire the reveal. The user can always flip back to 2D via
  // the toggle in the top command strip — we never auto-return.
  useEffect(() => {
    if (effectiveMode !== "query") return;
    if (!agentQuery?.trim() || q.phase !== "ready") return;
    if (!data || (data.nodes?.length ?? 0) === 0) return;
    const fingerprint = `${agentQuery.trim()}|${executedSynthesisMode}|${corpusIds.join(",")}`;
    if (lastAutoTransitionFingerprintRef.current === fingerprint) return;
    lastAutoTransitionFingerprintRef.current = fingerprint;
    setViewMode("3d");
  }, [effectiveMode, agentQuery, q.phase, q.synthesis?.markdown, data, executedSynthesisMode, corpusIds]);

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

  useEffect(() => {
    const sigmaInst = sigma.sigmaRef.current;
    const g = (sigmaInst as any)?.getGraph?.();
    if (!sigmaInst || !g || g.order === 0 || !data) return;
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
      <div className="flex h-full w-full items-center justify-center bg-[var(--bg-base)] text-content-secondary">
        <div className="max-w-sm px-6 text-center">
          <div className="mb-3 inline-flex h-12 w-12 items-center justify-center rounded-full border border-border-minimal bg-[var(--bg-raised)]">
            <Search className="h-5 w-5 text-content-tertiary" />
          </div>
          <div className="text-base font-semibold text-content-primary">
            Select a corpus to begin
          </div>
          <div className="mt-1.5 text-[12px] leading-relaxed text-content-tertiary font-mono">
            Tip: select multiple corpora to see cross-book connections.
          </div>
        </div>
      </div>
    );
  }

  // Header strip data
  const nodeCount = data?.nodes.length ?? 0;
  const linkCount = data?.links.length ?? 0;
  const headerMode =
    effectiveMode === "query" ? "Query" : "Corpora view";
  const headerPhase = effectiveMode === "brain"
    ? brain.loading ? "loading"
      : brain.error ? "error"
      : data ? "ready"
      : "idle"
    : activeQ.phase;

  const phaseTone =
    headerPhase === "ready"
      ? "success"
      : headerPhase === "loading"
        ? "warning"
        : headerPhase === "error"
          ? "error"
          : "neutral";

  return (
    <div className="graph-surface relative flex h-full w-full flex-col bg-[var(--bg-base)]">
      {/* ── Top command strip ──────────────────────────────────────── */}
      <header className="relative z-20 flex shrink-0 items-center justify-between gap-3 border-b border-border-minimal bg-[var(--bg-raised)]/95 px-3 py-2 backdrop-blur">
        <div className="flex min-w-0 items-center gap-2">
          <div className="flex items-center gap-1.5">
            {effectiveMode === "query" ? (
              <Search className="h-3.5 w-3.5 text-accent-main" />
            ) : (
              <Activity className="h-3.5 w-3.5 text-content-secondary" />
            )}
            <span className="text-[10px] font-mono uppercase tracking-[0.2em] text-content-tertiary">
              {headerMode}
            </span>
          </div>
          <span className="text-content-tertiary">·</span>
          <div className="flex min-w-0 items-center gap-1.5 font-mono text-[11px] text-content-secondary">
            <span className="tabular-nums text-content-primary">
              {corpusIds.length} {corpusIds.length === 1 ? "corpus" : "corpora"}
            </span>
            <span className="text-content-tertiary">·</span>
            <span className="tabular-nums text-content-tertiary">
              {nodeCount}n · {linkCount}e
            </span>
          </div>
        </div>

        <div className="flex items-center gap-1.5">
          <span className="hidden items-center gap-1 md:inline-flex">
            <LaneChip lane="corpus" label="corpus" />
          </span>
          <span className="hidden items-center gap-1 md:inline-flex">
            <LaneChip lane="graph" label="graph" />
          </span>
          {effectiveMode === "query" && (
            <span className="hidden items-center gap-1 lg:inline-flex">
              <RoleChip role="query_matched" />
              <RoleChip role="anchor" />
              <RoleChip role="bridge" />
              <RoleChip role="gap" />
            </span>
          )}
          <span
            className="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-[0.18em]"
            style={{
              borderColor:
                phaseTone === "success"
                  ? "rgba(16, 185, 129, 0.45)"
                  : phaseTone === "warning"
                    ? "rgba(245, 158, 11, 0.45)"
                    : phaseTone === "error"
                      ? "rgba(239, 68, 68, 0.45)"
                      : "var(--border-subtle)",
              background:
                phaseTone === "success"
                  ? "rgba(16, 185, 129, 0.10)"
                  : phaseTone === "warning"
                    ? "rgba(245, 158, 11, 0.10)"
                    : phaseTone === "error"
                      ? "rgba(239, 68, 68, 0.10)"
                      : "var(--bg-base)",
              color:
                phaseTone === "success"
                  ? "#6ee7b7"
                  : phaseTone === "warning"
                    ? "#fcd34d"
                    : phaseTone === "error"
                      ? "#fca5a5"
                      : "var(--text-tertiary, #94a3b8)",
            }}
          >
            {headerPhase === "loading" && (
              <Loader2 className="h-2.5 w-2.5 animate-spin" />
            )}
            <span>{headerPhase}</span>
          </span>
          {/* View-mode toggle: 2D sigma map vs lazy-loaded 3D atom. */}
          <div
            className="inline-flex items-center rounded-md p-0.5"
            style={{
              background: "var(--bg-base)",
              border: "1px solid var(--border-subtle)",
            }}
            role="tablist"
            aria-label="Canvas view mode"
          >
            <Button
              variant={viewMode === "2d" ? "primary" : "ghost"}
              size="sm"
              active={viewMode === "2d"}
              aria-pressed={viewMode === "2d"}
              onClick={() => setViewMode("2d")}
            >
              2D Map
            </Button>
            <Button
              variant={viewMode === "3d" ? "primary" : "ghost"}
              size="sm"
              active={viewMode === "3d"}
              aria-pressed={viewMode === "3d"}
              onClick={() => setViewMode("3d")}
            >
              3D Atom
            </Button>
          </div>
        </div>
      </header>

      {/* ── Body row: canvas + right rail ──────────────────────────── */}
      <div className="relative flex min-h-0 flex-1 flex-col md:flex-row">
        {/* Canvas column */}
        <div className="relative flex-1 min-h-0 min-w-0">
          {/* Research substrate: subtle vignette. */}
          <div
            className="pointer-events-none absolute inset-0 z-0"
            style={{ background: graphColors.substrate.vignette }}
          />
          {/* Subtle research grid (CSS — Sigma paints on its own canvas). */}
          <div
            className="pointer-events-none absolute inset-0 z-0"
            style={{
              backgroundImage:
                "linear-gradient(to right, rgba(148,163,184,0.04) 1px, transparent 1px), linear-gradient(to bottom, rgba(148,163,184,0.04) 1px, transparent 1px)",
              backgroundSize: "64px 64px",
            }}
          />

          {/* Hovered node tooltip — centered under the strip. */}
          {hoveredName && !selectedId && (
            <div className="pointer-events-none absolute top-3 left-1/2 z-20 -translate-x-1/2 rounded-md border border-border-minimal bg-[var(--bg-raised)]/95 px-3 py-1.5 backdrop-blur">
              <span className="font-mono text-sm text-content-primary">
                {hoveredName}
              </span>
            </div>
          )}

          {/* Loading / error / empty overlays share the same center stage. */}
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center pointer-events-none gap-3">
            {loading && !error && (
              <div className="pointer-events-auto flex items-center gap-2.5 rounded-md border border-border-minimal bg-[var(--bg-raised)]/90 px-3 py-2 text-[11px] font-mono text-content-secondary backdrop-blur">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-accent-main" />
                <span>
                  {effectiveMode === "query"
                    ? heavyQueryActive
                      ? "Synthesizing across corpora..."
                      : "Building question graph..."
                    : "Loading graph..."}
                </span>
              </div>
            )}

            {error && (
              <div className="pointer-events-auto max-w-md rounded-md border border-error/40 bg-[var(--bg-raised)]/95 px-4 py-3 text-[13px] backdrop-blur">
                <div className="mb-1 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-error">
                  <span className="h-2 w-2 rounded-full bg-error" />
                  Graph error
                </div>
                <p className="leading-relaxed text-content-secondary">{error}</p>
              </div>
            )}

            {data && data.nodes.length === 0 && !loading && (
              <EmptyDataState
                mode={effectiveMode}
                cacheWarming={effectiveMode === "brain" ? brain.cacheWarming : []}
                statuses={effectiveMode === "brain" ? brain.cacheStatuses : {}}
                rebuildingIds={effectiveMode === "brain" ? brain.rebuildingIds : new Set()}
                onRebuild={effectiveMode === "brain" ? brain.triggerRebuild : async () => {}}
              />
            )}
          </div>

          {/* Sigma canvas */}
          <div className="absolute inset-0 flex z-0">
            <div className="relative flex-1 min-w-0">
              <div
                ref={sigma.containerRef}
                className="absolute inset-0 z-10 cursor-grab active:cursor-grabbing"
              />
            </div>
          </div>

          {/* 3D Atom overlay — lazy-loaded; renders only when viewMode==='3d'
              and there's data. Uses the SAME nodes/links the 2D canvas
              has; never re-queries retrieval. Capped at 150n/350e inside
              GraphAtom3D. */}
          {viewMode === "3d" && data && data.nodes.length > 0 && (
            <div className="absolute inset-0 z-20 pointer-events-auto">
              <Suspense
                fallback={
                  <div
                    className="flex h-full w-full items-center justify-center"
                    style={{
                      color: "var(--ink-tertiary)",
                      fontFamily: "var(--font-mono)",
                      fontSize: "var(--type-sm)",
                    }}
                  >
                    Booting 3D atom…
                  </div>
                }
              >
                <GraphAtom3D
                  nodes={(() => {
                    const ctx = computeRoleContext(
                      data.nodes,
                      data.links,
                      activeQ.seedIds,
                      activeQ.hubIds,
                      activeQ.bridgeIds,
                      q.gaps ?? [],
                    );
                    return data.nodes.map((n: any) => ({
                      id: String(n.id),
                      label: String(n.display_name || n.label || n.id),
                      role: assignNodeRole(n, ctx).role,
                      weight: Number(n.mention_count ?? n.total_mentions ?? 1),
                    }));
                  })()}
                  links={data.links.map((l: any) => {
                    const source = String(
                      l.source && typeof l.source === "object"
                        ? (l.source as any).id ?? (l.source as any).key
                        : l.source,
                    );
                    const target = String(
                      l.target && typeof l.target === "object"
                        ? (l.target as any).id ?? (l.target as any).key
                        : l.target,
                    );
                    return {
                      source,
                      target,
                      kind:
                        l.predicate === "bridges_to"
                          ? "bridges"
                          : l.relation_family === "Structural" ||
                              l.predicate === "in_book"
                            ? "mentions"
                            : "supports",
                      label: String(l.predicate || "related_to"),
                      family: l.dominant_relation_family || l.relation_family || null,
                      confidence:
                        typeof l.confidence === "number" ? l.confidence : null,
                      weight:
                        typeof l.weight === "number"
                          ? l.weight
                          : typeof l.shared_entities === "number"
                            ? l.shared_entities
                            : null,
                      sourceLabel:
                        l.source_label ||
                        data.nodes.find((n: any) => String(n.id) === source)?.display_name,
                      targetLabel:
                        l.target_label ||
                        data.nodes.find((n: any) => String(n.id) === target)?.display_name,
                    };
                  })}
                  selectedNodeId={selectedId}
                  onSelectNode={sigma.setSelectedNode}
                  headline={q.synthesis?.headline || (effectiveMode === "query" ? agentInput : undefined)}
                  synthesisHeadline={q.synthesis?.markdown?.slice(0, 320)}
                />
              </Suspense>
            </div>
          )}

          {/* Bottom-right control cluster. */}
          <div className="absolute right-3 bottom-3 z-20 flex flex-col gap-1.5 pointer-events-auto md:right-4 md:bottom-4">
            <ToolButton onClick={sigma.zoomIn} title="Zoom in" icon={<ZoomIn className="h-4 w-4" />} />
            <ToolButton onClick={sigma.zoomOut} title="Zoom out" icon={<ZoomOut className="h-4 w-4" />} />
            <ToolButton onClick={sigma.resetZoom} title="Fit to screen" icon={<Maximize2 className="h-4 w-4" />} />
            <div className="my-0.5 h-px bg-border-minimal/60" />
            <ToolButton
              onClick={sigma.isLayoutRunning ? sigma.stopLayout : sigma.startLayout}
              title={sigma.isLayoutRunning ? "Pause layout" : "Resume layout"}
              icon={sigma.isLayoutRunning ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
              active={sigma.isLayoutRunning}
            />
          </div>

          {/* Layout running indicator. */}
          {sigma.isLayoutRunning && (
            <div className="absolute bottom-3 left-1/2 z-10 flex -translate-x-1/2 items-center gap-2 rounded-full border border-accent-main/30 bg-[var(--bg-raised)]/90 px-3 py-1.5 backdrop-blur md:bottom-4">
              <div className="h-2 w-2 animate-ping rounded-full bg-accent-main" />
              <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-content-secondary">
                Layout optimizing…
              </span>
            </div>
          )}
        </div>

        {/* Right rail */}
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
          webGroundingEnabled={draftWebGrounding}
          onWebGroundingChange={setDraftWebGrounding}
          followUpAvailable={lastGraphContext !== null}
          followUpPreview={lastGraphContext?.coreIdea || lastGraphContext?.query || ""}
          onBuildQuestionGraph={runQuestionGraph}
          agentPhase={q.phase}
          agentError={q.error}
          agentSynthesisMarkdown={q.synthesis?.markdown ?? null}
          agentProgressSteps={q.progressSteps}
          questionPhase={lightQ.phase}
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
    </div>
  );
}

export default GraphViewer;

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
      <div className="pointer-events-auto max-w-sm px-4 text-center font-mono text-content-tertiary">
        <div className="mb-1 text-[13px] text-content-secondary">
          No query result yet
        </div>
        <div className="text-[11px]">
          Type a question in the Graph Query panel
        </div>
      </div>
    );
  }
  if (cacheWarming.length === 0) {
    return (
      <div className="pointer-events-auto max-w-sm px-4 text-center font-mono text-content-tertiary">
        <div className="mb-1 text-[13px] text-content-secondary">
          No graph data
        </div>
        <div className="text-[11px]">
          The selected corpora have empty supernode overviews
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
    <div className="pointer-events-auto max-w-lg rounded-md border border-border-minimal bg-[var(--bg-raised)]/95 p-4 font-mono text-[11px] backdrop-blur">
      <div className="mb-3 text-[13px] font-semibold tracking-wide text-content-primary">
        Analytics cache not ready for{" "}
        <span className="text-accent-main">{cacheWarming.length}</span> corpor
        {cacheWarming.length === 1 ? "us" : "a"}
      </div>
      <div className="space-y-2 leading-relaxed text-content-secondary">
        {missingIds.length > 0 && (
          <div>
            <span className="text-error">{missingIds.length} never built</span>{" "}
            — cache was never generated for these corpora. Rebuild manually below.
          </div>
        )}
        {warmingIds.length > 0 && (
          <div>
            <span className="text-warning">{warmingIds.length} stale</span> —
            new docs were ingested since the last cache build. Rebuild to refresh.
          </div>
        )}
        {rebuildingHere.length > 0 && (
          <div className="text-accent-main">
            {rebuildingHere.length} currently rebuilding. The canvas will populate when each finishes.
          </div>
        )}
      </div>
      {buildable.length > 0 && (
        <button
          className="btn-primary mt-3 text-[11px]"
          onClick={() => onRebuild(buildable)}
        >
          Build cache for {buildable.length} corpor{buildable.length === 1 ? "us" : "a"}
        </button>
      )}
    </div>
  );
}

function ToolButton({
  onClick,
  title,
  icon,
  active,
}: {
  onClick: () => void;
  title: string;
  icon: React.ReactNode;
  active?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-label={title}
      className={`flex h-9 w-9 items-center justify-center rounded-md border border-border-minimal bg-[var(--bg-raised)] text-content-secondary transition-colors hover:text-content-primary ${
        active
          ? "border-accent-main text-accent-main"
          : "hover:border-content-secondary"
      }`}
    >
      {icon}
    </button>
  );
}
