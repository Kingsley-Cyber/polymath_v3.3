// GraphView.tsx — Phase K WebGL rewrite
// Replaces the react-force-graph-2d canvas renderer with sigma.js (WebGL) +
// graphology for data structure + Louvain community detection + ForceAtlas2
// layout. Scales smoothly to ~20k nodes / 60k edges on a modest GPU.
//
// The prior implementation (Agent Query, discourse mode, split mode) is
// preserved in GraphView.legacy.tsx for reference while the rewrite stabilizes.
//
// Data source: GET /api/corpora/{corpus_id}/graph/full by default;
// the cached map overview remains available behind the explicit map switch.
// Community detection: graphology-communities-louvain (client-side)
// Layout: graphology-layout-forceatlas2 (CPU, Barnes-Hut O(n log n))
// Rendering: sigma v3 (WebGL instanced rendering)

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  X,
  Search,
  Maximize2,
  RefreshCw,
  Info,
  Sparkles,
  MessageSquare,
  Layers,
  GitFork,
  HelpCircle,
  TrendingUp,
} from "lucide-react";
import Graph from "graphology";
import { SigmaContainer, useLoadGraph, useSigma, useRegisterEvents } from "@react-sigma/core";
import louvain from "graphology-communities-louvain";
import forceAtlas2 from "graphology-layout-forceatlas2";
import "@react-sigma/core/lib/style.css";
import * as api from "../../lib/api";
import {
  contextGraphFromDiscoverResponse,
  contextGraphFromOverviewResponse,
} from "../../lib/contextGraph";
import { useSettingsStore } from "../../stores/settingsStore";
import { useGraphSessionStore } from "../../stores/graphSessionStore";
import type { FullGraphResponse } from "../../lib/api";
import type { DiscourseGraphResponse } from "../../types";
import type { ContextGraphPayload, DiscoverGraphLink, DiscoverGraphNode } from "../../types";

// Obsidian-like palette: muted graph field first, color only where structure
// earns it. Communities beyond the list wrap around.
const COMMUNITY_PALETTE = [
  "#d0c7a7",
  "#87b6a7",
  "#8fa8c8",
  "#b79ac8",
  "#c28f7e",
  "#aeb4bb",
  "#b6b27d",
  "#7fb3bf",
  "#a6967a",
  "#98a0c8",
  "#c4a36d",
  "#8aa889",
];

// Maps the DomainColor keys used by graphSessionStore (Tailwind color names)
// to concrete hex codes so sigma can render them. Kept in sync with the
// palette in graphSessionStore.ts.
const DOMAIN_HEX: Record<string, string> = {
  violet: "#a78bfa",
  amber: "#fbbf24",
  rose: "#fb7185",
  teal: "#2dd4bf",
  indigo: "#818cf8",
  sage: "#a3e635",
  sky: "#38bdf8",
  coral: "#fb923c",
  olive: "#ca8a04",
  plum: "#e879f9",
  slate: "#94a3b8",
  stone: "#a8a29e",
};

// Ghosted fill for nodes that are outside the active discovery subgraph.
const DIMMED_NODE_COLOR = "rgba(55, 60, 68, 0.45)";
const DIMMED_EDGE_COLOR = "rgba(30, 34, 42, 0.12)";
const RELATION_FAMILY_EDGE_COLOR: Record<string, string> = {
  Structural: "rgba(255, 255, 255, 0.42)",
  Operational: "rgba(125, 211, 252, 0.44)",
  Referential: "rgba(167, 139, 250, 0.36)",
  Causal: "rgba(251, 191, 36, 0.48)",
  Conflict: "rgba(251, 113, 133, 0.58)",
  WeakAssociation: "rgba(148, 163, 184, 0.18)",
  Discourse: "rgba(210, 255, 171, 0.32)",
};

const OBSIDIAN_NODE = {
  group: "#a89466",
  query: "#aeb5b2",
  quiet: "#687075",
  bridge: "#d6a761",
  gap: "#596168",
  weak: "#a9666d",
  document: "#7da391",
};

const OBSIDIAN_EDGE = {
  membership: "rgba(142, 150, 150, 0.075)",
  context: "rgba(130, 138, 142, 0.10)",
  bridge: "rgba(214, 167, 97, 0.50)",
  document: "rgba(125, 163, 145, 0.15)",
  gap: "rgba(160, 168, 174, 0.08)",
  weak: "rgba(169, 102, 109, 0.30)",
};

const MISSION_CONTEXT_GRAPH_EVENT = "mission-control-context-graph";

interface GraphViewProps {
  onClose: () => void;
}

type GraphViewMode = "context" | "discourse" | "overview" | "raw" | "full";
type DiscourseLens = "topics" | "concepts" | "gaps" | "context";

const GRAPH_VIEW_MODES: {
  id: GraphViewMode;
  label: string;
  title: string;
  nodeCap: number;
  edgeCap: number;
}[] = [
  {
    id: "context",
    label: "context",
    title: "Context map: query concept neighborhoods plus unique evidence files",
    nodeCap: 140,
    edgeCap: 650,
  },
  {
    id: "discourse",
    label: "discourse",
    title: "Language map for exploring recurring terms and relationships",
    nodeCap: 140,
    edgeCap: 650,
  },
  {
    id: "overview",
    label: "kg map",
    title: "Show cached entity domains and concept neighborhoods",
    nodeCap: 80,
    edgeCap: 220,
  },
  {
    id: "raw",
    label: "entities",
    title: "Load a large entity graph for the selected corpus",
    nodeCap: 20000,
    edgeCap: 60000,
  },
  {
    id: "full",
    label: "full",
    title: "Load the largest visual corpus graph allowed by the backend",
    nodeCap: 50000,
    edgeCap: 200000,
  },
];

