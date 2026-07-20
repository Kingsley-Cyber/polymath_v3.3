#!/bin/bash
# launch_ingest.sh — runbook-faithful launcher (frozen certified contract bodies,
# OWNER_MANUAL_INGESTION_RUNBOOK_2026-07-17.md) with direct token mint instead of
# the .env admin login (known-stale password). Resumable via the same state files.
# Usage: CORPUS_NAME=x SOURCE_PATH=/ingest-source/x [EXTEND_CORPUS_ID=id] ./launch_ingest.sh
set -Eeuo pipefail

CORPUS_NAME="${CORPUS_NAME:?}"
SOURCE_PATH="${SOURCE_PATH:?}"
SUMMARIES="${SUMMARIES:-on}"
CONCURRENCY="${CONCURRENCY:-1}"
PROFILE="${PROFILE:-runpod_burst}"
EXTEND_CORPUS_ID="${EXTEND_CORPUS_ID:-}"
BACKEND="polymath_v33-backend-1"

# GUARD (2026-07-19): ≥2 ingest workers or throughput is GIL-bound to 1 core.
WORKERS_UP="$(docker ps --format '{{.Names}}' | grep -c 'polymath_v33-ingest-worker' || true)"
if [[ "${WORKERS_UP:-0}" -lt 2 ]]; then
  echo "only $WORKERS_UP ingest worker(s) up — scaling to 2 (lease-safe)" >&2
  (cd /Users/king/polymath-wt/build-first-queue && docker compose -p polymath_v33 \
    -f docker-compose.yml -f /Users/king/polymath_v3.3/docker-compose.override.yml \
    -f docker-compose.offline-ingest.yml -f docker-compose.apple-mlx.yml -f docker-compose.daily.yml \
    --env-file /Users/king/polymath_v3.3/.env --profile offline-ingest \
    up -d --scale ingest-worker=2 --no-recreate ingest-worker >/dev/null 2>&1) || true
fi


case "$SUMMARIES" in
  on)  SUMMARIES_JSON=true ;;
  off) SUMMARIES_JSON=false ;;
  *) echo "SUMMARIES must be on or off" >&2; exit 2 ;;
esac


# ── THROUGHPUT PREFLIGHT (owner order 2026-07-19) ────────────────────────────
# A batch may NEVER launch throttled. Assert the full dispatch chain at
# launch time; build routes DYNAMICALLY from enabled+healthy accounts.
throughput_preflight() {
  local want="$CONCURRENCY"
  # 1. worker env governors must be >= requested file-concurrency
  for VAR in EXTRACTION_MAX_ACTIVE_DOCS INGEST_GLOBAL_MAX_DOCS INGEST_MAX_ACTIVE_JOBS INGEST_MAX_MODEL_PHASE_DOCS; do
    local val
    val="$(docker exec polymath_v33-ingest-worker-1 sh -c "printenv $VAR" 2>/dev/null || echo 0)"
    if [[ "${val:-0}" -lt "$want" ]]; then
      echo "PREFLIGHT FAIL: $VAR=$val < requested concurrency $want." >&2
      echo "Fix: set it in /Users/king/polymath_v3.3/.env (OFFLINE_ prefix where applicable) and recreate workers." >&2
      exit 3
    fi
  done
  # 2. routes = enabled accounts whose endpoint answers /health with a live worker
  ROUTES_JSON="$(docker exec -i polymath_v33-backend-1 python - 2>/dev/null <<'PYEOF'
import asyncio, os, json, httpx
import motor.motor_asyncio
async def main():
    from services.settings import settings_service
    cli = motor.motor_asyncio.AsyncIOMotorClient(os.environ["MONGODB_URI"])
    settings_service.attach(cli.get_default_database())
    routes = []
    for acct, key in await settings_service.get_system_runpod_flash_accounts():
        try:
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.get(f"https://api.runpod.ai/v2/{acct.endpoint_id}/health",
                                headers={"Authorization": f"Bearer {key}"})
            w = r.json().get("workers", {})
            jobs = r.json().get("jobs", {})
            alive = sum(int(w.get(k, 0)) for k in ("ready", "idle", "running", "initializing"))
            proven = int(jobs.get("completed", 0)) > 0
            zombie = int(w.get("unhealthy", 0)) > 0 and not proven
        except Exception:
            alive = 0; proven = False; zombie = True
        if alive >= 1 and (proven or not zombie):
            routes.append({"account_name": acct.name, "endpoint_id": acct.endpoint_id})
        else:
            print(f"# preflight: dropping DEAD lane {acct.name}/{acct.endpoint_id}", flush=True)
    print(json.dumps(routes))
asyncio.run(main())
PYEOF
)"
  ROUTES_JSON="$(echo "$ROUTES_JSON" | grep -v '^#' | tail -1)"
  local n
  n="$(echo "$ROUTES_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)"
  if [[ "${n:-0}" -lt 1 ]]; then
    echo "PREFLIGHT FAIL: no healthy extraction lanes." >&2; exit 3
  fi
  echo "preflight OK: governors>=$want, healthy lanes=$n"
}
throughput_preflight

FILE_COUNT="$(
  docker exec -e PYTHONPATH=/app -w /app "$BACKEND" \
    python -c 'import sys; from services.ingestion.batches import discover_local_files; _, rows = discover_local_files(sys.argv[1], recursive=True, extensions=[".pdf", ".md", ".epub"]); print(len(rows))' \
    "$SOURCE_PATH"
)"
[[ "$FILE_COUNT" =~ ^[0-9]+$ && "$FILE_COUNT" -gt 0 ]] || { echo "No files under $SOURCE_PATH" >&2; exit 2; }
SUMMARY_AUTHORITY_USD="$(awk -v n="$FILE_COUNT" 'BEGIN {printf "%.2f", n * 0.50}')"
echo "files=$FILE_COUNT authority=\$$SUMMARY_AUTHORITY_USD"

