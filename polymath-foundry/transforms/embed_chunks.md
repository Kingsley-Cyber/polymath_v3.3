# `embed_chunks.py`

Foundry Python transform — embed each Chunk into a 1024-dim vector and register it with Foundry Vector Search.

> **DoD-laptop note:** paste the code block into `transforms/embed_chunks.py` on the Foundry side.

## Code

```python
"""
embed_chunks.py — Foundry Python transform

PURPOSE
-------
Embed each Chunk into a 1024-dimensional vector using the AIP-hosted
embedding model. Writes vectors back to the Chunk dataset and registers
them with the Foundry Vector Search Service.

Runs automatically after `chunk_documents`.

INPUTS
------
/Polymath/clean/chunks

OUTPUTS
-------
/Polymath/clean/chunks_embedded
    columns: all of /Polymath/clean/chunks plus
      embedding   array<float>  (length 1024)

Side effect: registers (chunk_id, embedding, corpus_id, document_id,
chunk_type) with the Foundry Vector Search Service index `polymath_chunks`.

NOTES
-----
- Embedding model is pinned to whatever AIP exposes that matches the
  v3.3 Qwen3-Embedding-0.6B family. If it isn't available, the model
  pin in `polymath_lib.embedding` must be updated, and the eval set
  re-run before going live.
- Batch size is governed by AIP quotas — see EMBED_BATCH below.
- Idempotent: only re-embeds chunks whose text changed since last run.
"""

from transforms.api import transform, Input, Output, configure, incremental
from pyspark.sql import functions as F
from pyspark.sql.types import ArrayType, FloatType

from polymath_lib.embedding import embed_batch, EMBEDDING_DIM
from polymath_lib.vector_search import register_index

EMBED_BATCH = 64
INDEX_NAME = "polymath_chunks"


@configure(profile=["DRIVER_MEMORY_MEDIUM", "EXECUTOR_MEMORY_LARGE", "GPU_ENABLED"])
@incremental()
@transform(
    chunks=Input("/Polymath/clean/chunks"),
    embedded=Output("/Polymath/clean/chunks_embedded"),
)
def compute(ctx, chunks, embedded):
    df = chunks.dataframe()  # incremental: only new/changed rows

    @F.pandas_udf(ArrayType(FloatType()))
    def embed_series(texts):
        return embed_batch(texts.tolist(), batch_size=EMBED_BATCH)

    out = df.withColumn("embedding", embed_series("text"))

    assert out.schema["embedding"].dataType.elementType == FloatType()

    embedded.write_dataframe(
        out, mode="upsert_by_primary_key", primary_key=["chunk_id"]
    )

    register_index(
        ctx=ctx,
        index_name=INDEX_NAME,
        dataset=out,
        id_column="chunk_id",
        vector_column="embedding",
        vector_dim=EMBEDDING_DIM,
        facet_columns=["corpus_id", "document_id", "chunk_type"],
    )
```
