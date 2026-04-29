// LoginView.tsx — Brutalist terminal-inspired authentication screen
// Obsidian Protocol aesthetic: scanlines, monospace prompts, geometric precision

import { useState, useEffect, useCallback, FormEvent } from "react";
import { Lock, Terminal, AlertTriangle, ChevronRight } from "lucide-react";
import { useAuthStore } from "../../stores/authStore";
import * as api from "../../lib/api";

export function LoginView() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);
  const [showCursor, setShowCursor] = useState(true);

  const { setAuth, setError } = useAuthStore();

  // Mount animation trigger
  useEffect(() => {
    const timer = setTimeout(() => setMounted(true), 50);
    return () => clearTimeout(timer);
  }, []);

  // Blinking cursor effect
  useEffect(() => {
    const interval = setInterval(() => setShowCursor((prev) => !prev), 530);
    return () => clearInterval(interval);
  }, []);

  const handleSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setLoginError(null);

      if (!username.trim() || !password.trim()) {
        setLoginError("ALL FIELDS REQUIRED // SUPPLY CREDENTIALS");
        return;
      }

      setIsSubmitting(true);

      try {
        const response = await api.login({
          username: username.trim(),
          password,
        });

        setAuth(response.access_token, response.user);
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "AUTHENTICATION FAILED";
        setLoginError(
          message.includes("401")
            ? "ACCESS DENIED // INVALID CREDENTIALS"
            : `SYSTEM ERROR: ${message}`,
        );
        setError(message);
      } finally {
        setIsSubmitting(false);
      }
    },
    [username, password, setAuth, setError],
  );

  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center bg-[var(--bg-base)] overflow-hidden">
      {/* Background Grid Pattern */}
      <div
        className="absolute inset-0 opacity-[0.03]"
        style={{
          backgroundImage: `
            linear-gradient(var(--accent-primary) 1px, transparent 1px),
            linear-gradient(90deg, var(--accent-primary) 1px, transparent 1px)
          `,
          backgroundSize: "40px 40px",
        }}
      />

      {/* Animated Scanline */}
      <div className="absolute top-0 left-0 w-full h-[2px] bg-[var(--accent-primary)] opacity-40 animate-scanline-vertical" />

      {/* Radial Glow */}
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,_var(--accent-primary)_0%,_transparent_70%)] opacity-[0.03]" />

      {/* ── Login Card ── */}
      <div
        className={`relative w-full max-w-[420px] mx-4 transition-all duration-300 ease-out ${
          mounted ? "opacity-100 translate-y-0" : "opacity-0 translate-y-4"
        }`}
      >
        {/* Top Accent Line */}
        <div className="h-[1px] w-full bg-[var(--accent-primary)] mb-0" />

        {/* Card Body */}
        <div className="border-x border-[var(--border-subtle)] bg-[var(--bg-surface)] px-8 py-10">
          {/* ── Header ── */}
          <div className="mb-10">
            {/* System Status */}
            <div className="flex items-center gap-2 mb-6">
              <div className="w-1.5 h-1.5 bg-[var(--accent-primary)] animate-pulse" />
              <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-[var(--text-secondary)]">
                Polymath v3.3 // Secure Terminal
              </span>
            </div>

            {/* Title */}
            <h1 className="text-2xl font-bold tracking-tight text-[var(--text-primary)] mb-2 flex items-center gap-3">
              <Lock className="w-5 h-5 text-[var(--accent-primary)]" />
              AUTH_REQUIRED
            </h1>

            <p className="text-[11px] tracking-widest uppercase text-[var(--text-tertiary)]">
              Supply credentials to initialize session
            </p>
          </div>

          {/* ── Error Display ── */}
          {loginError && (
            <div className="mb-6 border border-red-500/30 bg-red-500/5 px-4 py-3 flex items-start gap-3 animate-fade-in">
              <AlertTriangle className="w-4 h-4 text-red-400 mt-0.5 shrink-0" />
              <div>
                <p className="text-[11px] font-bold uppercase tracking-widest text-red-400">
                  Authentication Error
                </p>
                <p className="text-xs text-red-300/80 mt-1 font-mono">
                  {loginError}
                </p>
              </div>
            </div>
          )}

          {/* ── Form ── */}
          <form onSubmit={handleSubmit} className="space-y-6">
            {/* Username Field */}
            <div>
              <label className="block text-[10px] font-bold uppercase tracking-[0.2em] text-[var(--text-secondary)] mb-2">
                <Terminal className="w-3 h-3 inline mr-1.5" />
                Username
              </label>
              <div className="relative">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-[11px] text-[var(--text-tertiary)] font-mono select-none">
                  user@
                </span>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username"
                  autoFocus
                  spellCheck={false}
                  className="w-full bg-[var(--bg-base)] border border-[var(--border-subtle)] py-2.5 pl-14 pr-3 text-sm text-[var(--text-primary)] font-mono placeholder:text-[var(--text-tertiary)] focus:outline-none focus:border-[var(--accent-primary)] transition-colors"
                  placeholder="admin"
                />
              </div>
            </div>

            {/* Password Field */}
            <div>
              <label className="block text-[10px] font-bold uppercase tracking-[0.2em] text-[var(--text-secondary)] mb-2">
                <Lock className="w-3 h-3 inline mr-1.5" />
                Password
              </label>
              <div className="relative">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-[11px] text-[var(--text-tertiary)] font-mono select-none">
                  pass:
                </span>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  spellCheck={false}
                  className="w-full bg-[var(--bg-base)] border border-[var(--border-subtle)] py-2.5 pl-14 pr-3 text-sm text-[var(--text-primary)] font-mono placeholder:text-[var(--text-tertiary)] focus:outline-none focus:border-[var(--accent-primary)] transition-colors"
                  placeholder="••••••••"
                />
              </div>
            </div>

            {/* Submit Button */}
            <button
              type="submit"
              disabled={isSubmitting}
              className={`w-full relative group flex items-center justify-center gap-2 py-3 text-xs font-bold uppercase tracking-[0.25em] border transition-all duration-200 ${
                isSubmitting
                  ? "border-[var(--accent-primary)]/50 text-[var(--accent-primary)]/50 cursor-wait"
                  : "border-[var(--accent-primary)] text-[var(--accent-primary)] hover:bg-[var(--accent-primary)] hover:text-[var(--bg-base)] cursor-pointer"
              }`}
            >
              {/* Scanline pulse on button */}
              {isSubmitting && (
                <div className="absolute top-0 left-0 h-full w-full overflow-hidden">
                  <div className="h-[1px] w-full bg-[var(--accent-primary)] animate-pulse" />
                </div>
              )}

              {isSubmitting ? (
                <>
                  <span className="animate-pulse">AUTHENTICATING</span>
                  <span className="inline-block w-1.5 h-3 bg-[var(--accent-primary)]/50 animate-cursor-blink" />
                </>
              ) : (
                <>
                  <span>EXECUTE LOGIN</span>
                  <ChevronRight className="w-3.5 h-3.5 transition-transform group-hover:translate-x-1" />
                </>
              )}
            </button>
          </form>

          {/* ── Footer Info ── */}
          <div className="mt-8 pt-6 border-t border-[var(--border-subtle)]">
            <p className="text-[10px] text-[var(--text-tertiary)] font-mono leading-relaxed">
              <span className="text-[var(--accent-primary)]">$</span> Default
              credentials configured via .env
              <br />
              <span className="text-[var(--accent-primary)]">$</span> Token
              expires after 7 days
              <br />
              <span className="text-[var(--text-tertiary)]">
                {showCursor ? "█" : " "}
              </span>
            </p>
          </div>
        </div>

        {/* Bottom Accent Line */}
        <div className="h-[1px] w-full bg-[var(--accent-primary)] mt-0" />

        {/* Version Tag */}
        <div className="mt-4 text-center">
          <span className="text-[9px] font-bold uppercase tracking-[0.4em] text-[var(--text-tertiary)]">
            kingsleylab.xyz // secure gateway
          </span>
        </div>
      </div>
    </div>
  );
}
