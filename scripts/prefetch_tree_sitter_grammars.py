"""Pre-download tree-sitter grammars used by the code lane.

The pack downloads grammars lazily on first parse (~200ms each). For
production workers — and for Docker image builds where outbound network
at parse time is undesirable — run this script once to populate the local
cache. Idempotent: already-downloaded grammars are skipped silently by
the pack.

Disk footprint: ~2 MB per grammar. The default list below is ~25 grammars
≈ 50 MB total. Memory at runtime is negligible (~200KB per loaded grammar).

Usage:
    python scripts/prefetch_tree_sitter_grammars.py
    python scripts/prefetch_tree_sitter_grammars.py luau python rust  # subset
"""

import sys
import time

DEFAULT_LANGUAGES = [
    # ─── mainstream code ──────────────────────────────────────────────
    "python", "javascript", "typescript", "tsx",
    "lua", "luau",
    "go", "rust",
    "java", "kotlin",
    "c", "cpp", "cuda",
    "swift", "objc",
    "dart",
    "csharp",
    "ruby",
    "php",
    "scala",
    "bash", "sql",
    "r",
    "haskell",
    "nix",
    # ─── shaders ──────────────────────────────────────────────────────
    "glsl", "hlsl",
    # ─── web frameworks ───────────────────────────────────────────────
    "vue", "svelte",
    # ─── markup + styling ─────────────────────────────────────────────
    "html", "css", "xml",
    # ─── data / config ────────────────────────────────────────────────
    "json", "yaml", "toml", "ini",
    # ─── IaC + build + API ────────────────────────────────────────────
    "hcl", "dockerfile", "make", "cmake",
    "proto", "graphql",
]


def main(argv: list[str]) -> int:
    try:
        import tree_sitter_language_pack as pack
    except ImportError as exc:
        print(f"ERROR: tree-sitter-language-pack not installed: {exc}", file=sys.stderr)
        return 1

    requested = argv[1:] or DEFAULT_LANGUAGES
    print(f"Pre-fetching {len(requested)} grammars to {pack.cache_dir()}\n")

    ok = 0
    failed: list[tuple[str, str]] = []
    for lang in requested:
        t0 = time.perf_counter()
        try:
            cfg = pack.ProcessConfig(language=lang, symbols=True)
            # Trivial probe forces grammar download + load.
            pack.process("x", cfg)
            dt = (time.perf_counter() - t0) * 1000
            print(f"  ok    {lang:<12}  ({dt:.0f} ms)")
            ok += 1
        except Exception as exc:
            failed.append((lang, f"{type(exc).__name__}: {exc}"))
            print(f"  FAIL  {lang:<12}  {type(exc).__name__}: {exc}")

    print()
    print(f"Cached {ok}/{len(requested)} grammars.")
    if failed:
        print(f"Failures: {len(failed)}")
        return 2

    cached = sorted(pack.downloaded_languages())
    print(f"Cache now holds {len(cached)} grammars total.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
