# Ingestion — Polymath on Foundry

How raw source artifacts become typed, semantically chunked, governed Ontology objects. Every file type goes through the same pipeline shape; only the parser changes.

> **TL;DR pipeline**
> `parse → normalize → summarize → semantic-chunk → embed → extract entities → extract claims → status:indexed`

---

## 1. Supported file types

| Type | Extensions | Parser | Structure preserved | Chunker emphasis |
|---|---|---|---|---|
| PDF | `.pdf` | Docling | Headings, paragraphs, lists, tables, figures (page + bbox) | Tables kept whole; figures get a VLM caption that becomes a chunk of `chunk_type=paragraph` |
| Word | `.docx`, `.doc` | python-docx (Docling fallback for legacy `.doc`) | Headings, paragraphs, lists, tables; comments dropped | Heading-aware split |
| PowerPoint | `.pptx`, `.ppt` | python-pptx | Slide title, bullets, speaker notes, embedded tables | One slide = one section; bullets become a list chunk |
| Spreadsheet | `.xlsx`, `.xls` | openpyxl | Sheets, header row, data rows | Each sheet = one Document; non-tabular sheets fall back to row-text concatenation |
| CSV / TSV | `.csv`, `.tsv` | pandas | Header + row structure | Large CSVs (> 5k rows) treated as datasets (no chunking); registered as a separate object class |
| HTML | `.html`, `.htm` | readability-lxml + BeautifulSoup | Headings, paragraphs, lists, tables, code blocks | Nav / footer / ads stripped before chunking |
| Markdown | `.md`, `.mdx` | mistune | Headings, paragraphs, lists, tables, code fences | YAML frontmatter parsed into `Document.tags` and metadata |
| Plain text | `.txt`, `.log` | passthrough | Paragraphs (double-newline split) | Length-based pass only — no structure to exploit |
| Source code | `.py`, `.js`, `.ts`, `.go`, `.rs`, `.java`, `.cpp`, `.c`, `.rb`, `.sh` | tree-sitter | Functions, classes, top-level docstrings, comments | Each top-level symbol = one chunk; long bodies split on logical breakpoints |
| Image | `.png`, `.jpg`, `.jpeg`, `.webp`, `.tif` | OCR (Tesseract) + VLM caption (AIP-hosted) | OCR text + caption combined | Single chunk per image unless OCR yields long text |
| Audio | `.mp3`, `.wav`, `.m4a`, `.ogg` | Whisper (AIP-hosted) | Timestamped utterances | Each utterance kept as a chunk with `start_ms` / `end_ms` |
| Email | `.eml`, `.msg` | stdlib `email` + `extract-msg` | Headers (from/to/subject/date), body, threading | Body chunked; attachments processed recursively as their own Documents linked back via `from_source` |
| JSON / YAML | `.json`, `.yaml`, `.yml` | pyyaml / json | Top-level keys, nested structure | Each top-level key path becomes a section |
| Archive | `.zip`, `.tar`, `.gz` | zipfile / tarfile | Recursive: each contained file ingested individually | Container archive itself produces no Document |

**Out of scope for v1:** CAD files, proprietary GIS layers, raw binary formats without a parser, video (use audio extract path only).

---

## 2. Pipeline stages

```
Source file
    │
    ▼
1. parse                → raw structured text + structural hints (block list)
    │
    ▼
2. normalize            → unicode NFC, strip control chars, collapse whitespace,
                          normalize quotes/dashes, drop repeated boilerplate
    │
    ▼
3. summarize_document   → ≤ 200-token executive summary on Document.summary
                          (AIP-hosted LLM; deterministic, temperature=0)
    │
    ▼
4. semantic_chunk       → Chunks with chunk_type (paragraph/table/list/code/heading)
    │
    ▼
5. embed                → 1024-dim vector per Chunk; indexed in Vector Search
    │
    ▼
6. extract_entities     → Entity objects + Chunk→Entity (`mentions`) links
    │
    ▼
7. extract_claims       → Claim objects + Chunk→Claim (`supports`) links
    │
    ▼
8. Document.status      → "indexed"; visible to retrieval
```

