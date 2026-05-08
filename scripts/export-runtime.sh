#!/usr/bin/env bash
set -euo pipefail

runtime_root="${POLYMATH_DOCKER_DATA_ROOT:-$HOME/PolymathRuntime}"
destination="${1:-polymath-runtime-core-$(date -u +%Y%m%d-%H%M%S)}"
allow_running="${ALLOW_RUNNING:-0}"
include_env="${INCLUDE_ENV:-0}"
include_models="${INCLUDE_MODELS:-0}"
archive="${ARCHIVE:-0}"
archive_path="${ARCHIVE_PATH:-}"

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

if [[ ! -d "$runtime_root" ]]; then
  echo "Runtime root not found: $runtime_root" >&2
  exit 1
fi

mkdir -p "$destination/runtime"

copy_item() {
  local rel="$1"
  local src="$runtime_root/$rel"
  local dst="$destination/runtime/$rel"
  if [[ ! -e "$src" ]]; then
    echo "Skipping missing runtime item: $rel" >&2
    return
  fi
  mkdir -p "$(dirname "$dst")"
  if command -v rsync >/dev/null 2>&1; then
    if [[ -d "$src" ]]; then
      mkdir -p "$dst"
      rsync -a "$src"/ "$dst"/
    else
      rsync -a "$src" "$dst"
    fi
  else
    cp -a "$src" "$dst"
  fi
}

for rel in \
  "volumes/mongodb" \
  "volumes/qdrant" \
  "volumes/neo4j" \
  "volumes/redis" \
  "volumes/n8n" \
  "binds/litellm" \
  "binds/modal_embedder.py"; do
  copy_item "$rel"
done

if [[ "$include_models" == "1" ]]; then
  copy_item "models"
fi

if [[ "$include_env" == "1" && -f ".env" ]]; then
  mkdir -p "$destination/repo"
  cp -a ".env" "$destination/repo/.env"
fi

cat > "$destination/manifest.json" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "runtime_root": "$runtime_root",
  "include_env": $([[ "$include_env" == "1" ]] && echo true || echo false),
  "include_models": $([[ "$include_models" == "1" ]] && echo true || echo false),
  "note": "Stop containers before export/import. Mongo, Qdrant, and Neo4j are the ingestion-critical stores."
}
EOF

echo "Exported Polymath runtime core to: $destination"

if [[ "$archive" == "1" || -n "$archive_path" ]]; then
  if [[ -z "$archive_path" ]]; then
    archive_path="${destination%/}.tar.gz"
  fi
  mkdir -p "$(dirname "$archive_path")"
  if [[ -e "$archive_path" && "${OVERWRITE:-0}" != "1" ]]; then
    echo "Archive already exists: $archive_path. Set OVERWRITE=1 or choose a new ARCHIVE_PATH." >&2
    exit 1
  fi
  tar -czf "$archive_path" -C "$destination" .
  echo "Created portable archive: $archive_path"
fi
