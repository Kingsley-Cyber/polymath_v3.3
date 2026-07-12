import { useMemo, useState } from "react";
import { Check, Cloud, KeyRound, Loader2, Server, Zap } from "lucide-react";
import type { ModelProfileRef } from "../../types";
import * as api from "../../lib/api";

type IngestionRole = "summary" | "extraction" | "embedding";

interface Props {
  title: string;
  subtitle: string;
  role: IngestionRole;
  profiles: ModelProfileRef[];
  value: ModelProfileRef[];
  onChange: (next: ModelProfileRef[]) => void;
  editing: boolean;
}

function profileRuntime(profile: ModelProfileRef): string {
  if (profile.runtime) return profile.runtime;
  const provider = (profile.provider_preset || "").toLowerCase();
  const base = (profile.base_url || "").toLowerCase();
  const model = (profile.model || "").toLowerCase();
  if (
    provider.includes("vllm") ||
    base.includes("host.docker.internal") ||
    base.includes("192.168.") ||
    model.includes("polymath-extract")
  ) {
    return "rtx";
  }
  return base.startsWith("http") ? "cloud" : "custom";
}

function selectorRef(profile: ModelProfileRef): ModelProfileRef {
  return {
    ...profile,
    // Corpus payloads are secret-free. The backend materializes these fields
    // from profile_id before persisting the worker-facing snapshot.
    api_key: null,
    lifecycle_api_key: null,
    extra_params: { ...(profile.extra_params || {}) },
  };
}

