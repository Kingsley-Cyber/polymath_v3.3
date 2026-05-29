#!/usr/bin/env bash
set -euo pipefail

runtime_root="${POLYMATH_DOCKER_DATA_ROOT:-}"
ingest_source_root="${POLYMATH_INGEST_SOURCE_ROOT:-}"
compose_profiles="${COMPOSE_PROFILES:-local-embed,local-rerank,local-parser,mcp}"
generate_secrets=0
force_secrets=0
stage_models=0
skip_docker_check=0
dry_run=0

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap-runtime.sh [options]

Options:
  --runtime-root PATH      Host runtime root. Default: $POLYMATH_DOCKER_DATA_ROOT or ~/PolymathRuntime
  --ingest-source-root PATH Host folder mounted read-only at /ingest-source. Default: runtime-root/ingest-source
  --compose-profiles LIST  Compose profiles to enable. Default: local-embed,local-rerank,local-parser,mcp
  --generate-secrets      Fill missing CHANGE_ME secrets in .env
  --force-secrets         Regenerate secrets even when values already exist
  --stage-models          Download Qwen3-Embedding-0.6B and Qwen3-Reranker-0.6B-Q8_0-GGUF
  --skip-docker-check     Do not run docker compose config --quiet
  --dry-run               Print intended work without writing files
  -h, --help              Show this help
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
    --generate-secrets)
      generate_secrets=1
      shift
      ;;
    --force-secrets)
      force_secrets=1
      shift
      ;;
    --stage-models)
      stage_models=1
      shift
      ;;
    --skip-docker-check)
      skip_docker_check=1
      shift
      ;;
    --dry-run)
      dry_run=1
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

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
env_file="$repo_root/.env"
env_example="$repo_root/.env.example"

step() {
  echo "==> $*"
}

generate_hex() {
  local bytes="${1:-32}"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$bytes"
  else
    LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c "$((bytes * 2))"
  fi
}

env_get() {
  local key="$1"
  [[ -f "$env_file" ]] || return 0
  awk -F= -v k="$key" '$1 == k {sub(/^[^=]*=/, ""); print; exit}' "$env_file"
}

if [[ -z "$runtime_root" ]]; then
  runtime_root="$(env_get POLYMATH_DOCKER_DATA_ROOT)"
fi
if [[ -z "$runtime_root" ]]; then
  runtime_root="$HOME/PolymathRuntime"
