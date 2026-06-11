"""ghost_b_local.py — fully-local, deterministic Ghost B extraction.

Drop-in replacement for `services.ghost_b.extract_entities` in the ingestion
worker's Ghost B branch. NO cloud LLM, NO SLM. The output is the same
`ExtractionResult` dataclass shape the cloud extractor emits, so everything
downstream (Mongo staging, Neo4j MERGE, Qdrant) is unaffected.

The stack, per chunk:
  1. GLiNER pass-1  -> entities (14 Ghost B types)          [facet_tagger.get_gliner]
  2. enrich.py      -> numeric facts + in-text aliases       [deterministic Python]
  3. enrich.py      -> qualitative facts (status/category/   [deterministic Python]
                       tag/rule_condition/rule_action)
  4. GLiREL         -> relations (30 Ghost B predicates,      [glirel_infer]
                       sentence-windowed, type-gated)
Then once per doc:
  5. GLiNER pass-2  -> object_kind facet per UNIQUE entity    [facet_tagger.tag_facets]

Confidence sentinels (locked): GLiNER softmax for entities, GLiREL score for
relations, 1.0 for the four deterministic fact types, 0.9 for the five
qualitative ones.

RUNTIME TOPOLOGY (two modes, auto-detected):

  in-process  When the ML stack (torch/gliner/glirel) is importable — i.e. the
              worker runs natively on macOS — extraction runs in this process
              on MPS, in a worker thread under an inference lock.

  http        When the ML stack is NOT importable — i.e. the worker runs in the
              Linux Docker backend, which has no Metal and no torch — the tasks
              are POSTed to the native ghost_b_extract sidecar
              (scripts/apple_ml_services/ghost_b_extract_svc, default
              http://host.docker.internal:8084), which runs the same
              `_extract_raw` pipeline and returns the validated wire dicts.

  Override with LOCAL_GHOST_B_EXTRACT_MODE=auto|inproc|http.
  Sidecar URL: LOCAL_GHOST_B_EXTRACT_URL  (default http://host.docker.internal:8084)
  Timeout:     LOCAL_GHOST_B_EXTRACT_TIMEOUT_S (default 600 — a whole doc's
               chunks travel in one request; 230 chunks ≈ 80 s warm).

The wire format is the pipeline's native output: `ExtractionResult`-shaped
plain dicts whose entities/relations/facts have already passed LLMEntity /
LLMRelation / LLMFact validation (drops are counted per chunk). Dataclass
construction (`services.ghost_b` — backend-only imports) happens ONLY in
`_to_results`, on the worker side, after either mode returns. That split is
what lets the sidecar import this module from the `local_ghost_b` venv without
dragging in the backend dependency chain.

Determinism: every stage is a forward pass + threshold or pure-Python regex —
no sampling — so the same tasks reproduce the same output on a given machine
(JSON float roundtrip is repr-exact, so http mode changes nothing).

If the sidecar is unreachable in http mode, extraction RAISES — local
extraction is the pipeline, not a best-effort enrichment; failing loudly is
correct.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

# These are light (pydantic / re only) and import nothing from services.ghost_b,
# so they are safe at module load — in the backend container AND in the sidecar
# venv. The heavy / circular-prone imports (services.ghost_b dataclasses,
# glirel_infer, gliner, torch, pipeline_config) are deferred into functions.
from services.ghost_b_schemas import LLMEntity, LLMFact, LLMRelation
from services.ingestion.enrich import (
    extract_aliases,
    extract_definitional_phrases,
    extract_facts,
    extract_qualitative_facts,
    extract_table_facts,
    table_entity_text,
)
from services.ingestion.facet_tagger import get_gliner, tag_facets

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "polymath.extract.v1"

# Confidence sentinels (locked decision).
_FACT_CONF_DETERMINISTIC = 1.0   # quantity / timestamp / threshold / property
_FACT_CONF_QUALITATIVE = 0.9     # status / category / tag / rule_condition / rule_action

# Serialize all model inference: one REQUEST holds the models at a time. The
# dual-lane GLiREL below runs two threads INSIDE the locked region (one MPS,
# one CPU) — that's intra-request parallelism, not concurrent requests.
_INFER_LOCK = threading.Lock()
_GLIREL: Any = None        # GliRELClassifier singleton (GPU/MPS)
_GLIREL_CPU: Any = None    # second instance on CPU — the otherwise-idle cores
_ML_AVAILABLE: bool | None = None  # cached import probe
LAST_TIMINGS: dict = {}    # stage split of the most recent _extract_raw call

# Dual-lane GLiREL (default OFF — measured null on Apple Silicon). Theory: the
# idle 10-core CPU runs DeBERTa at ~half MPS speed, so a parallel CPU lane
# should add ~1.5x. Measured: 420 -> 428 ms/chunk — NO gain. Together with
# fp16 measuring 0.99x and MPS being only 2.1x CPU, the consistent explanation
# is that unified-memory BANDWIDTH (~400 GB/s shared by CPU+GPU) is the
# binding constraint for DeBERTa-large inference on this machine; parallel
# engines split the same bandwidth. Kept env-gated for hardware where the
# lanes have separate memory.
GLIREL_CPU_LANE = os.environ.get("GHOST_B_GLIREL_CPU_LANE", "0").strip().lower() in (
    "1", "true", "yes", "on")

# Comma-separated list fans work across MULTIPLE sidecar instances — on a
# 96 GB CUDA box, N processes (~3.7 GB each) parallelize the GIL-bound Python
# preprocessing that a single process serializes (the GLiNER stage stayed
# CPU-bound after GPU batching), each with its own CUDA stream.
SIDECAR_URLS = [
    u.strip().rstrip("/")
    for u in os.environ.get(
        "LOCAL_GHOST_B_EXTRACT_URL", "http://host.docker.internal:8084"
    ).split(",")
    if u.strip()
]
SIDECAR_URL = SIDECAR_URLS[0]  # back-compat for error messages
# Per-REQUEST ceiling. Requests are sliced to SIDECAR_SLICE chunks, so this
# bounds one slice, not a whole book — the pilot's 932 KB doc (~1800 chunks in
# one request) blew the old 600 s whole-doc ceiling at per-chunk speeds.
SIDECAR_TIMEOUT_S = float(os.environ.get("LOCAL_GHOST_B_EXTRACT_TIMEOUT_S", "1800"))
# 2048-chunk slices keep most BOOKS single-slice when ONE sidecar serves, so
# the per-doc facet/definitional dedup sees the whole document. With multiple
# sidecars the doc is split to keep every instance busy (the per-slice facet
# re-prediction overlap is the accepted price of N-way parallelism).
SIDECAR_SLICE = max(1, int(os.environ.get("LOCAL_GHOST_B_EXTRACT_SLICE", "2048")))
EXTRACT_MODE = os.environ.get("LOCAL_GHOST_B_EXTRACT_MODE", "auto").strip().lower()


# --------------------------------------------------------------- path / models
def _repo_root() -> Path:
    # backend/services/ghost_b_local.py -> parents[2] == repo root
    return Path(__file__).resolve().parents[2]


def _ensure_local_ghost_b_on_path() -> Any:
    """Put local_ghost_b (and its tools/) on sys.path and return pipeline_config.
    LOCAL_GHOST_B_DIR overrides the default repo-relative location."""
    lgb = Path(os.environ.get("LOCAL_GHOST_B_DIR") or (_repo_root() / "local_ghost_b"))
    for p in (lgb, lgb / "tools"):
        sp = str(p)
        if p.exists() and sp not in sys.path:
            sys.path.insert(0, sp)
    import pipeline_config  # noqa: E402  (resolved via the inserted path)
    return pipeline_config


def _ml_stack_available() -> bool:
    """True when torch+gliner+glirel are importable in THIS process (native Mac
    venv). False in the Linux backend container, which routes to the sidecar."""
    global _ML_AVAILABLE
    if _ML_AVAILABLE is None:
        try:
            import gliner  # noqa: F401
            import glirel  # noqa: F401
            import torch  # noqa: F401
            _ML_AVAILABLE = True
        except ImportError:
            _ML_AVAILABLE = False
    return _ML_AVAILABLE


def _build_glirel(device: str) -> Any:
    pc = _ensure_local_ghost_b_on_path()
    import json as _json

    from glirel_infer import GliRELClassifier

    root = _repo_root()
    ckpt = os.environ.get("GLIREL_CKPT_DIR") or str(root / "models" / pc.GLIREL_BUNDLE / "best")
    labels_path = Path(ckpt) / pc.GLIREL_LABELS_FILE
    if not labels_path.exists():
        labels_path = root / "local_ghost_b" / "heads" / pc.GLIREL_BUNDLE / pc.GLIREL_LABELS_FILE
    labels = _json.loads(Path(labels_path).read_text(encoding="utf-8"))
    logger.info("ghost_b_local: loading GLiREL %s on %s (%d labels)", ckpt, device, len(labels))
    return GliRELClassifier(
        ckpt, labels, device,
        threshold=pc.GLIREL_THRESHOLD,
        max_entities=pc.PAIR_MAX_ENTITIES_PER_CHUNK,
        max_tokens=pc.GLIREL_MAX_TOKENS_PER_SENTENCE,
        type_gate=pc.APPLY_TYPE_CONSTRAINTS,
        danger_guard=pc.APPLY_DANGER_GUARD,
    )


def _get_glirel() -> Any:
    """Lazy, cached fine-tuned GLiREL classifier (sentence-windowed, type-gated)."""
    global _GLIREL
    if _GLIREL is None:
        from glirel_infer import pick_device
        _GLIREL = _build_glirel(pick_device())
    return _GLIREL


def _get_glirel_cpu() -> Any | None:
    """Second GLiREL on the CPU cores (dual-lane). None when the GPU lane IS
    the CPU (no point doubling) or the lane is disabled."""
    global _GLIREL_CPU
    if not GLIREL_CPU_LANE:
        return None
    gpu = _get_glirel()
    if getattr(gpu, "device", "cpu") == "cpu":
        return None
    if _GLIREL_CPU is None:
        import torch
        # Leave cores for the embedder/uvicorn/IO; 8 of 10 for the lane.
        torch.set_num_threads(max(1, min(8, (os.cpu_count() or 8) - 2)))
        _GLIREL_CPU = _build_glirel("cpu")
    return _GLIREL_CPU


# ----------------------------------------------------------------- helpers
def _lens_id(schema_lens: Any) -> str | None:
    """Best-effort schema_lens id for ExtractionResult.schema_lens_id passthrough."""
    if schema_lens is None:
        return None
    for attr in ("schema_lens_id", "lens_id", "id"):
        v = getattr(schema_lens, attr, None)
        if v:
            return str(v)
    if isinstance(schema_lens, dict):
        for k in ("schema_lens_id", "lens_id", "id"):
            if schema_lens.get(k):
                return str(schema_lens[k])
    return None


def _task_dict(task: Any) -> dict:
    """Normalize an ExtractionTask (or wire dict) to the plain-dict task shape.

    chunk_kind routes table chunks to the deterministic table-fact extractor;
    `columns` is the one metadata key extraction consumes (set by the table
    linearizer), slimmed out of the full metadata dict so the wire format stays
    small and JSON-safe."""
    if isinstance(task, dict):
        meta = task.get("metadata") or {}
        columns = task.get("columns") or (meta.get("columns") if isinstance(meta, dict) else None)
        return {
            "chunk_id": str(task.get("chunk_id") or ""),
            "doc_id": str(task.get("doc_id") or ""),
            "corpus_id": str(task.get("corpus_id") or ""),
            "text": task.get("text") or "",
            "chunk_kind": str(task.get("chunk_kind") or "body").lower(),
            "columns": [str(c) for c in (columns or []) if str(c).strip()],
        }
    meta = getattr(task, "metadata", None) or {}
    columns = meta.get("columns") if isinstance(meta, dict) else None
    return {
        "chunk_id": getattr(task, "chunk_id", "") or "",
        "doc_id": getattr(task, "doc_id", "") or "",
        "corpus_id": getattr(task, "corpus_id", "") or "",
        "text": getattr(task, "text", "") or "",
        "chunk_kind": str(getattr(task, "chunk_kind", "") or "body").lower(),
        "columns": [str(c) for c in (columns or []) if str(c).strip()],
    }


def _dedupe_entities(raw: list[dict], is_junk_surface) -> list[dict]:
    """Collapse GLiNER hits into one entity per canonical_name, drop junk
    surfaces, keep the max softmax score as confidence and surface variants as
    query_aliases.

    Two levels: first by (canonical, type) — like chunk_with_gliner.dedupe_entities
    but PRESERVING the score (that function drops it) — then a second collapse to
    one entity per canonical_name, where GLiNER tagged the same surface with two
    types in different sentences (e.g. Flame as Software AND Organization). The
    highest-confidence type wins; the other surfaces fold into query_aliases. The
    cloud lane likewise emits one entity per canonical, and a single entity per
    name keeps GLiREL span assignment and the Neo4j MERGE clean. Deterministic:
    GLiNER output order is stable, so insertion order and max()-tie-breaking are
    reproducible."""
    by_key: dict[tuple[str, str], dict] = {}
    for e in raw:
        surface = (e.get("text") or "").strip()
        label = e.get("label") or "Concept"
        if not surface or is_junk_surface(surface, label):
            continue
        canonical = surface.lower()
        key = (canonical, label)
        score = float(e.get("score") or 0.0)
        slot = by_key.get(key)
        if slot is None:
            by_key[key] = {
                "canonical_name": canonical,
                "entity_type": label,
                "surface_form": surface,
                "query_aliases": [],
                "confidence": score,
            }
        else:
            slot["confidence"] = max(slot["confidence"], score)
            if surface != slot["surface_form"] and surface not in slot["query_aliases"]:
                slot["query_aliases"].append(surface)

    # second level: one entity per canonical_name (highest-confidence type wins)
    grouped: dict[str, list[dict]] = {}
    for slot in by_key.values():
        grouped.setdefault(slot["canonical_name"], []).append(slot)
    out: list[dict] = []
    for canon, slots in grouped.items():
        rep = max(slots, key=lambda s: s["confidence"])
        aliases = list(rep["query_aliases"])
        seen = {rep["surface_form"].lower(), canon}
        seen.update(a.lower() for a in aliases)
        for s in slots:
            for a in (s["surface_form"], *s["query_aliases"]):
                if a and a.lower() not in seen:
                    aliases.append(a)
                    seen.add(a.lower())
        rep["query_aliases"] = aliases[:5]
        out.append(rep)
    return out


def _noise_gate(ent_dicts: list[dict], pc: Any, counters: dict) -> list[dict]:
    """Deterministic entity precision gates (see pipeline_config):
    blocklisted single-word generics die at any confidence; low-confidence
    all-lowercase single words die below GLINER_ENTITY_CONF_FLOOR. Multi-word
    names and proper-cased surfaces are exempt from the floor."""
    floor = pc.GLINER_ENTITY_CONF_FLOOR
    blocklist = pc.GENERIC_ENTITY_BLOCKLIST
    out = []
    for d in ent_dicts:
        canon = d.get("canonical_name") or ""
        surface = d.get("surface_form") or canon
        if canon in blocklist:
            counters["entity_drop"] += 1
            continue
        if (float(d.get("confidence") or 0.0) < floor
                and " " not in canon
                and surface.isalpha()
                and surface == surface.lower()):
            counters["entity_drop"] += 1
            continue
        out.append(d)
    return out


def _merge_aliases(ent_dicts: list[dict], alias_map: dict[str, list[str]]) -> None:
    """Fold in-text aliases (Schwartz-Hearst + casing) into each entity dict,
    cap 5, dedup — so both GLiREL span-location and EntityItem.query_aliases see
    them."""
    for d in ent_dicts:
        canon = d.get("canonical_name") or ""
        new = alias_map.get(canon) or []
        if not new:
            continue
        existing = {a.lower() for a in (d.get("query_aliases") or []) if a}
        existing.add((d.get("surface_form") or "").lower())
        existing.add(canon.lower())
        merged = list(d.get("query_aliases") or [])
        for a in new:
            if isinstance(a, str) and a.lower() not in existing:
                merged.append(a)
                existing.add(a.lower())
        d["query_aliases"] = merged[:5]


# ----------------------------------------------- validation -> wire dicts
def _validated_entities(ent_dicts: list[dict], counters: dict) -> list[dict]:
    out = []
    for d in ent_dicts:
        try:
            cand = LLMEntity(
                canonical_name=str(d.get("canonical_name") or ""),
                surface_form=str(d.get("surface_form") or "")[:300],
                entity_type=d.get("entity_type") or "other",
                confidence=float(d.get("confidence") or 0.0),
                query_aliases=[a for a in (d.get("query_aliases") or []) if isinstance(a, str)][:5],
                object_kind="",  # filled by the doc-level facet pass
            )
        except (ValidationError, ValueError, TypeError):
            counters["entity_drop"] += 1
            continue
        out.append(cand.model_dump())
    return out


def _validated_relations(edges: list[dict], counters: dict) -> list[dict]:
    out = []
    for edge in edges:
        subj = (edge.get("sub") or "").strip()
        obj = (edge.get("obj") or "").strip()
        pred = (edge.get("pred") or "related_to").strip()
        ev = (edge.get("ev") or "").strip()
        if not subj or not obj:
            counters["relation_drop"] += 1
            continue
        if not ev:  # Phase B evidence gate — no traceable phrase, drop it
            counters["evidence_drop"] += 1
            continue
        try:
            cand = LLMRelation(
                subject=subj, predicate=pred, object=obj,
                object_kind="entity",  # GLiREL relations are always entity->entity
                confidence=float(edge.get("score") or 0.0),
                evidence_phrase=ev[:500], relation_cue="",
            )
        except (ValidationError, ValueError, TypeError):
            counters["relation_drop"] += 1
            continue
        out.append(cand.model_dump())
    return out


def _validated_facts(fact_dicts: list[dict], confidence: float, counters: dict) -> list[dict]:
    out = []
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
            counters["fact_drop"] += 1
            continue
        out.append(v.model_dump())
    return out


# --------------------------------------------------------- synchronous core
def _extract_raw(task_dicts: list[dict], do_facts: bool, lens_id: str | None) -> list[dict]:
    """The blocking GLiNER+GLiREL+enrich pipeline. Runs under the inference lock
    in whatever process has the ML stack (native worker thread OR the sidecar).
    Takes and returns PLAIN DICTS only — no services.ghost_b import — so the
    sidecar can serve it from the local_ghost_b venv. One result dict per task,
    order preserved; entities/relations/facts are already Pydantic-validated.

    Model calls are BATCHED across the doc's chunks (GLINER_BATCH_SIZE /
    GLIREL_UNIT_BATCH / FACET_BATCH in pipeline_config): per-chunk forward
    passes left the GPU idle between tiny kernels and dominated wall time.
    Batch composition follows chunk order, so it is deterministic per doc."""
    import time as _time

    pc = _ensure_local_ghost_b_on_path()
    from chunk_with_gliner import is_junk_surface, strip_noise

    entity_types = pc.GHOST_B_ENTITY_TYPES
    gliner_threshold = pc.GLINER_THRESHOLD
    max_related = pc.MAX_RELATED_TO_PER_CHUNK
    gliner_bs = max(1, int(getattr(pc, "GLINER_BATCH_SIZE", 32)))
    glirel_ub = max(1, int(getattr(pc, "GLIREL_UNIT_BATCH", 64)))

    with _INFER_LOCK:
        gliner = get_gliner()
        glirel = _get_glirel()
        t0 = _time.time()

        n = len(task_dicts)
        is_table_flags = [t.get("chunk_kind") == "table" for t in task_dicts]
        counters_per = [
            {"entity_drop": 0, "relation_drop": 0, "evidence_drop": 0, "fact_drop": 0}
            for _ in range(n)
        ]

        # ---- Stage A: GLiNER pass-1, batched across chunks -----------------
        # Prose chunks see noise-stripped text (facts/evidence/GLiREL stay on
        # RAW text — strip only removes content, so surfaces still locate);
        # table chunks see cell VALUES only (cloud rule: headers/captions are
        # never entities).
        gliner_inputs: list[str] = []
        for task, is_table in zip(task_dicts, is_table_flags):
            text = task["text"]
            if not text.strip():
                gliner_inputs.append("")
            elif is_table:
                gliner_inputs.append(table_entity_text(text, task.get("columns")) or "")
            else:
                gliner_inputs.append(strip_noise(text))
        raw_per_task: list[list] = [[] for _ in range(n)]
        live = [(i, s) for i, s in enumerate(gliner_inputs) if s.strip()]
        for start in range(0, len(live), gliner_bs):
            sl = live[start:start + gliner_bs]
            for (i, _s), spans in zip(
                sl,
                gliner.batch_predict_entities(
                    [s for _i, s in sl], entity_types, threshold=gliner_threshold,
                    # Forward size measured on real book chunks (CUDA): 8 is
                    # the sweet spot — large forwards pad length-varied texts
                    # to the batch max and LOSE (8: 328 ms/chunk total;
                    # 32: 654; 256: 842). Outer slicing still amortizes the
                    # Python call overhead; this knob only sizes the GPU
                    # forward.
                    batch_size=max(1, int(getattr(pc, "GLINER_FORWARD", 8)))),
            ):
                raw_per_task[i] = spans
        t_gliner = _time.time()

        # ---- Stage B: CPU per chunk — gates, aliases, definitional ---------
        ents_per_task: list[list[dict]] = []
        entities_per_task: list[list[dict]] = []
        defs_by_chunk: list[tuple[str, dict[str, str]]] = []
        for i, task in enumerate(task_dicts):
            counters = counters_per[i]
            ent_dicts = _noise_gate(
                _dedupe_entities(raw_per_task[i], is_junk_surface), pc, counters)
            if ent_dicts and not is_table_flags[i]:
                _merge_aliases(ent_dicts, extract_aliases(task["text"], ent_dicts))
                defs = extract_definitional_phrases(task["text"], ent_dicts)
                if defs:
                    defs_by_chunk.append((task["chunk_id"], defs))
            ents_per_task.append(ent_dicts)
            entities_per_task.append(_validated_entities(ent_dicts, counters))
        t_cpu = _time.time()

        # ---- Stage C: GLiREL, sentence units batched across chunks ---------
        # Dual-lane: 2 of every 3 relation-bearing chunks go to the GPU
        # classifier, the rest to a CPU instance running in a parallel thread
        # (torch releases the GIL during forwards). Assignment is positional,
        # so lane placement is deterministic per doc.
        rel_idx = [i for i in range(n)
                   if not is_table_flags[i] and len(ents_per_task[i]) >= 2]
        relations_per_task: list[list[dict]] = [[] for _ in range(n)]
        if rel_idx:
            def chunk_of(i: int) -> dict:
                return {
                    "chunk_id": task_dicts[i]["chunk_id"],
                    "doc_id": task_dicts[i]["doc_id"],
                    "text": task_dicts[i]["text"],
                    "entities": ents_per_task[i],
                }

            glirel_cpu = _get_glirel_cpu()
            if glirel_cpu is not None and len(rel_idx) >= 12:
                gpu_idx = [i for k, i in enumerate(rel_idx) if k % 3 != 2]
                cpu_idx = [i for k, i in enumerate(rel_idx) if k % 3 == 2]
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f_gpu = pool.submit(
                        glirel.extract_chunks, [chunk_of(i) for i in gpu_idx],
                        max_related, glirel_ub)
                    f_cpu = pool.submit(
                        glirel_cpu.extract_chunks, [chunk_of(i) for i in cpu_idx],
                        max_related, glirel_ub)
                    for idx_list, edge_lists in ((gpu_idx, f_gpu.result()),
                                                 (cpu_idx, f_cpu.result())):
                        for i, edges in zip(idx_list, edge_lists):
                            relations_per_task[i] = _validated_relations(
                                edges, counters_per[i])
            else:
                edge_lists = glirel.extract_chunks(
                    [chunk_of(i) for i in rel_idx],
                    max_related=max_related, unit_batch=glirel_ub)
                for i, edges in zip(rel_idx, edge_lists):
                    relations_per_task[i] = _validated_relations(edges, counters_per[i])
        t_glirel = _time.time()

        # ---- Stage D: facts (pure Python) -----------------------------------
        results: list[dict] = []
        for i, task in enumerate(task_dicts):
            text = task["text"]
            counters = counters_per[i]
            facts: list[dict] = []
            if do_facts and text.strip():
                if is_table_flags[i]:
                    facts += _validated_facts(
                        extract_table_facts(text, task.get("columns"),
                                            max_facts=pc.TABLE_MAX_FACTS_PER_CHUNK),
                        _FACT_CONF_DETERMINISTIC, counters)
                elif ents_per_task[i]:
                    facts += _validated_facts(
                        extract_facts(text, ents_per_task[i]),
                        _FACT_CONF_DETERMINISTIC, counters)
                    facts += _validated_facts(
                        extract_qualitative_facts(text, ents_per_task[i]),
                        _FACT_CONF_QUALITATIVE, counters)

            results.append({
                "schema_version": SCHEMA_VERSION,
                "chunk_id": task["chunk_id"],
                "doc_id": task["doc_id"],
                "corpus_id": task["corpus_id"],
                "entities": entities_per_task[i],
                "relations": relations_per_task[i],
                "facts": facts,
                "text": text,  # Pt 10b — REQUIRED for downstream taxonomy matching
                "entity_drop_count": counters["entity_drop"],
                "relation_drop_count": counters["relation_drop"],
                "evidence_drop_count": counters["evidence_drop"],
                "fact_drop_count": counters["fact_drop"],
                "schema_lens_id": lens_id,
            })

        # ---- doc-level: definitional phrases (first definition wins) ---------
        doc_defs: dict[str, str] = {}
        for _cid, defs in sorted(defs_by_chunk, key=lambda x: x[0]):  # deterministic
            for canon, sent in defs.items():
                doc_defs.setdefault(canon, sent)
        if doc_defs:
            for r in results:
                for e in r["entities"]:
                    canon = (e.get("canonical_name") or "").strip().lower()
                    if canon in doc_defs and not e.get("definitional_phrase"):
                        e["definitional_phrase"] = doc_defs[canon]

        # ---- doc-level GLiNER pass-2: object_kind facet per unique entity ----
        all_entities = [e for r in results for e in r["entities"]]
        if all_entities:
            # Context preference: the entity's defining sentence ("X is a Y…")
            # beats the first-occurrence chunk — it's exactly the construction
            # the facet vocabulary keys on, and it ignores surrounding noise.
            context_by_entity: dict[str, str] = dict(doc_defs)
            for r in sorted(results, key=lambda x: x["chunk_id"]):  # deterministic first-occurrence
                ctx = (r["text"] or "")[:1000]
                if not ctx:
                    continue
                for e in r["entities"]:
                    canon = (e.get("canonical_name") or "").strip().lower()
                    if canon and canon not in context_by_entity:
                        context_by_entity[canon] = ctx
            try:
                tag_facets(all_entities, context_by_entity, model=gliner)
            except Exception as exc:  # noqa: BLE001 — facets are best-effort, never abort
                logger.warning("ghost_b_local: facet pass failed: %s", exc)
        t_facet = _time.time()

        logger.info(
            "ghost_b_local: %d chunks in %.1fs (gliner %.1fs, cpu %.1fs, "
            "glirel %.1fs, facts+facets %.1fs) = %.0f ms/chunk",
            n, t_facet - t0, t_gliner - t0, t_cpu - t_gliner,
            t_glirel - t_cpu, t_facet - t_glirel,
            (t_facet - t0) * 1000 / max(1, n),
        )
        # Exposed in the sidecar's /extract response so remote callers can
        # see the stage split without log access (single-writer: we hold
        # _INFER_LOCK).
        LAST_TIMINGS.clear()
        LAST_TIMINGS.update({
            "chunks": n,
            "total_s": round(t_facet - t0, 2),
            "gliner_s": round(t_gliner - t0, 2),
            "cpu_s": round(t_cpu - t_gliner, 2),
            "glirel_s": round(t_glirel - t_cpu, 2),
            "facts_facets_s": round(t_facet - t_glirel, 2),
            "ms_per_chunk": round((t_facet - t0) * 1000 / max(1, n)),
        })

    return results


# ----------------------------------------------------- wire -> dataclasses
def _to_results(raw: list[dict]) -> list:
    """Build ExtractionResult dataclasses from validated wire dicts. The ONLY
    place this module touches services.ghost_b — backend-side only."""
    from services.ghost_b import EntityItem, ExtractionResult, FactItem, RelationItem

    out = []
    for r in raw:
        entities = [
            EntityItem(
                canonical_name=e["canonical_name"],
                surface_form=e.get("surface_form", ""),
                entity_type=e["entity_type"],
                confidence=float(e.get("confidence") or 0.0),
                query_aliases=list(e.get("query_aliases") or []),
                definitional_phrase=e.get("definitional_phrase", ""),
                object_kind=e.get("object_kind", ""),
            )
            for e in (r.get("entities") or [])
        ]
        relations = [
            RelationItem(
                subject=x["subject"], predicate=x["predicate"], object=x["object"],
                object_kind=x.get("object_kind", "entity"),
                confidence=float(x.get("confidence") or 0.0),
                evidence_phrase=x.get("evidence_phrase", ""),
                relation_cue=x.get("relation_cue", ""),
                source_predicate=None, validation_status=None,
            )
            for x in (r.get("relations") or [])
        ]
        facts = [
            FactItem(
                subject=f["subject"], fact_type=f["fact_type"],
                property_name=f.get("property_name", ""), value=f.get("value", ""),
                unit=(f.get("unit") or None), condition=(f.get("condition") or None),
                confidence=float(f.get("confidence") or 0.0),
                evidence_phrase=f.get("evidence_phrase", ""),
            )
            for f in (r.get("facts") or [])
        ]
        out.append(ExtractionResult(
            schema_version=r.get("schema_version") or SCHEMA_VERSION,
            chunk_id=r.get("chunk_id", ""),
            doc_id=r.get("doc_id", ""),
            corpus_id=r.get("corpus_id", ""),
            entities=entities,
            relations=relations,
            facts=facts,
            text=r.get("text", ""),
            entity_drop_count=int(r.get("entity_drop_count") or 0),
            relation_drop_count=int(r.get("relation_drop_count") or 0),
            evidence_drop_count=int(r.get("evidence_drop_count") or 0),
            fact_drop_count=int(r.get("fact_drop_count") or 0),
            schema_lens_id=r.get("schema_lens_id"),
        ))
    return out


def _metrics(raw: list[dict]) -> dict:
    return {
        "model": "ghost_b_local",
        "schema_version": SCHEMA_VERSION,
        "n_chunks": len(raw),
        "n_entities": sum(len(r.get("entities") or []) for r in raw),
        "n_relations": sum(len(r.get("relations") or []) for r in raw),
        "n_facts": sum(len(r.get("facts") or []) for r in raw),
        "entity_drop_count": sum(int(r.get("entity_drop_count") or 0) for r in raw),
        "relation_drop_count": sum(int(r.get("relation_drop_count") or 0) for r in raw),
        "evidence_drop_count": sum(int(r.get("evidence_drop_count") or 0) for r in raw),
        "fact_drop_count": sum(int(r.get("fact_drop_count") or 0) for r in raw),
    }


# ------------------------------------------------------------- http client
async def _extract_via_sidecar(task_dicts: list[dict], do_facts: bool, lens_id: str | None) -> list[dict]:
    """POST the doc's tasks to the ghost_b_extract sidecar(s) and return the
    concatenated raw wire dicts, order preserved.

    Slicing bounds request size/timeout for book-scale docs. With ONE sidecar,
    slices go sequentially (SIDECAR_SLICE keeps most docs single-slice so the
    doc-level facet/definitional dedup sees the whole document). With MULTIPLE
    sidecars (comma-separated LOCAL_GHOST_B_EXTRACT_URL), the doc is split so
    every instance works in parallel — slices dispatch round-robin and run
    concurrently. Per-slice facet re-prediction overlap and lost cross-slice
    definitional backfill are the accepted price of N-way parallelism (the
    cloud lane had no doc-level pass at all). Timing dicts from each slice
    response are logged for remote stage diagnosis. Raises on any failure —
    extraction is the pipeline, not an optional enrichment."""
    import asyncio as _asyncio

    import httpx

    n_urls = len(SIDECAR_URLS)
    slice_size = SIDECAR_SLICE
    if n_urls > 1 and task_dicts:
        # Split the doc across instances, but never below 64 chunks per slice
        # (tiny slices waste batching) and never above SIDECAR_SLICE.
        per_url = -(-len(task_dicts) // n_urls)  # ceil
        slice_size = max(64, min(SIDECAR_SLICE, per_url))

    slices = [task_dicts[s:s + slice_size]
              for s in range(0, len(task_dicts), slice_size)]

    async def _post_slice(client: httpx.AsyncClient, idx: int, sl: list[dict]) -> list[dict]:
        url = SIDECAR_URLS[idx % n_urls]
        resp = await client.post(
            f"{url}/extract",
            json={"tasks": sl, "enable_facts": do_facts, "schema_lens_id": lens_id},
        )
        resp.raise_for_status()
        data = resp.json()
        t = data.get("timings")
        if t:
            logger.info("ghost_b_local: sidecar %s slice %d -> %s", url, idx, t)
        return list(data.get("results") or [])

    try:
        async with httpx.AsyncClient(timeout=SIDECAR_TIMEOUT_S) as client:
            if n_urls == 1:
                out: list[list[dict]] = []
                for i, sl in enumerate(slices):
                    out.append(await _post_slice(client, i, sl))
            else:
                out = list(await _asyncio.gather(
                    *(_post_slice(client, i, sl) for i, sl in enumerate(slices))
                ))
    except Exception as exc:
        raise RuntimeError(
            f"ghost_b_local: extract sidecar(s) {SIDECAR_URLS} failed "
            f"({type(exc).__name__}: {exc}). Start via "
            "scripts/apple_ml_services/start.sh (START_GHOST_B_EXTRACT=true) "
            "or run the worker natively where torch/gliner/glirel are importable."
        ) from exc

    results: list[dict] = []
    for r in out:
        results.extend(r)
    return results


# ----------------------------------------------------------------- entry point
async def extract_entities(
    tasks: list,
    model: str | None = None,
    schema: Any = None,
    schema_lens: Any = None,
    chunk_vectors: dict[str, list[float]] | None = None,
    schema_resolver: Any = None,
    *,
    pool: list[dict] | None = None,
    return_report: bool = False,
    enable_facts: bool | None = None,
    audit_event_sink: Any = None,
    audit_run_id: str | None = None,
) -> list:
    """Local Ghost B extraction. Signature mirrors services.ghost_b.extract_entities
    so the worker's _b_branch swaps the import with no call-site change.

    Honored: tasks, schema_lens (id passthrough), return_report, enable_facts.
    Accepted-and-ignored (no LLM in this lane): model, schema, chunk_vectors,
    schema_resolver, pool, audit_event_sink, audit_run_id.
    """
    if not tasks:
        return []

    do_facts = True if enable_facts is None else bool(enable_facts)
    lens_id = _lens_id(schema_lens)
    task_dicts = [_task_dict(t) for t in tasks]

    mode = EXTRACT_MODE
    if mode not in ("inproc", "http"):
        mode = "inproc" if _ml_stack_available() else "http"

    if mode == "inproc":
        raw = await asyncio.to_thread(_extract_raw, task_dicts, do_facts, lens_id)
    else:
        raw = await _extract_via_sidecar(task_dicts, do_facts, lens_id)

    results = _to_results(raw)

    if return_report:
        from services.ghost_b import ExtractionBatchReport
        return ExtractionBatchReport(results=results, failures=[], metrics=_metrics(raw))
    return results
