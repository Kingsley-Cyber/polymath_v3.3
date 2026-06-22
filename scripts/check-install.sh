#!/usr/bin/env bash
set -euo pipefail

runtime_root="${POLYMATH_DOCKER_DATA_ROOT:-}"
skip_compose_config=0
check_running=0
skip_runtime_contracts=0
failures=0
warnings=0

usage() {
  cat <<'EOF'
Usage: scripts/check-install.sh [options]

Options:
  --runtime-root PATH      Host runtime root. Default: .env POLYMATH_DOCKER_DATA_ROOT or ~/PolymathRuntime
  --skip-compose-config    Do not run docker compose config --quiet
  --skip-runtime-contracts Do not run the static startup/worker/trigger contract check
  --check-running          Probe localhost services after docker compose up
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime-root)
      runtime_root="$2"
      shift 2
      ;;
    --skip-compose-config)
      skip_compose_config=1
      shift
      ;;
    --skip-runtime-contracts)
      skip_runtime_contracts=1
      shift
      ;;
    --check-running)
      check_running=1
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

ok() {
  printf '[ OK ] %s\n' "$*"
}

warn() {
  warnings=$((warnings + 1))
  printf '[WARN] %s\n' "$*" >&2
}

fail() {
  failures=$((failures + 1))
  printf '[FAIL] %s\n' "$*" >&2
}

env_get() {
  local key="$1"
  [[ -f "$env_file" ]] || return 0
  awk -F= -v k="$key" '$1 == k {sub(/^[^=]*=/, ""); print; exit}' "$env_file"
}

http_probe() {
  local name="$1"
  local url="$2"
  local required="${3:-1}"
  if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 5 "$url" >/dev/null; then
    ok "$name reachable ($url)"
  elif [[ "$required" == "1" ]]; then
    fail "$name unreachable: $url"
  else
    warn "$name unreachable: $url"
  fi
}

echo "Polymath install check"
echo "Repo: $repo_root"

if [[ "$skip_runtime_contracts" != "1" ]]; then
  runtime_contract_script="$repo_root/scripts/verify_runtime_contracts.py"
  if command -v python3 >/dev/null 2>&1; then
    if (cd "$repo_root" && python3 "$runtime_contract_script"); then
      ok "Runtime setup/worker/trigger contracts are intact"
    else
      fail "Runtime setup/worker/trigger contract check failed"
    fi
  elif command -v python >/dev/null 2>&1; then
    if (cd "$repo_root" && python "$runtime_contract_script"); then
      ok "Runtime setup/worker/trigger contracts are intact"
    else
      fail "Runtime setup/worker/trigger contract check failed"
    fi
  else
    warn "Python not found; skipping runtime contract check"
  fi
fi

if [[ ! -f "$env_file" ]]; then
  fail ".env is missing. Run scripts/bootstrap-runtime.sh --generate-secrets"
else
  ok ".env exists"
fi

if [[ -z "$runtime_root" ]]; then
  runtime_root="$(env_get POLYMATH_DOCKER_DATA_ROOT)"
fi
if [[ -z "$runtime_root" ]]; then
  runtime_root="$HOME/PolymathRuntime"
fi

for key in \
  MONGO_PASSWORD \
  NEO4J_PASSWORD \
  AUTH_SECRET_KEY \
  DEFAULT_ADMIN_PASSWORD \
  LITELLM_MASTER_KEY \
  MCP_API_KEY; do
  value="$(env_get "$key")"
  if [[ -z "$value" || "$value" == *CHANGE_ME* ]]; then
    fail "$key is missing or still has CHANGE_ME"
  else
    ok "$key is set"
  fi
done

binds_root="$(env_get POLYMATH_RUNTIME_BINDS_ROOT)"
if [[ -z "$binds_root" ]]; then
  binds_root="$runtime_root/binds"
fi

for path in \
  "$binds_root/litellm/config.yaml" \
  "$binds_root/modal_embedder.py" \
  "$runtime_root/volumes/mongodb" \
  "$runtime_root/volumes/qdrant" \
  "$runtime_root/volumes/neo4j/data"; do
  if [[ -e "$path" ]]; then
    ok "Found $path"
  else
    fail "Missing $path"
  fi
done

models_root="$(env_get POLYMATH_MODELS_ROOT)"
if [[ -z "$models_root" ]]; then
  models_root="$runtime_root/models"
fi

for model in Qwen3-Embedding-0.6B Qwen3-Reranker-0.6B-Q8_0-GGUF; do
  if [[ -d "$models_root/$model" ]]; then
    ok "Found model directory $models_root/$model"
  else
    warn "Model directory missing: $models_root/$model. Run bootstrap with --stage-models or use cloud embeddings."
  fi
done

if ! command -v docker >/dev/null 2>&1; then
  fail "Docker is not on PATH"
else
  ok "Docker is on PATH"
  if [[ -f "$repo_root/docker-compose.override.yml" ]]; then
    warn "Local docker-compose.override.yml detected; docker compose will auto-merge machine-specific overrides."
  fi
  if [[ "$skip_compose_config" != "1" ]]; then
    if (cd "$repo_root" && docker compose config --quiet); then
      ok "docker compose config is valid"
    else
      fail "docker compose config failed"
    fi
  fi
fi

if [[ "$check_running" == "1" ]]; then
  http_probe "Frontend" "http://localhost:3000" 1
  http_probe "Backend health" "http://localhost:8000/api/health" 1
  http_probe "MCP health" "http://localhost:8765/health" 0
  http_probe "Qdrant" "http://localhost:6333/healthz" 0
  http_probe "Neo4j browser" "http://localhost:7474" 0
fi

echo
echo "Summary: $failures failure(s), $warnings warning(s)"
if [[ "$failures" -gt 0 ]]; then
  exit 1
fi
