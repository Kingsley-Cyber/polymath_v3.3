# Per-Corpus Disposition Matrix — 2026-07-13 (S3 deliverable)

Anchors: `P0.5` (OCR-corrupt heading audit + source-safe repair rule) and
`P2.7b` (per-corpus disposition matrix BEFORE any mass job).
Plan row: `docs/PLAN_CRITIQUE_2026-07-13.md` S3 (feeds S4 paid pass and S11 burst).

Method: read-only census/sampling of `parent_chunks` via the backend container
(motor + `get_settings`), deterministic pure-Python classifiers (no LLM), plus a
code-level capability audit of the ingestion chunker and docling sidecar.
Audit script exit 0; ecom/mark/UGO were **full censuses** (not samples); v2 was a
501-parent systematic sample (every 261st in parent_id order, doc-grouped).

Evidence labels: **VERIFIED** = probed/read today; **INFERRED** = follows
directly from verified facts; **ASSUMED** = supplied number not reproducible
from repo docs today.

---

## 1. Verified data state (census unless noted)

| Probe | ecommerce_AI_FILM_SCHOOL | markbuildsbrands_transcripts | UGO_CORPUS | polymath_v2 (sample 501) |
|---|---|---|---|---|
| Parents / children / docs | 10,222 / 56,996 / 79 | 1,015 / 3,791 / 103 | 203 / 659 / 1 | 130,503 / 240,876 / 498 |
| ghost_b_extractions rows | 56,996 (restored today — untouched) | 3,791 | 659 | 202,375 |
| heading_path non-empty | 100% | 100% | 100% | 86.0% (112,175) |
| page_start present (any value) | **0** | **0** | **0** | **0** |
| source_tier | tier_a 100% | tier_a 100% | tier_a 100% | tier_a 89% / tier_c 6% / tier_b 5% |
| Heading depth histogram | 1:640, 2:7,483, 3:1,547, 4:509, 5:43 | 1:1,007, 2:8 | 1:203 | 0:65, 1:238, 2:128, 3:47, 4:12, 5:11 |
| Corrupt-heading flag (any) | 7.62% of parents | 0.0% | 0.0% | 1.6% |
| Page-slug headings ("Page N") | **45.2%** of parents | 0% | 0% | 0.8% |
| OCR marker in text head | 4.64% | 0% | 0% | 0% |
| Doc verdicts (CORRUPT/SUSPECT/CLEAN) | 9 / 6 / 61 (+3 junk report docs) | 0 / 49* / 54 | 0 / 1* / 0 | 9 / 3 / 253 (weak n=1 samples) |

\* mark/UGO "SUSPECT" fired only on the flatness heuristic (top-heading share
≥ 0.9), not on any corruption flag — see verdicts below.

Classifiers (deterministic): non-word char density > 0.15; broken-word pattern
(`x y z` single-letter runs); repeated-char runs; numeric-only headings;
all-caps elements; heading element > 120 chars; OCR markers
(`OCR_FROM_IMAGES|OCR_FALLBACK_TEXT`); page slugs (`Page N`); heading-vs-text
redundancy (last element in first 200 chars); per-doc duplicate-heading share;
depth distribution. Script: `heading_audit_s3.py` (scratchpad; run via
`docker exec -w /app polymath_v33-backend-1 python _s3_heading_audit.py`,
EXIT=0).

## 2. What the "corruption" actually is (VERIFIED, per-doc drill-down)

ecom's 100% heading presence hides four distinct source-baked defects:

1. **Page-marker headings, no semantic sections** — 4 scanned-book docs
   (`five-c-s-of-cinematography`, `sound-design`, `framed-ink`,
   `manga-illustrating-battles`): headings are `<filename> > Page N`
   (+ `OCR_FALLBACK_TEXT` elements on some). 45.2% of ALL ecom parents carry a
   page-slug element. Structure is honest for scans but semantically empty.
2. **EPUB machine-path headings** — `building-a-storybrand`
   (`text/9780718033330_Chapter_1.xhtml`), `anatomy-for-sculptors`
   (`index_split_000.html`, plus broken `7 8 > **9**` numeric headings).
3. **Long bibliographic filename repeated on every parent** —
   `facial-action-coding-system` (364 parents, one 150-char heading with
   `-- Paul Ekman; ... -- isbn`), `intl-journal-reasoning` (21 parents).
4. **Under-ingested books (content gap, not metadata)** — 3 art books whose
   entire stored text is < 100 chars: `framed-ink-2` (40 chars),
   `animators-survival-kit` (74), `framed-perspective-vol1` (84); plus
   `laban-for-actors-and-dancers` thin at 6,050 chars. These books are
   effectively NOT in the corpus.
   Plus 3 pipeline-report files ingested as corpus content
   (`ocr-completion-report.md`, `ocr-marker-append-report.md`,
   `epub-backfill-status-report.md`) — retrieval junk.

