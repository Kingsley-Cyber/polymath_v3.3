"""relex_extract_svc — the ONE executable GLiNER-Relex extraction service.

Deterministic local Relex lane (owner invariant 2026-08-03: one extraction
path only). This sidecar replaces the retired ordinary-GLiNER/GLiREL
ghost_b_extract_svc lane for the canonical `relex_local` engine.

Contract:
  - Model: knowledgator/gliner-relex-large-v1.0, weights hash-pinned.
  - Generation parameters are FROZEN and byte-match
    backend/scripts/regen_relex_benchmark.py (the gold-scoring generator):
    11 entity labels, 28 relation labels, ent/rel thresholds 0.30,
    batch size 1, flat_ner False.
  - Output rows are the goldscore prediction shape consumed by
    backend/services/extraction/relex_adapter.build_relation_evidence:
    entities [{start,end,text,type,score}], raw_pair_scores
    [{head,tail,scores}] (sigmoid over ALL 28 labels for every ordered
    pair whose endpoints both passed the entity threshold), plus
    thresholded relations for compatibility.
  - Device: MPS FP32 (frozen determinism contract), CPU fallback only when
    explicitly allowed via RELEX_ALLOW_CPU_FALLBACK=1.
  - Readiness: /health reports ready=false until model file exists, hash
    matches the pin, the session loads, and a test inference returns the
    expected schema. While not ready, /extract fails closed (503).

There is NO fallback to any older extractor here. Relex unavailable →
extraction blocked, exact reason recorded by the caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("relex_extract_svc")

# ---------------------------------------------------------------------------
# Frozen generation parameters — MUST match regen_relex_benchmark.py exactly.
# ---------------------------------------------------------------------------
MODEL_ID = os.environ.get("RELEX_MODEL_ID", "knowledgator/gliner-relex-large-v1.0")
ENTITY_LABELS = [
    "person", "organization", "location", "product", "software",
    "document", "method", "concept", "event", "standard", "artifact",
]
RELATION_LABELS = [
    "affiliated with", "causes", "created by", "creates", "depends on",
    "deploys", "derived from", "detects", "evaluates", "example of",
    "has part", "implements", "improves", "includes", "instance of",
    "located in", "member of", "owns", "part of", "produces",
    "quantizes", "references", "runs on", "supports", "synonym of",
    "trains", "uses", "works for",
]
ENT_THRESHOLD = 0.30
REL_THRESHOLD = 0.30
FLAT_NER = False
EXTRACTOR_IDENTITY = "relex_local"
SERVICE_SCHEMA_VERSION = "polymath.relex_sidecar.v1"

# Release pin: sha256 of model.safetensors for gliner-relex-large-v1.0
# (HF snapshot revision 4aedc9226a5ac9e2f6b5ea3e91c1ee577c88a290).
MODEL_HASH_PIN = os.environ.get(
    "RELEX_MODEL_HASH_PIN",
    "7c5bd751e1b24e4254d70fe4355a986cd65400676ce3735f7752429fcc26960a",
)

# Dense-chunk guard (owner safeguard #11): bounded pair growth. The upstream
# chunker owns sentence-boundary splitting; the sidecar hard-caps the entity
# set so one pathological chunk cannot N^2-crash the batch. Truncation is
# recorded honestly in the row metrics — never silent.
ENTITY_HARD_LIMIT = 30

_TEST_SENTENCE = "Ada Lovelace created the first algorithm for the Analytical Engine."


def _label_inventory_hash(labels: list[str]) -> str:
    payload = json.dumps(sorted(labels), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


ENTITY_SCHEMA_HASH = _label_inventory_hash(ENTITY_LABELS)
RELATION_SCHEMA_HASH = _label_inventory_hash(RELATION_LABELS)


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------
class _Runtime:
    def __init__(self) -> None:
        self.model: Any = None
        self.device: str = "unresolved"
        self.ready = False
        self.readiness_error = ""
        self.model_hash_verified = False
        self.model_hash = ""
        self.model_file_path = ""
        self.test_inference_ok = False
        self.lock = threading.Lock()


RT = _Runtime()
app = FastAPI(title="relex_extract_svc", version="1.0.0")


def _resolve_model_file() -> tuple[str, str]:
    """Locate the cached safetensors weights and return (path, sha256)."""
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    repo_dir = hub / ("models--" + MODEL_ID.replace("/", "--"))
    snap_root = repo_dir / "snapshots"
    if snap_root.is_dir():
        for snap in sorted(snap_root.iterdir()):
            weights = snap / "model.safetensors"
            if weights.exists():
                h = hashlib.sha256()
                with weights.open("rb") as fh:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        h.update(block)
                return str(weights), h.hexdigest()
    return "", ""


def _resolve_device():
    import torch

    if torch.backends.mps.is_available():
        return torch.device("mps")
    if os.environ.get("RELEX_ALLOW_CPU_FALLBACK") == "1":
        return torch.device("cpu")
    raise RuntimeError(
        "MPS unavailable and RELEX_ALLOW_CPU_FALLBACK != 1 — refusing to "
        "serve on a non-deterministic device path"
    )


def _mps_sync() -> None:
    import torch

    if torch.backends.mps.is_available():
        torch.mps.synchronize()


@app.on_event("startup")
def _startup_readiness() -> None:
    """Owner safeguard #3: verify everything before accepting jobs."""
    try:
        path, digest = _resolve_model_file()
        if not path:
            raise RuntimeError(f"model weights not found for {MODEL_ID}")
        RT.model_file_path = path
        RT.model_hash = digest
        if digest != MODEL_HASH_PIN:
            raise RuntimeError(
                f"model hash mismatch: got {digest}, pin {MODEL_HASH_PIN} — "
                "refusing to serve an unverified extractor"
            )
        RT.model_hash_verified = True

        from gliner import GLiNER
        import torch

        device = _resolve_device()
        model = GLiNER.from_pretrained(MODEL_ID)
        model = model.to(device)
        model.eval()
        _mps_sync()
        RT.model = model
        RT.device = str(device)

        # Test inference must return the expected schema before we go ready.
        row = _predict_one(_TEST_SENTENCE)
        if not isinstance(row.get("entities"), list):
            raise RuntimeError("test inference did not return entities")
        if not isinstance(row.get("raw_pair_scores"), list):
            raise RuntimeError("test inference did not return raw_pair_scores")
        RT.test_inference_ok = True
        RT.ready = True
        log.info(
            "relex_extract_svc READY device=%s model_hash_verified=%s "
            "test_entities=%d",
            RT.device, RT.model_hash_verified, len(row["entities"]),
        )
    except Exception as exc:  # noqa: BLE001 — readiness must never crash boot
        RT.ready = False
        RT.readiness_error = f"{type(exc).__name__}: {exc}"
        log.error("relex_extract_svc NOT READY: %s", RT.readiness_error)


