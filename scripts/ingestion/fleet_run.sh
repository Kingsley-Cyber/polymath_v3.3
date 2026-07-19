#!/bin/bash
# fleet_run.sh — sequential 4-corpus ingestion with deepseek key rotation.
# Emits one stdout line per meaningful event (Monitor-friendly).
set -uo pipefail
DIR="$HOME/PolymathRuntime/manual-ingest-state"
LAUNCH="$DIR/launch_ingest.sh"
# DeepSeek rotation keys load from the operator key file — NEVER inline keys here.
KEYFILE="$DIR/rotation_keys.env"
KEYS=()
if [[ -f "$KEYFILE" ]]; then
  while IFS='=' read -r k v; do
    case "$k" in DEEPSEEK_KEY_*) KEYS+=("$v") ;; esac
  done < "$KEYFILE"
fi
[[ ${#KEYS[@]} -gt 0 ]] || { echo "no DEEPSEEK_KEY_* in $KEYFILE" >&2; exit 1; }
ACTIVE_IDX_FILE="$DIR/active_deepseek_idx"
[[ -s "$ACTIVE_IDX_FILE" ]] || echo 0 > "$ACTIVE_IDX_FILE"

balance() { curl -s --max-time 15 https://api.deepseek.com/user/balance -H "Authorization: Bearer $1" | python3 -c "import json,sys; print(json.load(sys.stdin)['balance_infos'][0]['total_balance'])" 2>/dev/null || echo "?"; }

rotate_if_low() {
  local idx key bal nxt
  idx=$(cat "$ACTIVE_IDX_FILE"); key="${KEYS[$idx]}"; bal=$(balance "$key")
  echo "KEYCHECK active=#$((idx+1)) balance=\$$bal"
  if python3 -c "import sys; sys.exit(0 if float('$bal' if '$bal' != '?' else 0) < 1.20 else 1)" 2>/dev/null; then
    nxt=$(( (idx + 1) % 3 ))
    if [[ "$nxt" != "$idx" ]]; then
      docker exec -i polymath_v33-backend-1 python - 2>/dev/null <<PYEOF
import asyncio, os
import motor.motor_asyncio
async def main():
    from services.settings import settings_service
    cli = motor.motor_asyncio.AsyncIOMotorClient(os.environ["MONGODB_URI"])
    settings_service.attach(cli.get_default_database())
    await settings_service.update_api_keys("6a132beafef900c17f87848e", {"deepseek": "${KEYS[$nxt]}"})
    print("rotated")
asyncio.run(main())
PYEOF
      echo "$nxt" > "$ACTIVE_IDX_FILE"
      echo "ROTATED deepseek key #$((idx+1)) -> #$((nxt+1)) (balance \$$bal < \$1.20)"
    fi
  fi
}

run_corpus() {
  local name="$1" src="$2" extend="${3:-}"
  rotate_if_low
  echo "LAUNCH $name ($src)"
  local out
  out=$(CORPUS_NAME="$name" SOURCE_PATH="$src" EXTEND_CORPUS_ID="$extend" CONCURRENCY="${4:-6}" PROFILE="${5:-runpod_burst}" "$LAUNCH" 2>/dev/null | tail -3)
  local batch_id
  batch_id=$(echo "$out" | sed -n 's/^batch_id=//p')
  if [[ -z "$batch_id" ]]; then echo "LAUNCH-FAILED $name: $out"; return 1; fi
  echo "BATCH $name id=$batch_id"
  local token resumed=0 stall=0 last_done=-1
  token=$(docker exec polymath_v33-backend-1 python -c "
from services.auth import AuthService
print(AuthService().create_access_token('6a132beafef900c17f87848e', 'Sambenja'))" 2>/dev/null | tail -1)
  while true; do
    local snap st cnt done_n
    snap=$(curl -fsS --max-time 30 -H "Authorization: Bearer $token" "http://localhost:8000/api/ingest-batches/$batch_id?include_items=false" 2>/dev/null) || { sleep 60; continue; }
    st=$(echo "$snap" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "?")
    cnt=$(echo "$snap" | python3 -c "import json,sys; print(json.load(sys.stdin).get('counts'))" 2>/dev/null || echo "?")
    done_n=$(echo "$snap" | python3 -c "import json,sys; c=json.load(sys.stdin)['counts']; print(c.get('done',0)+c.get('staged',0))" 2>/dev/null || echo 0)
    case "$st" in
      done|partial) echo "DONE $name ($st) $cnt"; return 0 ;;
      failed|cancelled)
        if [[ "$resumed" == 0 ]]; then
          resumed=1
          echo "STOPPED $name ($st) — attempting one resume: $cnt"
          curl -fsS --max-time 60 -X POST -H "Authorization: Bearer $token" "http://localhost:8000/api/ingest-batches/$batch_id/resume" >/dev/null 2>&1
        else
          echo "FAILED $name after resume: $cnt — moving on (durable, resumable later)"
          return 1
        fi ;;
    esac
    # stall detection: no progress for 30 min → emit once per corpus
    if [[ "$done_n" == "$last_done" ]]; then stall=$((stall+1)); else stall=0; last_done="$done_n"; fi
    if [[ "$stall" == 30 ]]; then echo "STALL-WARNING $name no progress 30min at $cnt"; fi
    sleep 60
  done
}

echo "FLEET-START $(date -u +%H:%MZ)"
# Phase 0: wait for the in-flight books batch to close before GPU-serial transcripts
BOOKS_BATCH=9dc27284-5012-44c0-84f9-864bb8193062
TOK=$(docker exec polymath_v33-backend-1 python -c "
from services.auth import AuthService
print(AuthService().create_access_token('6a132beafef900c17f87848e', 'Sambenja'))" 2>/dev/null | tail -1)
while true; do
  BST=$(curl -fsS --max-time 30 -H "Authorization: Bearer $TOK" "http://localhost:8000/api/ingest-batches/$BOOKS_BATCH?include_items=false" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "?")
  case "$BST" in done|partial|failed|cancelled) echo "BOOKS-CLOSED ($BST)"; break ;; esac
  sleep 120
done
run_corpus video_generations_schools /ingest-source/Rag/Video_generations_school "" 6
run_corpus markbuildsbrands_transcripts /ingest-source/markbuildsbrands_transcripts "" 6
run_corpus meta-andromeda-rag /ingest-source/Rag/meta-andromeda-rag "" 6
run_corpus cybersecurity_study /ingest-source/Rag/CyberSecurity_study "" 4 runpod_extract_first
IDX=$(cat "$ACTIVE_IDX_FILE")
echo "FLEET-COMPLETE $(date -u +%H:%MZ) final-balances k1=\$$(balance "${KEYS[0]}") k2=\$$(balance "${KEYS[1]}") k3=\$$(balance "${KEYS[2]}")"
