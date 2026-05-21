# `extract_claims.py`

Foundry Python transform — extract subject-predicate-object claims from each Chunk with extractor confidence.

> **DoD-laptop note:** paste the code block into `transforms/extract_claims.py` on the Foundry side.

## Code

```python
"""
extract_claims.py — Foundry Python transform

PURPOSE
-------
Extract subject-predicate-object claims from each Chunk and emit:
  1. new Claim rows
  2. `supports` link rows (Chunk → Claim)

Runs automatically after `embed_chunks` (and ideally after
`extract_entities` so subject/object can be resolved).

INPUTS
------
/Polymath/clean/chunks_embedded
/Polymath/clean/entities

OUTPUTS
-------
/Polymath/clean/claims
    columns:
      claim_id, statement, subject_entity_id, object_entity_id,
      predicate, confidence, flagged, flag_reason, flagged_by
/Polymath/links/chunk_supports_claim
    columns:
      chunk_id, claim_id, score

NOTES
-----
- Extractor is an AIP-hosted instruction-tuned model invoked via a
  small batched prompt. Output is JSON-validated; rows that fail
  schema validation are dropped (not silently retried).
- Confidence < 0.4 claims are dropped.
- A claim is keyed by hash(normalized_statement + predicate) — same
  statement surfacing from multiple chunks accumulates `supports` links.
"""

from transforms.api import transform, Input, Output, configure, incremental
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType, StringType, FloatType, StructType, StructField
)

from polymath_lib.claims import extract_claims_from_text
from polymath_lib.ids import stable_claim_id
from polymath_lib.ner import resolve_entity_reference


CLAIM_STRUCT = ArrayType(StructType([
    StructField("statement", StringType()),
    StructField("subject", StringType(), nullable=True),
    StructField("predicate", StringType()),
    StructField("object", StringType(), nullable=True),
    StructField("confidence", FloatType()),
]))


@configure(profile=["DRIVER_MEMORY_MEDIUM", "EXECUTOR_MEMORY_LARGE", "GPU_ENABLED"])
@incremental()
@transform(
    chunks=Input("/Polymath/clean/chunks_embedded"),
    entities=Input("/Polymath/clean/entities"),
    claims=Output("/Polymath/clean/claims"),
    supports=Output("/Polymath/links/chunk_supports_claim"),
)
def compute(ctx, chunks, entities, claims, supports):
    df = chunks.dataframe()
    ent = entities.dataframe().select("entity_id", "canonical_name", "aliases")

    @F.udf(CLAIM_STRUCT)
    def extract(text):
        return [c for c in extract_claims_from_text(text) if c["confidence"] >= 0.4]

    exploded = (
        df.withColumn("claims", extract("text"))
          .withColumn("c", F.explode("claims"))
          .select(
              "chunk_id",
              F.col("c.statement").alias("statement"),
              F.col("c.subject").alias("subject_text"),
              F.col("c.predicate").alias("predicate"),
              F.col("c.object").alias("object_text"),
              F.col("c.confidence").alias("confidence"),
          )
    )

    resolved = resolve_entity_reference(exploded, ent)
    resolved = resolved.withColumn(
        "claim_id",
        F.udf(stable_claim_id, StringType())("statement", "predicate"),
    )

    claim_rows = resolved.select(
        "claim_id", "statement",
        F.col("subject_entity_id"),
        F.col("object_entity_id"),
        "predicate",
        "confidence",
        F.lit(False).alias("flagged"),
        F.lit(None).cast(StringType()).alias("flag_reason"),
        F.lit(None).cast(StringType()).alias("flagged_by"),
    ).dropDuplicates(["claim_id"])
    claims.write_dataframe(
        claim_rows, mode="upsert_by_primary_key", primary_key=["claim_id"]
    )

    link_rows = resolved.select(
        "chunk_id", "claim_id", F.col("confidence").alias("score")
    )
    supports.write_dataframe(
        link_rows, mode="upsert_by_primary_key", primary_key=["chunk_id", "claim_id"]
    )
```
