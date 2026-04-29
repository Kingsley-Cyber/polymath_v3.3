import type {
  FullGraphEdge,
  FullGraphNode,
  FullGraphResponse,
} from "./api";
import type {
  ContextGraphJumpTarget,
  ContextGraphLink,
  ContextGraphNode,
  ContextGraphPayload,
  DiscoverGraphLink,
  DiscoverGraphNode,
  GraphDiscoverResponse,
} from "../types";

const FALLBACK_CONCEPT_CAP = 64;
const FALLBACK_DOCUMENT_CAP = 8;
const OVERVIEW_TOPIC_CAP = 10;
const OVERVIEW_CONCEPT_CAP = 42;

type ReceiptFile = {
  doc_id: string;
  source_label: string;
  chunk_count: number;
  chunk_ids: string[];
  has_temporal?: boolean;
};

export function contextGraphFromDiscoverResponse(
  response: GraphDiscoverResponse | null | undefined,
): ContextGraphPayload | null {
  if (!response) return null;
  if (response.context_graph?.nodes?.length) return response.context_graph;
  return legacyContextGraphFromResponse(response);
}

export function contextGraphFromOverviewResponse(
  response: FullGraphResponse | null | undefined,
): ContextGraphPayload | null {
  if (!response?.nodes?.length) return null;

  const domainNodes = response.nodes
    .filter((node) => node.supernode_type === "domain" || node.entity_type === "domain")
    .sort((a, b) => graphOverviewNodeScore(b) - graphOverviewNodeScore(a))
    .slice(0, OVERVIEW_TOPIC_CAP);
  const concepts = response.nodes
    .filter((node) => node.supernode_type === "concept" || node.entity_type?.includes("concept"))
    .sort((a, b) => graphOverviewNodeScore(b) - graphOverviewNodeScore(a))
    .slice(0, OVERVIEW_CONCEPT_CAP);

  if (domainNodes.length === 0 && concepts.length === 0) return null;

  const nodes: ContextGraphNode[] = [];
  const links: ContextGraphLink[] = [];
  const seenNodes = new Set<string>();
  const seenLinks = new Set<string>();
  const domainTopicByName = new Map<string, string>();

  for (const domain of domainNodes) {
    const topicId = overviewTopicId(domain);
    domainTopicByName.set(domain.display_name, topicId);
    addNode(nodes, seenNodes, {
      id: `topic:${topicId}`,
      label: domain.display_name || "Corpus Neighborhood",
      kind: "topic",
      role: "corpus_neighborhood",
      topic_id: null,
      size: 8 + Math.min(7, Math.sqrt(Math.max(1, domain.mention_count || 1))),
      weight: Math.max(1, domain.mention_count || 1),
      evidence_count: Math.max(1, domain.top_entities?.length || 1),
      top_entities: domain.top_entities || [],
      jump_targets: [
        {
          section: "trace",
          label: "corpus neighborhood",
          detail: domain.display_name,
          target_id: domain.id,
        },
      ],
    });
  }

  const visibleConceptIds = new Set<string>();
  const membershipLinkLimit = domainNodes.length <= 1 ? 12 : OVERVIEW_CONCEPT_CAP;
  let membershipLinkCount = 0;
  for (const concept of concepts) {
    const topicId = topicIdForOverviewConcept(concept, domainTopicByName);
    if (topicId && !seenNodes.has(`topic:${topicId}`)) {
      addNode(nodes, seenNodes, overviewSyntheticTopic(topicId, concept.primary_domain || "Other"));
    }
    visibleConceptIds.add(concept.id);
    addNode(nodes, seenNodes, {
      id: concept.id,
      label: concept.display_name || concept.id,
      kind: "concept",
      role: Number(concept.bridge_count || 0) > 0 ? "bridge_anchor" : "corpus_concept",
      topic_id: topicId,
      size: 3 + Math.min(8, Math.sqrt(Math.max(1, concept.mention_count || 1)) * 0.9),
      weight: Math.max(1, concept.mention_count || 1),
      evidence_count: Math.max(1, concept.top_entities?.length || 1),
      top_entities: concept.top_entities || [],
      jump_targets: [
        {
          section: "trace",
          label: "corpus concept",
          detail: concept.display_name || concept.id,
          target_id: concept.id,
        },
      ],
    });
    if (topicId && membershipLinkCount < membershipLinkLimit) {
      membershipLinkCount += 1;
      addLink(links, seenLinks, {
        source: `topic:${topicId}`,
        target: concept.id,
        kind: "topic_overlay",
        role: "topic_overlay",
        weight: Math.max(0.2, Math.min(1.2, Number(concept.mention_count || 1) / 40)),
        suggested: false,
        evidence: "Cached corpus overview membership",
      });
    }
  }

  for (const edge of response.edges || []) {
    if (!visibleConceptIds.has(edge.source) || !visibleConceptIds.has(edge.target)) continue;
    addLink(links, seenLinks, overviewContextLink(edge));
  }

  return {
    nodes,
    links,
    meta: {
      default_view: "context_map",
      source: "cached_corpus_overview",
      grouping_basis:
        "Corpus opening view: cached concept communities are shown as a bounded overview. Run a graph query to replace this with query-specific evidence files, bridges, and gaps.",
      topic_count: nodes.filter((node) => node.kind === "topic").length,
      concept_count: visibleConceptIds.size,
      raw_concept_count: response.concept_count ?? response.nodes.length,
      hidden_concept_count: Math.max(0, (response.concept_count ?? response.nodes.length) - visibleConceptIds.size),
      visible_concept_cap: OVERVIEW_CONCEPT_CAP,
      document_count: 0,
    },
  };
}

