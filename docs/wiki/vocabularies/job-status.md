# job-status

AUTHORITY: backend/services/ingestion/extraction_jobs.py:184 classify_extraction_status + audit map §2

COUNT: **8 terminal/runnable states documented in audit map §2 table**

## Values (verbatim)

```
queued, running, succeeded, promoted, skipped, failed, provider_failed, validation_failed, blocked_provider_contract, dead_letter
```

## Consumers

- claim_runnable_jobs runnable_statuses (audit map §1)
- reconcile_terminal_extraction_jobs (audit map §1)
- release stamp tests

## MUST MATCH

- Runnable set = {blocked_provider_contract, failed, provider_failed, queued, validation_failed} (audit map §2 table); anything else terminal

