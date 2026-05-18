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
  // DeepSeek V4 Flash / Pro — supports `thinking: {type}` toggle +
  // `reasoning_effort` (low/medium → DeepSeek "high", high → "max",
  // none → disabled). Verified 2026-05-15.
  //
  // Negative on the older "reasoner" / "r1" / "chat" / "v3" models:
  // only V4 follows the toggle contract. Backend's _is_deepseek_v4
  // mirrors this gate.
  {
    test: (m) => /deepseek-v4(-flash|-pro)?\b/.test(m),
    provider: "deepseek",
  },

  // Mistral Magistral — Mistral's named reasoning family
  // (magistral-small, magistral-medium, future variants).
  // Binary dial: `reasoning_effort: "high" | "none"`. Other Mistral
  // models (mistral-small, mistral-large, codestral, open-mistral-7b)
  // are NOT reasoning models and stay hidden.
  {
    test: (m) => m.includes("magistral"),
    provider: "mistral",
  },

  // GLM (Z.AI) — pure binary toggle via `thinking: {type}`. No effort
  // gradient. Matches glm-5, glm-5.1, glm-5-turbo, glm-5v-turbo,
  // glm-4.5, glm-4.6, glm-4.7. Excludes glm-4, glm-4-plus, glm-3-*
  // (non-thinking chat models per the docs).
  {
    test: (m) => /glm-(5|4\.[5-7])/.test(m),
    provider: "zai",
  },
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


// ─── Vision capability heuristic (Phase 29) ──────────────────────────────
// Mirrors backend/services/vision_capabilities.py — when these change,
// update both. Used by ChatInput to show a warning when the user
// attaches an image but has a non-vision model selected; the backend
// pre-flight check is the source of truth (this is just a UX nicety
// to avoid a server round-trip for the error).

const VISION_PATTERNS: ReadonlyArray<RegExp> = [
  // OpenAI GPT-4o family + GPT-4 Turbo + o-series
  /(^|\/)gpt-4o(\b|-)/,
  /(^|\/)gpt-4-turbo/,
  /(^|\/)gpt-4-vision/,
  /(^|\/)o[134](-|\b)/,
  // Anthropic — every Claude 3+ supports vision
  /claude/,
  // Google Gemini 1.5+ / 2.x
  /gemini-(1\.5|2\.\d|2-)/,
  // GLM vision variants (v suffix)
  /glm-(4\.5v|5v)/,
  // Mistral Pixtral line
  /pixtral/,
  // Qwen-VL
  /qwen[\d.]*-vl/,
  // Llama vision (3.2-vision, 3.2-90b-vision-instruct, 4-maverick, 4-scout)
  /llama[\w.-]*(vision|maverick|scout)/,
];

/**
 * True if the model name matches any registered vision-capable
 * pattern. Returns false for unknown models — conservative bias.
 */
export function supportsVision(modelName: string | null | undefined): boolean {
  if (!modelName) return false;
  const m = modelName.toLowerCase().trim();
  // Defensive — bare pool/profile references aren't resolved here;
  // the ChatInput component resolves them via the pool store before
  // calling this function.
  if (m.startsWith("pool:") || m.startsWith("profile:")) return false;
  return VISION_PATTERNS.some((re) => re.test(m));
}

/**
 * User-facing hint for the error/warning when an image is attached
 * but the picked model has no vision support.
 */
export function visionCapableModelsHint(): string {
  return "Pick a vision model — GPT-4o, Claude 3.5/4, Gemini 1.5/2.x, GLM-5V, Pixtral, or Qwen-VL.";
}
