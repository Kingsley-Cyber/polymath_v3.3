/**
 * graph-colors — thin compatibility layer over `design-tokens.ts`.
 *
 * The new design system puts role / lane / state colors in
 * `lib/design-tokens.ts` so the React components read the same source
 * of truth as the CSS custom properties. This file keeps the existing
 * `graphColors.*` and `nodeFillColor` exports working for the few
 * call-sites that still reference them (mostly the adapter + canvas),
 * but the long-term home for color is `design-tokens.ts`.
 */

import {
  colorTokens,
  laneBg,
  laneBorder,
  laneColor,
  laneText,
  roleColor,
  roleSoft,
  type EvidenceLane,
  type NodeRole,
} from "./design-tokens";

export type { EvidenceLane, NodeRole };

export {
  colorTokens as graphColors,
  laneBg,
  laneBorder,
  laneColor,
  laneText,
  roleColor,
  roleSoft,
};

// Legacy adapter hooks used by the polymath-graph-adapter and atomic view.
export function corpusHue(corpusId: string): number {
  let h = 0;
  for (const ch of corpusId) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % 360;
}

export function corpusColor(corpusId: string): string {
  return `hsl(${corpusHue(corpusId)}, 60%, 52%)`;
}

function stableHash(value: string): number {
  let h = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function hslToHex(h: number, s: number, l: number): string {
  const hue = (((h % 360) + 360) % 360) / 60;
  const sat = Math.max(0, Math.min(100, s)) / 100;
  const light = Math.max(0, Math.min(100, l)) / 100;
  const c = (1 - Math.abs(2 * light - 1)) * sat;
  const x = c * (1 - Math.abs((hue % 2) - 1));
  let r = 0;
  let g = 0;
  let b = 0;

  if (hue < 1) {
    r = c;
    g = x;
  } else if (hue < 2) {
    r = x;
    g = c;
  } else if (hue < 3) {
    g = c;
    b = x;
  } else if (hue < 4) {
    g = x;
    b = c;
  } else if (hue < 5) {
    r = x;
    b = c;
  } else {
    r = c;
    b = x;
  }

  const m = light - c / 2;
  const toHex = (v: number) =>
    Math.round((v + m) * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

function hexToRgb(value: string): { r: number; g: number; b: number } | null {
  const text = value.trim();
  const short = /^#([a-f\d])([a-f\d])([a-f\d])$/i.exec(text);
  if (short) {
    return {
      r: parseInt(short[1] + short[1], 16),
      g: parseInt(short[2] + short[2], 16),
      b: parseInt(short[3] + short[3], 16),
    };
  }
  const full = /^#([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(text);
  if (full) {
    return {
      r: parseInt(full[1], 16),
      g: parseInt(full[2], 16),
      b: parseInt(full[3], 16),
    };
  }
  const rgb = /^rgba?\((\d+),\s*(\d+),\s*(\d+)/i.exec(text);
  if (rgb) {
    return {
      r: Math.max(0, Math.min(255, Number(rgb[1]))),
      g: Math.max(0, Math.min(255, Number(rgb[2]))),
      b: Math.max(0, Math.min(255, Number(rgb[3]))),
    };
  }
  return null;
}

function rgbToHue({ r, g, b }: { r: number; g: number; b: number }): number {
  const nr = r / 255;
  const ng = g / 255;
  const nb = b / 255;
  const max = Math.max(nr, ng, nb);
  const min = Math.min(nr, ng, nb);
  const delta = max - min;
  if (delta === 0) return 42;
  let hue = 0;
  if (max === nr) hue = ((ng - nb) / delta) % 6;
  else if (max === ng) hue = (nb - nr) / delta + 2;
  else hue = (nr - ng) / delta + 4;
  return (hue * 60 + 360) % 360;
}

function activeThemeHue(): number {
  const fallback = rgbToHue(hexToRgb(colorTokens.accent.main) || { r: 251, g: 191, b: 36 });
  if (typeof window === "undefined") return fallback;
  try {
    const styles = window.getComputedStyle(document.documentElement);
    const accent =
      styles.getPropertyValue("--accent-main") ||
      styles.getPropertyValue("--color-accent-main") ||
      styles.getPropertyValue("--accent-primary");
    const rgb = hexToRgb(accent);
    return rgb ? rgbToHue(rgb) : fallback;
  } catch {
    return fallback;
  }
}

function normalizeHue(hue: number): number {
  return ((hue % 360) + 360) % 360;
}

function documentNodeHue(): number {
  return normalizeHue(activeThemeHue() + 2);
}

export type GraphSpawnTone = "document" | "property" | "bridge" | "scaffold";

const HARMONY_OFFSETS: Record<GraphSpawnTone, number[]> = {
  // Near-accent analogous hues: document heads belong to the theme.
  document: [-6, 4, 12],
  // Split-complement cool hues: properties/entities separate cleanly from docs.
  property: [146, 174, 202, 226],
  // Strong but not loud: bridges sit between corpus accent and graph lane.
  bridge: [82, 126, 206],
  // Very dark version of the cool complement for tentacle texture.
  scaffold: [178, 206],
};

export function graphSpawnColor(tone: GraphSpawnTone, key: string): string {
  const offsets = HARMONY_OFFSETS[tone];
  const hash = stableHash(`${tone}:${key}`);
  const offset = offsets[hash % offsets.length];
  const jitter = ((hash >>> 8) % 13) - 6;
  const hue = activeThemeHue() + offset + jitter;

  if (tone === "document") {
    const light = 44 + ((hash >>> 16) % 5);
    return hslToHex(hue, 48, light);
  }
  if (tone === "property") {
    const light = 63 + ((hash >>> 16) % 8);
    return hslToHex(hue, 58, light);
  }
  if (tone === "bridge") {
    const light = 50 + ((hash >>> 16) % 8);
    return hslToHex(hue, 54, light);
  }
  return hslToHex(hue, 30, 15);
}

export type GraphGeneticCategory =
  | "Person"
  | "Organization"
  | "Location"
  | "Event"
  | "Document"
  | "Rule"
  | "Law"
  | "Product"
  | "Artifact"
  | "Method"
  | "Software"
  | "Standard"
  | "Concept"
  | "TimeReference"
  | "RobloxService"
  | "RobloxClass"
  | "RobloxNetworkPrimitive"
  | "LuauDataType"
  | "Other";

export type GraphGenomeInput = {
  category?: string | null;
  weight?: number | null;
};

export type GraphCategoryGenome = {
  hue: number;
  dominant: GraphGeneticCategory;
  diversity: number;
  weights: Partial<Record<GraphGeneticCategory, number>>;
  signature: string;
};

export const GRAPH_GENETIC_CATEGORIES: GraphGeneticCategory[] = [
  "Person",
  "Organization",
  "Location",
  "Event",
  "Document",
  "Rule",
  "Law",
  "Product",
  "Artifact",
  "Method",
  "Software",
  "Standard",
  "Concept",
  "TimeReference",
  "RobloxService",
  "RobloxClass",
  "RobloxNetworkPrimitive",
  "LuauDataType",
  "Other",
];

const CATEGORY_BASE_HUE: Record<GraphGeneticCategory, number> = {
  Person: 326,
  Organization: 218,
  Location: 142,
  Event: 26,
  Document: 212,
  Rule: 58,
  Law: 338,
  Product: 174,
  Artifact: 166,
  Method: 276,
  Software: 204,
  Standard: 188,
  Concept: 258,
  TimeReference: 236,
  RobloxService: 202,
  RobloxClass: 246,
  RobloxNetworkPrimitive: 18,
  LuauDataType: 176,
  Other: 214,
};

function normalizeGeneticCategory(value: string | null | undefined): GraphGeneticCategory {
  const text = String(value || "").trim();
  if ((GRAPH_GENETIC_CATEGORIES as string[]).includes(text)) {
    return text as GraphGeneticCategory;
  }
  const lowered = text.toLowerCase();
  if (lowered.includes("person") || lowered.includes("persona")) return "Person";
  if (lowered.includes("org") || lowered.includes("company")) return "Organization";
  if (lowered.includes("place") || lowered.includes("location")) return "Location";
  if (lowered.includes("event")) return "Event";
  if (lowered.includes("rule") || lowered.includes("constraint")) return "Rule";
  if (lowered.includes("law") || lowered.includes("policy")) return "Law";
  if (lowered.includes("product")) return "Product";
  if (lowered.includes("artifact") || lowered.includes("file")) return "Artifact";
  if (lowered.includes("method") || lowered.includes("process")) return "Method";
  if (lowered.includes("software") || lowered.includes("library")) return "Software";
  if (lowered.includes("standard") || lowered.includes("spec")) return "Standard";
  if (lowered.includes("time") || lowered.includes("date")) return "TimeReference";
  if (lowered.includes("document") || lowered.includes("book")) return "Document";
  if (lowered.includes("concept") || lowered.includes("topic")) return "Concept";
  return "Other";
}

function themedCategoryHue(category: GraphGeneticCategory): number {
  // Keep semantic hue identity stable, but nudge the whole wheel slightly
  // toward the active graph accent so the palette feels born from the theme.
  const theme = activeThemeHue();
  const themeNudge = (((theme - 42 + 540) % 360) - 180) * 0.12;
  return CATEGORY_BASE_HUE[category] + themeNudge;
}

function blendHues(a: number, b: number, t: number): number {
  const delta = ((((b - a) % 360) + 540) % 360) - 180;
  return (a + delta * Math.max(0, Math.min(1, t)) + 360) % 360;
}

export function graphCategoryGenome(
  inputs: GraphGenomeInput[],
  fallbackKey: string,
): GraphCategoryGenome {
  const weights: Partial<Record<GraphGeneticCategory, number>> = {};
  let total = 0;

  for (const input of inputs) {
    const category = normalizeGeneticCategory(input.category);
    const weight = Math.max(0.1, Number(input.weight || 1));
    weights[category] = (weights[category] || 0) + weight;
    total += weight;
  }

  if (total <= 0) {
    const category =
      GRAPH_GENETIC_CATEGORIES[
        stableHash(`genome:fallback:${fallbackKey}`) % (GRAPH_GENETIC_CATEGORIES.length - 1)
      ] || "Other";
    return {
      hue: themedCategoryHue(category),
      dominant: category,
      diversity: 0,
      weights: { [category]: 1 },
      signature: `${category}:1.00`,
    };
  }

  let x = 0;
  let y = 0;
  let dominant: GraphGeneticCategory = "Other";
  let dominantWeight = 0;
  const normalized: Partial<Record<GraphGeneticCategory, number>> = {};

  for (const [category, rawWeight] of Object.entries(weights)) {
    const typed = category as GraphGeneticCategory;
    const weight = rawWeight / total;
    normalized[typed] = weight;
    if (weight > dominantWeight) {
      dominant = typed;
      dominantWeight = weight;
    }
    const radians = (themedCategoryHue(typed) * Math.PI) / 180;
    x += Math.cos(radians) * weight;
    y += Math.sin(radians) * weight;
  }

  const hue = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  const diversity = Math.max(0, Math.min(1, 1 - dominantWeight));
  const signature = Object.entries(normalized)
    .sort((a, b) => (b[1] || 0) - (a[1] || 0) || a[0].localeCompare(b[0]))
    .map(([category, weight]) => `${category}:${(weight || 0).toFixed(2)}`)
    .join("|");

  return { hue, dominant, diversity, weights: normalized, signature };
}

export function graphGenomeDocumentColor(
  _genome: GraphCategoryGenome,
  _key: string,
): string {
  return graphDocumentNodeColor();
}

export function graphDocumentNodeColor(): string {
  return hslToHex(documentNodeHue(), 52, 49);
}

export function graphGenomePropertyColor(
  categoryValue: string | null | undefined,
  genome: GraphCategoryGenome,
  key: string,
): string {
  const category = normalizeGeneticCategory(categoryValue);
  const hash = stableHash(`property-genome:${key}:${category}:${genome.signature}`);
  const categoryHue = themedCategoryHue(category);
  const categoryWeight = genome.weights[category] || 0;
  const blend = 0.22 + Math.min(0.28, categoryWeight * 0.45);
  const hue = blendHues(categoryHue, genome.hue, blend) + ((hash >>> 10) % 11) - 5;
  const saturation = 58 + Math.min(14, genome.diversity * 16);
  const lightness = 61 + ((hash >>> 18) % 8);
  return hslToHex(hue, saturation, lightness);
}

/**
 * Backwards-compat helper. The new primary signal is *role*, so prefer
 * `roleColor(role)` directly. This helper returns the role color with
 * cross-corpus provenance as a strong amber override and falls back to
 * the legacy entity-type hue when role isn't known.
 */
export function nodeFillColor(
  entityType: string | undefined,
  sourceCorpora: string[] | undefined,
  roleHint?: NodeRole,
): string {
  if (sourceCorpora && sourceCorpora.length > 1) {
    return colorTokens.role.cross_corpus;
  }
  if (roleHint) return colorTokens.role[roleHint];
  const legacyEntity = (colorTokens.entity as Record<string, string>)[
    entityType ?? ""
  ];
  if (legacyEntity) return legacyEntity;
  if (sourceCorpora && sourceCorpora.length === 1) {
    return corpusColor(sourceCorpora[0]);
  }
  return colorTokens.role.frontier;
}
