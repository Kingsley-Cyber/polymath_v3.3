#!/usr/bin/env bash
set -euo pipefail

# This script is the child of run_claims_owner_window_locked_command.py.
# The parent owns /tmp/polymath-eval.lock across the complete OFF -> ON
# transition, including any fail-closed rollback.

CURRENT_WT="/Users/king/polymath-wt/build-first-queue"
HARNESS_WT="/Users/king/polymath-wt/claims-owner-window-harness"
CANONICAL_REPO="/Users/king/polymath_v3.3"
BACKEND_CONTAINER="polymath_v33-backend-1"
LOCK_OWNER="codex/build-first-queue-20260718"

OFF_HOST="${CURRENT_WT}/docs/baselines/BUILD_FIRST_CLAIMS_FRESH_OFF_6_2026-07-18.json"
ON_HOST="${CURRENT_WT}/docs/baselines/BUILD_FIRST_CLAIMS_ON_REPLAY_6_GREEN_2026-07-18.json"
OFF_LOG_HOST="${CURRENT_WT}/docs/baselines/BUILD_FIRST_CLAIMS_FRESH_OFF_6_2026-07-18.log"
ON_LOG_HOST="${CURRENT_WT}/docs/baselines/BUILD_FIRST_CLAIMS_ON_REPLAY_6_2026-07-18.log"
OFF_CONTAINER="/tmp/build_first_claims_fresh_off_6.json"
ON_CONTAINER="/tmp/build_first_claims_on_replay_6.json"
OFF_LOG_CONTAINER="/tmp/build_first_claims_fresh_off_6.log"
ON_LOG_CONTAINER="/tmp/build_first_claims_on_replay_6.log"

CLAIMS_ON_DEPLOYED=0
WINDOW_COMPLETE=0

require_outer_window() {
  test "${POLYMATH_EVAL_OUTER_LOCK_ATTESTED:-}" = "1"
  test "${POLYMATH_EVAL_LOCK_OWNER:-}" = "${LOCK_OWNER}"
  test -n "${POLYMATH_EVAL_WINDOW_NONCE:-}"
  test -n "${POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC:-}"
  test "$(tr -d '\n' < /tmp/polymath-eval.lock)" = "${LOCK_OWNER}"
  test "$(tr -d '\n' < /tmp/polymath-eval.lock.nonce)" = "${POLYMATH_EVAL_WINDOW_NONCE}"
}

compose_backend() {
  local claims_enabled="$1"
  RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED=true \
  ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED=true \
  TEMPORAL_QUERY_ROUTING_ENABLED=true \
  ATOMIC_CLAIM_ANCHORS_ENABLED="${claims_enabled}" \
  TWO_LANE_ANCHORING=false \
  TWO_LANE_ANCHORING_ENABLED=false \
  GROUNDED_QUERY_PLANNER_ENABLED=false \
  FOUR_LANE_TIER0_ROUTER_ENABLED=false \
  FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED=false \
  WATERFALL_ASSEMBLY=false \
  docker compose \
    -p polymath_v33 \
    --env-file "${CANONICAL_REPO}/.env" \
    -f "${CURRENT_WT}/docker-compose.yml" \
    -f "${CANONICAL_REPO}/docker-compose.override.yml" \
    -f "${CURRENT_WT}/docker-compose.offline-ingest.yml" \
    -f "${CURRENT_WT}/docker-compose.apple-mlx.yml" \
    -f "${CURRENT_WT}/docker-compose.daily.yml" \
    -f "${CURRENT_WT}/docker-compose.claim-anchor-eval.yml" \
    up -d --build backend
}

wait_backend_healthy() {
  local attempts=0
  while [ "${attempts}" -lt 90 ]; do
    if [ "$(docker inspect -f '{{.State.Health.Status}}' "${BACKEND_CONTAINER}" 2>/dev/null || true)" = "healthy" ]; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 2
  done
  docker logs --tail 120 "${BACKEND_CONTAINER}" >&2 || true
  return 1
}

install_harness() {
  docker cp \
    "${HARNESS_WT}/backend/scripts/run_claim_anchor_owner_window.py" \
    "${BACKEND_CONTAINER}:/app/scripts/run_claim_anchor_owner_window.py"
  docker cp \
    "${HARNESS_WT}/backend/evals/canonical_refusal_contract.py" \
    "${BACKEND_CONTAINER}:/app/evals/canonical_refusal_contract.py"
}

attest_runtime() {
  docker exec "${BACKEND_CONTAINER}" env PYTHONPATH=/app python -c \
    'import json; from config import get_settings; from evals.canonical_refusal_contract import snapshot_claims_retrieval_runtime; print(json.dumps(snapshot_claims_retrieval_runtime(get_settings()), sort_keys=True))'
}

