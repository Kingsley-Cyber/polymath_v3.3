#!/usr/bin/env bash
# Post-deploy guard against SILENT embedder/reranker miswiring.
#
# Unlike scripts/smoke_apple_mlx.sh (which checks the HOST sidecars are up), this
# checks that the backend CONTAINER can actually embed through whatever runtime
# it was configured for: in-cluster CUDA services on RTX, or host-native MLX
# sidecars on Apple Silicon. Run after backend redeploys. Exits non-zero if
# vector retrieval would be broken.
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

# 1) Print resolved endpoints. Both internal Docker URLs (RTX) and
# host.docker.internal URLs (Apple MLX) are valid if the live embed check passes.
urls="$(docker exec "${CONTAINER}" sh -c 'printf "%s|%s" "$EMBEDDER_URL" "$RERANKER_URL"' 2>/dev/null || true)"
emb="${urls%%|*}"; rer="${urls##*|}"
echo "[verify] EMBEDDER_URL=${emb:-<unset>}"
echo "[verify] RERANKER_URL=${rer:-<unset>}"
[[ -n "${emb}" ]] || { echo "[verify] FAIL: EMBEDDER_URL is empty." >&2; fail=1; }
[[ -n "${rer}" ]] || echo "[verify] WARN: RERANKER_URL is empty; rerank will fall back to score-sort." >&2

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
  The backend cannot produce embeddings through its configured runtime. Vector,
  Hybrid, and Graph retrieval will return empty or degraded results while the
  container may still report "healthy" because liveness is not retrieval quality.

  FIX (Apple MLX):
    docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build backend
    # or:  bash scripts/setup_apple_mlx.sh

  FIX (RTX/NVIDIA):
    bash scripts/bootstrap-runtime.sh --generate-secrets --stage-models
    docker compose up -d --build embedder backend

  Then re-run this script.
EOF
  exit 1
fi

echo "[verify] OK: backend runtime wiring healthy (embedder reachable, dim=${dim})."
exit 0
