/**
 * Polymath sigma rendering constants — books-as-clusters Brain View.
 *
 * Node taxonomy is aligned 1:1 with the backend's
 * `services/graph/neo4j_writer.py::ENTITY_TYPE_PRIORITY` so an entity's
 * `primary_entity_type` flows through to the renderer without a translation
 * layer. The two supernode kinds (Domain / Concept) are layered on top for
 * the Mission Control discovery view.
 *
 * Edge styling is family-based: every RELATES_TO edge carries a
 * `relation_family` from the backend's `RELATION_FAMILY_MAP`, and the
 * renderer keys off that family rather than the raw predicate so the
 * canvas reads cleanly even when the underlying predicate set drifts.
 *
 * Books (`Document` nodes with `is_cluster_anchor: true`) get a strong
 * visual anchor — larger size, gold halo, bigger label — so the books-as-
 * clusters layout reads at a glance even at 1k+ books.
 */

// =============== NODE KINDS ===============
//
// Two supernode kinds (Domain, Concept) for Mission Control, fourteen
// entity-level kinds matching backend ENTITY_TYPE_PRIORITY, plus `Book`
// for Document anchors and `Other` as the typed-fallback bucket.
export type PolymathNodeKind =
  | "Domain"
  | "Concept"
  | "Book"
  | "Person"
  | "Organization"
  | "Location"
  | "Event"
  | "Method"
  | "Product"
  | "Software"
  | "Document"
  | "Standard"
  | "Rule"
  | "Law"
  | "Artifact"
  | "TimeReference"
  | "Other";

// =============== NODE COLORS ===============
// Tuned for dark backgrounds. Books anchor the canvas in amber so the
// cluster structure is legible before any label loads.
export const NODE_COLORS: Record<PolymathNodeKind, string> = {
  Domain: "#a855f7",        // violet — Louvain domain supernodes
  Concept: "#818cf8",       // indigo — concept communities & entities
  Book: "#f59e0b",          // amber — Document cluster anchors
  Person: "#ec4899",        // rose
  Organization: "#eab308",  // yellow
  Location: "#0ea5e9",      // sky
  Event: "#f97316",         // orange
  Method: "#10b981",        // emerald
  Product: "#06b6d4",       // cyan
  Software: "#60a5fa",      // blue
  Document: "#e5e7eb",      // silver — document refs must stay visible on black
  Standard: "#14b8a6",      // teal
  Rule: "#6366f1",          // indigo
  Law: "#ef4444",           // red
  Artifact: "#84cc16",      // lime
  TimeReference: "#3b82f6", // blue
  Other: "#64748b",         // slate-500 — mid-tone so genuinely-untyped nodes
                            // read as a distinct grey, not near-white
};

// =============== NODE SIZES ===============
// Dramatic differences so structural anchors dominate before the user
// reads any label — same principle GitNexus uses.
//
// Pt 3 polish: shrunk Book from 16 → 7 to match the user's "stars in
// the night sky" creative direction. Final rendered size grows
// logarithmically with bridge_count via `nodeReducer` below, so a
// well-connected book gets larger (~13px) and an isolated one stays
// small (~7px). Either way it reads as a point of light, not a blob.
// "Points of light, not blobs." Base sizes shrunk roughly 40% across the
// board so Books read as dots even before the bridge-count multiplier
// kicks in. The largest base (Domain at 14) still beats the smallest
// (Book at 4) by ~3.5×, preserving the visual hierarchy without
// dominating the canvas.
export const NODE_SIZES: Record<PolymathNodeKind, number> = {
  Domain: 14,
  Concept: 9,
  Book: 4,
  Person: 9,
  Organization: 10,
  Location: 7,
  Event: 8,
  Method: 9,
  Product: 8,
  Software: 8,
  Document: 6,
  Standard: 7,
  Rule: 7,
  Law: 7,
  Artifact: 7,
  TimeReference: 5,
  Other: 4,
};

// =============== NODE MASSES (ForceAtlas2) ===============
// Higher mass = stronger repulsion. Anchors (Domain, Book) fan out and
// drag their children with them; entity-level kinds stay light so they
// cluster around their parent rather than fighting it.
export const NODE_MASSES: Record<PolymathNodeKind, number> = {
  Domain: 45,
  Concept: 18,
  Book: 30,
  Person: 20,
  Organization: 22,
  Location: 15,
  Event: 16,
  Method: 18,
  Product: 17,
  Software: 17,
  Document: 10,
  Standard: 14,
  Rule: 15,
  Law: 15,
  Artifact: 12,
  TimeReference: 9,
  Other: 8,
};

