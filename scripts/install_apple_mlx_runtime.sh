#!/usr/bin/env bash
# Polymath 3.3 — Apple Silicon MLX runtime installer.
#
# What this does (idempotent):
#   1. Verifies host is Darwin/arm64.
#   2. Stages scripts/apple_ml_services/ into ~/PolymathRuntime/apple_ml_services/
#   3. Creates a uv-managed venv with requirements.txt
#   4. Pre-warms and verifies the HuggingFace cache with the MLX model weights
#   5. Writes a LaunchAgent (com.polymath.apple-ml) and bootstraps it
#   6. Smoke-tests enabled sidecars
#
# Usage (run from repo root on macOS with every manifest value exported):
#   bash scripts/install_apple_mlx_runtime.sh --env-manifest /secure/off.json
#
# Re-run any time. Safe to interrupt; rerun resumes.

set -euo pipefail

if [[ "$#" -ne 2 || "$1" != "--env-manifest" ]]; then
  echo "ERROR: --env-manifest PATH is mandatory; implicit deployment defaults are forbidden." >&2
  exit 2
fi
ENV_MANIFEST="$2"

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
arbiter_expected="$(printf '%s' "${ARBITER_ENABLED:?ARBITER_ENABLED must be exported}" | tr '[:upper:]' '[:lower:]')"
"${PYTHON:-python3}" "${REPO_ROOT}/scripts/apple_mlx_env_manifest.py" \
  --manifest "${ENV_MANIFEST}" \
  --expect-arbiter-enabled "${arbiter_expected}" \
  --check-process-environment

RUNTIME_ROOT="${POLYMATH_DOCKER_DATA_ROOT:?POLYMATH_DOCKER_DATA_ROOT must be exported}"
SERVICES_DIR="${RUNTIME_ROOT}/apple_ml_services"
LOG_DIR="${RUNTIME_ROOT}/logs"
LAUNCH_AGENT_NAME="com.polymath.apple-ml"
LAUNCH_AGENT_PATH="${HOME}/Library/LaunchAgents/${LAUNCH_AGENT_NAME}.plist"
APPLE_MLX_EMBED_MODEL_ID="${APPLE_MLX_EMBED_MODEL_ID:?}"
APPLE_MLX_RERANKER_MODEL_ID="${APPLE_MLX_RERANKER_MODEL_ID:?}"
APPLE_RERANKER_BACKEND="${APPLE_RERANKER_BACKEND:?}"
APPLE_TORCH_RERANKER_MODEL_ID="${APPLE_TORCH_RERANKER_MODEL_ID:?}"
RERANKER_SCORE_SCALE="${RERANKER_SCORE_SCALE:?}"
EMBEDDER_MODEL_NAME="${EMBEDDER_MODEL_NAME:?}"
EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:?}"
EMBED_MAX_LENGTH="${EMBED_MAX_LENGTH:?}"
EMBEDDER_REQUEST_TIMEOUT_SECONDS="${EMBEDDER_REQUEST_TIMEOUT_SECONDS:?}"
EMBEDDER_QUEUE_TIMEOUT_SECONDS="${EMBEDDER_QUEUE_TIMEOUT_SECONDS:?}"
EMBEDDER_WARMUP_TIMEOUT_SECONDS="${EMBEDDER_WARMUP_TIMEOUT_SECONDS:?}"
MLX_CACHE_LIMIT_GB="${MLX_CACHE_LIMIT_GB:?}"
RERANKER_CAL_MU="${RERANKER_CAL_MU:?}"
RERANKER_CAL_T="${RERANKER_CAL_T:?}"
RERANKER_CAL_VERSION="${RERANKER_CAL_VERSION:?}"
RERANKER_BATCH_SIZE="${RERANKER_BATCH_SIZE:?}"
RERANKER_MAX_DOC_CHARS="${RERANKER_MAX_DOC_CHARS:?}"
RERANKER_MAX_QUERY_CHARS="${RERANKER_MAX_QUERY_CHARS:?}"
RERANKER_REQUEST_TIMEOUT_SECONDS="${RERANKER_REQUEST_TIMEOUT_SECONDS:?}"
RERANKER_QUEUE_TIMEOUT_SECONDS="${RERANKER_QUEUE_TIMEOUT_SECONDS:?}"
RERANKER_WARM_ON_STARTUP="${RERANKER_WARM_ON_STARTUP:?}"
RERANKER_WARMUP_CANDIDATE_SHAPES="${RERANKER_WARMUP_CANDIDATE_SHAPES:?}"
RERANKER_WARMUP_CANDIDATES="${RERANKER_WARMUP_CANDIDATES:?}"
RERANKER_WARMUP_DOC_CHARS="${RERANKER_WARMUP_DOC_CHARS:?}"
START_EMBEDDER="${START_EMBEDDER:?}"
START_RERANKER="${START_RERANKER:?}"
START_DOCLING="${START_DOCLING:?}"
ARBITER_ENABLED="${ARBITER_ENABLED:?}"
ARBITER_HOST="${ARBITER_HOST:?}"
ARBITER_PORT="${ARBITER_PORT:?}"
ARBITER_ACQUIRE_TIMEOUT_SECONDS="${ARBITER_ACQUIRE_TIMEOUT_SECONDS:?}"
ARBITER_EMBED_HOLD_TARGET_MS="${ARBITER_EMBED_HOLD_TARGET_MS:?}"
ARBITER_RERANK_HOLD_TARGET_MS="${ARBITER_RERANK_HOLD_TARGET_MS:?}"
ARBITER_MAX_EMBED_BURST="${ARBITER_MAX_EMBED_BURST:?}"
ARBITER_RERANK_STARVATION_SECONDS="${ARBITER_RERANK_STARVATION_SECONDS:?}"
ARBITER_STALE_LEASE_SECONDS="${ARBITER_STALE_LEASE_SECONDS:?}"

