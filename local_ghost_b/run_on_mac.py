"""
Mac entry point for the Polymath broke-mode local Ghost B cascade.

Wires the bundled head paths (heads/<name>) into LocalExtractor and runs either a
demo or a chunks file. On Apple Silicon, PyTorch uses the MPS backend if available.

Setup (Mac):
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt

Demo:
    python run_on_mac.py --demo

Process a chunks JSONL (each line: {chunk_id, doc_id, text, entities:[...]}):
    python run_on_mac.py --chunks my_chunks.jsonl --out local_relations.jsonl

Hybrid (cascade + Qwen resolver on ambiguous edges):
    python run_on_mac.py --hybrid --chunks my_chunks.jsonl --out local_relations.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

# These heads are encoder classifiers cached locally in this bundle; never hit HF.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import torch

from polymath_local_extractor import LocalExtractor

HERE = Path(__file__).resolve().parent


def pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def make_extractor() -> LocalExtractor:
    device = pick_device()
    print(f"[device] {device}", flush=True)
    return LocalExtractor(
        runs_dir=str(HERE / "heads"),
        backbone="backbone_v1", easy="easy_predicate_v1", family="family_v1",
        device=device,
    )


_GHOST_B_REL_FIELDS = {"t", "sub", "pred", "obj", "ok", "cf", "ev", "cue", "chunk_id", "doc_id"}


def _validate_record(rec: dict, valid_preds: set) -> Optional[str]:
    missing = _GHOST_B_REL_FIELDS - rec.keys()
    if missing:
        return f"missing {sorted(missing)}"
    if rec["t"] != "r":
        return f"bad t: {rec['t']}"
    if rec["pred"] not in valid_preds:
        return f"off-vocab pred: {rec['pred']}"
    if rec["ok"] not in ("entity", "literal"):
        return f"bad ok: {rec['ok']}"
    if not isinstance(rec["cf"], (int, float)):
        return f"cf not numeric: {type(rec['cf']).__name__}"
    return None


def _run_glirel_extract_chunk(args) -> None:
    """LOCAL_GHOST_B_CLASSIFIER=glirel path. Uses the canonical glirel_infer
    (sentence-windowed, batched). Bundle search order:
      1. models/glirel_ghost_b_v1/best/   (RTX training output, shipped)
      2. models/glirel_ghost_b_v1/        (landing dir parent)
      3. local_ghost_b/heads/glirel_ghost_b_v1/  (legacy bundle slot)
      4. jackboyla/glirel-large-v0        (zero-shot fallback)
    """
    import time
    from glirel_infer import GliRELClassifier, to_record

    repo_root = HERE.parent  # /Users/king/polymath_v3.3
    candidates = [
        repo_root / "models" / "glirel_ghost_b_v1" / "best",
        repo_root / "models" / "glirel_ghost_b_v1",
        HERE / "heads" / "glirel_ghost_b_v1",
    ]
    ckpt = None
    for c in candidates:
        # accept HF-style (config.json+safetensors) OR glirel-style
        # (glirel_config.json+pytorch_model.bin)
        if (c / "config.json").exists() or (c / "model.safetensors").exists() \
                or (c / "glirel_config.json").exists() or (c / "pytorch_model.bin").exists():
            ckpt = str(c)
            break
    if ckpt is None:
        ckpt = "jackboyla/glirel-large-v0"
        print(f"[glirel] no trained checkpoint found in {[str(c) for c in candidates]}", flush=True)
        print(f"[glirel] WARNING: falling back to zero-shot {ckpt}", flush=True)
    else:
        print(f"[glirel] loading checkpoint: {ckpt}", flush=True)

    # Load labels — prefer ones shipped with the ckpt; fall back to bundle slot
    labels_path = Path(ckpt) / "labels.json"
    if not labels_path.exists():
        labels_path = HERE / "heads" / "glirel_ghost_b_v1" / "labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    print(f"[glirel] {len(labels)} labels from {labels_path}", flush=True)

    thr = float(os.environ.get("LOCAL_GHOST_B_GLIREL_THRESHOLD") or 0.40)
    print(f"[glirel] threshold={thr}", flush=True)
    clf = GliRELClassifier(ckpt_dir=ckpt, labels=labels, device=pick_device(),
                           threshold=thr)
    max_related = int(os.environ.get("LOCAL_GHOST_B_MAX_RELATED_TO_PER_CHUNK") or 3)
    valid_preds = set(labels) | {"related_to"}

    from ghost_b_cascade_infer import iter_chunks
    n_chunks = n_written = n_typed = n_rejects = 0
    t0 = time.time()
    with open(args.out, "w", encoding="utf-8") as out_f:
        for chunk in iter_chunks(args.chunks, args.limit or None):
            n_chunks += 1
            for e in clf.extract_chunk(chunk, max_related=max_related):
                rec = to_record(e, chunk)
                err = _validate_record(rec, valid_preds)
                if err:
                    n_rejects += 1
                    continue
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_written += 1
                if e["pred"] != "related_to":
                    n_typed += 1
    dt = time.time() - t0
    pct = 100 * n_typed / max(n_written, 1)
    print(f"[done] chunks={n_chunks} relations={n_written} typed={n_typed} ({pct:.0f}%) "
          f"rejects={n_rejects} -> {args.out}")
    print(f"[perf] {n_chunks/dt:.2f} chunks/sec over {dt:.1f}s wall")


DEMO = [
    {"text": "Qdrant stores vectors in an HNSW index", "cue": "stores",
     "subject": "qdrant", "subject_type": "Software", "object": "vectors", "object_type": "Artifact"},
    {"text": "The retriever module is part of the RAG pipeline", "cue": "part of",
     "subject": "retriever module", "subject_type": "Software", "object": "rag pipeline", "object_type": "Concept"},
    {"text": "Flutter uses the Dart language", "cue": "uses",
     "subject": "flutter", "subject_type": "Software", "object": "dart", "object_type": "Software"},
    {"text": "Alice Chen works for Meta AI", "cue": "works for",
     "subject": "alice chen", "subject_type": "Person", "object": "meta ai", "object_type": "Organization"},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--chunks", default="")
    ap.add_argument("--out", default="local_relations.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--hybrid", action="store_true",
                    help="Use ModernBERT cascade + Qwen resolver on ambiguous edges.")
    ap.add_argument("--qwen_mlx", default="qwen_resolver_mlx",
                    help="MLX-converted Qwen dir (used with --hybrid if present).")
    ap.add_argument("--qwen_hf", default="qwen_resolver_merged",
                    help="PyTorch merged Qwen dir (fallback if MLX dir missing).")
    ap.add_argument("--classifier", choices=["cascade", "glirel", "ensemble"],
                    default=None,
                    help="Predicate classifier. Default cascade (or "
                         "LOCAL_GHOST_B_CLASSIFIER env var). Ignored with --hybrid.")
    ap.add_argument("--glirel-bundle", dest="glirel_bundle",
                    default="glirel_ghost_b_v1",
                    help="Subdir under heads/ for fine-tuned GLiREL. "
                         "If empty or missing, falls back to zero-shot model.")
    args = ap.parse_args()

    if args.hybrid:
        from qwen_resolver import HybridExtractor
        head_kw = dict(backbone="backbone_v1", easy="easy_predicate_v1", family="family_v1")
        if (HERE / args.qwen_mlx).exists():
            from qwen_resolver_mlx import QwenResolverMLX
            print(f"[qwen] MLX backend: {args.qwen_mlx}", flush=True)
            ex = HybridExtractor(runs_dir=str(HERE / "heads"),
                                 resolver=QwenResolverMLX(str(HERE / args.qwen_mlx)),
                                 device=pick_device(), **head_kw)
        else:
            print(f"[qwen] PyTorch backend: {args.qwen_hf} "
                  f"(convert to MLX for speed — see README_QWEN_CLAUDE.md)", flush=True)
            ex = HybridExtractor(runs_dir=str(HERE / "heads"),
                                 qwen_dir=str(HERE / args.qwen_hf),
                                 device=pick_device(), **head_kw)
        print("[config] hybrid (cascade + Qwen resolver)", flush=True)
    else:
        # ADDITIVE flag-gated switch per Phase 3 contract.
        # LOCAL_GHOST_B_CLASSIFIER ∈ {existing, glirel}; default = existing.
        # "existing" routes to the original cascade path (unchanged).
        # "glirel"  routes to the chunk-level extract_chunk path (bypasses
        # candidate_pairs + relation_exists gate per the new contract).
        # Legacy --classifier values (cascade, glirel, ensemble) still work
        # if explicitly passed.
        raw_env = (os.environ.get("LOCAL_GHOST_B_CLASSIFIER") or "").strip().lower()
        if args.classifier:
            classifier_name = args.classifier
        elif raw_env == "glirel":
            classifier_name = "glirel_extract_chunk"
        elif raw_env == "":
            # No env override → use pipeline_config DEFAULT_CLASSIFIER
            from pipeline_config import DEFAULT_CLASSIFIER
            classifier_name = ("glirel_extract_chunk" if DEFAULT_CLASSIFIER == "glirel"
                               else "cascade")
        elif raw_env in ("existing", "cascade"):
            classifier_name = "cascade"
        elif raw_env == "ensemble":
            classifier_name = "ensemble"
        else:
            classifier_name = "cascade"
        print(f"[classifier] {classifier_name} (env LOCAL_GHOST_B_CLASSIFIER={raw_env or 'unset'})", flush=True)

        # New per-chunk path: extract_chunk does its own pair gen + safety + cap.
        # Skips the cascade's candidate_pairs + relation_exists gate.
        if classifier_name == "glirel_extract_chunk":
            return _run_glirel_extract_chunk(args)

        cascade_ex = None
        glirel_ex = None
        if classifier_name in ("cascade", "ensemble"):
            cascade_ex = make_extractor()
            print(f"[config:cascade] {json.dumps(cascade_ex.config_summary())}", flush=True)
        if classifier_name in ("glirel", "ensemble"):
            from glirel_classifier import GliRELClassifier
            glirel_ex = GliRELClassifier(
                ckpt_dir=str(HERE / "heads" / args.glirel_bundle),
                device=pick_device(),
            )
            print(f"[config:glirel] {json.dumps(glirel_ex.config_summary())}", flush=True)

        # Unified extract dispatcher used by both demo and chunks paths.
        if classifier_name == "cascade":
            ex = cascade_ex
        elif classifier_name == "glirel":
            ex = glirel_ex
        else:  # ensemble
            from ensemble_classifier import EnsembleClassifier
            ex = EnsembleClassifier(cascade_ex, glirel_ex)
            print(f"[config:ensemble] {json.dumps(ex.config_summary())}", flush=True)

    if args.demo or not args.chunks:
        for e in ex.extract(DEMO):
            print(f"  {e.subject} --{e.predicate}--> {e.object}  "
                  f"[{e.tier}/{e.source}] conf={e.confidence}")
        return

    # delegate to the full gated inference pipeline (with direction resolution)
    from ghost_b_cascade_infer import (apply_related_cap, candidate_pairs,
                                       iter_chunks, resolve_directions,
                                       RelationExistsGate)
    max_related = int(os.environ.get("LOCAL_GHOST_B_MAX_RELATED_TO_PER_CHUNK") or 3)
    # learned pair-quality gate (auto-on if the model is bundled in heads/)
    gate = None
    gate_dir = HERE / "heads" / "relation_exists_v1"
    if gate_dir.exists():
        thr = float(os.environ.get("LOCAL_GHOST_B_RELEXIST_THRESHOLD") or 0.70)
        gate = RelationExistsGate(str(gate_dir), threshold=thr, device=pick_device())
        print(f"[gate] relation_exists ON (threshold={thr})", flush=True)
    n_chunks = n_written = 0
    with open(args.out, "w", encoding="utf-8") as out_f:
        for chunk in iter_chunks(args.chunks, args.limit or None):
            n_chunks += 1
            pairs = candidate_pairs(chunk)
            if not pairs:
                continue
            if gate is not None:
                pairs = gate.filter(pairs)
                if not pairs:
                    continue
            edges = ex.extract(pairs)
            # collapse reverse/duplicate-direction edges (candidate_pairs permutes)
            keep = resolve_directions(edges, pairs)
            edges = [edges[i] for i in keep]
            kept_pairs = [pairs[i] for i in keep]
            edges = apply_related_cap(edges, max_related)
            for e, p in zip(edges, kept_pairs):
                rec = LocalExtractor.to_ghost_b_record(e, p)
                if rec is None:
                    continue
                rec["chunk_id"] = chunk.get("chunk_id", "")
                rec["doc_id"] = chunk.get("doc_id", "")
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_written += 1
    print(f"[done] chunks={n_chunks} relations={n_written} -> {args.out}")


if __name__ == "__main__":
    main()
