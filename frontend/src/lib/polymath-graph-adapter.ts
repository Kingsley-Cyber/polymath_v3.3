/**
 * Polymath ⇄ graphology adapter — port of GitNexus's `graph-adapter.ts`
 * adapted to Polymath's payload shapes.
 *
 * GitNexus uses BFS-from-structural-roots positioning so parents anchor
 * before children. Polymath has a similar mental model:
 *   • Domain supernodes      → BFS roots in domains mode
 *   • Concept supernodes     → first-level children (positioned around domains)
 *   • Book cluster anchors   → BFS roots in books mode
 *   • Entity nodes           → leaf children (positioned near their cluster)
 *
 * Initial radial layout via golden angle so ForceAtlas2 converges in
 * seconds rather than spinning forever from random positions.
 */

import Graph from "graphology";
import {
  inferNodeKind,
  NODE_COLORS,
  NODE_SIZES,
  NODE_MASSES,
  CORPUS_COLORS,
  EDGE_STYLES,
  DEFAULT_EDGE_STYLE,
  getCommunityColor,
  type PolymathNodeKind,
} from "./sigma-constants";

export interface SigmaNodeAttributes {
  x: number;
  y: number;
  size: number;
  color: string;
  label: string;
  nodeKind: PolymathNodeKind;
  display_name: string;
  mention_count: number;
  source_corpora?: string[];
  source_corpus?: string;
  primary_domain?: string;
  member_ids?: string[];
  community?: number;
  isSeed?: boolean;
  isHub?: boolean;
  hidden?: boolean;
  zIndex?: number;
  highlighted?: boolean;
  forceLabel?: boolean;
  mass?: number;
}

export interface SigmaEdgeAttributes {
  size: number;
  color: string;
  type?: string;
  curvature?: number;
  predicate?: string;
  weight?: number;
  confidence?: number;
  source_corpora?: string[];
  source_corpus?: string;
  dangling?: boolean;
  hidden?: boolean;
  zIndex?: number;
}

export type ColorMode = "community" | "corpus";

export interface PolymathRawNode {
  id: string;
  display_name: string;
  entity_type?: string;
  mention_count?: number;
  total_mentions?: number;
  supernode_type?: string;
  primary_domain?: string;
  source_corpora?: string[];
  source_corpus?: string;
  top_entities?: string[];
  member_ids?: string[];
  primary_doc_id?: string;
  bridge_doc_ids?: string[];
  per_doc_mentions?: Record<string, number>;
  kind?: "book";
  is_cluster_anchor?: boolean;
}

export interface PolymathRawEdge {
  source: string;
  target: string;
  predicate?: string;
  relation_family?: string;
  weight?: number;
  confidence?: number;
  source_corpora?: string[];
  source_corpus?: string;
  dangling?: boolean;
  cross_cluster?: boolean;
}

export interface BuildOpts {
  colorMode: ColorMode;
  seedIds?: Set<string>;
  hubIds?: Set<string>;
  bridgeIds?: Set<string>;
  /** Optional cluster centroids per concept_id / domain key, lets the
   *  adapter cluster-position entity-level nodes around their parent. */
  clusterCenters?: Map<string, { x: number; y: number }>;
}

// Scale node size down as graph density grows so the visual hierarchy
// stays readable when the canvas has 10k+ nodes.
function getScaledNodeSize(baseSize: number, nodeCount: number): number {
  if (nodeCount > 50000) return Math.max(1, baseSize * 0.4);
  if (nodeCount > 20000) return Math.max(1.5, baseSize * 0.5);
  if (nodeCount > 5000) return Math.max(2, baseSize * 0.65);
  if (nodeCount > 1000) return Math.max(2.5, baseSize * 0.8);
  return baseSize;
}

function colorForCorpus(corpora: string[] | undefined): string {
  if (!corpora || corpora.length === 0) return CORPUS_COLORS[0];
  // Hash the first corpus_id to pick a stable hue from CORPUS_COLORS.
  const seed = corpora[0];
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return CORPUS_COLORS[h % CORPUS_COLORS.length];
}

function pickNodeColor(
  kind: PolymathNodeKind,
  raw: PolymathRawNode,
  colorMode: ColorMode,
): string {
  if (colorMode === "corpus") {
    return colorForCorpus(raw.source_corpora);
  }
  // Community mode:
  //   • Concept supernodes → color by primary_domain hash so concepts in
  //     the same domain share a hue family (visual community grouping).
  //   • Domain supernodes → use the Domain palette color.
  //   • Book anchors → color by hash of doc_id so each book is distinct.
  //   • Entity-level nodes → color by entity_type from NODE_COLORS.
  if (kind === "Concept") {
    const domain = raw.primary_domain || raw.id;
    let h = 0;
    for (let i = 0; i < domain.length; i++) h = (h * 31 + domain.charCodeAt(i)) >>> 0;
    return getCommunityColor(h);
  }
  if (kind === "Book") {
    let h = 0;
    for (let i = 0; i < raw.id.length; i++) h = (h * 31 + raw.id.charCodeAt(i)) >>> 0;
    return getCommunityColor(h);
  }
  return NODE_COLORS[kind];
}

