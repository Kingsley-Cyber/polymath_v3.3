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
  // EDGE_COLORS_BY_FAMILY, colorForFamily, colorForEntityType, and
  // RelationFamily were used by the old dominant_family / dominant_
  // relation_family color paths. The constellation refactor replaced
  // those with a deterministic corpus-hash hue for nodes and a single
  // violet (EDGE_STYLES.bridges_to) with opacity-by-strength for edges,
  // so those imports are no longer needed here. Left in sigma-constants
  // exports so other surfaces can still use them.
  type PolymathNodeKind,
} from "./sigma-constants";

// Pt 5: cap how many outbound bridges any single book can show. Prevents
// hub-books from creating a web that swamps the canvas; long-tail bridges
// drop off the visible graph but stay in the payload.
// Pt 6: now overridable via BuildOpts.maxBridgesPerBook so the dashboard
// slider can dial the cap up/down at runtime.
const MAX_BRIDGES_PER_BOOK_DEFAULT = 3;

// Pt 5: hide bridges below this shared-entity strength at the top-level
// brain view. They're still in the API response — just not rendered.
// Pt 6: overridable via BuildOpts.minBridgeStrength.
const MIN_BRIDGE_STRENGTH_DEFAULT = 2;

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
  // Pt 7c: text label rendered at edge midpoint (gated by
  // `labelRenderedSizeThreshold`). Used for Brain View bridges to show
  // the top shared concept name between two books at a glance.
  label?: string;
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
  /** Optional pre-cleaned label (e.g. "Title -- Author" from cleanBookLabel)
   *  — used by sigma's label renderer in preference to display_name. */
  label?: string;
  /** Optional explicit forceLabel — overrides the default kind-based rule.
   *  Used by the Brain View top-N forceLabel tagging. */
  forceLabel?: boolean;
  /** Brain View bridge count, used by sigma-constants::nodeReducer to scale
   *  Book anchor size logarithmically (well-connected books read larger). */
  bridge_count?: number;
}

export interface PolymathRawEdge {
  source: string;
  target: string;
  predicate?: string;
  relation_family?: string;
  // Pt 5: dominant_relation_family is computed in the Brain View Cypher
  // and used to color bridge edges via EDGE_COLORS_BY_FAMILY.
  dominant_relation_family?: string | null;
  weight?: number;
  confidence?: number;
  source_corpora?: string[];
  source_corpus?: string;
  dangling?: boolean;
  cross_cluster?: boolean;
  // Pt 7c: Brain View bridges carry the top shared concept names from
  // the backend. Drives the on-edge label so users see what links two
  // books without clicking. `shared_entities` is the total count so we
  // can compute a "+N more" suffix when only the top 3 names are shown.
  top_shared_entities?: string[];
  shared_entities?: number;
}

export interface BuildOpts {
  colorMode: ColorMode;
  seedIds?: Set<string>;
  hubIds?: Set<string>;
  bridgeIds?: Set<string>;
  /** Optional cluster centroids per concept_id / domain key, lets the
   *  adapter cluster-position entity-level nodes around their parent. */
  clusterCenters?: Map<string, { x: number; y: number }>;
  /** Pt 6: dashboard-controlled bridge filter knobs. Override the static
   *  Pt 5 defaults so the user can crank the slider live. */
  minBridgeStrength?: number;
  maxBridgesPerBook?: number;
}

