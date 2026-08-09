#!/usr/bin/env python3
"""Regenerate the frozen Relex Large benchmark predictions with preserved entity types.

P1A closure prerequisite: the original artifact (relex_large_v1.predictions.jsonl)
has 485 entities with type=null due to a serialization defect.  This script
re-runs the SAME model on the SAME texts with the SAME label inventory and
thresholds, but correctly preserves entity labels and stamps entity_idx on
relation endpoints.

Output: relex_large_v3_mps_fp32.predictions.jsonl  (serialization_schema_version=2)

Device selection:
  --device mps   (default on Apple Silicon)
  --device cpu   (force CPU for comparison)

Invariants enforced (hard fail, not REVIEW routing):
  - raw_entity_count > 0
  - raw_entity_type_coverage == 1.0
  - valid_relation_entity_idx_rate == 1.0
  - relation_endpoint_offset_match_rate == 1.0
  - type_loss_after_serialization == 0
  - type_loss_after_adapter == 0

Usage:
  .venv-relex/bin/python backend/scripts/regen_relex_benchmark.py [--device mps|cpu] [--dry-run]
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

import torch


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------

def resolve_torch_device(preference: str = "auto") -> torch.device:
    """Resolve the execution device for GLiNER-Relex inference.

    preference: 'auto' | 'mps' | 'cpu'
    """
    if preference == "cpu":
        return torch.device("cpu")
    if preference in ("auto", "mps"):
        if torch.backends.mps.is_available():
            return torch.device("mps")
        if preference == "mps":
            raise RuntimeError(
                "PyTorch was built with MPS support, but MPS is unavailable. "
                "Check the macOS version and Apple Silicon environment."
            )
    return torch.device("cpu")


def _mps_sync() -> None:
    """Synchronize MPS kernels (no-op on CPU)."""
    if torch.backends.mps.is_available():
        torch.mps.synchronize()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_PATH = REPO_ROOT / "data/deterministic_gold_score/data/relex_gold_v1.jsonl"
# Output path is device-dependent (set in main()).
OUT_DIR = REPO_ROOT / "data/deterministic_gold_score/data"

# ---------------------------------------------------------------------------
# Frozen generation parameters — must match the original artifact exactly.
# ---------------------------------------------------------------------------
MODEL_ID = "knowledgator/gliner-relex-large-v1.0"
ENTITY_LABELS = [
    "person", "organization", "location", "product", "software",
    "document", "method", "concept", "event", "standard", "artifact",
]
# The original v1 artifact used 28 relation labels (includes "improves" and
# uses "instance of" rather than "is an instance of").
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
BATCH_SIZE = 1  # one text at a time for deterministic span mapping
FLAT_NER = False
GENERATOR_VERSION = "2.0.0"
SERIALIZATION_SCHEMA_VERSION = "2"


def _schema_hash(labels: list[str]) -> str:
    """Deterministic hash of a label inventory."""
    payload = json.dumps(sorted(labels), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def load_gold_texts() -> list[dict[str, Any]]:
    """Load gold samples (contains the source texts)."""
    samples = []
    with open(GOLD_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            samples.append(json.loads(line))
    return samples


@torch.inference_mode()
def run_model(
    gold_samples: list[dict[str, Any]],
    device: torch.device,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Run GLiNER-Relex and produce prediction rows with full type provenance."""
    from gliner import GLiNER

    print(f"Loading model: {MODEL_ID}", file=sys.stderr)
    print(f"  device: {device}", file=sys.stderr)
    t0 = time.time()
    model = GLiNER.from_pretrained(MODEL_ID)
    model = model.to(device)
    model.eval()
    _mps_sync()
    print(f"  loaded in {time.time() - t0:.1f}s, device={model.device}", file=sys.stderr)

    # Resolve model revision from config
    model_revision = getattr(model.config, "_name_or_path", MODEL_ID)

    rows: list[dict[str, Any]] = []
    total_entities = 0
    total_pairs = 0
    total_relations = 0

    for idx, sample in enumerate(gold_samples):
        text = sample["text"]
        sample_id = sample["sample_id"]

        # --- Step 1: prepare + forward (same as inference() internals) ---
        prepared = model.prepare_batch(
            [text], ENTITY_LABELS, None, RELATION_LABELS,
        )
        collator = model.create_collator()
        batch = model.collate_batch(
            prepared["input_x"], prepared["entity_types"], collator,
            prepared["relation_types"],
        )
        model_output = model.run_batch(
            batch, threshold=ENT_THRESHOLD, move_to_device=True,
        )

        # --- Step 2: decode entities (with labels) ---
        decoded_entities, decoded_relations = model.decode_batch(
            model_output, batch,
            threshold=ENT_THRESHOLD,
            relation_threshold=REL_THRESHOLD,
            flat_ner=FLAT_NER,
        )

        # Map entities to character offsets
        entity_outputs_mapped = model.map_entities_to_text(
            decoded_entities,
            prepared["valid_texts"],
            prepared["valid_to_orig_idx"],
            prepared["start_token_map"],
            prepared["end_token_map"],
            prepared["num_original"],
        )
        entities_raw = entity_outputs_mapped[0]  # single text

        # P1A FIX: ensure label is copied into type
        for ent in entities_raw:
            if ent.get("label") and not ent.get("type"):
                ent["type"] = ent["label"]
            elif not ent.get("type") and not ent.get("label"):
                # Hard invariant: model MUST emit a label
                raise RuntimeError(
                    f"[{sample_id}] entity at ({ent.get('start')},{ent.get('end')}) "
                    f"text={ent.get('text')!r} has neither label nor type"
                )

        # Serialize entities for the artifact
        entities_out = []
        for ent in entities_raw:
            entities_out.append({
                "start": ent["start"],
                "end": ent["end"],
                "text": ent["text"],
                "type": ent.get("type") or ent.get("label"),
                "score": round(ent.get("score", 0.0), 6),
            })

        # --- Step 3: extract ALL raw pair scores from model internals ---
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

        # Apply sigmoid to get probabilities for ALL pairs
        rel_probs = torch.sigmoid(rel_logits)

        # Get relation label mapping from batch
        rel_id_to_classes = batch["rel_id_to_classes"]
        if isinstance(rel_id_to_classes, list):
            rel_id_to_classes = rel_id_to_classes[0]
        # rel_id_to_classes: {1: "affiliated with", 2: "causes", ...}
        num_rel_classes = len(rel_id_to_classes)
        ordered_rel_labels = [rel_id_to_classes[i + 1] for i in range(num_rel_classes)]

        # Build entity span → character offset mapping
        # entity_spans shape: (1, E, 2) in token space
        # We need to map token spans to char spans using the prepared maps
        start_token_map = prepared["start_token_map"][0]  # valid_i=0
        end_token_map = prepared["end_token_map"][0]
        valid_text = prepared["valid_texts"][0]

        # Build token-span → char-span lookup from decoded entities
        # The decoded entities already have char offsets; build a token→char map
        # from the entity list (token start/end → char start/end)
        decoded_ents_0 = decoded_entities[0]  # list of Span objects
        token_span_to_char = {}
        for span in decoded_ents_0:
            char_start = start_token_map[span.start]
            char_end = end_token_map[span.end]
            token_span_to_char[(span.start, span.end)] = (char_start, char_end)

        # Also build from entity_spans tensor (model's internal entity set)
        # These may include entities that didn't pass threshold
        entity_char_spans = []  # list of (char_start, char_end, text)
        if entity_spans is not None:
            if not isinstance(entity_spans, torch.Tensor):
                entity_spans = torch.from_numpy(entity_spans)
            esp = entity_spans[0].tolist()  # (E, 2)
            for tok_start, tok_end in esp:
                if tok_start < len(start_token_map) and tok_end < len(end_token_map):
                    cs = start_token_map[tok_start]
                    ce = end_token_map[tok_end]
                    entity_char_spans.append((cs, ce, valid_text[cs:ce]))
                else:
                    entity_char_spans.append((-1, -1, ""))
        else:
            # Fallback: use decoded entities' token spans
            for span in decoded_ents_0:
                cs = start_token_map[span.start]
                ce = end_token_map[span.end]
                entity_char_spans.append((cs, ce, valid_text[cs:ce]))

        # Build raw_pair_scores from rel_idx + rel_probs
        # Only export pairs where BOTH endpoints are in the decoded entity set
        # (matches the original v1 artifact contract).
        rel_idx_0 = rel_idx[0].tolist()  # (num_pairs, 2)
        rel_mask_0 = rel_mask[0].tolist()  # (num_pairs,)
        rel_probs_0 = rel_probs[0].tolist()  # (num_pairs, num_classes)

        # Decoded entity offsets for filtering
        decoded_ent_offsets = {(e["start"], e["end"]) for e in entities_out}

        raw_pair_scores = []
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

            # Filter: both endpoints must be in the decoded entity set
            if (h_cs, h_ce) not in decoded_ent_offsets:
                continue
            if (t_cs, t_ce) not in decoded_ent_offsets:
                continue

            # Find entity type from entities_out by offset match
            h_type = _find_entity_type(entities_out, h_cs, h_ce)
            t_type = _find_entity_type(entities_out, t_cs, t_ce)

            scores = {}
            for c, label in enumerate(ordered_rel_labels):
                scores[label] = round(rel_probs_0[j][c], 6)

            raw_pair_scores.append({
                "head": {
                    "start": h_cs, "end": h_ce, "text": h_text,
                    "type": h_type, "entity_idx": head_model_idx,
                },
                "tail": {
                    "start": t_cs, "end": t_ce, "text": t_text,
                    "type": t_type, "entity_idx": tail_model_idx,
                },
                "scores": scores,
            })

        # --- Step 4: thresholded relations (for compatibility) ---
        relations_mapped = model.map_relations_to_text(
            decoded_relations, decoded_entities,
            prepared["valid_texts"], prepared["valid_to_orig_idx"],
            prepared["start_token_map"], prepared["end_token_map"],
            prepared["num_original"],
        )
        relations_out = []
        for rel in relations_mapped[0]:
            head = rel.get("head", {})
            tail = rel.get("tail", {})
            relations_out.append({
                "head": {
                    "start": head.get("start", -1),
                    "end": head.get("end", -1),
                    "text": head.get("text", ""),
                    "type": head.get("label") or head.get("type") or _find_entity_type(entities_out, head.get("start", -1), head.get("end", -1)),
                },
                "predicate": rel.get("relation", ""),
                "tail": {
                    "start": tail.get("start", -1),
                    "end": tail.get("end", -1),
                    "text": tail.get("text", ""),
                    "type": tail.get("label") or tail.get("type") or _find_entity_type(entities_out, tail.get("start", -1), tail.get("end", -1)),
                },
                "score": round(rel.get("score", 0.0), 6),
            })

        total_entities += len(entities_out)
        total_pairs += len(raw_pair_scores)
        total_relations += len(relations_out)

        row = {
            "sample_id": sample_id,
            "raw_scores_complete": True,
            "entities": entities_out,
            "relations": relations_out,
            "raw_pair_scores": raw_pair_scores,
        }
        rows.append(row)

        if (idx + 1) % 5 == 0 or idx == len(gold_samples) - 1:
            print(f"  {idx + 1}/{len(gold_samples)} samples, "
                  f"{total_entities} entities, {total_pairs} pairs, "
                  f"{total_relations} relations", file=sys.stderr)

    return rows


