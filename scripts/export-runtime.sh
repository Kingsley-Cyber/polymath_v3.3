#!/usr/bin/env bash
set -euo pipefail

# ── tar binary selection — important for cross-OS handoff ──────────
# On Windows we MUST use git-bash GNU tar at /usr/bin/tar. The Windows
# 10+ system tar.exe is BSD libarchive — it produces archives that
# macOS BSD tar sometimes can't read back when paths contain backslashes
# or alternate streams. Picked up by both the full archive and the
# preflight smoke.
pick_tar_bin() {
  local candidate="tar"
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*)
      if [[ -x "/usr/bin/tar" ]]; then
        candidate="/usr/bin/tar"
      elif [[ -x "/mingw64/bin/tar" ]]; then
        candidate="/mingw64/bin/tar"
      fi
      ;;
  esac
  printf '%s' "$candidate"
}
tar_bin="$(pick_tar_bin)"

# ── preflight smoke ─────────────────────────────────────────────────
# Cheap round-trip test that doesn't need the runtime root to exist.
# Use this BEFORE a big export to confirm tar selection + cross-OS
# archive validity on this host.
#   bash scripts/export-runtime.sh --smoke
#   SMOKE=1 bash scripts/export-runtime.sh
if [[ "${SMOKE:-0}" == "1" || "${1:-}" == "--smoke" ]]; then
  smoke_dir="$(mktemp -d)"
  echo "polymath export smoke $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$smoke_dir/probe.txt"
  smoke_archive="$smoke_dir/smoke.tar.gz"
  "$tar_bin" -czf "$smoke_archive" -C "$smoke_dir" probe.txt
  if ! "$tar_bin" -tzf "$smoke_archive" 2>/dev/null | grep -q "probe.txt"; then
    echo "SMOKE FAIL: tar archive round-trip broken on this host." >&2
    echo "  tar binary: $tar_bin" >&2
    echo "  version   : $("$tar_bin" --version 2>&1 | head -1)" >&2
    exit 1
  fi
  echo "SMOKE OK: $tar_bin"
  "$tar_bin" --version 2>&1 | head -1 | sed 's/^/  /'
  rm -rf "$smoke_dir"
  # If the user only asked for smoke, stop here.
  if [[ "${1:-}" == "--smoke" || "${SMOKE_ONLY:-0}" == "1" ]]; then
    exit 0
  fi
fi

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

  echo "Archiving with: $tar_bin ($("$tar_bin" --version 2>&1 | head -1))"

  "$tar_bin" -czf "$archive_path" -C "$destination" .

  # Cross-OS sanity: verify the archive lists cleanly. Catches the
  # rare case where tar wrote out paths with backslashes or stored
  # files that can't be read back.
  if ! "$tar_bin" -tzf "$archive_path" >/dev/null 2>&1; then
    echo "ERROR: archive failed validation after write: $archive_path" >&2
    exit 1
  fi

  size_h="$(du -h "$archive_path" 2>/dev/null | awk '{print $1}')"
  echo "Created portable archive: $archive_path ($size_h)"
fi