@app.get("/health")
async def health() -> dict[str, Any]:
    # Async + no locks: must stay responsive while /extract holds MPS.
    return {
        "status": "ok" if RT.ready else "not_ready",
        "extractor": EXTRACTOR_IDENTITY,
        "ready": RT.ready,
        "readiness_error": RT.readiness_error or None,
        "model_id": MODEL_ID,
        "model_hash_verified": RT.model_hash_verified,
        "model_hash": RT.model_hash or None,
        "model_hash_pin": MODEL_HASH_PIN,
        "model_file": RT.model_file_path or None,
        "device": RT.device,
        "test_inference_ok": RT.test_inference_ok,
        "entity_schema_hash": ENTITY_SCHEMA_HASH,
        "relation_schema_hash": RELATION_SCHEMA_HASH,
        "ent_threshold": ENT_THRESHOLD,
        "rel_threshold": REL_THRESHOLD,
        "service_schema_version": SERVICE_SCHEMA_VERSION,
    }


class ExtractTask(BaseModel):
    chunk_id: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=100_000)


class ExtractRequest(BaseModel):
    tasks: list[ExtractTask] = Field(default_factory=list, max_length=512)


def _find_entity_type(entities: list[dict], cs: int, ce: int) -> str:
    for ent in entities:
        if ent["start"] == cs and ent["end"] == ce:
            return ent.get("type") or ent.get("label") or "unknown"
    return "unknown"