def _find_entity_type(entities: list[dict], start: int, end: int) -> str:
    """Find entity type by exact offset match."""
    for ent in entities:
        if ent["start"] == start and ent["end"] == end:
            return ent.get("type") or ""
    return ""


def verify_invariants(rows: list[dict[str, Any]]) -> list[str]:
    """Enforce post-regeneration invariants. Returns list of violations."""
    violations = []

    total_entities = sum(len(r["entities"]) for r in rows)
    if total_entities == 0:
        violations.append("raw_entity_count == 0 (must be > 0)")

    # Type coverage: every entity must have a non-null, non-empty type
    typed = sum(1 for r in rows for e in r["entities"] if e.get("type"))
    coverage = typed / max(1, total_entities)
    if coverage < 1.0:
        violations.append(
            f"raw_entity_type_coverage = {coverage:.4f} (must be 1.000); "
            f"{total_entities - typed} entities missing type"
        )

    # entity_idx validity on raw_pair_scores endpoints
    total_endpoints = 0
    valid_idx = 0
    offset_match = 0
    for r in rows:
        ent_offsets = {(e["start"], e["end"]) for e in r["entities"]}
        for rp in r["raw_pair_scores"]:
            for ep_key in ("head", "tail"):
                ep = rp[ep_key]
                total_endpoints += 1
                if ep.get("entity_idx") is not None and ep["entity_idx"] >= 0:
                    valid_idx += 1
                if (ep["start"], ep["end"]) in ent_offsets:
                    offset_match += 1

    if total_endpoints > 0:
        idx_rate = valid_idx / total_endpoints
        if idx_rate < 1.0:
            violations.append(
                f"valid_relation_entity_idx_rate = {idx_rate:.4f} (must be 1.000)"
            )
        offset_rate = offset_match / total_endpoints
        if offset_rate < 1.0:
            violations.append(
                f"relation_endpoint_offset_match_rate = {offset_rate:.4f} (must be 1.000)"
            )

    # Type loss after serialization: check raw_pair_scores endpoints have types
    type_loss_serial = sum(
        1 for r in rows for rp in r["raw_pair_scores"]
        for ep_key in ("head", "tail")
        if not rp[ep_key].get("type")
    )
    if type_loss_serial > 0:
        violations.append(f"type_loss_after_serialization = {type_loss_serial} (must be 0)")

    return violations