mark: 905/1,015 parents heading = `Transcript`, 101 = `Description`, 8 rich AMA
headings. Flat by format (YouTube transcripts; title lives in doc name). Zero
corruption flags.

UGO: all 203 parents share the single heading `index_split_000` — an EPUB spine
machine label. Presence 100%, semantic value zero.

v2 (sample): 13.0% empty headings (matches the 86% presence figure), corrupt
flags on 1.6%; the 9 "CORRUPT" docs are n=1 samples (weak evidence, one long
heading each). No systemic OCR signature found in the sample.

## 3. page_start: can the pipeline emit it at all? (VERIFIED, code-level)

- `page_start` is emitted **only** in the `ocr_ast` chunker lane
  (`backend/services/ingestion/tier_chunker.py:1440-1476`, `_page_blocks` over
  per-page markdown).
- **Every parent in all four corpora is tier_a/b/c — none took that lane.** The
  structural lane (`_sections_to_parent_blocks(parse_result.sections)`) has no
  page provenance: the docling sidecar never reads DocItem `prov`
  (no `prov` access anywhere in `docling_svc/main.py`), and the `Section`
  schema hydrated in `backend/services/ingestion/docling_adapter.py:1975-1985`
  carries heading_path/text/element_type/level/language/metadata only.
- **Consequence: a plain reingest will NOT add page_start for tier_a documents.**
  It requires a code change first (carry docling `prov.page_no` →
  `Section.metadata` → `ParentChunk.page_start` in the structural lane). The
  plan's assumption "page_start can only ever come from a reingest lane" is
  necessary but not sufficient — reingest + prov-capture code change.
- **Partial recovery WITHOUT reingest (new finding):** 45.6% of ecom parents
  (4,663/10,222) carry a deterministic page marker — `Page N` heading element
  (4,624) or `## Page N` line in text (4,590). A projection-only backfill can
  parse these into `page_start`/`page_end` payload fields today (Mongo +
  Qdrant `set_payload`, the established `backfill_child_domain.py` pattern) —
  no chunk-id churn, no re-embed, no summary invalidation. Coverage: the four
  scanned books ≈ fully; structurally-parsed books not at all.
- mark (video transcripts) and UGO (EPUB): fixed page numbers do not exist in
  the source medium; `page_start` is permanently N/A regardless of lane.

## 4. THE DISPOSITION MATRIX

Throughput basis for cost cells: P2.7 gate receipts of **37.2 and 22.6
chunks/s** (supplied from the gate-run session; not reproducible from repo docs
today — **ASSUMED**). Cost cells quote single-stream wall-clock ranges at
22.6–37.2 chunks/s; billed worker-seconds = wall-clock x active workers; dollar
conversion needs the endpoint per-second rate (no $ receipt exists in-repo).
Paid-summary costs are quoted in call counts (comparable receipt: P0.1
regenerated 2,633 paid Ghost A summaries in-day).

