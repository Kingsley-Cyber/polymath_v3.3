// Phase 24 — MCP Settings tab.
//
// Surfaces the Polymath MCP server's connection details so users can paste
// ready-to-use config snippets into Claude Desktop, Cursor, or other MCP
// clients. The actual MCP_API_KEY is NEVER returned by the API — we only
// show whether one is set, and the user copies the snippet then substitutes
// their own token.
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
} from "lucide-react";
import * as api from "../../lib/api";
import type { McpInfo } from "../../lib/api";

export function McpTab() {
  const [info, setInfo] = useState<McpInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.getMcpInfo();
        if (!cancelled) setInfo(data);
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
  const claudeDesktopSnippet = JSON.stringify(
    {
      mcpServers: {
        polymath: {
          type: info.transport === "stdio" ? "stdio" : "streamable-http",
          url: `${info.url}/mcp/`,
          ...(info.require_auth && {
            headers: { Authorization: "Bearer YOUR_MCP_API_KEY" },
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
              headers: { Authorization: "Bearer YOUR_MCP_API_KEY" },
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
  ${info.require_auth ? `-H "Authorization: Bearer YOUR_MCP_API_KEY" \\\n  ` : ""}-d '{"jsonrpc":"2.0","method":"tools/list","id":1}'`;

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
        <div className="grid grid-cols-[140px_1fr] gap-x-4 gap-y-2 text-[12px]">
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
            {info.has_api_key
              ? "configured (in .env as MCP_API_KEY)"
              : "not set — using JWT auth only"}
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
            <span className="font-bold">Auth is on.</span> Replace{" "}
            <code className="font-mono text-amber-300">YOUR_MCP_API_KEY</code> in
            the snippets below with the value of{" "}
            <code className="font-mono text-amber-300">MCP_API_KEY</code> from
            your <code className="font-mono">.env</code>. To rotate it, edit{" "}
            <code className="font-mono">.env</code> and restart the backend
            container. Setting <code className="font-mono">MCP_REQUIRE_AUTH=false</code>{" "}
            disables auth (only safe for trusted local dev).
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
