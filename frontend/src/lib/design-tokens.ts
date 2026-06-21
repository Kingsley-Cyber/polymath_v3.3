/**
 * Polymath design tokens — premium research-grade system.
 *
 * Color, spacing, type, motion, density and elevation all live here as
 * CSS custom properties (declared in `index.css`). The TS mirror below
 * is the single source of truth the JS/TS components read at runtime
 * (so we never drift between the CSS variables and the inline styles).
 *
 * Color philosophy: every color in the workspace *means* something.
 *   - Surfaces describe substrate depth, not decoration.
 *   - Role colors encode node meaning (anchor / hub / bridge / gap /
 *     evidence / cross-corpus / query_matched / frontier). One hue per
 *     role; used identically in the canvas, the chips, the badges, and
 *     the synthesis body.
 *   - Lane colors mark evidence provenance (corpus / graph / web).
 *   - State colors communicate status (success / warning / danger /
 *     info) and live above role and lane.
 */

export type NodeRole =
  | "anchor"
  | "hub"
  | "bridge"
  | "gap"
  | "evidence"
  | "cross_corpus"
  | "query_matched"
  | "frontier";

export type EvidenceLane = "corpus" | "graph" | "web";

export type Density = "compact" | "comfortable" | "spacious";

export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "info";

// ─── Color tokens ────────────────────────────────────────────────────────

export const colorTokens = {
  surface: {
    base: "#0a0d14",        // page substrate, deepest
    canvas: "#0c0e13",      // graph canvas
    panel: "#11141b",       // raised panels (sidebar, header)
    inset: "#161a23",       // cards within panels
    overlay: "#1c2230",     // floating chips, pills
  },
  ink: {
    primary: "#f1f5f9",
    secondary: "#cbd5e1",
    tertiary: "#94a3b8",
    muted: "#64748b",
    inverse: "#0a0d14",
  },
  border: {
    thin: "rgba(148, 163, 184, 0.12)",
    regular: "rgba(148, 163, 184, 0.18)",
    strong: "rgba(148, 163, 184, 0.28)",
    focus: "rgba(125, 211, 252, 0.85)",
  },
  accent: {
    // Single source of truth for the workspace accent — amber, tuned for
    // both light and dark substrates.
    main: "#fbbf24",
    soft: "rgba(251, 191, 36, 0.16)",
    strong: "#f59e0b",
    on: "#0a0d14",
  },
  // ─── Roles ────────────────────────────────────────────────────────────
  // ONE color per role. Used everywhere a node/relation of that role is
  // shown — canvas dot, chip, badge, synthesis pill, lane legend.
  role: {
    anchor:        "#fcd34d", // amber-300 — Books / domain supernodes
    hub:           "#7dd3fc", // sky-300 — high-degree nodes
    bridge:        "#c4b5fd", // violet-300 — cross-domain connectors
    gap:           "#fda4af", // rose-300 — candidate gaps / missing
    evidence:      "#6ee7b7", // emerald-300 — source-grounded evidence
    cross_corpus:  "#fbbf24", // amber-400 — multi-corpus provenance
    query_matched: "#a5b4fc", // indigo-300 — seeds from the query
    frontier:      "#cbd5e1", // slate-300 — peripheral / long-tail
  } satisfies Record<NodeRole, string>,
  // ─── Evidence lanes ──────────────────────────────────────────────────
  // Layered above role because a single node can be a "hub" (role) AND
  // live in "corpus" evidence (lane). Lane is provenance, role is shape.
  lane: {
    corpus: {
      solid: "#3b82f6",
      bg: "rgba(59, 130, 246, 0.10)",
      border: "rgba(96, 165, 250, 0.45)",
      text: "#93c5fd",
    },
    graph: {
      solid: "#a78bfa",
      bg: "rgba(167, 139, 250, 0.10)",
      border: "rgba(167, 139, 250, 0.45)",
      text: "#c4b5fd",
    },
    web: {
      solid: "#38bdf8",
      bg: "rgba(56, 189, 248, 0.10)",
      border: "rgba(56, 189, 248, 0.45)",
      text: "#7dd3fc",
    },
  },
  // ─── Status (success / warning / danger / info) ────────────────────
  status: {
    success: { solid: "#10b981", bg: "rgba(16, 185, 129, 0.10)", text: "#6ee7b7" },
    warning: { solid: "#f59e0b", bg: "rgba(245, 158, 11, 0.10)", text: "#fcd34d" },
    danger:  { solid: "#ef4444", bg: "rgba(239, 68, 68, 0.10)",  text: "#fca5a5" },
    info:    { solid: "#3b82f6", bg: "rgba(59, 130, 246, 0.10)", text: "#93c5fd" },
  },
  // Grid + vignette for the canvas substrate.
  substrate: {
    gridLine: "rgba(148, 163, 184, 0.04)",
    vignette: "radial-gradient(ellipse at center, transparent 0%, rgba(0,0,0,0.35) 100%)",
  },
  // ─── Legacy compat namespaces ──────────────────────────────────────
  // These stay so the existing draw loops in AtomicView, ConstellationCanvas,
  // BookDrillPanel, and GalaxyBackground keep compiling. They mirror the
  // role/lane/state tokens above; the new role colors are the source of
  // truth but legacy keys aren't removed so the diff stays focused on
  // the token system itself.
  entity: {
    Person:        "#7dd3fc",
    Organization:  "#a5b4fc",
    Method:        "#c4b5fd",
    Product:       "#4ade80",
    Concept:       "#f9a8d4",
    Document:      "#cbd5e1",
    Artifact:      "#5eead4",
    RobloxService: "#f87171",
    RobloxClass:   "#818cf8",
    RobloxNetworkPrimitive: "#fb923c",
    LuauDataType:  "#2dd4bf",
    Software:      "#93c5fd",
    Standard:      "#67e8f9",
    Rule:          "#a5f3fc",
    Law:           "#fda4af",
    Event:         "#fdba74",
    Location:      "#86efac",
    TimeReference: "#94a3b8",
    Book:          "#fcd34d",
    Domain:        "#fcd34d",
    Other:         "#cbd5e1",
  },
  relation: {
    supports: "#94a3b8",
    mentions: "#cbd5e1",
    bridges:  "#c4b5fd",
    gap:      "#fda4af",
  },
  atomic: {
    background: "#0c0e13",
    ring: "#d1d5db",
    nucleus: "#fcd34d",
    nucleusStroke: "#f59e0b",
    evidence: "#f8fafc",
    bridgeFill: "#f1f5f9",
    crossCorpusBridgeFill: "rgba(251, 191, 36, 0.10)",
    gapFill: "rgba(253, 164, 175, 0.10)",
    label: "#f1f5f9",
    tooltipBg: "#11141b",
  },
  state: {
    selected: "#f1f5f9",
    hovered: "#fcd34d",
    inactive: "#64748b",
    crossCorpus: "#fbbf24",
  },
} as const;