| Corpus | Heading quality verdict (numbers) | page_start recoverable? | Temporal fields impact | Recommended disposition + why | What it unblocks (S4?) | Cost | Risk |
|---|---|---|---|---|---|---|---|
| **ecommerce_AI_FILM_SCHOOL** | Presence 100% but 7.6% of parents corrupt-flagged, 45.2% page-slug headings, 4.6% OCR markers in text; 9 CORRUPT docs (1,098 parents/4,991 children), 6 SUSPECT (535 p), 3+4 junk/empty docs; 61/79 docs CLEAN | Projection-only for 45.6% of parents (page markers already in data); full coverage needs prov-capture code change + reingest; **reingest alone will NOT add it** | None from reingest: temporal_class/time_expressions ride S1+S4 (summary contract) and T-HOOK-1 (extraction); doc_date/author ride S2 doc-level backfill | **Reingest SUBSET (candidate list §5, 7 docs pending source re-acquisition) + projection-only heading-repair rider for all 79 docs + remove 3 report docs; remainder re-extract-only (S11)**. Reingest-from-same-.md reproduces the source-baked defects, so full reingest buys nothing headings-wise; the real gaps are 3 empty books (need re-conversion/re-OCR upstream, not just reingest) and metadata noise that a deterministic projection repairs | **S4 UNBLOCKED on the 61 clean + 6 suspect docs immediately** (93.9% of parents). Subset docs: land reingest BEFORE S4 so their new parents ride the same paid pass (else re-buy ≤ 1,644 summaries) | Projection repair ≈ $0 (payload updates, no re-embed); subset reingest = docling+chunk+local embed for ~7 docs (wall-clock minutes, no cloud $) — but 3 books need external re-OCR first; full re-extract 56,996 children = 25.5–42.0 min wall at gate throughput | Subset chunk-id churn invalidates the restored ghost_b rows for those docs only (~6.9k of 56,996; replaced by S11 burst anyway — rows untouched today); projection repair fixes metadata, NOT the page-marker noise baked inside embedded text (accepted; full text-clean = reingest-grade) |
| **markbuildsbrands_transcripts** | Presence 100%, **zero corruption flags**; flat by format (905x `Transcript`, 101x `Description`, 8 rich AMA headings); title lives in doc_name | N/A permanently (video transcripts — no pages; timestamp fields would be the analog, different contract) | Same as ecom: rides S1/S2/S4/T-HOOK-1, not reingest | **Re-extract-only** (confirms the Active-PoC consequence already in the ledger). Chunking sound, headings format-honest; nothing for reingest to improve | **S4 UNBLOCKED as-is** — no reingest dependency; S11 burst proceeds per P2.7b | S4: 1,015 paid summaries; S11 re-extract 3,791 children = 1.7–2.8 min wall | Minimal; flat headings limit heading-based facet value (card/doc_name carries the title — P0.5 doc_name backfill covers the rest) |
| **UGO_CORPUS** | Presence 100%, zero corruption flags, but **zero semantic value**: all 203 parents share one machine label `index_split_000` | N/A permanently (EPUB — no fixed pages) | Same seam as pair; UGO is the S4 canary | **Projection-only now** (heading-repair rule maps spine labels → doc title); optional canary reingest LATER, only after an EPUB spine→title chunker fix exists — reingest today reproduces the same junk | **S4 canary UNBLOCKED** (summary quality does not consume heading_path) | ≈ $0 projection; canary reingest (1 doc, 659 children) trivially cheap when justified | None material; risk of over-engineering — do not build an EPUB heading fix for a 1-doc corpus before the pair needs it |
| **polymath_v2** | Presence 86% (112,175); sampled corruption 1.6%, empty 13%; no systemic OCR signature | No — and **prohibited**: reingest is a heavy op on a frozen corpus | Deterministic temporal classifier backfill ONLY (T-MAIN Phase 3, per S4 row); no paid regen, no reingest | **Projection-only (FROZEN)** — restates the owner directive in the ledger; nothing found today justifies unfreezing | Not an S4 target (deterministic backfill only) | $0 beyond the deterministic pass | Leaving 13% headingless parents is accepted debt; P0.5 doc_name/source-identity backfill (projection) is the mitigation |

## 5. ecom reingest-subset candidate list (doc_ids, VERIFIED counts)

Group A — content gap; needs source re-acquisition / re-OCR BEFORE reingest
(reingest of the current .md sources cannot recover content that was never
converted):

| doc_id (prefix) | name | parents | children | stored chars |
|---|---|---|---|---|
| 06bbb7ad8f59 | mateu-mestre-framed-ink-2-2021 | 1 | 1 | 40 |
| b98b6bda8d1b | richard-williams-the-animator-s-survival-kit-2002 | 1 | 1 | 74 |
| be8b85fb4d19 | marcos-mateu-mestre-framed-perspective-volume-1-2016 | 1 | 1 | 84 |
| cf766913f175 | laban-for-actors-and-dancers | 1 | 5 | 6,050 |

Group B — owner-optional re-conversion (scanned books whose headings are page
markers; reingest of same sources reproduces them; only better upstream OCR
changes the outcome):

| doc_id (prefix) | name | parents | children |
|---|---|---|---|
| c2812f1e082d | sound-design-the-expressive-power… | 270 | 1,810 |
| c5e7661fc9ef | joseph-v.-mascelli-the-five-c-s-of-cinematography-1998 | 182 | 478 |
| 6c7171641c01 | framed-ink | 60 | 250 |
| 9faef56709b1 | how-to-draw-manga-illustrating-battles | 34 | 131 |

Group C — projection-repair sufficient (no reingest value; listed because they
drove the CORRUPT verdicts):

| doc_id (prefix) | name | parents | defect |
|---|---|---|---|
| 1f55f7f5c4e3 | facial-action-coding-system (Ekman) | 364 | 150-char bibliographic heading on every parent |
| ed3ed9666e18 | donald-miller-building-a-storybrand-2017 | 68 | EPUB xhtml paths as headings |
| e9e323211dbc | anatomy-for-sculptors | 98 | EPUB split paths + numeric junk headings |
| f509edd4716e | international-journal-of-reasoning… | 21 | long bibliographic heading |