Each stage is its own transform — see `transforms/*.md`. Failure in any stage marks the relevant `IngestionJob` `status=failed`; downstream stages do not run for that document.

---

## 3. Semantic chunking — what "true semantic" means here

A fixed sliding-window chunker is the v3.3-era baseline. "True semantic" means **structure first, length second**.

### 3.1 Structure-first pass

The parser emits typed blocks: `heading`, `paragraph`, `list`, `table`, `code`, `figure_caption`.

The chunker walks blocks in order and groups them into chunks under these rules:

| Block type | Rule |
|---|---|
| `heading` | Starts a new chunk (`chunk_type=heading`); attaches its level (h1/h2/…) as metadata |
| `paragraph` | Appended to the current chunk if length budget allows; otherwise starts a new chunk |
| `list` | Kept whole if length budget allows; if too long, split at top-level list items (never mid-item) |
| `table` | Kept whole; oversized tables become a single chunk with `chunk_type=table` regardless of budget (retrieval-time truncation handles the rest) |
| `code` | Kept whole when ≤ 2× budget; split on function/class boundaries via tree-sitter otherwise |
| `figure_caption` | Appended to the surrounding chunk; never alone |

A chunk inherits the most recent heading path as `Chunk.headings: array<string>`.

### 3.2 Length-based pass

After structural grouping, any paragraph exceeding the corpus's `default_chunking_profile.max_tokens` is further split:

- On sentence boundary (spaCy sentence splitter) closest to the budget boundary.
- With overlap of `default_chunking_profile.overlap` tokens — bidirectional, never crossing a heading.

Profiles:

| Profile | max_tokens | overlap | When to use |
|---|---|---|---|
| `fine` | 256 | 32 | Dense technical references; benchmarks |
| `balanced` | 512 | 64 | Default; mixed prose / docs |
| `coarse` | 1024 | 128 | Long-form narrative; books; legal text |

### 3.3 What gets dropped

- Empty chunks (whitespace only).
- Chunks shorter than 16 tokens AFTER normalization (signal-to-noise too low).
- Repeated boilerplate (footers, page numbers) detected by per-Document frequency analysis.

---

## 4. Extraction — entities

`transforms/extract_entities.md` runs after `embed`. Two passes:

### 4.1 NER pass

AIP-hosted instruction-tuned model classifies spans into:

| Type | Examples |
|---|---|
| `person` | Joe Smith, MG Davidson |
| `org` | NATO, JSOC, Acme Corp |
| `place` | Fort Bragg, Brussels |
| `system` | Maven Smart System, Polymath |
| `concept` | "sensor-to-shooter", "ontology", "RAG" |
| `event` | STEADFAST DETERRENCE 2025, FY26 |
| `product` | Foundry, AIP Chatbot Studio |
| `doctrine` | JP 3-0, AR 25-50 |

Spans with confidence < 0.5 are dropped.

### 4.2 Alias resolution

For each mention, the resolver:

1. Normalizes surface form (case, punctuation, common suffixes like "Corp", "Inc").
2. Looks up exact match on `Entity.canonical_name` + `entity_type`.
3. If no match, fuzzy match (Jaro-Winkler ≥ 0.92) against `Entity.aliases`.
4. If still no match, creates a new Entity row (queued for curator review or auto-confirmed below a threshold).
5. Writes a `mentions` link from Chunk → Entity with `span_start`, `span_end`, `score`.

Conservative bias: a near-miss becomes a new Entity rather than risking a wrong merge. The curator (or `MergeEntities` action) cleans up.

---

## 5. Extraction — claims

`transforms/extract_claims.md` runs after `embed` (in parallel with `extract_entities`).

A claim is a subject-predicate-object triple expressed in natural language, with provenance:

```
"NATO has adopted Maven Smart System for combined operations planning."
→ subject:    NATO (Entity)
  predicate:  "has adopted"
  object:     Maven Smart System (Entity)
  confidence: 0.91
```

### 5.1 Extractor

AIP-hosted instruction-tuned model, prompted batch-wise per chunk. Output is JSON; rows that fail schema validation are dropped (no silent retry).