def _row_from_batch(prepared, batch, model_output, decoded_entities,
                    decoded_relations, i: int) -> dict[str, Any]:
    """Build the frozen prediction row for batch index ``i``.

    Single source of truth for the decode + map + raw-pair-score extraction
    shared by _predict_one (i=0) and _predict_many (i in range(B)). Every
    step mirrors backend/scripts/regen_relex_benchmark.py exactly — only the
    batch position is parametrized so batched and unbatched paths cannot
    drift in decode order, thresholds, truncation, or rounding.
    """
    import torch

    entity_outputs_mapped = RT.model.map_entities_to_text(
        decoded_entities,
        prepared["valid_texts"],
        prepared["valid_to_orig_idx"],
        prepared["start_token_map"],
        prepared["end_token_map"],
        prepared["num_original"],
    )
    entities_raw = entity_outputs_mapped[i]

    # P1A: label must flow into type — never lose endpoint types.
    for ent in entities_raw:
        if ent.get("label") and not ent.get("type"):
            ent["type"] = ent["label"]
        elif not ent.get("type") and not ent.get("label"):
            raise RuntimeError(
                f"entity at ({ent.get('start')},{ent.get('end')}) has "
                "neither label nor type — contract violation"
            )

    # Dense-chunk guard: hard entity cap, top-score survivors, recorded.
    entity_cap_truncated = False
    if len(entities_raw) > ENTITY_HARD_LIMIT:
        entity_cap_truncated = True
        entities_raw = sorted(
            entities_raw, key=lambda e: e.get("score", 0.0), reverse=True
        )[:ENTITY_HARD_LIMIT]

    entities_out = [
        {
            "start": ent["start"],
            "end": ent["end"],
            "text": ent["text"],
            "type": ent.get("type") or ent.get("label"),
            "score": round(ent.get("score", 0.0), 6),
        }
        for ent in entities_raw
    ]

    # --- Raw pair scores over ALL labels for every surviving pair ---
    rel_idx = model_output.rel_idx
    rel_logits = model_output.rel_logits
    rel_mask = model_output.rel_mask
    entity_spans = getattr(model_output, "entity_spans", None)

    if not isinstance(rel_idx, torch.Tensor):
        rel_idx = torch.from_numpy(rel_idx)
    if not isinstance(rel_logits, torch.Tensor):
        rel_logits = torch.from_numpy(rel_logits)
    if not isinstance(rel_mask, torch.Tensor):
        rel_mask = torch.from_numpy(rel_mask)

    rel_probs = torch.sigmoid(rel_logits)

    rel_id_to_classes = batch["rel_id_to_classes"]
    if isinstance(rel_id_to_classes, list):
        rel_id_to_classes = rel_id_to_classes[0]
    num_rel_classes = len(rel_id_to_classes)
    ordered_rel_labels = [rel_id_to_classes[c + 1] for c in range(num_rel_classes)]

    start_token_map = prepared["start_token_map"][i]
    end_token_map = prepared["end_token_map"][i]
    valid_text = prepared["valid_texts"][i]

    entity_char_spans: list[tuple[int, int, str]] = []
    if entity_spans is not None:
        if not isinstance(entity_spans, torch.Tensor):
            entity_spans = torch.from_numpy(entity_spans)
        esp = entity_spans[i].tolist()
        for tok_start, tok_end in esp:
            if tok_start < len(start_token_map) and tok_end < len(end_token_map):
                cs = start_token_map[tok_start]
                ce = end_token_map[tok_end]
                entity_char_spans.append((cs, ce, valid_text[cs:ce]))
            else:
                entity_char_spans.append((-1, -1, ""))
    else:
        for span in decoded_entities[i]:
            cs = start_token_map[span.start]
            ce = end_token_map[span.end]
            entity_char_spans.append((cs, ce, valid_text[cs:ce]))

    rel_idx_0 = rel_idx[i].tolist()
    rel_mask_0 = rel_mask[i].tolist()
    rel_probs_0 = rel_probs[i].tolist()
    decoded_ent_offsets = {(e["start"], e["end"]) for e in entities_out}

    raw_pair_scores: list[dict[str, Any]] = []
    for j in range(len(rel_idx_0)):
        if not rel_mask_0[j]:
            continue
        head_model_idx = rel_idx_0[j][0]
        tail_model_idx = rel_idx_0[j][1]
        if head_model_idx < 0 or tail_model_idx < 0:
            continue
        if head_model_idx >= len(entity_char_spans) or tail_model_idx >= len(entity_char_spans):
            continue
        h_cs, h_ce, h_text = entity_char_spans[head_model_idx]
        t_cs, t_ce, t_text = entity_char_spans[tail_model_idx]
        if h_cs < 0 or t_cs < 0:
            continue
        if (h_cs, h_ce) not in decoded_ent_offsets:
            continue
        if (t_cs, t_ce) not in decoded_ent_offsets:
            continue
        scores = {
            label: round(rel_probs_0[j][c], 6)
            for c, label in enumerate(ordered_rel_labels)
        }
        raw_pair_scores.append({
            "head": {
                "start": h_cs, "end": h_ce, "text": h_text,
                "type": _find_entity_type(entities_out, h_cs, h_ce),
                "entity_idx": head_model_idx,
            },
            "tail": {
                "start": t_cs, "end": t_ce, "text": t_text,
                "type": _find_entity_type(entities_out, t_cs, t_ce),
                "entity_idx": tail_model_idx,
            },
            "scores": scores,
        })

    # --- Thresholded relations (compatibility lane) ---
    relations_mapped = RT.model.map_relations_to_text(
        decoded_relations, decoded_entities,
        prepared["valid_texts"], prepared["valid_to_orig_idx"],
        prepared["start_token_map"], prepared["end_token_map"],
        prepared["num_original"],
    )
    relations_out = []
    for rel in relations_mapped[i]:
        head = rel.get("head", {})
        tail = rel.get("tail", {})
        relations_out.append({
            "head": {
                "start": head.get("start", -1),
                "end": head.get("end", -1),
                "text": head.get("text", ""),
                "type": head.get("label") or head.get("type")
                or _find_entity_type(entities_out, head.get("start", -1), head.get("end", -1)),
            },
            "predicate": rel.get("relation", ""),
            "tail": {
                "start": tail.get("start", -1),
                "end": tail.get("end", -1),
                "text": tail.get("text", ""),
                "type": tail.get("label") or tail.get("type")
                or _find_entity_type(entities_out, tail.get("start", -1), tail.get("end", -1)),
            },
            "score": round(rel.get("score", 0.0), 6),
        })

    return {
        "raw_scores_complete": True,
        "entities": entities_out,
        "relations": relations_out,
        "raw_pair_scores": raw_pair_scores,
        "entity_cap_truncated": entity_cap_truncated,
    }


