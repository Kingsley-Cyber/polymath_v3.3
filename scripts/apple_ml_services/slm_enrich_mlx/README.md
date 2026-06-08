# slm_enrich_mlx — Pass-2 enrichment sidecar

Host-native FastAPI + MLX service. Facets + out-of-text aliases + qualitative facts.
Mirrors `embedder_mlx` / `reranker_mlx`. **Drop into `scripts/apple_ml_services/slm_enrich_mlx/`.**

## Run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
APPLE_SLM_ENRICH_MODEL_ID=LiquidAI/LFM2-1.2B-Extract SLM_ENRICH_PORT=8083 python main.py
# if the HF model isn't an MLX build: python -m mlx_lm.convert --hf-path LiquidAI/LFM2-1.2B-Extract -q
```

## Endpoints
- `GET /info` · `GET /health`
- `POST /enrich/facets_aliases` — `{entities:[{canonical_name, entity_type, context, in_text_aliases}]}` → `{results:[{canonical_name, object_kind, query_aliases}]}`
- `POST /enrich/facts` — `{chunks:[{chunk_id, text, entities:[{canonical_name, entity_type}]}]}` → `{results:[{chunk_id, facts:[{subject, fact_type, property_name, value, unit, condition}]}]}`

## Contract notes for the backend adapter
- **Determinism:** greedy decode (temp=0). Reproducible on this Mac/weights/quant.
- **Validation is the adapter's job** — this service returns best-effort JSON; the adapter
  validates each row against `LLMEntity` / `LLMFact` / `FactType` and **drops on failure,
  never resamples**. The light filters here (alias-exclude, fact_type∈9, subject∈listed)
  are convenience, not the authority.
- **Grain:** call `/enrich/facets_aliases` once per *unique entity*; `/enrich/facts` once
  per *cue-flagged chunk*. Batch many per request.
- **Untested on Mac from my side** (drafted on the RTX, no MLX here). The one spot to
  check on first run is the `mlx_lm.generate` call in `generate_json()` — the greedy
  sampler arg moved across mlx-lm versions; both paths are handled, but pin `mlx-lm` and
  confirm `/info` then a 1-entity `/enrich/facets_aliases` returns valid JSON.
