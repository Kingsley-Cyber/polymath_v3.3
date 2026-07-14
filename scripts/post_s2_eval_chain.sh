#!/bin/bash
cd /Users/king/polymath_v3.3
export TOKEN=$(docker exec polymath_v33-backend-1 cat /tmp/probe_token)
LOG=/tmp/post_s2_eval.log
echo "=== post-S2 3-tier regression $(date -u +%FT%TZ)" > $LOG
for TIER in qdrant_only qdrant_mongo qdrant_mongo_graph; do
  echo "--- tier $TIER start $(date -u +%FT%TZ)" >> $LOG
  python3 backend/scripts/run_heldout_eval.py --tier $TIER >> $LOG 2>&1
  echo "TIER_${TIER}_EXIT=$?" >> $LOG
  if [ -f docs/baselines/EVAL_2026-07-13_${TIER}.json ]; then
    mv docs/baselines/EVAL_2026-07-13_${TIER}.json docs/baselines/EVAL_2026-07-13_postS2_${TIER}.json
    echo "renamed -> EVAL_2026-07-13_postS2_${TIER}.json" >> $LOG
  fi
done
echo "CHAIN_DONE $(date -u +%FT%TZ)" >> $LOG
