#!/usr/bin/env bash
set -euo pipefail
PACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="${1:-$(cd "$PACK_ROOT/.." && pwd)}"
python3 "$PACK_ROOT/controller.py" init --repo-root "$REPO_ROOT"
python3 "$PACK_ROOT/controller.py" verify-pack
python3 "$PACK_ROOT/controller.py" next
