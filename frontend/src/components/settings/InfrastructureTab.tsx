// InfrastructureTab.tsx — 9-service health dashboard + Modal GPU deploy section
import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  Server,
  RefreshCw,
  CheckCircle,
  XCircle,
  Clock,
  Database,
  Cpu,
  Cloud,
  Globe,
  HardDrive,
  Zap,
  Layers,
  Activity,
  Save,
  Loader2,
  AlertTriangle,
  ShieldCheck,
  Eye,
  EyeOff,
  Rocket,
  Trash2,
  ChevronDown,
  ChevronRight,
} from "lucide-react";
import * as api from "../../lib/api";
import type {
  ServiceStatus,
  InfrastructureSettings,
  ModalDeploySettings,
  ModalGpuTier,
  ModalStatus,
  ModalDeployEvent,
} from "../../types";
import { MODAL_GPU_TIERS } from "../../types";

// Service definitions with icons and labels
const SERVICES: {
  key: string;
  label: string;
  icon: typeof Server;
  iconColor: string;
  urlKey: keyof InfrastructureSettings;
}[] = [
  {
    key: "mongodb",
    label: "MongoDB",
    icon: Database,
    iconColor: "text-green-400",
    urlKey: "mongodb_url",
  },
  {
    key: "qdrant",
    label: "Qdrant",
    icon: HardDrive,
    iconColor: "text-blue-400",
    urlKey: "qdrant_url",
  },
  {
    key: "neo4j",
    label: "Neo4j",
    icon: Globe,
    iconColor: "text-purple-400",
    urlKey: "neo4j_uri",
  },
  {
    key: "litellm",
    label: "LiteLLM",
    icon: Zap,
    iconColor: "text-amber-400",
    urlKey: "litellm_base_url",
  },
  {
    key: "ollama",
    label: "Ollama",
    icon: Cpu,
    iconColor: "text-cyan-400",
    urlKey: "ollama_base_url",
  },
  {
    key: "redis",
    label: "Redis",
    icon: Activity,
    iconColor: "text-red-400",
    urlKey: "redis_url",
  },
  {
    key: "embedder",
    label: "Embedder",
    icon: Layers,
    iconColor: "text-teal-400",
    urlKey: "embedder_url",
  },
  {
    key: "reranker",
    label: "Reranker",
    icon: Cloud,
    iconColor: "text-indigo-400",
    urlKey: "reranker_url",
  },
  {
    key: "modal",
    label: "Modal GPU",
    icon: Cloud,
    iconColor: "text-pink-400",
    urlKey: "modal_embedder_url",
  },
];

