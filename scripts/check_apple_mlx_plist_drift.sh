#!/usr/bin/env bash
# Read-only LaunchAgent drift check. Pass the same environment overrides used
# for installation (especially ARBITER_ENABLED) when checking a custom deploy.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${POLYMATH_DOCKER_DATA_ROOT:-${HOME}/PolymathRuntime}"
SERVICES_DIR="${RUNTIME_ROOT}/apple_ml_services"
LOG_DIR="${RUNTIME_ROOT}/logs"
LAUNCH_AGENT_NAME="com.polymath.apple-ml"
LAUNCH_AGENT_PATH="${HOME}/Library/LaunchAgents/${LAUNCH_AGENT_NAME}.plist"
APPLE_MLX_EMBED_MODEL_ID="${APPLE_MLX_EMBED_MODEL_ID:-mlx-community/Qwen3-Embedding-0.6B-mxfp8}"
APPLE_MLX_RERANKER_MODEL_ID="${APPLE_MLX_RERANKER_MODEL_ID:-mlx-community/jina-reranker-v3-4bit-mxfp4}"
APPLE_RERANKER_BACKEND="${APPLE_RERANKER_BACKEND:-torch_fp16}"
APPLE_TORCH_RERANKER_MODEL_ID="${APPLE_TORCH_RERANKER_MODEL_ID:-jinaai/jina-reranker-v3}"
EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-32}"
START_EMBEDDER="${START_EMBEDDER:-true}"
START_RERANKER="${START_RERANKER:-true}"
START_DOCLING="${START_DOCLING:-false}"
ARBITER_ENABLED="${ARBITER_ENABLED:-false}"
ARBITER_HOST="${ARBITER_HOST:-127.0.0.1}"
ARBITER_PORT="${ARBITER_PORT:-8085}"
ARBITER_ACQUIRE_TIMEOUT_SECONDS="${ARBITER_ACQUIRE_TIMEOUT_SECONDS:-30}"
ARBITER_EMBED_HOLD_TARGET_MS="${ARBITER_EMBED_HOLD_TARGET_MS:-2000}"
ARBITER_RERANK_HOLD_TARGET_MS="${ARBITER_RERANK_HOLD_TARGET_MS:-500}"
ARBITER_MAX_EMBED_BURST="${ARBITER_MAX_EMBED_BURST:-1}"
ARBITER_RERANK_STARVATION_SECONDS="${ARBITER_RERANK_STARVATION_SECONDS:-0.5}"
ARBITER_STALE_LEASE_SECONDS="${ARBITER_STALE_LEASE_SECONDS:-75}"
if [[ "${APPLE_RERANKER_BACKEND}" == "torch_fp16" ]]; then
  RERANKER_SCORE_SCALE="probability"
else
  RERANKER_SCORE_SCALE="cosine"
fi

PYTHON="${SERVICES_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi
expected="$(mktemp "${TMPDIR:-/tmp}/polymath-apple-ml-plist.XXXXXX")"
trap 'rm -f "${expected}"' EXIT

"${PYTHON}" "${REPO_ROOT}/scripts/render_apple_mlx_launch_agent.py" \
  --output "${expected}" \
  --label "${LAUNCH_AGENT_NAME}" \
  --runtime-root "${RUNTIME_ROOT}" \
  --services-dir "${SERVICES_DIR}" \
  --log-dir "${LOG_DIR}" \
  --embed-model "${APPLE_MLX_EMBED_MODEL_ID}" \
  --reranker-model "${APPLE_MLX_RERANKER_MODEL_ID}" \
  --reranker-backend "${APPLE_RERANKER_BACKEND}" \
  --torch-reranker-model "${APPLE_TORCH_RERANKER_MODEL_ID}" \
  --embed-batch-size "${EMBED_BATCH_SIZE}" \
  --start-embedder "${START_EMBEDDER}" \
  --start-reranker "${START_RERANKER}" \
  --start-docling "${START_DOCLING}" \
  --reranker-score-scale "${RERANKER_SCORE_SCALE}" \
  --arbiter-enabled "${ARBITER_ENABLED}" \
  --arbiter-host "${ARBITER_HOST}" \
  --arbiter-port "${ARBITER_PORT}" \
  --arbiter-acquire-timeout-seconds "${ARBITER_ACQUIRE_TIMEOUT_SECONDS}" \
  --arbiter-embed-hold-target-ms "${ARBITER_EMBED_HOLD_TARGET_MS}" \
  --arbiter-rerank-hold-target-ms "${ARBITER_RERANK_HOLD_TARGET_MS}" \
  --arbiter-max-embed-burst "${ARBITER_MAX_EMBED_BURST}" \
  --arbiter-rerank-starvation-seconds "${ARBITER_RERANK_STARVATION_SECONDS}" \
  --arbiter-stale-lease-seconds "${ARBITER_STALE_LEASE_SECONDS}"

plutil -lint "${expected}" >/dev/null
if [[ ! -f "${LAUNCH_AGENT_PATH}" ]]; then
  echo "ERROR: LaunchAgent missing: ${LAUNCH_AGENT_PATH}" >&2
  exit 1
fi
if ! cmp -s "${expected}" "${LAUNCH_AGENT_PATH}"; then
  echo "ERROR: LaunchAgent plist drift detected: ${LAUNCH_AGENT_PATH}" >&2
  exit 1
fi
echo "[apple-mlx] plist drift check: clean (${LAUNCH_AGENT_PATH})"
