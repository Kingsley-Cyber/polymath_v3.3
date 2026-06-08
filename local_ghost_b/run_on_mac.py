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
        ex = make_extractor()
        print(f"[config] {json.dumps(ex.config_summary())}", flush=True)

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