/**
 * The main converter. Takes a Polymath payload (nodes + links arrays as
 * returned by /graph/overview, /graph/full, /graph/cluster, /graph/by-document,
 * or /graph/query) and produces a graphology Graph ready for sigma.js.
 */
export function polymathToGraphology(
  rawNodes: PolymathRawNode[],
  rawLinks: PolymathRawEdge[],
  opts: BuildOpts,
): Graph<SigmaNodeAttributes, SigmaEdgeAttributes> {
  // colorMode is consumed inside addNodeToGraph via the opts pass-through.
  const {
    seedIds = new Set<string>(),
    hubIds = new Set<string>(),
    bridgeIds = new Set<string>(),
    clusterCenters,
  } = opts;

  const graph = new Graph<SigmaNodeAttributes, SigmaEdgeAttributes>({
    multi: false,
    type: "directed",
  });
  const n = rawNodes.length;

  // Separate "structural" nodes (the anchors that should be positioned
  // first via wide radial spread) from the "content" nodes that cluster
  // around them.
  const structuralKinds = new Set<PolymathNodeKind>(["Domain", "Book"]);
  const conceptKinds = new Set<PolymathNodeKind>(["Concept"]);
  const structural: PolymathRawNode[] = [];
  const concepts: PolymathRawNode[] = [];
  const leaves: PolymathRawNode[] = [];

  for (const raw of rawNodes) {
    const kind = inferNodeKind(raw);
    if (structuralKinds.has(kind)) structural.push(raw);
    else if (conceptKinds.has(kind)) concepts.push(raw);
    else leaves.push(raw);
  }

  // Wide spread for structural anchors — same approach GitNexus uses for
  // folders. Square-root scaling so 5000 nodes don't try to cram into the
  // same window 100 nodes do.
  const structuralSpread = Math.sqrt(Math.max(n, 50)) * 32;
  const conceptOrbit = structuralSpread * 0.55;
  const leafJitter = Math.sqrt(Math.max(n, 50)) * 4;
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));

  const positions = new Map<string, { x: number; y: number }>();

  // 1) Structural anchors → wide golden-angle radial spread.
  structural.forEach((raw, idx) => {
    const angle = idx * goldenAngle;
    const radius =
      structuralSpread *
      Math.sqrt((idx + 1) / Math.max(structural.length, 1));
    const jitter = structuralSpread * 0.12;
    const x = radius * Math.cos(angle) + (Math.random() - 0.5) * jitter;
    const y = radius * Math.sin(angle) + (Math.random() - 0.5) * jitter;
    positions.set(raw.id, { x, y });
    addNodeToGraph(graph, raw, x, y, n, opts);
  });

  // 2) Concept supernodes → orbit their primary_domain anchor when known,
  //    else just a tighter golden-angle radial spread.
  concepts.forEach((raw, idx) => {
    let center = { x: 0, y: 0 };
    if (raw.primary_domain) {
      const parent = structural.find(
        (s) => s.display_name === raw.primary_domain,
      );
      if (parent) {
        const p = positions.get(parent.id);
        if (p) center = p;
      }
    }
    const angle = idx * goldenAngle;
    const radius = conceptOrbit * Math.sqrt((idx + 1) / Math.max(concepts.length, 1));
    const x = center.x + radius * Math.cos(angle) * 0.8;
    const y = center.y + radius * Math.sin(angle) * 0.8;
    positions.set(raw.id, { x, y });
    addNodeToGraph(graph, raw, x, y, n, opts);
  });

  // 3) Leaf entities → cluster around their parent (book anchor or concept
  //    centroid if available), else spread randomly within the canvas.
  leaves.forEach((raw, idx) => {
    let cx = 0;
    let cy = 0;
    let positioned = false;
    // Book mode: leaf entities have primary_doc_id → orbit that book anchor.
    if (raw.primary_doc_id) {
      const anchorPos = positions.get(`book:${raw.primary_doc_id}`);
      if (anchorPos) {
        cx = anchorPos.x;
        cy = anchorPos.y;
        positioned = true;
      }
    }
    // Cluster centers for query/drill mode if caller provides them.
    if (!positioned && clusterCenters) {
      const c = clusterCenters.get(raw.primary_domain || "");
      if (c) {
        cx = c.x;
        cy = c.y;
        positioned = true;
      }
    }
    // Fallback: scatter inside the canvas.
    if (!positioned) {
      const angle = idx * goldenAngle;
      const radius =
        structuralSpread *
        0.45 *
        Math.sqrt((idx + 1) / Math.max(leaves.length, 1));
      cx = radius * Math.cos(angle);
      cy = radius * Math.sin(angle);
    }
    const x = cx + (Math.random() - 0.5) * leafJitter;
    const y = cy + (Math.random() - 0.5) * leafJitter;
    positions.set(raw.id, { x, y });
    addNodeToGraph(graph, raw, x, y, n, opts);
  });

  // 4) Edges. EDGE_STYLES gives each predicate / relation_family a
  //    distinct color + size multiplier so the mesh of relationships is
  //    visually parseable rather than a uniform gray cloud.
  const edgeBaseSize = n > 20000 ? 0.4 : n > 5000 ? 0.6 : 1.0;
  rawLinks.forEach((rel, i) => {
    const s = String(typeof rel.source === "object" ? (rel as any).source.id : rel.source);
    const t = String(typeof rel.target === "object" ? (rel as any).target.id : rel.target);
    if (!graph.hasNode(s) || !graph.hasNode(t) || s === t) return;
    if (graph.hasEdge(s, t)) return;
    const styleKey = String(rel.predicate || rel.relation_family || "").toLowerCase();
    const style = EDGE_STYLES[styleKey] || DEFAULT_EDGE_STYLE;
    // Curvature jitter prevents perfectly-overlapping double edges.
    const curvature = 0.12 + Math.random() * 0.08;
    let color = style.color;
    if (rel.dangling) {
      color = "#d97706"; // amber for dangling edges (target outside loaded set)
    } else if (
      Array.isArray(rel.source_corpora) &&
      rel.source_corpora.length > 1
    ) {
      // Cross-corpus edge — mix the style color with violet to signal "bridge."
      color = "#a78bfa";
    }
    // Brain View bridges carry a `weight` = shared-entity strength from
    // /api/graph/brain-view. Thicken the edge proportionally so a
    // strong bridge (many shared entities) reads more present than a
    // weak one (1-2 shared entities). PRD spec for edgeReducer.
    let size = edgeBaseSize * style.sizeMultiplier;
    if (rel.predicate === "bridges_to" && typeof rel.weight === "number") {
      // 1.2 baseline + 0.18 per shared edge, capped at 5.5 — matches the
      // strength formula in sigma-constants.ts::edgeReducer.
      const strength = Math.max(0, rel.weight);
      size = Math.min(1.2 + strength * 0.18, 5.5);
    }
    graph.addEdgeWithKey(`e${i}`, s, t, {
      size,
      color,
      type: "curved",
      curvature,
      predicate: rel.predicate,
      weight: rel.weight,
      confidence: rel.confidence,
      source_corpora: rel.source_corpora || [],
      source_corpus: rel.source_corpus,
      dangling: Boolean(rel.dangling),
    });
  });

  // 5) Mark seeds + hubs + bridges (used by useSigma's nodeReducer).
  graph.forEachNode((id, attrs) => {
    if (seedIds.has(id)) {
      graph.mergeNodeAttributes(id, {
        isSeed: true,
        forceLabel: true,
        size: attrs.size + 4,
      });
    }
    if (hubIds.has(id) || bridgeIds.has(id)) {
      graph.mergeNodeAttributes(id, { isHub: true });
    }
  });

  return graph;
}