rollback_if_needed() {
  local exit_code="$?"
  if [ "${exit_code}" -ne 0 ] && [ "${CLAIMS_ON_DEPLOYED}" = "1" ] && [ "${WINDOW_COMPLETE}" = "0" ]; then
    echo "CLAIMS_WINDOW_ROLLBACK=begin"
    compose_backend false || true
    wait_backend_healthy || true
    echo "CLAIMS_WINDOW_ROLLBACK=claims_false"
  fi
  return "${exit_code}"
}
trap rollback_if_needed EXIT

require_outer_window

for path in "${OFF_HOST}" "${ON_HOST}" "${OFF_LOG_HOST}" "${ON_LOG_HOST}"; do
  if [ -e "${path}" ]; then
    echo "CLAIMS_WINDOW_ABORT=artifact_exists:${path}" >&2
    exit 73
  fi
done

install_harness
echo "CLAIMS_RUNTIME_OFF=$(attest_runtime)"

docker exec "${BACKEND_CONTAINER}" env PYTHONPATH=/app \
  python /app/scripts/run_eval_with_embedder_preflight.py -- \
  python -c 'print("CLAIMS_EMBEDDER_PREFLIGHT=ready")'

docker exec \
  -e POLYMATH_EVAL_OUTER_LOCK_ATTESTED \
  -e POLYMATH_EVAL_LOCK_OWNER \
  -e POLYMATH_EVAL_WINDOW_NONCE \
  -e POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC \
  "${BACKEND_CONTAINER}" \
  sh -c \
  'PYTHONPATH=/app python /app/scripts/run_claim_anchor_owner_window.py capture-off --output /tmp/build_first_claims_fresh_off_6.json --base http://127.0.0.1:8000 > /tmp/build_first_claims_fresh_off_6.log 2>&1; echo EXIT=$? >> /tmp/build_first_claims_fresh_off_6.log'

docker cp "${BACKEND_CONTAINER}:${OFF_LOG_CONTAINER}" "${OFF_LOG_HOST}"
off_exit="$(tail -n 1 "${OFF_LOG_HOST}")"
echo "CLAIMS_OFF_TRUE_EXIT=${off_exit}"
if [ "${off_exit}" != "EXIT=0" ]; then
  tail -n 120 "${OFF_LOG_HOST}" >&2
  exit 74
fi
docker cp "${BACKEND_CONTAINER}:${OFF_CONTAINER}" "${OFF_HOST}"
off_sha="$(sha256sum "${OFF_HOST}" | awk '{print $1}')"
echo "CLAIMS_OFF_SHA256=${off_sha}"

compose_backend true
CLAIMS_ON_DEPLOYED=1
wait_backend_healthy
install_harness
docker cp "${OFF_HOST}" "${BACKEND_CONTAINER}:${OFF_CONTAINER}"
echo "CLAIMS_RUNTIME_ON=$(attest_runtime)"

docker exec \
  -e POLYMATH_EVAL_OUTER_LOCK_ATTESTED \
  -e POLYMATH_EVAL_LOCK_OWNER \
  -e POLYMATH_EVAL_WINDOW_NONCE \
  -e POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC \
  "${BACKEND_CONTAINER}" \
  sh -c \
  "PYTHONPATH=/app python /app/scripts/run_claim_anchor_owner_window.py replay-on --off-artifact ${OFF_CONTAINER} --off-artifact-sha256 ${off_sha} --output ${ON_CONTAINER} > ${ON_LOG_CONTAINER} 2>&1; echo EXIT=\$? >> ${ON_LOG_CONTAINER}"

docker cp "${BACKEND_CONTAINER}:${ON_LOG_CONTAINER}" "${ON_LOG_HOST}"
on_exit="$(tail -n 1 "${ON_LOG_HOST}")"
echo "CLAIMS_ON_TRUE_EXIT=${on_exit}"
if [ "${on_exit}" != "EXIT=0" ]; then
  tail -n 120 "${ON_LOG_HOST}" >&2
  exit 75
fi
docker cp "${BACKEND_CONTAINER}:${ON_CONTAINER}" "${ON_HOST}"
on_sha="$(sha256sum "${ON_HOST}" | awk '{print $1}')"
echo "CLAIMS_ON_SHA256=${on_sha}"
echo "CLAIMS_RUNTIME_FINAL=$(attest_runtime)"

WINDOW_COMPLETE=1
trap - EXIT
echo "CLAIMS_ATOMIC_WINDOW=GREEN"
