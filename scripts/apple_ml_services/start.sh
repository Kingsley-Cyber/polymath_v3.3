#!/usr/bin/env bash
# Starts the three host-native MLX sidecars on Apple Silicon.
# Invoked by the LaunchAgent (com.polymath.apple-ml).
#
# Tunables (override via env or the LaunchAgent plist):
#   EMBEDDER_HOST / EMBEDDER_PORT     default 0.0.0.0 / 8082
#   RERANKER_HOST / RERANKER_PORT     default 0.0.0.0 / 8081
#   DOCLING_HOST  / DOCLING_PORT      default 0.0.0.0 / 8500
#   EMBED_BATCH_SIZE                  default 8   (unified-memory friendly)
#   EMBED_MAX_LENGTH                  default 512
#   RERANKER_BATCH_SIZE               default 16
#   RERANKER_MAX_DOC_CHARS            default 6000
#   RERANKER_MAX_QUERY_CHARS          default 2000

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${HOME}/PolymathRuntime/logs"
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

export EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-8}"
export EMBED_MAX_LENGTH="${EMBED_MAX_LENGTH:-512}"
export RERANKER_BATCH_SIZE="${RERANKER_BATCH_SIZE:-16}"
export RERANKER_MAX_DOC_CHARS="${RERANKER_MAX_DOC_CHARS:-6000}"
export RERANKER_MAX_QUERY_CHARS="${RERANKER_MAX_QUERY_CHARS:-2000}"

start_service() {
  local name="$1" module="$2" host_var="$3" port_var="$4"
  local host="${!host_var}" port="${!port_var}"
  echo "[apple-ml] starting ${name} on ${host}:${port}"
  ${PY} -m uvicorn "${module}:app" \
    --host "${host}" --port "${port}" \
    --log-level info \
    >> "${LOG_DIR}/apple_ml_services.log" 2>> "${LOG_DIR}/apple_ml_services.err.log" &
  echo $! > "${LOG_DIR}/${name}.pid"
}

cd "${PROJECT_ROOT}"

start_service "embedder" "embedder_mlx.main" EMBEDDER_HOST EMBEDDER_PORT
start_service "reranker" "reranker_mlx.main" RERANKER_HOST RERANKER_PORT
start_service "docling"  "docling_svc.main"  DOCLING_HOST  DOCLING_PORT

# launchd's KeepAlive needs a foreground process to monitor. We can't
# use `wait -n` here — that's bash 4.3+, and the macOS-shipped /bin/bash
# is still 3.2.x. The LaunchAgent points at /bin/bash so even with
# Homebrew bash 5 installed, the plist won't pick it up. Poll the
# child PIDs at 5s cadence; first one to die kills the supervisor and
# launchd restarts the whole group. Compatible with macOS system bash.
PID_FILES=(
  "${LOG_DIR}/embedder.pid"
  "${LOG_DIR}/reranker.pid"
  "${LOG_DIR}/docling.pid"
)
trap 'echo "[apple-ml] supervisor signalled, exiting"; exit 0' INT TERM
while true; do
  for pid_file in "${PID_FILES[@]}"; do
    [[ -f "${pid_file}" ]] || continue
    pid="$(cat "${pid_file}" 2>/dev/null || echo)"
    if [[ -z "${pid}" ]]; then
      continue
    fi
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "[apple-ml] sidecar pid=${pid} (${pid_file##*/}) exited; bubbling up to launchd"
      exit 1
    fi
  done
  sleep 5
done
