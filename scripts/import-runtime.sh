#!/usr/bin/env bash
set -euo pipefail

source_dir="${1:-}"
runtime_root="${POLYMATH_DOCKER_DATA_ROOT:-$HOME/PolymathRuntime}"
allow_running="${ALLOW_RUNNING:-0}"
include_env="${INCLUDE_ENV:-0}"
merge="${MERGE:-0}"
overwrite_env="${OVERWRITE_ENV:-0}"

if [[ -z "$source_dir" ]]; then
  echo "Usage: scripts/import-runtime.sh <export-dir|export.tar.gz|export.zip>" >&2
  exit 1
fi

temp_dir=""
cleanup() {
  if [[ -n "$temp_dir" && -d "$temp_dir" ]]; then
    rm -rf "$temp_dir"
  fi
}
trap cleanup EXIT

if [[ -f "$source_dir" ]]; then
  temp_dir="$(mktemp -d)"
  case "$source_dir" in
    *.tar.gz|*.tgz)
      tar -xzf "$source_dir" -C "$temp_dir"
      ;;
    *.zip)
      if ! command -v unzip >/dev/null 2>&1; then
        echo "Importing .zip archives requires unzip. Use a .tar.gz archive or install unzip." >&2
        exit 1
      fi
      unzip -q "$source_dir" -d "$temp_dir"
      ;;
    *)
      echo "Unsupported archive type: $source_dir. Use a directory, .tar.gz, .tgz, or .zip." >&2
      exit 1
      ;;
  esac
  source_dir="$temp_dir"
fi

if [[ ! -d "$source_dir/runtime" ]]; then
  echo "Runtime export is missing the 'runtime' directory: $source_dir/runtime" >&2
  exit 1
fi

if [[ "$allow_running" != "1" ]] && command -v docker >/dev/null 2>&1; then
  running="$(
    {
      docker ps --filter "label=com.docker.compose.project=polymath_v33" --format "{{.Names}}" 2>/dev/null || true
      docker ps --filter "name=polymath-mcp" --format "{{.Names}}" 2>/dev/null || true
    } | sort -u
  )"
  if [[ -n "$running" ]]; then
    echo "Polymath containers are running. Run 'docker compose down' first, or set ALLOW_RUNNING=1." >&2
    exit 1
  fi
fi

if [[ -d "$runtime_root" && "$merge" != "1" ]] && [[ -n "$(find "$runtime_root" -mindepth 1 -maxdepth 1 2>/dev/null || true)" ]]; then
  echo "Runtime root already has files: $runtime_root. Set MERGE=1 to import into it." >&2
  exit 1
fi

mkdir -p "$runtime_root"
if command -v rsync >/dev/null 2>&1; then
  rsync -a "$source_dir/runtime"/ "$runtime_root"/
else
  cp -a "$source_dir/runtime"/. "$runtime_root"/
fi

if [[ "$include_env" == "1" ]]; then
  if [[ ! -f "$source_dir/repo/.env" ]]; then
    echo "Requested INCLUDE_ENV=1, but export does not contain repo/.env" >&2
  elif [[ -f ".env" && "$overwrite_env" != "1" ]]; then
    echo ".env already exists. Set OVERWRITE_ENV=1 to replace it." >&2
    exit 1
  else
    cp -a "$source_dir/repo/.env" ".env"
  fi
fi

echo "Imported Polymath runtime core into: $runtime_root"