def _predict_many(texts: list[str]) -> list[dict[str, Any]]:
    """A window of chunks → frozen prediction rows, in INPUT order.

    Runs ONE prepare/collate/run/decode forward for the whole window (the
    model API is natively batched), then builds each row via _row_from_batch.
    Item i's row is decode-independent of every other row: decode, span
    mapping, truncation, and rounding are all indexed per-item, so batched
    output must equal per-text _predict_one output bit-for-bit (verified by
    the gold qualification harness). Empty texts short-circuit to empty rows
    at their position, matching _predict_one's contract.
    """
    import torch

    model = RT.model
    n = len(texts)
    rows: list[dict[str, Any] | None] = [None] * n
    forward_idx = [k for k, t in enumerate(texts) if t and t.strip()]
    if not forward_idx:
        for k in range(n):
            rows[k] = {"entities": [], "raw_pair_scores": [], "relations": []}
        return rows  # type: ignore[return-value]

    forward_texts = [texts[k] for k in forward_idx]
    with torch.inference_mode():
        prepared = model.prepare_batch(
            forward_texts, ENTITY_LABELS, None, RELATION_LABELS,
        )
        collator = model.create_collator()
        batch = model.collate_batch(
            prepared["input_x"], prepared["entity_types"], collator,
            prepared["relation_types"],
        )
        model_output = model.run_batch(
            batch, threshold=ENT_THRESHOLD, move_to_device=True,
        )
        decoded_entities, decoded_relations = model.decode_batch(
            model_output, batch,
            threshold=ENT_THRESHOLD,
            relation_threshold=REL_THRESHOLD,
            flat_ner=FLAT_NER,
        )
        for slot, k in enumerate(forward_idx):
            rows[k] = _row_from_batch(
                prepared, batch, model_output, decoded_entities,
                decoded_relations, slot,
            )
        _mps_sync()

    for k in range(n):
        if rows[k] is None:
            rows[k] = {"entities": [], "raw_pair_scores": [], "relations": []}
    return rows  # type: ignore[return-value]