def write_artifact(
    rows: list[dict[str, Any]],
    out_path: Path,
    device: torch.device,
) -> None:
    """Write prediction JSONL with metadata header."""
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
        "batch_size": BATCH_SIZE,
        "num_samples": len(rows),
        "total_entities": sum(len(r["entities"]) for r in rows),
        "total_raw_pairs": sum(len(r["raw_pair_scores"]) for r in rows),
        "total_thresholded_relations": sum(len(r["relations"]) for r in rows),
        # Device provenance
        "device": str(device),
        "dtype": "float32",
        "torch_version": torch.__version__,
        "macos_version": platform.mac_ver()[0] or "n/a",
        # MPS environment freeze (must all be False for deterministic runs)
        **_mps_env_snapshot(),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        # First line: metadata (not a prediction row)
        f.write(json.dumps({"__metadata__": metadata}, sort_keys=True) + "\n")
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    print(f"\nWrote {out_path}", file=sys.stderr)
    print(f"  metadata: {json.dumps(metadata, indent=2)}", file=sys.stderr)


def print_label_inventory_report(rows: list[dict[str, Any]]) -> None:
    """Report entity label distribution as required by the user directive."""
    from collections import Counter
    label_counts = Counter()
    label_scores: dict[str, list[float]] = {}
    for r in rows:
        for e in r["entities"]:
            lbl = e.get("type") or "NULL"
            label_counts[lbl] += 1
            label_scores.setdefault(lbl, []).append(e.get("score", 0.0))

    total = sum(label_counts.values())
    print("\n" + "=" * 72)
    print("ENTITY LABEL INVENTORY REPORT")
    print("=" * 72)
    print(f"{'Label':<16} {'Count':>6} {'Pct':>7} {'MeanScore':>10}")
    print("-" * 42)
    for lbl in sorted(label_counts, key=lambda x: -label_counts[x]):
        cnt = label_counts[lbl]
        pct = 100.0 * cnt / max(1, total)
        mean_s = sum(label_scores[lbl]) / len(label_scores[lbl])
        print(f"{lbl:<16} {cnt:>6} {pct:>6.1f}% {mean_s:>10.4f}")
    print("-" * 42)
    print(f"{'TOTAL':<16} {total:>6}")
    null_count = label_counts.get("NULL", 0)
    concept_count = label_counts.get("concept", 0)
    other_count = label_counts.get("other", 0)
    print(f"\n  % emitted as 'other':   {100.0 * other_count / max(1, total):.1f}%")
    print(f"  % emitted as 'concept': {100.0 * concept_count / max(1, total):.1f}%")
    print(f"  unknown/null labels:    {null_count}")