Group D — removal candidates (pipeline reports ingested as content):
`fa763b1cf658` ocr-completion-report (1 parent), `8da8a9754b81`
ocr-marker-append-report (1), `e21e0fd6fcc7` epub-backfill-status-report (6).

Totals if the full candidate set (A+B+C+D) were reingested/removed: 1,644
parents / 6,860 children = 16.1% / 12.0% of the corpus. Recommended actual
reingest set = A (+B only if re-OCR'd sources are provided) — Group C is
projection-repair, Group D is deletion.

## 6. Cost/impact of each disposition option (what each pass invalidates & recovers)

| Option | Invalidates | Cost (receipts basis) | Recovers | Cannot recover |
|---|---|---|---|---|
| Reingest (full/subset) | Chunk IDs of affected docs → their parent summaries (paid), child vectors (local re-embed), ghost_b extraction rows, lexicon/card references; S8 card rebuild absorbs the card side | Docling parse + chunk + local embed (wall-clock, $0 cloud); IF after S4: re-buys paid summaries for affected parents; IF after S11: re-buys extraction at 22.6–37.2 chunks/s | Chunk boundaries, scrubbed markers, page_start (ONLY with the prov-capture code change), missing content (ONLY with re-acquired sources) | Source-baked heading junk (same .md in = same junk out); pages for EPUB/video media |
| Re-extract-only | ghost_b rows of the corpus (replaced); Neo4j promotions downstream | ecom 56,996 children = 25.5–42.0 min wall; mark 3,791 = 1.7–2.8 min; billed worker-seconds = wall x workers (ASSUMED throughput; no in-repo $ rate) | Entity/relation quality, T-HOOK-1 temporal capture in extractions | Anything chunk- or summary-shaped |
| Projection-only | Nothing durable (in-place Mongo + Qdrant payload updates) | ≈ $0; minutes of wall-clock | heading_path normalization (strip OCR markers, map EPUB paths → chapter labels, truncate bibliographic strings, spine label → doc title), page_start for the 45.6% marker-carrying ecom parents, doc_name/source-identity backfill | Embedded text noise (page markers inside vectors), true section structure, content gaps |
| Leave (frozen) | Nothing | $0 | Nothing | Everything above |

Sequencing consequence (the reason S3 precedes S4): every paid artifact bought
before a reingest of the same rows is destroyed by it. With the subset kept to
Group A (+B optional) and landed before S4, the incremental paid-summary cost
of reingest is ~0 (new parents ride the same S4 pass) and the extraction cost
is ~0 (S11 burst re-extracts the pair anyway). The restored ecom ghost_b rows
(56,996) are NOT touched by this audit and remain valid until S11 replaces
them by design.

## 7. Proposed source-safe heading repair rule (P0.5, for sign-off)

Deterministic, reversible (old value archived in a sidecar field), projection-only:

1. Strip `OCR_FROM_IMAGES` / `OCR_FALLBACK_TEXT` elements from heading_path.
2. Parse `Page N` elements → `page_start`/`page_end` payload; drop the element
   from heading_path (page number is provenance, not a heading).
3. Map EPUB machine paths (`*_Chapter_N.xhtml` → `Chapter N`;
   `index_split_NNN.html` / bare spine labels → drop, fall back to doc title).
4. Truncate heading elements > 120 chars to the doc-title segment (before the
   first ` -- `), preserving the full string in the archive field.
5. Never touch chunk text, chunk IDs, vectors, or summaries.

## 8. OWNER SIGN-OFF

*(empty — owner fills; no disposition below is executed until signed)*

| Corpus | Disposition (sign one) | Signed by | Date | Notes/waivers |
|---|---|---|---|---|
| ecommerce_AI_FILM_SCHOOL | reingest full / **reingest subset (recommended: Group A, B optional)** / re-extract-only / projection-only / leave | | | Group D deletions? Group B re-OCR sources available? |
| markbuildsbrands_transcripts | reingest full / reingest subset / **re-extract-only (recommended)** / projection-only / leave | | | |
| UGO_CORPUS | reingest full / reingest subset / re-extract-only / **projection-only (recommended)** / leave | | | canary reingest deferred until EPUB spine fix exists |
| polymath_v2 | reingest full / reingest subset / re-extract-only / **projection-only, FROZEN (recommended)** / leave | | | |
| Heading repair rule (§7) | approve / amend / reject | | | |
| page_start prov-capture code change (§3) | approve for S11-adjacent build / defer | | | required for any future page_start beyond the 45.6% projection |
