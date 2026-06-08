"""
slm_enrich_mlx/main.py
Pass-2 enrichment sidecar — facets + out-of-text aliases + qualitative facts.
Mirrors the embedder_mlx / reranker_mlx FastAPI+MLX host-native pattern.

Model-agnostic: everything goes through one generate_json() call, so LFM2-1.2B-Extract
or any other MLX chat model swaps via APPLE_SLM_ENRICH_MODEL_ID. Default target is
LFM2-1.2B-Extract (tuned for text->JSON, defaults to JSON, greedy-recommended).

Determinism: greedy decode (temp=0). The sidecar does NOT validate against backend
Pydantic — it returns best-effort JSON and the backend adapter validates against
LLMEntity / LLMFact / FactType and drops on failure (no resample). That keeps this
service lean and zero-coupled to backend code.

Endpoints:
  GET  /info                  -> {model_id, device, sampler, max_tokens}
  GET  /health                -> {status}
  POST /enrich/facets_aliases -> per-entity {object_kind, query_aliases<=5}
  POST /enrich/facts          -> per-chunk  {facts:[LLMFact-shaped]}

Run:  APPLE_SLM_ENRICH_MODEL_ID=mlx-community/LFM2-1.2B-Extract-... \
      SLM_ENRICH_PORT=8083 python main.py
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

MODEL_ID = os.environ.get("APPLE_SLM_ENRICH_MODEL_ID", "LiquidAI/LFM2-1.2B-Extract")
PORT = int(os.environ.get("SLM_ENRICH_PORT", "8083"))
MAX_TOKENS = int(os.environ.get("SLM_ENRICH_MAX_TOKENS", "160"))  # bounded output = bounded latency

# 9-value FactType vocabulary (mirror backend ghost_b_schemas.FactType). The model is
# steered toward these; the adapter is the authority that drops off-vocab.
FACT_TYPES = ["property", "status", "timestamp", "quantity", "threshold",
              "category", "tag", "rule_condition", "rule_action"]

# ----------------------------------------------------------------------- model
_model = None
_tokenizer = None
_sampler = None


def _load():
    """Lazy-load the MLX model + a greedy (temp=0) sampler. Isolated so the import
    error message is clear if mlx-lm isn't installed on the host."""
    global _model, _tokenizer, _sampler
    if _model is not None:
        return
    from mlx_lm import load
    _model, _tokenizer = load(MODEL_ID)
    # greedy sampler — API differs slightly across mlx-lm versions; both paths covered.
    try:
        from mlx_lm.sample_utils import make_sampler
        _sampler = make_sampler(temp=0.0)
    except Exception:
        _sampler = None


def generate_json(system: str, user: str) -> Any:
    """Single model entry point: greedy-decode a chat turn, return parsed JSON (dict/
    list) or {} on parse failure. Model-agnostic — swap the model id, nothing else."""
    _load()
    from mlx_lm import generate as mlx_generate
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    prompt = _tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    kw = dict(max_tokens=MAX_TOKENS, verbose=False)
    if _sampler is not None:
        kw["sampler"] = _sampler          # greedy
    else:
        kw["temp"] = 0.0                  # older mlx-lm
    text = mlx_generate(_model, _tokenizer, prompt=prompt, **kw)
    return _extract_json(text)


def _extract_json(text: str) -> Any:
    """Robustly pull the first JSON object/array out of the model output (handles
    code fences / stray prose). Returns {} if nothing parses."""
    if not text:
        return {}
    text = text.strip()
    # strip ```json fences if present
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    # fast path
    try:
        return json.loads(text)
    except Exception:
        pass
    # find first balanced {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i = text.find(open_c)
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(text)):
            if text[j] == open_c:
                depth += 1
            elif text[j] == close_c:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:j + 1])
                    except Exception:
                        break
    return {}


# ------------------------------------------------------------------- contracts
class EntIn(BaseModel):
    canonical_name: str
    entity_type: str = "Concept"
    context: str = ""
    in_text_aliases: list[str] = []


class FacetsAliasesReq(BaseModel):
    entities: list[EntIn]


