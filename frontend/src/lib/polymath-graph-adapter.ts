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
  getEdgeStyleByFamily,
  getCommunityColor,
  colorForFamily,
  colorForEntityType,
  type PolymathNodeKind,
} from "./sigma-constants";
import type { QueryFingerprint, QueryLayoutMode } from "./query-fingerprint";

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
  type?: string;
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
  relation_family?: string | null;
  dominant_relation_family?: string | null;
  weight?: number;
  display_weight?: number;
  confidence?: number;
  source_corpora?: string[];
  source_corpus?: string;
  source_doc_id?: string;
  target_doc_id?: string;
  source_corpus_id?: string;
  target_corpus_id?: string;
  source_label?: string;
  target_label?: string;
  shared_entities?: number;
  top_shared_entities?: string[];
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
  /** Optional pre-baked offset from a parent anchor. Used by compact
   *  book-satellite placement; values are graph-space offsets, not
   *  absolute viewport pixels. */
  x?: number;
  y?: number;
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
  /** Brain View semantic color facets supplied by the backend Document anchor. */
  dominant_family?: string | null;
  dominant_entity_type?: string | null;
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
  source_doc_id?: string;
  target_doc_id?: string;
  source_corpus_id?: string;
  target_corpus_id?: string;
  source_label?: string;
  target_label?: string;
  dangling?: boolean;
  cross_cluster?: boolean;
  // Pt 7c: Brain View bridges carry the top shared concept names from
  // the backend. Drives the on-edge label so users see what links two
  // books without clicking. `shared_entities` is the total count so we
  // can compute a "+N more" suffix when only the top 3 names are shown.
  top_shared_entities?: string[];
  shared_entities?: number;
  visual_scaffold?: boolean;
}

