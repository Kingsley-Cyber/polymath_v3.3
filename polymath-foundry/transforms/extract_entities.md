# `extract_entities.py`

Foundry Python transform — NER + alias resolution. Produces Entity rows and `mentions` link rows.

> **DoD-laptop note:** paste the code block into `transforms/extract_entities.py` on the Foundry side.

## Code

```python
"""
extract_entities.py — Foundry Python transform

PURPOSE
-------
Run named-entity recognition on each Chunk, resolve aliases against
existing Entity objects, and emit:
  1. new Entity rows (canonical entities not seen before)
  2. `mentions` link rows (Chunk → Entity)

Runs automatically after `embed_chunks`.

INPUTS
------
/Polymath/clean/chunks_embedded
/Polymath/clean/entities         (read for alias resolution / dedup)

OUTPUTS
-------
/Polymath/clean/entities                       (new + updated rows)
/Polymath/links/chunk_mentions_entity
    columns:
      chunk_id      string
      entity_id     string
      span_start    integer
      span_end      integer
      mention_text  string
      score         float

NOTES
-----
- Entity types: person, org, place, system, concept, event, product, doctrine.
- Alias resolution is conservative — fuzzy-match only when normalized
  surface form + entity_type match. Anything ambiguous becomes a new
  Entity and is left for the curator (or MergeEntities action) to merge.
- Confidence < 0.5 mentions are dropped, not stored.
"""

from transforms.api import transform, Input, Output, configure, incremental
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType, StringType, IntegerType, FloatType, StructType, StructField
)

from polymath_lib.ner import extract_entities_from_text, resolve_alias
from polymath_lib.ids import stable_entity_id


MENTION_STRUCT = ArrayType(StructType([
    StructField("canonical_name", StringType()),
    StructField("entity_type", StringType()),
    StructField("span_start", IntegerType()),
    StructField("span_end", IntegerType()),
    StructField("mention_text", StringType()),
    StructField("score", FloatType()),
]))


@configure(profile=["DRIVER_MEMORY_MEDIUM", "EXECUTOR_MEMORY_LARGE", "GPU_ENABLED"])
@incremental()
@transform(
    chunks=Input("/Polymath/clean/chunks_embedded"),
    existing_entities=Input("/Polymath/clean/entities"),
    entities=Output("/Polymath/clean/entities"),
    mentions=Output("/Polymath/links/chunk_mentions_entity"),
)
def compute(ctx, chunks, existing_entities, entities, mentions):
    df = chunks.dataframe()
    known = existing_entities.dataframe().select(
        "entity_id", "canonical_name", "entity_type", "aliases"
    )

    @F.udf(MENTION_STRUCT)
    def ner(text):
        return [m for m in extract_entities_from_text(text) if m["score"] >= 0.5]

    exploded = (
        df.withColumn("mentions", ner("text"))
          .withColumn("m", F.explode("mentions"))
          .select(
              "chunk_id",
              F.col("m.canonical_name").alias("canonical_name"),
              F.col("m.entity_type").alias("entity_type"),
              F.col("m.span_start").alias("span_start"),
              F.col("m.span_end").alias("span_end"),
              F.col("m.mention_text").alias("mention_text"),
              F.col("m.score").alias("score"),
          )
    )

    resolved = resolve_alias(exploded, known)
    new_entities = (
        resolved.filter(F.col("entity_id").isNull())
                .select(
                    F.udf(stable_entity_id, StringType())(
                        "canonical_name", "entity_type"
                    ).alias("entity_id"),
                    "canonical_name",
                    "entity_type",
                    F.array().alias("aliases"),
                    F.lit(None).cast(StringType()).alias("canonical_uri"),
                    F.lit(None).cast(StringType()).alias("description"),
                    F.lit(None).cast(StringType()).alias("merged_into"),
                )
                .dropDuplicates(["entity_id"])
    )

    entities.write_dataframe(
        new_entities, mode="upsert_by_primary_key", primary_key=["entity_id"]
    )

    link_rows = resolved.select(
        "chunk_id", "entity_id", "span_start", "span_end", "mention_text", "score"
    )
    mentions.write_dataframe(
        link_rows, mode="replace_partition_by", partition_keys=["chunk_id"]
    )
```
