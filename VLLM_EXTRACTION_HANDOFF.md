# vLLM Constrained-Decoding Extraction — RTX Setup Handoff

Goal: ONE extraction engine on the RTX box — a small LLM served by vLLM
with **constrained (guided) decoding** to the Polymath extraction schema.
GLiNER is fully retired from serving. RunPod model: one model, full card.

Polymath already has the seam (`ghost_b_llm` engine, json_schema lane).
This doc is everything the box needs so Polymath can call it and qualify it.

---

## 1. Retire GLiNER (free the whole GPU)

```bash
sudo systemctl stop  relex-sidecar@8738 relex-sidecar@8739 relex-sidecar@8740 relex-sidecar
sudo systemctl disable relex-sidecar@8738 relex-sidecar@8739 relex-sidecar@8740   # keep :8737 unit installed as cold fallback, just stopped
/usr/lib/wsl/lib/nvidia-smi --query-gpu=memory.free --format=csv,noheader        # expect ~90 GB free
```

## 2. vLLM launch config (full card + guided decoding)

The engine-exit you hit was KV-cache starvation — it must own the card:

```
--model /models/qwen            # Qwen3-4B-Instruct-2507 (or your chosen SLM)
--served-model-name polymath-extract
--gpu-memory-utilization 0.90   # THE fix: was starved
--guided-decoding-backend xgrammar   # constrained decoding (outlines also fine)
--max-model-len 8192
--host 0.0.0.0 --port 8000
--api-key <the lane-manager key>     # same X-Api-Key / Bearer already in use
```

Guided decoding is the whole point: with `xgrammar`, vLLM makes
schema-invalid output physically impossible — the model can only emit
tokens the JSON grammar allows. Determinism by construction.

Keep it behind the lane manager `/up` `/down` at :8085 and the
OpenAI-compatible endpoint at `:8000/v1` exactly as today. Nothing else
changes; Polymath already speaks this.

## 3. Model choice

Default: **Qwen3-4B-Instruct-2507** (`polymath-extract`) — strong at
constrained JSON, fits the card many times over, fast. Swap freely for
any instruct SLM; only requirement is json_schema/guided-decoding support
(all modern vLLM builds have it). Bigger models raise quality but lower
throughput — for extraction, 4B + constraint is the sweet spot.

## 4. The constraint Polymath sends (do NOT hand-build — FYI only)

Polymath generates the JSON grammar from its frozen `ExtractionResponse`
pydantic model and sends it as `response_format={"type":"json_schema",
"json_schema":{...,"strict":true}}`. The vocabulary the grammar enforces:

ENTITY TYPES (15): Person, Organization, Location, Event, Concept, Method,
Product, Software, Document, Standard, Rule, Law, Artifact, TimeReference, other

PREDICATES (31): part_of, member_of, located_in, works_for, created_by,
owns, affiliated_with, synonym_of, instance_of, uses, runs_on, trained_on,
references, implements, depends_on, produces, consumes, stores, detects,
supports, defines, represents, maps_to, preceded_by, causes, overlaps,
derived_from, contradicts, excepts, overrides, related_to

Type-pair constraints (which predicate fires between which entity types)
live in `config/ontology.yaml` and are enforced Polymath-side after
decode. The model only needs to emit the schema; Polymath validates pairs.

## 5. Verification (run after /up)

```bash
curl -s http://192.168.1.83:8000/v1/models -H "Authorization: Bearer <key>"   # served-model listed
# Polymath side proves guided decoding + schema conformance automatically
# during the qualification battery (next step, Mac-driven).
```

## 6. What Polymath does once this answers (no RTX action)

1. Loads `config/ontology.yaml` into a qualification corpus as
   entity_schema/relation_schema, schema_strict=hard.
2. Runs the extraction battery with a PRODUCTION-ONTOLOGY-AWARE scorer
   (the fix: predicates in the 31-value schema are valid, not "leaks" —
   the old scorer used a 12-predicate list and wrongly failed the model).
3. On pass, flips the three corpora to engine `ghost_b_llm` → the ~90k
   enrichment tail finishes on vLLM at ~18 chunks/s instead of grinding
   on one sidecar. On fail, stays on the qualified GLiNER baseline.

Handoff complete. Ping "vLLM up" (or leave /up working) and Polymath takes it.
