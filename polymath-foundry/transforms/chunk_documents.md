# `chunk_documents.py`

Foundry Python transform — split each Document into structure-aware Chunks (paragraph / table / list / code / heading).

> **DoD-laptop note:** paste the code block into `transforms/chunk_documents.py` on the Foundry side.

## Code

```python
"""
chunk_documents.py — Foundry Python transform

PURPOSE
-------
Split each Document into Chunks with structural awareness. A Chunk preserves
its source type (paragraph / table / list / code / heading), page, and bbox
where applicable. Chunk size and overlap come from the corpus's
`default_chunking_profile`.

Runs automatically after `ingest_documents`.

INPUTS
------
/Polymath/clean/documents
/Polymath/clean/corpora     (to read default_chunking_profile)

OUTPUTS
-------
/Polymath/clean/chunks
    columns:
      chunk_id      string
      document_id   string
      corpus_id     string   (denormalized for Vector Search facet)
      ordinal       integer
      text          string
      token_count   integer
      chunk_type    string
      headings      array<string>
      page          integer  nullable
      bbox          string   nullable, JSON-encoded

NOTES
-----
- Embeddings are written by the next transform (embed_chunks.py), not here.
- Chunking strategy: structural first (split by headings / tables / code blocks
  intact), then a length-based pass with overlap within long paragraphs.
- Profiles:
    fine     → 256 tokens / 32 overlap
    balanced → 512 / 64
    coarse   → 1024 / 128
"""

from transforms.api import transform, Input, Output, configure
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType, StringType, IntegerType, StructType, StructField
)

from polymath_lib.chunking import structural_chunk
from polymath_lib.ids import stable_chunk_id


CHUNK_PROFILES = {
    "fine":     {"max_tokens": 256,  "overlap": 32},
    "balanced": {"max_tokens": 512,  "overlap": 64},
    "coarse":   {"max_tokens": 1024, "overlap": 128},
}

CHUNK_STRUCT = ArrayType(
    StructType([
        StructField("ordinal", IntegerType()),
        StructField("text", StringType()),
        StructField("token_count", IntegerType()),
        StructField("chunk_type", StringType()),
        StructField("headings", ArrayType(StringType())),
        StructField("page", IntegerType(), nullable=True),
        StructField("bbox", StringType(), nullable=True),
    ])
)


@configure(profile=["DRIVER_MEMORY_MEDIUM", "EXECUTOR_MEMORY_LARGE"])
@transform(
    documents=Input("/Polymath/clean/documents"),
    corpora=Input("/Polymath/clean/corpora"),
    chunks=Output("/Polymath/clean/chunks"),
)
def compute(ctx, documents, corpora, chunks):
    docs = documents.dataframe()
    corp = corpora.dataframe().select("corpus_id", "default_chunking_profile")

    joined = docs.join(corp, on="corpus_id", how="left")

    @F.udf(CHUNK_STRUCT)
    def split(text, profile_name):
        cfg = CHUNK_PROFILES.get(profile_name or "balanced", CHUNK_PROFILES["balanced"])
        return structural_chunk(text, max_tokens=cfg["max_tokens"], overlap=cfg["overlap"])

    exploded = (
        joined
        .withColumn("chunks", split("full_text", "default_chunking_profile"))
        .withColumn("chunk", F.explode("chunks"))
        .select(
            "document_id",
            "corpus_id",
            F.col("chunk.ordinal").alias("ordinal"),
            F.col("chunk.text").alias("text"),
            F.col("chunk.token_count").alias("token_count"),
            F.col("chunk.chunk_type").alias("chunk_type"),
            F.col("chunk.headings").alias("headings"),
            F.col("chunk.page").alias("page"),
            F.col("chunk.bbox").alias("bbox"),
        )
        .withColumn(
            "chunk_id",
            F.udf(stable_chunk_id, StringType())("document_id", "ordinal"),
        )
    )

    chunks.write_dataframe(
        exploded, mode="replace_partition_by", partition_keys=["document_id"]
    )
```
