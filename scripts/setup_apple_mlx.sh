#!/usr/bin/env bash
# One-shot Apple Silicon setup for Polymath's host-native MLX profile.

set -euo pipefail

runtime_root="${POLYMATH_DOCKER_DATA_ROOT:-${HOME}/PolymathRuntime}"
ingest_source_root="${POLYMATH_INGEST_SOURCE_ROOT:-}"
compose_profiles="${COMPOSE_PROFILES:-mcp}"
skip_bootstrap=0
skip_docker_up=0
force_secrets=0
preserve_host_sidecars=0

usage() {
  cat <<'EOF'
Usage: scripts/setup_apple_mlx.sh [options]

Options:
  --runtime-root PATH         Host runtime root. Default: $POLYMATH_DOCKER_DATA_ROOT or ~/PolymathRuntime
  --ingest-source-root PATH   Host folder mounted read-only at /ingest-source. Default: runtime-root/ingest-source
  --compose-profiles LIST     Compose profiles to enable. Default: mcp
  --skip-bootstrap            Do not run bootstrap-runtime.sh
  --skip-docker-up            Install MLX sidecars but do not start Docker
  --force-secrets             Regenerate bootstrap secrets
  --preserve-host-sidecars    Do not overwrite existing host sidecar main.py files
  -h, --help                  Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime-root)
      runtime_root="$2"
      shift 2
      ;;
    --ingest-source-root)
      ingest_source_root="$2"
      shift 2
      ;;
    --compose-profiles)
      compose_profiles="$2"
      shift 2
      ;;
    --skip-bootstrap)
      skip_bootstrap=1
      shift
      ;;
    --skip-docker-up)
      skip_docker_up=1
      shift
      ;;
    --force-secrets)
      force_secrets=1
      shift
      ;;
    --preserve-host-sidecars)
      preserve_host_sidecars=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "ERROR: Apple MLX setup requires a Darwin/arm64 host. This machine is $(uname -s)/$(uname -m)." >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
compose_files=(-f docker-compose.yml -f docker-compose.apple-mlx.yml)

if [[ -z "${ingest_source_root}" ]]; then
  ingest_source_root="${runtime_root%/}/ingest-source"
fi

export POLYMATH_DOCKER_DATA_ROOT="${runtime_root}"
export POLYMATH_INGEST_SOURCE_ROOT="${ingest_source_root}"
export COMPOSE_PROFILES="${compose_profiles}"

echo "[apple-mlx] repo root    : ${repo_root}"
echo "[apple-mlx] runtime root : ${POLYMATH_DOCKER_DATA_ROOT}"
echo "[apple-mlx] ingest root  : ${POLYMATH_INGEST_SOURCE_ROOT}"
echo "[apple-mlx] profiles     : ${COMPOSE_PROFILES}"

if [[ "${skip_bootstrap}" != "1" ]]; then
  bootstrap_args=(
    --runtime-root "${POLYMATH_DOCKER_DATA_ROOT}"
    --ingest-source-root "${POLYMATH_INGEST_SOURCE_ROOT}"
    --compose-profiles "${COMPOSE_PROFILES}"
    --generate-secrets
  )
  if [[ "${force_secrets}" == "1" ]]; then
    bootstrap_args+=(--force-secrets)
  fi
  echo "[apple-mlx] bootstrapping runtime layout"
  bash "${repo_root}/scripts/bootstrap-runtime.sh" "${bootstrap_args[@]}"
elif [[ ! -d "${POLYMATH_INGEST_SOURCE_ROOT}" ]]; then
  mkdir -p "${POLYMATH_INGEST_SOURCE_ROOT}"
fi

if [[ "${preserve_host_sidecars}" == "1" ]]; then
  export POLYMATH_APPLE_MLX_PRESERVE_HOST=1
fi

echo "[apple-mlx] installing host-native MLX sidecars and models"
bash "${repo_root}/scripts/install_apple_mlx_runtime.sh"

if [[ "${skip_docker_up}" == "1" ]]; then
  echo "[apple-mlx] Docker startup skipped."
  exit 0
fi

echo "[apple-mlx] starting Docker stack with MLX override"
(
  cd "${repo_root}"
  docker compose "${compose_files[@]}" up -d --build
)

echo "[apple-mlx] verifying backend MLX wiring"
backend_id="$(
  cd "${repo_root}"
  docker compose "${compose_files[@]}" ps -q backend
)"
if [[ -z "${backend_id}" ]]; then
  echo "ERROR: backend container is not running." >&2
  exit 1
fi

docker exec "${backend_id}" sh -c '
  test "$EMBEDDER_URL" = "http://host.docker.internal:8082"
  test "$RERANKER_URL" = "http://host.docker.internal:8081"
  test "$DOCLING_URL" = "http://host.docker.internal:8500"
  test "$RERANKER_SCORE_SCALE" = "cosine"
  printf "EMBEDDER_URL=%s\nRERANKER_URL=%s\nDOCLING_URL=%s\nRERANKER_SCORE_SCALE=%s\n" \
    "$EMBEDDER_URL" "$RERANKER_URL" "$DOCLING_URL" "$RERANKER_SCORE_SCALE"
'

echo "[apple-mlx] running end-to-end MLX smoke"
bash "${repo_root}/scripts/smoke_apple_mlx.sh"

echo "[apple-mlx] compose status"
(
  cd "${repo_root}"
  docker compose "${compose_files[@]}" ps
)

echo
echo "[apple-mlx] setup complete."
echo "Open: http://localhost:3000"
