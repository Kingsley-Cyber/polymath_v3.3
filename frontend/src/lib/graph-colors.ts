export const graphColors = {
  entity: {
    Person: "#3b82f6",
    Organization: "#10b981",
    Method: "#8b5cf6",
    Product: "#f59e0b",
    Concept: "#ec4899",
    Document: "#6b7280",
    Artifact: "#0ea5e9",
    RobloxService: "#dc2626",
    RobloxClass: "#7c3aed",
    RobloxNetworkPrimitive: "#ea580c",
    LuauDataType: "#0891b2",
  },
  relation: {
    supports: "#94a3b8",
    mentions: "#cbd5e1",
    bridges: "#a78bfa",
    gap: "#fca5a5",
  },
  state: {
    selected: "#ffffff",
    hovered: "#fde68a",
    inactive: "#4b5563",
    crossCorpus: "#f59e0b",
  },
  atomic: {
    background: "#fafaf7",
    ring: "#e2e8f0",
    nucleus: "#1e293b",
    nucleusStroke: "#0f172a",
    evidence: "#f8fafc",
    bridgeFill: "#f1f5f9",
    crossCorpusBridgeFill: "#fef3c7",
    gapFill: "#fef2f2",
    label: "#1e293b",
    tooltipBg: "#0f172a",
  },
} as const;

export function corpusHue(corpusId: string): number {
  let h = 0;
  for (const ch of corpusId) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % 360;
}

export function corpusColor(corpusId: string): string {
  return `hsl(${corpusHue(corpusId)}, 65%, 50%)`;
}

export function nodeFillColor(
  entityType: string | undefined,
  sourceCorpora: string[] | undefined,
): string {
  if (sourceCorpora && sourceCorpora.length > 1) {
    return graphColors.state.crossCorpus;
  }
  if (
    entityType &&
    Object.prototype.hasOwnProperty.call(graphColors.entity, entityType)
  ) {
    return graphColors.entity[entityType as keyof typeof graphColors.entity];
  }
  if (sourceCorpora && sourceCorpora.length === 1) {
    return corpusColor(sourceCorpora[0]);
  }
  return "#64748b";
}
