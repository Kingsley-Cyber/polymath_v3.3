// Sprint 3 — unified query_model_pool + models subdoc types.
// Mirrors backend models.schemas.QueryModelPoolEntry / HydeConfig /
// AgenticConfig / ModelsConfig. `api_key_ciphertext` is always masked
// as "[set]" on GET; "[set]" round-trips to "preserve existing" on PUT.

/** Provider id is now an open string so the registry can grow (google, xai,
 * zai, …) without a type-level churn. The runtime authority is the entry
 * in POOL_PROVIDER_PRESETS keyed by this id. */
export type PoolProvider = string;

export type PoolSource = "ollama" | "cloud";

export interface QueryModelPoolEntry {
  entry_id: string;
  label: string;
  provider: PoolProvider;
  base_url: string | null;
  /** "[set]" on GET when a key is stored; plaintext on POST; null for ollama. */
  api_key_ciphertext: string | null;
  model_name: string;
  source: PoolSource;
  enabled: boolean;
  created_at: string;
}

export interface HydeConfig {
  default_enabled: boolean;
  pool_entry_id: string | null;
}

export interface AgenticConfig {
  default_enabled: boolean;
  pool_entry_id: string | null;
}

// Phase 24 — Reasoning Cascade analyst model (separate from chat model).
export interface ReasoningConfig {
  default_enabled: boolean;
  pool_entry_id: string | null;
}

export interface ModelsConfig {
  query_model_pool: QueryModelPoolEntry[];
  hyde: HydeConfig;
  agentic: AgenticConfig;
  reasoning: ReasoningConfig;
}

/** Structured provider preset. Carries the LiteLLM prefix explicitly so the
 * UI can compose `{litellm_provider}/{example_model}` at select time — the
 * prefix is what LiteLLM's wildcard router matches on. `kwargs` merges into
 * the pool entry's extra_params for providers that need fixed headers
 * (OpenRouter) or body params.
 *
 * Note: `litellm_provider` may not equal `id`. Several OpenAI-compatible
 * providers (SiliconFlow, Z.AI, generic custom) ride the `openai/*` route
 * with a per-entry `api_base` override — their litellm_provider is "openai"
 * even though the UI id differs. */
export interface PoolProviderPreset {
  id: string;
  name: string;
  litellm_provider: string;
  base_url: string;
  example_model: string;
  kwargs?: Record<string, unknown>;
}

export const POOL_PROVIDER_PRESETS: PoolProviderPreset[] = [
  {
    id: "openai",
    name: "OpenAI",
    litellm_provider: "openai",
    base_url: "https://api.openai.com/v1",
    example_model: "gpt-4o",
  },
  {
    id: "anthropic",
    name: "Anthropic",
    litellm_provider: "anthropic",
    base_url: "https://api.anthropic.com/v1",
    example_model: "claude-sonnet-4-6",
  },
  {
    id: "deepseek",
    name: "DeepSeek",
    litellm_provider: "deepseek",
    base_url: "https://api.deepseek.com/v1",
    example_model: "deepseek-chat",
  },
  {
    id: "google",
    name: "Google (Gemini)",
    litellm_provider: "gemini",
    base_url: "https://generativelanguage.googleapis.com/v1beta",
    example_model: "gemini-2.0-flash",
  },
  {
    id: "mistral",
    name: "Mistral",
    litellm_provider: "mistral",
    base_url: "https://api.mistral.ai/v1",
    example_model: "mistral-large-latest",
  },
  {
    id: "groq",
    name: "Groq",
    litellm_provider: "groq",
    base_url: "https://api.groq.com/openai/v1",
    example_model: "llama-3.3-70b-versatile",
  },
  {
    id: "moonshot",
    name: "Moonshot",
    litellm_provider: "openai",
    base_url: "https://api.moonshot.ai/v1",
    example_model: "kimi-k2-0711-preview",
  },
  {
    id: "together",
    name: "Together",
    litellm_provider: "together_ai",
    base_url: "https://api.together.xyz/v1",
    example_model: "meta-llama/Llama-3.3-70B-Instruct-Turbo",
  },
  {
    id: "xai",
    name: "xAI",
    litellm_provider: "xai",
    base_url: "https://api.x.ai/v1",
    example_model: "grok-2-latest",
  },
  // OpenAI-compatible providers — litellm_provider="openai" but custom base_url.
  // Model name stored as `openai/<model>`; LiteLLM routes via the openai
  // provider using the per-entry api_base.
  {
    id: "siliconflow",
    name: "SiliconFlow",
    litellm_provider: "openai",
    base_url: "https://api.siliconflow.com/v1",
    example_model: "deepseek-ai/DeepSeek-V3.2",
  },
  {
    id: "zai",
    name: "Z.AI",
    litellm_provider: "openai",
    base_url: "https://api.z.ai/api/paas/v4",
    example_model: "glm-4-plus",
  },
  {
    id: "openrouter",
    name: "OpenRouter",
    litellm_provider: "openrouter",
    base_url: "https://openrouter.ai/api/v1",
    example_model: "anthropic/claude-sonnet-4.5",
    kwargs: {
      extra_headers: {
        "HTTP-Referer": "https://polymath.local",
        "X-Title": "Polymath RAG",
      },
    },
  },
  {
    id: "ollama",
    name: "Ollama (local)",
    litellm_provider: "ollama",
    base_url: "http://ollama:11434",
    example_model: "qwen2.5:1.5b-instruct",
  },
  {
    id: "vllm-local",
    name: "vLLM (local)",
    litellm_provider: "openai",
    base_url: "http://vllm-summary:8000/v1",
    example_model: "lfm2-rag",
  },
  {
    id: "custom",
    name: "Custom (OpenAI-compat)",
    litellm_provider: "openai",
    base_url: "",
    example_model: "",
  },
];

/** Legacy alias retained so existing ingestion UX code keeps importing
 * `PROVIDER_PRESETS` without a churn rename. Same shape. */
export const PROVIDER_PRESETS = POOL_PROVIDER_PRESETS;

/** Lookup helper — returns the preset for a given id, or undefined if the
 * id is unknown (e.g. user-typed custom entry). */
export function findPreset(id: string | null | undefined): PoolProviderPreset | undefined {
  if (!id) return undefined;
  return POOL_PROVIDER_PRESETS.find((p) => p.id === id);
}

/** Compose the LiteLLM model string for a preset. Returns the bare model
 * untouched when the preset is unknown or either value is empty. */
export function composeModelString(presetId: string | null | undefined, bareModel: string): string {
  const preset = findPreset(presetId);
  const model = bareModel.trim();
  if (!preset || !preset.litellm_provider || !model) return model;
  const prefix = `${preset.litellm_provider}/`;
  if (model.startsWith(prefix)) return model;
  return `${prefix}${model}`;
}

export interface OllamaInstalledModel {
  name: string;
  /** raw bytes — use size_human for display */
  size_bytes?: number;
  size_human?: string;
  modified_at?: string | null;
  digest?: string;
  details?: Record<string, unknown>;
}
