#!/usr/bin/env bash
# run_backfill.sh — launch + babysit the full authentic_files backfill.
#
# Sleep-proof: the whole script runs under `caffeinate -ims` (prevents idle/
# system/disk sleep while it lives; display MAY sleep — processes keep running
# on a desktop Mac with the lid... none). This matters: this machine's system
# sleep is set to 1 MINUTE and is only held off by transient assertions —
# almost certainly what killed Docker Desktop's VM twice on 2026-06-10.
#
# Durable: uses POST /corpora/{id}/ingest-batches/local — the backend writes a
# manifest to Mongo BEFORE processing, then leases/resumes items from it, so a
# crash/restart resumes instead of restarting. State (corpus/batch ids) is kept
# in .backfill_state so re-running this script RESUMES the same batch.
#
# Usage:
#   scripts/run_backfill.sh                         # preflight + launch/resume + monitor
#   SKIP_RECLAIM=1 scripts/run_backfill.sh          # skip memory reclaim
#   RTX_EXTRACT_HEALTH_URL=http://host:8086/health scripts/run_backfill.sh
#
# Preflights hard-fail loudly: Docker stack, backend health, RTX extraction
# sidecar warm, native embedder, source directory mounted, ≥25 GB free disk.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$REPO/.backfill_state"
SOURCE_HOST_DIR="${SOURCE_HOST_DIR:-/Volumes/Flash Drive/authentic_files}"
SOURCE_PATH="${SOURCE_PATH:-/ingest-source/authentic_files}"   # container view of SOURCE_HOST_DIR
CORPUS_NAME="${CORPUS_NAME:-authentic_library}"
RTX_EXTRACT_HEALTH_URL="${RTX_EXTRACT_HEALTH_URL:-http://host.docker.internal:8086/health}"
LOG="$REPO/backfill_$(date +%Y%m%d_%H%M%S).log"

# Re-exec under caffeinate so sleep prevention lives exactly as long as we do.
if [[ -z "${CAFFEINATED:-}" ]]; then
  echo "[backfill] re-launching under caffeinate (system/idle/disk sleep prevented)"
  CAFFEINATED=1 exec caffeinate -ims "$0" "$@"
fi

say_log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# ---------- preflights -------------------------------------------------------
say_log "preflight: docker"
docker ps >/dev/null 2>&1 || { say_log "FATAL: docker daemon down — open -a Docker first"; exit 1; }

say_log "preflight: backend"
curl -s -m 5 http://localhost:8000/api/health >/dev/null || { say_log "FATAL: backend not responding"; exit 1; }

say_log "preflight: RTX extraction sidecar (${RTX_EXTRACT_HEALTH_URL})"
RTX=$(docker exec -e RTX_EXTRACT_HEALTH_URL="$RTX_EXTRACT_HEALTH_URL" polymath_v33-backend-1 python -c "
import os, urllib.request, json
r = json.load(urllib.request.urlopen(os.environ['RTX_EXTRACT_HEALTH_URL'], timeout=8))
g = r.get('gliner') or {}
ok = (r.get('status')=='ok' and r.get('warm') and g.get('backend')=='onnx'
      and 'CUDAExecutionProvider' in (g.get('providers') or []))
print('ok' if ok else 'bad', g.get('backend','?'), str(g.get('providers'))[:48])" 2>/dev/null || echo "unreachable")
case "$RTX" in ok*) say_log "  RTX: $RTX";; *) say_log "FATAL: RTX ONNX sidecar not ok/warm/cuda: $RTX"; exit 1;; esac

say_log "preflight: embedder"
curl -s -m 5 http://localhost:8082/health | grep -q '"status":"ok"' || { say_log "FATAL: embedder :8082 down"; exit 1; }

say_log "preflight: source files"
N_FILES=$(find "$SOURCE_HOST_DIR" -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
[[ "$N_FILES" -gt 400 ]] || { say_log "FATAL: expected ~498 .md files, found $N_FILES in $SOURCE_HOST_DIR — source mounted?"; exit 1; }
say_log "  $N_FILES files visible"

say_log "preflight: disk space"
FREE_GB=$(df -g / | awk 'NR==2 {print $4}')
[[ "$FREE_GB" -ge 25 ]] || { say_log "FATAL: only ${FREE_GB}GB free (need 25)"; exit 1; }

if [[ -z "${SKIP_RECLAIM:-}" ]]; then
  say_log "memory reclaim (dry-run shown; run with --apply manually if desired)"
  "$REPO/scripts/ingest_reclaim_memory.sh" 2>&1 | tail -3 | tee -a "$LOG"
fi

# ---------- auth -------------------------------------------------------------
PW=$(grep '^DEFAULT_ADMIN_PASSWORD=' "$REPO/.env" | cut -d= -f2-)
TOKEN=$(curl -s -m 10 -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
[[ -n "$TOKEN" ]] || { say_log "FATAL: login failed"; exit 1; }

# ---------- create-or-resume -------------------------------------------------
if [[ -f "$STATE" ]]; then
  source "$STATE"
  say_log "resuming existing batch $BATCH_ID (corpus $CORPUS_ID)"
  curl -s -m 30 -X POST "http://localhost:8000/api/ingest-batches/$BATCH_ID/resume" \
    -H "Authorization: Bearer $TOKEN" >/dev/null || true
else
  CORPUS_ID=$(curl -s -m 15 -X POST http://localhost:8000/api/corpora \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"name\":\"$CORPUS_NAME\",\"description\":\"Full local backfill of authentic_files (498 docs) — RTX extraction, 128-tok children\"}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['corpus_id'])")
  say_log "corpus: $CORPUS_ID"
  BATCH_ID=$(curl -s -m 60 -X POST "http://localhost:8000/api/corpora/$CORPUS_ID/ingest-batches/local" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"root_path\":\"$SOURCE_PATH\",\"recursive\":false,\"extensions\":[\".md\"],\"use_neo4j\":true,\"concurrency\":3}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['batch_id'])")
  say_log "durable batch: $BATCH_ID"
  printf 'CORPUS_ID=%s\nBATCH_ID=%s\n' "$CORPUS_ID" "$BATCH_ID" > "$STATE"
fi

# ---------- monitor ----------------------------------------------------------
say_log "monitoring (60s cadence) — log: $LOG"
LAST_DONE=-1
while true; do
  SNAP=$(curl -s -m 15 "http://localhost:8000/api/ingest-batches/$BATCH_ID" -H "Authorization: Bearer $TOKEN" \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = d['counts']
state = 'DONE' if (c['done']+c['skipped']) == d['total'] else ('STALLED?' if c['running']==0 and c['queued']==0 else 'working')
print(state, c['done'], c['failed'], d['total'])" 2>/dev/null || echo "POLL_FAIL 0 0 0")
  read -r ST DONE FAILED TOTAL <<< "$SNAP"
  if [[ "$DONE" != "$LAST_DONE" ]]; then
    say_log "progress: $DONE/$TOTAL done, $FAILED failed"
    LAST_DONE="$DONE"
  fi
  case "$ST" in
    DONE)   say_log "BACKFILL COMPLETE: $DONE/$TOTAL done, $FAILED failed"; break;;
    POLL_FAIL) say_log "WARN: poll failed (backend restarting?) — retrying";;
  esac
  sleep 60
done
say_log "review failures (if any) with: GET /api/ingest-batches/$BATCH_ID"
