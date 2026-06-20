#!/usr/bin/env bash
# Post-deploy guard against the SILENT embedder/reranker misw iring that happens
# when the backend is recreated WITHOUT the compose override (see CLAUDE.md).
#
# Unlike scripts/smoke_apple_mlx.sh (which checks the HOST sidecars are up), this
# checks that the backend CONTAINER is actually WIRED to them — the gap that let
# a bad `docker compose -f docker-compose.yml up backend` pass smoke yet return
# vector=0. Run after EVERY backend (re)deploy. Exits non-zero with the fix.
#
# Usage: bash scripts/verify_backend_runtime.sh [container_name]
set -uo pipefail

CONTAINER="${1:-${POLYMATH_BACKEND_CONTAINER:-polymath_v33-backend-1}}"
fail=0

echo "[verify] backend container: ${CONTAINER}"

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
  echo "[verify] FAIL: container '${CONTAINER}' not found — start the stack first (see CLAUDE.md)." >&2
  exit 2
fi

# 1) Resolved endpoints must not be the dead compose defaults.
urls="$(docker exec "${CONTAINER}" sh -c 'printf "%s|%s" "$EMBEDDER_URL" "$RERANKER_URL"' 2>/dev/null || true)"
emb="${urls%%|*}"; rer="${urls##*|}"
echo "[verify] EMBEDDER_URL=${emb:-<unset>}"
echo "[verify] RERANKER_URL=${rer:-<unset>}"
case "${emb}" in
  ""|*embedder:80*) echo "[verify] FAIL: EMBEDDER_URL is the dead compose default — the override was dropped." >&2; fail=1 ;;
esac
case "${rer}" in
  ""|*reranker:8080*) echo "[verify] WARN: RERANKER_URL is the compose default; rerank will fall back to score-sort." >&2 ;;
esac

# 2) A live embed through the backend must return a real vector.
dim="$(docker exec "${CONTAINER}" python -c '
import asyncio
try:
    from services.embedder import embed_query
    v = asyncio.run(embed_query("polymath runtime verify"))
    print(len(v or []))
except Exception as e:
    print("ERR:" + repr(e)[:160])
' 2>/dev/null | tail -1)"
echo "[verify] live embed dim: ${dim:-<none>}"
case "${dim}" in
  ""|0|ERR:*|*[!0-9]*) echo "[verify] FAIL: backend cannot embed — vector retrieval will return nothing." >&2; fail=1 ;;
esac

if [ "${fail}" -ne 0 ]; then
  cat >&2 <<'EOF'

[verify] BACKEND RUNTIME WIRING IS BROKEN.
  The backend was likely recreated WITHOUT the compose override, so it points at
  the dead default embedder:80 / reranker:8080. Vector/Hybrid/Graph retrieval
  returns nothing while the container reports "healthy".

  FIX (Mac):
    docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build backend
    # or:  bash scripts/setup_apple_mlx.sh
  NEVER:  docker compose -f docker-compose.yml up backend   (drops the override)

  Then re-run this script.
EOF
  exit 1
fi

echo "[verify] OK: backend runtime wiring healthy (embedder reachable, dim=${dim})."
exit 0
