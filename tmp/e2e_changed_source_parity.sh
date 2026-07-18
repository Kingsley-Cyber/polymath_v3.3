#!/bin/sh
set -eu
paths='models/schemas.py
services/ingestion/worker.py
services/ingestion_service.py
services/runpod_flash_extraction.py
services/runpod_local_extraction.py'
for path in $paths; do
  expected=$(shasum -a 256 "backend/$path" | awk '{print $1}')
  for container in polymath_v33-backend-1 polymath_v33-ingest-worker-1; do
    actual=$(docker exec "$container" sha256sum "/app/$path" | awk '{print $1}')
    if [ "$expected" != "$actual" ]; then
      echo "mismatch container=$container path=$path"
      exit 1
    fi
  done
done
echo "changed_source_files=5 containers=2 mismatches=0"
