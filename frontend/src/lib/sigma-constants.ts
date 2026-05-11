/**
 * Polymath sigma rendering constants — port of GitNexus's
 * `gitnexus-web/src/lib/constants.ts` adapted to Polymath's node taxonomy.
 *
 * GitNexus has hierarchical code-graph types (Project / Folder / File /
 * Class / Function...). Polymath has a different mental model:
 *
 *   • Domain supernodes      = top-level Louvain domain communities
 *   • Concept supernodes     = sub-community (concept neighborhoods)
 *   • Book clusters          = each Document, when in books-as-clusters mode
 *   • Entity nodes           = real Neo4j Entity nodes (in drill / query mode)
 *
 * Colors and sizes are tuned for the same visual hierarchy GitNexus uses
 * (structural anchors are large, content nodes are small) so the canvas
 * has a clear figure/ground when looking at hundreds of nodes at once.
 */

export type PolymathNodeKind =
  | "Domain"
  | "Concept"
  | "Book"
  | "Person"
  | "Organization"
  | "Product"
  | "Method"
  | "ConceptEntity"
  | "Document"
  | "Event"
  | "Place"
  | "Artifact"
  | "Rule"
  | "Time"
  | "Other";

// Tuned for dark backgrounds — same palette family as GitNexus but mapped
// to Polymath's semantic types instead of code-element types.
export const NODE_COLORS: Record<PolymathNodeKind, string> = {
  Domain: "#a855f7", // Purple — the big anchors (matches GitNexus Project)
  Concept: "#818cf8", // Indigo — concept communities (matches GitNexus Community)
  Book: "#f59e0b", // Amber — file-as-cluster (visual cousin of GitNexus File)
  Person: "#ec4899", // Pink
  Organization: "#fbbf24", // Yellow-amber
  Product: "#22d3ee", // Cyan
  Method: "#10b981", // Emerald
  ConceptEntity: "#a78bfa", // Violet light (entity-level concept; distinct from Concept supernode)
  Document: "#94a3b8", // Slate (de-emphasized — document references shouldn't dominate)
  Event: "#fb7185", // Rose
  Place: "#fcd34d", // Gold
  Artifact: "#5eead4", // Teal
  Rule: "#c084fc", // Purple-light
  Time: "#60a5fa", // Blue
  Other: "#9ca3af", // Cool gray fallback
};

// Sizes follow GitNexus's "structural nodes are MUCH larger" principle —
// dramatic differences make the cluster/anchor structure obvious before
// the user reads any label.
export const NODE_SIZES: Record<PolymathNodeKind, number> = {
  Domain: 18, // Largest — anchors the canvas
  Concept: 11, // Mid-large — readable as cluster head
  Book: 14, // File-as-cluster anchor — between domain and concept
  Person: 5,
  Organization: 6,
  Product: 7,
  Method: 4,
  ConceptEntity: 5,
  Document: 3,
  Event: 4,
  Place: 4,
  Artifact: 4,
  Rule: 4,
  Time: 3,
  Other: 4,
};

// ForceAtlas2 mass — higher mass = stronger repulsion = nodes spread further.
// Anchors (Domain, Book) get high mass so they fan out and pull their
// children with them. Entity-level nodes are light → they cluster around
// their parent supernode rather than fighting it.
export const NODE_MASSES: Record<PolymathNodeKind, number> = {
  Domain: 40,
  Concept: 18,
  Book: 25,
  Person: 3,
  Organization: 4,
  Product: 5,
  Method: 2,
  ConceptEntity: 3,
  Document: 2,
  Event: 2,
  Place: 2,
  Artifact: 2,
  Rule: 2,
  Time: 2,
  Other: 2,
};

