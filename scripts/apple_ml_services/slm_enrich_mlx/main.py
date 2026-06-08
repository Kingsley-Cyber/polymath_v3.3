"""
slm_enrich_mlx/main.py — Pass-2 enrichment sidecar (facets + out-of-text aliases +
qualitative facts). Mirrors embedder_mlx / reranker_mlx.

Two things make the small model produce the EXPECTED output, not schema-imitation:
  1. PROMPT  — each of the 9 FactType values is DEFINED with a micro-example, every
     field has a "this is X, NOT Y" rule, and a full worked example is shown. A 1.2B
     model copies an example far better than it follows a bare list.
  2. GRAMMAR — optional hard JSON-schema constraint at decode so `fact_type` is forced
     into the 9 (GGUF + llama-cpp-python). MLX path runs the same prompt, unconstrained.

Backend via env SLM_ENRICH_BACKEND = "mlx" (default) | "gguf":
  mlx : APPLE_SLM_ENRICH_MODEL_ID  (e.g. Unravler/LFM2-1.2B-Extract-MLX-4bit) — prompt only
  gguf: APPLE_SLM_ENRICH_GGUF_PATH (LFM2-1.2B-Extract-GGUF .gguf) — prompt + grammar

Determinism: greedy (temp=0). Sidecar returns best-effort JSON; the backend adapter is
the validation authority (LLMEntity / LLMFact / FactType, drop on failure, no resample).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

BACKEND = os.environ.get("SLM_ENRICH_BACKEND", "gguf").lower()
MODEL_ID = os.environ.get("APPLE_SLM_ENRICH_MODEL_ID", "Unravler/LFM2-1.2B-Extract-MLX-4bit")
GGUF_PATH = os.environ.get("APPLE_SLM_ENRICH_GGUF_PATH", "")
PORT = int(os.environ.get("SLM_ENRICH_PORT", "8083"))
MAX_TOKENS = int(os.environ.get("SLM_ENRICH_MAX_TOKENS", "400"))  # 160 truncated mid-fact

FACT_TYPES = ["property", "status", "timestamp", "quantity", "threshold",
              "category", "tag", "rule_condition", "rule_action"]

# ----------------------------------------------------------- JSON schemas (grammar)
FACTS_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "fact_type": {"type": "string", "enum": FACT_TYPES},   # <- the hard constraint
            "property_name": {"type": "string"},
            "value": {"type": "string"},
            "unit": {"type": "string"},
            "condition": {"type": "string"},
        },
        "required": ["subject", "fact_type", "property_name", "value", "unit", "condition"],
    }}},
    "required": ["facts"],
}
FA_SCHEMA = {
    "type": "object",
    "properties": {
        "object_kind": {"type": "string"},
        "query_aliases": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["object_kind", "query_aliases"],
}

# --------------------------------------------------------------------- prompts
_FACT_SYS = """You extract structured FACTS about specific entities from text, as JSON.

fact_type MUST be EXACTLY ONE of these 9 strings (never combine two, never invent one):
  "quantity"       a measured amount, usually with a unit (e.g. "3.5 GB", "7B parameters")
  "timestamp"      a date or time (e.g. "2021")
  "threshold"      a limit or bound (e.g. "at least 4 GB")
  "status"         a lifecycle / maturity state (e.g. "deprecated", "production-ready")
  "category"       a class the subject belongs to (e.g. "vector database")
  "tag"            a keyword / label applied to the subject
  "property"       a named attribute and its value (property_name="version", value="1.7")
  "rule_condition" a condition that triggers behavior (e.g. "the index is full")
  "rule_action"    a required or forbidden action (e.g. "reject new inserts")

Each fact has these fields:
  subject       : MUST be copied exactly from the "Entities:" list provided below.
                  Never invent a subject. Never use a name not in that list.
  fact_type     : exactly one of the 9 above.
  property_name : the ATTRIBUTE being described (e.g. "maturity", "release_date", "ram").
                  This is NOT the entity's type and NOT the fact_type — it names the attribute.
  value         : the attribute's value, AS WRITTEN IN THE TEXT BELOW.
  unit          : a unit if present in the text, else "".
  condition     : for rule_action facts, the triggering condition phrase; else "".

OUTPUT FORMAT (these brackets are placeholders — replace each with values that come
from the actual Entities: list and Text below):
{"facts":[
 {"subject":"<one of the listed entities>","fact_type":"<one of the 9 strings>","property_name":"<the attribute>","value":"<as written in text>","unit":"<unit or empty>","condition":"<trigger or empty>"}
]}

If the text below states no facts about the listed entities, output {"facts":[]}.
Output ONE JSON object only. No prose, no markdown, no code fences.

EXTRACT NOW FROM THE TEXT BELOW. Every "subject" you emit MUST be a string from
the "Entities:" list. Do not carry over names from the format spec above."""


def _fact_user(c) -> str:
    ents = ", ".join(f'"{e.canonical_name}"({e.entity_type})' for e in c.entities)
    return f"Entities: {ents}\nText: {c.text[:1400]}\n{{\"facts\":"


_FA_SYS = """You label a named entity with a short facet noun and well-known aliases,
using ONLY the given context.

