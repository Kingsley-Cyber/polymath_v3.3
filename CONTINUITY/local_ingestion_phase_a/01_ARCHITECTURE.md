# Architecture — Fully Local Ingestion

## Pipeline order (final, locked)

```
DOC IN (file upload via API endpoint)
  │
  ▼ docling_svc :8500          PDF/etc → markdown            (already local sidecar)
  │
  ▼ tier_chunker.py             chunks (children + parents)   (already local, Python)
  │
  ▼ schema_lens lookup          per-chunk schema             (already local, Python)
  │
  ▼ Ghost A (summaries)         CLOUD LLM call                (UNCHANGED — leave alone)
  │
  ▼ Ghost B (extraction)        NEW LOCAL STACK              (Phase A — what we build)
  │   ┌─────────────────────────────────────────────────┐
  │   │  GLiNER pass-1: 15 Ghost B entity types         │
  │   │  GLiNER pass-2: facet vocab → object_kind       │
  │   │  GLiREL: 30 Ghost B predicates                  │
  │   │  enrich.py: numeric facts + in-text aliases     │
  │   │  enrich.py: qualitative-fact rules (NEW)        │
  │   └─────────────────────────────────────────────────┘
  │   emits: list[ExtractionResult] — same shape as cloud Ghost B
  │
  ▼ embed_batch                 chunks + summaries → vectors  (already local sidecar :8082)
  │
  ▼ qdrant_writer.upsert        local Docker Qdrant
  │
  ▼ graph_backfill              local Docker Neo4j MERGE
  │
  DONE
```

## The 15 Ghost B entity types (GLiNER pass-1)

Already locked in `local_ghost_b/pipeline_config.py:GHOST_B_ENTITY_TYPES`:
```
Person, Organization, Location, Event, Concept, Method, Product, Software,
Document, Standard, Rule, Law, Artifact, TimeReference, other
```

Actually only 11 are in current `pipeline_config.py` (Person, Organization, Software, Product, Method, Artifact, Concept, Location, Document, Standard, Event). The full 15 includes Rule, Law, TimeReference, other — these were used in the GLiREL training. For Phase A: USE THE 15 (expand pipeline_config to all 15 in Phase A.2).

## The facet vocabulary (GLiNER pass-2 — NEW for Phase A.3)

Goes into `pipeline_config.py:GHOST_B_FACET_VOCAB`. Recommended starting set (refine in smoke test):
```
vector_database, web_framework, embedding_model, dataset, algorithm,
protocol, language, game_engine, library, framework, platform, model,
api, schema, format, plugin, extension, package, runtime, server,
ide, compiler, tool, service, database, ontology, methodology, paradigm
```

Per-entity. Deduped (run once per unique canonical_name across the doc).
Drives `EntityItem.object_kind`.

## The 30 Ghost B predicates (GLiREL)

Already in `local_ghost_b/heads/glirel_ghost_b_v1/labels.json`:
```
part_of, member_of, located_in, works_for, created_by, owns,
affiliated_with, synonym_of, instance_of, example_of, uses, references,
implements, depends_on, produces, stores, detects, supports, defines,
represents, maps_to, preceded_by, causes, overlaps, during, derived_from,
contradicts, excepts, overrides, related_to
```

GLiREL fine-tuned weights live at `models/glirel_ghost_b_v1/best/pytorch_model.bin` (1.87 GB).

## The 9 FactTypes

Already in `backend/services/ghost_b_schemas.py:FactType`:
```
property, status, timestamp, quantity, threshold, category, tag,
rule_condition, rule_action
```

- `quantity, timestamp, threshold, property` → handled by existing `enrich.py` numeric rules
- `status, category, tag, rule_condition, rule_action` → handled by NEW rules in Phase A.4

## Throughput target

| stage | per-chunk wall time | notes |
|---|---|---|
| GLiNER pass-1 | ~80 ms | MPS in-process |
| GLiNER pass-2 (per unique entity, deduped — amortized per chunk) | ~30 ms | MPS in-process |
| GLiREL | ~210 ms | MPS, sentence-windowed (measured Phase 4 baseline) |
| enrich.py P1 + qualitative rules | ~10 ms | CPU |
| **subtotal local Ghost B** | **~330 ms/chunk** | 6× faster than the SLM path |
| embedder (batched, amortized) | ~10 ms equiv | already local |

For 230-chunk file: ~75 sec local Ghost B + ~5 sec embedder + ~30 sec Qdrant/Neo4j ≈ **~2 min total**.
For 450k-chunk full directory: ~40 hr ≈ **~2 days** (vs 10 days with SLM).
