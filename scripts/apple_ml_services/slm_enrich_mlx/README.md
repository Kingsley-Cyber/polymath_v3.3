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
| | **`gguf` (default)** | `mlx` (fast escape hatch) |
|---|---|---|
| model | `APPLE_SLM_ENRICH_GGUF_PATH` (`Qwen/Qwen2.5-1.5B-Instruct-GGUF`, Q4_K_M ~1.1 GB) | `APPLE_SLM_ENRICH_MODEL_ID` (e.g. `mlx-community/Qwen2.5-1.5B-Instruct-4bit`) |
| `fact_type` enforcement | **hard grammar — mathematically forced into the 9** | prompt only |
| dep | `llama-cpp-python` (Metal auto-detected on Apple Silicon) | `mlx-lm` |
| warm latency (M1 Max, Q4 1.5B) | ~2.4 s for 3-fact output; ~0.55 s for 1 facet | ~0.7 s facts; ~0.55 s facets |
| facts passing Pydantic | 3/3 on the Qdrant test (real win) | 0/3 (all off-vocab `fact_type` get dropped) |

Cold start (first call) is ~14 s for GGUF on Apple Silicon — Metal library init alone takes ~10 s. Subsequent calls are warm; keep the sidecar long-lived. MLX cold start is ~5 s.

## Setup (default: GGUF + grammar)
```bash
pip install -r requirements.txt   # installs mlx-lm AND llama-cpp-python (Metal)

# Download Qwen 2.5 1.5B Instruct GGUF (Q4_K_M ~1.1 GB):
python -c "from huggingface_hub import hf_hub_download; \
    print(hf_hub_download(repo_id='Qwen/Qwen2.5-1.5B-Instruct-GGUF', \
                          filename='qwen2.5-1.5b-instruct-q4_k_m.gguf'))"
# copy the printed path to APPLE_SLM_ENRICH_GGUF_PATH

APPLE_SLM_ENRICH_GGUF_PATH=<that path> SLM_ENRICH_PORT=8083 python main.py
```

## MLX escape hatch (faster but produces no validated facts)
The MLX path runs the same prompts unconstrained — useful for prompt iteration
or when latency dominates over correctness:
```bash
SLM_ENRICH_BACKEND=mlx \
  APPLE_SLM_ENRICH_MODEL_ID=mlx-community/Qwen2.5-1.5B-Instruct-4bit \
  SLM_ENRICH_PORT=8083 python main.py
# expect /enrich/facets_aliases to produce reasonable labels but
# /enrich/facts to emit off-vocab fact_types that the adapter drops.
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