fi
case "$runtime_root" in
  /*) ;;
  *) runtime_root="$PWD/$runtime_root" ;;
esac
runtime_root="${runtime_root%/}"
if [[ -z "$ingest_source_root" ]]; then
  ingest_source_root="$runtime_root/ingest-source"
fi
case "$ingest_source_root" in
  /*) ;;
  *) ingest_source_root="$PWD/$ingest_source_root" ;;
esac
ingest_source_root="${ingest_source_root%/}"
binds_root="$runtime_root/binds"
models_root="$runtime_root/models"
cache_root="$runtime_root"

env_set() {
  local key="$1"
  local value="$2"
  if [[ "$dry_run" == "1" ]]; then
    echo "Would set $key=$value"
    return
  fi
  if [[ -f "$env_file" ]] && grep -q "^${key}=" "$env_file"; then
    awk -v k="$key" -v v="$value" '
      BEGIN { done = 0 }
      $0 ~ "^" k "=" { print k "=" v; done = 1; next }
      { print }
      END { if (!done) print k "=" v }
    ' "$env_file" > "$env_file.tmp"
    mv "$env_file.tmp" "$env_file"
  else
    echo "$key=$value" >> "$env_file"
  fi
}

needs_secret() {
  local value="${1:-}"
  [[ -z "$value" || "$value" == *CHANGE_ME* ]]
}

copy_seed_file() {
  local src="$1"
  local dst="$2"
  [[ -f "$src" ]] || { echo "Missing required repo file: $src" >&2; exit 1; }
  if [[ "$dry_run" == "1" ]]; then
    echo "Would copy $src -> $dst"
    return
  fi
  mkdir -p "$(dirname "$dst")"
  cp -f "$src" "$dst"
}

hf_download() {
  local model="$1"
  local destination="$2"
  local cmd=""
  if command -v hf >/dev/null 2>&1; then
    cmd="hf"
  elif command -v huggingface-cli >/dev/null 2>&1; then
    cmd="huggingface-cli"
  else
    echo "Hugging Face CLI not found. Install with: pip install -U huggingface_hub" >&2
    return
  fi
  if [[ "$dry_run" == "1" ]]; then
    echo "Would download $model to $destination"
    return
  fi
  mkdir -p "$destination"
  "$cmd" download "$model" --local-dir "$destination"
}

step "Preparing Polymath runtime at $runtime_root"

if [[ ! -f "$env_file" ]]; then
  [[ -f "$env_example" ]] || { echo "Missing .env.example" >&2; exit 1; }
  step "Creating .env from .env.example"
  if [[ "$dry_run" != "1" ]]; then
    cp "$env_example" "$env_file"
  fi
fi

for dir in \
  "$runtime_root" \
  "$runtime_root/volumes/mongodb" \
  "$runtime_root/volumes/qdrant" \
  "$runtime_root/volumes/neo4j/data" \
  "$runtime_root/volumes/neo4j/plugins" \
  "$runtime_root/volumes/neo4j/logs" \
  "$runtime_root/volumes/redis" \
  "$runtime_root/volumes/hf-cache" \
  "$runtime_root/volumes/docling/models" \
  "$runtime_root/volumes/ingest-files" \
  "$ingest_source_root" \
  "$binds_root/litellm" \
  "$models_root"; do
  if [[ "$dry_run" != "1" ]]; then
    mkdir -p "$dir"
  fi
done

step "Seeding bind-mounted config files"
copy_seed_file "$repo_root/litellm/config.yaml" "$binds_root/litellm/config.yaml"
copy_seed_file "$repo_root/modal_embedder.py" "$binds_root/modal_embedder.py"

env_set "POLYMATH_DOCKER_DATA_ROOT" "$runtime_root"
env_set "POLYMATH_RUNTIME_BINDS_ROOT" "$binds_root"
env_set "POLYMATH_CACHE_ROOT" "$cache_root"
env_set "POLYMATH_MODELS_ROOT" "$models_root"
env_set "POLYMATH_INGEST_SOURCE_ROOT" "$ingest_source_root"
env_set "COMPOSE_PROFILES" "$compose_profiles"
env_set "LOCAL_EMBEDDER_ENABLED" "true"
env_set "LOCAL_RERANKER_ENABLED" "true"

if [[ "$generate_secrets" == "1" ]]; then
  step "Generating missing secrets"
  mongo_password="$(env_get MONGO_PASSWORD)"
  if [[ "$force_secrets" == "1" ]] || needs_secret "$mongo_password"; then
    mongo_password="$(generate_hex 24)"
    env_set "MONGO_PASSWORD" "$mongo_password"
  fi
  env_set "MONGODB_URI" "mongodb://polymath:${mongo_password}@mongodb:27017/polymath?authSource=admin"

  for spec in \
    "NEO4J_PASSWORD:24" \
    "AUTH_SECRET_KEY:48" \
    "DEFAULT_ADMIN_PASSWORD:18" \
    "LITELLM_MASTER_KEY:32" \
    "MCP_API_KEY:32"; do
    key="${spec%%:*}"
    bytes="${spec##*:}"
    current="$(env_get "$key")"
    if [[ "$force_secrets" == "1" ]] || needs_secret "$current"; then
      env_set "$key" "$(generate_hex "$bytes")"
    fi
  done
fi

if [[ "$skip_docker_check" != "1" ]]; then
  step "Checking Docker"
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker was not found on PATH. Install Docker Desktop or Docker Engine first." >&2
    exit 1
  fi
  if [[ "$dry_run" != "1" ]]; then
    (cd "$repo_root" && docker compose config --quiet)
  fi
fi

if [[ "$stage_models" == "1" ]]; then
  step "Downloading local embedding and reranker models"
  hf_download "Qwen/Qwen3-Embedding-0.6B" "$models_root/Qwen3-Embedding-0.6B"
  hf_download "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF" "$models_root/Qwen3-Reranker-0.6B-Q8_0-GGUF"
else
  echo "Skipping model downloads. Re-run with --stage-models when ready."
fi

cat <<EOF

Polymath runtime bootstrap complete.
Next:
  docker compose up -d --build
  scripts/check-install.sh
  open http://localhost:3000
EOF