OUTPUT FORMAT (these brackets are placeholders — replace each with values that
describe THE ENTITY GIVEN BELOW, not anything else from the context):
{"object_kind":"<a specific facet noun for the given entity — e.g. vector_database, web_framework, embedding_model, dataset, algorithm, protocol, language, game_engine; prefer SPECIFIC over generic words like 'library' or 'tool'>",
 "query_aliases":["<up to 5 widely-used synonyms or abbreviations OF THE GIVEN ENTITY itself, not in the exclude list>"]}

Rules:
- object_kind must describe THE ENTITY GIVEN below — not any other named thing
  that happens to appear in the context.
- query_aliases must be alternate names for THE GIVEN ENTITY itself, not related
  concepts or other entities mentioned nearby.
- If unsure of a field, use "" or [].

Output ONE JSON object only. No prose, no markdown, no code fences.

LABEL NOW FOR THE ENTITY GIVEN BELOW. Do not carry over names from the format spec above."""


def _fa_user(e) -> str:
    return (f'Entity: "{e.canonical_name}" (type: {e.entity_type})\n'
            f"Context: {e.context[:800]}\n"
            f"Exclude: {json.dumps(e.in_text_aliases)}\n"
            "{\"object_kind\":")


# ----------------------------------------------------------------------- model
_mlx = None        # (model, tokenizer)
_mlx_sampler = None
_gguf = None       # Llama


def _load():
    global _mlx, _mlx_sampler, _gguf
    if BACKEND == "gguf":
        if _gguf is not None:
            return
        from llama_cpp import Llama
        if not GGUF_PATH:
            raise RuntimeError("SLM_ENRICH_BACKEND=gguf needs APPLE_SLM_ENRICH_GGUF_PATH")
        _gguf = Llama(model_path=GGUF_PATH, n_ctx=4096, n_gpu_layers=-1, verbose=False)
    else:
        if _mlx is not None:
            return
        from mlx_lm import load
        _mlx = load(MODEL_ID)
        try:
            from mlx_lm.sample_utils import make_sampler
            _mlx_sampler = make_sampler(temp=0.0)
        except Exception:
            _mlx_sampler = None


def generate_json(system: str, user: str, schema: dict) -> Any:
    """Greedy-decode one chat turn -> parsed JSON. GGUF path hard-constrains to `schema`;
    MLX path runs prompt-only. Returns {} on parse failure."""
    _load()
    if BACKEND == "gguf":
        from llama_cpp import LlamaGrammar
        grammar = LlamaGrammar.from_json_schema(json.dumps(schema))
        out = _gguf.create_chat_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            grammar=grammar, temperature=0.0, max_tokens=MAX_TOKENS)
        return _extract_json(out["choices"][0]["message"]["content"])
    # mlx
    from mlx_lm import generate as mlx_generate
    model, tok = _mlx
    prompt = tok.apply_chat_template(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        add_generation_prompt=True, tokenize=False)
    kw = dict(max_tokens=MAX_TOKENS, verbose=False)
    if _mlx_sampler is not None:
        kw["sampler"] = _mlx_sampler
    else:
        kw["temp"] = 0.0
    return _extract_json(mlx_generate(model, tok, prompt=prompt, **kw))


def _extract_json(text: str) -> Any:
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    # the user prompt primes with `{"facts":` / `{"object_kind":` — re-add if the model
    # continued from there without repeating the opening brace
    for prefix in ('{"facts":', '{"object_kind":'):
        if text.startswith(prefix[1:]) and not text.startswith("{"):
            text = "{" + text
    try:
        return json.loads(text)
    except Exception:
        pass
    for oc, cc in (("{", "}"), ("[", "]")):
        i = text.find(oc)
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(text)):
            depth += (text[j] == oc) - (text[j] == cc)
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


# ------------------------------------------------------------------------- app
app = FastAPI(title="slm_enrich_mlx", version="2")


@app.get("/info")
def info():
    return {"backend": BACKEND, "model": GGUF_PATH if BACKEND == "gguf" else MODEL_ID,
            "constrained": BACKEND == "gguf", "sampler": "greedy(temp=0)",
            "max_tokens": MAX_TOKENS, "fact_types": FACT_TYPES}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/enrich/facets_aliases")
def facets_aliases(req: FacetsAliasesReq):
    out = []
    for e in req.entities:
        obj = generate_json(_FA_SYS, _fa_user(e), FA_SCHEMA)
        obj = obj if isinstance(obj, dict) else {}
        aliases = obj.get("query_aliases") or []
        aliases = aliases if isinstance(aliases, list) else []
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
        obj = generate_json(_FACT_SYS, _fact_user(c), FACTS_SCHEMA)
        fl = obj.get("facts") if isinstance(obj, dict) else None
        fl = fl if isinstance(fl, list) else []
        names = {e.canonical_name.lower() for e in c.entities}
        clean = []
        for f in fl:
            if not isinstance(f, dict) or f.get("fact_type") not in FACT_TYPES:
                continue
            if str(f.get("subject", "")).lower() not in names:
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