// =============== EDGE FAMILIES ===============
// Mirrors backend's `RELATION_FAMILY_MAP` (services/graph/neo4j_writer.py).
// Every RELATES_TO edge persisted by the writer carries one of these.
export type RelationFamily =
  | "Structural"
  | "Operational"
  | "Referential"
  | "Causal"
  | "Conflict"
  | "Provenance"
  | "Affiliation"
  | "Spatial"
  | "Canonicalization"
  | "WeakAssociation";

export const EDGE_COLORS_BY_FAMILY: Record<RelationFamily, string> = {
  Structural: "#10b981",       // emerald
  Operational: "#3b82f6",      // blue (most common)
  Referential: "#8b5cf6",      // violet
  Causal: "#f59e0b",           // amber
  Conflict: "#ef4444",         // red — pops on the canvas
  Provenance: "#14b8a6",       // teal
  Affiliation: "#ec4899",      // rose
  Spatial: "#0ea5e9",          // sky
  Canonicalization: "#94a3b8", // slate-400 (faint — identity bookkeeping)
  WeakAssociation: "#cbd5e1",  // slate-300 (very faint catch-all)
};

const EDGE_SIZE_BY_FAMILY: Record<RelationFamily, number> = {
  Conflict: 2.8,
  Causal: 2.8,
  Operational: 2.0,
  Structural: 1.8,
  Affiliation: 1.6,
  Provenance: 1.6,
  Referential: 1.6,
  Spatial: 1.6,
  Canonicalization: 1.0,
  WeakAssociation: 1.0,
};

export interface EdgeStyleSpec {
  color: string;
  size: number;
  type: "arrow" | "line";
  opacity: number;
}

export function getEdgeStyleByFamily(
  family: string | null | undefined,
  _predicate?: string,
): EdgeStyleSpec {
  const safeFamily = (family as RelationFamily) || "WeakAssociation";
  const color = EDGE_COLORS_BY_FAMILY[safeFamily] ?? EDGE_COLORS_BY_FAMILY.WeakAssociation;
  const size = EDGE_SIZE_BY_FAMILY[safeFamily] ?? 1.4;
  const faint = safeFamily === "WeakAssociation" || safeFamily === "Canonicalization";
  return {
    color,
    size,
    type: "arrow",
    opacity: faint ? 0.45 : 0.85,
  };
}

// =============== LEGACY PREDICATE-KEYED EDGE STYLES ===============
//
// Kept for the existing `polymath-graph-adapter.ts` callsite, which still
// looks up edges by raw predicate. New code should prefer family lookup
// via `getEdgeStyleByFamily()` above — the adapter will migrate when the
// Brain View renderer takes over the canvas.
export const EDGE_STYLES: Record<string, { color: string; sizeMultiplier: number }> = {
  // Membership / containment (faint, structural)
  in_book: { color: "#2d5a3d", sizeMultiplier: 0.35 },
  member_of: { color: "#10b981", sizeMultiplier: 0.45 },
  contains: { color: "#0e7490", sizeMultiplier: 0.45 },
  part_of: { color: "#10b981", sizeMultiplier: 0.55 },
  // Bridges between clusters / docs
  bridges_to: { color: "#7c3aed", sizeMultiplier: 0.7 },
  shared_hub: { color: "#7c3aed", sizeMultiplier: 0.6 },
  // Operational
  uses: { color: "#3b82f6", sizeMultiplier: 0.6 },
  produces: { color: "#3b82f6", sizeMultiplier: 0.6 },
  detects: { color: "#3b82f6", sizeMultiplier: 0.55 },
  // Referential / semantic
  references: { color: "#8b5cf6", sizeMultiplier: 0.5 },
  derived_from: { color: "#8b5cf6", sizeMultiplier: 0.7 },
  implements: { color: "#3b82f6", sizeMultiplier: 0.7 },
  depends_on: { color: "#3b82f6", sizeMultiplier: 0.6 },
  // Detector edges from analytics
  fragile_bridge: { color: "#fb923c", sizeMultiplier: 0.9 },
  structural_analog: { color: "#5eead4", sizeMultiplier: 0.7 },
  terminological_gap: { color: "#fb7185", sizeMultiplier: 0.55 },
  // Conflict
  contradicts: { color: "#ef4444", sizeMultiplier: 0.85 },
  overrides: { color: "#ef4444", sizeMultiplier: 0.75 },
};

export const DEFAULT_EDGE_STYLE = { color: "#4a4a5a", sizeMultiplier: 0.45 };

