# Owner Manual Ingestion Runbook — 2026-07-17

Status: SENIOR DRAFT — section 3's exact command is EXECUTOR-FILL; Codex
completes and live-verifies it in the READY receipt before first owner use.
Everything else below is receipted fact from the certified E2E program.

## 1. What this pathway is

The validated main ingestion pathway (owner-designated): your files →
chunking → RunPod extraction (GLiNER/spaCy, locked custom image by
immutable digest, deterministic runtime) → local MLX embedding → Qdrant +
Mongo + Neo4j writes (corpus-qualified composite identities) → API
summaries. Certified by the 15-book E2E: 15/15 verified, 595/595 RunPod
jobs, five production bugs fixed with permanent invariants.

## 2. Before you start (preconditions)

- No live eval running (evals and ingestion fight over the one Metal GPU).
  Check: `ps aux | grep run_two_lane` (and siblings) — must be empty; the
  channel's last entry should show the eval lock released.
- Backend healthy: `curl -s localhost:8000/api/health` → 200.
- Sidecars healthy: `curl -s localhost:8082/health` (embedder),
  `:8081/health` (reranker), `:8084/health` (Ghost B) — all status ok.
- RunPod green endpoints live (Codex verifies in the READY receipt; if a
  worker probe fails, ingestion still queues — jobs wait, nothing is lost).

## 3. The command

The API accepts any folder readable by the backend and ingest-worker
containers. On this Mac, `/Volumes/Flash Drive` is mounted read-only as
`/ingest-source`, so host folder `/Volumes/Flash Drive/my_books` is passed as
`/ingest-source/my_books`. Files elsewhere must first be copied under the
configured `POLYMATH_INGEST_SOURCE_ROOT`; do not change mounts or recreate the
stack while an ingest is active. The certified types for this command are
`.pdf`, `.md`, and `.epub`; subfolders are included and hidden/AppleDouble
files are ignored by production discovery.

Copy this whole block. Change only `CORPUS_NAME`, `SOURCE_PATH`, and
`SUMMARIES`. Use a new, unique corpus name. `SUMMARIES=on` runs the complete
validated path and reserves a hard summary ceiling of `$0.50 × file count`
(measured average is about `$0.23/book`); `SUMMARIES=off` makes no summary API
calls. The state file contains only corpus and batch IDs, never credentials.
Re-running the same block with the same three values resumes that durable
batch.

