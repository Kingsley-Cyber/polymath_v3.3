#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/king/polymath_v3.3"
UID_NUM="$(id -u)"

echo "[heavy] starting Qwen reranker"
launchctl bootstrap "gui/${UID_NUM}" "/Users/king/Library/LaunchAgents/com.polymath.reranker-qwen3.plist" 2>/dev/null || true
launchctl kickstart -k "gui/${UID_NUM}/com.polymath.reranker-qwen3"

echo "[heavy] starting Ghost B extractor; it will exit after 1h idle"
launchctl bootstrap "gui/${UID_NUM}" "/Users/king/Library/LaunchAgents/com.polymath.ghostb-extract.plist" 2>/dev/null || true
launchctl kickstart -k "gui/${UID_NUM}/com.polymath.ghostb-extract"

echo "[heavy] applying high-throughput Docker compose caps"
cd "${ROOT}"
docker compose \
  --profile mcp \
  --profile cloudflare \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.apple-mlx.yml \
  -f docker-compose.heavy-ingest.yml \
  up -d --remove-orphans

echo "[heavy] done"
