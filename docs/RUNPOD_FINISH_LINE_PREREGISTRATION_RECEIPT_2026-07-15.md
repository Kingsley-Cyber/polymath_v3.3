# RunPod finish-line preregistration receipt — 2026-07-15

Status: **GREEN; frozen before first inference or test-corpus creation.**

## Scope and authority

This receipt freezes the same-chunk comparison, the deterministic 15-document
selection, and the three-tier retrieval evaluation for the owner-authorized
RunPod finish line. RunPod owns GLiNER/spaCy/Python extraction. API providers
remain the certified summary/digest lane. Existing corpora are outside this
test's write scope.

No inference, image promotion, endpoint deployment, provider call, corpus
creation, database write, graph write, or vector write occurred while creating
or verifying these artifacts.

## Frozen artifacts

| Artifact | SHA-256 | Contract |
|---|---|---|
| `backend/evals/runpod_same_chunk_lockdown_v1.json` | `a214bff374e5684e0f4b521eb042d6e253c4b28e07846cd7bfd935ce0cdde6f8` | 12 byte-identical tasks; exact structural fields; confidence tolerance `0.00001`; zero missing/extra/semantic mismatches; `relations=[]` |
| `backend/evals/runpod_e2e_15doc_selection_v1.json` | `da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00` | 15 documents from the 75-file owner source |
| `backend/evals/runpod_e2e_retrieval_preregister_v1.json` | `8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110` | 17 frozen queries across all three retrieval tiers = 51 executions |

Same-chunk source fixture SHA is
`866d1b2104c7bc7d3a5696462058053a6bb041c8bd6784eae59a1f390d5e7816`.
Its nine sample texts are supplemented by temporal, negation, and long-window
synthetics. Gold outputs are not loaded by the reference runner.

## Deterministic selection

The selector reads filenames and byte sizes—not document content or retrieval
gold. It builds five balanced 15-member filename-TF-IDF topic bands, then
chooses minimum/lower-median/maximum byte-size members from each. AppleDouble
files are excluded. The 75-file source-manifest SHA is
`fd3319adffb5ed3b07cce6c76ea16d1be022360b928cc7c0a2af42fa0f807329`.

The frozen selection intentionally retains tiny/stub/status-like files where
the deterministic procedure selected them; no post-selection hand-picking was
performed.

## Retrieval gates

The 17 queries comprise 5 direct-expert, 1 direct-fact, 4 lay-language, 4
relationship/multi-document, and 3 negative controls. Every query executes on
Qdrant-only, Qdrant+Mongo, and Qdrant+Mongo+Graph with top-k 10.

Promotion gates are: direct doc-hit `>=0.85`; lay-language doc-hit `>=0.75`;
relationship distinct-target rate `>=0.75`; negative refusal `=1.0`; corpus
boundary precision `=1.0`; citation source-membership `=1.0`; all tiers
required; zero writes to existing corpora. Targets and thresholds become
immutable on the first test-corpus query.

## Commands and true exits

```text
sh -c 'python3 scripts/verify_runpod_e2e_preregistration.py --json-out /tmp/runpod_e2e_prereg_verify_final.json > /tmp/runpod_e2e_prereg_verify_final.log 2>&1; echo EXIT=$? >> /tmp/runpod_e2e_prereg_verify_final.log'
EXIT=0
all_green=true; selection=15; source_hash_mismatches=[]; anchor_misses=[]; executions=51

sh -c 'python3 scripts/select_runpod_e2e_documents.py --out /tmp/runpod_e2e_selection_repeat_final.json > /tmp/runpod_e2e_selection_repeat_final.log 2>&1 && cmp -s backend/evals/runpod_e2e_15doc_selection_v1.json /tmp/runpod_e2e_selection_repeat_final.json; echo EXIT=$? >> /tmp/runpod_e2e_selection_repeat_final.log'
EXIT=0
original SHA=da7b94c1...; repeat SHA=da7b94c1...

python3 -m py_compile <three prep scripts>
python -m black --check <three prep scripts>
git diff --check
EXIT=0
```

## Rejected candidate history retained

- Unconstrained k-medoids failed because a topic band had fewer than three
  files: true `EXIT=1`, `/tmp/runpod_e2e_selection.log`.
- The first balanced run failed on host Python 3.9 because of the nonessential
  Python-3.10-only `zip(strict=True)` keyword: true `EXIT=1`,
  `/tmp/runpod_e2e_selection_balanced.log`.
- The final balanced algorithm and compatibility-only correction passed
  without changing targets, thresholds, or inspecting content.

## Remaining pre-E2E gates

The pinned local reference, immutable image identity, blue-green synthetic
canary, live same-chunk comparison, fresh-corpus create/ingest, and API summary
spend ceiling are not claimed here. Each remains a fail-closed gate before the
retrieval matrix can execute.
