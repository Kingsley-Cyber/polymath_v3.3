export type QueryLayoutMode =
  | "force"
  | "radial"
  | "bipartite"
  | "chain"
  | "cluster"
  | "hierarchy";

export interface QueryFingerprint {
  layoutMode: QueryLayoutMode;
  intent:
    | "mechanism"
    | "causal"
    | "comparison"
    | "path"
    | "enumeration"
    | "containment"
    | "general";
  tokenCount: number;
  interrogativeDepth: number;
  repulsionMultiplier: number;
  springMultiplier: number;
  dampingMultiplier: number;
  edgeCurvature: number;
}

const WORD_RE = /[a-z0-9][a-z0-9.+#-]*/gi;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

export function fingerprintGraphQuery(query: string): QueryFingerprint {
  const normalized = query.trim().toLowerCase();
  const tokens = normalized.match(WORD_RE) ?? [];
  const tokenCount = tokens.length;
  const has = (pattern: RegExp) => pattern.test(normalized);

  let layoutMode: QueryLayoutMode = "force";
  let intent: QueryFingerprint["intent"] = "general";

  if (has(/\b(compare|contrast|versus|vs\.?|difference|similarit(?:y|ies))\b/)) {
    layoutMode = "bipartite";
    intent = "comparison";
  } else if (
    has(/\b(trace|path|route|from\b.+\bto|through|evolution|progression)\b/)
  ) {
    layoutMode = "chain";
    intent = "path";
  } else if (has(/\b(why|cause|causes|causal|because|lead(?:s)? to|depends on)\b/)) {
    layoutMode = "chain";
    intent = "causal";
  } else if (has(/\b(how|mechanism|process|workflow|pipeline|works?|operate)\b/)) {
    layoutMode = "radial";
    intent = "mechanism";
  } else if (has(/\b(what contains|contains|part of|components?|subsystems?|inside)\b/)) {
    layoutMode = "hierarchy";
    intent = "containment";
  } else if (has(/\b(list|all|enumerate|catalog|map out|show me)\b/)) {
    layoutMode = "cluster";
    intent = "enumeration";
  }

  const questionMarks = (query.match(/\?/g) ?? []).length;
  const interrogatives = tokens.filter((t) =>
    ["how", "why", "what", "where", "when", "which", "compare", "trace"].includes(t),
  ).length;
  const interrogativeDepth = clamp(questionMarks + interrogatives, 0, 5);
  const lengthPressure = clamp((tokenCount - 8) / 28, 0, 1);

  const modePhysics: Record<QueryLayoutMode, [number, number, number, number]> = {
    force: [1.0, 1.0, 1.0, 0.18],
    radial: [1.15, 1.1, 1.05, 0.22],
    bipartite: [1.35, 1.2, 1.1, 0.34],
    chain: [1.1, 1.45, 1.18, 0.28],
    cluster: [1.45, 0.9, 1.05, 0.2],
    hierarchy: [0.95, 1.35, 1.15, 0.16],
  };
  const [modeRepulsion, modeSpring, modeDamping, modeCurvature] =
    modePhysics[layoutMode];

  return {
    layoutMode,
    intent,
    tokenCount,
    interrogativeDepth,
    repulsionMultiplier: clamp(modeRepulsion + lengthPressure * 0.25, 0.75, 1.8),
    springMultiplier: clamp(modeSpring + interrogativeDepth * 0.04, 0.7, 1.7),
    dampingMultiplier: clamp(modeDamping + lengthPressure * 0.15, 0.85, 1.6),
    edgeCurvature: clamp(modeCurvature + interrogativeDepth * 0.015, 0.1, 0.45),
  };
}
