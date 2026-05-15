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
 * True if the model name matches a known thinking-capable pattern.
 *
 * Strips any `pool:`/`profile:` prefix and the provider/ slash so a pool
 * reference like `pool:abc123` (caller should resolve first) or a raw
 * LiteLLM id like `openai/o3-mini` both work.
 *
 * Patterns:
 *   - OpenAI o-series: o1*, o3*, o4* (anywhere in the id after the slash)
 *   - Anthropic Claude: anything containing "claude"
 *   - Gemini 2.5+: anything containing "gemini-2.5" (older 1.x has no dial)
 *   - DeepSeek reasoner: "deepseek-reasoner" (R1 family)
 */
export function supportsThinking(modelName: string | null | undefined): boolean {
  if (!modelName) return false;
  const m = modelName.toLowerCase();

  // OpenAI o-series — match both "o3-mini" (raw) and "openai/o3-mini" (LiteLLM).
  // Use word-boundary check to avoid matching unrelated substrings like "cargo".
  if (/(^|[/\s-])o[134](-|$)/.test(m)) return true;

  // Anthropic Claude — every modern Claude (3.7 Sonnet+, 4 Opus/Sonnet) has
  // thinking support; the mapper handles the budget_tokens encoding.
  if (m.includes("claude")) return true;

  // Gemini 2.5+ (older 1.5/1.0 have no thinking dial).
  if (m.includes("gemini-2.5")) return true;

  // DeepSeek R1 / reasoner.
  if (m.includes("deepseek-reasoner")) return true;
  if (m.includes("deepseek-r1")) return true;

  return false;
}

/**
 * Returns a human-readable explanation of WHY a model doesn't support
 * thinking — used for the disabled-state tooltip. Keep these short.
 */
export function whyNoThinking(modelName: string | null | undefined): string {
  if (!modelName) return "Pick a model first.";
  const m = modelName.toLowerCase();
  if (m.includes("gpt-4o") || m.includes("gpt-4-")) {
    return "GPT-4 models don't expose a thinking dial — only the o-series does.";
  }
  if (m.includes("gemini-1") || m.includes("gemini-2.0")) {
    return "Pre-2.5 Gemini doesn't expose a thinking dial.";
  }
  if (m.includes("deepseek") && !supportsThinking(modelName)) {
    return "Only deepseek-reasoner / R1 expose thinking. DeepSeek-Chat is non-reasoning.";
  }
  return "This model doesn't expose a thinking dial.";
}
