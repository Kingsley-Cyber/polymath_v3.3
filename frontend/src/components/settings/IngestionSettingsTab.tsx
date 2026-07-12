// IngestionSettingsTab.tsx — Global ingestion settings.
// Extraction Engines and Summary Defaults are mutable. The remaining cards are
// read-only structural defaults that pre-fill corpus creation.

import { Layers, Info, Copy, Check, Cpu, Plus, Trash2, Cloud, Zap, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "../ui/Button";
import type { IngestionConfig, ModelProfileRef, TokenBudget } from "../../types";
import { DEFAULT_INGESTION_CONFIG } from "../../types";
import type {
  ExtractionEndpoint,
  GlobalIngestionSettings,
  ExtractionValidationReport,
  RunpodFlashExtractionSettings,
  RunpodFlashTestResult,
} from "../../types/settings";
import {
  getGlobalSettings,
  updateGlobalSettings,
  validateExtraction,
  testRunpodFlashExtraction,
} from "../../lib/api";
import { IngestionModelPool } from "./IngestionModelPool";

// ── Helpers ──────────────────────────────────────────────────────────────

function formatTokenBudget(budget: TokenBudget): string {
  return `${budget.min_tokens} / ${budget.target_tokens} / ${budget.max_tokens}`;
}

function copyToClipboard(text: string): Promise<void> {
  return navigator.clipboard.writeText(text);
}

// ── Read-only field row ──────────────────────────────────────────────────

function ReadOnlyField({
  label,
  value,
  hint,
  mono = false,
}: {
  label: string;
  value: string | number | boolean;
  hint?: string;
  mono?: boolean;
}) {
  const [copied, setCopied] = useState(false);

  const displayValue =
    typeof value === "boolean" ? (value ? "ON" : "OFF") : String(value);

  const handleCopy = async () => {
    await copyToClipboard(displayValue);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="flex items-start justify-between gap-3 py-2.5 border-b border-white/5 last:border-b-0">
      <div className="flex-1 min-w-0">
        <div className="text-[12px] text-gray-400">{label}</div>
        {hint && <div className="text-[10px] text-gray-600 mt-0.5">{hint}</div>}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <span
          className={`text-[13px] ${
            typeof value === "boolean"
              ? value
                ? "text-green-400"
                : "text-gray-500"
              : "text-white"
          } ${mono ? "font-mono" : ""}`}
        >
          {displayValue}
        </span>
        <button
          onClick={handleCopy}
          className="p-0.5 text-gray-600 hover:text-gray-300 transition-colors"
          title="Copy value"
        >
          {copied ? (
            <Check className="w-3 h-3 text-green-400" />
          ) : (
            <Copy className="w-3 h-3" />
          )}
        </button>
      </div>
    </div>
  );
}

// ── Section card ─────────────────────────────────────────────────────────

function SectionCard({
  title,
  icon: Icon,
  iconColor,
  children,
}: {
  title: string;
  icon: typeof Layers;
  iconColor: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
      <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
        <Icon size={16} className={iconColor} /> {title}
      </h3>
      <div className="space-y-0">{children}</div>
    </div>
  );
}

// ── Extraction Engines (mutable) ─────────────────────────────────────────
// Toggleable sidecar endpoints. The ingestion worker health-probes ENABLED
// endpoints per document (top-to-bottom preference) and dispatches to the
// live ones — turn a GPU box off and work flows to the next enabled engine.

function ExtractionEnginesCard() {
  const [endpoints, setEndpoints] = useState<ExtractionEndpoint[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [engine, setEngine] = useState<
    | "local"
    | "cloud"
    | "runpod_flash"
    | "legacy_local"
    | "local_then_cloud"
    | "local_then_enrich"
    | "dual"
    | "off"
  >("local");
  const [validating, setValidating] = useState(false);
  const [report, setReport] = useState<ExtractionValidationReport | null>(null);

  useEffect(() => {
    getGlobalSettings()
      .then((r) => {
        setEndpoints(r.settings.extraction?.endpoints ?? []);
        setEngine(r.settings.extraction?.engine ?? "local");
      })
      .catch((e) => setError(String(e)));
  }, []);

  const mutate = (next: ExtractionEndpoint[]) => {
    setEndpoints(next);
    setDirty(true);
    setReport(null); // edits invalidate the last validation
  };

  const save = async () => {
    if (!endpoints) return;
    setSaving(true);
    setError(null);
    try {
      await updateGlobalSettings({ extraction: { endpoints, engine } });
      setDirty(false);
      setReport(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  // Probes run from the BACKEND's network position (what the worker actually
  // uses) against the SAVED config — hence disabled while there are unsaved
  // edits.
  const validate = async () => {
    setValidating(true);
    setError(null);
    try {
      setReport(await validateExtraction());
    } catch (e) {
      setError(String(e));
    } finally {
      setValidating(false);
    }
  };

  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
          <Cpu size={16} className="text-emerald-400" /> Extraction Engines
        </h3>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            onClick={validate}
            disabled={dirty || validating || endpoints === null}
            title={
              dirty
                ? "Save changes first — validation probes the saved config"
                : "Probe every engine from the backend (reachable, healthy, GPU active)"
            }
          >
            {validating ? "Validating…" : "Validate"}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={save}
            disabled={!dirty || saving}
          >
            {saving ? "Saving…" : dirty ? "Save" : "Saved"}
          </Button>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-[10px] tracking-widest text-gray-500 uppercase">Engine</span>
        {([
          ["local", "LOCAL PRIVATE LLM"],
          ["cloud", "CLOUD/API LLM"],
          ["runpod_flash", "RUNPOD FLASH"],
          ["legacy_local", "LEGACY SIDECAR"],
          ["local_then_cloud", "LEGACY → PROVIDER"],
          ["local_then_enrich", "LEGACY + ENRICH"],
          ["dual", "DUAL (LEGACY + PROVIDER)"],
          ["off", "OFF"],
        ] as const).map(([val, label]) => (
          <button
            key={val}
            type="button"
            onClick={() => { setEngine(val); setDirty(true); }}
            className={
              "px-2 py-1 text-[10px] tracking-wider border " +
              (engine === val
                ? "border-accent-main text-content-primary bg-bg-base"
                : "border-border-minimal text-content-tertiary hover:text-content-primary")
            }
          >
            {label}
          </button>
        ))}
      </div>
      <p className="text-[12px] text-gray-500">
        Machines that run entity/relation extraction during ingestion. The
        worker checks which enabled engines are online for each document and
        uses them in this order — a powered-off GPU box is skipped
        automatically, so the local engine quietly handles small batches.
      </p>
      {error && <p className="text-[12px] text-red-400">{error}</p>}
      {report && (
        <p
          className={`text-[12px] rounded px-2 py-1.5 ${
            report.deploy_ready
              ? "text-emerald-400 bg-emerald-500/10"
              : "text-red-400 bg-red-500/10"
          }`}
        >
          {report.deploy_ready
            ? `Deploy ready — ${report.enabled_ready}/${report.enabled_total} enabled engine${
                report.enabled_total === 1 ? "" : "s"
              } fully validated from the backend.`
            : "Not deploy ready — no enabled engine passed validation. Enable a healthy engine or fix the flagged ones before ingesting."}
        </p>
      )}
      {endpoints === null ? (
        <p className="text-[12px] text-gray-600">Loading…</p>
      ) : (
        <div className="space-y-2">
          {endpoints.map((ep, i) => (
            <div
              key={i}
              className="flex items-center gap-2 bg-black/20 rounded px-3 py-2"
            >
              <button
                onClick={() =>
                  mutate(
                    endpoints.map((e, j) =>
                      j === i ? { ...e, enabled: !e.enabled } : e,
                    ),
                  )
                }
                title={ep.enabled ? "Enabled — click to disable" : "Disabled — click to enable"}
                className={`w-9 h-5 rounded-full relative transition-colors shrink-0 ${
                  ep.enabled ? "bg-emerald-600" : "bg-white/10"
                }`}
              >
                <span
                  className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all ${
                    ep.enabled ? "left-[18px]" : "left-0.5"
                  }`}
                />
              </button>
              <input
                value={ep.label}
                placeholder="Label"
                onChange={(ev) =>
                  mutate(
                    endpoints.map((e, j) =>
                      j === i ? { ...e, label: ev.target.value } : e,
                    ),
                  )
                }
                className="w-36 bg-transparent border border-white/10 rounded px-2 py-1 text-[12px] text-white"
              />
              <input
                value={ep.url}
                placeholder="http://192.168.x.x:8084"
                onChange={(ev) =>
                  mutate(
                    endpoints.map((e, j) =>
                      j === i ? { ...e, url: ev.target.value } : e,
                    ),
                  )
                }
                className="flex-1 bg-transparent border border-white/10 rounded px-2 py-1 text-[12px] text-white font-mono"
              />
              {(() => {
                const r = report?.endpoints.find((x) => x.url === ep.url);
                if (!r) return null;
                const cls =
                  r.state === "ready"
                    ? "text-emerald-400 border-emerald-500/30"
                    : r.state === "warning"
                      ? "text-amber-400 border-amber-500/30"
                      : "text-red-400 border-red-500/30";
                const text =
                  r.state === "ready"
                    ? `✓ ${r.info.backend ?? "?"} · ${r.info.device ?? "?"}`
                    : r.state === "warning"
                      ? "⚠ degraded"
                      : "✗ offline";
                const fmt = (v: boolean | null | undefined) =>
                  v === null || v === undefined ? "n/a" : v ? "yes" : "NO";
                const tip = [
                  `reachable: ${fmt(r.checks.reachable)}`,
                  `healthy: ${fmt(r.checks.healthy)}`,
                  `warm: ${fmt(r.checks.warm)}`,
                  `model loaded: ${fmt(r.checks.model_loaded)}`,
                  `gpu active: ${fmt(r.checks.gpu_active)}`,
                  `version match: ${fmt(r.checks.version_match)}`,
                  r.detail,
                ]
                  .filter(Boolean)
                  .join("\n");
                return (
                  <span
                    title={tip}
                    className={`shrink-0 text-[11px] border rounded px-1.5 py-0.5 font-mono ${cls}`}
                  >
                    {text}
                  </span>
                );
              })()}
              <Button
                variant="ghost"
                size="icon"
                onClick={() => mutate(endpoints.filter((_, j) => j !== i))}
                title="Remove engine"
                className="hover:text-red-400 hover:bg-red-500/10"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </Button>
            </div>
          ))}
          <Button
            variant="secondary"
            size="sm"
            onClick={() =>
              mutate([...endpoints, { label: "", url: "", enabled: true }])
            }
          >
            <Plus className="w-3.5 h-3.5" /> Add engine
          </Button>
        </div>
      )}
    </div>
  );
}

const DEFAULT_GLOBAL_INGESTION: GlobalIngestionSettings = {
  summary: {
    enabled: false,
    max_summary_tokens: DEFAULT_INGESTION_CONFIG.max_summary_tokens,
    max_concurrent: 4,
    summary_models: [],
  },
  runpod_flash: {
    enabled: false,
    endpoint_id: "",
    endpoint_name: "polymath-gliner-relex",
    model_id: "knowledgator/gliner-relex-large-v0.5",
    model_revision: "9c4171ae1e690fc29b87f33579e50bcd65faf2cc",
    spacy_pipeline: "blank:en",
    min_workers: 0,
    max_workers: 8,
    worker_max_concurrency: 1,
    idle_timeout_seconds: 180,
    scaler_value: 1,
    request_batch_size: 32,
    request_concurrency: 8,
    timeout_seconds: 1800,
    poll_interval_seconds: 1,
    entity_threshold: 0.4,
    adjacency_threshold: 0.6,
    relation_threshold: 0.75,
    entity_lens_enabled: true,
    entity_lens_max_labels: 6,
    model_batch_size: 32,
    max_window_words: 260,
    benchmark_chunks: 5000,
    target_speedup: 100,
    budget_cap_usd: 40,
    estimated_gpu_rate_per_second_usd: 0.00031,
    cost_overhead_multiplier: 1.5,
  },
};

function cloneGlobalIngestion(
  source?: GlobalIngestionSettings | null,
): GlobalIngestionSettings {
  return {
    summary: {
      ...DEFAULT_GLOBAL_INGESTION.summary,
      ...(source?.summary ?? {}),
      summary_models: [...(source?.summary?.summary_models ?? [])],
    },
    runpod_flash: {
      ...DEFAULT_GLOBAL_INGESTION.runpod_flash,
      ...(source?.runpod_flash ?? {}),
    },
  };
}

function SummaryDefaultsCard() {
  const [ingestion, setIngestion] = useState<GlobalIngestionSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    getGlobalSettings()
      .then((r) => setIngestion(cloneGlobalIngestion(r.settings.ingestion)))
      .catch((e) => setError(String(e)));
  }, []);

  const mutateSummary = (
    patch: Partial<GlobalIngestionSettings["summary"]>,
  ) => {
    setIngestion((prev) => {
      const base = cloneGlobalIngestion(prev);
      return {
        ...base,
        summary: {
          ...base.summary,
          ...patch,
        },
      };
    });
    setDirty(true);
    setSaved(false);
  };

  const save = async () => {
    if (!ingestion) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const latest = cloneGlobalIngestion(
        (await getGlobalSettings()).settings.ingestion,
      );
      latest.summary = ingestion.summary;
      const { settings } = await updateGlobalSettings({ ingestion: latest });
      setIngestion(cloneGlobalIngestion(settings.ingestion));
      setDirty(false);
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  if (!ingestion) {
    return (
      <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 text-[12px] text-content-tertiary">
        Loading summary defaults...
      </div>
    );
  }

  const summary = ingestion.summary;
  const pool: ModelProfileRef[] = summary.summary_models ?? [];
  const requestedConcurrency = pool.reduce(
    (sum, m) => sum + (m.max_concurrent || 1),
    0,
  );

  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
            <Cloud size={16} className="text-amber-400" /> Summary Defaults
          </h3>
          <p className="text-[11px] text-content-tertiary mt-1 leading-relaxed">
            Global Ghost A defaults for new corpora and agent-created corpora.
            Configure one or more local/cloud summary models here; corpus-level
            pools still override this when set.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {saved && (
            <span className="text-[10px] text-emerald-300 tracking-widest uppercase">
              saved
            </span>
          )}
          <Button
            variant="primary"
            size="sm"
            onClick={save}
            disabled={!dirty || saving}
            className="text-[10px] font-bold tracking-widest uppercase"
          >
            <Check className="w-3 h-3" />
            {saving ? "Saving..." : "Save"}
          </Button>
        </div>
      </div>

      {error && (
        <div className="text-[11px] text-red-300 bg-red-950/30 border border-red-500/20 px-2 py-1 rounded">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        <label className="flex items-center justify-between gap-3 border border-white/5 bg-[#121418] rounded px-3 py-2">
          <span>
            <span className="block text-[10px] font-bold tracking-widest uppercase text-content-secondary">
              Enable by default
            </span>
            <span className="block text-[9px] text-content-tertiary">
              Prefills new corpus summaries
            </span>
          </span>
          <input
            type="checkbox"
            checked={summary.enabled}
            onChange={(e) => mutateSummary({ enabled: e.target.checked })}
            className="accent-accent-main"
          />
        </label>

        <label className="border border-white/5 bg-[#121418] rounded px-3 py-2">
          <span className="block text-[10px] font-bold tracking-widest uppercase text-content-secondary">
            Max summary tokens
          </span>
          <input
            type="number"
            min={32}
            max={1024}
            value={summary.max_summary_tokens}
            onChange={(e) =>
              mutateSummary({
                max_summary_tokens: Math.max(
                  32,
                  Math.min(1024, Number(e.target.value) || 175),
                ),
              })
            }
            className="mt-1 w-full bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono"
          />
        </label>

        <label className="border border-white/5 bg-[#121418] rounded px-3 py-2">
          <span className="block text-[10px] font-bold tracking-widest uppercase text-content-secondary">
            Global concurrency cap
          </span>
          <input
            type="number"
            min={1}
            max={64}
            value={summary.max_concurrent}
            onChange={(e) =>
              mutateSummary({
                max_concurrent: Math.max(
                  1,
                  Math.min(64, Number(e.target.value) || 1),
                ),
              })
            }
            className="mt-1 w-full bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono"
          />
          <span className="mt-1 block text-[9px] text-content-tertiary">
            requested {requestedConcurrency || 0} · active cap {summary.max_concurrent}
          </span>
        </label>
      </div>

      <IngestionModelPool
        title="Default Summary Models"
        subtitle="Local and cloud Ghost A lanes · copied into new empty corpus configs"
        value={pool}
        onChange={(next) => mutateSummary({ summary_models: next })}
        editing={true}
        testKind="chat"
      />
    </div>
  );
}

function RunpodNumberField({
  label,
  value,
  min,
  max,
  step = 1,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="border border-white/5 bg-[#121418] rounded px-3 py-2">
      <span className="block text-[9px] font-bold tracking-widest uppercase text-content-secondary">
        {label}
      </span>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) =>
          onChange(Math.max(min, Math.min(max, Number(event.target.value) || min)))
        }
        className="mt-1 w-full bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono"
      />
    </label>
  );
}

function RunpodFlashCard() {
  const [ingestion, setIngestion] = useState<GlobalIngestionSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<RunpodFlashTestResult | null>(null);

  useEffect(() => {
    getGlobalSettings()
      .then((response) =>
        setIngestion(cloneGlobalIngestion(response.settings.ingestion)),
      )
      .catch((reason) => setError(String(reason)));
  }, []);

  const mutate = (patch: Partial<RunpodFlashExtractionSettings>) => {
    setIngestion((previous) => {
      const next = cloneGlobalIngestion(previous);
      next.runpod_flash = { ...next.runpod_flash, ...patch };
      return next;
    });
    setDirty(true);
    setTestResult(null);
  };

  const save = async () => {
    if (!ingestion) return;
    setSaving(true);
    setError(null);
    try {
      const latest = cloneGlobalIngestion(
        (await getGlobalSettings()).settings.ingestion,
      );
      latest.runpod_flash = ingestion.runpod_flash;
      const response = await updateGlobalSettings({ ingestion: latest });
      setIngestion(cloneGlobalIngestion(response.settings.ingestion));
      setDirty(false);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      setTestResult(await testRunpodFlashExtraction());
    } catch (reason) {
      setError(String(reason));
    } finally {
      setTesting(false);
    }
  };

  if (!ingestion) {
    return (
      <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 text-[12px] text-content-tertiary">
        Loading Runpod Flash settings...
      </div>
    );
  }
  const value = ingestion.runpod_flash;
  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
            <Zap size={16} className="text-amber-400" /> Runpod Flash Burst Extraction
          </h3>
          <p className="text-[11px] text-content-tertiary mt-1 leading-relaxed">
            Joint GLiNER-Relex entity/relation inference on autoscaling GPUs. spaCy
            performs deterministic sentence windows; validation and storage stay local.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <Button
            variant="secondary"
            size="sm"
            onClick={test}
            disabled={dirty || testing || !value.enabled || !value.endpoint_id}
            title={dirty ? "Save settings before testing" : "Run one real extraction canary"}
          >
            {testing ? <Loader2 className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />}
            {testing ? "Testing..." : "Test"}
          </Button>
          <Button variant="primary" size="sm" onClick={save} disabled={!dirty || saving}>
            <Check className="w-3 h-3" /> {saving ? "Saving..." : "Save"}
          </Button>
        </div>
      </div>

      {error && (
        <div className="text-[11px] text-red-300 bg-red-950/30 border border-red-500/20 px-2 py-1 rounded">
          {error}
        </div>
      )}
      {testResult && (
        <div className={`text-[11px] border px-2 py-1 rounded ${testResult.ok ? "text-emerald-300 border-emerald-500/30" : "text-red-300 border-red-500/30"}`}>
          {testResult.ok ? "Canary passed" : "Canary failed"} · {testResult.entity_count} entities · {testResult.relation_count} relations
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <label className="flex items-center justify-between gap-3 border border-white/5 bg-[#121418] rounded px-3 py-2">
          <span>
            <span className="block text-[10px] font-bold tracking-widest uppercase text-content-secondary">Enabled</span>
            <span className="block text-[9px] text-content-tertiary">Available to corpus extraction profiles</span>
          </span>
          <input type="checkbox" checked={value.enabled} onChange={(event) => mutate({ enabled: event.target.checked })} className="accent-accent-main" />
        </label>
        <label className="border border-white/5 bg-[#121418] rounded px-3 py-2">
          <span className="block text-[9px] font-bold tracking-widest uppercase text-content-secondary">Endpoint ID</span>
          <input value={value.endpoint_id} onChange={(event) => mutate({ endpoint_id: event.target.value.trim() })} placeholder="Runpod endpoint ID" className="mt-1 w-full bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono" />
        </label>
        <label className="border border-white/5 bg-[#121418] rounded px-3 py-2">
          <span className="block text-[9px] font-bold tracking-widest uppercase text-content-secondary">Endpoint name</span>
          <input value={value.endpoint_name} onChange={(event) => mutate({ endpoint_name: event.target.value })} className="mt-1 w-full bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono" />
        </label>
        <label className="border border-white/5 bg-[#121418] rounded px-3 py-2">
          <span className="block text-[9px] font-bold tracking-widest uppercase text-content-secondary">Model</span>
          <input value={value.model_id} onChange={(event) => mutate({ model_id: event.target.value })} className="mt-1 w-full bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono" />
        </label>
        <label className="border border-white/5 bg-[#121418] rounded px-3 py-2">
          <span className="block text-[9px] font-bold tracking-widest uppercase text-content-secondary">Model revision</span>
          <input value={value.model_revision} onChange={(event) => mutate({ model_revision: event.target.value.trim() })} placeholder="Optional Hugging Face commit" className="mt-1 w-full bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono" />
        </label>
        <label className="border border-white/5 bg-[#121418] rounded px-3 py-2">
          <span className="block text-[9px] font-bold tracking-widest uppercase text-content-secondary">spaCy pipeline</span>
          <input value={value.spacy_pipeline} onChange={(event) => mutate({ spacy_pipeline: event.target.value })} className="mt-1 w-full bg-[#0b0c10] text-white border border-white/10 rounded px-2 py-1 text-[11px] font-mono" />
        </label>
      </div>

      <p className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary">Deploy-time endpoint controls</p>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <RunpodNumberField label="Min workers" value={value.min_workers} min={0} max={64} onChange={(next) => mutate({ min_workers: next })} />
        <RunpodNumberField label="Max workers" value={value.max_workers} min={1} max={64} onChange={(next) => mutate({ max_workers: next })} />
        <RunpodNumberField label="Worker concurrency" value={value.worker_max_concurrency} min={1} max={8} onChange={(next) => mutate({ worker_max_concurrency: next })} />
        <RunpodNumberField label="Idle timeout (s)" value={value.idle_timeout_seconds} min={5} max={3600} onChange={(next) => mutate({ idle_timeout_seconds: next })} />
        <RunpodNumberField label="Jobs / worker target" value={value.scaler_value} min={1} max={100} onChange={(next) => mutate({ scaler_value: next })} />
      </div>

      <p className="text-[9px] font-bold tracking-widest uppercase text-content-tertiary">Runtime dispatch controls</p>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <RunpodNumberField label="Chunks / request" value={value.request_batch_size} min={1} max={128} onChange={(next) => mutate({ request_batch_size: next })} />
        <RunpodNumberField label="In-flight requests" value={value.request_concurrency} min={1} max={64} onChange={(next) => mutate({ request_concurrency: next })} />
        <RunpodNumberField label="GPU batch" value={value.model_batch_size} min={1} max={256} onChange={(next) => mutate({ model_batch_size: next })} />
        <RunpodNumberField label="Window words" value={value.max_window_words} min={80} max={800} onChange={(next) => mutate({ max_window_words: next })} />
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <RunpodNumberField label="Entity threshold" value={value.entity_threshold} min={0} max={1} step={0.05} onChange={(next) => mutate({ entity_threshold: next })} />
        <RunpodNumberField label="Adjacency threshold" value={value.adjacency_threshold} min={0} max={1} step={0.05} onChange={(next) => mutate({ adjacency_threshold: next })} />
        <RunpodNumberField label="Relation threshold" value={value.relation_threshold} min={0} max={1} step={0.05} onChange={(next) => mutate({ relation_threshold: next })} />
        <RunpodNumberField label="Entity lens labels" value={value.entity_lens_max_labels} min={2} max={14} onChange={(next) => mutate({ entity_lens_max_labels: next })} />
        <RunpodNumberField label="Job timeout (s)" value={value.timeout_seconds} min={30} max={7200} onChange={(next) => mutate({ timeout_seconds: next })} />
        <RunpodNumberField label="Benchmark chunks" value={value.benchmark_chunks} min={100} max={50000} onChange={(next) => mutate({ benchmark_chunks: next })} />
        <RunpodNumberField label="Target speedup" value={value.target_speedup} min={1} max={1000} onChange={(next) => mutate({ target_speedup: next })} />
        <RunpodNumberField label="Budget cap ($)" value={value.budget_cap_usd} min={0} max={10000} step={1} onChange={(next) => mutate({ budget_cap_usd: next })} />
        <RunpodNumberField label="GPU rate ($/s)" value={value.estimated_gpu_rate_per_second_usd} min={0} max={1} step={0.00001} onChange={(next) => mutate({ estimated_gpu_rate_per_second_usd: next })} />
        <RunpodNumberField label="Cost overhead" value={value.cost_overhead_multiplier} min={1} max={10} step={0.1} onChange={(next) => mutate({ cost_overhead_multiplier: next })} />
      </div>
      <label className="flex items-center justify-between gap-3 border border-white/5 bg-[#121418] rounded px-3 py-2">
        <span>
          <span className="block text-[9px] font-bold tracking-widest uppercase text-content-secondary">Model-driven entity lens</span>
          <span className="block text-[9px] text-content-tertiary">Broad entity pass, then compact ontology batches for relation recall</span>
        </span>
        <input type="checkbox" checked={value.entity_lens_enabled} onChange={(event) => mutate({ entity_lens_enabled: event.target.checked })} className="accent-accent-main" />
      </label>
      <p className="text-[10px] text-content-tertiary">
        Store the Runpod credential in API Keys. Worker, scaler, and idle controls are a deployment contract; redeploy Flash after changing them. Cost is a conservative estimate from job execution telemetry and the configured rate; Runpod billing remains authoritative.
      </p>
    </div>
  );
}

// ── Main component ───────────────────────────────────────────────────────

export function IngestionSettingsTab() {
  const config: IngestionConfig = DEFAULT_INGESTION_CONFIG;

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold text-white mb-2">Ingestion</h2>
        <p className="text-[13px] text-gray-500">
          Global defaults for document ingestion pipeline. These values
          pre-fill the corpus creation form. Per-corpus overrides are frozen
          after first document ingest.
        </p>
      </div>

      {/* Extraction Engines — mutable, applies on next ingest */}
      <ExtractionEnginesCard />

      {/* Summary Defaults — mutable, applies to new corpora + runtime cap */}
      <SummaryDefaultsCard />

      <RunpodFlashCard />

      {/* Info banner */}
      <div className="flex items-start gap-3 bg-blue-950/20 border border-blue-700/30 rounded-lg px-4 py-3">
        <Info className="w-4 h-4 text-blue-400 mt-0.5 shrink-0" />
        <p className="text-[12px] text-blue-300/80">
          Structural chunking and embedding identity are still read-only global
          defaults. Summary model lanes above are mutable and are copied into
          new corpora when the corpus config does not already define a pool.
        </p>
      </div>

      {/* Embedding */}
      <SectionCard title="Embedding" icon={Layers} iconColor="text-purple-400">
        <ReadOnlyField
          label="Embedding Model"
          value={config.embedding_model}
          mono
        />
        <ReadOnlyField
          label="Dimension"
          value={config.embedding_dimension}
          hint="Changing requires full Qdrant re-index"
        />
        <ReadOnlyField
          label="Model ID"
          value={config.embedding_model_id}
          mono
        />
        <ReadOnlyField
          label="Embed Mode"
          value={config.embed_mode}
          hint="local = sentence-transformers GPU | api = OpenAI-compatible | modal = Modal cloud"
          mono
        />
      </SectionCard>

      {/* Chunking */}
      <SectionCard
        title="Chunking — Auto Policy"
        icon={Layers}
        iconColor="text-cyan-400"
      >
        <ReadOnlyField
          label="Chunking Mode"
          value="AUTO"
          hint="Resolved per file after parsing: headings, token windows, or PDF page groups"
          mono
        />
        <ReadOnlyField
          label="Parent Chunk Tokens (min / target / max)"
          value={formatTokenBudget(config.parent_chunk_tokens)}
          hint="Used by Auto when sections/pages need token-sized parents"
          mono
        />
        <ReadOnlyField
          label="Child Chunk Tokens (min / target / max)"
          value={formatTokenBudget(config.child_chunk_tokens)}
          hint="Auto currently resolves children to sentence-merged passages"
          mono
        />
        <ReadOnlyField
          label="Chunk Overlap"
          value={`${config.chunk_overlap} tokens`}
          hint="Trailing sentences carried to next parent"
        />
        <ReadOnlyField
          label="Max Summary Tokens"
          value={config.max_summary_tokens}
          hint="Token cap per parent summary (GHOST A output)"
        />
        <ReadOnlyField
          label="Child Splitter"
          value="AUTO → semantic_split + structured routers"
          hint="One idea per child; lists/lines/code/tables/transcripts auto-route; SaT sentence engine; semantic escalation for topic-fused paragraphs"
          mono
        />
      </SectionCard>

      {/* GHOST A — Summary Pool */}
      <SectionCard
        title="GHOST A — Summary Pool"
        icon={Layers}
        iconColor="text-amber-400"
      >
        <ReadOnlyField
          label="Summary Models"
          value={
            (config.summary_models ?? [])
              .map((m) => m.model)
              .join(", ") || "—"
          }
          hint="Round-robin pool. Edit per-corpus in Corpus Manager."
          mono
        />
        <ReadOnlyField
          label="Total Concurrency (sum)"
          value={(config.summary_models ?? [])
            .reduce((sum, m) => sum + (m.max_concurrent || 1), 0)}
          hint="Sum of each entry's max_concurrent"
        />
        <ReadOnlyField
          label="Max Tokens per Summary"
          value={config.max_summary_tokens}
        />
        <ReadOnlyField
          label="Summarization Enabled"
          value={config.chunk_summarization}
          hint="Run GHOST A: summarize parents + embed summaries"
        />
      </SectionCard>

      {/* GHOST B — Extraction Pool */}
      <SectionCard
        title="GHOST B — Extraction Pool"
        icon={Layers}
        iconColor="text-rose-400"
      >
        <ReadOnlyField
          label="Extraction Models"
          value={
            config.models_linked
              ? "(using Summary pool — models_linked=true)"
              : (config.extraction_models ?? [])
                  .map((m) => m.model)
                  .join(", ") || "—"
          }
          hint="Round-robin pool. Edit per-corpus in Corpus Manager."
          mono
        />
        <ReadOnlyField
          label="Total Concurrency (sum)"
          value={(config.models_linked
            ? config.summary_models ?? []
            : config.extraction_models ?? []
          ).reduce((sum, m) => sum + (m.max_concurrent || 1), 0)}
          hint="Sum of each entry's max_concurrent"
        />
        <ReadOnlyField
          label="Entity Confidence Threshold"
          value={config.entity_confidence_threshold}
          hint="Min confidence to keep extracted entity/relation"
        />
        <ReadOnlyField
          label="Neo4j Enabled"
          value={config.use_neo4j}
          hint="Run GHOST B: entity extraction + Neo4j graph"
        />
      </SectionCard>

      {/* Schema (Ontology-Lite) — Phase 14 */}
      <SectionCard
        title="Schema (Ontology-Lite)"
        icon={Layers}
        iconColor="text-fuchsia-400"
      >
        <ReadOnlyField
          label="Entity Types"
          value={
            config.entity_schema && config.entity_schema.length > 0
              ? config.entity_schema.join(", ")
              : "(open — default 4-bucket enum)"
          }
          hint="LLM creates instances freely under these types. 'other' is implicit fallback."
          mono
        />
        <ReadOnlyField
          label="Relation Predicates"
          value={
            config.relation_schema && config.relation_schema.length > 0
              ? config.relation_schema.join(", ")
              : "(open — free-form predicates)"
          }
          hint="'related_to' is implicit fallback."
          mono
        />
        <ReadOnlyField
          label="Strict Mode"
          value={config.schema_strict ?? "soft"}
          hint="soft = out-of-schema entries remap to sentinels (universal schema is always 'soft')."
          mono
        />
      </SectionCard>

      {/* Qdrant Targets */}
      <SectionCard
        title="Qdrant Targets"
        icon={Layers}
        iconColor="text-green-400"
      >
        <ReadOnlyField
          label="Target Collections"
          value={config.target_qdrant_collections.join(", ")}
          hint="Collections written during ingest: naive | hrag | graph"
          mono
        />
      </SectionCard>
    </div>
  );
}
