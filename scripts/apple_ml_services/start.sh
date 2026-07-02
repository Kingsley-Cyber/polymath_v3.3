#!/usr/bin/env bash
# Starts host-native MLX sidecars on Apple Silicon.
# Invoked by the LaunchAgent (com.polymath.apple-ml).
#
# Tunables (override via env or the LaunchAgent plist):
#   APPLE_MLX_EMBED_MODEL_ID          default mlx-community/Qwen3-Embedding-0.6B-mxfp8
#   APPLE_MLX_RERANKER_MODEL_ID       default mlx-community/jina-reranker-v3-4bit-mxfp4
#   EMBEDDER_HOST / EMBEDDER_PORT     default 0.0.0.0 / 8082
#   RERANKER_HOST / RERANKER_PORT     default 0.0.0.0 / 8081
#   DOCLING_HOST  / DOCLING_PORT      default 0.0.0.0 / 8500
#   START_EMBEDDER                    default true
#   START_RERANKER                    default false
#   START_DOCLING                     default false
#   EMBED_BATCH_SIZE                  default 32  (M-series Studio friendly; lower if memory pressure appears)
#   EMBED_MAX_LENGTH                  default 512
#   RERANKER_BATCH_SIZE               default 16
#   RERANKER_MAX_DOC_CHARS            default 6000
#   RERANKER_MAX_QUERY_CHARS          default 2000

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="${POLYMATH_DOCKER_DATA_ROOT:-${HOME}/PolymathRuntime}"
LOG_DIR="${RUNTIME_ROOT}/logs"
mkdir -p "${LOG_DIR}"

# Prefer the venv installed by install_apple_mlx_runtime.sh
if [[ -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
  PY="${PROJECT_ROOT}/.venv/bin/python"
elif command -v uv >/dev/null 2>&1; then
  PY="uv run python"
else
  PY="python3"
fi

export EMBEDDER_HOST="${EMBEDDER_HOST:-0.0.0.0}"
export EMBEDDER_PORT="${EMBEDDER_PORT:-8082}"
export RERANKER_HOST="${RERANKER_HOST:-0.0.0.0}"
export RERANKER_PORT="${RERANKER_PORT:-8081}"
export DOCLING_HOST="${DOCLING_HOST:-0.0.0.0}"
export DOCLING_PORT="${DOCLING_PORT:-8500}"
export START_EMBEDDER="${START_EMBEDDER:-true}"
export START_RERANKER="${START_RERANKER:-false}"
export START_DOCLING="${START_DOCLING:-false}"
export HF_HOME="${HF_HOME:-${RUNTIME_ROOT}/volumes/hf-cache}"
export HF_HUB_CACHE="${HF_HUB_CACHE:-${HF_HOME}/hub}"
export DOCLING_ARTIFACTS_PATH="${DOCLING_ARTIFACTS_PATH:-${RUNTIME_ROOT}/volumes/docling/models}"

export APPLE_MLX_EMBED_MODEL_ID="${APPLE_MLX_EMBED_MODEL_ID:-mlx-community/Qwen3-Embedding-0.6B-mxfp8}"
export APPLE_MLX_RERANKER_MODEL_ID="${APPLE_MLX_RERANKER_MODEL_ID:-mlx-community/jina-reranker-v3-4bit-mxfp4}"
# Retrieval Layer v4 (2026-07-02): the mlx backend was a bi-encoder cosine
# pass, never a cross-encoder. torch_fp16 runs jina-reranker-v3's TRUE
# listwise cross-encoder head (fp16 on MPS) with calibrated [0,1] scores.
# Rollback: APPLE_RERANKER_BACKEND=mlx
export APPLE_RERANKER_BACKEND="${APPLE_RERANKER_BACKEND:-torch_fp16}"
export APPLE_TORCH_RERANKER_MODEL_ID="${APPLE_TORCH_RERANKER_MODEL_ID:-jinaai/jina-reranker-v3}"
export EMBEDDER_MODEL_NAME="${EMBEDDER_MODEL_NAME:-Qwen3-Embedding-0.6B}"
export RERANKER_SCORE_SCALE="${RERANKER_SCORE_SCALE:-cosine}"

export EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-${LOCAL_EMBED_BATCH_SIZE:-32}}"
export EMBED_MAX_LENGTH="${EMBED_MAX_LENGTH:-512}"
export RERANKER_BATCH_SIZE="${RERANKER_BATCH_SIZE:-16}"
export RERANKER_MAX_DOC_CHARS="${RERANKER_MAX_DOC_CHARS:-6000}"
export RERANKER_MAX_QUERY_CHARS="${RERANKER_MAX_QUERY_CHARS:-2000}"

PID_FILES=()

should_start() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

start_service() {
  local name="$1" module="$2" host_var="$3" port_var="$4"
  local host="${!host_var}" port="${!port_var}"
  echo "[apple-ml] starting ${name} on ${host}:${port}"
  ${PY} -m uvicorn "${module}:app" \
    --host "${host}" --port "${port}" \
    --log-level info \
    >> "${LOG_DIR}/apple_ml_services.log" 2>> "${LOG_DIR}/apple_ml_services.err.log" &
  local pid=$!
  echo "${pid}" > "${LOG_DIR}/${name}.pid"
  PID_FILES+=("${LOG_DIR}/${name}.pid")
}

skip_service() {
  local name="$1"
  echo "[apple-ml] skipping ${name}"
  rm -f "${LOG_DIR}/${name}.pid"
}

cd "${PROJECT_ROOT}"

if should_start "${START_EMBEDDER}"; then
  start_service "embedder" "embedder_mlx.main" EMBEDDER_HOST EMBEDDER_PORT
else
  skip_service "embedder"
fi

if should_start "${START_RERANKER}"; then
  start_service "reranker" "reranker_mlx.main" RERANKER_HOST RERANKER_PORT
else
  skip_service "reranker"
fi

if should_start "${START_DOCLING}"; then
  start_service "docling"  "docling_svc.main"  DOCLING_HOST  DOCLING_PORT
else
  skip_service "docling"
fi

if [[ "${#PID_FILES[@]}" -eq 0 ]]; then
  echo "[apple-ml] no sidecars enabled; exiting"
  exit 1
fi

# launchd's KeepAlive needs a foreground process to monitor. We can't
# use `wait -n` here — that's bash 4.3+, and the macOS-shipped /bin/bash
# is still 3.2.x. The LaunchAgent points at /bin/bash so even with
# Homebrew bash 5 installed, the plist won't pick it up. Poll the
# enabled child PIDs at 5s cadence; first one to die kills the supervisor
# and launchd restarts the enabled group. Compatible with macOS system bash.

stop_children() {
  for pid_file in "${PID_FILES[@]}"; do
    [[ -f "${pid_file}" ]] || continue
    pid="$(cat "${pid_file}" 2>/dev/null || echo)"
    [[ -n "${pid}" ]] || continue
    kill "${pid}" 2>/dev/null || true
  done
}

trap 'echo "[apple-ml] supervisor signalled, stopping sidecars"; stop_children; exit 0' INT TERM
while true; do
  for pid_file in "${PID_FILES[@]}"; do
    [[ -f "${pid_file}" ]] || continue
    pid="$(cat "${pid_file}" 2>/dev/null || echo)"
    if [[ -z "${pid}" ]]; then
      continue
    fi
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "[apple-ml] sidecar pid=${pid} (${pid_file##*/}) exited; bubbling up to launchd"
      stop_children
      exit 1
    fi
  done
  sleep 5
done
