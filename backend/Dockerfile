# backend/Dockerfile
# Polymath RAG v3.3 - FastAPI Backend
# Python 3.11 with async support

# Obscura has no arm64 manifest. On Apple Silicon, skip the optional JS-render
# binary and let live web fetching fall back to the native HTML path.
# FROM h4ckf0r0day/obscura:latest AS obscura

FROM python:3.11-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output.
# XDG_CACHE_HOME points the tree-sitter-language-pack rust binding (and any
# other XDG-respecting tool) at /app/.cache, which is chowned to appuser
# below. Without this, the pre-warm step would cache under /root/.cache
# at build time, then the appuser runtime would EACCES on its own
# ~/.cache and silently fall back to byte-chunking every code chunk —
# leaving symbols_defined empty for Phase 4/5's entity promotion.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/app/.cache

# Install system dependencies required by some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Optional JS-rendered page fetcher for the live web lane.
# Disabled on arm64; set OBSCURA_COMMAND empty so native HTML fetch is used.
# COPY --from=obscura /obscura /usr/local/bin/obscura
# COPY --from=obscura /obscura-worker /usr/local/bin/obscura-worker

# Pt 11 — pre-warm tree-sitter grammars used by the code lane so the first
# ingest in a fresh container doesn't pay the per-language download cost
# (~200ms × N grammars, ~62 MB cache). Mirrors scripts/prefetch_tree_sitter_
# grammars.py at project root but inlined here because that script lives
# outside the backend/ build context. Soft-fail if graphifyy is missing
# — the build continues; the augmenter degrades to no-op at runtime.
RUN python -c "\
import tree_sitter_language_pack as p; \
langs = ['python','javascript','typescript','tsx','lua','luau','go','rust', \
        'java','kotlin','c','cpp','cuda','swift','objc','dart','csharp', \
        'ruby','php','scala','bash','sql','r','haskell','nix', \
        'glsl','hlsl','vue','svelte','html','css','xml', \
        'json','yaml','toml','ini','hcl','dockerfile','make','cmake', \
        'proto','graphql']; \
[p.process('x', p.ProcessConfig(language=l)) for l in langs]; \
print(f'tree-sitter pack: pre-warmed {len(langs)} grammars')" \
    || echo "tree-sitter pack pre-warm skipped (code lane will download on first use)"

# Copy application code
COPY . .

# Create non-root user for security. chown /home/appuser too so any tool
# that ignores XDG_CACHE_HOME and writes to $HOME/.cache directly still
# works at runtime (huggingface_hub, transformers, etc.).
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser && \
    mkdir -p /home/appuser/.cache /app/.cache && \
    chown -R appuser:appuser /app /home/appuser

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