export interface BuildOpts {
  colorMode: ColorMode;
  layoutMode?: "brain" | "query";
  queryFingerprint?: QueryFingerprint;
  seedIds?: Set<string>;
  hubIds?: Set<string>;
  bridgeIds?: Set<string>;
  queryForceLabelIds?: Set<string>;
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

function edgeEndpointId(value: unknown): string {
  if (value && typeof value === "object") {
    const obj = value as { id?: unknown; key?: unknown };
    return String(obj.id ?? obj.key ?? "");
  }
  return String(value ?? "");
}

function stableUnitHash(value: string): number {
  let h = 2166136261;
  for (let i = 0; i < value.length; i++) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 4294967295;
}

type QueryShellRole = "seed" | "hub" | "bridge" | "anchor" | "concept" | "leaf";

function queryShellRole(
  raw: PolymathRawNode,
  opts: BuildOpts,
  degree: number,
): QueryShellRole {
  const id = String(raw.id);
  const kind = inferNodeKind(raw);
  if (opts.seedIds?.has(id)) return "seed";
  if (opts.hubIds?.has(id)) return "hub";
  if (opts.bridgeIds?.has(id)) return "bridge";
  if (kind === "Domain" || kind === "Book") return "anchor";
  if (kind === "Concept") return degree >= 5 ? "hub" : "concept";
  return degree >= 5 ? "hub" : "leaf";
}

function queryShellBounds(
  role: QueryShellRole,
  nodeCount: number,
): { inner: number; outer: number } {
  const scale = Math.sqrt(Math.max(nodeCount, 80));
  switch (role) {
    case "seed":
      return { inner: 0, outer: scale * 9 };
    case "hub":
      return { inner: scale * 13, outer: scale * 27 };
    case "bridge":
      return { inner: scale * 20, outer: scale * 36 };
    case "anchor":
      return { inner: scale * 28, outer: scale * 45 };
    case "concept":
      return { inner: scale * 34, outer: scale * 55 };
    case "leaf":
    default:
      return { inner: scale * 48, outer: scale * 78 };
  }
}

function queryRoleRank(role: QueryShellRole): number {
  switch (role) {
    case "seed":
      return 0;
    case "hub":
      return 1;
    case "bridge":
      return 2;
    case "anchor":
      return 3;
    case "concept":
      return 4;
    case "leaf":
    default:
      return 5;
  }
}

function queryLayoutMode(opts: BuildOpts): QueryLayoutMode {
  return opts.queryFingerprint?.layoutMode ?? "radial";
}

function queryLayoutPoint(
  mode: QueryLayoutMode,
  role: QueryShellRole,
  raw: PolymathRawNode,
  localIndex: number,
  roleTotal: number,
  globalIndex: number,
  total: number,
  goldenAngle: number,
  fingerprint?: QueryFingerprint,
): { x: number; y: number } {
  const scale = Math.sqrt(Math.max(total, 80));
  const rank = queryRoleRank(role);
  const centered = localIndex - (roleTotal - 1) / 2;
  const laneGap = scale * 16;
  const t = Math.sqrt((localIndex + 1) / Math.max(roleTotal, 1));
  const jitter = (stableUnitHash(`${raw.id}:jitter`) - 0.5) * scale * 1.1;
  const bond = scale * 13.5;
  const roleRing: Record<QueryShellRole, number> = {
    seed: 0.45,
    hub: 1.55,
    bridge: 2.2,
    anchor: 2.85,
    concept: 3.55,
    leaf: 4.45,
  };
  const moleculeAngle =
    ((localIndex % 6) * Math.PI) / 3 +
    Math.floor(localIndex / 6) * 0.36 +
    rank * 0.18 +
    stableUnitHash(`${raw.id}:atom-angle`) * 0.14;
  const moleculeRing =
    roleRing[role] +
    Math.floor(localIndex / 6) * 0.58 +
    (stableUnitHash(`${raw.id}:atom-ring`) - 0.5) * 0.12;

  const chainPoint = () => ({
    x: (globalIndex - (total - 1) / 2) * scale * 6.8,
    y: (rank - 2.5) * laneGap + jitter,
  });
  const vennPoint = () => {
    const core = role === "seed" || role === "hub" || role === "bridge";
    if (core) {
      const radius = bond * (0.42 + rank * 0.18 + Math.floor(localIndex / 6) * 0.18);
      return {
        x: radius * Math.cos(moleculeAngle),
        y: radius * Math.sin(moleculeAngle) * 0.78,
      };
    }
    const side = stableUnitHash(`${raw.id}:venn-side`) < 0.5 ? -1 : 1;
    const wing = scale * (34 + rank * 7 + Math.floor(localIndex / 7) * 5);
    return {
      x: side * wing + jitter,
      y: centered * scale * 8.4 + side * scale * 2.5,
    };
  };
  const treePoint = () => ({
    x: centered * scale * 9.8 + jitter,
    y: (rank - 2.3) * scale * 24,
  });
  const scatterPoint = () => {
    const axis = stableUnitHash(`${raw.id}:scatter-axis`) * 2 - 1;
    const vertical = stableUnitHash(`${raw.id}:scatter-y`) * 2 - 1;
    const importance = role === "seed" ? 0.45 : role === "hub" || role === "bridge" ? 0.72 : 1;
    return {
      x: axis * scale * (46 + rank * 5) * importance,
      y: vertical * scale * (24 + rank * 4) + centered * scale * 1.4,
    };
  };
  const sociogramPoint = () => {
    const faction = Math.floor(stableUnitHash(`${raw.id}:social-faction`) * 5);
    const factionAngle = (faction / 5) * Math.PI * 2 + rank * 0.14;
    const centrality = role === "seed" || role === "hub" || role === "bridge" ? 0.72 : 1.12;
    const radius = bond * moleculeRing * centrality;
    return {
      x: radius * Math.cos(factionAngle + localIndex * 0.11),
      y: radius * Math.sin(factionAngle + localIndex * 0.11),
    };
  };
  const mindmapPoint = () => {
    const spokeCount = Math.max(5, Math.min(9, Math.ceil(Math.sqrt(total))));
    const spoke = localIndex % spokeCount;
    const spokeAngle = (spoke / spokeCount) * Math.PI * 2 + rank * 0.1;
    const depth = rank + 0.65 + Math.floor(localIndex / spokeCount) * 0.55;
    const radius = bond * depth;
    return {
      x: radius * Math.cos(spokeAngle),
      y: radius * Math.sin(spokeAngle),
    };
  };
  const moleculePoint = () => {
    const nucleusPull = role === "seed" ? 0.72 : 1;
    const radius = bond * moleculeRing * nucleusPull;
    return {
      x: radius * Math.cos(moleculeAngle),
      y: radius * Math.sin(moleculeAngle),
    };
  };
  const clusterPoint = () => {
    const angle = moleculeAngle + globalIndex * goldenAngle * 0.12;
    const radius = bond * moleculeRing;
    return {
      x: radius * Math.cos(angle),
      y: radius * Math.sin(angle),
    };
  };
  const legacyRadialPoint = () => {
    const bounds = queryShellBounds(role, total);
    const angle =
      localIndex * goldenAngle +
      stableUnitHash(`${raw.id}:angle`) * Math.PI * 0.7;
    const wobble = (stableUnitHash(`${raw.id}:radius`) - 0.5) * 0.16;
    const radialMultiplier = mode === "force" ? 1.12 : 1;
    const radius =
      (bounds.inner +
        (bounds.outer - bounds.inner) * Math.max(0, Math.min(1, t + wobble))) *
      radialMultiplier;
    return { x: radius * Math.cos(angle), y: radius * Math.sin(angle) };
  };

  let point: { x: number; y: number };
  if (mode === "chain") {
    point = chainPoint();
  } else if (mode === "venn_molecule") {
    point = vennPoint();
  } else if (mode === "topological_tree") {
    point = treePoint();
  } else if (mode === "scatter_correlation") {
    point = scatterPoint();
  } else if (mode === "sociogram") {
    point = sociogramPoint();
  } else if (mode === "mindmap") {
    point = mindmapPoint();
  } else if (mode === "bipartite") {
    const left = role === "seed" || role === "hub" || role === "bridge";
    point = {
      x: (left ? -1 : 1) * scale * 46 + jitter * 0.5,
      y: centered * scale * 9.2,
    };
  } else if (mode === "hierarchy") {
    point = treePoint();
  } else if (mode === "cluster") {
    point = clusterPoint();
  } else if (mode === "force" || mode === "radial") {
    point = moleculePoint();
  } else {
    point = legacyRadialPoint();
  }

  const blendToward = (target: { x: number; y: number }, amount: number) => {
    const ratio = Math.max(0, Math.min(0.58, amount));
    point = {
      x: point.x * (1 - ratio) + target.x * ratio,
      y: point.y * (1 - ratio) + target.y * ratio,
    };
  };
  const blend = fingerprint?.blend;
  if (blend) {
    if (mode !== "venn_molecule" && blend.venn > 0) {
      blendToward(vennPoint(), blend.venn * 0.42);
    }
    if (mode !== "topological_tree" && blend.tree > 0) {
      blendToward(treePoint(), blend.tree * 0.38);
    }
    if (mode !== "scatter_correlation" && blend.scatter > 0) {
      blendToward(scatterPoint(), blend.scatter * 0.42);
    }
    if (mode !== "sociogram" && blend.sociogram > 0) {
      blendToward(sociogramPoint(), blend.sociogram * 0.42);
    }
    if (mode !== "mindmap" && blend.mindmap > 0) {
      blendToward(mindmapPoint(), blend.mindmap * 0.36);
    }
    if (mode !== "chain" && blend.causal > 0) {
      blendToward(chainPoint(), blend.causal * 0.32);
    }
  }

  return point;
}

function separateQueryPositions(
  nodes: PolymathRawNode[],
  positions: Map<string, { x: number; y: number }>,
) {
  if (nodes.length < 2) return;
  const scale = Math.sqrt(Math.max(nodes.length, 80));
  const minDist = scale * (nodes.length > 300 ? 6.4 : 8.8);

  for (let pass = 0; pass < 12; pass += 1) {
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      const pa = positions.get(a.id);
      if (!pa) continue;
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        const pb = positions.get(b.id);
        if (!pb) continue;
        let dx = pa.x - pb.x;
        let dy = pa.y - pb.y;
        let dist = Math.sqrt(dx * dx + dy * dy);
        if (!Number.isFinite(dist) || dist < 0.01) {
          const angle = stableUnitHash(`${a.id}:${b.id}:separate`) * Math.PI * 2;
          dx = Math.cos(angle) * 0.01;
          dy = Math.sin(angle) * 0.01;
          dist = 0.01;
        }
        if (dist >= minDist) continue;
        const push = ((minDist - dist) / dist) * 0.58;
        const px = dx * push;
        const py = dy * push;
        pa.x += px;
        pa.y += py;
        pb.x -= px;
        pb.y -= py;
      }
    }
  }
}