// ────────────────────────────────────────────────────────────────────────
// Inner graph loader — runs inside SigmaContainer and populates graphology
// state with nodes/edges, Louvain communities, degree sizing, and FA2 layout.
// ────────────────────────────────────────────────────────────────────────
function GraphLoader({
  data,
  onReady,
}: {
  data: FullGraphResponse;
  onReady: (stats: { communities: number; nodes: number; edges: number }) => void;
}) {
  const loadGraph = useLoadGraph();

  useEffect(() => {
    const graph = new Graph({ multi: false, type: "directed" });
    const isContextMap = data.view === "context";
    const topicNodes = data.nodes.filter(
      (n) => n.context_kind === "topic" || n.entity_type === "topic_island" || n.entity_type === "context_neighborhood",
    );
    const topicIndex = new Map(topicNodes.map((n, i) => [n.id, i]));
    const topicPosition = new Map<string, { x: number; y: number }>();
    const goldenAngle = Math.PI * (3 - Math.sqrt(5));
    topicNodes.forEach((node, i) => {
      const radius = 2.3 + Math.sqrt(i + 1) * 2.55;
      const angle = i * goldenAngle - Math.PI / 2;
      topicPosition.set(node.id, {
        x: Math.cos(angle) * radius,
        y: Math.sin(angle) * radius * 0.78,
      });
    });

    // 1. Add nodes with seed placement on a small grid (FA2 needs initial x/y).
    const side = Math.ceil(Math.sqrt(Math.max(1, data.nodes.length)));
    data.nodes.forEach((n, i) => {
      const isTopic = n.context_kind === "topic" || n.entity_type === "topic_island" || n.entity_type === "context_neighborhood";
      const role = n.context_role || "";
      const topicId = n.topic_id ? `topic:${n.topic_id}` : "";
      const parentTopic = topicPosition.get(topicId);
      let gx = (i % side) - side / 2;
      let gy = Math.floor(i / side) - side / 2;
      let size = 2;
      let color = "#666";
      let forceLabel = false;

      if (isContextMap) {
        if (isTopic) {
          const pos = topicPosition.get(n.id) || { x: gx, y: gy };
          gx = pos.x;
          gy = pos.y;
          size = 5.6 + Math.min(5.2, Math.sqrt(Math.max(1, n.evidence_count || n.mention_count || 1)) * 1.15);
          color = OBSIDIAN_NODE.group;
          forceLabel = i < 6 || Number(n.evidence_count || 0) > 2;
        } else {
          const topicSlot = topicIndex.get(topicId) ?? hashStringToInt(n.primary_domain || n.id) % Math.max(1, topicNodes.length || 1);
          const hash = hashStringToInt(n.id);
          const angle = ((hash % 360) / 360) * Math.PI * 2 + topicSlot * 0.19;
          const isDocument = role.includes("document") || n.entity_type === "evidence_document";
          const orbit = isDocument
            ? 5.2 + (hash % 5) * 0.35
            : 1.6 + Math.min(6.0, Math.sqrt(Math.max(1, n.context_weight || n.mention_count || 1)) * 0.72);
          gx = (parentTopic?.x || 0) + Math.cos(angle) * orbit;
          gy = (parentTopic?.y || 0) + Math.sin(angle) * orbit * 0.82;
          const base = isDocument ? 3.9 : 2.35;
          const roleBoost = role.includes("bridge") ? 2.7 : isDocument ? 1.2 : role.includes("gap") ? 0.2 : 0.65;
          size = base + roleBoost + Math.min(3.4, Math.sqrt(Math.max(1, n.context_weight || n.mention_count || 1)) * 0.72);
          color = role.includes("bridge")
            ? OBSIDIAN_NODE.bridge
            : role.includes("weak")
              ? OBSIDIAN_NODE.weak
              : role.includes("gap")
                ? OBSIDIAN_NODE.gap
                : isDocument
                  ? OBSIDIAN_NODE.document
                  : Number(n.context_weight || n.mention_count || 0) > 3
                    ? OBSIDIAN_NODE.query
                    : OBSIDIAN_NODE.quiet;
          forceLabel = role.includes("bridge") || isDocument || Number(n.context_weight || 0) > 7;
        }
      }

      graph.addNode(n.id, {
        label: !isContextMap || forceLabel ? n.display_name || n.id : "",
        searchLabel: n.display_name || n.id,
        entity_type: n.entity_type,
        mention_count: n.mention_count,
        object_kind: n.object_kind || "",
        object_kind_parent: n.object_kind_parent || "",
        object_kind_root: n.object_kind_root || "",
        domain_type: n.domain_type || "",
        domain_type_parent: n.domain_type_parent || "",
        domain_type_root: n.domain_type_root || "",
        canonical_family: n.canonical_family || "",
        ontology_version: n.ontology_version || "",
        supernode_type: n.supernode_type || "",
        primary_domain: n.primary_domain || "",
        top_entities: n.top_entities || [],
        bridge_count: n.bridge_count || 0,
        context_kind: n.context_kind || "",
        context_role: n.context_role || "",
        topic_id: n.topic_id || "",
        evidence_count: n.evidence_count || 0,
        context_weight: n.context_weight || 0,
        x: gx + (((hashStringToInt(n.id) % 100) / 100) - 0.5) * 0.18,
        y: gy + ((((hashStringToInt(n.id) >> 8) % 100) / 100) - 0.5) * 0.18,
        size,
        color,
        forceLabel,
      });
    });

    // 2. Add edges — de-duplicate (source, target) pairs to keep graph simple.
    const seen = new Set<string>();
    data.edges.forEach((e) => {
      if (!graph.hasNode(e.source) || !graph.hasNode(e.target)) return;
      if (e.source === e.target) return; // self-loops add noise
      const key = `${e.source}->${e.target}`;
      if (seen.has(key)) return;
      seen.add(key);
      try {
        const role = e.role || "";
        const suggested = Boolean(e.suggested);
        const contextColor = suggested
          ? OBSIDIAN_EDGE.gap
          : role.includes("bridge")
            ? OBSIDIAN_EDGE.bridge
            : role.includes("weak")
              ? OBSIDIAN_EDGE.weak
              : role.includes("document")
                ? OBSIDIAN_EDGE.document
                : role === "topic_overlay" || role === "query_neighborhood" || e.relation_family === "Discourse"
                  ? OBSIDIAN_EDGE.membership
                  : OBSIDIAN_EDGE.context;
        const contextSize = suggested
          ? 0.16
          : role.includes("bridge")
            ? 0.95
            : role.includes("document")
              ? 0.22
              : role === "topic_overlay" || role === "query_neighborhood"
                ? 0.16
                : 0.32;
        graph.addEdgeWithKey(key, e.source, e.target, {
          predicate: e.predicate,
          relation_family: e.relation_family || "WeakAssociation",
          confidence: e.confidence,
          weight: e.weight || 1,
          role,
          suggested,
          color: isContextMap
            ? contextColor
            : RELATION_FAMILY_EDGE_COLOR[e.relation_family || "WeakAssociation"] ||
              "rgba(168, 178, 195, 0.18)",
          size: isContextMap ? contextSize : 0.5,
        });
      } catch {
        /* dup — safe to skip */
      }
    });

    // 3. Louvain community detection — single pass, deterministic given seed.
    // Falls back gracefully on tiny graphs where Louvain produces 1 community.
    let communityCount = 0;
    if (!isContextMap && graph.size > 0 && graph.order > 1) {
      try {
        louvain.assign(graph, { nodeCommunityAttribute: "community" });
        const communities = new Set<number>();
        graph.forEachNode((_n, attrs) => {
          if (typeof attrs.community === "number") {
            communities.add(attrs.community);
          }
        });
        communityCount = communities.size;
      } catch (e) {
        console.warn("Louvain failed — falling back to entity_type coloring", e);
      }
    }

    // 4. Color nodes by community (palette wraps). Fallback to entity_type hash
    // if Louvain didn't produce distinct groups.
    if (!isContextMap) {
      graph.forEachNode((n, attrs) => {
        const c =
          typeof attrs.community === "number"
            ? attrs.community
            : hashStringToInt(attrs.primary_domain || attrs.entity_type || "other");
        graph.setNodeAttribute(n, "color", COMMUNITY_PALETTE[c % COMMUNITY_PALETTE.length]);
      });
    }

    // 5. Degree-centrality sizing. Raw degree → sqrt scale so hubs don't dwarf.
    let maxDeg = 1;
    graph.forEachNode((n) => {
      const deg = graph.degree(n);
      maxDeg = Math.max(maxDeg, deg);
    });
    if (!isContextMap) {
      graph.forEachNode((n) => {
        const deg = graph.degree(n);
        const normalized = Math.sqrt(deg / maxDeg); // 0..1
        graph.setNodeAttribute(n, "size", 3 + normalized * 12); // 3px..15px
      });
    }

    // 6. ForceAtlas2 layout. Large graphs need fewer iterations to keep the
    // full-corpus mode responsive enough to inspect.
    if (graph.order > 1) {
      const iterations = isContextMap ? 35 : graph.order < 500 ? 100 : graph.order < 10000 ? 200 : 90;
      forceAtlas2.assign(graph, {
        iterations,
        settings: {
          barnesHutOptimize: graph.order > 500,
          gravity: isContextMap ? 0.16 : 1.2,
          scalingRatio: isContextMap ? 24 : 8,
          strongGravityMode: false,
          slowDown: isContextMap ? 18 : 3,
        },
      });
    }

    loadGraph(graph);
    onReady({
      communities: communityCount,
      nodes: graph.order,
      edges: graph.size,
    });
  }, [data, loadGraph, onReady]);

  return null;
}

