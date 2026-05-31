#!/usr/bin/env bash
# Polymath 3.3 — Apple Silicon MLX runtime installer.
#
# What this does (idempotent):
#   1. Verifies host is Darwin/arm64.
#   2. Stages scripts/apple_ml_services/ into ~/PolymathRuntime/apple_ml_services/
#   3. Creates a uv-managed venv with requirements.txt
#   4. Pre-warms and verifies the HuggingFace cache with the MLX model weights
#   5. Writes a LaunchAgent (com.polymath.apple-ml) and bootstraps it
#   6. Smoke-tests embeddings, reranking, and docling health
#
# Usage (run from repo root on macOS):
#   bash scripts/install_apple_mlx_runtime.sh
#
# Re-run any time. Safe to interrupt; rerun resumes.

set -euo pipefail

# ── 1. Platform gate ─────────────────────────────────────────────────
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: install_apple_mlx_runtime.sh runs on macOS only (uname=$(uname -s))." >&2
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "ERROR: Apple Silicon required (uname -m=$(uname -m))." >&2
  echo "       MLX requires an arm64 Mac. Use the standard docker-compose.yml on Intel." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${POLYMATH_DOCKER_DATA_ROOT:-${HOME}/PolymathRuntime}"
SERVICES_DIR="${RUNTIME_ROOT}/apple_ml_services"
LOG_DIR="${RUNTIME_ROOT}/logs"
LAUNCH_AGENT_NAME="com.polymath.apple-ml"
LAUNCH_AGENT_PATH="${HOME}/Library/LaunchAgents/${LAUNCH_AGENT_NAME}.plist"
APPLE_MLX_EMBED_MODEL_ID="${APPLE_MLX_EMBED_MODEL_ID:-mlx-community/Qwen3-Embedding-0.6B-mxfp8}"
APPLE_MLX_RERANKER_MODEL_ID="${APPLE_MLX_RERANKER_MODEL_ID:-mlx-community/jina-reranker-v3-4bit-mxfp4}"
EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-32}"

echo "[apple-mlx] runtime root : ${RUNTIME_ROOT}"
echo "[apple-mlx] services     : ${SERVICES_DIR}"
echo "[apple-mlx] launch agent : ${LAUNCH_AGENT_PATH}"
echo "[apple-mlx] embed batch  : ${EMBED_BATCH_SIZE}"

mkdir -p "${SERVICES_DIR}" "${LOG_DIR}" "${RUNTIME_ROOT}/models" "${RUNTIME_ROOT}/volumes/hf-cache"

# ── 2. Stage code ────────────────────────────────────────────────────
EXCLUDES=(
  '.venv'
  '__pycache__'
  '*.pyc'
)
SIDECAR_RELS=(
  'embedder_mlx/main.py'
  'reranker_mlx/main.py'
  'docling_svc/main.py'
)
if [[ "${POLYMATH_APPLE_MLX_PRESERVE_HOST:-0}" == "1" ]]; then
  for rel in "${SIDECAR_RELS[@]}"; do
    if [[ -f "${SERVICES_DIR}/${rel}" ]]; then
      EXCLUDES+=("${rel}")
      echo "[apple-mlx] preserving host file: ${rel}"
    fi
  done
else
  backup_dir="${LOG_DIR}/apple_ml_services_backups/$(date +%Y%m%d-%H%M%S)"
  for rel in "${SIDECAR_RELS[@]}"; do
    if [[ -f "${SERVICES_DIR}/${rel}" ]] && ! cmp -s "${REPO_ROOT}/scripts/apple_ml_services/${rel}" "${SERVICES_DIR}/${rel}"; then
      mkdir -p "${backup_dir}/$(dirname "${rel}")"
      cp "${SERVICES_DIR}/${rel}" "${backup_dir}/${rel}"
      echo "[apple-mlx] backed up prior host file: ${backup_dir}/${rel}"
    fi
  done
fi

echo "[apple-mlx] syncing source from repo → runtime"
RSYNC_EXCLUDES=()
for ex in "${EXCLUDES[@]}"; do
  RSYNC_EXCLUDES+=(--exclude="${ex}")
