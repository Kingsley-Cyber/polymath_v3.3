# `compute_source_health.py`

Foundry Python transform — compute `Source.health_score` from a 30-day window of IngestionJob outcomes.

> **DoD-laptop note:** paste the code block into `transforms/compute_source_health.py` on the Foundry side.

## Code

```python
"""
compute_source_health.py — Foundry Python transform

PURPOSE
-------
Compute Source.health_score from recent ingestion outcomes. Score is a
weighted blend of success rate and latency over a rolling 30-day window.

Runs on a daily schedule.

INPUTS
------
/Polymath/ops/ingestion_jobs
/Polymath/clean/sources

OUTPUTS
-------
/Polymath/clean/sources     (updates only the health_score column)

FORMULA
-------
health_score = 0.7 * success_rate_30d + 0.3 * latency_score
where
  success_rate_30d = succeeded_jobs / total_jobs           (1.0 if no jobs)
  latency_score    = clamp(1 - mean_latency_seconds/60, 0, 1)
"""

from transforms.api import transform, Input, Output, configure
from pyspark.sql import functions as F

WINDOW_DAYS = 30


@configure(profile=["DRIVER_MEMORY_SMALL", "EXECUTOR_MEMORY_MEDIUM"])
@transform(
    jobs=Input("/Polymath/ops/ingestion_jobs"),
    sources_in=Input("/Polymath/clean/sources"),
    sources_out=Output("/Polymath/clean/sources"),
)
def compute(ctx, jobs, sources_in, sources_out):
    cutoff = F.date_sub(F.current_date(), WINDOW_DAYS)

    j = jobs.dataframe().filter(F.col("started_at") >= cutoff)

    per_source = (
        j.groupBy("source_id")
         .agg(
             F.count("*").alias("total"),
             F.sum(F.when(F.col("status") == "succeeded", 1).otherwise(0)).alias("succeeded"),
             F.avg(
                 F.unix_timestamp("finished_at") - F.unix_timestamp("started_at")
             ).alias("mean_latency_s"),
         )
         .withColumn("success_rate", F.col("succeeded") / F.col("total"))
         .withColumn(
             "latency_score",
             F.greatest(
                 F.lit(0.0),
                 F.least(
                     F.lit(1.0),
                     F.lit(1.0) - F.col("mean_latency_s") / F.lit(60.0),
                 ),
             ),
         )
    )

    sources = sources_in.dataframe()
    joined = sources.join(per_source, on="source_id", how="left")

    updated = joined.withColumn(
        "health_score",
        F.when(F.col("total").isNull(), F.lit(1.0))
         .otherwise(F.col("success_rate") * 0.7 + F.col("latency_score") * 0.3),
    ).drop("total", "succeeded", "mean_latency_s", "success_rate", "latency_score")

    sources_out.write_dataframe(
        updated, mode="upsert_by_primary_key", primary_key=["source_id"]
    )
```