// Combines three overlays into the sigma node/edge reducers:
//   1. Discovery emphasis — recolor by domain + bump size for frontier /
//      transfer_hub / bridge_anchor / analogy_anchor; dim everything else.
//      Driven by graphSessionStore.activeDiscoverGraph, populated whenever
//      DiscoveryPanel pushes a new turn.
//   2. Search highlighting — matches on the client-side search box.
//   3. Hover ghosting — neighbor-focused reveal on node hover.
// Later layers win (hover > search > emphasis) so the user's active
// interaction always rises above the discovery coloring.
function InteractionLayer({
  searchTerm,
  corpusId,
  enableDiscoveryEmphasis = true,
}: {
  searchTerm: string;
  corpusId: string | null;
  enableDiscoveryEmphasis?: boolean;
}) {
  const sigma = useSigma();
  const registerEvents = useRegisterEvents();
  const [hovered, setHovered] = useState<string | null>(null);

  const activeDiscoverGraph = useGraphSessionStore((s) => s.activeDiscoverGraph);
  const activeCorpusId = useGraphSessionStore((s) => s.activeCorpusId);
  const getDomainColor = useGraphSessionStore((s) => s.getDomainColor);
  const openNodeNavigation = useGraphSessionStore((s) => s.openNodeNavigation);
  const scopedDiscoverGraph = activeCorpusId === corpusId ? activeDiscoverGraph : null;

  useEffect(() => {
    registerEvents({
      enterNode: (e) => setHovered(e.node),
      leaveNode: () => setHovered(null),
      clickNode: (e) => {
        const graph = sigma.getGraph();
        const label = graph.getNodeAttribute(e.node, "searchLabel") || graph.getNodeAttribute(e.node, "label") || e.node;
        openNodeNavigation(String(label), e.node);
      },
    });
  }, [openNodeNavigation, registerEvents, sigma]);

  // Precompute lookups from the active discover response so the reducer stays
  // O(1) per node/edge. Rebuilt only when the response changes.
  const { nodeEmphasisMap, edgeEmphasisMap } = useMemo(() => {
    const nodes = new Map<string, DiscoverGraphNode>();
    const edges = new Map<string, DiscoverGraphLink>();
    if (scopedDiscoverGraph) {
      for (const n of scopedDiscoverGraph.nodes) nodes.set(n.id, n);
      for (const l of scopedDiscoverGraph.links) {
        // Index both directions — the canvas stores directed edges but
        // discovery treats relations symmetrically for emphasis.
        edges.set(`${l.source}->${l.target}`, l);
        edges.set(`${l.target}->${l.source}`, l);
      }
    }
    return { nodeEmphasisMap: nodes, edgeEmphasisMap: edges };
  }, [scopedDiscoverGraph]);

  // In overview mode the base graph contains domains/concept supernodes, not
  // every entity. Inject the bounded Mission Control query neighborhood so
  // the canvas can still show the selected entities and their edges.
  useEffect(() => {
    const graph = sigma.getGraph();
    const injectedEdges: string[] = [];
    graph.forEachEdge((edge, attrs) => {
      if (attrs.overlayInjected) injectedEdges.push(edge);
    });
    for (const edge of injectedEdges) {
      if (graph.hasEdge(edge)) graph.dropEdge(edge);
    }

    const activeIds = new Set((scopedDiscoverGraph?.nodes || []).map((n) => n.id));
    const injectedNodes: string[] = [];
    graph.forEachNode((node, attrs) => {
      if (attrs.overlayInjected && !activeIds.has(node)) injectedNodes.push(node);
    });
    for (const node of injectedNodes) {
      if (graph.hasNode(node)) graph.dropNode(node);
    }

    if (!enableDiscoveryEmphasis || !scopedDiscoverGraph || scopedDiscoverGraph.nodes.length === 0) {
      sigma.refresh();
      return;
    }

    const count = scopedDiscoverGraph.nodes.length;
    const radius = 6 + Math.sqrt(Math.max(1, count)) * 1.4;
    scopedDiscoverGraph.nodes.forEach((n, i) => {
      if (graph.hasNode(n.id)) return;
      const angle = (i / Math.max(1, count)) * Math.PI * 2;
      graph.addNode(n.id, {
        label: n.label || n.id,
        entity_type: "query_entity",
        mention_count: Math.max(1, n.degree || 1),
        canonical_family: n.canonical_family || "",
        object_kind: n.object_kind || "",
        domain_type: n.domain_type || "",
        primary_domain: n.domain || "",
        concept: n.concept || "",
        overlayInjected: true,
        x: Math.cos(angle) * radius,
        y: Math.sin(angle) * radius,
        size: 5 + Math.min(8, Math.sqrt(Math.max(1, n.degree || 1))),
        color: "#e5e7eb",
      });
    });

    scopedDiscoverGraph.links.forEach((l) => {
      if (!graph.hasNode(l.source) || !graph.hasNode(l.target)) return;
      if (l.source === l.target) return;
      const key = `overlay:${l.source}->${l.target}`;
      if (graph.hasEdge(key)) return;
      try {
        graph.addEdgeWithKey(key, l.source, l.target, {
          overlayInjected: true,
          predicate: l.predicate || l.classification || "query_relation",
          relation_family: l.relation_family || "WeakAssociation",
          confidence: l.confidence || 0.7,
          color:
            RELATION_FAMILY_EDGE_COLOR[l.relation_family || "WeakAssociation"] ||
            "rgba(125, 211, 252, 0.35)",
          size: 0.8,
        });
      } catch {
        /* existing base edge or duplicate overlay edge */
      }
    });
    sigma.refresh();
  }, [scopedDiscoverGraph, enableDiscoveryEmphasis, sigma]);

  // Apply hover ghosting + search highlighting + emphasis via reducers.
  useEffect(() => {
    const graph = sigma.getGraph();
    const normalized = searchTerm.trim().toLowerCase();
    const hasEmphasis = enableDiscoveryEmphasis && !!scopedDiscoverGraph && corpusId !== null;

    const neighbors = new Set<string>();
    if (hovered && graph.hasNode(hovered)) {
      neighbors.add(hovered);
      graph.forEachNeighbor(hovered, (n) => neighbors.add(n));
    }

    sigma.setSetting("nodeReducer", (node, attrs) => {
      const out: any = { ...attrs };

      // 1. EMPHASIS LAYER — recolor by domain, bump size by role, dim the rest.
      if (hasEmphasis) {
        const d = nodeEmphasisMap.get(node);
        if (!d) {
          out.color = DIMMED_NODE_COLOR;
          out.size = attrs.size * 0.45;
          out.label = "";
        } else {
          const conceptKey = d.concept || d.domain_type || d.canonical_family || d.object_kind;
          if (conceptKey) {
            out.color = COMMUNITY_PALETTE[hashStringToInt(conceptKey) % COMMUNITY_PALETTE.length];
          } else {
            const colorKey = getDomainColor(corpusId!, d.domain);
            out.color = DOMAIN_HEX[colorKey] ?? DOMAIN_HEX.stone;
          }
          switch (d.emphasis) {
            case "transfer_hub":
              out.size = attrs.size * 2;
              out.zIndex = 3;
              out.highlighted = true;
              break;
            case "bridge_anchor":
            case "analogy_anchor":
            case "bridge":
            case "analogy":
              out.size = attrs.size * 1.5;
              out.zIndex = 2;
              out.highlighted = true;
              break;
            case "frontier":
              out.size = attrs.size * 1.25;
              out.zIndex = 2;
              break;
            default:
              // "normal" / unrecognised — leave size, keep domain color.
              break;
          }
        }
      }

      // 2. SEARCH LAYER — stays highlighted even under emphasis.
      const name = String(attrs.searchLabel || attrs.label || "").toLowerCase();
      if (normalized.length > 0 && name.includes(normalized)) {
        out.highlighted = true;
        out.zIndex = Math.max(Number(out.zIndex) || 0, 2);
      }

      // 3. HOVER LAYER — overrides everything else for focus mode.
      if (hovered) {
        if (!neighbors.has(node)) {
          out.color = "#1f2228";
          out.label = "";
        } else if (node === hovered) {
          out.highlighted = true;
          out.zIndex = 4;
        }
      }

      return out;
    });

    sigma.setSetting("edgeReducer", (edge, attrs) => {
      const [s, t] = graph.extremities(edge);
      const out: any = { ...attrs };

      // 1. EMPHASIS LAYER on edges.
      if (hasEmphasis) {
        const l = edgeEmphasisMap.get(`${s}->${t}`);
        if (!l) {
          out.color = DIMMED_EDGE_COLOR;
          out.size = 0.3;
        } else {
          switch (l.emphasis) {
            case "bridge":
              out.color = "rgba(255, 255, 255, 0.7)";
              out.size = 1.6;
              break;
            case "context_edge":
              out.color =
                RELATION_FAMILY_EDGE_COLOR[l.relation_family || "WeakAssociation"] ||
                "rgba(125, 211, 252, 0.35)";
              out.size = 0.75;
              break;
            case "weak_edge":
              out.color = "rgba(251, 113, 133, 0.72)";
              out.size = 1.1;
              break;
            case "fragile_bridge":
              out.color = "rgba(251, 191, 36, 0.65)";
              out.size = 0.9;
              break;
            case "gap_edge":
              out.color = "rgba(255, 255, 255, 0.22)";
              out.size = 0.55;
              break;
            case "ghost_analogy":
              out.color = "rgba(167, 139, 250, 0.6)";
              out.size = 0.7;
              break;
            default:
              out.color = "rgba(200, 210, 225, 0.35)";
              out.size = 0.8;
          }
        }
      }

      // 2. HOVER LAYER — spotlight the hovered node's neighborhood.
      if (hovered) {
        if (!(neighbors.has(s) && neighbors.has(t))) {
          out.color = "rgba(30, 34, 42, 0.2)";
          out.hidden = false;
        } else {
          out.color = "rgba(255, 255, 255, 0.55)";
          out.size = 1.2;
        }
      }

      return out;
    });

    sigma.refresh();
  }, [
    hovered,
    searchTerm,
    sigma,
    scopedDiscoverGraph,
    corpusId,
    enableDiscoveryEmphasis,
    nodeEmphasisMap,
    edgeEmphasisMap,
    getDomainColor,
  ]);

  return null;
}

