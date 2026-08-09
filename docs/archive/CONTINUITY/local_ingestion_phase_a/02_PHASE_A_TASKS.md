# Phase A — Task list with deliverables + acceptance criteria

7 sub-tasks. ~10 hr total. Sequential dependencies noted.

---

## A.1 — Inspect cloud Ghost B's `ExtractionResult` emit path (task #45)

**Goal**: nail the exact field layout of `ExtractionResult` so local Ghost B emits a byte-compatible shape.

**Files to study**:
- `/Users/king/polymath_v3.3/backend/services/ghost_b.py` — the cloud Ghost B implementation
  - Find: `class ExtractionResult` at line ~1735 (dataclass)
  - Find: `EntityItem`, `RelationItem`, `FactItem` dataclasses (lines ~1676, ~1699, ~1724)
  - Find: where `extract_entities()` constructs `ExtractionResult` — read the final return path
  - Find: how confidence is set (cloud uses LLM-reported `cf`; local will use sentinels)
  - Find: how `schema_lens_id` is set (read line ~1772)
  - Find: how the Phase-14 counters (`entity_remap_count`, `evidence_drop_count`, etc.) are populated — local lane sets these to 0 unless we trigger the same logic
- `/Users/king/polymath_v3.3/backend/services/ghost_b_schemas.py` — Pydantic `LLMEntity`, `LLMFact`, `LLMRelation`, `FactType` (already studied; data layer = dataclass, schema layer = Pydantic)
- `/Users/king/polymath_v3.3/backend/services/ingestion/worker.py:_b_branch()` (line ~754) — how the worker calls Ghost B and what it does with the result

**Deliverable**: a short markdown doc (`03_FILE_MAP.md` already has the high-level; this just nails which fields need exact-match population).

**Acceptance**: enumerate every field of `ExtractionResult`, `EntityItem`, `RelationItem`, `FactItem` and note how local Ghost B should populate each.

---

## A.2 — Build `backend/services/ghost_b_local.py` (task #46)

**Goal**: the local extractor module that orchestrates GLiNER ×2 + GLiREL + enrich + qualitative rules and emits `list[ExtractionResult]`.

**API to match** (drop-in replacement for what `_b_branch()` calls today):
```python
async def extract_entities(
    chunks: list[dict],          # children chunks from tier_chunker
    schema_lens: SchemaLens,     # already computed by worker
    corpus_id: str,
    doc_id: str,
    # ... whatever cloud extract_entities takes; A.1 enumerates this exactly
) -> list[ExtractionResult]:
    ...
```

**Internal flow per chunk**:
1. GLiNER pass-1 → entities (with entity_type from the 15)
2. enrich.py Pass-1 → numeric facts + in-text aliases (mutates the entities)
3. enrich.py qualitative rules → status/category/tag/rule_* facts (NEW, Phase A.4)
4. (deferred but deduped at chunk level) collect entities for the doc-level facet pass
5. GLiREL → relations between entity pairs
6. Construct `EntityItem` + `RelationItem` + `FactItem` dataclasses
7. Wrap in `ExtractionResult`

**Doc-level pass** (after per-chunk loop):
- Dedup entities by `canonical_name` across all chunks
- For each unique entity, run GLiNER pass-2 against facet vocab → `object_kind`
- Mutate the `EntityItem.object_kind` for each occurrence

**Acceptance**: function signature matches cloud's; produces valid `ExtractionResult` list; passes Pydantic via `LLMEntity`/`LLMFact`/`LLMRelation`; no SLM call; runs in <1s per chunk on M1 Max.

---

## A.3 — GLiNER pass-2 facet tagger (task #53)

**Goal**: a new module `backend/services/ingestion/facet_tagger.py` that classifies a list of unique entities against the facet vocab in one GLiNER call.

**API**:
```python
def tag_facets(
    entities: list[EntityItem],
    chunks_text_lookup: dict[str, str],  # chunk_id → text, for entity context
) -> None:
    """Mutates entities in-place: sets entity.object_kind via GLiNER pass-2.
    Deduped — runs once per unique canonical_name."""
```

**Implementation**:
- Use existing GLiNER instance (lazy-loaded, shared with pass-1)
- For each unique entity, build a context string (first-occurrence chunk text, deterministic)
- Call `model.predict_entities(context, GHOST_B_FACET_VOCAB, threshold=0.45)`
- Pick the facet that matches the entity's surface form / canonical name
- Set `entity.object_kind = <facet label>`

**Add** to `local_ghost_b/pipeline_config.py`:
```python
GHOST_B_FACET_VOCAB = ["vector_database", "web_framework", ...]  # see 01_ARCHITECTURE.md
```

**Acceptance**: facets land on the entity dataclass; no LLM call; runs ~30 ms per unique entity.

