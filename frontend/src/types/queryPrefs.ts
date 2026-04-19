// Phase F — User Query Preferences.
// Per-user mapping of the 3 query-time roles (HyDE / Agentic / default
// Query) to entries in the existing model_pool, plus per-user Ollama
// model exclusions. Chips themselves live in model_pool — this object
// stores ONLY pool entry_id references.

export interface QueryPrefs {
  user_id: string;
  hyde_pool_id: string | null;
  agentic_pool_id: string | null;
  query_pool_id: string | null;
  ollama_exclusions: string[];
  updated_at: string | null;
}

export interface QueryPrefsUpdate {
  hyde_pool_id?: string | null;
  agentic_pool_id?: string | null;
  query_pool_id?: string | null;
  ollama_exclusions?: string[];
}
