#!/bin/sh
for attempt in $(seq 1 30); do
  backend=$(docker inspect polymath_v33-backend-1 --format '{{.State.Health.Status}}' 2>/dev/null || true)
  worker=$(docker inspect polymath_v33-ingest-worker-1 --format '{{.State.Health.Status}}' 2>/dev/null || true)
  if [ "$backend" = "healthy" ] && [ "$worker" = "healthy" ]; then
    echo "backend=$backend worker=$worker attempts=$attempt"
    exit 0
  fi
  sleep 2
done
echo "backend=$backend worker=$worker"
exit 1