function legacyContextGraphFromResponse(response: GraphDiscoverResponse): ContextGraphPayload | null {
  const sourceNodes = response.graph?.nodes || [];
  const files = receiptFiles(response).slice(0, FALLBACK_DOCUMENT_CAP);
  if (sourceNodes.length === 0 && files.length === 0) return null;

  const nodes: ContextGraphNode[] = [];
  const links: ContextGraphLink[] = [];
  const seenNodes = new Set<string>();
  const seenLinks = new Set<string>();
  const topicCounts = new Map<string, number>();
  const topicLabels = new Map<string, string>();
  const topicTopEntities = new Map<string, string[]>();
  const nodeTopic = new Map<string, string>();

  const communityById = new Map(
    (response.concept_communities || []).map((community) => [
      normalizeTopicId(community.concept_id),
      community,
    ]),
  );

  const ranked = [...sourceNodes].sort((a, b) => {
    const scoreDelta = graphNodeScore(b) - graphNodeScore(a);
    return scoreDelta || String(a.label || a.id).localeCompare(String(b.label || b.id));
  });
  const visibleSourceNodes = ranked.slice(0, FALLBACK_CONCEPT_CAP);
  const visibleIds = new Set(visibleSourceNodes.map((node) => node.id));

  for (const graphNode of visibleSourceNodes) {
    const topic = topicForGraphNode(response, graphNode, communityById);
    if (topic) {
      nodeTopic.set(graphNode.id, topic.id);
      topicCounts.set(topic.id, (topicCounts.get(topic.id) || 0) + 1);
      topicLabels.set(topic.id, topic.label);
      topicTopEntities.set(topic.id, topic.topEntities);
    }
  }

  for (const [topicId, count] of topicCounts) {
    const label = topicLabels.get(topicId) || titleFromId(topicId);
    addNode(nodes, seenNodes, {
      id: `topic:${topicId}`,
      label,
      kind: "topic",
      role: "query_neighborhood",
      topic_id: null,
      size: 8 + Math.min(7, Math.sqrt(count) * 2.1),
      weight: count,
      evidence_count: count,
      top_entities: topicTopEntities.get(topicId) || [],
      jump_targets: [
        {
          section: "themes",
          label: "theme",
          detail: label,
          target_id: topicId,
        },
      ],
    });
  }

  for (const graphNode of visibleSourceNodes) {
    const topicId = nodeTopic.get(graphNode.id) || null;
    addNode(nodes, seenNodes, {
      id: graphNode.id,
      label: graphNode.label || graphNode.id,
      kind: "concept",
      role: roleForGraphNode(graphNode),
      topic_id: topicId,
      size: 3 + Math.min(8, Math.sqrt(Math.max(1, graphNode.degree || 1))),
      weight: Math.max(1, graphNode.degree || 1),
      evidence_count: Math.max(1, graphNode.degree || 1),
      top_entities: [graphNode.label || graphNode.id].filter(Boolean),
      jump_targets: jumpTargetsForGraphNode(response, graphNode, topicId),
    });

    if (topicId) {
      addLink(links, seenLinks, {
        source: `topic:${topicId}`,
        target: graphNode.id,
        kind: "topic_overlay",
        role: "query_neighborhood",
        weight: 1,
        suggested: false,
        evidence: `Grouped by query-scoped concept neighborhood: ${topicLabels.get(topicId) || topicId}`,
      });
    }
  }

  for (const graphLink of response.graph?.links || []) {
    if (!visibleIds.has(graphLink.source) || !visibleIds.has(graphLink.target)) continue;
    addLink(links, seenLinks, {
      source: graphLink.source,
      target: graphLink.target,
      kind: graphLink.predicate || graphLink.classification || "query_relation",
      role: roleForGraphLink(graphLink),
      weight: Math.max(0.3, Number(graphLink.confidence ?? 0.7)),
      suggested: graphLink.emphasis === "gap_edge",
      evidence: graphLink.evidence || graphLink.relation_family || graphLink.classification || "query graph edge",
    });
  }

  const visibleConceptIds = visibleSourceNodes.map((node) => node.id);
  files.forEach((file, index) => {
    const anchorId = visibleConceptIds[index % Math.max(1, visibleConceptIds.length)];
    const topicId = anchorId ? nodeTopic.get(anchorId) || null : null;
    const docId = `doc:${file.doc_id || index + 1}`;
    const sourceLabel = readableSourceLabel(file.source_label || file.doc_id, index);
    addNode(nodes, seenNodes, {
      id: docId,
      label: sourceLabel,
      kind: "document",
      role: "evidence_document",
      topic_id: topicId,
      size: 4 + Math.min(5, Math.sqrt(Math.max(1, file.chunk_count))),
      weight: Math.max(1, file.chunk_count),
      evidence_count: Math.max(1, file.chunk_count),
      top_entities: [],
      jump_targets: [
        {
          section: "trace",
          label: "file receipt",
          detail: sourceLabel,
          target_id: file.doc_id,
        },
      ],
    });
    if (anchorId) {
      addLink(links, seenLinks, {
        source: docId,
        target: anchorId,
        kind: "document_context",
        role: "document_context",
        weight: Math.max(1, file.chunk_count),
        suggested: false,
        evidence: `${sourceLabel} supplied ${file.chunk_count || 1} chunk${file.chunk_count === 1 ? "" : "s"}`,
      });
    }
  });

  return {
    nodes,
    links,
    meta: {
      default_view: "context_map",
      source: "frontend_legacy_response_fallback",
      grouping_basis:
        "Legacy session fallback: only the concepts and files returned by this query are grouped by their available concept labels; this is not a whole-corpus bucket view.",
      topic_count: topicCounts.size,
      concept_count: visibleSourceNodes.length,
      raw_concept_count: sourceNodes.length,
      hidden_concept_count: Math.max(0, sourceNodes.length - visibleSourceNodes.length),
      visible_concept_cap: FALLBACK_CONCEPT_CAP,
      document_count: files.length,
    },
  };
}