// Convert "#rrggbb" → "rgba(r,g,b,a)". Lets us bake opacity into edge
// colors so weak bridges fade out without needing a separate opacity attr
// (sigma EdgeCurveProgram reads color alpha directly).
function hexToRgba(hex: string, alpha: number): string {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!m) return hex;
  const r = parseInt(m[1], 16);
  const g = parseInt(m[2], 16);
  const b = parseInt(m[3], 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})`;
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
  // Constellation aesthetic — Books are colored by a deterministic hash
  // of their corpus_id so same-corpus books cluster visually as a single
  // hue. Within a corpus, lightness varies with bridge_count so books
  // aren't clones — well-connected hubs read brighter than leaf books.
  //
  // Replaces the dominant_family / dominant_entity_type chain (those
  // fields aren't reliably populated upstream and produced mostly-amber
  // graphs).
  if (kind === "Book") {
    const corpusId = String(
      (raw as any).source_corpus || raw.source_corpora?.[0] || "",
    );
    if (corpusId) {
      let h = 0;
      for (const ch of corpusId) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
      h = h % 360;
      // Lightness ramp drives within-corpus differentiation. 5 buckets
      // means a corpus with many books still produces ≤5 distinct
      // brightness tiers, keeping the hue identity stable.
      const bridges = Math.max(0, Number((raw as any).bridge_count ?? 0));
      const l = 42 + (bridges % 5) * 7; // 42%, 49%, 56%, 63%, 70%
      return `hsl(${h}, 72%, ${l}%)`;
    }
    return NODE_COLORS.Book;
  }
  if (colorMode === "corpus") {
    return colorForCorpus(raw.source_corpora);
  }
  // Community mode:
  //   • Concept supernodes → color by primary_domain hash so concepts in
  //     the same domain share a hue family (visual community grouping).
  //   • Domain supernodes → use the Domain palette color.
  //   • Entity-level nodes → color by entity_type from NODE_COLORS.
  if (kind === "Concept") {
    const domain = raw.primary_domain || raw.id;
    let h = 0;
    for (let i = 0; i < domain.length; i++) h = (h * 31 + domain.charCodeAt(i)) >>> 0;
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
  //
  // Pt 4 polish: bumped the floor (max(n, 50) → max(n, 120)) and the
  // multiplier (32 → 48) so a brain view with ~16 books starts from a
  // wider seed. FA2 then only has to fine-tune positions, not push the
  // whole pile outward, so the canvas reads as spread-out within the
  // first second instead of clumping for 20s while FA2 settles.
  const structuralSpread = Math.sqrt(Math.max(n, 120)) * 48;
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
  //
  // Pt 5 polish — galaxy aesthetic for bridges_to:
  //   • Filter weight<MIN_BRIDGE_STRENGTH (=2) — drops 1-shared-entity
  //     noise that wasn't carrying signal anyway.
  //   • Cap to MAX_BRIDGES_PER_BOOK (=3) per source — strongest bridges
  //     only, sorted by weight desc. Hub-books no longer web out.
  //   • Color by dominant_relation_family from the backend (Pt 5 Cypher
  //     extension) → semantic edge color (Operational=blue, Causal=amber,
  //     Conflict=red, etc.) instead of all-violet.
  //   • Thinner size: max ~1.8px ("thin spaghetti" per user spec).
  const edgeBaseSize = n > 20000 ? 0.4 : n > 5000 ? 0.6 : 1.0;

  // Pre-process Brain View bridges so we can apply the strength filter +
  // per-source top-N cap before writing edges. All non-bridge edges pass
  // through unchanged.
  const bridgeLinks: PolymathRawEdge[] = [];
  const otherLinks: PolymathRawEdge[] = [];
  for (const rel of rawLinks) {
    if (rel.predicate === "bridges_to") bridgeLinks.push(rel);
    else otherLinks.push(rel);
  }
  // Filter weak bridges then sort each source's outbound bridges by weight
  // desc, keeping only the top MAX_BRIDGES_PER_BOOK. Pt 6: thresholds are
  // now overridable via opts so the dashboard slider can crank them live.
  const minStrength =
    typeof opts.minBridgeStrength === "number"
      ? opts.minBridgeStrength
      : MIN_BRIDGE_STRENGTH_DEFAULT;
  const maxPerBook =
    typeof opts.maxBridgesPerBook === "number"
      ? opts.maxBridgesPerBook
      : MAX_BRIDGES_PER_BOOK_DEFAULT;
  const strongBridges = bridgeLinks.filter(
    (b) => (b.weight ?? 0) >= minStrength,
  );
  const perSourceCount = new Map<string, number>();
  strongBridges.sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0));
  const cappedBridges: PolymathRawEdge[] = [];
  for (const rel of strongBridges) {
    const s = String(rel.source);
    const used = perSourceCount.get(s) ?? 0;
    if (used >= maxPerBook) continue;
    cappedBridges.push(rel);
    perSourceCount.set(s, used + 1);
  }
  const linksToRender: PolymathRawEdge[] = [...cappedBridges, ...otherLinks];

  linksToRender.forEach((rel, i) => {
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
    // /api/graph/brain-view. ONE color (EDGE_STYLES.bridges_to violet)
    // for every book-to-book bridge, with opacity carrying the signal:
    // faint = weak connection, bright = strong. The old
    // dominant_relation_family branch produced mostly-gray edges
    // because that field wasn't reliably populated upstream.
    let size = edgeBaseSize * style.sizeMultiplier;
    let edgeLabel: string | undefined;
    if (rel.predicate === "bridges_to" && typeof rel.weight === "number") {
      const strength = Math.max(0, rel.weight);
      size = Math.min(0.3 + strength * 0.04, 1.2);
      const opacity = Math.max(0.12, Math.min(0.9, 0.12 + strength * 0.06));
      color = hexToRgba(EDGE_STYLES.bridges_to.color, opacity);
      // On-edge concept label: strongest shared entity name with
      // "+N more" overflow suffix so users see what two books share
      // at a glance. Only show on stronger bridges (≥3 shared) so
      // weak edges don't clutter the canvas with labels.
      const tops = rel.top_shared_entities ?? [];
      if (tops.length > 0 && strength >= 3) {
        const rawHead = tops[0];
        const head =
          rawHead.length > 20 ? rawHead.slice(0, 19) + "…" : rawHead;
        const totalShared = rel.shared_entities ?? tops.length;
        const overflow = Math.max(0, totalShared - 1);
        edgeLabel = overflow > 0 ? `${head} +${overflow}` : head;
      }
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
      label: edgeLabel,
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
  // Pt 3 polish:
  //   • `raw.label` (pre-cleaned by cleanBookLabel) wins over display_name.
  //   • `raw.forceLabel` boolean (Brain View top-N tagging) overrides the
  //     default Domain/Book always-on rule when explicitly provided.
  const renderedLabel =
    (raw as PolymathRawNode).label ||
    raw.display_name ||
    raw.id;
  const forceLabel =
    typeof (raw as PolymathRawNode).forceLabel === "boolean"
      ? Boolean((raw as PolymathRawNode).forceLabel)
      : kind === "Domain" || kind === "Book";
  graph.addNode(raw.id, {
    x,
    y,
    size: scaledSize + mentionBoost,
    color,
    label: renderedLabel,
    nodeKind: kind,
    display_name: raw.display_name || raw.id,
    mention_count: Number(mentionWeight || 1),
    source_corpora: raw.source_corpora || [],
    source_corpus: raw.source_corpus || "",
    primary_domain: raw.primary_domain,
    member_ids: raw.member_ids,
    hidden: false,
    forceLabel,
    mass: NODE_MASSES[kind] || 1,
    // Carried for sigma-constants::nodeReducer star-field sizing.
    bridge_count: (raw as PolymathRawNode).bridge_count,
  } as any);
}