// =============== FAMILY-TO-COLOR PALETTE (Pt 5) ===============
//
// Deterministic color schema driven by Polymath's extraction schema. Each
// canonical_family from `backend/services/graph/canonical_families.json`
// gets a stable hue here. Unmapped families fall back to amber (NODE_COLORS.Book).
//
// Picked to read as a galaxy: cyans, blues, violets dominate (cool stars),
// emeralds + ambers + roses for warm clusters, all bright enough against
// #06060a canvas.
export const FAMILY_TO_COLOR: Record<string, string> = {
  // Seeded from canonical_families.json — extend as new families surface.
  physics_simulation: "#22d3ee", // cyan       — blue-white stars
  creative_coding:    "#a78bfa", // violet-lt  — distant nebula
  cymatics:           "#06b6d4", // cyan-deep
  generative_ai:      "#10b981", // emerald    — green-yellow stars
  identity_extraction:"#34d399", // emerald-lt
  graph_rag:          "#3b82f6", // blue
  app_architecture:   "#8b5cf6", // violet-deep
  council_chat:       "#ec4899", // rose
  book_generation:    "#f59e0b", // amber      — sun-like
  mobile_ai:          "#84cc16", // lime
  prd_architecture:   "#6366f1", // indigo
  source_management:  "#fb7185", // rose-lt
  // Optional generic-purpose seeds for future families:
  philosophy:         "#a855f7",
  psychology:         "#f472b6",
  literature:         "#fb923c",
  software:           "#60a5fa",
  mathematics:        "#c084fc",
  economics:          "#a3e635",
  biology:            "#5eead4",
  history:            "#fbbf24",
};

/** Stable color for a canonical_family. Falls back to amber Book default. */
export function colorForFamily(family: string | null | undefined): string {
  if (!family) return NODE_COLORS.Book;
  return FAMILY_TO_COLOR[family] ?? NODE_COLORS.Book;
}

// Optional secondary palette — color by primary_entity_type when no
// canonical_family is set. Reuses the existing NODE_COLORS entries.
export function colorForEntityType(t: string | null | undefined): string {
  if (!t) return NODE_COLORS.Book;
  return (NODE_COLORS as any)[t] ?? NODE_COLORS.Book;
}

// =============== COMMUNITY / CORPUS PALETTES ===============
// Curated 12-color palettes for community- and corpus-keyed coloring
// (Mission Control supernode view + multi-corpus Brain View).
export const COMMUNITY_COLORS = [
  "#ef4444", "#f97316", "#eab308", "#22c55e",
  "#06b6d4", "#3b82f6", "#8b5cf6", "#d946ef",
  "#ec4899", "#f43f5e", "#14b8a6", "#84cc16",
];

export const getCommunityColor = (idx: number): string =>
  COMMUNITY_COLORS[Math.abs(idx) % COMMUNITY_COLORS.length];

export const CORPUS_COLORS = [
  "#f472b6", "#22d3ee", "#fbbf24", "#a78bfa",
  "#34d399", "#fb7185", "#60a5fa", "#a3e635",
  "#fcd34d", "#c084fc", "#5eead4", "#f97316",
];

// =============== NODE KIND INFERENCE ===============
// Maps a backend payload to its canonical PolymathNodeKind.
//   • supernode_type ∈ {domain, concept} → Domain | Concept
//   • is_cluster_anchor or kind="book"   → Book
//   • else entity_type lookup            → matching kind, fallback Other
export function inferNodeKind(node: any): PolymathNodeKind {
  if (node?.supernode_type === "domain") return "Domain";
  if (node?.supernode_type === "concept") return "Concept";
  if (node?.kind === "book" || node?.is_cluster_anchor) return "Book";
  const t = String(node?.entity_type || node?.primary_entity_type || "").trim();
  switch (t) {
    case "Person": return "Person";
    case "Organization": return "Organization";
    case "Location":
    case "Place": return "Location";
    case "Event": return "Event";
    case "Method": return "Method";
    case "Product": return "Product";
    case "Software": return "Software";
    case "Document": return "Document";
    case "Standard": return "Standard";
    case "Rule": return "Rule";
    case "Law": return "Law";
    case "Artifact": return "Artifact";
    case "TimeReference":
    case "Time":
    case "Date": return "TimeReference";
    case "Concept": return "Concept";
    default: return "Other";
  }
}

// =============== BRAIN VIEW SIGMA REDUCERS ===============
//
// `nodeReducer` runs once per node per draw — used to overlay Book anchors
// with a gold border + halo + larger label so books pop out of the canvas.
// `edgeReducer` thickens bridge edges by bridge strength so two books
// sharing many entities are visually obviously connected.

export interface BrainViewNodePayload {
  kind?: PolymathNodeKind;
  node_kind?: string;
  is_cluster_anchor?: boolean;
  label?: string;
  filename?: string;
  display_name?: string;
  name?: string;
  bridge_count?: number;
  [key: string]: any;
}

export interface BrainViewEdgePayload {
  relation_family?: string;
  strength?: number;
  shared_entities?: number;
  predicate?: string;
  [key: string]: any;
}

