#!/usr/bin/env bash
# ingest_reclaim_memory.sh — deterministic memory reclaim before a batch ingest.
#
# The Mac Studio's main job is hosting; before a long ingestion run this quits
# every regular GUI app EXCEPT the hosting/work keep-list, so ingestion always
# starts from a known memory state. Measured budget (2026-06-10, under load):
# extraction sidecar ~3-5 GB + embedder ~2 GB + Docker stack ~10-12 GB +
# macOS floor ~3.5 GB ≈ 20-22 GB peak — so reclaiming to ≥25 GB available
# leaves margin for doubled inference batches across a multi-day run.
#
# Usage:
#   scripts/ingest_reclaim_memory.sh              # DRY RUN: show what would quit
#   scripts/ingest_reclaim_memory.sh --apply      # actually quit the apps
#   scripts/ingest_reclaim_memory.sh --apply --require-gb 22
#                                                 # exit 1 if available < 22 GB after
#
# Keep-list override:  KEEP_APPS="Docker,Claude,Ghostty" scripts/ingest_reclaim_memory.sh
#
# Notes: quits are GRACEFUL (AppleScript quit — apps can prompt to save);
# nothing is force-killed. Docker/colima, terminals, and Claude are never
# touched by default. Browsers are quit by default — override KEEP_APPS if a
# browser must stay.

set -euo pipefail

APPLY=false
REQUIRE_GB=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=true ;;
    --require-gb) ;;  # value handled below
    *) [[ "${prev:-}" == "--require-gb" ]] && REQUIRE_GB="$arg" ;;
  esac
  prev="$arg"
done

# Never quit these (substring match, case-insensitive). Terminals + Docker +
# Claude (desktop/CLI sessions often drive the ingest) + Finder/system.
DEFAULT_KEEP="Finder,Docker,Terminal,iTerm,Ghostty,Warp,Alacritty,kitty,Claude,Activity Monitor"
KEEP="${KEEP_APPS:-$DEFAULT_KEEP}"

avail_gb() {
  # free + inactive + purgeable + speculative ≈ reclaimable-on-demand memory.
  vm_stat | awk '
    /page size of/   { ps = $8 }
    /Pages free/     { free = $3 }
    /Pages inactive/ { inact = $3 }
    /Pages purgeable/{ purg = $3 }
    /Pages speculative/{ spec = $3 }
    END { gsub(/\./,"",free); gsub(/\./,"",inact); gsub(/\./,"",purg); gsub(/\./,"",spec)
          printf "%.1f", (free+inact+purg+spec) * ps / 1073741824 }'
}

BEFORE=$(avail_gb)
echo "[reclaim] available before: ${BEFORE} GB (of $(sysctl -n hw.memsize | awk '{printf "%.0f", $1/1073741824}') GB)"

# Regular (non-background) GUI apps via System Events.
RUNNING=$(osascript -e 'tell application "System Events" to get name of (processes where background only is false)' | tr "," "\n" | sed 's/^ *//;s/ *$//')

QUIT_LIST=()
while IFS= read -r app; do
  [[ -z "$app" ]] && continue
  skip=false
  IFS=',' read -ra KEEPS <<< "$KEEP"
  for k in "${KEEPS[@]}"; do
    k_trim=$(echo "$k" | sed 's/^ *//;s/ *$//')
    [[ -n "$k_trim" ]] && shopt -s nocasematch && [[ "$app" == *"$k_trim"* ]] && skip=true && shopt -u nocasematch && break
    shopt -u nocasematch || true
  done
  $skip || QUIT_LIST+=("$app")
done <<< "$RUNNING"

if [[ ${#QUIT_LIST[@]} -eq 0 ]]; then
  echo "[reclaim] nothing to quit (keep-list: $KEEP)"
else
  for app in "${QUIT_LIST[@]}"; do
    rss=$(ps -axo rss,comm | grep -i "$app" | awk '{s+=$1} END {printf "%.2f", s/1048576}')
    if $APPLY; then
      echo "[reclaim] quitting: $app (~${rss:-?} GB)"
      osascript -e "tell application \"$app\" to quit" 2>/dev/null || \
        echo "  [warn] $app did not quit (unsaved changes prompt?)"
    else
      echo "[dry-run] would quit: $app (~${rss:-?} GB)"
    fi
  done
fi

if $APPLY; then
  sleep 5
  AFTER=$(avail_gb)
  echo "[reclaim] available after: ${AFTER} GB (was ${BEFORE} GB)"
  if [[ "$REQUIRE_GB" != "0" ]]; then
    if awk -v a="$AFTER" -v r="$REQUIRE_GB" 'BEGIN{exit !(a < r)}'; then
      echo "[reclaim] FAIL: ${AFTER} GB < required ${REQUIRE_GB} GB. Largest residents:"
      ps -axo rss,comm | sort -rn | head -8 | awk '{printf "  %5.2f GB  %s\n", $1/1048576, substr($0, index($0,$2))}'
      exit 1
    fi
    echo "[reclaim] OK: ${AFTER} GB ≥ ${REQUIRE_GB} GB required"
  fi
else
  echo "[dry-run] re-run with --apply to execute; add --require-gb 22 to enforce a floor"
fi
