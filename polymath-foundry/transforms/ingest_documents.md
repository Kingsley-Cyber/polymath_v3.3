# `ingest_documents.py`

Foundry Python transform — convert raw source artifacts (HTML, PDFs, plain text) into Document rows that back the `Document` object type.

> **DoD-laptop note:** this `.md` wraps a `.py`. Paste the code block below into `transforms/ingest_documents.py` on the Foundry side.

## Code

```python
"""
ingest_documents.py — Foundry Python transform

PURPOSE
-------
Convert raw source artifacts (HTML, PDFs, plain text uploads) into Document
rows that back the `Document` object type in the Ontology.

Triggered by:
  - Action `IngestDocument` (writes a row to /Polymath/raw/sources)
  - Schedule (catches any sources flagged stale by curator)

INPUTS
------
/Polymath/raw/sources
    columns:
      source_id        string
      corpus_id        string
      raw_uri          string
      kind             string   (web | upload | api | drive)
      bytes_uri        string   (Foundry-managed blob location)

OUTPUTS
-------
/Polymath/clean/documents
    columns:
      document_id      string   (stable across reingest)
      source_id        string
      corpus_id        string
      title            string
      full_text        string
      content_sha256   string   (natural key for change detection)
      ingested_at      timestamp
      token_count      integer
      version          integer  (1 on first ingest, bumped on reingest)
      status           string   ("draft")

NOTES
-----
- Idempotent on (source_id, content_sha256). If the hash matches the
  prior version, this transform writes nothing for that row.
- HTML uses readability-lxml; PDFs use Docling; plain text passes through
  normalize_text only.
- Errors land in /Polymath/ops/ingestion_jobs with status=failed. No swallowing.
"""

from transforms.api import transform, Input, Output, configure
from pyspark.sql import functions as F
from pyspark.sql.types import StringType, IntegerType

from polymath_lib.parsing import parse_html, parse_pdf, normalize_text
from polymath_lib.hashing import sha256_text
from polymath_lib.tokens import count_tokens
from polymath_lib.ids import stable_document_id


@configure(profile=["DRIVER_MEMORY_MEDIUM", "EXECUTOR_MEMORY_MEDIUM"])
@transform(
    sources=Input("/Polymath/raw/sources"),
    existing=Input("/Polymath/clean/documents"),
    documents=Output("/Polymath/clean/documents"),
)
def compute(ctx, sources, existing, documents):
    raw = sources.dataframe()
    prior = existing.dataframe().select(
        "source_id",
        F.col("content_sha256").alias("prior_content_sha256"),
        F.col("version").alias("prior_version"),
        F.col("document_id").alias("prior_document_id"),
    )

    parse = F.udf(_parse_to_text, StringType())
    hashed = F.udf(sha256_text, StringType())
    tokens = F.udf(count_tokens, IntegerType())
    doc_id = F.udf(stable_document_id, StringType())

    enriched = (
        raw
        .withColumn("full_text", parse("kind", "bytes_uri"))
        .withColumn("content_sha256", hashed("full_text"))
        .withColumn("token_count", tokens("full_text"))
        .withColumn("document_id", doc_id("source_id"))
    )

    joined = enriched.join(prior, on="source_id", how="left")

    new_or_changed = joined.filter(
        (F.col("content_sha256") != F.col("prior_content_sha256"))
        | F.col("prior_content_sha256").isNull()
    )

    out = (
        new_or_changed
        .withColumn("version", F.coalesce(F.col("prior_version") + 1, F.lit(1)))
        .withColumn("ingested_at", F.current_timestamp())
        .withColumn("status", F.lit("draft"))
        .withColumnRenamed("raw_uri", "title")
        .select(
            "document_id", "source_id", "corpus_id", "title",
            "full_text", "content_sha256", "ingested_at",
            "token_count", "version", "status",
        )
    )
    documents.write_dataframe(
        out, mode="upsert_by_primary_key", primary_key=["document_id", "version"]
    )


def _parse_to_text(kind: str, bytes_uri: str) -> str:
    if kind == "web":
        return normalize_text(parse_html(bytes_uri))
    if kind == "upload" and bytes_uri.lower().endswith(".pdf"):
        return normalize_text(parse_pdf(bytes_uri))
    return normalize_text(parse_html(bytes_uri))
```