function addNode(nodes: ContextGraphNode[], seen: Set<string>, node: ContextGraphNode) {
  if (seen.has(node.id)) return;
  seen.add(node.id);
  nodes.push(node);
}

function addLink(links: ContextGraphLink[], seen: Set<string>, link: ContextGraphLink) {
  if (link.source === link.target) return;
  const key = `${link.source}->${link.target}:${link.role}`;
  if (seen.has(key)) return;
  seen.add(key);
  links.push(link);
}

function graphNodeScore(node: DiscoverGraphNode): number {
  const emphasis = String(node.emphasis || "");
  const degree = Math.sqrt(Math.max(1, node.degree || 1));
  if (emphasis.includes("bridge") || emphasis.includes("analogy") || emphasis === "transfer_hub") {
    return 100 + degree;
  }
  if (emphasis.includes("weak") || emphasis.includes("fragile")) return 75 + degree;
  if (emphasis.includes("gap")) return 68 + degree;
  if (emphasis === "frontier") return 55 + degree;
  return 20 + degree;
}

function roleForGraphNode(node: DiscoverGraphNode): string {
  const emphasis = String(node.emphasis || "");
  if (emphasis.includes("bridge") || emphasis.includes("analogy") || emphasis === "transfer_hub") {
    return "bridge_anchor";
  }
  if (emphasis.includes("weak") || emphasis.includes("fragile")) return "weak_link";
  if (emphasis.includes("gap")) return "suggested_gap";
  return "query_entity";
}

