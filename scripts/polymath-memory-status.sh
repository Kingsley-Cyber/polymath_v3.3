#!/usr/bin/env bash
set -euo pipefail

echo "== macOS memory pressure =="
memory_pressure

echo
echo "== top resident processes =="
ps -axo pid,%mem,rss,comm,args | sort -k2 -nr | head -20

echo
echo "== docker container memory =="
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' 2>/dev/null || true

echo
echo "== host sidecars =="
launchctl list | rg -i 'polymath|hermes|reranker|ghost' || true