function discourseToFullGraph(result: DiscourseGraphResponse): FullGraphResponse {
  const bridgeByTerm = new Map(result.bridges.map((b) => [b.term, b]));
  return {
    view: "discourse",
    status: "ready",
    nodes: result.graph.nodes.map((n) => {
      const bridge = bridgeByTerm.get(n.id);
      return {
        id: n.id,
        display_name: n.label,
        entity_type: "Term",
        mention_count: n.freq,
        supernode_type: "lexeme",
        primary_domain: n.cluster == null ? "Unclustered" : `Topic ${n.cluster + 1}`,
        top_entities: [],
        bridge_count: bridge?.connects_clusters.length ?? 0,
      };
    }),
    edges: result.graph.links.map((l) => ({
      source: l.source,
      target: l.target,
      predicate: "co_occurs",
      relation_family: "Discourse",
      confidence: Math.min(1, Math.max(0.15, l.weight / 20)),
      weight: l.weight,
    })),
    truncated: false,
    raw_node_count: result.graph.nodes.length,
    raw_edge_count: result.graph.links.length,
    concept_count: result.clusters.length,
    domain_count: result.clusters.length,
  };
}

function contextToFullGraph(result: ContextGraphPayload): FullGraphResponse {
  return {
    view: "context",
    status: "ready",
    nodes: result.nodes.map((n) => ({
      id: n.id,
      display_name: n.label,
      entity_type: n.kind === "topic" ? "context_neighborhood" : n.kind === "document" ? "evidence_document" : "query_concept",
      mention_count: Math.max(1, Math.round(n.weight || n.evidence_count || 1)),
      supernode_type: n.kind,
      primary_domain: n.topic_id || n.role || "context",
      top_entities: n.top_entities || [],
      bridge_count: n.role.includes("bridge") ? 1 : 0,
      context_kind: n.kind,
      context_role: n.role,
      topic_id: n.topic_id || null,
      evidence_count: n.evidence_count,
      context_weight: n.weight,
    })),
    edges: result.links.map((l) => ({
      source: l.source,
      target: l.target,
      predicate: l.kind || l.role,
      relation_family: l.suggested ? "WeakAssociation" : l.role === "document_context" ? "Referential" : l.role === "topic_overlay" || l.role === "query_neighborhood" ? "Discourse" : "Structural",
      confidence: l.suggested ? 0.25 : 0.75,
      weight: l.weight || 1,
      role: l.role,
      suggested: l.suggested,
    })),
    truncated: false,
    raw_node_count: result.nodes.length,
    raw_edge_count: result.links.length,
    concept_count: result.meta?.concept_count ?? result.nodes.filter((n) => n.kind === "concept").length,
    domain_count: result.meta?.topic_count ?? result.nodes.filter((n) => n.kind === "topic").length,
  };
}

