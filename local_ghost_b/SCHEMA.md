# local_ghost_b — locked schema contracts

Single-source-of-truth field layouts for every JSONL the pipeline reads or
writes. Anything not listed here is undefined — don't rely on it.

Schema version: matches `pipeline_config.PIPELINE_VERSION` (`v1.2026.06`).

---

## 1. Chunks JSONL (extractor input)

Produced by `tools/chunk_with_gliner.py` (or any upstream that follows
this shape). One JSON object per line.

```json
{
  "chunk_id": "<sha256(doc_name+head)>_<NNNN>",
  "doc_id":   "<sha256(doc_name+head)>",
  "text":     "<full chunk text, code/URL/citation stripped>",
  "entities": [
    {
      "canonical_name": "flame",
      "surface_form":   "Flame",
      "entity_type":    "Software",
      "query_aliases":  ["flame engine", "FLAME"]
    }
  ]
}
```

| field | type | required | notes |
|---|---|---|---|
| `chunk_id` | str | ✓ | unique across all chunks in the run |
| `doc_id` | str | ✓ | shared across chunks from the same doc |
| `text` | str | ✓ | whitespace-tokenizable; positions are word-index |
| `entities` | list[dict] | ✓ | empty list is allowed but means no extraction possible |
| `entities[].canonical_name` | str | ✓ | lowercase, used for graph node identity |
| `entities[].surface_form` | str | ✓ | case-preserved as found in text |
| `entities[].entity_type` | str | ✓ | one of `pipeline_config.GHOST_B_ENTITY_TYPES` (11 values) |
| `entities[].query_aliases` | list[str] | optional | alternate surface forms seen in this chunk |

**Tokenizer alignment**: `text.split()` produces the tokens that GLiREL
position indices refer to. Don't change the tokenizer in chunker or
classifier — they must match.

---

## 2. Relations JSONL (extractor output)

Produced by `run_on_mac.py` regardless of `--classifier {cascade,glirel,ensemble}`.
One JSON object per relation.

```json
{
  "t":        "r",
  "sub":      "disclosure",
  "pred":     "preceded_by",
  "obj":      "data",
  "ok":       "entity",
  "cf":       0.836,
  "ev":       "...suppressing the sensitive data before any disclosure...",
  "cue":      "before any",
  "chunk_id": "abc123...0004",
  "doc_id":   "abc123..."
}
```

| field | type | required | notes |
|---|---|---|---|
| `t` | str | ✓ | always `"r"` (relation row) |
| `sub` | str | ✓ | subject canonical_name |
| `pred` | str | ✓ | one of the 30 Ghost B predicates OR `"related_to"` |
| `obj` | str | ✓ | object canonical_name |
| `ok` | str | ✓ | always `"entity"` (not `"literal"`) |
| `cf` | float | ✓ | confidence ∈ [0, 1] |
| `ev` | str | ✓ | evidence sentence (the same-sentence window) |
| `cue` | str | ✓ | inter-entity cue phrase (may be empty) |
| `chunk_id` | str | ✓ | matches input chunk |
| `doc_id` | str | ✓ | matches input chunk |

**Predicate vocabulary**: see `heads/glirel_ghost_b_v1/label_descriptions.json`
(30 typed + `no_relation`). Output `pred` is one of those OR `related_to`
(the fallback when no typed predicate is confidently committed).
**`no_relation` never appears in output** — it's an internal classifier label
that gets demoted to `related_to` by the safety layer.

---

## 3. label_descriptions.json (classifier input)

Lives at `heads/glirel_ghost_b_v1/label_descriptions.json`. Defines the
ontology GLiREL classifies against. **Ships with the bundle, independent
of training weights.**

```json
{
  "_schema": "polymath.ghost_b.label_descriptions.v1",
  "_notes":  "...",
  "depends_on": "Subject requires Object to function...",
  "part_of":    "Subject is a component or module of Object...",
  ...
  "no_relation": "Subject and Object co-occur but no specific predicate applies."
}
```

| field | type | required | notes |
|---|---|---|---|
| keys starting with `_` | any | optional | metadata (schema name, notes); filtered out at load time |
| `<predicate_name>` | str | ✓ | natural-language description used at zero-shot inference and as canonical ontology reference |

**Must include `no_relation`** as a label — classifier needs it to abstain.

---

## 4. GLiREL training JSONL (for the RTX fine-tune)

When training data prep happens (Phase 1, not yet implemented), output
must conform to GLiREL's native format:

```json
{
  "tokenized_text": ["Flame", "is", "a", "modular", "Flutter", "game", "engine", "."],
  "ner": [
    [0, 0, "Software", "Flame"],
    [4, 6, "Software", "Flutter game engine"]
  ],
  "relations": [
    {
      "head":          {"mention": "Flame",               "position": [0, 0], "type": "Software"},
      "tail":          {"mention": "Flutter game engine", "position": [4, 6], "type": "Software"},
      "relation_text": "part_of"
    }
  ]
}
```

| field | type | notes |
|---|---|---|
| `tokenized_text` | list[str] | result of `text.split()` — must match inference |
| `ner` | list[[start, end_inclusive, type, mention]] | word-index positions |
| `relations[]` | list[dict] | one per labeled pair, includes negatives with `relation_text: "no_relation"` |

Position convention at training: **end-inclusive**. At inference GLiREL
emits **end-exclusive** in `head_pos` / `tail_pos`. The classifier
normalizes this difference.

---

## 5. Bundle directory layout

```
local_ghost_b/heads/
  relation_exists_v1/           BINARY GATE (filters pairs before classifier)
    model.safetensors
    config.json
    tokenizer.json
    ...

  backbone_v1/                  BERT CASCADE (11 predicates + none)
    model.safetensors
    label_map.json
    ...

  easy_predicate_v1/            BERT CASCADE (7 distinctive predicates)
    ...

  family_v1/                    BERT CASCADE (8-way family router)
    ...

  glirel_ghost_b_v1/            FINE-TUNED GLiREL classifier slot
    README.md                   <-- documents what goes here
    label_descriptions.json     <-- ships with bundle, defines ontology
    model.safetensors           <-- DROPS IN after RTX training
    config.json                 <-- DROPS IN
    tokenizer.json              <-- DROPS IN
    tokenizer_config.json       <-- DROPS IN
    special_tokens_map.json     <-- DROPS IN
    spm.model                   <-- DROPS IN (if sentencepiece-based)
```

When `glirel_ghost_b_v1/model.safetensors` is absent, the classifier
falls back to the zero-shot model (`jackboyla/glirel-large-v0` by
default) with a loud WARNING log line. Wiring stays the same.

---

## Versioning rule

Bump `pipeline_config.PIPELINE_VERSION` and create a new bundle dir
(`glirel_ghost_b_v2/`) when any of these change:

- GLiNER model or entity type list
- chunker target/min chars or noise-strip patterns
- gate threshold
- cascade head label maps
- relation predicate vocabulary or label descriptions

Don't mutate v1 in place after training data exists for it — keep the
old bundle around so re-runs are reproducible.