// ─── Spacing (4pt scale) ─────────────────────────────────────────────────

export const space = {
  0: "0",
  px: "1px",
  0.5: "2px",
  1: "4px",
  1.5: "6px",
  2: "8px",
  3: "12px",
  4: "16px",
  5: "20px",
  6: "24px",
  7: "28px",
  8: "32px",
  10: "40px",
  12: "48px",
  16: "64px",
  20: "80px",
} as const;

// ─── Type (modular scale, ratio ~1.125) ──────────────────────────────────

export const type = {
  family: {
    ui: '"Rubik", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    reading: '"Atkinson Hyperlegible", "Inter", "Segoe UI", system-ui, sans-serif',
    mono: '"Roboto Mono", Consolas, Monaco, monospace',
  },
  // Modular scale (1.125): 11 → 12 → 14 → 16 → 18 → 20 → 24 → 27 → 32.
  size: {
    xs: 11,
    sm: 12,
    base: 13,
    md: 14,
    lg: 16,
    xl: 20,
    "2xl": 24,
    "3xl": 32,
  },
  weight: {
    regular: 400,
    medium: 500,
    semibold: 600,
    bold: 700,
  },
  lineHeight: {
    tight: 1.2,
    snug: 1.35,
    normal: 1.5,
    relaxed: 1.65,
  },
  letterSpacing: {
    tight: "-0.01em",
    normal: "0",
    wide: "0.04em",
    wider: "0.10em",
    widest: "0.18em",
  },
} as const;

// ─── Motion ──────────────────────────────────────────────────────────────

export const motion = {
  duration: {
    instant: "60ms",
    fast: "120ms",
    base: "200ms",
    slow: "320ms",
    slower: "480ms",
  },
  ease: {
    standard: "cubic-bezier(0.2, 0, 0, 1)",
    emphasized: "cubic-bezier(0.2, 0, 0, 1.2)",
    decelerate: "cubic-bezier(0, 0, 0.2, 1)",
    accelerate: "cubic-bezier(0.4, 0, 1, 1)",
  },
  // Ambient graph FPS — matches the prior sigma constant. Drives the
  // BookGlow breathing animation cadence.
  ambientGraphFps: 18,
} as const;