```bash
set -Eeuo pipefail

cd /Users/king/polymath_v3.3
CORPUS_NAME="${CORPUS_NAME:-owner_books_20260717}"
SOURCE_PATH="${SOURCE_PATH:-/ingest-source/owner_books_20260717}"
SUMMARIES="${SUMMARIES:-on}"       # on | off
DRY_RUN="${DRY_RUN:-0}"            # 1 parses/discovers only; no API writes
BACKEND="${BACKEND:-polymath_v33-backend-1}"

case "$SUMMARIES" in
  on)  SUMMARIES_JSON=true ;;
  off) SUMMARIES_JSON=false ;;
  *) echo "SUMMARIES must be on or off" >&2; exit 2 ;;
esac
[[ -n "$CORPUS_NAME" && ${#CORPUS_NAME} -le 200 ]] ||
  { echo "CORPUS_NAME must contain 1-200 characters" >&2; exit 2; }
[[ "$SOURCE_PATH" == /* ]] ||
  { echo "SOURCE_PATH must be an absolute container path" >&2; exit 2; }
docker inspect "$BACKEND" >/dev/null

# Use the production discovery function: recursive, stable ordering, and
# automatic exclusion of hidden/AppleDouble files.
FILE_COUNT="$(
  docker exec -e PYTHONPATH=/app -w /app "$BACKEND" \
    python -c 'import sys; from services.ingestion.batches import discover_local_files; _, rows = discover_local_files(sys.argv[1], recursive=True, extensions=[".pdf", ".md", ".epub"]); print(len(rows))' \
    "$SOURCE_PATH"
)"
[[ "$FILE_COUNT" =~ ^[0-9]+$ && "$FILE_COUNT" -gt 0 ]] ||
  { echo "No supported files found under $SOURCE_PATH" >&2; exit 2; }
SUMMARY_AUTHORITY_USD="$(awk -v n="$FILE_COUNT" 'BEGIN {printf "%.2f", n * 0.50}')"

# This is the exact frozen corpus contract used by the certified 15-book E2E.
CORPUS_BODY="$(
  jq -nc \
    --arg name "$CORPUS_NAME" \
    --argjson summaries "$SUMMARIES_JSON" \
    '{
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
        runpod_local_extraction_routes: [
          {account_name: "primary", endpoint_id: "hk81nfl5cnwufx"},
          {account_name: "secondary", endpoint_id: "8tafde7potcsjw"}
        ],
        models_linked: false,
        extraction_models: [],
        summary_models: [{
          provider_preset: "deepseek",
          model: "deepseek/deepseek-v4-flash",
          base_url: "https://api.deepseek.com",
          max_concurrent: 24,
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
  jq -nc \
    --arg root "$SOURCE_PATH" \
    --arg authority "$SUMMARY_AUTHORITY_USD" \
    --argjson summaries "$SUMMARIES_JSON" \
    '{
      root_path: $root,
      profile: "runpod_burst",
      recursive: true,
      extensions: [".pdf", ".md", ".epub"],
      store_files: true,
      use_neo4j: true,
      chunk_summarization: $summaries,
      model: "",
      concurrency: 1,
      start: true
    } + (if $summaries then
      {summary_cost_authority_usd: $authority}
    else {} end)'
)"

# No-write verification mode. It runs production file discovery above and
# validates both request bodies through the same Pydantic classes as the API.
if [[ "$DRY_RUN" == 1 ]]; then
  printf '%s\n%s\n' "$CORPUS_BODY" "$BATCH_BODY" |
    docker exec -i -e PYTHONPATH=/app -w /app "$BACKEND" \
      python -c 'import json, sys; from models.schemas import CorpusCreate; from routers.ingestion import LocalIngestBatchRequest; rows = [json.loads(line) for line in sys.stdin if line.strip()]; corpus = CorpusCreate.model_validate(rows[0]); batch = LocalIngestBatchRequest.model_validate(rows[1]); print(f"DRY_RUN_OK corpus={corpus.name!r} wire={corpus.default_ingestion_config.runpod_wire_contract} files={sys.argv[1]} summaries={batch.chunk_summarization}")' \
      "$FILE_COUNT"
  exit 0
fi

# Authenticate without printing the password or bearer token. Never run this
# block with `set -x`.
ADMIN_USERNAME="$(sed -n 's/^DEFAULT_ADMIN_USERNAME=//p' .env | tail -1)"
ADMIN_PASSWORD="$(sed -n 's/^DEFAULT_ADMIN_PASSWORD=//p' .env | tail -1)"
[[ -n "$ADMIN_USERNAME" && -n "$ADMIN_PASSWORD" ]] ||
  { echo "Admin login settings are missing from .env" >&2; exit 1; }
TOKEN="$(
  jq -nc --arg username "$ADMIN_USERNAME" --arg password "$ADMIN_PASSWORD" \
    '{username:$username,password:$password}' |
    curl -fsS --max-time 15 -H 'Content-Type: application/json' \
      --data-binary @- http://localhost:8000/api/auth/login |
    jq -er '.access_token | select(length > 0)'
)"
unset ADMIN_PASSWORD
trap 'unset TOKEN' EXIT

STATE_KEY="$(printf '%s' "$CORPUS_NAME" | tr -cs 'A-Za-z0-9._-' '_')"
STATE_DIR="$HOME/PolymathRuntime/manual-ingest-state"
STATE="$STATE_DIR/$STATE_KEY.json"
mkdir -p "$STATE_DIR"
umask 077
CORPUS_ID=""
BATCH_ID=""

write_state() {
  local temporary="$STATE.tmp"
  jq -n \
    --arg corpus_name "$CORPUS_NAME" \
    --arg source_path "$SOURCE_PATH" \
    --arg summaries "$SUMMARIES" \
    --arg corpus_id "$CORPUS_ID" \
    --arg batch_id "$BATCH_ID" \
    '{corpus_name:$corpus_name,source_path:$source_path,summaries:$summaries,corpus_id:$corpus_id,batch_id:$batch_id}' \
    > "$temporary"
  mv "$temporary" "$STATE"
}

if [[ -s "$STATE" ]]; then
  [[ "$(jq -er .corpus_name "$STATE")" == "$CORPUS_NAME" &&
     "$(jq -er .source_path "$STATE")" == "$SOURCE_PATH" &&
     "$(jq -er .summaries "$STATE")" == "$SUMMARIES" ]] ||
    { echo "State inputs differ; restore the original values or choose a new corpus name" >&2; exit 1; }
  CORPUS_ID="$(jq -er .corpus_id "$STATE")"
  BATCH_ID="$(jq -r '.batch_id // ""' "$STATE")"
else
  EXISTING_COUNT="$(
    curl -fsS --max-time 15 \
      -H "Authorization: Bearer $TOKEN" \
      http://localhost:8000/api/corpora |
      jq --arg name "$CORPUS_NAME" '[.[] | select(.name == $name)] | length'
  )"
  [[ "$EXISTING_COUNT" == 0 ]] ||
    { echo "Corpus name already exists but no local state file was found; stop and recover its ID" >&2; exit 1; }
  CORPUS_ID="$(
    curl -fsS --max-time 60 -X POST \
      -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' \
      --data-binary "$CORPUS_BODY" \
      http://localhost:8000/api/corpora |
      jq -er '.corpus_id | select(length > 0)'
  )"
  write_state
fi

if [[ -z "$BATCH_ID" ]]; then
  BATCH_RESPONSE="$(
    curl -fsS --max-time 600 -X POST \
      -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' \
      --data-binary "$BATCH_BODY" \
      "http://localhost:8000/api/corpora/$CORPUS_ID/ingest-batches/local"
  )"
  jq -e --argjson expected "$FILE_COUNT" '.batch_id and .total == $expected' \
    <<<"$BATCH_RESPONSE" >/dev/null
  BATCH_ID="$(jq -er '.batch_id | select(length > 0)' <<<"$BATCH_RESPONSE")"
  write_state
else
  curl -fsS --max-time 60 -X POST \
    -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8000/api/ingest-batches/$BATCH_ID/resume" >/dev/null
fi

echo "corpus_id=$CORPUS_ID"
echo "batch_id=$BATCH_ID"
echo "state=$STATE"
echo "Ctrl-C stops only this watcher; the durable batch keeps running."

while true; do
  SNAPSHOT="$(
    curl -fsS --max-time 30 \
      -H "Authorization: Bearer $TOKEN" \
      "http://localhost:8000/api/ingest-batches/$BATCH_ID?include_items=false"
  )"
  jq '{status,total,counts,updated_at}' <<<"$SNAPSHOT"
  case "$(jq -r .status <<<"$SNAPSHOT")" in
    done) break ;;
    failed|cancelled) echo "Batch stopped in a failure state" >&2; exit 1 ;;
  esac
  sleep 30
done
```

