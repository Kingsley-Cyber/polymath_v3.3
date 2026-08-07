#!/usr/bin/env python3
"""Regenerate Relex Large predictions with BATCHED MPS inference.

Qualification variant of regen_relex_benchmark.py: identical generation
parameters (11 entity labels, 28 relation labels, ent/rel thresholds 0.30,
flat_ner False, FP32, MPS) but runs ``--batch-size N`` texts per forward
pass instead of 1. Decode/span-map/raw-pair-score extraction is strictly
per-item (batch position parametrized, decode order unchanged), so a passing
batch size must produce rows that match the frozen batch-1 artifact
(relex_large_v3_mps_fp32.predictions.jsonl) bit-for-bit modulo float
tolerance.

This exists ONLY to qualify RELEX_SIDECAR_BATCH_SIZE before it is raised in
the live sidecar. It is the gold-scoring mirror of the sidecar's
_predict_many — both must stay step-for-step aligned with
regen_relex_benchmark.py (the frozen batch-1 generator).

Usage:
  .venv-relex/bin/python backend/scripts/regen_relex_benchmark_batched.py \
      --device mps --batch-size 8
Output: data/deterministic_gold_score/data/relex_large_v3_mps_fp32.batch{N}.predictions.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

import torch

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_PATH = REPO_ROOT / "data/deterministic_gold_score/data/relex_gold_v1.jsonl"
OUT_DIR = REPO_ROOT / "data/deterministic_gold_score/data"

MODEL_ID = "knowledgator/gliner-relex-large-v1.0"
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
GENERATOR_VERSION = "2.0.0"
SERIALIZATION_SCHEMA_VERSION = "2"


def _mps_sync() -> None:
    if torch.backends.mps.is_available():
        torch.mps.synchronize()


def _schema_hash(labels: list[str]) -> str:
    payload = json.dumps(sorted(labels), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _find_entity_type(entities: list[dict], start: int, end: int) -> str:
    for ent in entities:
        if ent["start"] == start and ent["end"] == end:
            return ent.get("type") or ""
    return ""


def load_gold_texts() -> list[dict[str, Any]]:
    samples = []
    with open(GOLD_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples


def _row_from_batch(prepared, batch, model_output, decoded_entities,
                    decoded_relations, i: int) -> dict[str, Any]:
    """Decode batch position ``i`` — mirrors regen step-for-step, index-parametrized."""
    model = _MODEL
    entity_outputs_mapped = model.map_entities_to_text(
        decoded_entities,
        prepared["valid_texts"],
        prepared["valid_to_orig_idx"],
        prepared["start_token_map"],
        prepared["end_token_map"],
        prepared["num_original"],
    )
    entities_raw = entity_outputs_mapped[i]

    for ent in entities_raw:
        if ent.get("label") and not ent.get("type"):
            ent["type"] = ent["label"]
        elif not ent.get("type") and not ent.get("label"):
            raise RuntimeError(
                f"entity at ({ent.get('start')},{ent.get('end')}) has neither label nor type"
            )

    entities_out = [{
        "start": ent["start"],
        "end": ent["end"],
        "text": ent["text"],
        "type": ent.get("type") or ent.get("label"),
        "score": round(ent.get("score", 0.0), 6),
    } for ent in entities_raw]

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
        scores = {label: round(rel_probs_0[j][c], 6)
                  for c, label in enumerate(ordered_rel_labels)}
        raw_pair_scores.append({
            "head": {"start": h_cs, "end": h_ce, "text": h_text,
                     "type": _find_entity_type(entities_out, h_cs, h_ce),
                     "entity_idx": head_model_idx},
            "tail": {"start": t_cs, "end": t_ce, "text": t_text,
                     "type": _find_entity_type(entities_out, t_cs, t_ce),
                     "entity_idx": tail_model_idx},
            "scores": scores,
        })

    relations_mapped = model.map_relations_to_text(
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
            "head": {"start": head.get("start", -1), "end": head.get("end", -1),
                     "text": head.get("text", ""),
                     "type": head.get("label") or head.get("type")
                     or _find_entity_type(entities_out, head.get("start", -1), head.get("end", -1))},
            "predicate": rel.get("relation", ""),
            "tail": {"start": tail.get("start", -1), "end": tail.get("end", -1),
                     "text": tail.get("text", ""),
                     "type": tail.get("label") or tail.get("type")
                     or _find_entity_type(entities_out, tail.get("start", -1), tail.get("end", -1))},
            "score": round(rel.get("score", 0.0), 6),
        })

    return {
        "raw_scores_complete": True,
        "entities": entities_out,
        "relations": relations_out,
        "raw_pair_scores": raw_pair_scores,
    }


_MODEL = None


@torch.inference_mode()
def run_model(gold_samples, device, batch_size: int):
    global _MODEL
    from gliner import GLiNER

    print(f"Loading model: {MODEL_ID}  device={device}  batch={batch_size}", file=sys.stderr)
    t0 = time.time()
    model = GLiNER.from_pretrained(MODEL_ID)
    model = model.to(device)
    model.eval()
    _mps_sync()
    _MODEL = model
    print(f"  loaded in {time.time() - t0:.1f}s", file=sys.stderr)

    texts = [s["text"] for s in gold_samples]
    sample_ids = [s["sample_id"] for s in gold_samples]
    rows: list[dict[str, Any]] = []

    for wstart in range(0, len(texts), batch_size):
        wtexts = texts[wstart:wstart + batch_size]
        wids = sample_ids[wstart:wstart + batch_size]
        prepared = model.prepare_batch(wtexts, ENTITY_LABELS, None, RELATION_LABELS)
        collator = model.create_collator()
        batch = model.collate_batch(
            prepared["input_x"], prepared["entity_types"], collator,
            prepared["relation_types"],
        )
        model_output = model.run_batch(batch, threshold=ENT_THRESHOLD, move_to_device=True)
        decoded_entities, decoded_relations = model.decode_batch(
            model_output, batch,
            threshold=ENT_THRESHOLD, relation_threshold=REL_THRESHOLD, flat_ner=FLAT_NER,
        )
        for slot, sid in enumerate(wids):
            row = _row_from_batch(prepared, batch, model_output,
                                  decoded_entities, decoded_relations, slot)
            row["sample_id"] = sid
            rows.append(row)
        _mps_sync()
        print(f"  {min(wstart + batch_size, len(texts))}/{len(texts)} samples", file=sys.stderr)

    return rows


def verify_invariants(rows) -> list[str]:
    violations = []
    total_entities = sum(len(r["entities"]) for r in rows)
    if total_entities == 0:
        violations.append("raw_entity_count == 0")
    typed = sum(1 for r in rows for e in r["entities"] if e.get("type"))
    if typed / max(1, total_entities) < 1.0:
        violations.append(f"raw_entity_type_coverage={typed / max(1, total_entities):.4f} != 1.0")
    total_endpoints = valid_idx = offset_match = 0
    for r in rows:
        ent_offsets = {(e["start"], e["end"]) for e in r["entities"]}
        for rp in r["raw_pair_scores"]:
            for k in ("head", "tail"):
                ep = rp[k]
                total_endpoints += 1
                if ep.get("entity_idx") is not None and ep["entity_idx"] >= 0:
                    valid_idx += 1
                if (ep["start"], ep["end"]) in ent_offsets:
                    offset_match += 1
    if total_endpoints:
        if valid_idx / total_endpoints < 1.0:
            violations.append(f"valid_relation_entity_idx_rate={valid_idx / total_endpoints:.4f}")
        if offset_match / total_endpoints < 1.0:
            violations.append(f"relation_endpoint_offset_match_rate={offset_match / total_endpoints:.4f}")
    type_loss = sum(1 for r in rows for rp in r["raw_pair_scores"]
                    for k in ("head", "tail") if not rp[k].get("type"))
    if type_loss:
        violations.append(f"type_loss_after_serialization={type_loss}")
    return violations


def write_artifact(rows, out_path: Path, device, batch_size: int) -> None:
    metadata = {
        "model_id": MODEL_ID,
        "model_revision": MODEL_ID,
        "entity_labels": ENTITY_LABELS,
        "entity_label_schema_hash": _schema_hash(ENTITY_LABELS),
        "relation_labels": RELATION_LABELS,
        "relation_label_schema_hash": _schema_hash(RELATION_LABELS),
        "generator_version": GENERATOR_VERSION,
        "serialization_schema_version": SERIALIZATION_SCHEMA_VERSION,
        "entity_threshold": ENT_THRESHOLD,
        "relation_threshold": REL_THRESHOLD,
        "flat_ner": FLAT_NER,
        "batch_size": batch_size,
        "num_samples": len(rows),
        "total_entities": sum(len(r["entities"]) for r in rows),
        "total_raw_pairs": sum(len(r["raw_pair_scores"]) for r in rows),
        "total_thresholded_relations": sum(len(r["relations"]) for r in rows),
        "device": str(device),
        "dtype": "float32",
        "torch_version": torch.__version__,
        "macos_version": platform.mac_ver()[0] or "n/a",
        "qualification_variant_of": "regen_relex_benchmark.py",
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        f.write(json.dumps({"__metadata__": metadata}, sort_keys=True) + "\n")
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"Wrote {out_path}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="mps")
    ap.add_argument("--batch-size", type=int, required=True)
    args = ap.parse_args()
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be >= 1")

    if args.device == "cpu":
        device = torch.device("cpu")
    else:
        if not torch.backends.mps.is_available():
            raise SystemExit("MPS unavailable")
        device = torch.device("mps")

    gold = load_gold_texts()
    out_path = OUT_DIR / f"relex_large_v3_mps_fp32.batch{args.batch_size}.predictions.jsonl"
    print(f"QUALIFICATION regen batch_size={args.batch_size} -> {out_path.name}", file=sys.stderr)

    _mps_sync()
    t0 = time.time()
    rows = run_model(gold, device, args.batch_size)
    _mps_sync()
    elapsed = time.time() - t0
    print(f"Inference complete in {elapsed:.1f}s ({elapsed / len(gold):.3f}s/sample)", file=sys.stderr)

    violations = verify_invariants(rows)
    if violations:
        print("INVARIANT VIOLATIONS:", file=sys.stderr)
        for v in violations:
            print(f"  FAIL: {v}", file=sys.stderr)
        return 1
    print("  ALL INVARIANTS PASS", file=sys.stderr)
    write_artifact(rows, out_path, device, args.batch_size)
    # Report throughput for the batch comparison.
    print(f"THROUGHPUT batch={args.batch_size} total_s={elapsed:.2f} per_sample_s={elapsed / len(gold):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
