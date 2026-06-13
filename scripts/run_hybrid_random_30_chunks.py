#!/usr/bin/env python3
"""
Run a deterministic 30-random-chunk smoke test for the local Ghost B hybrid path:

    raw Ghost B chunk rows -> gated candidate pairs -> ModernBERT cascade
    -> Qwen MLX fallback for ambiguous pairs -> Ghost B-compatible JSONL

This is a test harness, not production integration. It intentionally samples only
rows with enough text/entities to exercise the relation path.
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import random
import sys
import time
from pathlib import Path
from typing import Iterable, List


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def iter_rows(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def good_chunk(row: dict, min_entities: int, min_text_chars: int) -> bool:
    text = str(row.get("text") or "")
    entities = row.get("entities") or []
    if len(text) < min_text_chars or len(entities) < min_entities:
        return False
    names = set()
    for ent in entities:
        sf = str(ent.get("surface_form") or ent.get("canonical_name") or "").strip().lower()
        if sf:
            names.add(sf)
    return len(names) >= min_entities


def reservoir_sample(path: Path, n: int, seed: int, min_entities: int, min_text_chars: int) -> tuple[List[dict], int]:
    rng = random.Random(seed)
    sample: List[dict] = []
    eligible = 0
    for row in iter_rows(path):
        if not good_chunk(row, min_entities=min_entities, min_text_chars=min_text_chars):
            continue
        eligible += 1
        if len(sample) < n:
            sample.append(row)
        else:
            j = rng.randrange(eligible)
            if j < n:
                sample[j] = row
    return sample, eligible


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, help="SSD copy of polymath_local_ghost_b_v1_CLAUDE")
    ap.add_argument("--raw", required=True, help="raw Ghost B extractions JSONL")
    ap.add_argument("--qwen", required=True, help="local SSD MLX Qwen resolver path")
    ap.add_argument("--out-dir", default="data_eval/hybrid_random_30")
    ap.add_argument("--seed", type=int, default=20260606)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--min-entities", type=int, default=3)
    ap.add_argument("--min-text-chars", type=int, default=180)
    ap.add_argument("--max-related", type=int, default=3)
    ap.add_argument("--device", default="mps")
    args = ap.parse_args()

    bundle = Path(args.bundle).resolve()
    raw = Path(args.raw).resolve()
    qwen_path = Path(args.qwen).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(bundle))
    ghost_infer = load_module("ghost_b_cascade_infer", bundle / "ghost_b_cascade_infer.py")
    local_extractor = load_module("polymath_local_extractor", bundle / "polymath_local_extractor.py")
    qwen_resolver = load_module("qwen_resolver", bundle / "qwen_resolver.py")
    qwen_resolver_mlx = load_module("qwen_resolver_mlx", bundle / "qwen_resolver_mlx.py")

    chunks, eligible = reservoir_sample(
        raw,
        n=args.n,
        seed=args.seed,
        min_entities=args.min_entities,
        min_text_chars=args.min_text_chars,
    )
    if len(chunks) < args.n:
        raise SystemExit(f"Only found {len(chunks)} eligible chunks; wanted {args.n}")

    sample_path = out_dir / "sample_chunks.jsonl"
    sample_path.write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks),
        encoding="utf-8",
    )

    t_load = time.perf_counter()
    resolver = qwen_resolver_mlx.QwenResolverMLX(str(qwen_path))
    hybrid = qwen_resolver.HybridExtractor(
        runs_dir=str(bundle / "heads"),
        resolver=resolver,
        device=args.device,
        backbone="backbone_v1",
        easy="easy_predicate_v1",
        family="family_v1",
    )
    load_s = time.perf_counter() - t_load

    out_jsonl = out_dir / "hybrid_relations.jsonl"
    records = []
    tier_counts = collections.Counter()
    pred_counts = collections.Counter()
    source_counts = collections.Counter()
    chunk_rows = []
    qwen_resolved = 0
    pairs_total = 0
    written_total = 0
    chunks_with_written = 0
    chunks_with_qwen = 0

    t0 = time.perf_counter()
    for chunk in chunks:
        pairs = ghost_infer.candidate_pairs(chunk)
        pairs_total += len(pairs)
        edges = hybrid.extract(pairs) if pairs else []
        edges = ghost_infer.apply_related_cap(edges, args.max_related)

        chunk_written = 0
        chunk_qwen = 0
        for edge, pair in zip(edges, pairs):
            tier_counts[edge.tier] += 1
            source_counts[edge.source] += 1
            if edge.tier == "qwen_resolved":
                qwen_resolved += 1
                chunk_qwen += 1
            rec = local_extractor.LocalExtractor.to_ghost_b_record(edge, pair)
            if rec is None:
                continue
            rec["chunk_id"] = chunk.get("chunk_id", "")
            rec["doc_id"] = chunk.get("doc_id", "")
            records.append(rec)
            pred_counts[rec["pred"]] += 1
            chunk_written += 1
        if chunk_written:
            chunks_with_written += 1
        if chunk_qwen:
            chunks_with_qwen += 1
        written_total += chunk_written
        chunk_rows.append(
            {
                "chunk_id": chunk.get("chunk_id", ""),
                "doc_id": chunk.get("doc_id", ""),
                "text_chars": len(str(chunk.get("text") or "")),
                "entities": len(chunk.get("entities") or []),
                "ghost_b_relations_existing": len(chunk.get("relations") or []),
                "candidate_pairs": len(pairs),
                "written_edges": chunk_written,
                "qwen_resolved_edges": chunk_qwen,
            }
        )

    infer_s = time.perf_counter() - t0
    out_jsonl.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )

    report = {
        "schema": "hybrid_random_30_chunks_v1",
        "seed": args.seed,
        "n_requested": args.n,
        "eligible_chunks_seen": eligible,
        "sample_chunks_path": str(sample_path),
        "output_jsonl": str(out_jsonl),
        "bundle": str(bundle),
        "qwen_path": str(qwen_path),
        "load_s": load_s,
        "infer_s": infer_s,
        "chunks": len(chunks),
        "pairs": pairs_total,
        "written_edges": written_total,
        "qwen_resolved_edges": qwen_resolved,
        "chunks_with_written_edges": chunks_with_written,
        "chunks_with_qwen_resolved": chunks_with_qwen,
        "chunks_per_s_after_load": len(chunks) / infer_s if infer_s else 0,
        "pairs_per_s_after_load": pairs_total / infer_s if infer_s else 0,
        "written_edges_per_chunk": written_total / max(len(chunks), 1),
        "qwen_resolved_per_chunk": qwen_resolved / max(len(chunks), 1),
        "tier_counts": dict(tier_counts),
        "predicate_distribution": dict(pred_counts),
        "source_counts": dict(source_counts),
        "chunk_rows": chunk_rows,
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    md = out_dir / "report.md"
    md.write_text(
        "\n".join(
            [
                "# Hybrid Random 30 Chunk Test",
                "",
                f"- seed: `{args.seed}`",
                f"- chunks: `{len(chunks)}` from `{eligible}` eligible rows",
                f"- load_s: `{load_s:.3f}`",
                f"- infer_s: `{infer_s:.3f}`",
                f"- chunks/sec after load: `{report['chunks_per_s_after_load']:.2f}`",
                f"- candidate pairs: `{pairs_total}`",
                f"- written edges: `{written_total}`",
                f"- qwen resolved edges: `{qwen_resolved}`",
                f"- chunks with written edges: `{chunks_with_written}/{len(chunks)}`",
                f"- chunks with qwen fallback: `{chunks_with_qwen}/{len(chunks)}`",
                "",
                "## Tier Counts",
                "```json",
                json.dumps(dict(tier_counts), indent=2),
                "```",
                "",
                "## Predicate Distribution",
                "```json",
                json.dumps(dict(pred_counts), indent=2),
                "```",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