export function IngestionProviderSelector({
  title,
  subtitle,
  role,
  profiles,
  value,
  onChange,
  editing,
}: Props) {
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testMessage, setTestMessage] = useState<{
    id: string;
    ok: boolean;
    text: string;
  } | null>(null);

  const available = useMemo(
    () =>
      profiles.filter(
        (profile) =>
          profile.enabled !== false &&
          Boolean(profile.profile_id) &&
          (!profile.capabilities?.length || profile.capabilities.includes(role)),
      ),
    [profiles, role],
  );
  const selectedIds = new Set(value.map((entry) => entry.profile_id).filter(Boolean));
  const legacy = value.filter((entry) => !entry.profile_id);

  const toggle = (profile: ModelProfileRef) => {
    if (!editing || !profile.profile_id) return;
    if (selectedIds.has(profile.profile_id)) {
      onChange(value.filter((entry) => entry.profile_id !== profile.profile_id));
    } else {
      onChange([...value, selectorRef(profile)]);
    }
  };

  const setConcurrency = (profileId: string, concurrency: number) => {
    onChange(
      value.map((entry) =>
        entry.profile_id === profileId
          ? { ...entry, max_concurrent: Math.max(1, Math.min(256, concurrency || 1)) }
          : entry,
      ),
    );
  };

  const test = async (profile: ModelProfileRef) => {
    if (!profile.profile_id) return;
    setTestingId(profile.profile_id);
    setTestMessage(null);
    try {
      const result = await api.testIngestionModelRef({
        kind: role === "embedding" ? "embedding" : "chat",
        entry: selectorRef(profile),
      });
      setTestMessage({
        id: profile.profile_id,
        ok: result.ok,
        text: result.ok
          ? `Connected${result.latency_ms ? ` in ${result.latency_ms}ms` : ""}`
          : result.error || "Connection failed",
      });
    } catch (error) {
      setTestMessage({
        id: profile.profile_id,
        ok: false,
        text: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setTestingId(null);
    }
  };

  return (
    <div className="border border-border-minimal bg-bg-base px-3 py-3 space-y-2">
      <div>
        <div className="text-[11px] font-bold tracking-widest uppercase text-content-primary">
          {title}
        </div>
        <div className="mt-0.5 text-[10px] leading-snug text-content-tertiary">
          {subtitle}
        </div>
      </div>

      {available.length === 0 ? (
        <div className="border border-amber-400/25 bg-amber-400/5 px-2 py-2 text-[10px] leading-snug text-amber-200">
          No saved {role} routes. Add and save one under Settings → Ingestion →
          Provider Registry, then reopen this corpus.
        </div>
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-1.5">
          {available.map((profile) => {
            const id = profile.profile_id as string;
            const selected = selectedIds.has(id);
            const selectedEntry = value.find((entry) => entry.profile_id === id);
            const runtime = profileRuntime(profile);
            const RuntimeIcon = runtime === "rtx" ? Server : Cloud;
            return (
              <div
                key={id}
                className={`border px-2.5 py-2 ${
                  selected
                    ? "border-accent-main/70 bg-accent-main/5"
                    : "border-border-minimal bg-bg-surface"
                }`}
              >
                <div className="flex items-start gap-2">
                  <button
                    type="button"
                    data-testid={`ingestion-provider-${role}-${id}`}
                    onClick={() => toggle(profile)}
                    disabled={!editing}
                    aria-pressed={selected}
                    className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center border ${
                      selected
                        ? "border-accent-main bg-accent-main text-bg-base"
                        : "border-content-tertiary text-transparent"
                    } disabled:opacity-50`}
                    title={selected ? "Remove route" : "Use route"}
                  >
                    <Check className="h-3 w-3" />
                  </button>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="truncate text-[11px] font-bold text-content-primary">
                        {profile.profile_label || profile.model}
                      </span>
                      <span className="inline-flex items-center gap-1 border border-border-minimal px-1 py-0.5 text-[8px] font-bold uppercase tracking-widest text-content-tertiary">
                        <RuntimeIcon className="h-2.5 w-2.5" />
                        {runtime}
                      </span>
                      <span
                        className="inline-flex items-center gap-1 text-[8px] uppercase tracking-widest text-content-tertiary"
                        title="Credential is stored in Settings and never copied into this form"
                      >
                        <KeyRound className="h-2.5 w-2.5" /> saved
                      </span>
                    </div>
                    <div className="mt-0.5 truncate font-mono text-[9px] text-content-secondary">
                      {profile.provider_preset || "custom"} · {profile.model}
                    </div>
                    {profile.base_url && (
                      <div className="truncate font-mono text-[8px] text-content-tertiary">
                        {profile.base_url}
                      </div>
                    )}
                  </div>
                </div>
                <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-border-minimal pt-1.5">
                  {selected ? (
                    <label className="flex items-center gap-1.5 text-[9px] uppercase tracking-widest text-content-tertiary">
                      concurrency
                      <input
                        type="number"
                        min={1}
                        max={256}
                        value={selectedEntry?.max_concurrent ?? profile.max_concurrent ?? 1}
                        onChange={(event) => setConcurrency(id, Number(event.target.value))}
                        className="w-14 border border-border-minimal bg-bg-base px-1 py-0.5 text-center font-mono text-[10px] text-content-primary"
                      />
                    </label>
                  ) : (
                    <span className="text-[9px] text-content-tertiary">
                      default concurrency {profile.max_concurrent || 1}
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => void test(profile)}
                    disabled={testingId !== null}
                    className="inline-flex items-center gap-1 border border-border-minimal px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-widest text-content-secondary hover:border-accent-main hover:text-accent-main disabled:opacity-40"
                    title="Test this saved ingestion route"
                  >
                    {testingId === id ? (
                      <Loader2 className="h-2.5 w-2.5 animate-spin" />
                    ) : (
                      <Zap className="h-2.5 w-2.5" />
                    )}
                    Test
                  </button>
                </div>
                {testMessage?.id === id && (
                  <div
                    className={`mt-1 text-[9px] ${
                      testMessage.ok ? "text-emerald-300" : "text-error"
                    }`}
                  >
                    {testMessage.text}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {legacy.length > 0 && (
        <div className="border border-amber-400/25 bg-amber-400/5 px-2 py-1.5 text-[9px] text-amber-200">
          {legacy.length} legacy inline route{legacy.length === 1 ? "" : "s"} remain
          active for compatibility. Select a saved profile above and remove the old
          route when convenient.
        </div>
      )}
    </div>
  );
}