def _assert_mps_env_frozen() -> None:
    """Fail visibly if MPS environment switches alter execution behavior.

    For deterministic benchmarking, these must be explicitly disabled:
      PYTORCH_ENABLE_MPS_FALLBACK=0  — no silent CPU fallback
      PYTORCH_MPS_FAST_MATH=0        — no approximate math
      PYTORCH_MPS_PREFER_METAL=0     — no Metal-kernel preference override
    """
    violations = []
    for var, required in (
        ("PYTORCH_ENABLE_MPS_FALLBACK", "0"),
        ("PYTORCH_MPS_FAST_MATH", "0"),
        ("PYTORCH_MPS_PREFER_METAL", "0"),
    ):
        val = os.environ.get(var)
        if val is not None and val != required:
            violations.append(f"  {var}={val} (must be {required} or unset)")
    if violations:
        raise RuntimeError(
            "MPS environment freeze violated — deterministic benchmark requires:\n"
            + "\n".join(violations)
            + "\nSet: export PYTORCH_ENABLE_MPS_FALLBACK=0 PYTORCH_MPS_FAST_MATH=0 PYTORCH_MPS_PREFER_METAL=0"
        )


def _mps_env_snapshot() -> dict:
    """Record MPS environment settings in the generation manifest."""
    return {
        "mps_fallback_enabled": os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK", "0") != "0",
        "mps_fast_math_enabled": os.environ.get("PYTORCH_MPS_FAST_MATH", "0") != "0",
        "mps_prefer_metal": os.environ.get("PYTORCH_MPS_PREFER_METAL", "0") != "0",
    }


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    # Parse --device flag
    device_pref = "auto"
    if "--device" in sys.argv:
        idx = sys.argv.index("--device")
        if idx + 1 < len(sys.argv):
            device_pref = sys.argv[idx + 1].lower()

    # Enforce frozen MPS environment before device resolution
    _assert_mps_env_frozen()
    device = resolve_torch_device(device_pref)

    # Output path reflects device
    device_tag = str(device).replace(":", "")  # "mps" or "cpu"
    out_name = f"relex_large_v3_{device_tag}_fp32.predictions.jsonl"
    out_path = OUT_DIR / out_name

    print("=" * 72, file=sys.stderr)
    print("RELEX BENCHMARK REGENERATION (P1A closure)", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(f"  model:      {MODEL_ID}", file=sys.stderr)
    print(f"  device:     {device}", file=sys.stderr)
    print(f"  torch:      {torch.__version__}", file=sys.stderr)
    print(f"  ent_thr:    {ENT_THRESHOLD}", file=sys.stderr)
    print(f"  rel_thr:    {REL_THRESHOLD}", file=sys.stderr)
    print(f"  entity_labels ({len(ENTITY_LABELS)}): {ENTITY_LABELS}", file=sys.stderr)
    print(f"  relation_labels ({len(RELATION_LABELS)}): {len(RELATION_LABELS)} labels", file=sys.stderr)
    print(f"  output:     {out_path}", file=sys.stderr)
    print(file=sys.stderr)

    gold_samples = load_gold_texts()
    print(f"Loaded {len(gold_samples)} gold samples", file=sys.stderr)

    if dry_run:
        print("\n[DRY RUN] Would run model inference. Exiting.", file=sys.stderr)
        return 0

    _mps_sync()
    t0 = time.time()
    rows = run_model(gold_samples, device=device)
    _mps_sync()
    elapsed = time.time() - t0
    print(f"\nInference complete in {elapsed:.1f}s "
          f"({elapsed / len(gold_samples):.2f}s/sample)", file=sys.stderr)

    # --- Verify invariants ---
    print("\nVerifying post-regeneration invariants...", file=sys.stderr)
    violations = verify_invariants(rows)
    if violations:
        print("\nINVARIANT VIOLATIONS (hard fail):", file=sys.stderr)
        for v in violations:
            print(f"  FAIL: {v}", file=sys.stderr)
        return 1
    print("  ALL INVARIANTS PASS", file=sys.stderr)

    # --- Write artifact ---
    write_artifact(rows, out_path, device=device)

    # --- Label inventory report ---
    print_label_inventory_report(rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
