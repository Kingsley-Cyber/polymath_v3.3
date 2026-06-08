# slm_enrich_mlx — Pass-2 enrichment sidecar (v2)

Facets + out-of-text aliases + qualitative facts. Drop into
`scripts/apple_ml_services/slm_enrich_mlx/`.

## What changed in v2 (fixes the off-vocab `fact_type` calibration wall)
The model was reading chunks correctly but emitting `fact_type:"property status"` and
putting the entity *type* in `property_name`. Two fixes, layered:
1. **Prompt** — each of the 9 `fact_type` values is now **defined** with a micro-example,
   every field has an explicit "this is X, NOT Y" rule, and a **full worked example**
   (chunk → correct JSON) is shown so the 1.2B model copies a pattern instead of
   inventing the schema.
2. **Grammar** — optional **hard JSON-schema constraint** (`fact_type` ∈ the 9, enforced
   at decode) via a GGUF backend.
Also: `max_tokens` default 160 → **400** (160 truncated mid-fact).

## Backends (env `SLM_ENRICH_BACKEND`)
| | `mlx` (default) | `gguf` |
|---|---|---|
| model | `APPLE_SLM_ENRICH_MODEL_ID` (e.g. `Unravler/LFM2-1.2B-Extract-MLX-4bit`) | `APPLE_SLM_ENRICH_GGUF_PATH` (`LFM2-1.2B-Extract-GGUF` .gguf) |
| `fact_type` enforcement | **prompt only** (improved) | **hard grammar — mathematically forced into the 9** |
| dep | `mlx-lm` | `llama-cpp-python` (Metal) |

## TEST SEQUENCE (do this in order — isolates prompt vs grammar)
```bash
pip install -r requirements.txt          # mlx path needs no llama-cpp

# 1) MLX + new prompt ONLY (no new dep) — does the better prompt alone fix vocab adherence?
SLM_ENRICH_BACKEND=mlx APPLE_SLM_ENRICH_MODEL_ID=Unravler/LFM2-1.2B-Extract-MLX-4bit \
  SLM_ENRICH_PORT=8083 python main.py
#    re-run the Qdrant /enrich/facts test. Count how many facts survive the fact_type∈9 gate.
#    if most survive with sensible types -> ship mlx, done.

# 2) ONLY if step 1 still emits off-vocab fact_types -> GGUF hard constraint:
CMAKE_ARGS="-DGGML_METAL=on" pip install llama-cpp-python
# download LiquidAI/LFM2-1.2B-Extract-GGUF  (a *.gguf file)
SLM_ENRICH_BACKEND=gguf APPLE_SLM_ENRICH_GGUF_PATH=/path/to/LFM2-1.2B-Extract.gguf \
  SLM_ENRICH_PORT=8083 python main.py
#    grammar forces fact_type into the 9 -> 0% off-vocab by construction.
```

## Endpoints (unchanged contracts — adapter already matches)
- `GET /info` (now reports `backend` + `constrained`) · `GET /health`
- `POST /enrich/facets_aliases` → `{results:[{canonical_name, object_kind, query_aliases}]}`
- `POST /enrich/facts` → `{results:[{chunk_id, facts:[{subject,fact_type,property_name,value,unit,condition}]}]}`

## Contract notes
- **Determinism:** greedy (temp=0). GGUF grammar makes the output *structurally* valid.
- **Validation authority is the adapter** — this service returns best-effort JSON; the
  adapter validates against `LLMEntity`/`LLMFact`/`FactType` and **drops on failure, never
  resamples**. The light filters here are convenience.
- **Honest caveat:** grammar guarantees `fact_type` ∈ 9 and valid JSON; it does NOT
  guarantee the model picks the *right* type or perfect `value`/`property_name`. The new
  prompt is what improves the *choice*; the grammar guarantees *validity*. Eyeball
  precision on ~10 facts before flipping `LOCAL_SLM_ENRICH_ENABLED=true`.