// Curated 12-color community palette for nodes coloured by their domain
// or community membership rather than entity type. Indexed by the
// community/domain integer so two adjacent communities have visually
// distinct hues.
export const COMMUNITY_COLORS = [
  "#ef4444", // red
  "#f97316", // orange
  "#eab308", // yellow
  "#22c55e", // green
  "#06b6d4", // cyan
  "#3b82f6", // blue
  "#8b5cf6", // violet
  "#d946ef", // fuchsia
  "#ec4899", // pink
  "#f43f5e", // rose
  "#14b8a6", // teal
  "#84cc16", // lime
];

export const getCommunityColor = (idx: number): string =>
  COMMUNITY_COLORS[Math.abs(idx) % COMMUNITY_COLORS.length];

// Per-corpus palette — when colorMode === "corpus", each corpus gets a
// hue from this set so adjacent corpora visually contrast.
export const CORPUS_COLORS = [
  "#f472b6", // pink
  "#22d3ee", // cyan
  "#fbbf24", // amber
  "#a78bfa", // violet
  "#34d399", // emerald
  "#fb7185", // rose
  "#60a5fa", // blue
  "#a3e635", // lime
  "#fcd34d", // yellow
  "#c084fc", // purple
  "#5eead4", // teal
  "#f97316", // orange
];

// Edge color by relationship "kind" — drawn from Polymath's relation_family
// values surfaced by services/graph/orchestrator and neo4j_reader.
export const EDGE_STYLES: Record<string, { color: string; sizeMultiplier: number }> = {
  // Membership / containment (faint, structural)
  in_book: { color: "#2d5a3d", sizeMultiplier: 0.35 },
  member_of: { color: "#2d5a3d", sizeMultiplier: 0.35 },
  contains: { color: "#0e7490", sizeMultiplier: 0.45 },
  part_of: { color: "#0e7490", sizeMultiplier: 0.45 },
  // Bridges between clusters / docs
  bridges_to: { color: "#7c3aed", sizeMultiplier: 0.7 },
  shared_hub: { color: "#7c3aed", sizeMultiplier: 0.6 },
  // Operational / behavioral
  uses: { color: "#1d4ed8", sizeMultiplier: 0.6 },
  produces: { color: "#1d4ed8", sizeMultiplier: 0.6 },
  detects: { color: "#1d4ed8", sizeMultiplier: 0.55 },
  // Type / semantic
  references: { color: "#a78bfa", sizeMultiplier: 0.5 },
  derived_from: { color: "#c2410c", sizeMultiplier: 0.7 },
  implements: { color: "#be185d", sizeMultiplier: 0.7 },
  depends_on: { color: "#0e7490", sizeMultiplier: 0.6 },
  // Detector edges from the analytics pipeline
  fragile_bridge: { color: "#fb923c", sizeMultiplier: 0.9 },
  structural_analog: { color: "#5eead4", sizeMultiplier: 0.7 },
  terminological_gap: { color: "#fb7185", sizeMultiplier: 0.55 },
  // Conflict / contradiction
  contradicts: { color: "#fb7185", sizeMultiplier: 0.85 },
  overrides: { color: "#fb7185", sizeMultiplier: 0.75 },
};

export const DEFAULT_EDGE_STYLE = { color: "#4a4a5a", sizeMultiplier: 0.45 };

// Map a Polymath payload node to one of the canonical node kinds above.
export function inferNodeKind(node: any): PolymathNodeKind {
  if (node.supernode_type === "domain") return "Domain";
  if (node.supernode_type === "concept") return "Concept";
  if (node.kind === "book" || node.is_cluster_anchor) return "Book";
  const t = String(node.entity_type || "").trim();
  if (t === "Person") return "Person";
  if (t === "Organization") return "Organization";
  if (t === "Product") return "Product";
  if (t === "Method") return "Method";
  if (t === "Concept") return "ConceptEntity";
  if (t === "Document") return "Document";
  if (t === "Event") return "Event";
  if (t === "Place") return "Place";
  if (t === "Artifact") return "Artifact";
  if (t === "Rule") return "Rule";
  if (t === "Time" || t === "TimeReference" || t === "Date") return "Time";
  return "Other";
}