function StatusBadge({ status }: { status: "ok" | "error" | null }) {
  if (status === "ok") {
    return (
      <div className="flex items-center gap-1.5 text-green-400">
        <CheckCircle className="w-3.5 h-3.5" />
        <span className="text-[11px] font-bold uppercase tracking-wider">
          OK
        </span>
      </div>
    );
  }
  if (status === "error") {
    return (
      <div className="flex items-center gap-1.5 text-red-400">
        <XCircle className="w-3.5 h-3.5" />
        <span className="text-[11px] font-bold uppercase tracking-wider">
          ERROR
        </span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-1.5 text-gray-500">
      <Clock className="w-3.5 h-3.5" />
      <span className="text-[11px] font-bold uppercase tracking-wider">
        UNKNOWN
      </span>
    </div>
  );
}

function ServiceCard({
  service,
  result,
  onTest,
  isTesting,
}: {
  service: (typeof SERVICES)[number];
  result: ServiceStatus | undefined;
  onTest: () => void;
  isTesting: boolean;
}) {
  const Icon = service.icon;

  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-4 flex flex-col gap-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 bg-[#333] rounded-lg flex items-center justify-center">
            <Icon className={`w-4 h-4 ${service.iconColor}`} />
          </div>
          <div>
            <div className="text-[13px] font-semibold text-white">
              {service.label}
            </div>
            <div className="text-[10px] font-mono text-gray-500 truncate max-w-[160px]">
              {service.urlKey}
            </div>
          </div>
        </div>
        <StatusBadge status={result?.status ?? null} />
      </div>

      {/* Metrics */}
      {result && (
        <div className="flex items-center gap-4 text-[11px] text-gray-400">
          {result.latency_ms !== null && (
            <div className="flex items-center gap-1">
              <Clock className="w-3 h-3" />
              <span>{result.latency_ms}ms</span>
            </div>
          )}
          {result.error && (
            <div
              className="text-red-400 truncate max-w-[200px]"
              title={result.error}
            >
              {result.error}
            </div>
          )}
        </div>
      )}

      {/* Test Button */}
      <button
        onClick={onTest}
        disabled={isTesting}
        className="w-full px-3 py-1.5 text-[10px] font-bold tracking-widest uppercase border border-white/10 text-gray-300 hover:border-accent-main hover:text-accent-main disabled:opacity-40 disabled:cursor-not-allowed rounded transition-colors"
      >
        {isTesting ? (
          <span className="flex items-center justify-center gap-1.5">
            <RefreshCw className="w-3 h-3 animate-spin" />
            Testing...
          </span>
        ) : (
          "Test"
        )}
      </button>
    </div>
  );
}

export function InfrastructureTab() {
  const [results, setResults] = useState<Record<string, ServiceStatus>>({});
  const [isLoading, setIsLoading] = useState(false);
  const [testingService, setTestingService] = useState<string | null>(null);
  const [lastTested, setLastTested] = useState<Date | null>(null);

  const runAllTests = useCallback(async () => {
    setIsLoading(true);
    setTestingService(null);
    try {
      const resp = await api.testInfrastructure();
      setResults(resp.services);
      setLastTested(new Date());
    } catch (err) {
      console.error("Infrastructure test failed:", err);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const testSingleService = useCallback(async (serviceName: string) => {
    setTestingService(serviceName);
    try {
      const resp = await api.testService(serviceName);
      setResults((prev) => ({
        ...prev,
        [resp.service]: {
          status: resp.status as "ok" | "error" | null,
          latency_ms: resp.latency_ms,
          error: resp.error ?? null,
        },
      }));
      setLastTested(new Date());
    } catch (err) {
      setResults((prev) => ({
        ...prev,
        [serviceName]: {
          status: "error",
          latency_ms: null,
          error: err instanceof Error ? err.message : String(err),
        },
      }));
    } finally {
      setTestingService(null);
    }
  }, []);

  // Auto-test on mount
  useEffect(() => {
    runAllTests();
  }, [runAllTests]);

  // Summary counts
  const okCount = Object.values(results).filter(
    (r) => r.status === "ok",
  ).length;
  const errorCount = Object.values(results).filter(
    (r) => r.status === "error",
  ).length;
  const totalTested = Object.keys(results).length;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-white mb-2">
            Infrastructure
          </h2>
          <p className="text-[13px] text-gray-500">
            Service health dashboard. URLs are read-only from{" "}
            <code className="text-[11px] bg-[#333] px-1 py-0.5 rounded text-gray-400">
              .env
            </code>
            . Test connectivity below.
          </p>
        </div>
        <button
          onClick={runAllTests}
          disabled={isLoading}
          className="flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-bold tracking-widest uppercase bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 text-white rounded transition-colors shrink-0"
        >
          <RefreshCw className={`w-3 h-3 ${isLoading ? "animate-spin" : ""}`} />
          {isLoading ? "Testing All..." : "Test All"}
        </button>
      </div>

      {/* Summary bar */}
      {totalTested > 0 && (
        <div className="flex items-center gap-4 px-4 py-2.5 bg-[#2a2a2a] border border-white/5 rounded-lg">
          <Server className="w-4 h-4 text-gray-400" />
          <span className="text-[12px] text-gray-300">
            <span className="text-green-400 font-semibold">{okCount}</span>{" "}
            healthy
            {" · "}
            <span className="text-red-400 font-semibold">
              {errorCount}
            </span>{" "}
            errors
            {" · "}
            <span className="text-gray-500">{totalTested}</span> tested
          </span>
          {lastTested && (
            <span className="ml-auto text-[10px] text-gray-600 font-mono">
              Last: {lastTested.toLocaleTimeString()}
            </span>
          )}
        </div>
      )}

      {/* Service grid */}
      {isLoading && totalTested === 0 ? (
        <div className="flex items-center justify-center py-16 text-[12px] text-gray-500">
          <RefreshCw className="w-4 h-4 animate-spin mr-2" />
          Testing all services...
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {SERVICES.map((svc) => (
            <ServiceCard
              key={svc.key}
              service={svc}
              result={results[svc.key]}
              onTest={() => testSingleService(svc.key)}
              isTesting={testingService === svc.key}
            />
          ))}
        </div>
      )}

      {/* Info footer */}
      <div className="text-[11px] text-gray-600 leading-relaxed px-1">
        Service URLs come from environment variables in{" "}
        <code className="bg-[#333] px-1 py-0.5 rounded">.env</code>. Changing
        them requires a container restart. API keys and passwords are masked as{" "}
        <code className="bg-[#333] px-1 py-0.5 rounded">••••••••</code>.
      </div>

      {/* Modal GPU section — Sprint 2B one-click deploy */}
      <ModalSection />
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// ModalSection — Sprint 2B
//
// Co-located: Modal control-plane tokens (token_id + token_secret), optional
// proxy Bearer, GPU tier, container fleet, idle timeout, deploy/destroy/
// redeploy actions, and live SSE deploy progress.
//
// Tokens persist via /api/settings/api-keys (Fernet-encrypted at rest).
// Deploy config persists via /api/settings PUT modal subsection.
// Deploy/destroy/status use new /api/infrastructure/modal/* endpoints
// (Sprint 2B Terminal 1). If the backend hasn't shipped them yet the
// buttons will surface a 404 inline — design choice per handshake.
// ───────────────────────────────────────────────────────────────────────────

function ModalSection() {
  const [open, setOpen] = useState(true);

  // Persisted settings
  const [cfg, setCfg] = useState<ModalDeploySettings | null>(null);
  const [cfgDirty, setCfgDirty] = useState(false);
  const [isLoadingCfg, setIsLoadingCfg] = useState(false);
  const [savingCfg, setSavingCfg] = useState(false);
  const [cfgError, setCfgError] = useState<string | null>(null);

  // Token storage (api_keys: modal_token_id, modal_token_secret, modal)
  const [tokenIdMasked, setTokenIdMasked] = useState<string>("[not set]");
  const [tokenSecretMasked, setTokenSecretMasked] =
    useState<string>("[not set]");
  const [bearerMasked, setBearerMasked] = useState<string>("[not set]");
  const [tokenIdDraft, setTokenIdDraft] = useState("");
  const [tokenSecretDraft, setTokenSecretDraft] = useState("");
  const [bearerDraft, setBearerDraft] = useState("");
  const [revealId, setRevealId] = useState(false);
  const [revealSecret, setRevealSecret] = useState(false);
  const [revealBearer, setRevealBearer] = useState(false);
  const [savingTokens, setSavingTokens] = useState(false);
  const [tokensError, setTokensError] = useState<string | null>(null);
  const [tokensSavedAt, setTokensSavedAt] = useState<number | null>(null);

  // Verify
  const [verifying, setVerifying] = useState(false);
  const [verifyResult, setVerifyResult] = useState<{
    ok: boolean;
    workspace: string | null;
    error: string | null;
  } | null>(null);

  // Status (deployed?)
  const [status, setStatus] = useState<ModalStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [refreshingStatus, setRefreshingStatus] = useState(false);

  // Deploy stream
  const [deploying, setDeploying] = useState(false);
  const [deployEvent, setDeployEvent] = useState<ModalDeployEvent | null>(null);
  const [deployError, setDeployError] = useState<string | null>(null);
  const cancelDeploy = useRef<boolean>(false);

  // Destroy
  const [destroying, setDestroying] = useState(false);
  const [confirmDestroy, setConfirmDestroy] = useState(false);

  // ── Load on mount ──────────────────────────────────────────────────────
  const loadAll = useCallback(async () => {
    setIsLoadingCfg(true);
    setCfgError(null);
    try {
      const [{ settings }, keys, st] = await Promise.all([
        api.getGlobalSettings(),
        api.listApiKeys(),
        api.getModalStatus().catch((err) => {
          // Backend endpoint may not be live yet — surface in status panel,
          // not as a fatal error for the whole section.
          setStatusError(err instanceof Error ? err.message : String(err));
          return null;
        }),
      ]);
      setCfg(settings.modal);
      setCfgDirty(false);
      setTokenIdMasked(keys.keys?.modal_token_id || "[not set]");
      setTokenSecretMasked(keys.keys?.modal_token_secret || "[not set]");
      setBearerMasked(keys.keys?.modal || "[not set]");
      if (st) setStatus(st);
    } catch (err) {
      setCfgError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsLoadingCfg(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const updateCfg = <K extends keyof ModalDeploySettings>(
    key: K,
    value: ModalDeploySettings[K],
  ) => {
    if (!cfg) return;
    setCfg({ ...cfg, [key]: value });
    setCfgDirty(true);
  };

  const saveCfg = async () => {
    if (!cfg) return;
    setSavingCfg(true);
    setCfgError(null);
    try {
      const { settings } = await api.updateGlobalSettings({ modal: cfg });
      setCfg(settings.modal);
      setCfgDirty(false);
    } catch (err) {
      setCfgError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingCfg(false);
    }
  };

  const saveTokens = async () => {
    setSavingTokens(true);
    setTokensError(null);
    try {
      const patch: Record<string, string> = {};
      if (tokenIdDraft.trim()) patch.modal_token_id = tokenIdDraft.trim();
      if (tokenSecretDraft.trim())
        patch.modal_token_secret = tokenSecretDraft.trim();
      if (bearerDraft.trim()) patch.modal = bearerDraft.trim();
      if (Object.keys(patch).length === 0) {
        setTokensError("Nothing to save — paste a token first.");
        return;
      }
      const data = await api.updateApiKeys(patch);
      setTokenIdMasked(data.keys?.modal_token_id || "[not set]");
      setTokenSecretMasked(data.keys?.modal_token_secret || "[not set]");
      setBearerMasked(data.keys?.modal || "[not set]");
      setTokenIdDraft("");
      setTokenSecretDraft("");
      setBearerDraft("");
      setTokensSavedAt(Date.now());
      setTimeout(() => setTokensSavedAt(null), 2500);
    } catch (err) {
      setTokensError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingTokens(false);
    }
  };

  const verify = async () => {
    setVerifying(true);
    setVerifyResult(null);
    try {
      const res = await api.verifyModalToken();
      setVerifyResult(res);
      if (res.ok && cfg && res.workspace && res.workspace !== cfg.workspace) {
        // Sync workspace into cfg so URL preview/persistence stays current.
        setCfg({ ...cfg, workspace: res.workspace });
        setCfgDirty(true);
      }
    } catch (err) {
      setVerifyResult({
        ok: false,
        workspace: null,
        error: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setVerifying(false);
    }
  };

  const refreshStatus = async () => {
    setRefreshingStatus(true);
    setStatusError(null);
    try {
      const st = await api.getModalStatus();
      setStatus(st);
    } catch (err) {
      setStatusError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshingStatus(false);
    }
  };

  const startDeploy = async () => {
    if (!cfg) return;
    if (cfgDirty) await saveCfg();
    setDeploying(true);
    setDeployError(null);
    setDeployEvent(null);
    cancelDeploy.current = false;

    // Fire-and-stream: kick off POST /deploy (don't await), then read SSE for
    // phase progress. Backend serializes deploy via module-level lock so
    // concurrent fires from another tab won't race.
    const deployPromise = api
      .deployModal({
        gpu_tier: cfg.gpu_tier,
        max_containers: cfg.max_containers,
        min_containers: cfg.min_containers,
        idle_timeout: cfg.idle_timeout_seconds,
        app_name: cfg.app_name,
      })
      .catch((err) => {
        setDeployError(err instanceof Error ? err.message : String(err));
        return null;
      });

    try {
      for await (const evt of api.streamModalDeploy()) {
        if (cancelDeploy.current) break;
        setDeployEvent(evt);
        if (evt.phase === "ready") break;
        if (evt.phase === "failed") {
          setDeployError(evt.error || `Failed at ${evt.at_phase ?? "deploy"}`);
          break;
        }
      }
    } catch (err) {
      // SSE may not be supported by backend yet — fall back to deploy result.
      const msg = err instanceof Error ? err.message : String(err);
      if (!deployError) setDeployError(msg);
    }

    const result = await deployPromise;
    if (result) {
      setDeployEvent({
        phase: "ready",
        message: "Deployed",
        url: result.url,
        app_id: result.app_id,
      });
    }
    setDeploying(false);
    await refreshStatus();
  };

  const cancelStream = () => {
    cancelDeploy.current = true;
  };

  const destroy = async () => {
    setDestroying(true);
    setDeployError(null);
    try {
      await api.destroyModal();
      setStatus({
        deployed: false,
        url: null,
        app_id: null,
        container_count: null,
        deployed_at: null,
      });
      setDeployEvent(null);
      setConfirmDestroy(false);
    } catch (err) {
      setDeployError(err instanceof Error ? err.message : String(err));
    } finally {
      setDestroying(false);
    }
  };

  const tokensReady =
    tokenIdMasked !== "[not set]" && tokenSecretMasked !== "[not set]";

  const gpuMeta = useMemo(
    () => MODAL_GPU_TIERS.find((g) => g.tier === cfg?.gpu_tier),
    [cfg?.gpu_tier],
  );

  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg overflow-hidden">
      {/* Header — collapsible */}
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-4 py-3 hover:bg-white/[0.02] transition-colors"
      >
        <div className="flex items-center gap-2">
          {open ? (
            <ChevronDown size={14} className="text-gray-500" />
          ) : (
            <ChevronRight size={14} className="text-gray-500" />
          )}
          <Cloud size={16} className="text-pink-400" />
          <h3 className="text-[15px] font-semibold text-white">Modal GPU</h3>
          {status?.deployed ? (
            <span className="px-1.5 py-0.5 text-[9px] font-bold tracking-widest uppercase border border-emerald-500/40 text-emerald-300 rounded">
              Deployed
            </span>
          ) : (
            <span className="px-1.5 py-0.5 text-[9px] font-bold tracking-widest uppercase border border-white/10 text-gray-500 rounded">
              Not deployed
            </span>
          )}
        </div>
        <span className="text-[11px] text-gray-500">
          one-click cloud GPU embedding
        </span>
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-5 border-t border-white/5">
          {(cfgError || isLoadingCfg) && (
            <div className="text-[12px] text-gray-500 italic flex items-center gap-2 pt-3">
              {isLoadingCfg && <Loader2 size={14} className="animate-spin" />}
              {cfgError ?? "Loading Modal config…"}
            </div>
          )}

          {cfg && (
            <>
              {/* ── 1. Tokens ────────────────────────────────────────── */}
              <Section title="Modal Tokens" accent="purple" pad>
                <p className="text-[11px] text-gray-500 leading-relaxed">
                  Token ID + Secret authorize deploy/destroy against
                  modal.com (control-plane). Proxy Bearer is used by the
                  backend when calling your deployed endpoint with{" "}
                  <code className="bg-[#1a1a1a] px-1 rounded">use_auth</code>{" "}
                  on. All three encrypt at rest.
                </p>

                <TokenInput
                  label="Token ID"
                  storedMask={tokenIdMasked}
                  draft={tokenIdDraft}
                  onChange={setTokenIdDraft}
                  reveal={revealId}
                  setReveal={setRevealId}
                  placeholder="ak-..."
                />
                <TokenInput
                  label="Token Secret"
                  storedMask={tokenSecretMasked}
                  draft={tokenSecretDraft}
                  onChange={setTokenSecretDraft}
                  reveal={revealSecret}
                  setReveal={setRevealSecret}
                  placeholder="as-..."
                />
                <TokenInput
                  label="Proxy Bearer (optional)"
                  storedMask={bearerMasked}
                  draft={bearerDraft}
                  onChange={setBearerDraft}
                  reveal={revealBearer}
                  setReveal={setRevealBearer}
                  placeholder="(only if endpoint is auth-gated)"
                />

                {tokensError && (
                  <div className="flex items-start gap-2 text-[12px] text-red-300 border border-red-500/30 bg-red-500/5 rounded p-2">
                    <AlertTriangle size={14} className="shrink-0 mt-0.5" />
                    <span className="font-mono break-words">
                      {tokensError}
                    </span>
                  </div>
                )}

                <div className="flex items-center gap-2 flex-wrap">
                  <button
                    onClick={saveTokens}
                    disabled={
                      savingTokens ||
                      (!tokenIdDraft.trim() &&
                        !tokenSecretDraft.trim() &&
                        !bearerDraft.trim())
                    }
                    className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-semibold border border-blue-500/50 text-blue-300 hover:bg-blue-500/10 disabled:opacity-40 disabled:cursor-not-allowed rounded"
                  >
                    {savingTokens ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : tokensSavedAt ? (
                      <CheckCircle size={13} className="text-emerald-400" />
                    ) : (
                      <Save size={13} />
                    )}
                    Save tokens
                  </button>
                  <button
                    onClick={verify}
                    disabled={verifying || !tokensReady}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-semibold border border-emerald-500/50 text-emerald-300 hover:bg-emerald-500/10 disabled:opacity-40 disabled:cursor-not-allowed rounded"
                    title={
                      tokensReady
                        ? "Verify Modal tokens"
                        : "Save Token ID + Secret first"
                    }
                  >
                    {verifying ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : (
                      <ShieldCheck size={13} />
                    )}
                    Test Token
                  </button>
                  {verifyResult?.ok && (
                    <span className="text-[11px] text-emerald-300 flex items-center gap-1">
                      <CheckCircle size={12} />
                      workspace:{" "}
                      <code className="font-mono">{verifyResult.workspace}</code>
                    </span>
                  )}
                  {verifyResult && !verifyResult.ok && (
                    <span className="text-[11px] text-red-300 flex items-center gap-1">
                      <AlertTriangle size={12} />
                      {verifyResult.error ?? "verification failed"}
                    </span>
                  )}
                </div>
              </Section>

              {/* ── 2. Deploy Config ─────────────────────────────────── */}
              <Section title="Deploy Configuration" accent="blue" pad>
                {/* App name + workspace */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label className="text-[11px] font-semibold text-gray-300 block mb-1">
                      App Name
                    </label>
                    <input
                      type="text"
                      value={cfg.app_name}
                      onChange={(e) => updateCfg("app_name", e.target.value)}
                      placeholder="polymath-embedder"
                      className="w-full bg-[#1a1a1a] border border-white/10 rounded px-2 py-1 text-[12px] text-white font-mono"
                    />
                  </div>
                  <div>
                    <label className="text-[11px] font-semibold text-gray-300 block mb-1">
                      Workspace
                    </label>
                    <input
                      type="text"
                      value={cfg.workspace}
                      onChange={(e) => updateCfg("workspace", e.target.value)}
                      placeholder="(filled by Test Token)"
                      className="w-full bg-[#1a1a1a] border border-white/10 rounded px-2 py-1 text-[12px] text-white font-mono"
                    />
                  </div>
                </div>

                {/* GPU tier grid */}
                <div>
                  <label className="text-[11px] font-semibold text-gray-300 block mb-1">
                    GPU Tier
                  </label>
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                    {MODAL_GPU_TIERS.map((g) => (
                      <button
                        key={g.tier}
                        onClick={() =>
                          updateCfg("gpu_tier", g.tier as ModalGpuTier)
                        }
                        className={`text-left p-2.5 border rounded transition-colors ${
                          cfg.gpu_tier === g.tier
                            ? "border-blue-400/70 bg-blue-500/10"
                            : "border-white/10 bg-[#1a1a1a] hover:border-white/30"
                        }`}
                      >
                        <div className="text-[12px] font-bold text-white">
                          {g.label}
                        </div>
                        <div className="text-[9px] text-blue-300 font-mono">
                          {g.priceHint}
                        </div>
                        <div className="text-[9px] text-gray-500 mt-0.5 leading-snug">
                          {g.notes}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>

                {/* Containers + idle timeout */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label className="flex items-center justify-between text-[11px] font-semibold text-gray-300 mb-1">
                      <span>Max containers</span>
                      <span className="font-mono text-blue-300">
                        {cfg.max_containers}
                      </span>
                    </label>
                    <input
                      type="range"
                      min={1}
                      max={10}
                      value={Math.min(10, Math.max(1, cfg.max_containers))}
                      onChange={(e) =>
                        updateCfg("max_containers", Number(e.target.value))
                      }
                      className="w-full accent-blue-400"
                    />
                    <div className="flex items-center justify-between text-[9px] text-gray-500 font-mono">
                      <span>1</span>
                      <span>10</span>
                    </div>
                  </div>
                  <div>
                    <label className="flex items-center justify-between text-[11px] font-semibold text-gray-300 mb-1">
                      <span>Min (warm) containers</span>
                      <span className="font-mono text-blue-300">
                        {cfg.min_containers}
                      </span>
                    </label>
                    <input
                      type="range"
                      min={0}
                      max={5}
                      value={cfg.min_containers}
                      onChange={(e) =>
                        updateCfg("min_containers", Number(e.target.value))
                      }
                      className="w-full accent-blue-400"
                    />
                    <div className="flex items-center justify-between text-[9px] text-gray-500 font-mono">
                      <span>0 (scale-to-zero)</span>
                      <span>5</span>
                    </div>
                  </div>
                </div>

                <div>
                  <label className="text-[11px] font-semibold text-gray-300 block mb-1">
                    Idle timeout (seconds)
                  </label>
                  <input
                    type="number"
                    min={30}
                    max={3600}
                    value={cfg.idle_timeout_seconds}
                    onChange={(e) =>
                      updateCfg(
                        "idle_timeout_seconds",
                        Number(e.target.value) || 120,
                      )
                    }
                    className="w-full bg-[#1a1a1a] border border-white/10 rounded px-2 py-1 text-[12px] text-white"
                  />
                  <div className="text-[10px] text-gray-500 mt-1">
                    Containers shut down after this many seconds idle. Default
                    120s.
                  </div>
                </div>

                {gpuMeta && (
                  <div className="rounded border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-200 font-mono">
                    {gpuMeta.priceHint} × {cfg.min_containers} warm × 730h ={" "}
                    <span className="text-amber-100 font-bold">
                      $
                      {(
                        gpuMeta.pricePerHour *
                        Math.max(0, cfg.min_containers) *
                        730
                      ).toFixed(2)}
                      /mo idle cost
                    </span>
                    {cfg.min_containers === 0 && (
                      <span className="text-gray-500 ml-2">
                        (scale-to-zero — pay per request)
                      </span>
                    )}
                  </div>
                )}

                <div className="flex items-center gap-2">
                  <button
                    onClick={saveCfg}
                    disabled={!cfgDirty || savingCfg}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-semibold border border-white/10 text-gray-300 hover:border-white/30 disabled:opacity-40 disabled:cursor-not-allowed rounded"
                  >
                    {savingCfg ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : (
                      <Save size={13} />
                    )}
                    Save config
                  </button>
                  {cfgDirty && (
                    <span className="text-[11px] text-amber-400">
                      Unsaved changes — Deploy will save first
                    </span>
                  )}
                </div>
              </Section>

              {/* ── 3. Status + Actions ──────────────────────────────── */}
              <Section title="Deployment" accent="purple" pad>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-[11px]">
                  <div>
                    <div className="text-gray-500 uppercase tracking-widest text-[9px] mb-0.5">
                      Status
                    </div>
                    <div
                      className={
                        status?.deployed
                          ? "text-emerald-300 font-bold"
                          : "text-gray-400"
                      }
                    >
                      {status?.deployed ? "Live" : "Not deployed"}
                    </div>
                  </div>
                  <div>
                    <div className="text-gray-500 uppercase tracking-widest text-[9px] mb-0.5">
                      Containers
                    </div>
                    <div className="text-white font-mono">
                      {status?.container_count ?? "—"}
                    </div>
                  </div>
                  <div className="col-span-2">
                    <div className="text-gray-500 uppercase tracking-widest text-[9px] mb-0.5">
                      URL
                    </div>
                    <div className="text-white font-mono break-all text-[10px]">
                      {status?.url || cfg.embedder_url || "—"}
                    </div>
                  </div>
                  <div className="col-span-2">
                    <div className="text-gray-500 uppercase tracking-widest text-[9px] mb-0.5">
                      Last deploy
                    </div>
                    <div className="text-gray-400 font-mono text-[10px]">
                      {status?.deployed_at
                        ? new Date(status.deployed_at).toLocaleString()
                        : "never"}
                    </div>
                  </div>
                </div>

                {statusError && (
                  <div className="text-[11px] text-amber-400 italic">
                    Status endpoint unavailable: {statusError}
                  </div>
                )}

                {/* Action buttons */}
                <div className="flex items-center gap-2 flex-wrap pt-1">
                  {!status?.deployed ? (
                    <button
                      onClick={startDeploy}
                      disabled={deploying || !tokensReady}
                      title={
                        tokensReady
                          ? "Deploy this config to Modal"
                          : "Save and verify Modal tokens first"
                      }
                      className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-bold tracking-wider uppercase border border-emerald-500/60 text-emerald-300 hover:bg-emerald-500/10 disabled:opacity-40 disabled:cursor-not-allowed rounded"
                    >
                      {deploying ? (
                        <Loader2 size={13} className="animate-spin" />
                      ) : (
                        <Rocket size={13} />
                      )}
                      Deploy to Modal
                    </button>
                  ) : (
                    <>
                      <button
                        onClick={startDeploy}
                        disabled={deploying || !tokensReady}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-bold tracking-wider uppercase border border-blue-500/60 text-blue-300 hover:bg-blue-500/10 disabled:opacity-40 disabled:cursor-not-allowed rounded"
                      >
                        {deploying ? (
                          <Loader2 size={13} className="animate-spin" />
                        ) : (
                          <RefreshCw size={13} />
                        )}
                        Redeploy
                      </button>
                      {confirmDestroy ? (
                        <>
                          <span className="text-[11px] text-red-300 font-semibold uppercase tracking-wider">
                            Sure?
                          </span>
                          <button
                            onClick={destroy}
                            disabled={destroying}
                            className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-bold tracking-wider uppercase border border-red-500/60 text-red-300 hover:bg-red-500/10 disabled:opacity-40 rounded"
                          >
                            {destroying ? (
                              <Loader2 size={13} className="animate-spin" />
                            ) : (
                              <Trash2 size={13} />
                            )}
                            Destroy
                          </button>
                          <button
                            onClick={() => setConfirmDestroy(false)}
                            className="px-3 py-1.5 text-[12px] font-semibold border border-white/10 text-gray-400 hover:border-white/30 rounded"
                          >
                            Cancel
                          </button>
                        </>
                      ) : (
                        <button
                          onClick={() => setConfirmDestroy(true)}
                          className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-bold tracking-wider uppercase border border-red-500/40 text-red-300 hover:bg-red-500/10 rounded"
                        >
                          <Trash2 size={13} />
                          Destroy
                        </button>
                      )}
                    </>
                  )}
                  <button
                    onClick={refreshStatus}
                    disabled={refreshingStatus}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] text-gray-400 hover:text-white border border-white/10 hover:border-white/30 disabled:opacity-40 rounded"
                  >
                    {refreshingStatus ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : (
                      <RefreshCw size={13} />
                    )}
                    Refresh
                  </button>
                  {deploying && (
                    <button
                      onClick={cancelStream}
                      className="px-3 py-1.5 text-[12px] text-gray-400 hover:text-white border border-white/10 rounded"
                    >
                      Stop watching
                    </button>
                  )}
                </div>

                {/* SSE progress panel */}
                {(deployEvent || deployError) && (
                  <DeployProgressPanel
                    event={deployEvent}
                    error={deployError}
                  />
                )}
              </Section>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Helpers ───────────────────────────────────────────────────────────────

function Section({
  title,
  accent,
  pad,
  children,
}: {
  title: string;
  accent: "blue" | "purple" | "gray";
  pad?: boolean;
  children: React.ReactNode;
}) {
  const borderClass =
    accent === "blue"
      ? "border-blue-400/20 bg-blue-500/5"
      : accent === "purple"
        ? "border-purple-400/20 bg-purple-500/5"
        : "border-white/10 bg-[#1a1a1a]/40";
  const titleClass =
    accent === "blue"
      ? "text-blue-300"
      : accent === "purple"
        ? "text-purple-300"
        : "text-gray-300";
  return (
    <div className={`space-y-3 border rounded ${pad ? "p-3" : ""} ${borderClass}`}>
      <div
        className={`text-[12px] font-bold tracking-widest uppercase ${titleClass}`}
      >
        {title}
      </div>
      {children}
    </div>
  );
}

function TokenInput({
  label,
  storedMask,
  draft,
  onChange,
  reveal,
  setReveal,
  placeholder,
}: {
  label: string;
  storedMask: string;
  draft: string;
  onChange: (v: string) => void;
  reveal: boolean;
  setReveal: (v: boolean) => void;
  placeholder: string;
}) {
  const isStored = storedMask !== "[not set]";
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <label className="text-[11px] font-semibold text-gray-300">
          {label}
        </label>
        <span
          className={`text-[10px] font-mono ${
            isStored ? "text-emerald-300" : "text-gray-500"
          }`}
        >
          {storedMask}
        </span>
      </div>
      <div className="relative">
        <input
          type={reveal ? "text" : "password"}
          value={draft}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="w-full bg-[#1a1a1a] border border-white/10 rounded px-2 py-1 pr-8 text-[12px] text-white font-mono placeholder:text-gray-600"
        />
        <button
          type="button"
          onClick={() => setReveal(!reveal)}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-white"
        >
          {reveal ? <EyeOff size={13} /> : <Eye size={13} />}
        </button>
      </div>
    </div>
  );
}

function DeployProgressPanel({
  event,
  error,
}: {
  event: ModalDeployEvent | null;
  error: string | null;
}) {
  // Phase order — synced with backend handshake (D3).
  const PHASES: ModalDeployEvent["phase"][] = [
    "verifying_tokens",
    "building_app",
    "deploying",
    "ready",
  ];
  const phaseIdx = event ? PHASES.indexOf(event.phase) : -1;
  const isFailed = event?.phase === "failed" || !!error;
  const pct =
    event?.phase === "ready"
      ? 100
      : phaseIdx >= 0
        ? Math.round(((phaseIdx + 1) / PHASES.length) * 100)
        : 0;

  return (
    <div className="rounded border border-white/10 bg-[#1a1a1a] p-3 space-y-2">
      <div className="flex items-center justify-between text-[11px]">
        <span className="font-bold tracking-widest uppercase text-gray-400">
          Deploy progress
        </span>
        <span className="text-gray-500 font-mono">{pct}%</span>
      </div>
      <div className="h-1.5 bg-[#2a2a2a] rounded overflow-hidden">
        <div
          className={`h-full transition-[width] duration-300 ${
            isFailed
              ? "bg-red-500"
              : event?.phase === "ready"
                ? "bg-emerald-400"
                : "bg-blue-400"
          }`}
          style={{ width: `${Math.max(2, pct)}%` }}
        />
      </div>
      <div className="text-[11px] text-gray-300">
        {error ? (
          <span className="text-red-300 flex items-start gap-1">
            <AlertTriangle size={12} className="mt-0.5 shrink-0" />
            <span>
              {event?.at_phase ? `[${event.at_phase}] ` : ""}
              {error}
            </span>
          </span>
        ) : event ? (
          <span className="flex items-center gap-1">
            <span className="font-mono text-gray-500">[{event.phase}]</span>
            <span>{event.message}</span>
            {event.estimated_seconds != null &&
              event.phase !== "ready" &&
              event.phase !== "failed" && (
                <span className="text-gray-500 ml-1">
                  ~{event.estimated_seconds}s
                </span>
              )}
          </span>
        ) : (
          "Waiting…"
        )}
      </div>
      {event?.phase === "ready" && event.url && (
        <div className="text-[11px] text-emerald-300 font-mono break-all">
          {event.url}
        </div>
      )}
    </div>
  );
}
