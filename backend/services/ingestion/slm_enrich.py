"""Pass-1 deterministic + Pass-2 SLM-residual enrichment for the local lane.

Two stages, both env-gated default-off:

  Pass-1  pass1_enrich(results) — synchronous, Python-only, bit-for-bit
          reproducible. Applies services/ingestion/enrich.py per chunk:
          numeric facts (quantity/timestamp/threshold/property) attached
          to the nearest in-sentence entity + in-text aliases via
          Schwartz-Hearst. Mutates ExtractionResult in place.

  Pass-2  pass2_enrich(results) — async HTTP to slm_enrich_mlx sidecar
          on Apple Silicon. Two batches: facets+out-of-text aliases per
          UNIQUE entity (Gate A: still missing object_kind/aliases);
          qualitative facts per CUE-FLAGGED chunk (Gate B: residual cues
          remain). Pydantic-validates each response row against
          LLMEntity / LLMFact / FactType and drops on failure — never
          resamples. Greedy decode on the sidecar makes the system
          deterministic on this Mac.

Both gates source from enrich.CUES + enrich.should_enrich_facts so
Pass-1's "Python couldn't structure this" and Pass-2's "SLM should look
at it" share one regex taxonomy.

Env flags:
    LOCAL_PASS1_ENRICH_ENABLED   default false   sync, no network
    LOCAL_SLM_ENRICH_ENABLED     default false   async, sidecar required
    LOCAL_SLM_ENRICH_URL         default http://localhost:8083
    LOCAL_SLM_ENRICH_TIMEOUT_S   default 30
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from pydantic import ValidationError

# Avoid a circular import: ghost_b imports services.ingestion modules itself
# in some paths, so defer the dataclass imports to call time. The Pydantic
# schemas are safe to import at module load (pure model layer).
from services.ghost_b_schemas import LLMEntity, LLMFact

from .enrich import extract as pass1_extract, should_enrich_facts

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


PASS1_ENABLED = _env_bool("LOCAL_PASS1_ENRICH_ENABLED", False)
PASS2_ENABLED = _env_bool("LOCAL_SLM_ENRICH_ENABLED", False)
SIDECAR_URL = os.environ.get("LOCAL_SLM_ENRICH_URL", "http://localhost:8083").rstrip("/")
TIMEOUT_S = float(os.environ.get("LOCAL_SLM_ENRICH_TIMEOUT_S", "30"))

# Confidence sentinels: deterministic Python output is treated as effectively
# certain; the SLM-emitted facts are treated as moderate-confidence because
# the model's only validator is the adapter's Pydantic gate.
PASS1_CONFIDENCE = 1.0
PASS2_CONFIDENCE = 0.70


# ----------------------------------------------------------------- Pass-1

def pass1_enrich(results: list[Any]) -> list[Any]:
    """Synchronous deterministic enrichment. Adds numeric facts and in-text
    aliases to each ExtractionResult. Safe to call unconditionally — no
    network, no model, no nondeterminism."""
    if not results:
        return results
    # Local import to avoid module-level circular dependency with services.ghost_b.
    from services.ghost_b import EntityItem, FactItem  # noqa: F401

    for r in results:
        text = getattr(r, "text", "") or ""
        ents = getattr(r, "entities", None) or []
        if not text or not ents:
            continue

        ent_dicts = [
            {"canonical_name": e.canonical_name,
             "surface_form": e.surface_form or e.canonical_name,
             "entity_type": e.entity_type}
            for e in ents
        ]
        try:
            out = pass1_extract(text, ent_dicts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pass1 enrich failed chunk=%s: %s", getattr(r, "chunk_id", "?"), exc)
            continue

        _merge_in_text_aliases(r, out.get("aliases") or {})
        _append_facts(r, out.get("facts") or [], confidence=PASS1_CONFIDENCE, FactItem=FactItem)

    return results


def _merge_in_text_aliases(result: Any, alias_map: dict[str, list[str]]) -> None:
    """Append novel aliases to each EntityItem.query_aliases (cap 5, dedup)."""
    for e in result.entities:
        new = alias_map.get(e.canonical_name) or []
        if not new:
            continue
        existing = {a.lower() for a in (e.query_aliases or []) if a}
        if e.surface_form:
            existing.add(e.surface_form.lower())
        existing.add((e.canonical_name or "").lower())
        merged = list(e.query_aliases or [])
        for a in new:
            if isinstance(a, str) and a.lower() not in existing:
                merged.append(a)
                existing.add(a.lower())
        e.query_aliases = merged[:5]


def _append_facts(result: Any, fact_dicts: list[dict], *, confidence: float, FactItem) -> None:
    """Pydantic-validate each fact dict; on success append a FactItem."""
    for fd in fact_dicts:
        try:
            v = LLMFact(
                subject=str(fd.get("subject") or ""),
                fact_type=fd.get("fact_type") or "",
                property_name=str(fd.get("property_name") or "")[:80],
                value=str(fd.get("value") or "")[:500],
                unit=str(fd.get("unit") or "")[:40],
                condition=str(fd.get("condition") or "")[:300],
                confidence=confidence,
                evidence_phrase=str(fd.get("evidence_phrase") or "")[:500],
            )
        except (ValidationError, ValueError, TypeError):
            continue  # drop on failure, never resample
        result.facts.append(FactItem(
            subject=v.subject,
            fact_type=v.fact_type,
            property_name=v.property_name,
            value=v.value,
            unit=v.unit or None,
            condition=v.condition or None,
            confidence=v.confidence,
            evidence_phrase=v.evidence_phrase,
        ))


# ----------------------------------------------------------------- Pass-2

def _dedup_unique_entities(results: list[Any]) -> dict[str, dict]:
    """canonical_name (lowercased) -> {entity_obj, context, in_text_aliases}.

    Context is the first occurrence by chunk_id ascending — a deterministic
    pick, so the same (entities, results) input produces the same sidecar
    request body byte-for-byte.
    """
    sorted_results = sorted(results, key=lambda r: r.chunk_id)
    seen: dict[str, dict] = {}
    for r in sorted_results:
        for e in r.entities:
            key = (e.canonical_name or "").strip().lower()
            if not key or key in seen:
                continue
            in_text: list[str] = []
            if e.surface_form:
                in_text.append(e.surface_form)
            in_text.extend(a for a in (e.query_aliases or []) if a)
            seen[key] = {
                "entity": e,
                "context": (r.text or "")[:1000],
                "in_text_aliases": in_text,
            }
    return seen


async def _post(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    try:
        resp = await client.post(f"{SIDECAR_URL}{path}", json=body)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("slm_enrich sidecar %s failed: %s", path, exc)
        return {"results": []}


async def pass2_enrich(results: list[Any]) -> list[Any]:
    """SLM residual enrichment: Gate A facets/aliases + Gate B qualitative
    facts via the slm_enrich_mlx sidecar. No-op if sidecar is unreachable —
    the partial result is preserved, no exception escapes."""
    if not results:
        return results
    from services.ghost_b import FactItem  # local: see pass1_enrich comment

    # --- Gate A: entities still missing object_kind or query_aliases -------
    dedup = _dedup_unique_entities(results)
    fa_queue: list[dict] = []
    for key, info in sorted(dedup.items()):  # stable order
        e = info["entity"]
        if e.object_kind and e.query_aliases:
            continue
        fa_queue.append({
            "canonical_name": e.canonical_name,
            "entity_type": e.entity_type,
            "context": info["context"],
            "in_text_aliases": info["in_text_aliases"],
        })

    # --- Gate B: chunks where qualitative cues remain unhandled ------------
    facts_queue: list[dict] = []
    for r in sorted(results, key=lambda x: x.chunk_id):
        existing_facts = [{"fact_type": f.fact_type} for f in r.facts]
        if should_enrich_facts(r.text or "", existing_facts):
            facts_queue.append({
                "chunk_id": r.chunk_id,
                "text": (r.text or "")[:2400],
                "entities": [
                    {"canonical_name": e.canonical_name, "entity_type": e.entity_type}
                    for e in r.entities
                ],
            })

    if not fa_queue and not facts_queue:
        return results

    # --- Sidecar HTTP ------------------------------------------------------
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        fa_data = (await _post(client, "/enrich/facets_aliases", {"entities": fa_queue})
                   if fa_queue else {"results": []})
        facts_data = (await _post(client, "/enrich/facts", {"chunks": facts_queue})
                      if facts_queue else {"results": []})

    # --- Merge facets/aliases (validated, additive only) ------------------
    fa_by_key = {
        (row.get("canonical_name") or "").strip().lower(): row
        for row in (fa_data.get("results") or [])
    }
    for key, info in dedup.items():
        row = fa_by_key.get(key)
        if not row:
            continue
        e = info["entity"]
        try:
            cand = LLMEntity(
                canonical_name=e.canonical_name,
                surface_form=e.surface_form or "",
                entity_type=e.entity_type,
                confidence=e.confidence,
                query_aliases=[a for a in (row.get("query_aliases") or []) if isinstance(a, str)][:5],
                object_kind=str(row.get("object_kind") or "")[:100],
            )
        except (ValidationError, ValueError, TypeError):
            continue  # drop, no resample

        # Additive: never overwrite existing values
        if not e.object_kind and cand.object_kind:
            e.object_kind = cand.object_kind
        if not e.query_aliases and cand.query_aliases:
            in_text_lc = {a.lower() for a in info["in_text_aliases"] if a}
            in_text_lc.add((e.canonical_name or "").lower())
            new = [a for a in cand.query_aliases if a.lower() not in in_text_lc]
            e.query_aliases = new[:5]

    # --- Merge facts (validated) ------------------------------------------
    facts_by_chunk = {
        row["chunk_id"]: row
        for row in (facts_data.get("results") or [])
        if "chunk_id" in row
    }
    for r in results:
        row = facts_by_chunk.get(r.chunk_id)
        if not row:
            continue
        _append_facts(r, row.get("facts") or [],
                      confidence=PASS2_CONFIDENCE, FactItem=FactItem)

    return results


# ------------------------------------------------------------ entry point

async def run_enrichment(results: list[Any]) -> list[Any]:
    """Sequential Pass-1 (sync) + Pass-2 (async). Each independently env-gated.

    Call this once after Ghost B Pass-1 extraction finishes for a doc,
    before graph_backfill writes to Neo4j. Both passes mutate the
    ExtractionResult list in place and also return it for convenience.

    Safe when either pass is disabled — that pass is simply skipped.
    Safe when the sidecar is unreachable — Pass-2 logs a warning and
    returns the input unchanged.
    """
    if not results:
        return results
    if PASS1_ENABLED:
        results = pass1_enrich(results)
    if PASS2_ENABLED:
        results = await pass2_enrich(results)
    return results
