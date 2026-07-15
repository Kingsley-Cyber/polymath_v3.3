# T9.3 B1 Prose Phase-2 Post-Stop Diagnosis — 2026-07-15

Status: **GREEN / PREREGISTERED ONE-RESUME CONDITION SATISFIED**.

This is a read-only diagnosis of the settled 148-terminal ledger. The senior
fixed the decision rule before this inspection: one unchanged-gate resume is
authorized only for content-driven document clustering without a provider-
health trend; repeated HTTP errors or latency drift parks the pass. No prompt,
gate, schema, provider contract, durable row, or canonical store was changed.

## Verdict

The evidence meets the first branch of the preregistered rule:

1. Failures are materially document-clustered. Document `333dd5a6…` owns
   **3/7 total failures** and **3/6 failures in the final rolling window**. It
   completed 7 accepted / 3 failed across its ten selected ordinals 178–187,
   a 30% failure rate. Every other failed document has exactly one failure.
2. The next 50 queued ordinals, 206–256, leave the hard-document region. They
   span six documents; only ordinal 206 overlaps any prior failure document,
   and none belongs to `333dd5a6…`. The other 49 rows are in five new
   documents.
3. Accepted-row latency and cost are stable. Comparing the prior nonoverlap
   50 terminal completions (ranks 49–98) with the final rolling 50 (ranks
   99–148), median latency rises 7.74% while p95 falls 5.49%; mean latency
   rises 4.77%. Median cost rises 9.28%, while p95 falls 3.63% and mean cost
   rises 4.32%. Mean provider calls per accepted row rises only 2.08%.
4. Provider transport failures do not form a degradation trend: one
   `ReadTimeout` occurred at rank 109 and one `HTTPStatusError` at rank 126.
   The latter correlates exactly with a single LiteLLM `HTTP/1.1 500` at
   `2026-07-15T08:22:25.008803339Z`, within milliseconds of the job's durable
   completion at `08:22:25.011Z`. There is no repeated 5xx outcome.

**Decision:** one resume is authorized under the exact published runner and
all unchanged gates. Any second rolling stop parks the pass for the owner.
The seven failed parents stay honest losses in the main ledger.

## Failures by document

| Document | Selected | Attempted | Accepted | Failed | Attempted failure rate | Final-window failed |
|---|---:|---:|---:|---:|---:|---:|
| `333dd5a6…` | 10 | 10 | 7 | **3** | **30.00%** | **3** |
| `419a49a6…` | 7 | 6 | 5 | 1 | 16.67% | 1 |
| `2ea6852b…` | 7 | 7 | 6 | 1 | 14.29% | 1 |
| `209d3863…` | 9 | 9 | 8 | 1 | 11.11% | 0 |
| `30cf4973…` | 12 | 12 | 11 | 1 | 8.33% | 1 |

The final 50 completions span eight documents. `333dd5a6…` contributes ten
terminal rows but half of all failures in that window. The first structural
failure, ordinal 125 in `209d3863…`, has final completion rank 68; the earlier
live receipt's “69 terminal” was the contemporaneous total row count, not its
final completion rank. The failed-stop receipt has been corrected to rank 68.

## Completion-order transport evidence

| Rank | UTC | Ordinal | Document | Outcome |
|---:|---|---:|---|---|
| 109 | `07:59:56.024Z` | 164 | `2ea6852b…` | `ReadTimeout` |
| 126 | `08:22:25.011Z` | 183 | `333dd5a6…` | `HTTPStatusError`, correlated `500` / 5xx |

There is one timeout and one HTTP 500 separated by 22 minutes and 17 terminal
completions. The sanitized dead-letter validation stores the exception class
but no numeric status. The status was recovered without printing the log line
or response body: the LiteLLM log window contains 28 `HTTP/1.1 200` records
and exactly one `HTTP/1.1 500`; the 500 timestamp matches the durable failure.

## Accepted-row stability

