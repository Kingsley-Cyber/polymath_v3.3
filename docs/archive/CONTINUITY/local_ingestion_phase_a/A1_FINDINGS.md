# A.1 — Cloud Ghost B emit-path findings

Direct quotes from `/Users/king/polymath_v3.3/backend/services/ghost_b.py`.
This is the contract `ghost_b_local.extract_entities()` must match.

## Entry point signature

```python
async def extract_entities(
    tasks: list[ExtractionTask],
    model: str | None = None,
    schema: SchemaContext | None = None,
    schema_lens: SchemaLens | dict | None = None,
    chunk_vectors: dict[str, list[float]] | None = None,
    schema_resolver: SchemaResolver | None = None,
    *,
    pool: list[dict] | None = None,
    return_report: bool = False,
    enable_facts: bool | None = None,
    audit_event_sink: GhostBAuditSink | None = None,
    audit_run_id: str | None = None,
) -> list[ExtractionResult] | ExtractionBatchReport:
    if not tasks:
        return []
    ...
```

(Defined at `ghost_b.py:3209`.)

## What the local extractor MUST honor

| param | local-lane behavior |
|---|---|
| `tasks` | iterate; one `ExtractionResult` per task |
| `model` | **ignore** (no LLM in local) |
| `schema` | **honor if present**: respect `entity_schema` / `relation_schema` / `strict` semantics for sentinels — see SchemaContext semantics in ghost_b.py:932+ |
| `schema_lens` | **honor if present**: bias predicate vocab; corpus-narrowed types come through here |
| `chunk_vectors` | **ignore** (cloud uses for schema retrieval; local doesn't need) |
| `schema_resolver` | **ignore** (cloud-specific) |
| `pool` | **ignore** (LLM provider pool) |
| `return_report` | **honor**: when True, return `ExtractionBatchReport` instead of `list[ExtractionResult]` |
| `enable_facts` | **honor**: when False, skip fact extraction entirely (default reads settings) |
| `audit_event_sink` / `audit_run_id` | **ignore** but accept the kwargs (no-op) |

## Input dataclass — ExtractionTask

```python
@dataclass
class ExtractionTask:
    chunk_id: str
    doc_id: str
    corpus_id: str
    text: str                # child chunk text only
    chunk_kind: str = "body"
    metadata: dict = field(default_factory=dict)
```

(at `ghost_b.py:1665`.)

## Output dataclasses (the contract)

### EntityItem (ghost_b.py:1675)

```python
@dataclass
class EntityItem:
    canonical_name: str           # lowercased, normalized
    surface_form: str             # as appears in text
    entity_type: str              # one of 15 Ghost B types
    confidence: float             # local-lane sentinel: GLiNER softmax
    query_aliases: list[str] = field(default_factory=list)  # in-text via Schwartz-Hearst
    definitional_phrase: str = ""    # optional 1-sentence def pulled from chunk
    object_kind: str = ""         # Pt9b — facet noun (e.g., "vector_database")
```

### RelationItem (ghost_b.py:1698)

```python
@dataclass
class RelationItem:
    subject: str                  # canonical_name of subject entity
    predicate: str                # one of 30 Ghost B predicates
    object: str                   # canonical_name OR literal string
    object_kind: str              # "entity" | "literal"
    confidence: float             # local-lane sentinel: GLiREL score
    evidence_phrase: str = ""     # the same-sentence window
    relation_cue: str = ""        # cue word/phrase (optional, may be empty)
    source_predicate: str | None = None    # pre-normalization label if remapped
    validation_status: str | None = None   # set by downstream validation
```

### FactItem (ghost_b.py:1723)

```python
@dataclass
class FactItem:
    subject: str                  # canonical_name of subject entity
    fact_type: FactType           # one of 9 Literal types
    property_name: str            # the attribute (e.g., "version", "maturity")
    value: str                    # the value
    unit: str | None              # unit if present, else None
    condition: str | None         # for rule_action: trigger condition, else None
    confidence: float             # local-lane sentinel:
                                  #   1.0 for Pass-1 deterministic numeric facts
                                  #   0.9 for Pass-1.5 qualitative-fact rules
    evidence_phrase: str          # source phrase from chunk
```

### ExtractionResult (ghost_b.py:1735)

```python
@dataclass
class ExtractionResult:
    schema_version: str           # ← what to set? See below.
    chunk_id: str                 # from ExtractionTask.chunk_id
    doc_id: str                   # from ExtractionTask.doc_id
    corpus_id: str                # from ExtractionTask.corpus_id
    entities: list[EntityItem] = field(default_factory=list)
    relations: list[RelationItem] = field(default_factory=list)
    facts: list[FactItem] = field(default_factory=list)
    text: str = ""                # Pt 10b — the chunk text, carried through

    # Phase 14 observability counters (per-chunk; corpus-aggregated later)
    entity_remap_count: int = 0          # local: 0 (no soft-remap logic)
    entity_drop_count: int = 0           # local: 0 (no hard-drop unless schema=strict)
    relation_remap_count: int = 0        # local: increment when GLiREL pred → related_to via safety gate
    relation_drop_count: int = 0         # local: increment for direction-collapse drops
    domain_range_remap_count: int = 0    # local: 0 (no D/R logic yet)
    domain_range_warn_count: int = 0     # local: 0
    endpoint_completion_count: int = 0   # local: 0 (don't synthesize endpoint entities)
    evidence_cue_repair_count: int = 0   # local: 0
    evidence_drop_count: int = 0         # local: increment when a relation has no traceable evidence_phrase
    fact_drop_count: int = 0             # local: increment for Pydantic-rejected facts
    schema_lens_id: str | None = None    # passed through from schema_lens arg
```

## Critical fields to populate correctly

### `schema_version`
Cloud uses `"polymath.extract.v1"` (per earlier inspection of sample chunks). Local should use the same value to preserve graph_backfill behavior. **Action**: hardcode `"polymath.extract.v1"`.

### `ExtractionResult.text`
**Must be the chunk text**. `graph_backfill.resolve_ontology_metadata` uses it as `text_context` for taxonomy lookup. Skipping breaks ~99% of object_kind / domain_type resolution downstream. So local Ghost B MUST populate `result.text = task.text`.

### Confidence sentinels (locked decision)
- `EntityItem.confidence` = GLiNER softmax score directly (typically 0.4-1.0)
- `RelationItem.confidence` = GLiREL score directly (typically 0.4-1.0)
- `FactItem.confidence` =
  - 1.0 for Pass-1 deterministic (quantity / timestamp / threshold / property)
  - 0.9 for Pass-1.5 qualitative rules (status / category / tag / rule_*)

### `RelationItem.object_kind`
Always `"entity"` in local lane (we don't emit literal objects yet — the literal case is for Ghost A facts like dates, which Ghost A handles). All GLiREL relations are entity-to-entity.

## ExtractionBatchReport (ghost_b.py:1787)

```python
@dataclass
class ExtractionBatchReport:
    results: list[ExtractionResult]
    failures: list[ExtractionFailureItem]
    metrics: dict
```

For local lane: `failures` will rarely populate (no LLM call, no network failures). Construct only when `return_report=True` is passed.

## Worker.py call site

```python
# worker.py:_b_branch (line ~754)
# ... extracts tasks via tier_chunker, computes schema_lens, etc.
results = await extract_entities(
    tasks,
    schema=schema_context,
    schema_lens=schema_lens,
    chunk_vectors=chunk_vectors,
    schema_resolver=schema_resolver,
    return_report=True,
    enable_facts=enable_facts,
)
```

**Action for A.5**: change `from services.ghost_b import extract_entities` to `from services.ghost_b_local import extract_entities`. That's the entire change. All call-site kwargs stay compatible — local extractor accepts and ignores cloud-specific kwargs.

## Other imports the worker pulls

```python
from services.ghost_b import (
    EntityItem,                    # data layer — keep importing from ghost_b
    ExtractionBatchReport,         # data layer — keep
    ExtractionFailureItem,         # data layer — keep
    ExtractionResult,              # data layer — keep
    ExtractionTask,                # data layer — keep
    FactItem,                      # data layer — keep
    RelationItem,                  # data layer — keep
    SchemaContext,                 # data layer — keep
    extract_entities,              # ← THE ONLY ONE TO REROUTE
)
```

So A.5 only reroutes `extract_entities`. All the dataclass imports continue to come from `services.ghost_b` — those are the shared data layer.

## Validation

Before constructing `EntityItem` / `RelationItem` / `FactItem` dataclasses inside `ghost_b_local.py`, validate via Pydantic:

```python
from services.ghost_b_schemas import LLMEntity, LLMFact, LLMRelation

try:
    cand = LLMEntity(
        canonical_name=...,
        surface_form=...,
        entity_type=...,
        confidence=...,
        query_aliases=...,
        object_kind=...,
    )
except ValidationError:
    drop_entity()   # increment entity_drop_count
    continue
# only THEN construct EntityItem(**cand.model_dump())
```

This catches:
- `fact_type` not in the 9-Literal
- canonical_name length > 200
- query_aliases > 5
- confidence out of [0,1]
- etc.

## A.1 — done. Next: A.4 (qualitative-fact rules) or A.3 (facet tagger).
