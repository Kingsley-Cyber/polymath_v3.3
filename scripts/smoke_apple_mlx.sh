#!/usr/bin/env bash
# End-to-end smoke for the Apple Silicon MLX hybrid profile.
# Run after install_apple_mlx_runtime.sh + docker compose up.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PY="${PYTHON:-python3}"
if [[ -x "${POLYMATH_DOCKER_DATA_ROOT:-${HOME}/PolymathRuntime}/apple_ml_services/.venv/bin/python" ]]; then
  PY="${POLYMATH_DOCKER_DATA_ROOT:-${HOME}/PolymathRuntime}/apple_ml_services/.venv/bin/python"
fi

should_start() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

VERIFY_ARGS=(
  --embedder-url "${EMBEDDER_URL:-http://localhost:8082}"
  --reranker-url "${RERANKER_URL:-http://localhost:8081}"
  --docling-url "${DOCLING_URL:-http://localhost:8500}"
  --wait "${APPLE_MLX_SMOKE_WAIT:-30}"
)

should_start "${START_EMBEDDER:-true}" || VERIFY_ARGS+=(--skip-embedder)
should_start "${START_RERANKER:-true}" || VERIFY_ARGS+=(--skip-reranker)
should_start "${START_DOCLING:-false}" || VERIFY_ARGS+=(--skip-docling)

"${PY}" "${REPO_ROOT}/scripts/verify_apple_mlx_runtime.py" \
  "${VERIFY_ARGS[@]}"