| Metric | Prior ranks 49–98 (49 accepted) | Final ranks 99–148 (44 accepted) | Change |
|---|---:|---:|---:|
| Latency mean | 147.047 s | 154.064 s | +4.77% |
| Latency p50 | 126.537 s | 136.331 s | +7.74% |
| Latency p95 | 273.553 s | 258.548 s | -5.49% |
| Cost mean | `$0.02829852` | `$0.02952017` | +4.32% |
| Cost p50 | `$0.02460110` | `$0.02688350` | +9.28% |
| Cost p95 | `$0.04851501` | `$0.04675222` | -3.63% |
| Provider calls mean | 1.469 | 1.500 | +2.08% |

The final window's accepted prompts are larger (median 10,541 versus 7,747
tokens), which supports content-composition variance rather than provider
slowdown. Despite that 36% prompt-size increase, latency p95 and cost p95 both
decline and mean/median movements remain modest.

## Next 50 queued ordinals

| Document | Count | Ordinals | Prior failure document? |
|---|---:|---|---|
| `419a49a6…` | 1 | 206 | Yes |
| `46da2726…` | 5 | 207–211 | No |
| `4ea6af83…` | 11 | 212–223 | No |
| `52e60832…` | 7 | 224–230 | No |
| `54e7933c…` | 7 | 231–237 | No |
| `55e404ad…` | 19 | 238–256 | No |

The queue is about to leave, not remain inside, the observed hard-document
cluster. This is the mechanism expected by the preregistered recovery branch:
new accepted completions can naturally displace clustered failures from the
fixed 50-row window.

## Actual commands and safe output tails

Durable-ledger diagnosis:

```bash
sh -c 'docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 python /tmp/t93_p2_poststop_diagnosis.py > /tmp/t93_p2_poststop_diagnosis.log 2>&1; rc=$?; echo EXIT=$rc >> /tmp/t93_p2_poststop_diagnosis.log; exit $rc'
```

```text
"failures_by_document": [{"document": {"doc_id_short": "333dd5a665b6…"},
"selection_count": 10, "attempted_count": 10, "accepted_count": 7,
"failure_count": 3, "attempted_failure_rate": 0.3}]
"accepted_metrics_prior_nonoverlap_50_terminal": {"accepted_count": 49,
"latency_seconds": {"p50": 126.537, "p95": 273.5534},
"actual_cost_usd": {"p50": 0.0246011, "p95": 0.04851501}}
"accepted_metrics_final_rolling_50_terminal": {"accepted_count": 44,
"latency_seconds": {"p50": 136.331, "p95": 258.54765},
"actual_cost_usd": {"p50": 0.0268835, "p95": 0.0467522175}}
"next_50_queued": {"count": 50, "ordinal_min": 206,
"ordinal_max": 256, "document_count": 6,
"failure_document_overlap_count": 1}
EXIT=0
```

HTTP status-class correlation, with raw log lines suppressed:

```bash
sh -c 'docker logs --timestamps --since 2026-07-15T08:20:00Z --until 2026-07-15T08:24:00Z polymath_v33-litellm-1 2>&1 | python3 /tmp/t93_p2_log_status_safe.py > /tmp/t93_p2_litellm_status_safe.log; rc=${PIPESTATUS[0]}; echo DOCKER_EXIT=$rc >> /tmp/t93_p2_litellm_status_safe.log; exit $rc'
```

```text
{"class":"2xx","code":200,"count":28,"pattern":"http_protocol"}
{"class":"5xx","code":500,"count":1,"pattern":"http_protocol",
"timestamps":["2026-07-15T08:22:25.008803339Z"]}
"raw_lines_printed": false
DOCKER_EXIT=0
```

## Evidence labels

- **VERIFIED:** durable row/document counts, completion order, metrics,
  status classes, next-queue composition, exact log status/timestamp, and true
  exits above.
- **INFERRED:** the failure pattern is content/document-driven rather than a
  provider-health trend; this inference follows the preregistered rule and is
  supported by clustering, stable accepted metrics, one nonrepeating 500, and
  the queue leaving the cluster.
- **ASSUMED:** none.
