# 128-token children: A/B results + what changed (2026-06-10, commit 6e071d9)

## What changed

| knob | was | now |
|---|---|---|
| corpus default `child_chunk_tokens` | 128 / **500** / 700 | 64 / **128** / 256 (new `ChildTokenBudget`, relaxed floors — the shared TokenBudget floors made the range unrepresentable) |
| tier_chunker fallback | min 128 / target 350 / max 512 | min 64 / target 128 / max 256 |
| prose noise | heading anchor links (`[¶](url "Link…")`), inline md links, image markdown, bare URLs survived into chunks | scrubbed at parse (paragraphs, headings, table cells); **code blocks verbatim** |

Existing corpora keep their stored budgets (config frozen per corpus); new
corpora get 128. Parents unchanged (1200) — small-to-big intact.

## A/B (same 3 files: flame prose 9KB, executorch tables 24KB, projective-techniques psych 47KB)

Control = explicit 500-target corpus; Treatment = new 128 defaults.
Chunks: 95 → 215 (2.26×). Ingest wall time: 90s vs 100s (~neutral).

6 probe queries, vector lane (naive collection), top-3:

| query | control | treatment |
|---|---|---|
| What is Flame built on? | same hits | same hits (tiny doc — chunks barely change) |
| Tap input in Flame? | same | same |
| Min Android version (table) | answer at #1; #2–3 = pub.dev boilerplate (1/3 relevant) | **3/3 relevant** Platform Support hits, score 0.737→0.778 |
| iOS backends (table) | BackendQuery + Default-Backends tables (good) | Platform Support table ×2 + body, higher scores — comparable |
| **Purpose of projective techniques (cross-domain)** | three tangential survey paragraphs, none direct | **direct answer at #1** ("Among the chief purposes of…"), 0.677→0.717 |
| Criticisms of projective tests | correct #1 | same correct #1, higher score |

**Verdict: no query regressed; the cross-domain query — the original motivation
for small chunks — flipped from miss to direct hit.** Treatment scores were
uniformly higher (+0.02–0.05): purer chunks match queries tighter.

Note on raw top-3 diversity: treatment sometimes returns multiple row-group
chunks of the SAME table — in production the retriever dedupes by parent_id, so
this collapses to one parent + the next candidates. Raw Qdrant top-3 understates
production diversity.

## Operational note

The three A/B-era corpora (flame_smoke/noiseproof/v2, table_facts_smoke,
ab_control_500, ab_treatment_128) are disposable test corpora — delete via the
UI/API whenever; the corpus-scoped verify fix means leftovers don't break
re-ingests.
