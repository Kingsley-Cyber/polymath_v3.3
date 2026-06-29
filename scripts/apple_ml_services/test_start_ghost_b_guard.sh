#!/usr/bin/env bash
# Self-asserting test for start.sh's Ghost B handling:
#   * START_GHOST_B_EXTRACT now defaults ON (a fresh clone ingests out of the box)
#   * but a MISSING venv interpreter must warn + SKIP, never start_service —
#     otherwise the supervisor's "any dead child => exit 1" rule would crash-loop
#     the whole group and take the embedder/reranker down with it.
#
# Exits non-zero on any failed assertion. Spawns NO real MLX services: every
# real sidecar is disabled and the runtime root is redirected to a temp dir, so
# this is safe to run on a machine whose apple-ml LaunchAgent is live.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${HERE}/start.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# Disable every real sidecar and redirect the runtime root so real pid files are
# untouched. `env -u START_GHOST_B_EXTRACT` clears any inherited value so each
# case exercises exactly the default-or-override it intends.
run() {
  env -u START_GHOST_B_EXTRACT \
      POLYMATH_DOCKER_DATA_ROOT="${TMP}/runtime" \
      START_EMBEDDER=false START_RERANKER=false \
      START_DOCLING=false START_SLM_ENRICH=false \
      "$@" bash "${SCRIPT}"
}

# 1. syntax
bash -n "${SCRIPT}" || fail "bash -n syntax"
pass "syntax"

# 2. venv MISSING + explicitly enabled -> warn, skip, never start, exit 1
out="$(run START_GHOST_B_EXTRACT=true GHOST_B_EXTRACT_PY="${TMP}/absent/python" 2>&1)"; rc=$?
grep -q "WARNING: ghost_b_extract is enabled" <<<"${out}" || fail "missing venv: no WARNING -> ${out}"
grep -q "skipping ghost_b_extract"            <<<"${out}" || fail "missing venv: not skipped -> ${out}"
grep -q "starting ghost_b_extract"            <<<"${out}" && fail "missing venv: must NOT start -> ${out}"
[ "${rc}" -eq 1 ] || fail "missing venv: expected exit 1 (no sidecars), got ${rc}"
pass "venv missing -> warn + skip, no start (embedder/reranker safe)"

# 3. default is now ON: with START_GHOST_B_EXTRACT unset, ghost_b is enabled
out="$(run GHOST_B_EXTRACT_PY="${TMP}/absent/python" 2>&1)"; rc=$?
grep -q "WARNING: ghost_b_extract is enabled" <<<"${out}" \
  || fail "default-on: unset env should ENABLE ghost_b -> ${out}"
pass "default flipped ON (unset env enables ghost_b, then guard skips missing venv)"

# 4. explicit opt-out still works: disabled -> plain skip, no warning
out="$(run START_GHOST_B_EXTRACT=false GHOST_B_EXTRACT_PY="${TMP}/absent/python" 2>&1)"; rc=$?
grep -q "skipping ghost_b_extract" <<<"${out}" || fail "opt-out: expected skip -> ${out}"
grep -q "WARNING"                  <<<"${out}" && fail "opt-out: should not warn -> ${out}"
pass "START_GHOST_B_EXTRACT=false -> clean skip, no warning"

# 5. venv PRESENT -> guard allows start_service. /usr/bin/true exits at once so
#    the supervisor detects the dead child and returns within a few seconds; we
#    only assert the synchronous 'starting' line that precedes the launch.
out="$(run START_GHOST_B_EXTRACT=true GHOST_B_EXTRACT_PY=/usr/bin/true GHOST_B_EXTRACT_PORT=18084 2>&1)"; rc=$?
grep -q "starting ghost_b_extract" <<<"${out}" || fail "present venv: did not start -> ${out}"
grep -q "WARNING"                  <<<"${out}" && fail "present venv: should not warn -> ${out}"
pass "venv present -> starts ghost_b_extract"

echo "ALL PASS"