---

## A.4 — `enrich.py` qualitative-fact rules (task #54)

**Goal**: extend `backend/services/ingestion/enrich.py` to deterministically structure the 5 `SLM_GATED` FactTypes.

**Functions to add**:
```python
def extract_qualitative_facts(text: str, entities: list[dict]) -> list[dict]:
    """Returns LLMFact-shaped dicts for status / category / tag / rule_condition / rule_action.
    Rules:
      - For each cue match in text, attach to nearest in-sentence entity (subject)
      - value = the matched phrase (verbatim substring of text)
      - property_name = inferred from the cue type (e.g. status → 'maturity' or 'lifecycle')
      - condition = filled for rule_action when there's a matching rule_condition nearby
    All deterministic, no LLM."""
```

**Pattern (same as cascade compiler)**:
- `CUES["status"]` matches "production-ready", "deprecated", "stable", etc.
  - subject = nearest entity to the cue
  - value = the matched phrase
  - property_name = "maturity"
- `CUES["category"]` matches "is a/an X", "kind of X", etc.
  - value = the X
  - property_name = "category"
- `CUES["tag"]` matches `^tags?:` lines
  - value = each comma-split tag
  - property_name = "tags"
- `CUES["rule_condition"]` matches "if/when/unless X"
  - condition = X
- `CUES["rule_action"]` matches "must/shall/never X"
  - value = X
  - If a `rule_condition` fires in the same sentence: attach as `condition`

**Update** `enrich.extract()` to merge qualitative-fact output into the per-chunk dict.

**Acceptance**: emits LLMFact-shaped dicts that pass Pydantic validation; precision target ≥90%; recall acceptable (~60-75% of SLM recall is fine).

---

## A.5 — Replace `_b_branch()` call in `worker.py` (task #49)

**Goal**: route `_b_branch()` to `ghost_b_local.extract_entities` instead of `services.ghost_b.extract_entities`.

**File**: `/Users/king/polymath_v3.3/backend/services/ingestion/worker.py:_b_branch()` (line ~754).

**Change**: replace the import + call. No env flag. No `LOCAL_GHOST_B_LANE` switch.

```python
# OLD
from services.ghost_b import extract_entities
...
results = await extract_entities(...)

# NEW
from services.ghost_b_local import extract_entities
...
results = await extract_entities(...)
```

**Cloud `services/ghost_b.py` stays in the repo as orphaned code** — do not delete. May be useful as a reference for `ExtractionResult` field semantics.

**Acceptance**: worker calls local extractor; cloud module untouched; no `lane` column added to Mongo (no lanes).

---

## A.6 — Smoke test on a real markdown (task #51)

**Goal**: prove the full chain works end-to-end on one real file.

**Test plan**:
1. Pick a small file from `/Volumes/Flash Drive/merged/` (e.g., `flame_engine_docs_complete.md` — 9 KB, ~8 chunks)
2. Run through the ingestion endpoint with `LOCAL_GHOST_B_LANE` flag NOT needed (local is the only path now)
3. Verify in Neo4j: entities exist with `object_kind` populated, relations exist, facts exist (mix of numeric + qualitative)
4. Verify in Qdrant: chunks + summaries embedded
5. Check counts: chunks > 0, entities > 0, relations > 0, facts > 0

**Acceptance**: file ingested end-to-end without exception; Neo4j MERGE succeeds; per-chunk wall time <500 ms.

---

## A.7 — Commit + push (task #52)

**Goal**: land Phase A on GitHub.

**Suggested commit grouping** (3-4 focused commits):
1. Add `services/ghost_b_local.py` (the new extractor)
2. Add `services/ingestion/facet_tagger.py` (GLiNER pass-2 module)
3. Extend `services/ingestion/enrich.py` with qualitative-fact rules
4. Wire `worker.py` `_b_branch` to call local extractor (no flag)

**Important**: commit author MUST be `Kingsley <ezeokonkwokingsley@gmail.com>`:
```bash
git -c user.name="Kingsley" -c user.email="ezeokonkwokingsley@gmail.com" commit -m "..."
```

**Co-author trailer required**: `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` (or 4.8 if running on that).

**Don't push without confirming with user**.

---

## Sequencing

A.1 unblocks A.2 (need ExtractionResult shape before building extractor).
A.2 + A.3 + A.4 can be built in parallel after A.1 (they're independent modules).
A.5 depends on A.2, A.3, A.4 being complete.
A.6 depends on A.5.
A.7 depends on A.6 passing.

Reasonable order: A.1 → A.4 (cheap, pure Python) → A.3 (small module) → A.2 (the heart, depends on A.3 + A.4) → A.5 → A.6 → A.7.