done
rsync -a --delete "${RSYNC_EXCLUDES[@]}" \
  "${REPO_ROOT}/scripts/apple_ml_services/" "${SERVICES_DIR}/"
chmod +x "${SERVICES_DIR}/start.sh" || true

# ── 3. Provision venv ────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  echo "[apple-mlx] installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

cd "${SERVICES_DIR}"
echo "[apple-mlx] creating venv + installing requirements"
uv venv .venv --python 3.11
# shellcheck disable=SC1091
source .venv/bin/activate
uv pip install --upgrade pip
uv pip install -r requirements.txt
deactivate

# ── 4. Pre-warm HuggingFace model cache ──────────────────────────────
echo "[apple-mlx] pre-pulling MLX model weights (this can take a while on first run)"
APPLE_MLX_EMBED_MODEL_ID="${APPLE_MLX_EMBED_MODEL_ID}" \
APPLE_MLX_RERANKER_MODEL_ID="${APPLE_MLX_RERANKER_MODEL_ID}" \
HF_HOME="${RUNTIME_ROOT}/volumes/hf-cache" \
HF_HUB_CACHE="${RUNTIME_ROOT}/volumes/hf-cache/hub" \
"${SERVICES_DIR}/.venv/bin/python" "${REPO_ROOT}/scripts/pull_apple_mlx_models.py"

APPLE_MLX_EMBED_MODEL_ID="${APPLE_MLX_EMBED_MODEL_ID}" \
APPLE_MLX_RERANKER_MODEL_ID="${APPLE_MLX_RERANKER_MODEL_ID}" \
HF_HOME="${RUNTIME_ROOT}/volumes/hf-cache" \
HF_HUB_CACHE="${RUNTIME_ROOT}/volumes/hf-cache/hub" \
"${SERVICES_DIR}/.venv/bin/python" "${REPO_ROOT}/scripts/pull_apple_mlx_models.py" --check-only

# ── 5. LaunchAgent ───────────────────────────────────────────────────
mkdir -p "${HOME}/Library/LaunchAgents"

# Stop any prior instance before rewriting the plist.
launchctl bootout "gui/$(id -u)/${LAUNCH_AGENT_NAME}" 2>/dev/null || true

cat > "${LAUNCH_AGENT_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LAUNCH_AGENT_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SERVICES_DIR}/start.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SERVICES_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HF_HOME</key>
        <string>${RUNTIME_ROOT}/volumes/hf-cache</string>
        <key>HF_HUB_CACHE</key>
        <string>${RUNTIME_ROOT}/volumes/hf-cache/hub</string>
        <key>POLYMATH_DOCKER_DATA_ROOT</key>
        <string>${RUNTIME_ROOT}</string>
        <key>APPLE_MLX_EMBED_MODEL_ID</key>
        <string>${APPLE_MLX_EMBED_MODEL_ID}</string>
        <key>APPLE_MLX_RERANKER_MODEL_ID</key>
        <string>${APPLE_MLX_RERANKER_MODEL_ID}</string>
        <key>EMBED_BATCH_SIZE</key>
        <string>${EMBED_BATCH_SIZE}</string>
        <key>RERANKER_SCORE_SCALE</key>
        <string>cosine</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/apple_ml_services.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/apple_ml_services.err.log</string>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENT_PATH}"
launchctl kickstart -k "gui/$(id -u)/${LAUNCH_AGENT_NAME}"

# ── 6. Smoke ─────────────────────────────────────────────────────────
echo "[apple-mlx] waiting up to 90s for sidecars to come up"
"${SERVICES_DIR}/.venv/bin/python" "${REPO_ROOT}/scripts/verify_apple_mlx_runtime.py" --wait 90

echo
echo "[apple-mlx] installed. Now bring up Docker with the override:"
echo "  docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build"
echo
echo "Logs : ${LOG_DIR}/apple_ml_services.log"
echo "Stop : launchctl bootout gui/\$(id -u)/${LAUNCH_AGENT_NAME}"
echo "Kick : launchctl kickstart -k gui/\$(id -u)/${LAUNCH_AGENT_NAME}"
