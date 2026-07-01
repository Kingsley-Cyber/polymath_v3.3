#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/king/polymath_v3.3"
UID_NUM="$(id -u)"

echo "[daily] stopping Ghost B extractor; it is ingestion-only and memory-heavy"
launchctl bootout "gui/${UID_NUM}" "/Users/king/Library/LaunchAgents/com.polymath.ghostb-extract.plist" 2>/dev/null || true

echo "[daily] stopping Qwen reranker; start heavy mode when high-quality rerank is needed"
launchctl bootout "gui/${UID_NUM}" "/Users/king/Library/LaunchAgents/com.polymath.reranker-qwen3.plist" 2>/dev/null || true

echo "[daily] applying conservative Docker compose caps"
cd "${ROOT}"
docker compose \
  --profile mcp \
  --profile cloudflare \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.apple-mlx.yml \
  -f docker-compose.daily.yml \
  up -d --remove-orphans

echo "[daily] done"