function roleForGraphLink(link: DiscoverGraphLink): string {
  const emphasis = String(link.emphasis || "");
  if (emphasis.includes("bridge") || emphasis.includes("analogy")) return "bridge";
  if (emphasis.includes("weak") || emphasis.includes("fragile")) return "weak_link";
  if (emphasis.includes("gap")) return "suggested_gap";
  if (emphasis.includes("context")) return "context_edge";
  return "context_edge";
}

function graphOverviewNodeScore(node: FullGraphNode): number {
  const bridgeBoost = Number(node.bridge_count || 0) * 12;
  const sizeScore = Math.sqrt(Math.max(1, node.mention_count || 1));
  const domainBoost = node.supernode_type === "domain" || node.entity_type === "domain" ? 20 : 0;
  return domainBoost + bridgeBoost + sizeScore;
}

function overviewTopicId(node: FullGraphNode): string {
  return normalizeTopicId(node.id || node.display_name || "corpus");
}

function topicIdForOverviewConcept(
  concept: FullGraphNode,
  domainTopicByName: Map<string, string>,
): string | null {
  const primary = concept.primary_domain || "";
  if (primary && domainTopicByName.has(primary)) return domainTopicByName.get(primary) || null;
  if (primary) return normalizeTopicId(primary);
  return null;
}

function overviewSyntheticTopic(topicId: string, label: string): ContextGraphNode {
  return {
    id: `topic:${topicId}`,
    label: titleFromId(label || topicId),
    kind: "topic",
    role: "corpus_neighborhood",
    topic_id: null,
    size: 7,
    weight: 1,
    evidence_count: 1,
    top_entities: [],
    jump_targets: [
      {
        section: "trace",
        label: "corpus neighborhood",
        detail: label || topicId,
        target_id: topicId,
      },
    ],
  };
}

function overviewContextLink(edge: FullGraphEdge): ContextGraphLink {
  const predicate = String(edge.predicate || "context_edge");
  const isGap = predicate.toLowerCase().includes("gap");
  const isWeak = predicate.toLowerCase().includes("fragile") || predicate.toLowerCase().includes("weak");
  const isBridge = predicate.toLowerCase().includes("analog") || predicate.toLowerCase().includes("bridge");
  return {
    source: edge.source,
    target: edge.target,
    kind: predicate,
    role: isGap ? "suggested_gap" : isWeak ? "weak_link" : isBridge ? "bridge" : "context_edge",
    weight: Math.max(0.3, Number(edge.weight ?? edge.confidence ?? 0.7)),
    suggested: isGap,
    evidence: predicate.replace(/[_-]+/g, " "),
  };
}

