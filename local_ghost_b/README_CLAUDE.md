# Polymath Broke-Mode Local Ghost B — v1 (CLAUDE bundle)

Local, no-API replacement for Ghost B **relation extraction**. Three ModernBERT
classifier heads + a deterministic Python compiler turn entity pairs into typed
Ghost B relations. Trained on the RTX PRO 6000 from 736K real Ghost B
extractions; this bundle runs inference on the Mac.

It does **not** do entity recognition — GLiNER/Python supplies entities upstream.
It writes the existing Ghost B JSONL relation shape, so no backend schema change.

---

## What's in this bundle

```
polymath_local_extractor.py   cascade + compiler + Ghost B JSONL adapter (core)
ghost_b_cascade_infer.py      chunk -> gated candidate pairs -> relations JSONL
simulate_ingestion.py         throughput + distribution measurement
run_on_mac.py                 Mac entry point (wires head paths, picks MPS/CPU)
requirements.txt
heads/
  easy_predicate_v1/          7 distinctive predicates + none
  family_v1/                  8-way family router
  backbone_v1/                11 high-volume confusables + none (cue input)
scripts/
  eval_cascade.py             end-to-end precision/coverage vs gold
  threshold_sweep.py          per-class precision@confidence
reports/                      eval + ingestion-sim JSON from the RTX run
```

---

## Architecture (the cascade)

```
GLiNER entities (upstream)
  -> candidate-pair GATE        same sentence + (cue verb between entities
                                 OR high-value type pair); caps per chunk
  -> ModernBERT cascade         backbone / easy / family heads
  -> Python compiler            cue rules + type/direction rules
  -> Ghost B JSONL adapter      {"t":"r","sub","pred","obj","ok","cf","ev","cue"}
```

Tiers (production rule): confident head OR cue-confirmed -> exact predicate;
confident family + cue/type resolves -> exact; else -> related_to; weak -> drop.
`part_of`/`uses` are loose catch-alls — written exact only when high-confidence
or a strong cue agrees, else they become `related_to` (no fake certainty).

---

## Run on Mac

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run_on_mac.py --demo                       # sanity check
python run_on_mac.py --chunks my_chunks.jsonl --out local_relations.jsonl
```

`heads/` are local; `HF_HUB_OFFLINE=1` is set automatically — no network needed.
On Apple Silicon the runner uses the MPS backend if available, else CPU.

Chunk input shape (one JSON per line):
```json
{"chunk_id":"...","doc_id":"...","text":"<full text>",
 "entities":[{"canonical_name":"...","entity_type":"Software",
              "surface_form":"...","query_aliases":["..."]}]}
```

---

## Shipped config (env flags)

```bash
LOCAL_GHOST_B_ENABLED=true
LOCAL_GHOST_B_MODE=modernbert_cascade
LOCAL_GHOST_B_ALLOW_RELATED_TO=true
LOCAL_GHOST_B_MIN_EXACT_CONF=0.80
LOCAL_GHOST_B_RELATED_MIN_CONF=0.80
LOCAL_GHOST_B_PART_OF_USES_REQUIRE_CUE=true
LOCAL_GHOST_B_MAX_RELATED_TO_PER_CHUNK=3
LOCAL_GHOST_B_MAX_PAIRS_PER_CHUNK=24
```

Keep cloud Ghost B primary; use this lane only when `ENABLED`, the API is down,
or budget mode is on.

---

## Measured performance (RTX, held-out, deduped vs all training)

- Predicate cascade vs Ghost B gold: **0.73 exact-edge precision** at 0.25 coverage
- Strong predicates: supports 0.95, created_by 0.94, depends_on 0.93, detects 0.92,
  implements 0.89, uses 0.87, member_of 0.86, located_in 0.78, part_of 0.73
- Ingestion (gated cooccur, 2000 chunks): ~1 exact + ~1.4 related_to per chunk,
  24.5 chunks/s on the RTX (candidate gate cut related_to noise 15x: 21.3 -> 1.4/chunk)
- Abstract/rare predicates (defines, contradicts, maps_to, instance_of) correctly
  route to `related_to` rather than guessing.

Tune the operating point with the thresholds: raise -> ~0.85 precision / lower
coverage; lower -> more coverage / ~0.61 precision.

---

## Stage 2: Qwen ambiguity-resolver (NEW — see README_QWEN_CLAUDE.md)

The cascade abstains (-> related_to) on ambiguous edges. A fine-tuned
Qwen2.5-1.5B resolver recovers ~70-78% of those into exact predicates, only
running on the ambiguous minority. Run the full hybrid:

    python run_on_mac.py --hybrid --chunks my_chunks.jsonl

Convert `qwen_resolver_merged/` to MLX first for speed (instructions in the
Qwen README).

---

## Known limits / next work

1. **Candidate-pair selection is the bottleneck, not the cascade.** v1 uses a
   deterministic gate (same-sentence + cue/type). A trained `relation_exists`
   binary head would tighten this further — not built yet (deliberately).
2. `part_of`/`uses` stay high-precision / low-coverage — Ghost B itself uses them
   loosely; not fixable from this data.
3. MLX note: these are encoder classifiers. For Mac, PyTorch-MPS (this bundle) or
   ONNX->CoreML are the paths — `mlx_lm.convert` does NOT apply to ModernBERT
   sequence classifiers.

Predicate vocabulary = the 30 Ghost B predicates + `related_to` sentinel
(`backend/services/ghost_b_schemas.py`).
