export type QueryLayoutMode =
  | "force"
  | "radial"
  | "bipartite"
  | "chain"
  | "cluster"
  | "hierarchy"
  | "venn_molecule"
  | "topological_tree"
  | "scatter_correlation"
  | "sociogram"
  | "mindmap";

export type QueryLayoutModifier =
  | "venn"
  | "causal"
  | "sociogram"
  | "scatter"
  | "tree"
  | "mindmap";

export interface QueryFingerprint {
  layoutMode: QueryLayoutMode;
  primaryLayout: QueryLayoutMode;
  modifiers: QueryLayoutModifier[];
  blend: {
    venn: number;
    causal: number;
    sociogram: number;
    scatter: number;
    tree: number;
    mindmap: number;
    molecule: number;
  };
  intent:
    | "mechanism"
    | "causal"
    | "comparison"
    | "path"
    | "enumeration"
    | "containment"
    | "taxonomy"
    | "correlation"
    | "social"
    | "ideation"
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

function uniqueModifiers(values: QueryLayoutModifier[]): QueryLayoutModifier[] {
  return [...new Set(values)];
}

export function fingerprintGraphQuery(query: string): QueryFingerprint {
  const normalized = query.trim().toLowerCase();
  const tokens = normalized.match(WORD_RE) ?? [];
  const tokenCount = tokens.length;
  const has = (pattern: RegExp) => pattern.test(normalized);

  const comparison = has(
    /\b(compare|contrast|versus|vs\.?|difference|differences|similar|similarity|similarities|shared|overlap)\b/,
  );
  const path = has(
    /\b(trace|path|route|from\b.+\bto|through|evolution|progression|sequence|journey)\b/,
  );
  const causal = has(
    /\b(why|cause|causes|causal|because|lead(?:s)? to|depends on|effect|effects|consequence|consequences|what happens if|if)\b/,
  );
  const mechanism = has(
    /\b(how|mechanism|process|workflow|pipeline|works?|operate|function|functions)\b/,
  );
  const tree = has(
    /\b(taxonomy|topology|topological|hierarchy|hierarchical|layers?|levels?|architecture|structure|where does .+ fit|fit into|depends on what|what depends on)\b/,
  );
  const containment = has(
    /\b(what contains|contains|part of|components?|subsystems?|inside|made of|composed of)\b/,
  );
  const correlation = has(
    /\b(correlation|correlate|relationship between|relation between|tradeoff|trade-off|tension|spectrum|axis|increase|decrease|predict|associated with)\b/,
  );
  const social = has(
    /\b(power|influence|authority|legitimacy|institution|institutions|governance|control|trust|social|society|status|dominance|politic(?:s|al)?)\b/,
  );
  const ideation = has(
    /\b(ideas?|ideate|brainstorm|design|build|prototype|possibilities|opportunities|invent|create|strategy|strategies)\b/,
  );
  const enumeration = has(/\b(list|all|enumerate|catalog|map out|show me)\b/);

  let layoutMode: QueryLayoutMode = "force";
  let intent: QueryFingerprint["intent"] = "general";

  if (comparison) {
    layoutMode = "venn_molecule";
    intent = "comparison";
  } else if (tree || containment) {
    layoutMode = "topological_tree";
    intent = tree ? "taxonomy" : "containment";
  } else if (path) {
    layoutMode = "chain";
    intent = "path";
  } else if (causal) {
    layoutMode = "chain";
    intent = "causal";
  } else if (correlation) {
    layoutMode = "scatter_correlation";
    intent = "correlation";
  } else if (mechanism) {
    layoutMode = "radial";
    intent = "mechanism";
  } else if (social) {
    layoutMode = "sociogram";
    intent = "social";
  } else if (ideation) {
    layoutMode = "mindmap";
    intent = "ideation";
  } else if (enumeration) {
    layoutMode = "cluster";
    intent = "enumeration";
  }

  const modifiers = uniqueModifiers([
    ...(comparison && layoutMode !== "venn_molecule" ? ["venn" as const] : []),
    ...(causal && layoutMode !== "chain" ? ["causal" as const] : []),
    ...(social && layoutMode !== "sociogram" ? ["sociogram" as const] : []),
    ...(correlation && layoutMode !== "scatter_correlation" ? ["scatter" as const] : []),
    ...((tree || containment) && layoutMode !== "topological_tree" ? ["tree" as const] : []),
    ...(ideation && layoutMode !== "mindmap" ? ["mindmap" as const] : []),
  ]);

  const questionMarks = (query.match(/\?/g) ?? []).length;
  const interrogatives = tokens.filter((t) =>
    [
      "how",
      "why",
      "what",
      "where",
      "when",
      "which",
      "compare",
      "trace",
      "if",
    ].includes(t),
  ).length;
  const interrogativeDepth = clamp(questionMarks + interrogatives, 0, 5);
  const lengthPressure = clamp((tokenCount - 8) / 28, 0, 1);

  const primaryBlend = {
    venn: layoutMode === "venn_molecule" ? 1 : 0,
    causal: layoutMode === "chain" && causal ? 1 : 0,
    sociogram: layoutMode === "sociogram" ? 1 : 0,
    scatter: layoutMode === "scatter_correlation" ? 1 : 0,
    tree: layoutMode === "topological_tree" ? 1 : 0,
    mindmap: layoutMode === "mindmap" ? 1 : 0,
    molecule:
      layoutMode === "force" ||
      layoutMode === "radial" ||
      layoutMode === "venn_molecule" ||
      layoutMode === "sociogram"
        ? 1
        : 0.55,
  };
  const blend = {
    venn: Math.max(primaryBlend.venn, modifiers.includes("venn") ? 0.5 : 0),
    causal: Math.max(primaryBlend.causal, modifiers.includes("causal") ? 0.45 : 0),
    sociogram: Math.max(
      primaryBlend.sociogram,
      modifiers.includes("sociogram") ? 0.5 : 0,
    ),
    scatter: Math.max(primaryBlend.scatter, modifiers.includes("scatter") ? 0.48 : 0),
    tree: Math.max(primaryBlend.tree, modifiers.includes("tree") ? 0.46 : 0),
    mindmap: Math.max(primaryBlend.mindmap, modifiers.includes("mindmap") ? 0.45 : 0),
    molecule: primaryBlend.molecule,
  };

  const modePhysics: Record<QueryLayoutMode, [number, number, number, number]> = {
    force: [1.0, 1.0, 1.0, 0.18],
    radial: [1.15, 1.1, 1.05, 0.22],
    bipartite: [1.35, 1.2, 1.1, 0.34],
    chain: [1.1, 1.45, 1.18, 0.28],
    cluster: [1.45, 0.9, 1.05, 0.2],
    hierarchy: [0.95, 1.35, 1.15, 0.16],
    venn_molecule: [1.45, 1.08, 1.08, 0.1],
    topological_tree: [1.08, 1.35, 1.16, 0.08],
    scatter_correlation: [1.6, 0.82, 1.08, 0.06],
    sociogram: [1.28, 1.22, 1.12, 0.16],
    mindmap: [1.5, 0.94, 1.05, 0.18],
  };
  const [modeRepulsion, modeSpring, modeDamping, modeCurvature] =
    modePhysics[layoutMode];

  const modifierRepulsion =
    blend.scatter * 0.16 + blend.mindmap * 0.12 + blend.venn * 0.08;
  const modifierSpring =
    blend.causal * 0.13 + blend.tree * 0.1 + blend.sociogram * 0.06;
  const modifierCurvature =
    blend.venn * 0.02 + blend.sociogram * 0.04 - blend.scatter * 0.03;

  return {
    layoutMode,
    primaryLayout: layoutMode,
    modifiers,
    blend,
    intent,
    tokenCount,
    interrogativeDepth,
    repulsionMultiplier: clamp(
      modeRepulsion + modifierRepulsion + lengthPressure * 0.25,
      0.75,
      1.95,
    ),
    springMultiplier: clamp(
      modeSpring + modifierSpring + interrogativeDepth * 0.04,
      0.7,
      1.85,
    ),
    dampingMultiplier: clamp(modeDamping + lengthPressure * 0.15, 0.85, 1.7),
    edgeCurvature: clamp(
      modeCurvature + modifierCurvature + interrogativeDepth * 0.015,
      0.04,
      0.45,
    ),
  };
}