function jumpTargetsForGraphNode(
  response: GraphDiscoverResponse,
  node: DiscoverGraphNode,
  topicId: string | null,
): ContextGraphJumpTarget[] {
  const jumps: ContextGraphJumpTarget[] = [];
  const concept = response.entity_concept_map?.[node.id];
  if (topicId || concept?.label) {
    jumps.push({
      section: "themes",
      label: "theme",
      detail: concept?.label || titleFromId(topicId || ""),
      target_id: topicId || concept?.concept_id || null,
    });
  }
  if (roleForGraphNode(node) === "bridge_anchor") {
    jumps.push({
      section: "bridges",
      label: "bridge",
      detail: node.label || node.id,
      target_id: node.id,
    });
  }
  if (["weak_link", "suggested_gap"].includes(roleForGraphNode(node))) {
    jumps.push({
      section: "gaps",
      label: roleForGraphNode(node) === "weak_link" ? "weak link" : "gap",
      detail: node.label || node.id,
      target_id: node.id,
    });
  }
  jumps.push({
    section: "trace",
    label: "trace",
    detail: "working-set node",
    target_id: node.id,
  });
  return jumps;
}

function topicForGraphNode(
  response: GraphDiscoverResponse,
  node: DiscoverGraphNode,
  communityById: Map<string, NonNullable<GraphDiscoverResponse["concept_communities"]>[number]>,
): { id: string; label: string; topEntities: string[] } | null {
  const mapped = response.entity_concept_map?.[node.id];
  const directId = mapped?.concept_id || node.concept || node.domain_type || node.canonical_family || "";
  if (!directId) return null;
  const id = normalizeTopicId(directId);
  const community = communityById.get(id);
  return {
    id,
    label: mapped?.label || community?.label || titleFromId(id),
    topEntities: mapped?.top_entities || community?.top_entities || [],
  };
}

function normalizeTopicId(value: string): string {
  return String(value || "")
    .replace(/^topic:/, "")
    .trim();
}

function titleFromId(value: string): string {
  return String(value || "Query Neighborhood")
    .replace(/^topic:/, "")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function readableSourceLabel(label: string, index: number): string {
  const clean = String(label || "").trim();
  if (/^[a-f0-9]{32,}$/i.test(clean)) return `Source ${index + 1} (${clean.slice(0, 8)})`;
  if (clean.length > 64) return `${clean.slice(0, 61).trim()}...`;
  return clean || `Source ${index + 1}`;
}

function receiptFiles(response: GraphDiscoverResponse): ReceiptFile[] {
  const direct = response.trace?.llm_context?.files;
  if (direct?.length) return uniqueReceiptFiles(direct);
  return uniqueReceiptFiles((response.trace?.source_docs || []).slice(0, 12).map((doc, index) => {
    const record = doc as Record<string, unknown>;
    const docId = String(record.doc_id ?? record.id ?? record.document_id ?? `source-${index + 1}`);
    const chunkIds = Array.isArray(record.chunk_ids) ? record.chunk_ids.map(String) : [];
    return {
      doc_id: docId,
      source_label: String(record.source_label ?? record.filename ?? record.title ?? record.source ?? docId),
      chunk_count: Number(record.chunk_count ?? record.chunks ?? chunkIds.length) || chunkIds.length || 1,
      chunk_ids: chunkIds,
      has_temporal: Boolean(record.has_temporal ?? record.timestamp ?? record.created_at),
    };
  }));
}

function uniqueReceiptFiles(files: ReceiptFile[]): ReceiptFile[] {
  const bySource = new Map<string, ReceiptFile>();
  for (const file of files) {
    const key = `${file.doc_id || ""}:${file.source_label || ""}`;
    const existing = bySource.get(key);
    if (!existing) {
      bySource.set(key, {
        ...file,
        chunk_ids: [...new Set(file.chunk_ids || [])],
      });
      continue;
    }
    const chunkIds = [...new Set([...(existing.chunk_ids || []), ...(file.chunk_ids || [])])];
    existing.chunk_ids = chunkIds;
    existing.chunk_count = Math.max(existing.chunk_count || 0, file.chunk_count || 0, chunkIds.length);
    existing.has_temporal = Boolean(existing.has_temporal || file.has_temporal);
  }
  return [...bySource.values()];
}