// ─── Density / shape ─────────────────────────────────────────────────────

export const density = {
  radius: {
    sm: "4px",
    md: "6px",
    lg: "10px",
    xl: "14px",
    full: "9999px",
  },
  border: {
    thin: "1px",
    regular: "1px",
    strong: "2px",
  },
  // Focus ring is *always* visible on keyboard nav.
  focus: {
    width: "2px",
    offset: "2px",
    color: colorTokens.border.focus,
  },
  shadow: {
    // Premium redesign keeps shadows minimal — only used for floating
    // overlays (drill panel, synthesis card, tooltips).
    floating:
      "0 24px 60px -24px rgba(0, 0, 0, 0.65), 0 2px 8px rgba(0, 0, 0, 0.25)",
    inset:
      "inset 0 0 0 1px rgba(148, 163, 184, 0.06)",
  },
} as const;

// ─── Button hierarchy ────────────────────────────────────────────────────

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "tertiary"
  | "ghost"
  | "danger";

export type ButtonSize = "sm" | "md" | "lg" | "icon";

export const buttonTokens = {
  variant: {
    primary: {
      bg: colorTokens.accent.main,
      bgHover: colorTokens.accent.strong,
      bgActive: colorTokens.accent.strong,
      fg: colorTokens.accent.on,
      border: "transparent",
      borderHover: "transparent",
      focusRing: colorTokens.border.focus,
    },
    secondary: {
      bg: colorTokens.surface.inset,
      bgHover: colorTokens.surface.overlay,
      bgActive: colorTokens.surface.overlay,
      fg: colorTokens.ink.primary,
      border: colorTokens.border.regular,
      borderHover: colorTokens.border.strong,
      focusRing: colorTokens.border.focus,
    },
    tertiary: {
      bg: "transparent",
      bgHover: colorTokens.surface.inset,
      bgActive: colorTokens.surface.overlay,
      fg: colorTokens.ink.secondary,
      border: "transparent",
      borderHover: colorTokens.border.regular,
      focusRing: colorTokens.border.focus,
    },
    ghost: {
      bg: "transparent",
      bgHover: "rgba(148, 163, 184, 0.06)",
      bgActive: "rgba(148, 163, 184, 0.10)",
      fg: colorTokens.ink.tertiary,
      border: "transparent",
      borderHover: "transparent",
      focusRing: colorTokens.border.focus,
    },
    danger: {
      bg: colorTokens.status.danger.solid,
      bgHover: "#dc2626",
      bgActive: "#b91c1c",
      fg: "#fff5f5",
      border: "transparent",
      borderHover: "transparent",
      focusRing: "rgba(239, 68, 68, 0.6)",
    },
  },
  size: {
    sm: { height: "24px", padX: space[2], padY: space[1], fontSize: type.size.xs, fontWeight: type.weight.medium, radius: density.radius.sm },
    md: { height: "32px", padX: space[3], padY: space[2], fontSize: type.size.sm, fontWeight: type.weight.medium, radius: density.radius.md },
    lg: { height: "40px", padX: space[4], padY: space[2], fontSize: type.size.base, fontWeight: type.weight.semibold, radius: density.radius.md },
  },
} as const;

// ─── Helpers ─────────────────────────────────────────────────────────────

/** Role → hex. */
export function roleColor(role: NodeRole): string {
  return colorTokens.role[role];
}

/** Role → rgba with alpha, for soft fills. */
export function roleSoft(role: NodeRole, alpha = 0.12): string {
  const hex = colorTokens.role[role];
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!m) return hex;
  const r = parseInt(m[1], 16);
  const g = parseInt(m[2], 16);
  const b = parseInt(m[3], 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** Lane → solid hex. */
export function laneColor(lane: EvidenceLane): string {
  return colorTokens.lane[lane].solid;
}

/** Lane → soft bg fill. */
export function laneBg(lane: EvidenceLane): string {
  return colorTokens.lane[lane].bg;
}

/** Lane → border. */
export function laneBorder(lane: EvidenceLane): string {
  return colorTokens.lane[lane].border;
}

/** Lane → text. */
export function laneText(lane: EvidenceLane): string {
  return colorTokens.lane[lane].text;
}

/** Status → solid/bg/text. */
export function stateTokens(tone: Tone) {
  switch (tone) {
    case "success": return colorTokens.status.success;
    case "warning": return colorTokens.status.warning;
    case "danger":  return colorTokens.status.danger;
    case "info":    return colorTokens.status.info;
    default:        return null;
  }
}
