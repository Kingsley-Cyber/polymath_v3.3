"""Run chunk+GLiNER+gated-extract pipeline over multiple markdown files.

Loads GLiNER and LocalExtractor ONCE, reuses across files. Writes per-file
chunks + relations JSONL into outdir/, then prints an aggregate summary.

Usage:
    python tools/batch_pipeline.py \\
        --outdir batch_out \\
        --threshold 0.45 \\
        /Volumes/Flash\\ Drive/merged/foo.md /Volumes/Flash\\ Drive/merged/bar.md
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from tools.chunk_with_gliner import (  # type: ignore
    GHOST_B_TYPES, chunk_markdown, dedupe_entities,
)


def stable_id(name: str, head: str) -> str:
    import hashlib
    return hashlib.sha256((name + head).encode("utf-8")).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="batch_out")
    ap.add_argument("--threshold", type=float, default=0.45)
    ap.add_argument("--gliner-model", default="urchade/gliner_medium-v2.1")
    ap.add_argument("--gate-threshold", type=float, default=0.70)
    ap.add_argument("--target-chars", type=int, default=400)
    ap.add_argument("--max-related", type=int, default=3)
    ap.add_argument("files", nargs="+", help="markdown files to process")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # Load GLiNER FIRST (must allow online fetch on first run to populate cache).
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    print(f"[load] gliner {args.gliner_model} ...", flush=True)
    from gliner import GLiNER
    gliner_model = GLiNER.from_pretrained(args.gliner_model)
    print(f"[load] gliner ready (labels={len(GHOST_B_TYPES)})", flush=True)

    # Now lock to offline for the bundled extractor heads.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    import torch
    from polymath_local_extractor import LocalExtractor
    from ghost_b_cascade_infer import (
        RelationExistsGate, apply_related_cap, candidate_pairs,
        iter_chunks, resolve_directions,
    )

    device = ("mps" if (getattr(torch.backends, "mps", None)
                        and torch.backends.mps.is_available())
              else "cpu")
    print(f"[device] {device}", flush=True)
    ex = LocalExtractor(
        runs_dir=str(HERE / "heads"),
        backbone="backbone_v1", easy="easy_predicate_v1", family="family_v1",
        device=device,
    )
    gate = RelationExistsGate(str(HERE / "heads" / "relation_exists_v1"),
                              threshold=args.gate_threshold, device=device)
    print(f"[gate] relation_exists ON (threshold={args.gate_threshold})", flush=True)

    per_file = []

    for path_str in args.files:
        path = Path(path_str)
        if not path.exists():
            print(f"  [skip] missing: {path}", flush=True)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks_text = chunk_markdown(text, target_chars=args.target_chars, min_chars=150)
        doc_id = stable_id(path.name, text[:64])

        chunks_path = outdir / f"{path.stem}_chunks.jsonl"
        rels_path = outdir / f"{path.stem}_relations.jsonl"

        ent_count = 0
        with chunks_path.open("w", encoding="utf-8") as f:
            for i, ctext in enumerate(chunks_text):
                raw = gliner_model.predict_entities(
                    ctext, GHOST_B_TYPES, threshold=args.threshold)
                ents = dedupe_entities(raw)
                ent_count += len(ents)
                row = {
                    "chunk_id": f"{doc_id}_{i:04d}",
                    "doc_id": doc_id,
                    "text": ctext,
                    "entities": ents,
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        n_chunks = n_written = 0
        rels_for_file: list[dict] = []
        with rels_path.open("w", encoding="utf-8") as out_f:
            for chunk in iter_chunks(str(chunks_path), None):
                n_chunks += 1
                pairs = candidate_pairs(chunk)
                if not pairs:
                    continue
                pairs = gate.filter(pairs)
                if not pairs:
                    continue
                edges = ex.extract(pairs)
                keep = resolve_directions(edges, pairs)
                edges = [edges[i] for i in keep]
                kept_pairs = [pairs[i] for i in keep]
                edges = apply_related_cap(edges, args.max_related)
                for e, p in zip(edges, kept_pairs):
                    rec = LocalExtractor.to_ghost_b_record(e, p)
                    if rec is None:
                        continue
                    rec["chunk_id"] = chunk.get("chunk_id", "")
                    rec["doc_id"] = chunk.get("doc_id", "")
                    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    rels_for_file.append(rec)
                    n_written += 1

        preds = collections.Counter(r["pred"] for r in rels_for_file)
        typed = sum(c for p, c in preds.items() if p != "related_to")
        per_file.append({
            "file": path.name,
            "chars": len(text),
            "chunks": n_chunks,
            "entities": ent_count,
            "relations": n_written,
            "typed": typed,
            "related_to": preds.get("related_to", 0),
            "preds": dict(preds),
        })
        print(f"  [done] {path.name}: chunks={n_chunks} ents={ent_count} "
              f"rels={n_written} (typed={typed}, related_to={preds.get('related_to',0)})",
              flush=True)

    print()
    print("=" * 72)
    print("AGGREGATE")
    print("=" * 72)
    total_chunks = sum(p["chunks"] for p in per_file)
    total_rels = sum(p["relations"] for p in per_file)
    total_typed = sum(p["typed"] for p in per_file)
    total_rel_fallback = sum(p["related_to"] for p in per_file)
    print(f"files={len(per_file)} chunks={total_chunks} relations={total_rels} "
          f"(typed={total_typed}, related_to={total_rel_fallback})")
    if total_rels:
        print(f"typed%={100*total_typed/total_rels:.0f}%  "
              f"related_to%={100*total_rel_fallback/total_rels:.0f}%  "
              f"rels/chunk={total_rels/max(total_chunks,1):.2f}")
    all_preds = collections.Counter()
    for p in per_file:
        all_preds.update(p["preds"])
    print(f"\npredicate distribution: {dict(all_preds)}")
    print()
    print(f"{'file':<55} {'chnk':>4} {'ent':>4} {'rel':>4} {'typ':>3}")
    for p in per_file:
        print(f"{p['file'][:55]:<55} {p['chunks']:>4} {p['entities']:>4} "
              f"{p['relations']:>4} {p['typed']:>3}")


if __name__ == "__main__":
    main()
