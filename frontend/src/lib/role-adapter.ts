/**
 * Role adapter — assigns each node / edge in a Polymath graph payload a
 * single semantic role from `NodeRole`. One role per node; the role is
 * the dominant visual signal everywhere the node is shown.
 *
 * Priority order (highest wins):
 *   1. cross_corpus  — appears in >1 source corpora
 *   2. query_matched — explicit seed from the user's query
 *   3. anchor        — Book / Domain / is_cluster_anchor
 *   4. bridge        — appears in the bridges list or predicate=bridges_to
 *   5. hub           — top-N by degree / mention_count
 *   6. gap           — appears in the gaps list
 *   7. evidence      — explicit source_doc in synthesis.trace
 *   8. frontier      — fallback (long-tail, peripheral)
 *
 * Edges follow their own classification:
 *   - predicate=bridges_to  → bridge
 *   - in gaps               → gap
 *   - everything else       → support (uses role palette for evidence)
 */

import type { NodeRole } from "./design-tokens";
import { colorTokens } from "./design-tokens";

export interface RoleAssignment {
  /** The dominant role for this node. */
  role: NodeRole;
  /** Whether this node lives in evidence (corpus/graph/web lanes). */
  inEvidence: boolean;
  /** The lane the node's evidence came from, if any. */
  lane?: "corpus" | "graph" | "web";
}

export interface RoleContext {
  seedIds: Set<string>;
  hubIds: Set<string>;
  bridgeIds: Set<string>;
  gapIds: Set<string>;
  /** Top-N frontier nodes by degree — computed once per payload. */
  frontierIds: Set<string>;
  /** Web-evidence node ids (synthesis web_search) if known. */
  webIds: Set<string>;
}

export function computeRoleContext(
  nodes: any[],
  links: any[],
  seedIds: Set<string>,
  hubIds: Set<string>,
  bridgeIds: Set<string>,
  gaps: any[] = [],
  options?: { frontierLimit?: number },
): RoleContext {
  const frontierLimit = options?.frontierLimit ?? 12;

  const gapIds = new Set<string>();
  for (const g of gaps) {
    const a = String(g?.entity_a_id ?? g?.entity_a_name ?? "");
    const b = String(g?.entity_b_id ?? g?.entity_b_name ?? "");
    if (a) gapIds.add(a);
    if (b) gapIds.add(b);
  }

  // Degree from visible links.
  const degree = new Map<string, number>();
  for (const l of links || []) {
    const s = String(endpointId(l?.source));
    const t = String(endpointId(l?.target));
    if (!s || !t) continue;
    degree.set(s, (degree.get(s) ?? 0) + 1);
    degree.set(t, (degree.get(t) ?? 0) + 1);
  }

  // Frontier = top-N by degree minus anchors/seeds/hubs/bridges.
  const ranked = [...(nodes || [])]
    .map((n) => ({
      id: String(n?.id ?? ""),
      d: degree.get(String(n?.id ?? "")) ?? 0,
    }))
    .filter((x) => x.id);
  ranked.sort((a, b) => b.d - a.d);
  const skipIds = new Set<string>([...seedIds, ...hubIds, ...bridgeIds]);
  const frontierIds = new Set<string>();
  for (const r of ranked) {
    if (frontierIds.size >= frontierLimit) break;
    if (skipIds.has(r.id)) continue;
    frontierIds.add(r.id);
  }

  return {
    seedIds: new Set([...seedIds].map(String)),
    hubIds: new Set([...hubIds].map(String)),
    bridgeIds: new Set([...bridgeIds].map(String)),
    gapIds,
    frontierIds,
    webIds: new Set(),
  };
}

function endpointId(value: unknown): string {
  if (value && typeof value === "object") {
    const obj = value as { id?: unknown; key?: unknown };
    return String(obj.id ?? obj.key ?? "");
  }
  return String(value ?? "");
}

export function assignNodeRole(node: any, ctx: RoleContext): RoleAssignment {
  const id = String(node?.id ?? "");
  const corpora = Array.isArray(node?.source_corpora) ? node.source_corpora : node?.source_corpus ? [node.source_corpus] : [];
  const isAnchor =
    node?.is_cluster_anchor === true ||
    node?.kind === "book" ||
    node?.supernode_type === "domain" ||
    node?.nodeKind === "Domain" ||
    node?.nodeKind === "Book";

  if (corpora.length > 1) {
    return { role: "cross_corpus", inEvidence: corpora.length > 0, lane: "corpus" };
  }
  if (ctx.seedIds.has(id)) return { role: "query_matched", inEvidence: true, lane: "corpus" };
  if (isAnchor) return { role: "anchor", inEvidence: true, lane: "corpus" };
  if (ctx.bridgeIds.has(id)) return { role: "bridge", inEvidence: true, lane: "graph" };
  if (ctx.gapIds.has(id)) return { role: "gap", inEvidence: false };
  if (ctx.hubIds.has(id)) return { role: "hub", inEvidence: true, lane: "graph" };
  if (ctx.webIds.has(id)) return { role: "evidence", inEvidence: true, lane: "web" };
  if (ctx.frontierIds.has(id)) return { role: "frontier", inEvidence: false };
  return { role: "evidence", inEvidence: true, lane: "corpus" };
}

export function roleHex(role: NodeRole): string {
  return colorTokens.role[role];
}

/** Compact CSS variable for a role — usable in inline styles. */
export function roleCssVar(role: NodeRole): string {
  return `var(--role-${role.replace(/_/g, "-")})`;
}