Worked example: put books in host folder
`/Volumes/Flash Drive/owner_books_20260717`, open a Bash shell, and paste the
block unchanged. Its defaults create corpus `owner_books_20260717`, discover
the same folder inside Docker at `/ingest-source/owner_books_20260717`, and
run with summaries on.

Before first use, the exact no-write check is the same invocation with
`DRY_RUN=1`. A successful check ends with `DRY_RUN_OK`; it does not
authenticate, create a corpus, create a batch, call a provider, or deploy
anything.

## 4. Cost and time expectations (measured, per book)

- Extraction: ~$0.04–0.07 (RunPod, rate-model), ~2–4 min active at
  batch 32–64 on the green fleet.
- Summaries: ~$0.23 average (API lane; provider-usage-accounted).
- All-in E2E measurement: 15 books = $4.46.

## 5. Watching progress

- Ingest jobs are durable and journaled; re-attach any time.
- Extraction fleet truth = Mongo settings endpoints[].enabled (not env).
- Chat lane must stay usable during ingestion, but expect slower embeds
  while extraction embeds batch (the GPU arbiter fixes this after deploy).

## 6. Safety guarantees (engraved invariants — why re-running is safe)

- Never-write-less resume: a resume can never erase information already
  in the durable store.
- Verified-duplicate skip: only documents with write_state.verified=true
  are skipped as duplicates; incomplete ingests resume, not skip.
- Composite (corpus_id, content_id) identity: a new ingest can never
  steal or overwrite another corpus's documents, chunks, or facts.
- Bounded graph transactions (100-row) — no OOM partial-graph states.
- Fail-closed refusals name their guard; noise is excluded at mention
  granularity, never by failing a whole document.
→ Practical meaning: if anything stops mid-run, RUN THE SAME COMMAND
AGAIN. It resumes exactly where it left off.

## 7. Do NOT do while a batch is running

- No live evals, no backend rebuilds/recreates, no sidecar restarts.
- Don't toggle retrieval flags mid-batch (flag flips happen in their own
  verified windows).

## 8. Current retrieval flag state you're ingesting into

relationship allocation ON (verified) · corpus_scope.v2 refusal ON
(verified) · temporal OFF (exonerated; flips on owner word) · claims OFF
(proven; flips on owner word) · router/waterfall/two-lane dark.
