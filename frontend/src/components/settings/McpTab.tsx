// Phase 24 — MCP Settings tab.
//
// Surfaces the Polymath MCP server's connection details so users can paste
// ready-to-use config snippets into Claude Desktop, Cursor, or other MCP
// clients. User-scoped MCP keys are shown once at generation time; saved keys
// are listed only by metadata/prefix.
//
// Inspired by Agent Zero's "External MCP Servers" surface, adapted to
// Polymath's stack.

import { useEffect, useState } from "react";
import {
  Plug,
  Loader2,
  AlertTriangle,
  CheckCircle,
  Copy,
  Check,
  KeyRound,
  Zap,
  Wrench,
  Trash2,
} from "lucide-react";
import * as api from "../../lib/api";
import type { McpApiKeyCreated, McpApiKeyPublic, McpInfo } from "../../lib/api";

export function McpTab() {
  const [info, setInfo] = useState<McpInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [keys, setKeys] = useState<McpApiKeyPublic[]>([]);
  const [generatedKey, setGeneratedKey] = useState<McpApiKeyCreated | null>(null);
  const [keyName, setKeyName] = useState("Desktop MCP key");
  const [generating, setGenerating] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [data, keyData] = await Promise.all([
          api.getMcpInfo(),
          api.listMcpApiKeys().catch(() => ({ keys: [] })),
        ]);
        if (!cancelled) {
          setInfo(data);
          setKeys(keyData.keys || []);
        }
      } catch (err) {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "Failed to load MCP info");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleCopy = async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied(null), 1500);
    } catch (e) {
      console.warn("clipboard write failed:", e);
    }
  };

  const handleGenerateKey = async () => {
    setGenerating(true);
    setError(null);
    try {
      const result = await api.createMcpApiKey(keyName);
      setGeneratedKey(result.key);
      const keyData = await api.listMcpApiKeys();
      setKeys(keyData.keys || []);
      const nextInfo = await api.getMcpInfo();
      setInfo(nextInfo);
      await handleCopy(result.key.api_key, "generated-key");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to generate MCP key");
    } finally {
      setGenerating(false);
    }
  };

  const handleRevokeKey = async (keyId: string) => {
    setRevoking(keyId);
    setError(null);
    try {
      await api.revokeMcpApiKey(keyId);
      const keyData = await api.listMcpApiKeys();
      setKeys(keyData.keys || []);
      const nextInfo = await api.getMcpInfo();
      setInfo(nextInfo);
      if (generatedKey?.key_id === keyId) setGeneratedKey(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to revoke MCP key");
    } finally {
      setRevoking(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-[12px] text-gray-500">
        <Loader2 className="w-4 h-4 animate-spin mr-2" />
        Loading MCP server info…
      </div>
    );
  }

  if (error || !info) {
    return (
      <div className="flex items-start gap-3 border border-red-500/30 bg-red-500/5 px-4 py-3 rounded-lg">
        <AlertTriangle className="w-4 h-4 text-red-400 mt-0.5 shrink-0" />
        <div className="text-[13px] text-red-300/80">
          {error || "MCP info unavailable. Is the backend running?"}
        </div>
      </div>
    );
  }

  // ── Connection snippet generators ───────────────────────────────────
  const authToken = generatedKey?.api_key || "YOUR_MCP_API_KEY";
  const hasUserKeys = Boolean(info.has_user_api_key || keys.length);
  const apiKeyStatus = info.has_static_api_key
    ? "static .env key configured"
    : hasUserKeys
      ? `${keys.length} user key${keys.length === 1 ? "" : "s"} active`
      : "no key yet";
  const claudeDesktopSnippet = JSON.stringify(
    {
      mcpServers: {
        polymath: {
          type: info.transport === "stdio" ? "stdio" : "streamable-http",
          url: `${info.url}/mcp/`,
          ...(info.require_auth && {
            headers: { Authorization: `Bearer ${authToken}` },
          }),
        },
      },
    },
    null,
    2,
  );

  const cursorSnippet = JSON.stringify(
    {
      mcp: {
        servers: {
          polymath: {
            transport: "streamable-http",
            url: `${info.url}/mcp/`,
            ...(info.require_auth && {
              headers: { Authorization: `Bearer ${authToken}` },
            }),
          },
        },
      },
    },
    null,
    2,
  );

  const curlSnippet = `curl -X POST "${info.url}/mcp/" \\
  -H "Content-Type: application/json" \\
  ${info.require_auth ? `-H "Authorization: Bearer ${authToken}" \\\n  ` : ""}-d '{"jsonrpc":"2.0","method":"tools/list","id":1}'`;

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold text-white mb-2">
          MCP Server (Polymath as Agent Tool)
        </h2>
        <p className="text-[13px] text-gray-500 leading-relaxed">
          Polymath exposes its RAG, skills, and tools registry as an{" "}
          <span className="text-cyan-400">MCP server</span>. Connect Claude
          Desktop, Cursor, or any MCP client to give external agents
          first-class access to your knowledge base.
        </p>
      </div>

      {/* Server status */}
      <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
        <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
          <Plug size={16} className="text-emerald-400" />
          Server Status
        </h3>
        <div className="grid grid-cols-1 sm:grid-cols-[140px_1fr] gap-x-4 gap-y-2 text-[12px]">
          <span className="text-gray-500 uppercase tracking-widest text-[10px]">
            Transport
          </span>
          <span className="text-white font-mono">{info.transport}</span>

          <span className="text-gray-500 uppercase tracking-widest text-[10px]">
            URL
          </span>
          <span className="text-white font-mono break-all">
            {info.url}
          </span>

          <span className="text-gray-500 uppercase tracking-widest text-[10px]">
            Port
          </span>
          <span className="text-white font-mono">{info.port}</span>

          <span className="text-gray-500 uppercase tracking-widest text-[10px]">
            Auth Required
          </span>
          <span
            className={
              info.require_auth ? "text-emerald-400" : "text-amber-400"
            }
          >
            {info.require_auth ? "yes" : "no (open access)"}
          </span>

          <span className="text-gray-500 uppercase tracking-widest text-[10px]">
            API Key
          </span>
          <span
            className={info.has_api_key ? "text-emerald-400" : "text-amber-400"}
          >
            {apiKeyStatus}
          </span>

          <span className="text-gray-500 uppercase tracking-widest text-[10px]">
            Default top_k
          </span>
          <span className="text-white font-mono">{info.default_top_k}</span>
        </div>
      </div>

      {/* Authentication note */}
      {info.require_auth && (
        <div className="flex items-start gap-3 border border-amber-400/30 bg-amber-400/5 px-4 py-3 rounded-lg">
          <KeyRound className="w-4 h-4 text-amber-300 mt-0.5 shrink-0" />
          <div className="text-[12px] text-amber-100/90 leading-relaxed">
            <span className="font-bold">Auth is on.</span> Generate a user-scoped
            key below, then paste it into the snippets. User keys are stored
            hashed, shown once, and work immediately without editing{" "}
            <code className="font-mono">.env</code>. Static{" "}
            <code className="font-mono text-amber-300">MCP_API_KEY</code> is still
            supported for trusted system agents.
          </div>
        </div>
      )}

      {/* User-scoped API key generator */}
      {info.require_auth && (
        <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-4">
          <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
            <KeyRound size={16} className="text-amber-300" />
            MCP API Keys
          </h3>
          <p className="text-[12px] text-gray-500 leading-relaxed">
            Generate a bearer key for Claude, Cursor, OpenClaw, or another MCP
            client. These keys are scoped to your user account and your allowed
            corpora.
          </p>

          <div className="flex flex-col sm:flex-row gap-2">
            <input
              value={keyName}
              onChange={(e) => setKeyName(e.target.value)}
              className="flex-1 bg-[#1a1a1a] border border-white/10 rounded px-3 py-2 text-[12px] text-white outline-none focus:border-cyan-400/60"
              placeholder="Key name"
            />
            <button
              type="button"
              onClick={handleGenerateKey}
              disabled={generating}
              className="inline-flex items-center justify-center gap-2 px-3 py-2 rounded bg-cyan-500/15 border border-cyan-400/30 text-cyan-100 text-[12px] hover:bg-cyan-500/25 disabled:opacity-60"
            >
              {generating ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <KeyRound className="w-3.5 h-3.5" />
              )}
              Generate key
            </button>
          </div>

          {generatedKey && (
            <div className="border border-emerald-400/25 bg-emerald-400/5 rounded-lg p-3 space-y-2">
              <div className="text-[12px] text-emerald-200 font-semibold">
                New key created. Copy it now; it will not be shown again.
              </div>
              <div className="flex items-center gap-2 bg-[#111] border border-white/10 rounded px-3 py-2">
                <code className="flex-1 min-w-0 font-mono text-[11px] text-emerald-100 break-all">
                  {generatedKey.api_key}
                </code>
                <button
                  type="button"
                  onClick={() => handleCopy(generatedKey.api_key, "generated-key")}
                  className="shrink-0 inline-flex items-center gap-1 text-[11px] text-emerald-200 hover:text-white"
                >
                  {copied === "generated-key" ? (
                    <Check className="w-3.5 h-3.5" />
                  ) : (
                    <Copy className="w-3.5 h-3.5" />
                  )}
                  {copied === "generated-key" ? "Copied" : "Copy"}
                </button>
              </div>
            </div>
          )}

          <div className="space-y-2">
            {keys.length === 0 ? (
              <div className="text-[11px] text-gray-500">
                No user-scoped MCP keys yet.
              </div>
            ) : (
              keys.map((key) => (
                <div
                  key={key.key_id}
                  className="flex items-center gap-3 px-3 py-2 bg-[#1a1a1a] border border-white/5 rounded"
                >
                  <CheckCircle className="w-4 h-4 text-emerald-400 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="text-[12px] text-white">{key.name}</div>
                    <div className="text-[11px] text-gray-500 font-mono">
                      {key.prefix}… · created {key.created_at || "unknown"}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleRevokeKey(key.key_id)}
                    disabled={revoking === key.key_id}
                    className="inline-flex items-center gap-1 text-[11px] text-red-300 hover:text-red-100 disabled:opacity-60"
                  >
                    {revoking === key.key_id ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Trash2 className="w-3.5 h-3.5" />
                    )}
                    Revoke
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* Connection snippets */}
      <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-4">
        <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
          <Zap size={16} className="text-cyan-400" />
          Connection Snippets
        </h3>

        <SnippetBlock
          title="Claude Desktop"
          subtitle={`paste into ~/.config/claude/claude_desktop_config.json`}
          code={claudeDesktopSnippet}
          copied={copied === "claude"}
          onCopy={() => handleCopy(claudeDesktopSnippet, "claude")}
        />

        <SnippetBlock
          title="Cursor"
          subtitle="add to .cursor/mcp.json"
          code={cursorSnippet}
          copied={copied === "cursor"}
          onCopy={() => handleCopy(cursorSnippet, "cursor")}
        />

        <SnippetBlock
          title="curl (smoke test)"
          subtitle="lists registered tools — sanity check the server is up"
          code={curlSnippet}
          copied={copied === "curl"}
          onCopy={() => handleCopy(curlSnippet, "curl")}
        />
      </div>

      {/* Registered tools */}
      <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-5 space-y-3">
        <h3 className="text-[15px] font-semibold text-white flex items-center gap-2">
          <Wrench size={16} className="text-purple-400" />
          Registered Tools ({info.tools.length})
        </h3>
        <p className="text-[12px] text-gray-500">
          Every tool below is callable by any MCP client connected to this
          server. All tools are corpus-scoped and respect your authenticated
          user's allowed corpus set.
        </p>
        <div className="space-y-2">
          {info.tools.map((t) => (
            <div
              key={t.name}
              className="flex items-start gap-3 px-3 py-2 bg-[#1a1a1a] border border-white/5 rounded"
            >
              <CheckCircle className="w-4 h-4 text-emerald-400 mt-0.5 shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-[12px] font-mono text-white">{t.name}</div>
                <div className="text-[11px] text-gray-500 leading-relaxed">
                  {t.description}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function SnippetBlock({
  title,
  subtitle,
  code,
  copied,
  onCopy,
}: {
  title: string;
  subtitle: string;
  code: string;
  copied: boolean;
  onCopy: () => void;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[12px] font-semibold text-white">{title}</div>
          <div className="text-[10px] text-gray-500">{subtitle}</div>
        </div>
        <button
          type="button"
          onClick={onCopy}
          className={`flex items-center gap-1.5 px-2 py-1 text-[10px] font-bold uppercase tracking-widest border transition-colors ${
            copied
              ? "border-emerald-400 text-emerald-400 bg-emerald-400/10"
              : "border-white/10 text-gray-400 hover:border-cyan-500 hover:text-cyan-400"
          }`}
        >
          {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="bg-[#1a1a1a] border border-white/5 rounded p-3 text-[11px] font-mono text-gray-300 whitespace-pre-wrap break-all overflow-x-auto">
        {code}
      </pre>
    </div>
  );
}
