/**
 * Card + Chip — small primitives that consume the design-token CSS
 * variables defined in `index.css`. Replaces the ad-hoc Tailwind card
 * pattern inside the graph components.
 */

import type { CSSProperties, ReactNode } from "react";

export interface CardProps {
  children: ReactNode;
  accent?: boolean;
  inset?: boolean;
  className?: string;
  style?: CSSProperties;
}

export function Card({ children, accent, inset, className = "", style }: CardProps) {
  const cls = [
    "gcard",
    accent ? "gcard--accent" : "",
    inset ? "gcard--inset" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls} style={style}>
      {children}
    </div>
  );
}

export type ChipTone =
  | "neutral"
  | "accent"
  | "success"
  | "warning"
  | "danger"
  | "info";

export interface ChipProps {
  children: ReactNode;
  tone?: ChipTone;
  /** Foreground color for the dot. Defaults to the tone's solid. */
  dotColor?: string;
  /** When true the chip renders with a soft role-tinted bg. */
  withDot?: boolean;
  className?: string;
  style?: CSSProperties;
  title?: string;
}

const TONE_BG: Record<ChipTone, string> = {
  neutral: "var(--surface-inset)",
  accent: "var(--accent-soft)",
  success: "var(--state-success-bg)",
  warning: "var(--state-warning-bg)",
  danger: "var(--state-danger-bg)",
  info: "var(--state-info-bg)",
};

const TONE_FG: Record<ChipTone, string> = {
  neutral: "var(--ink-tertiary)",
  accent: "var(--accent-main)",
  success: "var(--state-success-text)",
  warning: "var(--state-warning-text)",
  danger: "var(--state-danger-text)",
  info: "var(--state-info-text)",
};

const TONE_BORDER: Record<ChipTone, string> = {
  neutral: "var(--border-thin)",
  accent: "var(--accent-main)",
  success: "var(--state-success-solid)",
  warning: "var(--state-warning-solid)",
  danger: "var(--state-danger-solid)",
  info: "var(--state-info-solid)",
};

export function Chip({
  children,
  tone = "neutral",
  dotColor,
  withDot = true,
  className = "",
  style,
  title,
}: ChipProps) {
  const composedStyle: CSSProperties = {
    background: TONE_BG[tone],
    color: TONE_FG[tone],
    borderColor: TONE_BORDER[tone],
    ...style,
  };
  return (
    <span className={`gchip ${className}`} style={composedStyle} title={title}>
      {withDot && (
        <span
          className="gchip__dot"
          style={{ background: dotColor ?? TONE_BORDER[tone] }}
        />
      )}
      {children}
    </span>
  );
}

export interface LaneChipProps {
  lane: "corpus" | "graph" | "web";
  label?: string;
  count?: number;
}

export function LaneChip({ lane, label, count }: LaneChipProps) {
  const vars: Record<typeof lane, { solid: string; bg: string; border: string; text: string }> = {
    corpus: {
      solid: "var(--lane-corpus-solid)",
      bg: "var(--lane-corpus-bg)",
      border: "var(--lane-corpus-border)",
      text: "var(--lane-corpus-text)",
    },
    graph: {
      solid: "var(--lane-graph-solid)",
      bg: "var(--lane-graph-bg)",
      border: "var(--lane-graph-border)",
      text: "var(--lane-graph-text)",
    },
    web: {
      solid: "var(--lane-web-solid)",
      bg: "var(--lane-web-bg)",
      border: "var(--lane-web-border)",
      text: "var(--lane-web-text)",
    },
  };
  const v = vars[lane];
  return (
    <Chip
      tone="neutral"
      dotColor={v.solid}
      style={{ background: v.bg, color: v.text, borderColor: v.border }}
      title={`${label ?? lane} evidence lane`}
    >
      {label ?? lane}
      {typeof count === "number" && count > 0 && (
        <span style={{ marginLeft: 2, opacity: 0.8 }}>×{count}</span>
      )}
    </Chip>
  );
}

export interface RoleChipProps {
  role:
    | "anchor"
    | "hub"
    | "bridge"
    | "gap"
    | "evidence"
    | "cross_corpus"
    | "query_matched"
    | "frontier";
  count?: number;
}

export function RoleChip({ role, count }: RoleChipProps) {
  const cssVar = `var(--role-${role.replace(/_/g, "-")})`;
  return (
    <Chip
      tone="neutral"
      dotColor={cssVar}
      style={{
        background: "transparent",
        color: cssVar,
        borderColor: cssVar,
      }}
      title={`Role: ${role.replace(/_/g, " ")}`}
    >
      {role.replace(/_/g, " ")}
      {typeof count === "number" && count > 0 && (
        <span style={{ marginLeft: 2, opacity: 0.8 }}>×{count}</span>
      )}
    </Chip>
  );
}