CORPUS_BODY="$(
  jq -nc --arg name "$CORPUS_NAME" --argjson summaries "$SUMMARIES_JSON" --argjson routes "$ROUTES_JSON" '{
    name: $name,
    description: "Owner manual ingest through certified RunPod LocalExtractionV1",
    default_ingestion_config: {
      preset: "deep",
      embedding_model: "Qwen/Qwen3-Embedding-0.6B",
      embedding_dimension: 1024,
      embedding_model_id: "qwen3-embedding-0.6b-v1",
      embed_mode: "local",
      extraction_engine: "runpod_flash",
      runpod_wire_contract: "local_extraction_v1",
      runpod_local_extraction_routes: $routes,
      models_linked: false,
      extraction_models: [],
      summary_models: [{
        provider_preset: "deepseek",
        model: "deepseek/deepseek-v4-flash",
        base_url: "https://api.deepseek.com",
        max_concurrent: 40,
        extra_params: {disable_thinking: true}
      }, {
        provider_preset: "longcat",
        model: "longcat/LongCat-2.0",
        base_url: "https://api.longcat.chat/openai/v1",
        max_concurrent: 40,
        extra_params: {disable_thinking: true}
      }],
      max_summary_tokens: 256,
      use_neo4j: true,
      chunk_summarization: $summaries,
      target_qdrant_collections: ["naive", "hrag", "graph"],
      docling_ocr_enabled: false
    }
  }'
)"
BATCH_BODY="$(
  jq -nc --arg root "$SOURCE_PATH" --arg authority "$SUMMARY_AUTHORITY_USD" --arg prof "$PROFILE" --argjson summaries "$SUMMARIES_JSON" --argjson conc "$CONCURRENCY" '{
    root_path: $root, profile: $prof, recursive: true,
    extensions: [".pdf", ".md", ".epub"], store_files: true, use_neo4j: true,
    chunk_summarization: $summaries, model: "", concurrency: $conc, start: true
  } + (if $summaries then {summary_cost_authority_usd: $authority} else {} end)'
)"

TOKEN="$(docker exec "$BACKEND" python -c "
from services.auth import AuthService
print(AuthService().create_access_token('6a132beafef900c17f87848e', 'Sambenja'))" 2>/dev/null | tail -1)"
[[ -n "$TOKEN" ]] || { echo "token mint failed" >&2; exit 1; }

STATE_KEY="$(printf '%s' "$CORPUS_NAME" | tr -cs 'A-Za-z0-9._-' '_')"
STATE_DIR="$HOME/PolymathRuntime/manual-ingest-state"
STATE="$STATE_DIR/$STATE_KEY.json"
mkdir -p "$STATE_DIR"; umask 077
CORPUS_ID=""; BATCH_ID=""

write_state() {
  jq -n --arg corpus_name "$CORPUS_NAME" --arg source_path "$SOURCE_PATH" \
    --arg summaries "$SUMMARIES" --arg corpus_id "$CORPUS_ID" --arg batch_id "$BATCH_ID" \
    '{corpus_name:$corpus_name,source_path:$source_path,summaries:$summaries,corpus_id:$corpus_id,batch_id:$batch_id}' \
    > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
}

if [[ -s "$STATE" ]]; then
  [[ "$(jq -er .corpus_name "$STATE")" == "$CORPUS_NAME" && "$(jq -er .source_path "$STATE")" == "$SOURCE_PATH" ]] \
    || { echo "state mismatch for $CORPUS_NAME" >&2; exit 1; }
  CORPUS_ID="$(jq -er .corpus_id "$STATE")"
  BATCH_ID="$(jq -r '.batch_id // ""' "$STATE")"
elif [[ -n "$EXTEND_CORPUS_ID" ]]; then
  CORPUS_ID="$EXTEND_CORPUS_ID"
  write_state
else
  EXISTING="$(curl -fsS --max-time 15 -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/corpora |
    jq --arg name "$CORPUS_NAME" '[.[] | select(.name == $name)] | length')"
  [[ "$EXISTING" == 0 ]] || { echo "corpus name exists w/o state; aborting" >&2; exit 1; }
  CORPUS_ID="$(curl -fsS --max-time 60 -X POST -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' --data-binary "$CORPUS_BODY" \
    http://localhost:8000/api/corpora | jq -er '.corpus_id | select(length > 0)')"
  write_state
fi

if [[ -z "$BATCH_ID" ]]; then
  BATCH_RESPONSE="$(curl -fsS --max-time 600 -X POST -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' --data-binary "$BATCH_BODY" \
    "http://localhost:8000/api/corpora/$CORPUS_ID/ingest-batches/local")"
  jq -e --argjson expected "$FILE_COUNT" '.batch_id and .total == $expected' <<<"$BATCH_RESPONSE" >/dev/null
  BATCH_ID="$(jq -er '.batch_id | select(length > 0)' <<<"$BATCH_RESPONSE")"
  write_state
else
  curl -fsS --max-time 60 -X POST -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8000/api/ingest-batches/$BATCH_ID/resume" >/dev/null || true
fi

echo "corpus_id=$CORPUS_ID"
echo "batch_id=$BATCH_ID"
