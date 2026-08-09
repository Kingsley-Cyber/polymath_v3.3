# Table fact extraction — local execution half (2026-06-10, commit d2eb8e4)

The repo already had the table concept: docling_adapter detects pipe tables and
linearizes them (`Table:/Section:/Caption:/Columns:` + `Row N: col=val; …`),
tier_chunker keeps rows intact with header context repeated per child,
ChunkKind.TABLE is retrievable AND extractable, and cloud Ghost B injected
table prompt rules (`_render_table_extraction_rules`). What was missing was the
LOCAL extraction step — chunk_kind/metadata didn't survive the wire, and no
deterministic extractor consumed the linearized rows.

## What was added

| piece | where | behavior |
|---|---|---|
| wire fields | `ghost_b_local._task_dict` + sidecar `TaskIn` | `chunk_kind` + `columns` (slimmed from chunk metadata) travel to the extractor |
| `extract_table_facts` | `enrich.py` | row label → subject (only when the row's first pair IS the table's first declared column and names something — else Table/Section title); column header → property_name; cell → verbatim value; `quantity` for number+unit cells else `property`; `'; '`-in-cell recovery via known-column key matching; junk values skipped; evidence = the Row line; conf 1.0; deterministic cap `TABLE_MAX_FACTS_PER_CHUNK=24` |
| `table_entity_text` | `enrich.py` | GLiNER input = cell VALUES only — headers/captions/scaffolding never become entities (cloud rule, enforced by construction) |
| routing | `ghost_b_local._extract_raw` | `kind=="table"` → table extractor + values-only GLiNER; skips GLiREL (rows aren't relational prose), aliases, definitional |

## Validation

- Unit: subjects, semicolon recovery, junk skip, quantity typing, missing-label
  fallback to title, cap, determinism, no-op on non-linearized text.
- Flame regression: prose path byte-identical (18/6/8).
- E2E (`08_executorch_flutter_plugin.md`, corpus 6a31c576): 53 chunks (9 table /
  16 code / 28 body), verify ok=true, **34 of 43 facts table-sourced** —
  e.g. `android.min version = API 23`, `ios.backends = XNNPACK, CoreML, MPS,
  Vulkan*`, `macos.architectures = arm64, x86_64`.

## Sizing

196/523 merged files contain pipe tables (~107k table lines) → tables are the
single largest fact source for the backfill.

## Known limits

- Table subjects are row-label strings; when GLiNER doesn't also tag that value
  as an entity, the Fact lands without an Entity HAS_FACT link (chunk
  SUPPORTS_FACT provenance still present). Values-only GLiNER usually tags row
  labels, so linkage is the common case.
- Tables without `Row N:` lines (linearizer fallback / pre-existing raw tables)
  no-op gracefully — no facts, no errors.
- Relations are intentionally NOT extracted from tables (no relational prose);
  cross-row comparisons are query-time work over the property facts.
