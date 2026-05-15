/**
 * modelCapabilities — heuristic for which models expose a thinking /
 * reasoning-effort dial in their native API.
 *
 * Maintained as a pure frontend heuristic (no backend round-trip) because:
 *   - The patterns are deterministic and well-known
 *   - Round-tripping to fetch capability would gate every model-select
 *     dropdown render on a network call
 *   - Adding new models is a one-line change here
 *
 * Conservative bias: false positives (showing the selector for a model
 * that doesn't actually support thinking) are worse than false negatives
 * (hiding it for a model that does). The backend's thinking_mapper.py
 * is the source of truth — this heuristic just gates UI visibility.
 */

export type ThinkingEffort = "auto" | "none" | "low" | "medium" | "high";

export const THINKING_EFFORT_OPTIONS: ReadonlyArray<{
  value: ThinkingEffort;
  label: string;
  hint: string;
}> = [
  {
    value: "auto",
    label: "Auto",
    hint: "Let the provider pick a sensible default (usually medium).",
  },
  {
    value: "none",
    label: "None",
    hint: "Disable thinking — fastest, cheapest, no reasoning trace.",
  },
  {
    value: "low",
    label: "Low",
    hint: "Brief reasoning. Faster than medium, less depth.",
  },
  {
    value: "medium",
    label: "Medium",
    hint: "Default reasoning depth. Balanced for most queries.",
  },
  {
    value: "high",
    label: "High",
    hint: "Maximum reasoning budget. Slow, expensive, best on hard problems.",
  },
];

/**
 * Provider-pattern registry for thinking-capable models.
 *
 * Blank slate: no patterns are registered yet. Selector hides for ALL
 * models until concrete provider rules are added below — keeps the
 * UI honest about what's actually wired in the backend mapper.
 *
 * Workflow for adding a provider:
 *   1. Push an entry into THINKING_PATTERNS with a substring or
 *      regex test (lowercased model name).
 *   2. The backend's services/thinking_mapper.py must have a matching
 *      provider block. Without that, the selector would appear but
 *      the dial would be a no-op — exactly the false-positive case
 *      we want to avoid.
 *
 * Example (commented out until verified against current provider docs):
 *
 *     // OpenAI o-series — supports `reasoning_effort: low|medium|high`
 *     { test: (m) => /(^|\/)o[134](-|$)/.test(m), provider: "openai" },
 *
 *     // Anthropic Claude — supports `thinking: {budget_tokens: N}`
 *     { test: (m) => m.includes("claude"), provider: "anthropic" },
 */
type ThinkingPattern = {
  test: (lowercasedModel: string) => boolean;
  provider: string;
};

const THINKING_PATTERNS: ReadonlyArray<ThinkingPattern> = [
  // (none configured yet — selector will hide for every model)
];


/**
 * True if the model name matches any registered thinking-capable
 * pattern. Returns false (and the selector hides) when no patterns
 * are configured, which is the current blank-slate state.
 */
export function supportsThinking(modelName: string | null | undefined): boolean {
  if (!modelName) return false;
  const m = modelName.toLowerCase();
  return THINKING_PATTERNS.some((p) => p.test(m));
}


/**
 * Returns a human-readable explanation of WHY a model doesn't support
 * thinking. Currently a single generic string — extend with
 * provider-specific reasons as patterns are wired in.
 */
export function whyNoThinking(modelName: string | null | undefined): string {
  if (!modelName) return "Pick a model first.";
  return "This model doesn't expose a thinking dial in its API.";
}
