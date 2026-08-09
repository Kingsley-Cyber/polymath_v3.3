# Noise-proofing pass — 2026-06-10 (commits 389a9ca + b666785)

User directive: "ensure that this repo and rag system is noise proof for any
ingestion." Every gate below is deterministic and was validated e2e on the
flame doc (3 ingest cycles, corpora 56bad53d → 25af55e1 → e896a2ce).

## Gates added (where noise dies)

| layer | gate | kills |
|---|---|---|
| docling_adapter (markdown decode) | YAML frontmatter strip (doc start, ≤4 KB) | `source_url/extracted/priority` headers → were embedding noise + a confident junk `timestamp: 2026` fact |
| docling_adapter | leading bold-key metadata block strip (≥2 `**Key:** value` lines after title; title kept; single notes safe) | the merge script's `**Source:**/**Extracted:**/**Priority:**` block (17/523 files) |
| ghost_b_local (GLiNER input) | `strip_noise` (inline code, URLs, md links) — facts/evidence stay on RAW text | code-identifier and URL-host entity mistags |
| ghost_b_local (post-dedup) | `GLINER_ENTITY_CONF_FLOOR=0.55` for all-lowercase single words | `engine`@0.49-style low-confidence generics; proper nouns/acronyms/multi-word exempt |
| ghost_b_local (post-dedup) | `GENERIC_ENTITY_BLOCKLIST` (~180 single-word generics, pipeline_config) | `way/set/system/components/tutorials/...` at any confidence; multi-word forms survive |
| enrich (segmentation) | `_sentences()`: per-line split, markdown headings skipped | heading runs gluing into giant pseudo-sentences → corrupted fact subjects/evidence/definitional capture |
| enrich (property facts) | `_JUNK_PROPERTY_VALUES` | `default: true` → `property: 'true'` junk |
| (existing, kept) | entity-anchoring requirement on every fact | metadata lines with no entity produce nothing |

## Parity punch list landed in the same pass

- `EntityItem.definitional_phrase` now populated locally (the "X is a Y"
  sentence; 12/14 entities on the final flame ingest) — cloud-parity field.
- Facet context preference: definitional sentence → first-occurrence chunk.
  Restored flame→game_engine; coverage 5/14 graph-side, conservative policy.
- Category-fact subject corrected (was `engine`, now `flame`) by the
  segmentation fix.

## Final flame e2e numbers (corpus e896a2ce)

verify ok=true; 12 chunks ×3 collections; entities 14 (0 junk), 12 with
definitional_phrase; facts clean (0 frontmatter facts); 351 ms/chunk warm
standalone; deterministic byte-for-byte.

## Known residuals (ranked, all bounded)

1. **Anchor-link markup in heading-adjacent chunks** (`[¶](https://…#x "Link to
   this heading")`) still embeds — `_scrub_markup_noise` runs on the OCR tier,
   not the tier-A markdown section path. Graph unaffected (links stripped from
   GLiNER input). Fix = apply the md-link scrub in the tier-A path; touchy
   chunk-boundary surgery, do it with the 128-token chunker flip.
2. **License-footer chunks** (CC BY boilerplate; ~1 per scraped file, ranked #3
   in hits). Real page content; low harm; candidate for a footer classifier.
3. **Code chunk as vector top-1** for "what is X built on" — arguably correct
   for technical corpora; the reranker's job, not extraction's.