should_start() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

echo "[apple-mlx] runtime root : ${RUNTIME_ROOT}"
echo "[apple-mlx] services     : ${SERVICES_DIR}"
echo "[apple-mlx] launch agent : ${LAUNCH_AGENT_PATH}"
echo "[apple-mlx] embed batch  : ${EMBED_BATCH_SIZE}"
echo "[apple-mlx] sidecars     : embedder=${START_EMBEDDER} reranker=${START_RERANKER} docling=${START_DOCLING}"
echo "[apple-mlx] reranker     : backend=${APPLE_RERANKER_BACKEND} model=${APPLE_TORCH_RERANKER_MODEL_ID}"
echo "[apple-mlx] gpu arbiter   : enabled=${ARBITER_ENABLED} ${ARBITER_HOST}:${ARBITER_PORT}"

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
  'gpu_arbiter/client.py'
  'gpu_arbiter/main.py'
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
if [[ ! -x "${SERVICES_DIR}/.venv/bin/python" ]]; then
  uv venv .venv --python 3.11
else
  echo "[apple-mlx] reusing existing uv environment"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
uv pip install --upgrade pip
uv pip install -r requirements.txt
deactivate

# ── 4. Pre-warm HuggingFace model cache ──────────────────────────────
echo "[apple-mlx] pre-pulling MLX model weights (this can take a while on first run)"
APPLE_MLX_EMBED_MODEL_ID="${APPLE_MLX_EMBED_MODEL_ID}" \
APPLE_MLX_RERANKER_MODEL_ID="${APPLE_MLX_RERANKER_MODEL_ID}" \
APPLE_TORCH_RERANKER_MODEL_ID="${APPLE_TORCH_RERANKER_MODEL_ID}" \
HF_HOME="${RUNTIME_ROOT}/volumes/hf-cache" \
HF_HUB_CACHE="${RUNTIME_ROOT}/volumes/hf-cache/hub" \
"${SERVICES_DIR}/.venv/bin/python" "${REPO_ROOT}/scripts/pull_apple_mlx_models.py"

APPLE_MLX_EMBED_MODEL_ID="${APPLE_MLX_EMBED_MODEL_ID}" \
APPLE_MLX_RERANKER_MODEL_ID="${APPLE_MLX_RERANKER_MODEL_ID}" \
APPLE_TORCH_RERANKER_MODEL_ID="${APPLE_TORCH_RERANKER_MODEL_ID}" \
HF_HOME="${RUNTIME_ROOT}/volumes/hf-cache" \
HF_HUB_CACHE="${RUNTIME_ROOT}/volumes/hf-cache/hub" \
"${SERVICES_DIR}/.venv/bin/python" "${REPO_ROOT}/scripts/pull_apple_mlx_models.py" --check-only

# ── 5. LaunchAgent ───────────────────────────────────────────────────
mkdir -p "${HOME}/Library/LaunchAgents"

expected_plist="$(mktemp "${TMPDIR:-/tmp}/polymath-apple-ml-plist.XXXXXX")"
trap 'rm -f "${expected_plist}"' EXIT
"${SERVICES_DIR}/.venv/bin/python" "${REPO_ROOT}/scripts/render_apple_mlx_launch_agent.py" \
  --output "${expected_plist}" \
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
plutil -lint "${expected_plist}" >/dev/null

# ── 6. Transactional deploy + smoke ──────────────────────────────────
echo "[apple-mlx] waiting up to 90s for sidecars to come up"
VERIFY_ARGS=(--wait 90)
should_start "${START_EMBEDDER}" || VERIFY_ARGS+=(--skip-embedder)
should_start "${START_RERANKER}" || VERIFY_ARGS+=(--skip-reranker)
should_start "${START_DOCLING}" || VERIFY_ARGS+=(--skip-docling)
"${SERVICES_DIR}/.venv/bin/python" \
  "${REPO_ROOT}/scripts/apple_mlx_launch_agent_transaction.py" \
  --expected-plist "${expected_plist}" \
  --target-plist "${LAUNCH_AGENT_PATH}" \
  --label "${LAUNCH_AGENT_NAME}" \
  -- \
  "${SERVICES_DIR}/.venv/bin/python" \
  "${REPO_ROOT}/scripts/verify_apple_mlx_runtime.py" \
  "${VERIFY_ARGS[@]}"
echo "[apple-mlx] transactional plist deploy + smoke: clean"

echo
echo "[apple-mlx] installed. Now bring up Docker with the override:"
echo "  docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build"
echo
echo "Logs : ${LOG_DIR}/apple_ml_services.log"
echo "Stop : launchctl bootout gui/\$(id -u)/${LAUNCH_AGENT_NAME}"
echo "Kick : launchctl kickstart -k gui/\$(id -u)/${LAUNCH_AGENT_NAME}"
