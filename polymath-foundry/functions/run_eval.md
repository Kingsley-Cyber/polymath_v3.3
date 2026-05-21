# `run_eval.py`

Foundry Function — execute a named eval suite and write an `EvalRun` object.

> **DoD-laptop note:** paste the code block into `functions/run_eval.py` on the Foundry side.

## Code

```python
"""
run_eval.py — Foundry Function

PURPOSE
-------
Run a named eval suite against the current Polymath agent + Functions and
write an EvalRun object with the results and pass/fail decision.

INPUTS
------
suite_name: str   ('retrieval_recall_v1' | 'citation_coverage_v1' |
                   'faithfulness_v1' | 'latency_p95_v1')

OUTPUT
------
eval_run_id: str

NOTES
-----
- Suites are defined in polymath_lib.evals and reference a held-out
  golden set of (query, expected_chunk_ids, expected_answer) tuples.
- The function only computes metrics; alerting on metric.passed=False
  is handled by a separate AIP Logic workflow.
- commit_ref captures the current Foundry release marker so an EvalRun
  is reproducible.
"""

from functions.api import function

from polymath_lib.evals import run_suite
from polymath_lib.actions import create_eval_run
from polymath_lib.versioning import current_commit_ref


SUITE_THRESHOLDS = {
    "retrieval_recall_v1":   ("recall_at_10",      0.85, ">="),
    "citation_coverage_v1":  ("citation_coverage", 0.95, ">="),
    "faithfulness_v1":       ("faithfulness",      0.90, ">="),
    "latency_p95_v1":        ("latency_p95_s",     6.0,  "<="),
}


@function()
def run_eval(suite_name: str) -> str:
    commit_ref = current_commit_ref()
    metrics = run_suite(suite_name)  # dict[str, float]
    passed = _decide_pass(suite_name, metrics)
    return create_eval_run(
        suite_name=suite_name,
        metrics=metrics,
        commit_ref=commit_ref,
        passed=passed,
    )


def _decide_pass(suite_name: str, metrics: dict) -> bool:
    spec = SUITE_THRESHOLDS.get(suite_name)
    if not spec:
        return False
    key, threshold, op = spec
    val = metrics.get(key)
    if val is None:
        return False
    return val >= threshold if op == ">=" else val <= threshold
```