function addNodeToGraph(
  graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
  raw: PolymathRawNode,
  x: number,
  y: number,
  totalCount: number,
  opts: BuildOpts,
) {
  if (graph.hasNode(raw.id)) return;
  const kind = inferNodeKind(raw);
  const baseSize = NODE_SIZES[kind] || 4;
  const scaledSize = getScaledNodeSize(baseSize, totalCount);
  const color = pickNodeColor(kind, raw, opts.colorMode);
  // Sizing for leaf entities should weigh `total_mentions` (book mode) or
  // `mention_count` (overview / drill / query). Supernodes already get a
  // big kind-derived base so we just nudge by mention.
  const mentionWeight =
    typeof raw.total_mentions === "number"
      ? raw.total_mentions
      : raw.mention_count || 1;
  const mentionBoost =
    kind === "Domain" || kind === "Concept" || kind === "Book"
      ? 0
      : Math.min(6, Math.log2((mentionWeight || 1) + 1) * 1.5);
  graph.addNode(raw.id, {
    x,
    y,
    size: scaledSize + mentionBoost,
    color,
    label: raw.display_name || raw.id,
    nodeKind: kind,
    display_name: raw.display_name || raw.id,
    mention_count: Number(mentionWeight || 1),
    source_corpora: raw.source_corpora || [],
    source_corpus: raw.source_corpus || "",
    primary_domain: raw.primary_domain,
    member_ids: raw.member_ids,
    hidden: false,
    forceLabel: kind === "Domain" || kind === "Book",
    mass: NODE_MASSES[kind] || 1,
  });
}