def _predict_one(text: str) -> dict[str, Any]:
    """One chunk → frozen prediction row (entities + ALL raw pair scores).

    Mirrors backend/scripts/regen_relex_benchmark.py step-for-step so the
    production lane and the gold-scoring lane cannot drift. Equivalent to
    _predict_many([text])[0]; kept as the single-chunk fast path and as the
    startup readiness probe.
    """
    return _predict_many([text])[0]


# Batched MPS inference window. 1 = the original per-chunk forward (pure
# parity). >1 amortizes the single forward over RELEX_SIDECAR_BATCH_SIZE
# chunks while keeping decode strictly per-item (see _predict_many).
#
# DEFAULT 1 — DO NOT RAISE without re-qualifying (2026-08-05 qualification,
# CONTINUITY/MPS_BATCH_QUALIFICATION_20260805.md): batched MPS forward on
# gliner-relex-large is NOT output-deterministic vs frozen batch-1 — 99
# raw-confidence flips across the 0.30 relation threshold (max delta 0.856)
# on the 21 gold samples, spread across ALL window positions (not a padding
# or ordering fix). Gold F1/accepted-lane parity held, but the frozen
# determinism contract is output-determinism, and measured speedup was ZERO
# on long-form chunks (median ~520 chars; production chunks match). The knob
# stays for future re-qualification if chunk shapes change or a
# padding-invariant forward restores determinism.
RELEX_SIDECAR_BATCH_SIZE = max(1, int(os.environ.get("RELEX_SIDECAR_BATCH_SIZE", "1")))


def _extract_locked(tasks: list[ExtractTask]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
    """Run the serialized MPS inference lane off the asyncio event loop."""
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    t0 = time.time()
    # Single inference lane: serialize all chunks through one lock so MPS
    # determinism and memory stay bounded regardless of request fan-out.
    # Chunk-level failure isolation is preserved at WINDOW granularity: a
    # pathological window that raises fails only its own chunks, never the
    # whole request — identical behavior to the per-chunk loop when batch=1.
    with RT.lock:
        for wstart in range(0, len(tasks), RELEX_SIDECAR_BATCH_SIZE):
            window = tasks[wstart:wstart + RELEX_SIDECAR_BATCH_SIZE]
            try:
                rows = _predict_many([t.text for t in window])
                for task, row in zip(window, rows):
                    row["chunk_id"] = task.chunk_id
                    results.append(row)
            except Exception as exc:  # noqa: BLE001 — window-level isolation
                for task in window:
                    failures.append({
                        "chunk_id": task.chunk_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:2000],
                    })
    return results, failures, round(time.time() - t0, 3)


@app.post("/extract")
async def extract(request: Request, body: ExtractRequest) -> JSONResponse:
    # Fail closed: never serve while readiness is incomplete.
    if not RT.ready:
        return JSONResponse(
            status_code=503,
            content={
                "status": "blocked_extractor_unavailable",
                "extractor": EXTRACTOR_IDENTITY,
                "reason": RT.readiness_error or "readiness incomplete",
            },
        )
    # Never block the event loop with MPS work — /health must stay live for
    # Docker preflight and worker readiness probes during long extracts.
    results, failures, latency_s = await asyncio.to_thread(_extract_locked, body.tasks)
    return JSONResponse(content={
        "status": "ok",
        "extractor": EXTRACTOR_IDENTITY,
        "results": results,
        "failures": failures,
        "metrics": {
            "requested_chunks": len(body.tasks),
            "extracted_chunks": len(results),
            "failed_chunks": len(failures),
            "latency_s": latency_s,
        },
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("RELEX_SVC_HOST", "0.0.0.0"),
        port=int(os.environ.get("RELEX_SVC_PORT", "8086")),
        log_level="warning",
    )
