SPECIMEN: one request/response pair (shape, from source; values are per-run artifacts)

REQUEST (attempt_payload construction, ghost_b.py:4748-4760):
  messages = [
    {"role": "system", "content": _JSON_OBJECT_SYSTEM | _SYSTEM},   # schema/json_object vs normal lane
    {"role": "user", "content": build_schema_native_prompt(...)}    # vocab rendered from Literals
  ]
  response_format = {"type": "json_schema", "json_schema": {...ExtractionResponse...}}  # schema lane only
  attempt_max_tokens from _context_bounded_completion_tokens (ghost_b.py:4762-4770)

RESPONSE (parse path, audit map §1 run_extraction_jobs):
  ghost_b_extractions rows keyed by (doc_id, chunk_id) with contract_hash in extraction_job_id
  (extraction_jobs.py:170 extraction_job_id includes contract_hash + chunk_hash)
  _persist_extraction_rows writes results + failures (extraction_jobs.py:1012, audit map §1)

CROSS-CHECK THAT CATCHES THE BUG CLASS: the request's rendered entity_type list must equal the
grammar's 15 (schema lane). If you see "person|org|concept|other" next to a response_format
declaring Person/Organization/Location — that is ghost_b.py:1787's root cause.