function pctLabel(value: number | undefined): string {
  if (!value || !Number.isFinite(value)) return "0%";
  return `${Math.round(value * 100)}%`;
}

function humanizeTopicToken(term: string): string {
  const clean = term.replace(/[_-]+/g, " ").trim();
  const upper: Record<string, string> = {
    ai: "AI",
    api: "API",
    ios: "iOS",
    ml: "ML",
    llm: "LLM",
    rag: "RAG",
    ui: "UI",
    ux: "UX",
  };
  return clean
    .split(/\s+/)
    .filter(Boolean)
    .map((part) => upper[part.toLowerCase()] || part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function clusterName(cluster?: { top_terms?: string[]; cluster_id?: number }): string {
  const terms = (cluster?.top_terms || []).filter(Boolean).slice(0, 3);
  if (terms.length > 0) return terms.map(humanizeTopicToken).join(" ");
  return cluster?.cluster_id == null ? "Unclustered" : `Topic ${cluster.cluster_id + 1}`;
}

function InfraNodusLensOverlay({
  discourse,
  lens,
  onLensChange,
  onSearchTerm,
}: {
  discourse: DiscourseGraphResponse | null;
  lens: DiscourseLens;
  onLensChange: (lens: DiscourseLens) => void;
  onSearchTerm: (term: string) => void;
}) {
  const topClusters = useMemo(() => {
    if (!discourse) return [];
    const proportions = discourse.shape.cluster_proportions || {};
    return discourse.clusters
      .map((cluster) => ({
        ...cluster,
        pct: Number(proportions[String(cluster.cluster_id)] ?? 0),
      }))
      .sort((a, b) => b.pct - a.pct || b.size - a.size)
      .slice(0, 6);
  }, [discourse]);

  const topConcepts = useMemo(() => {
    if (!discourse) return [];
    const bridges = new Map(discourse.bridges.map((b) => [b.term, b]));
    return [...discourse.graph.nodes]
      .sort((a, b) => {
        const bridgeDelta = (bridges.get(b.id)?.centrality || 0) - (bridges.get(a.id)?.centrality || 0);
        return bridgeDelta || b.freq - a.freq;
      })
      .slice(0, 10)
      .map((node) => ({ ...node, bridge: bridges.get(node.id) }));
  }, [discourse]);

  const activeBody = () => {
    if (!discourse) {
      return (
        <div className="text-[11px] text-content-tertiary leading-relaxed">
          Select a corpus to build a language map from the indexed chunks.
        </div>
      );
    }
    if (lens === "topics") {
      return (
        <div className="space-y-2">
          {topClusters.map((cluster, index) => (
            <button
              key={cluster.cluster_id}
              onClick={() => onSearchTerm(cluster.top_terms[0] || "")}
              className="w-full text-left rounded-sm border border-white/10 bg-white/[0.03] hover:border-[#b9f27c]/40 px-2.5 py-2"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-[11px] text-content-primary font-semibold truncate">
                    {clusterName(cluster)}
                  </div>
                  <div className="mt-1 text-[9px] text-content-tertiary truncate">
                    {cluster.top_terms.slice(0, 5).join(" · ")}
                  </div>
                </div>
                <div className="text-[10px] text-[#b9f27c] shrink-0">
                  {cluster.pct > 0 ? pctLabel(cluster.pct) : `#${index + 1}`}
                </div>
              </div>
            </button>
          ))}
        </div>
      );
    }
    if (lens === "concepts") {
      return (
        <div className="space-y-1.5">
          {topConcepts.map((node) => (
            <button
              key={node.id}
              onClick={() => onSearchTerm(node.label)}
              className="w-full flex items-center justify-between gap-3 rounded-sm border border-white/10 bg-white/[0.03] hover:border-[#b9f27c]/40 px-2.5 py-1.5"
            >
              <span className="text-[11px] text-content-primary truncate">{node.label}</span>
              <span className="text-[9px] text-content-tertiary shrink-0">
                {node.bridge ? `bridge ${node.bridge.centrality.toFixed(2)}` : `${node.freq}x`}
              </span>
            </button>
          ))}
        </div>
      );
    }
    if (lens === "gaps") {
      return (
        <div className="space-y-2">
          {discourse.gaps.slice(0, 6).map((gap) => {
            const a = discourse.clusters.find((c) => c.cluster_id === gap.cluster_a);
            const b = discourse.clusters.find((c) => c.cluster_id === gap.cluster_b);
            return (
              <div key={`${gap.cluster_a}:${gap.cluster_b}`} className="rounded-sm border border-amber-300/20 bg-amber-300/[0.04] px-2.5 py-2">
                <div className="text-[10px] text-amber-200 uppercase tracking-wider">
                  {gap.severity.toLowerCase()} gap
                </div>
                <div className="mt-1 text-[11px] text-content-primary leading-snug">
                  {clusterName(a)} ↔ {clusterName(b)}
                </div>
                <div className="mt-1 text-[9px] text-content-tertiary">
                  bridges: {gap.bridging_words.length ? gap.bridging_words.join(" · ") : "none"}
                </div>
              </div>
            );
          })}
          {discourse.gaps.length === 0 && (
            <div className="text-[11px] text-content-tertiary">No thin or disconnected topic pairs surfaced.</div>
          )}
        </div>
      );
    }
    return (
      <div className="space-y-3">
        <div>
          <div className="text-[9px] text-content-tertiary uppercase tracking-widest">Shape</div>
          <div className="text-[12px] text-content-primary mt-1">{discourse.shape.shape}</div>
          <div className="text-[10px] text-content-tertiary mt-1 leading-relaxed">
            {discourse.shape.shape_description}
          </div>
        </div>
        <div>
          <div className="text-[9px] text-content-tertiary uppercase tracking-widest">RAG Context Hint</div>
          <div className="mt-1 text-[10px] text-content-secondary leading-relaxed">
            Inject main topics, high-betweenness bridge terms, and thin gaps before chat synthesis.
          </div>
        </div>
      </div>
    );
  };

  return (
    <>
      <div className="absolute left-4 top-4 z-20 flex flex-col gap-2">
        <button className="inline-flex items-center gap-2 rounded-sm border border-[#b9f27c]/30 bg-[#11141b]/90 px-3 py-2 text-[11px] text-[#d7ff9f] shadow-lg">
          <Sparkles className="w-3.5 h-3.5" />
          insights
        </button>
        <button className="inline-flex items-center gap-2 rounded-sm border border-white/10 bg-[#11141b]/85 px-3 py-2 text-[11px] text-content-secondary hover:text-content-primary">
          <MessageSquare className="w-3.5 h-3.5" />
          ai context
        </button>
      </div>

      <div className="absolute right-4 top-1/2 -translate-y-1/2 z-20 flex flex-col gap-1 rounded-sm border border-white/10 bg-[#11141b]/90 p-1 shadow-xl">
        {[
          { id: "topics" as const, label: "topics", icon: Layers },
          { id: "concepts" as const, label: "concepts", icon: GitFork },
          { id: "gaps" as const, label: "gaps", icon: HelpCircle },
          { id: "context" as const, label: "context", icon: TrendingUp },
        ].map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              onClick={() => onLensChange(item.id)}
              className={`flex items-center gap-2 rounded-sm px-3 py-2 text-[10px] uppercase tracking-wider ${
                lens === item.id
                  ? "bg-[#b9f27c]/15 text-[#d7ff9f]"
                  : "text-content-tertiary hover:text-content-primary hover:bg-white/5"
              }`}
            >
              <Icon className="w-3.5 h-3.5" />
              {item.label}
            </button>
          );
        })}
      </div>

      <div className="absolute right-4 bottom-4 z-20 w-[360px] max-w-[calc(100vw-2rem)] rounded-sm border border-white/10 bg-[#11141b]/92 shadow-2xl backdrop-blur">
        <div className="flex items-center justify-between border-b border-white/10 px-3 py-2">
          <div className="text-[10px] uppercase tracking-widest text-content-tertiary">
            {lens === "topics"
              ? "Main Topics"
              : lens === "concepts"
                ? "Influential Concepts"
                : lens === "gaps"
                  ? "Structural Gaps"
                  : "Context Hint"}
          </div>
          {discourse && (
            <div className="text-[9px] text-content-tertiary">
              {discourse.graph.nodes.length} terms · {discourse.graph.links.length} links
            </div>
          )}
        </div>
        <div className="max-h-[320px] overflow-y-auto custom-scrollbar p-3">{activeBody()}</div>
      </div>
    </>
  );
}

