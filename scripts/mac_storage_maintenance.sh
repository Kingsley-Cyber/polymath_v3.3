#!/bin/bash
# Nightly storage guardrail (installed 2026-08-09, owner-ordered:
# "ensure my storage isn't consumed any more").
# Only regenerable caches are touched — never data volumes, never images
# in use, never Mongo/Qdrant/Neo4j content.
LOG=/tmp/polymath_storage_maintenance.log
{
  echo "=== $(date) ==="
  # Docker build cache — the main regrowth source (image rebuilds)
  docker builder prune -af 2>/dev/null | tail -1
  # Dangling (untagged, unreferenced) images only — never tagged/in-use ones
  docker image prune -f 2>/dev/null | tail -1
  # Harness scratch logs older than 7 days
  find /tmp -maxdepth 1 -name "*.txt" -mtime +7 -delete 2>/dev/null
  df -h /System/Volumes/Data | tail -1
} >> "$LOG" 2>&1
