// Central type exports for Polymath RAG v3.3

export * from "./auth";
export * from "./chat";
export * from "./settings";
export * from "./tools";
export * from "./skills";
export * from "./corpus";
export * from "./extraction";
// Sprint 4C — legacy types/modelProfiles + types/modelPool removed.
// Unified replacement lives in ./queryModelPool (Sprint 3).
export * from "./queryPrefs";
export * from "./queryModelPool";
export * from "./discover";

// API compatibility aliases restored after Mission Control refactor.
export type ModelProfile = import("./queryModelPool").QueryModelPoolEntry;
export type ModelProfileCreate = Partial<import("./queryModelPool").QueryModelPoolEntry> & Record<string, unknown>;
export type ModelProfileUpdate = Partial<import("./queryModelPool").QueryModelPoolEntry> & Record<string, unknown>;
export interface ModelProfilesListResponse { profiles?: ModelProfile[]; entries?: ModelProfile[]; }
export type ModelProfileTestResult = Record<string, unknown>;
export type ModelPoolEntry = import("./queryModelPool").QueryModelPoolEntry;
export type ModelPoolEntryCreate = Partial<import("./queryModelPool").QueryModelPoolEntry> & Record<string, unknown>;
export type ModelPoolEntryUpdate = Partial<import("./queryModelPool").QueryModelPoolEntry> & Record<string, unknown>;
export interface ModelPoolListResponse { entries: ModelPoolEntry[]; }
export interface ModelPoolTestResult {
  ok: boolean;
  status?: number | null;
  latency_ms?: number | null;
  error?: string | null;
}
