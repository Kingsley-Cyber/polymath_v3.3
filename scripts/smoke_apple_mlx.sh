#!/usr/bin/env bash
# End-to-end smoke for the Apple Silicon MLX hybrid profile.
# Run after install_apple_mlx_runtime.sh + docker compose up.

set -euo pipefail

EMBED="${EMBEDDER_URL:-http://localhost:8082}"
RERANK="${RERANKER_URL:-http://localhost:8081}"
DOCLING="${DOCLING_URL:-http://localhost:8500}"

step() { printf "\n→ %s\n" "$1"; }

step "embedder /info"
curl -fsS "${EMBED}/info" | jq .

step "embedder /embeddings (1 input)"
curl -fsS "${EMBED}/embeddings" \
  -H "Content-Type: application/json" \
  -d '{"input":["hello polymath"]}' | jq '.data[0] | {dim: (.embedding | length), index}'

step "reranker /health"
curl -fsS "${RERANK}/health" | jq .

step "reranker /rerank — ordering check (relevant doc must score highest)"
RESPONSE=$(curl -fsS "${RERANK}/rerank" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "object-oriented design pattern",
    "documents": [
      "The decorator pattern adds responsibilities to objects dynamically.",
      "Lemonade recipe: lemons, water, sugar, ice.",
      "Composite pattern lets clients treat individual objects and compositions uniformly."
    ]
  }')
echo "${RESPONSE}" | jq .

# Score 0 + 2 should both exceed score 1 if the model is loaded properly.
DOC0=$(echo "${RESPONSE}" | jq '.scores[0]')
DOC1=$(echo "${RESPONSE}" | jq '.scores[1]')
DOC2=$(echo "${RESPONSE}" | jq '.scores[2]')
echo "  doc0=${DOC0} doc1=${DOC1} doc2=${DOC2}"
if awk "BEGIN{ exit !( ${DOC0} > ${DOC1} && ${DOC2} > ${DOC1} ) }"; then
  echo "  ✓ relevance ordering correct"
else
  echo "  ✗ ranker did not separate relevant from irrelevant — projector may not be loaded"
  echo "    (scaffold mode returns zeroes; replace reranker_mlx/main.py)"
fi

step "docling /health"
curl -fsS "${DOCLING}/health" | jq .

step "all checks done"
