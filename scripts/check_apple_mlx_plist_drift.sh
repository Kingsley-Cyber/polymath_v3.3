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
EMBEDDER_MODEL_NAME="${EMBEDDER_MODEL_NAME:-Qwen3-Embedding-0.6B}"
EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-32}"
EMBED_MAX_LENGTH="${EMBED_MAX_LENGTH:-512}"
EMBEDDER_REQUEST_TIMEOUT_SECONDS="${EMBEDDER_REQUEST_TIMEOUT_SECONDS:-60}"
EMBEDDER_QUEUE_TIMEOUT_SECONDS="${EMBEDDER_QUEUE_TIMEOUT_SECONDS:-30}"
EMBEDDER_WARMUP_TIMEOUT_SECONDS="${EMBEDDER_WARMUP_TIMEOUT_SECONDS:-30}"
MLX_CACHE_LIMIT_GB="${MLX_CACHE_LIMIT_GB:-1.0}"
RERANKER_CAL_MU="${RERANKER_CAL_MU:-0.2}"
RERANKER_CAL_T="${RERANKER_CAL_T:-0.12}"
RERANKER_CAL_VERSION="${RERANKER_CAL_VERSION:-cal.v1-provisional}"
RERANKER_BATCH_SIZE="${RERANKER_BATCH_SIZE:-16}"
RERANKER_MAX_DOC_CHARS="${RERANKER_MAX_DOC_CHARS:-6000}"
RERANKER_MAX_QUERY_CHARS="${RERANKER_MAX_QUERY_CHARS:-2000}"
RERANKER_REQUEST_TIMEOUT_SECONDS="${RERANKER_REQUEST_TIMEOUT_SECONDS:-60}"
RERANKER_QUEUE_TIMEOUT_SECONDS="${RERANKER_QUEUE_TIMEOUT_SECONDS:-5}"
RERANKER_WARM_ON_STARTUP="${RERANKER_WARM_ON_STARTUP:-true}"
RERANKER_WARMUP_CANDIDATE_SHAPES="${RERANKER_WARMUP_CANDIDATE_SHAPES:-16,24}"
RERANKER_WARMUP_CANDIDATES="${RERANKER_WARMUP_CANDIDATES:-16}"
RERANKER_WARMUP_DOC_CHARS="${RERANKER_WARMUP_DOC_CHARS:-768}"
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
  --embedder-model-name "${EMBEDDER_MODEL_NAME}" \
  --embed-batch-size "${EMBED_BATCH_SIZE}" \
  --embed-max-length "${EMBED_MAX_LENGTH}" \
  --embedder-request-timeout-seconds "${EMBEDDER_REQUEST_TIMEOUT_SECONDS}" \
  --embedder-queue-timeout-seconds "${EMBEDDER_QUEUE_TIMEOUT_SECONDS}" \
  --embedder-warmup-timeout-seconds "${EMBEDDER_WARMUP_TIMEOUT_SECONDS}" \
  --mlx-cache-limit-gb "${MLX_CACHE_LIMIT_GB}" \
  --reranker-cal-mu "${RERANKER_CAL_MU}" \
  --reranker-cal-t "${RERANKER_CAL_T}" \
  --reranker-cal-version "${RERANKER_CAL_VERSION}" \
  --reranker-batch-size "${RERANKER_BATCH_SIZE}" \
  --reranker-max-doc-chars "${RERANKER_MAX_DOC_CHARS}" \
  --reranker-max-query-chars "${RERANKER_MAX_QUERY_CHARS}" \
  --reranker-request-timeout-seconds "${RERANKER_REQUEST_TIMEOUT_SECONDS}" \
  --reranker-queue-timeout-seconds "${RERANKER_QUEUE_TIMEOUT_SECONDS}" \
  --reranker-warm-on-startup "${RERANKER_WARM_ON_STARTUP}" \
  --reranker-warmup-candidate-shapes "${RERANKER_WARMUP_CANDIDATE_SHAPES}" \
  --reranker-warmup-candidates "${RERANKER_WARMUP_CANDIDATES}" \
  --reranker-warmup-doc-chars "${RERANKER_WARMUP_DOC_CHARS}" \
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