export function nodeReducer(node: BrainViewNodePayload) {
  const kind = (node.kind || node.node_kind || "Other") as PolymathNodeKind;
  const baseColor = NODE_COLORS[kind] ?? NODE_COLORS.Other;
  const baseSize = NODE_SIZES[kind] ?? NODE_SIZES.Other;
  const baseMass = NODE_MASSES[kind] ?? NODE_MASSES.Other;
  const label = node.label || node.filename || node.display_name || node.name || "";

  const base = {
    ...node,
    color: baseColor,
    size: baseSize,
    mass: baseMass,
    label,
  };

  // "Stars in the night sky" — Books render as small bright dots that
  // grow with bridge count, but the growth is now flatter + tightly
  // capped: 1-bridge book ≈ 4px, 12-bridge ≈ 7.7px, 100+ mega-hub caps
  // at 8px. The Math.min(4, …) ceiling on the log term prevents any
  // single mega-hub from dominating the canvas.
  if (kind === "Book" || node.is_cluster_anchor) {
    const bridges = Math.max(0, Number(node.bridge_count ?? 1));
    const size = 4 + Math.min(4, Math.log2(bridges + 1));
    return {
      ...base,
      size,
      mass: 22,
      labelSize: 12,
      labelWeight: "600",
      zIndex: 2,
    };
  }
  return base;
}

export function edgeReducer(edge: BrainViewEdgePayload) {
  const family = (edge.relation_family || "WeakAssociation") as RelationFamily;
  const baseColor = EDGE_COLORS_BY_FAMILY[family] ?? EDGE_COLORS_BY_FAMILY.WeakAssociation;
  const strength = Number(edge.strength ?? edge.shared_entities ?? 1);
  const faint = family === "WeakAssociation" || family === "Canonicalization";

  return {
    ...edge,
    color: baseColor,
    // 1.2 baseline + 0.18 per shared edge, capped at 5.5 so a single hub
    // can't blow the canvas out.
    size: Math.min(1.2 + Math.max(0, strength) * 0.18, 5.5),
    opacity: faint ? 0.35 : Math.min(0.75 + strength * 0.04, 0.95),
    type: "arrow",
    curvature: 0.6,
    zIndex: faint ? 0 : 1,
  };
}

// =============== BRAIN VIEW SIGMA CONFIG ===============
//
// Single object the Brain View renderer spreads onto its `<SigmaContainer>`
// (or its imperative Sigma constructor). Tuned for 1k–2k books + 60k+
// inter-book bridge edges.
export const BRAIN_VIEW_CONFIG = {
  // Renderer
  renderLabels: true,
  hideEdgesOnMove: true,
  zIndex: true,

  // Canvas — deepened from slate-900 to near-black so bright Book anchors
  // read as stars in the night sky (Pt 3 creative direction).
  backgroundColor: "#06060a",
  defaultNodeColor: NODE_COLORS.Other,
  defaultEdgeColor: EDGE_COLORS_BY_FAMILY.WeakAssociation,

  // Labels
  labelSize: 13,
  labelBackground: true,
  labelBackgroundOpacity: 0.9,
  labelColor: "#e2e8f0",
  labelDensity: 1.0,
  labelGridCellSize: 80,             // dedupe overlapping labels at scale
  labelRenderedSizeThreshold: 6,

  // Pt 7c — Edge labels (Brain View bridges).
  // Each bridge edge gets a midpoint label like "Bayes' theorem +5" built
  // by the adapter from `top_shared_entities`. The same slate-200 used by
  // node labels above so edges + nodes read as one typographic family;
  // size 10 keeps books visually dominant. Gated by the shared
  // `labelRenderedSizeThreshold` — labels auto-hide at low zoom or when
  // many bridges overlap, fade in when the user zooms into a region.
  renderEdgeLabels: true,
  edgeLabelColor: { color: "#e2e8f0" },
  edgeLabelSize: 10,
  edgeLabelFont:
    "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  edgeLabelWeight: "400",

  // Camera bounds — keep the user from zooming so far out the canvas blanks.
  minCameraRatio: 0.1,
  maxCameraRatio: 8,

  // Reducers (the most important part — visual anchor styling lives here).
  nodeReducer,
  edgeReducer,

  // ForceAtlas2 — books-as-clusters layout. linLogMode + adjustSizes makes
  // anchors and their orbiting entities settle into clear rings.
  fa2: {
    barnesHutOptimize: true,
    gravity: 1.2,
    scalingRatio: 12,
    slowDown: 4,
    adjustSizes: true,
    linLogMode: true,
    strongGravityMode: false,
  },

  // No-overlap pass after FA2 converges — keeps Book anchor labels readable.
  noverlap: {
    nodeMargin: 10,
    scaleNodes: 1.3,
    speed: 4,
  },
};