function ContextMapTerminal({ context }: { context: ContextGraphPayload | null | undefined }) {
  const hasContext = Boolean(context?.nodes?.length);
  const hiddenConcepts = Number(context?.meta?.hidden_concept_count || 0);

  return (
    <div className="pointer-events-none absolute bottom-4 left-4 z-20 w-[min(24rem,calc(100vw-2rem))] rounded border border-emerald-300/15 bg-[#050807]/88 p-3 font-mono shadow-[0_0_42px_rgba(0,0,0,0.45)] backdrop-blur">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-[9px] uppercase tracking-[0.26em] text-emerald-200">
          context map
        </div>
        <div className="text-[9px] uppercase tracking-[0.18em] text-neutral-600">
          backend view
        </div>
      </div>
      <div className="rounded border border-white/10 bg-black/35 px-2 py-1.5 text-[10px] leading-relaxed text-neutral-400">
        {hasContext
          ? "Showing the backend's scoped map for the latest graph request."
          : "Run a graph request to replace the overview with a scoped backend map."}
      </div>
      <div className="mt-2 space-y-1 text-[10px] leading-relaxed text-neutral-500">
        <div>
          <span className="text-emerald-300/70">$ map</span> backend-scoped concepts, source nodes, and relationships
        </div>
        <div>
          <span className="text-emerald-300/70">$ request</span> details live in the Mission Control backend request panel
        </div>
        {hiddenConcepts > 0 && (
          <div>
            <span className="text-emerald-300/70">$ display</span> some labels are hidden to keep the map readable
          </div>
        )}
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Main GraphView
// ────────────────────────────────────────────────────────────────────────
export function GraphView({ onClose }: GraphViewProps) {
  const selectedCorpusIds = useSettingsStore((s) => s.selectedCorpusIds);
  const [graphCorpora, setGraphCorpora] = useState<Array<{ corpus_id: string; name?: string }>>([]);
  const fallbackCorpus = graphCorpora[0] || null;
  const corpusId = selectedCorpusIds[0] || fallbackCorpus?.corpus_id || null;
  const graphScopeLabel =
    selectedCorpusIds[0]
      ? "selected corpus"
      : fallbackCorpus
        ? `ALL -> ${fallbackCorpus.name || fallbackCorpus.corpus_id.slice(0, 8)}`
        : null;
  const activeCorpusId = useGraphSessionStore((s) => s.activeCorpusId);
  const activeDiscoverContextGraph = useGraphSessionStore((s) => s.activeDiscoverContextGraph);
  const turns = useGraphSessionStore((s) => s.turns);
  const scopedTurns = activeCorpusId === corpusId ? turns : [];
  const scopedActiveContextGraph =
    activeCorpusId === corpusId ? activeDiscoverContextGraph : null;
  const [eventContextGraph, setEventContextGraph] = useState<ContextGraphPayload | null>(null);
  const [overviewContextGraph, setOverviewContextGraph] = useState<ContextGraphPayload | null>(null);
  const latestTurnContextGraph = scopedTurns.length
    ? contextGraphFromDiscoverResponse(scopedTurns[scopedTurns.length - 1].response)
    : null;
  const contextGraphForCanvas =
    scopedActiveContextGraph && scopedActiveContextGraph.nodes.length > 0
      ? scopedActiveContextGraph
      : latestTurnContextGraph && latestTurnContextGraph.nodes.length > 0
        ? latestTurnContextGraph
        : scopedTurns.length > 0 && eventContextGraph && eventContextGraph.nodes.length > 0
          ? eventContextGraph
          : overviewContextGraph && overviewContextGraph.nodes.length > 0
            ? overviewContextGraph
            : null;

  const [data, setData] = useState<FullGraphResponse | null>(null);
  const [viewMode, setViewMode] = useState<GraphViewMode>("context");
  const [discourse, setDiscourse] = useState<DiscourseGraphResponse | null>(null);
  const [discourseLens, setDiscourseLens] = useState<DiscourseLens>("topics");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<{ communities: number; nodes: number; edges: number } | null>(
    null,
  );

  useEffect(() => {
    let cancelled = false;
    api.listCorpora()
      .then((corpora) => {
        if (!cancelled) {
          setGraphCorpora(corpora.map((corpus) => ({
            corpus_id: corpus.corpus_id,
            name: corpus.name,
          })));
        }
      })
      .catch((err) => {
        if (!cancelled) {
          console.warn("GraphView could not load fallback corpora", err);
          setGraphCorpora([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Debounced search — 200ms after user stops typing.
  const [searchInput, setSearchInput] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  useEffect(() => {
    const h = setTimeout(() => setSearchTerm(searchInput), 200);
    return () => clearTimeout(h);
  }, [searchInput]);

  const fetchData = useCallback(async () => {
    if (!corpusId) {
      setError("Select a corpus from the dropdown first.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const modeConfig = GRAPH_VIEW_MODES.find((m) => m.id === viewMode) || GRAPH_VIEW_MODES[0];
      if (viewMode === "context") {
        if (contextGraphForCanvas) {
          setDiscourse(null);
          setData(contextToFullGraph(contextGraphForCanvas));
          return;
        }
        const res = await api.getGraphOverview(corpusId, modeConfig.nodeCap, modeConfig.edgeCap);
        const freshState = useGraphSessionStore.getState();
        const freshContext =
          freshState.activeCorpusId === corpusId
            ? freshState.activeDiscoverContextGraph && freshState.activeDiscoverContextGraph.nodes.length > 0
              ? freshState.activeDiscoverContextGraph
              : contextGraphFromDiscoverResponse(
                  freshState.turns[freshState.turns.length - 1]?.response,
                )
            : null;
        if (freshContext && freshContext.nodes.length > 0) {
          setDiscourse(null);
          setData(contextToFullGraph(freshContext));
          return;
        }
        const overviewContext = contextGraphFromOverviewResponse(res);
        if (overviewContext) {
          setOverviewContextGraph(overviewContext);
          setDiscourse(null);
          setData(contextToFullGraph(overviewContext));
          return;
        }
        setOverviewContextGraph(null);
        setDiscourse(null);
        setData(res);
        if (res.status === "cache_warming") {
          setError(res.message || "Graph analytics are warming. Try again in a moment.");
        } else if (res.nodes.length === 0) {
          setError("Run a Mission Control query to build the context map overlay.");
        }
        return;
      }
      if (viewMode === "discourse") {
        const discourseResult = await api.getDiscourseGraph(corpusId, {
          topTerms: modeConfig.nodeCap,
          minCooccur: 3,
          chunkLimit: 2000,
        });
        setDiscourse(discourseResult);
        setData(discourseToFullGraph(discourseResult));
        if (discourseResult.graph.nodes.length === 0) {
          setError("No discourse terms could be built from this corpus yet.");
        }
        return;
      }
      const res =
        viewMode === "overview"
          ? await api.getGraphOverview(corpusId, modeConfig.nodeCap, modeConfig.edgeCap)
          : await api.getFullCorpusGraph(corpusId, modeConfig.nodeCap, modeConfig.edgeCap);
      setDiscourse(null);
      setData(res);
      if (res.status === "cache_warming") {
        setError(res.message || "Graph analytics are warming. Try again in a moment.");
      } else if (res.nodes.length === 0) {
        setError(
          "This corpus has no extracted entities yet. Ingest documents with " +
            "use_neo4j=true to populate the graph.",
        );
      }
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [contextGraphForCanvas, corpusId, viewMode]);

  useEffect(() => {
    setOverviewContextGraph(null);
  }, [corpusId]);

  useEffect(() => {
    if (viewMode !== "context" || !contextGraphForCanvas) return;
    setError(null);
    setDiscourse(null);
    setData(contextToFullGraph(contextGraphForCanvas));
  }, [contextGraphForCanvas, viewMode]);

  useEffect(() => {
    const onContextGraph = (event: Event) => {
      const context = (event as CustomEvent<ContextGraphPayload>).detail;
      if (!context?.nodes?.length) return;
      setEventContextGraph(context);
      if (viewMode === "context") {
        setError(null);
        setDiscourse(null);
        setData(contextToFullGraph(context));
      }
    };
    window.addEventListener(MISSION_CONTEXT_GRAPH_EVENT, onContextGraph);
    return () => window.removeEventListener(MISSION_CONTEXT_GRAPH_EVENT, onContextGraph);
  }, [viewMode]);

  useEffect(() => {
    if (scopedTurns.length === 0) setEventContextGraph(null);
  }, [scopedTurns.length]);

  useEffect(() => {
    if (corpusId) fetchData();
  }, [corpusId, fetchData]);

  const sigmaSettings = useMemo(
    () => ({
      allowInvalidContainer: true,
      defaultNodeColor: "#687075",
      defaultEdgeColor: "rgba(130, 138, 142, 0.10)",
      labelColor: { color: "#b9beb9" },
      labelSize: 9,
      labelWeight: "500",
      labelFont: "Inter, system-ui, sans-serif",
      labelRenderedSizeThreshold: 7.8,
      renderEdgeLabels: false,
      edgeLabelColor: { color: "#9ca3af" },
      minCameraRatio: 0.05,
      maxCameraRatio: 20,
      // Dampen wheel zoom — sigma default is aggressive (0.1). Smaller = gentler.
      zoomingRatio: 1.3,
      stagePadding: 90,
      zIndex: true,
    }),
    [],
  );

  return (
    <div
      id="global-graph-view"
      data-graph-view-build="mission-context-52vw"
      className="fixed left-0 top-0 bottom-0 right-[min(42rem,52vw)] z-50 bg-[#0b0c10] flex flex-col font-mono"
    >
      {/* Topbar */}
      <div className="flex items-center gap-3 px-4 py-2 border-b border-border-minimal bg-[#0f1116]">
        <div className="flex items-center gap-2 flex-1">
          <span className="text-[11px] text-content-primary tracking-widest uppercase">
            {viewMode === "context"
              ? "Graph · context"
              : viewMode === "discourse"
                ? "Graph · discourse"
                : viewMode === "overview"
                  ? "Graph · map"
                  : "Graph · entities"}
          </span>
          <span
            className="rounded border border-emerald-300/20 bg-emerald-300/[0.035] px-1.5 py-0.5 text-[8px] uppercase tracking-widest text-emerald-300/70"
            title="The Network button is mounted to the rebuilt GraphView component."
          >
            linked
          </span>
          {graphScopeLabel && (
            <span
              className="rounded border border-amber-300/20 bg-amber-300/[0.04] px-1.5 py-0.5 text-[8px] uppercase tracking-widest text-amber-200/70"
              title={
                selectedCorpusIds[0]
                  ? "Graph synthesis is scoped to the first selected corpus."
                  : "Chat is set to ALL; graph synthesis is single-corpus, so Mission Control uses the first available corpus."
              }
            >
              {graphScopeLabel}
            </span>
          )}
          {stats && (
            <span className="text-[9px] text-content-tertiary">
              {stats.nodes.toLocaleString()} nodes · {stats.edges.toLocaleString()} edges
              {stats.communities > 0 && <> · {stats.communities} communities</>}
              {data?.view === "overview" && typeof data.raw_node_count === "number" && (
                <> · bounded from {data.raw_node_count.toLocaleString()} raw</>
              )}
              {data?.truncated && " · truncated"}
            </span>
          )}
        </div>

        <div className="flex items-center rounded border border-border-minimal bg-[#1a1d24] p-0.5">
          {GRAPH_VIEW_MODES.filter((mode) => ["context", "discourse", "overview"].includes(mode.id)).map((mode) => (
            <button
              key={mode.id}
              onClick={() => setViewMode(mode.id)}
              title={mode.title}
              className={`px-2 py-0.5 text-[10px] uppercase tracking-wider ${
                viewMode === mode.id
                  ? "bg-accent-main/20 text-accent-main"
                  : "text-content-tertiary hover:text-content-primary"
              }`}
            >
              {mode.label}
            </button>
          ))}
        </div>

        <details className="relative">
          <summary className="cursor-pointer rounded border border-border-minimal bg-[#1a1d24] px-2 py-1 text-[10px] uppercase tracking-wider text-content-tertiary hover:text-content-primary">
            advanced
          </summary>
          <div className="absolute right-0 z-30 mt-1 grid min-w-28 gap-1 rounded border border-border-minimal bg-[#11141b] p-1 shadow-xl">
            {GRAPH_VIEW_MODES.filter((mode) => ["raw", "full"].includes(mode.id)).map((mode) => (
              <button
                key={mode.id}
                onClick={() => setViewMode(mode.id)}
                title={mode.title}
                className={`rounded px-2 py-1 text-left text-[10px] uppercase tracking-wider ${
                  viewMode === mode.id
                    ? "bg-accent-main/20 text-accent-main"
                    : "text-content-tertiary hover:text-content-primary hover:bg-white/5"
                }`}
              >
                {mode.label}
              </button>
            ))}
          </div>
        </details>

        {/* Search */}
        <div className="relative">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-content-tertiary" />
          <input
            type="text"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder={
              viewMode === "context"
                ? "search context…"
                : viewMode === "discourse"
                ? "search concepts…"
                : viewMode === "overview"
                  ? "search map…"
                  : "search entities…"
            }
            className="w-48 pl-7 pr-2 py-1 text-[11px] bg-[#1a1d24] border border-border-minimal text-content-primary placeholder-content-tertiary focus:outline-none focus:border-accent-main"
          />
        </div>

        <button
          onClick={fetchData}
          disabled={loading}
          title="Refetch graph"
          className="p-1 text-content-tertiary hover:text-accent-main disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
        </button>
        <button
          onClick={onClose}
          title="Close"
          className="p-1 text-content-tertiary hover:text-accent-main"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 relative">
        {error && (
          <div className="absolute inset-0 flex items-center justify-center p-6 z-10">
            <div className="max-w-md text-center text-[12px] text-content-secondary border border-border-minimal bg-[#0f1116] p-4">
              <Info className="w-5 h-5 mx-auto mb-2 text-content-tertiary" />
              <div>{error}</div>
            </div>
          </div>
        )}

        {!error && data && (
          <SigmaContainer
            style={{
              height: "100%",
              width: "100%",
              backgroundColor: "#070808",
              backgroundImage:
                "linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.018) 1px, transparent 1px)",
              backgroundSize: "42px 42px",
            }}
            settings={sigmaSettings}
          >
            <GraphLoader data={data} onReady={setStats} />
            <InteractionLayer
              searchTerm={searchTerm}
              corpusId={corpusId}
              enableDiscoveryEmphasis={viewMode !== "discourse" && viewMode !== "context"}
            />
            <ZoomToFitButton />
          </SigmaContainer>
        )}

        {!error && data && viewMode === "discourse" && (
          <InfraNodusLensOverlay
            discourse={discourse}
            lens={discourseLens}
            onLensChange={setDiscourseLens}
            onSearchTerm={(term) => setSearchInput(term)}
          />
        )}

        {!error && data && viewMode === "context" && (
          <ContextMapTerminal context={contextGraphForCanvas} />
        )}

        {loading && !data && (
          <div className="absolute inset-0 flex items-center justify-center text-[12px] text-content-secondary">
            loading graph…
          </div>
        )}
      </div>
    </div>
  );
}

// Floating zoom-to-fit button, bottom-right of the sigma container.
function ZoomToFitButton() {
  const sigma = useSigma();
  return (
    <button
      onClick={() => sigma.getCamera().animatedReset({ duration: 400 })}
      className="absolute bottom-4 right-4 p-2 bg-[#1a1d24] border border-border-minimal text-content-secondary hover:text-accent-main z-20"
      title="Zoom to fit"
    >
      <Maximize2 className="w-3.5 h-3.5" />
    </button>
  );
}

// Simple non-crypto string hash for fallback entity_type → palette mapping.
function hashStringToInt(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}