Each claim is keyed by `hash(normalized_statement + predicate)`. The same statement surfacing from multiple chunks accumulates `supports` links rather than spawning duplicate Claim objects.

### 5.2 Why claims are an object, not free text

Two operational reasons:

1. **Flagging.** Curators can flag a Claim once; the badge propagates to every Message that cited a Chunk supporting that Claim. Without a Claim object, you'd flag chunks individually and lose the cross-document view.
2. **Counter-evidence.** A future enhancement lets two Chunks support contradictory Claims; the graph makes "show me contested claims about X" a simple traversal.

### 5.3 Confidence threshold

Drop below 0.4. Display below 0.7 with a "low confidence" badge in citation UI.

---

## 6. Per-document summary

Stage 3 of the pipeline. Why before chunking, not after?

- Used in retrieval for **document-level reranking** (the agent can compare a summary against the query as a coarse filter).
- Used in citation UI as the hover preview.
- Generating it before chunking lets the summarizer see the whole document with no boundary artifacts.

Prompt outline:

```
You are summarizing a document for a research index. Produce ≤ 200 tokens.
Cover: subject, scope, key claims, document type, vintage. Do not invent.
If the document is fragmentary or non-prose (e.g. a spreadsheet), describe
its structure and what it appears to contain.
```

Temperature 0. Output stored on `Document.summary`.

---

## 7. Idempotency

Natural key: `(source_id, content_sha256)`.

| Scenario | Behavior |
|---|---|
| Same Source, same content | No-op — transform writes nothing for that row |
| Same Source, new content | Bumps `Document.version`, replaces Chunks atomically, re-runs extraction |
| New Source | Creates a new Document with version 1 |
| Manual `ReingestDocument` action | Forces re-run even if content unchanged |

Chunks are partitioned by `document_id` so a reingest can `replace_partition_by` without touching other documents.

---

## 8. Failure modes

| Failure | Where | Behavior |
|---|---|---|
| Parser throws | Stage 1 | IngestionJob `status=failed`, `error=<exception>`. Document not created. |
| LLM summarizer times out | Stage 3 | IngestionJob `status=failed`. Optional one-shot retry with smaller input. |
| Embedding service quota | Stage 5 | IngestionJob `status=failed` with backoff hint. Schedule retry. |
| Extractor JSON invalid | Stages 6–7 | Drop that row; do NOT fail the job. Log count of dropped rows. |
| Disk / storage error | Any | IngestionJob `status=failed`. Foundry retries per platform policy. |

No silent drops at the Document level. Stage-internal drops (invalid extractor JSON) are counted and surfaced in IngestionJob metadata.

---

## 9. Intent + Reasoning

- **Why one Document per source file, not per page or per section.** Document is the unit of provenance and the unit of governance (status / canon / archive). Pages are a PDF-specific accident. Sections are addressable as Chunks. Keeping Document = file means citations always point to a single human-meaningful artifact.
- **Why summarize before chunking.** Summaries are coarse signals for retrieval and UI. If we summarized chunks first and tried to merge, we'd amplify chunk-boundary artifacts. Summary at the file level only.
- **Why semantic chunking over fixed-window.** A 512-token window that splits mid-table is useless to retrieval and worse to display in a citation. Structure-first chunking keeps the unit of evidence aligned to the unit of human reading.
- **Why embeddings AFTER summary.** Order doesn't change vector quality but it does change recovery: if summary fails, we don't have stale embeddings hanging around.
- **Why Entity and Claim are separate objects.** Both are queryable and flaggable. A bag-of-tags model can't express "show me all chunks supporting this contested claim across corpora."
- **Why confidence is exposed, not hidden.** The retrieval surface is allowed to apply thresholds; the storage layer should not silently discard signals. Curators may want to inspect 0.45-confidence claims later.
- **Why per-type parsers, not a universal text dump.** A `.pptx` slide title carries different weight than a body paragraph. A code block's structure is semantic. A universal "extract all text" pass throws away those signals and the retrieval surface can't get them back.
- **Why no web freshness crawler in v1.** Removed from scope to ship a smaller v1. If/when reintroduced, it slots cleanly as a scheduled transform; nothing in this design precludes it.