class ChunkEnt(BaseModel):
    canonical_name: str
    entity_type: str = "Concept"


class ChunkIn(BaseModel):
    chunk_id: str
    text: str
    entities: list[ChunkEnt] = []


class FactsReq(BaseModel):
    chunks: list[ChunkIn]


# --------------------------------------------------------------------- prompts
_FA_SYS = (
    "You label a named entity with a short facet and well-known aliases, using ONLY the "
    "given context. Output ONE JSON object and nothing else:\n"
    '{"object_kind": "<short facet: library|framework|database|model|dataset|method|'
    'protocol|api|language|platform|concept|...>", "query_aliases": ["<=5 widely-used '
    'synonyms or abbreviations NOT already in the exclude list>"]}\n'
    "If you are unsure of a field, use \"\" or []. No prose, no markdown."
)


def _fa_user(e: EntIn) -> str:
    return (f'Entity: "{e.canonical_name}" (type: {e.entity_type})\n'
            f"Context: {e.context[:800]}\n"
            f"Exclude (already known): {json.dumps(e.in_text_aliases)}\n"
            "JSON:")


_FACT_SYS = (
    "You extract qualitative facts about the listed entities, grounded ONLY in the text. "
    "Output ONE JSON object and nothing else:\n"
    '{"facts": [{"subject": "<one of the listed entities>", "fact_type": "<one of: '
    + " ".join(FACT_TYPES) + '>", "property_name": "<short>", "value": "<short>", '
    '"unit": "", "condition": ""}]}\n'
    "Only facts clearly stated in the text. Subjects must be from the listed entities. "
    "If there are none, return {\"facts\": []}. No prose, no markdown."
)


def _fact_user(c: ChunkIn) -> str:
    ents = ", ".join(f'"{e.canonical_name}"({e.entity_type})' for e in c.entities)
    return f"Entities: {ents}\nText: {c.text[:1200]}\nJSON:"


# ------------------------------------------------------------------------- app
app = FastAPI(title="slm_enrich_mlx", version="1")


@app.get("/info")
def info():
    return {"model_id": MODEL_ID, "device": "mps", "sampler": "greedy(temp=0)",
            "max_tokens": MAX_TOKENS, "fact_types": FACT_TYPES}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/enrich/facets_aliases")
def facets_aliases(req: FacetsAliasesReq):
    out = []
    for e in req.entities:
        obj = generate_json(_FA_SYS, _fa_user(e))
        if not isinstance(obj, dict):
            obj = {}
        aliases = obj.get("query_aliases") or []
        if not isinstance(aliases, list):
            aliases = []
        # belt-and-suspenders: drop any alias already in the exclude list + cap 5
        excl = {a.lower() for a in e.in_text_aliases}
        aliases = [a for a in aliases if isinstance(a, str) and a.lower() not in excl][:5]
        out.append({"canonical_name": e.canonical_name,
                    "object_kind": str(obj.get("object_kind") or ""),
                    "query_aliases": aliases})
    return {"results": out}


@app.post("/enrich/facts")
def facts(req: FactsReq):
    out = []
    for c in req.chunks:
        obj = generate_json(_FACT_SYS, _fact_user(c))
        facts_list = obj.get("facts") if isinstance(obj, dict) else None
        if not isinstance(facts_list, list):
            facts_list = []
        clean = []
        names = {e.canonical_name.lower() for e in c.entities}
        for f in facts_list:
            if not isinstance(f, dict):
                continue
            if f.get("fact_type") not in FACT_TYPES:           # soft pre-filter; adapter is authority
                continue
            if str(f.get("subject", "")).lower() not in names:  # must be a listed entity
                continue
            clean.append({"subject": f.get("subject", ""), "fact_type": f["fact_type"],
                          "property_name": str(f.get("property_name") or ""),
                          "value": str(f.get("value") or ""),
                          "unit": str(f.get("unit") or ""),
                          "condition": str(f.get("condition") or "")})
        out.append({"chunk_id": c.chunk_id, "facts": clean})
    return {"results": out}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
