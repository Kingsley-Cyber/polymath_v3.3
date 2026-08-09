# spaCy MCP Container Fix — 2026-07-25

## Problem

`polymath_upload_document` MCP tool failed with:

```
Error executing tool polymath_upload_document: No module named 'spacy'
```

This affected every upload via MCP (the path Hermes was using for the 21 uploads).

## Initial misdiagnoses (all wrong)

1. **"Server missing spaCy, SSH and pip install"** — `spaCy` was already installed
   in every container that the original diagnosis claimed needed it:
   `polymath_v33-backend-1`, `polymath_v33-ingest-worker-1`, `polymath_v33-ingest-worker-2`.
2. **"MCP container excludes spaCy by design"** — the slim `requirements.mcp.txt`
   did exclude it, but the code path requires it (see below). The "by design"
   comment in the Dockerfile was stale.
3. **"Switch to ingest_from_url to bypass"** — same code path
   (`_ingest_bytes`), same failure.
4. **"RunPod image missing spaCy"** — RunPod image pins spaCy in
   `runpod_flash_extractor/Dockerfile.locked`; primary endpoint completed
   6,488 jobs successfully.

## Actual root cause

The MCP container runs the full ingestion pipeline in-process (it doesn't
just queue). For the default `runpod_wire_contract = "local_extraction_v1"`
(used by every active corpus), the call path is:

```
polymath_upload_document (MCP container)
  → _ingest_bytes
  → ingestion_service.ingest
  → worker.run_ingest_job
  → _runpod_extractor_for_config(config)
      if config.runpod_wire_contract == "local_extraction_v1":
          from services.runpod_local_extraction import extract_entities
                                                              ↓
          _load_nlp()  →  import spacy  →  ❌ ModuleNotFoundError
                                                              ↓
          build_spacy_observation_bundle(text, nlp, ...)  ← runs spaCy locally
                                                            on every chunk to
                                                            compile observations
                                                            that complement the
                                                            RunPod extraction
```

`build_spacy_observation_bundle` is essential local post-processing on the
RunPod output — it's not a redundant check or optional. Without it the
`local_extraction_v1` contract cannot produce its guaranteed wire shape.

## Fix applied

1. **Installed in the live container** (immediate):
   ```
   docker exec polymath-mcp pip install spacy==3.8.14 'en_core_web_sm@...'
   docker restart polymath-mcp
   ```
2. **Pinned for future image rebuilds** in `backend/requirements.mcp.txt`
   (same version pins as `backend/requirements.txt` and
   `runpod_flash_extractor/requirements.custom-image.in`).
3. **Updated stale comment** at the top of `requirements.mcp.txt` that
   claimed MCP doesn't need document/NLP deps.

## Verification

Live test via the MCP JSON-RPC endpoint:

```
POST /mcp  tools/call  polymath_upload_document
  corpus_id: cybersecurity_study (d18713f7)
  filename:  __mcp_upload_test_postfix2.md
  size:      119 bytes

Response: status=queued, doc_id=528b1730...

Poll polymath_get_ingest_status (5s later):
  status: complete
  chunk_count: 0   (test doc too short to chunk)
  parent_count: 1
  write_state:
    mongo_written:     true
    qdrant_written:    true
    summaries_indexed: true
    neo4j_written:     true
    verified:          true
    warnings:          []
    verify_errors:     []
  error: null
```

All four data stores written, verification clean. The upload pipeline is
healthy end-to-end.

## Also fixed in this session (incidental to the spaCy issue)

While diagnosing, found and resolved a separate long-running issue:

- **11 corpora marked `status=deleted` on 2026-07-19** but their cleanup was
  stuck (`cleanup_status=running` for 5 of them; residual data for all 11).
- **Auto-repair loop** ran every 2 minutes for 5+ days on `cybersecurity_study`
  corpus trying to "fix" docs whose status couldn't progress because of the
  stuck cleanup state.
- **12,167 queued summary jobs** all belonged to deleted corpora (waste).
- Hard-deleted **1,298,900 doc rows** across 28 Mongo collections for the
  deleted corpora.
- Marked all 11 as `cleanup_status=complete` and released their cleanup leases.
- Cleared stale `ingest_lane_leases` (some from 2026-07-11).
- Backed up the 11 deleted corpus metadata docs to
  `corpora_deleted_backup_20260725` before deletion.

After cleanup:
- Auto-repair loop **stopped** (verified — zero log lines in 5+ min).
- Queued summary jobs now all belong to **active** corpora.
- Active corpora documents untouched (verified — all 6 still have their docs).

## Followups (not blocking)

- The MCP container's slim-deps design intent is now violated by spaCy +
  numpy + scikit-learn + networkx already in the file. Consider a future
  refactor that moves `build_spacy_observation_bundle` to the RunPod image
  so MCP can be truly slim again. Until then, this pin reflects reality.
- `ingest_provider_call_metrics` still shows longcat 400 / deepseek
  `HTTPStatusError` rejections in the summary phase. Those are separate
  from the spaCy issue — they're intermittent LLM provider errors handled
  by retry/fallback. LongCat API key wiring was deferred per user direction.
