#!/usr/bin/env bash
set -euo pipefail

manifest="backend/evals/runpod_e2e_15doc_selection_v1.json"
root="$(jq -r '.source_root' "$manifest")"
target="/Volumes/Flash Drive/runpod_e2e_15doc_20260715"

test ! -e "$target"
mkdir "$target"

jq -r '.selected[] | [.filename, .sha256] | @tsv' "$manifest" |
while IFS=$'\t' read -r filename expected; do
  test "${filename#._}" = "$filename"
  actual="$(shasum -a 256 "$root/$filename" | awk '{print $1}')"
  test "$actual" = "$expected"
  cp -p "$root/$filename" "$target/$filename"
  copied="$(shasum -a 256 "$target/$filename" | awk '{print $1}')"
  test "$copied" = "$expected"
  printf '%s  %s\n' "$copied" "$filename"
done

staged_count="$(find "$target" -type f ! -name '._*' | wc -l | tr -d ' ')"
appledouble_count="$(find "$target" -type f -name '._*' | wc -l | tr -d ' ')"
test "$staged_count" = 15
test "$appledouble_count" = 0
printf 'STAGED_COUNT=%s\nAPPLEDOUBLE_COUNT=%s\nTARGET=%s\n' \
  "$staged_count" "$appledouble_count" "$target"