function compactQueryLabel(label: string, important: boolean): string {
  const limit = important ? 42 : 28;
  if (label.length <= limit) return label;
  const words = label.split(/\s+/).filter(Boolean);
  if (words.length <= 2) return `${label.slice(0, limit - 1)}…`;
  let out = "";
  for (const word of words) {
    const next = out ? `${out} ${word}` : word;
    if (next.length > limit - 1) break;
    out = next;
  }
  return `${out || label.slice(0, limit - 1)}…`;
}

function queryLabelScore(
  raw: PolymathRawNode,
  opts: BuildOpts,
  degree: number,
): number {
  const id = String(raw.id);
  const mentions = Number(raw.mention_count ?? raw.total_mentions ?? 1);
  let score = degree * 18;
  if (opts.seedIds?.has(id)) score += 900;
  if (opts.hubIds?.has(id)) score += 650;
  if (opts.bridgeIds?.has(id)) score += 620;
  if ((raw as any).is_working_entity) score += 120;
  if (Number.isFinite(mentions)) score += Math.log2(mentions + 1) * 18;
  return score;
}

function edgeLayoutWeight(
  rel: PolymathRawEdge,
  opts: BuildOpts,
): number {
  if (rel.visual_scaffold) return 0.01;
  const predicate = String(rel.predicate || "").toLowerCase();
  const family = String(
    rel.dominant_relation_family || rel.relation_family || "",
  ).toLowerCase();
  const rawWeight =
    typeof rel.weight === "number" && Number.isFinite(rel.weight)
      ? Math.max(0.1, rel.weight)
      : 1;

  // ForceAtlas2 has no explicit "rest length"; edge weight is the clean
  // proxy. Stronger weight = shorter/tighter spring. Weak bridges and
  // generic relations stay longer so the query graph breathes.
  let weight = Math.sqrt(rawWeight);
  if (
    predicate === "predicated_by" ||
    predicate === "in_book" ||
    predicate === "mentions" ||
    predicate === "part_of" ||
    predicate === "member_of" ||
    predicate === "synonym_of" ||
    predicate === "alias_of" ||
    predicate === "same_as" ||
    family === "canonicalization" ||
    family === "structural"
  ) {
    weight *= 1.6;
  } else if (
    predicate === "bridges_to" ||
    predicate === "related_to" ||
    predicate === "relates_to" ||
    family === "weakassociation"
  ) {
    weight *= 0.75;
  } else if (
    family === "causal" ||
    family === "operational" ||
    family === "provenance"
  ) {
    weight *= 1.2;
  }

  if (opts.layoutMode === "query") {
    const s = edgeEndpointId(rel.source);
    const t = edgeEndpointId(rel.target);
    const touchesAnchor =
      opts.seedIds?.has(s) ||
      opts.seedIds?.has(t) ||
      opts.hubIds?.has(s) ||
      opts.hubIds?.has(t) ||
      opts.bridgeIds?.has(s) ||
      opts.bridgeIds?.has(t);
    if (touchesAnchor) weight *= 1.2;
  }

  return Math.max(0.2, Math.min(8, weight));
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
  if (kind === "Book") {
    if (colorMode === "corpus") {
      return colorForCorpus(raw.source_corpora);
    }
    const familyColor = colorForFamily(raw.dominant_family);
    if (raw.dominant_family && familyColor !== NODE_COLORS.Book) {
      return familyColor;
    }
    const typeColor = colorForEntityType(raw.dominant_entity_type);
    if (raw.dominant_entity_type && typeColor !== NODE_COLORS.Book) {
      return typeColor;
    }
    // Community mode fallback: stable per-book hue, not per-corpus hue.
    // This keeps a one-corpus library from collapsing into one purple cloud
    // when older ingests do not have dominant semantic facets yet.
    const seed = String(raw.label || raw.display_name || raw.id || "");
    let h = 0;
    for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
    return getCommunityColor(h);
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
  const degreeById = new Map<string, number>();
  for (const rel of rawLinks) {
    const s = edgeEndpointId(rel.source);
    const t = edgeEndpointId(rel.target);
    if (!s || !t || s === t) continue;
    degreeById.set(s, (degreeById.get(s) ?? 0) + 1);
    degreeById.set(t, (degreeById.get(t) ?? 0) + 1);
  }

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
  const queryMode = opts.layoutMode === "query";
  const structuralSpread =
    Math.sqrt(Math.max(n, queryMode ? 90 : 120)) * (queryMode ? 58 : 48);
  const conceptOrbit = structuralSpread * (queryMode ? 0.68 : 0.55);
  const leafJitter =
    Math.sqrt(Math.max(n, queryMode ? 70 : 50)) * (queryMode ? 7 : 4);
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));

  const positions = new Map<string, { x: number; y: number }>();

  if (queryMode) {
    // Query graphs are not an atlas of the whole corpus. They should read
    // like an atom around the user's question: seeds at the nucleus,
    // hubs/bridges in inner shells, concepts/books in middle shells, and
    // leaves outside. ForceAtlas2 then resolves the real edge tensions
    // from a readable deterministic starting point instead of a jellyfish.
    const roleById = new Map<string, QueryShellRole>();
    const countByRole = new Map<QueryShellRole, number>();
    for (const raw of rawNodes) {
      const role = queryShellRole(raw, opts, degreeById.get(raw.id) ?? 0);
      roleById.set(raw.id, role);
      countByRole.set(role, (countByRole.get(role) ?? 0) + 1);
    }
    const seenByRole = new Map<QueryShellRole, number>();
    const ranked = [...rawNodes].sort((a, b) => {
      const ar = roleById.get(a.id) ?? "leaf";
      const br = roleById.get(b.id) ?? "leaf";
      return (
        queryRoleRank(ar) - queryRoleRank(br) ||
        (degreeById.get(b.id) ?? 0) - (degreeById.get(a.id) ?? 0) ||
        String(a.id).localeCompare(String(b.id))
      );
    });
    const queryForceLabelIds = new Set(
      [...rawNodes]
        .sort(
          (a, b) =>
            queryLabelScore(b, opts, degreeById.get(b.id) ?? 0) -
              queryLabelScore(a, opts, degreeById.get(a.id) ?? 0) ||
            String(a.id).localeCompare(String(b.id)),
        )
        .slice(0, n > 300 ? 8 : 11)
        .map((raw) => String(raw.id)),
    );
    const queryOpts: BuildOpts = { ...opts, queryForceLabelIds };
    ranked.forEach((raw) => {
      const role = roleById.get(raw.id) ?? "leaf";
      const localIndex = seenByRole.get(role) ?? 0;
      const roleTotal = Math.max(countByRole.get(role) ?? 1, 1);
      seenByRole.set(role, localIndex + 1);

      const { x, y } = queryLayoutPoint(
        queryLayoutMode(opts),
        role,
        raw,
        localIndex,
        roleTotal,
        positions.size,
        n,
        goldenAngle,
        opts.queryFingerprint,
      );
      positions.set(raw.id, { x, y });
      addNodeToGraph(graph, raw, x, y, n, queryOpts, degreeById);
    });
    separateQueryPositions(ranked, positions);
    for (const raw of ranked) {
      const pos = positions.get(raw.id);
      if (pos && graph.hasNode(raw.id)) {
        graph.mergeNodeAttributes(raw.id, pos);
      }
    }
  } else {
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
      addNodeToGraph(graph, raw, x, y, n, opts, degreeById);
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
      addNodeToGraph(graph, raw, x, y, n, opts, degreeById);
    });

    // 3) Leaf entities → cluster around their parent (book anchor or concept
    //    centroid if available), else spread randomly within the canvas.
    leaves.forEach((raw, idx) => {
      let cx = 0;
      let cy = 0;
      let positioned = false;
      let isOctopusSatellite = false;
      // Book mode: leaf entities have primary_doc_id → orbit that book anchor.
      if (raw.primary_doc_id) {
        const anchorPos = positions.get(`book:${raw.primary_doc_id}`);
        if (anchorPos) {
          cx = anchorPos.x;
          cy = anchorPos.y;
          positioned = true;
          isOctopusSatellite = true;
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

      let x: number;
      let y: number;
      if (isOctopusSatellite) {
        // Octopus mode — leaf carries a pre-baked polar offset (raw.x/y
        // set to a ring at ~28px from origin by GraphViewer). Add the
        // anchor center to land it in tight orbit around the book. This
        // is much more compact than the scattered-cloud `leafJitter`
        // fallback, so satellites read as tentacle tips, not as drifters.
        const offsetX = typeof raw.x === "number" ? raw.x : 0;
        const offsetY = typeof raw.y === "number" ? raw.y : 0;
        x = cx + offsetX + (Math.random() - 0.5) * 4;
        y = cy + offsetY + (Math.random() - 0.5) * 4;
      } else {
        x = cx + (Math.random() - 0.5) * leafJitter;
        y = cy + (Math.random() - 0.5) * leafJitter;
      }
      positions.set(raw.id, { x, y });
      addNodeToGraph(graph, raw, x, y, n, opts, degreeById);
    });
  }

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
  const edgeBaseSize = queryMode
    ? n > 800
      ? 1.15
      : n > 200
        ? 1.3
        : 1.45
    : n > 20000
      ? 0.4
      : n > 5000
        ? 0.6
        : 1.0;

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
  const perEndpointCount = new Map<string, number>();
  strongBridges.sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0));
  const cappedBridges: PolymathRawEdge[] = [];
  for (const rel of strongBridges) {
    const s = edgeEndpointId(rel.source);
    const t = edgeEndpointId(rel.target);
    const sourceUsed = perEndpointCount.get(s) ?? 0;
    const targetUsed = perEndpointCount.get(t) ?? 0;
    if (sourceUsed >= maxPerBook || targetUsed >= maxPerBook) continue;
    cappedBridges.push(rel);
    perEndpointCount.set(s, sourceUsed + 1);
    perEndpointCount.set(t, targetUsed + 1);
  }
  const linksToRender: PolymathRawEdge[] = [...cappedBridges, ...otherLinks];

  linksToRender.forEach((rel, i) => {
    const s = edgeEndpointId(rel.source);
    const t = edgeEndpointId(rel.target);
    if (!graph.hasNode(s) || !graph.hasNode(t) || s === t) return;
    if (graph.hasEdge(s, t)) return;
    const styleKey = String(rel.predicate || rel.relation_family || "").toLowerCase();
    const style = EDGE_STYLES[styleKey] || DEFAULT_EDGE_STYLE;
    const relationFamily =
      rel.dominant_relation_family || rel.relation_family || null;
    const familyStyle = getEdgeStyleByFamily(relationFamily, rel.predicate);
    // Curvature jitter prevents perfectly-overlapping double edges. Query
    // graphs get curvature from the user's semantic fingerprint so compare,
    // chain, hierarchy, and radial questions do not all feel like the same
    // generic force layout.
    const baseCurvature =
      opts.layoutMode === "query"
        ? (opts.queryFingerprint?.edgeCurvature ?? 0.22) * 0.34
        : 0.12;
    const curvature =
      opts.layoutMode === "query"
        ? Math.max(
            0.025,
            Math.min(
              0.18,
              baseCurvature +
                (stableUnitHash(`${s}:${t}:${i}:curve`) - 0.5) * 0.018,
            ),
          )
        : baseCurvature + Math.random() * 0.08;
    let color = relationFamily
      ? hexToRgba(familyStyle.color, familyStyle.opacity)
      : style.color;
    if (rel.visual_scaffold) {
      color = "rgba(148, 163, 184, 0.4)";
    } else if (rel.dangling) {
      color = "#d97706"; // amber for dangling edges (target outside loaded set)
    } else if (!relationFamily &&
      Array.isArray(rel.source_corpora) &&
      rel.source_corpora.length > 1
    ) {
      // Cross-corpus edge — mix the style color with violet to signal "bridge."
      color = "#a78bfa";
    }
    // Brain View bridges carry a `weight` = shared-entity strength from
    // /api/graph/brain-view. If the backend provides a dominant relation
    // family, color the bridge semantically while opacity carries bridge
    // strength: faint = weak connection, bright = strong.
    let size = edgeBaseSize * (relationFamily ? familyStyle.size : style.sizeMultiplier);
    if (rel.visual_scaffold) {
      size = Math.max(0.7, edgeBaseSize * 0.56);
    }
    let edgeLabel: string | undefined;
    if (rel.predicate === "bridges_to" && typeof rel.weight === "number") {
      const strength = Math.max(0, rel.weight);
      size = Math.min(0.35 + strength * 0.05, 1.8);
      const opacity = Math.max(0.16, Math.min(0.92, 0.14 + strength * 0.06));
      const bridgeColor = relationFamily
        ? familyStyle.color
        : EDGE_STYLES.bridges_to.color;
      color = hexToRgba(bridgeColor, opacity);
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
    const layoutWeight = edgeLayoutWeight(rel, opts);
    graph.addEdgeWithKey(`e${i}`, s, t, {
      size,
      color,
      type: "curved",
      curvature,
      predicate: rel.predicate,
      relation_family: rel.relation_family,
      dominant_relation_family: relationFamily,
      weight: layoutWeight,
      display_weight: rel.weight,
      confidence: rel.confidence,
      source_corpora: rel.source_corpora || [],
      source_corpus: rel.source_corpus,
      source_doc_id: rel.source_doc_id,
      target_doc_id: rel.target_doc_id,
      source_corpus_id: rel.source_corpus_id,
      target_corpus_id: rel.target_corpus_id,
      source_label: rel.source_label,
      target_label: rel.target_label,
      shared_entities: rel.shared_entities,
      top_shared_entities: rel.top_shared_entities,
      dangling: Boolean(rel.dangling),
      label: edgeLabel,
    });
  });

  // 5) Mark seeds + hubs + bridges (used by useSigma's nodeReducer).
  graph.forEachNode((id, attrs) => {
    if (seedIds.has(id)) {
      graph.mergeNodeAttributes(id, {
        isSeed: true,
        forceLabel:
          opts.layoutMode === "query"
            ? Boolean(opts.queryForceLabelIds?.has(id))
            : true,
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
  degreeById: Map<string, number>,
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
  const fullLabel =
    (raw as PolymathRawNode).label ||
    raw.display_name ||
    raw.id;
  const degree = degreeById.get(raw.id) ?? 0;
  const isQueryAnchor =
    opts.seedIds?.has(raw.id) ||
    opts.hubIds?.has(raw.id) ||
    opts.bridgeIds?.has(raw.id);
  const renderedLabel =
    opts.layoutMode === "query"
      ? compactQueryLabel(String(fullLabel), Boolean(isQueryAnchor) || degree >= 5)
      : fullLabel;
  const forceLabel =
    typeof (raw as PolymathRawNode).forceLabel === "boolean"
      ? Boolean((raw as PolymathRawNode).forceLabel)
      : opts.layoutMode === "query"
        ? Boolean(opts.queryForceLabelIds?.has(raw.id))
        : kind === "Domain" || kind === "Book";
  // Octopus satellites — leaves bound to a book via primary_doc_id —
  // need very low mass so FA2 doesn't yank them into other orbits. Cap
  // at 2; the book anchors themselves stay heavy (mass 22 in
  // sigma-constants::nodeReducer) so the orbit hierarchy is stable.
  const baseMass = NODE_MASSES[kind] || 1;
  const isOctopusSatellite =
    Boolean((raw as PolymathRawNode).primary_doc_id) && kind !== "Book";
  let mass = isOctopusSatellite ? Math.min(baseMass, 2) : baseMass;
  if (opts.layoutMode === "query") {
    mass += Math.min(16, Math.log2(degree + 1) * 2.2);
    if (opts.seedIds?.has(raw.id)) mass *= 1.45;
    else if (opts.hubIds?.has(raw.id) || opts.bridgeIds?.has(raw.id)) {
      mass *= 1.25;
    }
  }

  graph.addNode(raw.id, {
    x,
    y,
    ...(kind === "Book" ? { type: "bookGlow" } : {}),
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
    mass,
    // Carried for sigma-constants::nodeReducer star-field sizing.
    bridge_count: (raw as PolymathRawNode).bridge_count,
  } as any);
}
