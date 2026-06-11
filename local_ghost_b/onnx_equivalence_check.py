"""onnx_equivalence_check.py — torch-vs-ONNX quality gate for the GLiNER lane.

The ONNX Runtime lane (pipeline_config.GLINER_ONNX, env GHOST_B_GLINER_ONNX=1)
swaps the GLiNER forward used by BOTH extraction passes. Before any ONNX
variant (fp32 on a new box, fp16, int8) is allowed into production, its full
pipeline output must be equivalent to the torch lane on the same chunks.
This script is that gate. It runs the REAL pipeline (_extract_raw — GLiNER
pass-1 + facets + GLiREL + fact rules), not an isolated model probe, so any
wrapper/API drift in gliner's ONNX class fails loudly here instead of
mid-ingestion.

Usage (two dumps, lane picked by env exactly as in production, then compare):

    GHOST_B_GLINER_ONNX=0 python onnx_equivalence_check.py dump --out torch.json
    GHOST_B_GLINER_ONNX=1 python onnx_equivalence_check.py dump --out onnx.json
    python onnx_equivalence_check.py compare torch.json onnx.json

`dump --payload chunks.json` feeds real chunks (JSON list of dicts with at
least "text"; chunk_id/doc_id/corpus_id are filled if absent). Without
--payload, a small embedded cross-domain set is used — enough for a smoke
gate on a fresh box with no Mongo access (the RTX sidecar host).

Gates (compare exits 1 on failure):
    entity Jaccard  >= --min-ent-jaccard  (default 0.95)  on (name, type)
    relation Jaccard>= --min-rel-jaccard  (default 0.90)  on (subj, pred, obj)
    facet agreement >= --min-facet-agree  (default 0.95)  on matched entities
Confidence drift on matched entities is REPORTED (warn over --warn-conf-delta,
default 0.02) but does not gate: fp32 ONNX sits ~1e-4 from torch; fp16 a bit
wider. Facts are compared informationally — they are deterministic Python over
text + entities, so any diff there is entity drift surfacing downstream.

TIP (download size): gliner snapshot_downloads the WHOLE repo — for
onnx-community/gliner_medium-v2.1 that is every quantized variant (~3 GB+).
Pre-download selectively and point GHOST_B_GLINER_ONNX_REPO at the dir:

    huggingface-cli download onnx-community/gliner_medium-v2.1 \
        --include "*.json" "spm.model" "onnx/model.onnx" \
        --local-dir <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Mirror the sidecar's path setup: backend/ importable, local_ghost_b on path.
_REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (_REPO_ROOT / "backend", Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Embedded cross-domain fallback chunks (~128-token scale, like production
# children). Diverse on purpose: tech, business prose, psych, history —
# the domains the pilot graded.
_FALLBACK_TEXTS = [
    "Qdrant is an open-source vector database written in Rust. It supports "
    "named vectors, payload filtering, and HNSW indexing. Unlike Pinecone, "
    "which is a managed service, Qdrant can run self-hosted in Docker.",
    "The Mom Test, written by Rob Fitzpatrick, argues that customer "
    "interviews fail when founders pitch instead of listening. Good "
    "questions ask about the customer's past behavior, not hypothetical "
    "futures.",
    "Cronbach's alpha measures internal consistency reliability. Values "
    "above 0.7 are conventionally acceptable, though alpha depends on test "
    "length — a 40-item scale inflates alpha relative to a 10-item scale.",
    "Flame is a game engine built on Flutter. It provides a component "
    "system, sprite rendering, and collision detection, and it targets iOS, "
    "Android, and the web from a single Dart codebase.",
    "The Treaty of Westphalia in 1648 ended the Thirty Years' War and "
    "established the principle of state sovereignty that still anchors "
    "international law.",
    "Working capital equals current assets minus current liabilities. A "
    "negative working capital position forced the company to renegotiate "
    "its revolving credit facility in March.",
    "GLiNER performs zero-shot named entity recognition by matching span "
    "representations against label embeddings, using a DeBERTa-v3 backbone. "
    "It was introduced by Urchade Zaratiana in 2023.",
    "Cognitive behavioral therapy treats anxiety disorders by restructuring "
    "maladaptive thought patterns. A typical course runs 12 to 20 sessions, "
    "and exposure exercises are assigned between sessions.",
]


def _load_tasks(payload: str | None) -> list[dict]:
    if payload:
        items = json.loads(Path(payload).read_text())
    else:
        items = [{"text": t} for t in _FALLBACK_TEXTS]
    tasks = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not (it.get("text") or "").strip():
            continue
        tasks.append({
            "chunk_id": it.get("chunk_id") or f"eqcheck_{i:04d}",
            "doc_id": it.get("doc_id") or "eqcheck_doc",
            "corpus_id": it.get("corpus_id") or "eqcheck",
            "text": it["text"],
            "chunk_kind": it.get("chunk_kind") or "body",
            "columns": it.get("columns") or [],
        })
    if not tasks:
        sys.exit("payload contained no usable chunks")
    return tasks


def cmd_dump(args: argparse.Namespace) -> None:
    tasks = _load_tasks(args.payload)
    from services.ghost_b_local import _extract_raw, LAST_TIMINGS  # noqa: PLC0415
    from services.ingestion.facet_tagger import gliner_backend_info  # noqa: PLC0415

    t0 = time.perf_counter()
    raw = _extract_raw(tasks, True, None)
    elapsed = time.perf_counter() - t0
    lane = gliner_backend_info()
    out = {
        "lane": lane,
        "n_chunks": len(tasks),
        "elapsed_s": round(elapsed, 3),
        "ms_per_chunk": round(1000.0 * elapsed / max(1, len(tasks)), 1),
        "timings": dict(LAST_TIMINGS),
        "results": raw,
    }
    Path(args.out).write_text(json.dumps(out, indent=1, default=str))
    print(f"lane={lane.get('backend')} providers={lane.get('providers')} "
          f"device={lane.get('device')}")
    print(f"{len(tasks)} chunks in {elapsed:.1f}s "
          f"({out['ms_per_chunk']} ms/chunk, includes model load on cold run) "
          f"-> {args.out}")


# ------------------------------------------------------------------ compare
def _ent_key(e: dict) -> tuple:
    return ((e.get("canonical_name") or "").strip().lower(),
            e.get("entity_type") or "")


def _rel_key(r: dict) -> tuple:
    return ((r.get("subject") or "").strip().lower(),
            r.get("predicate") or "",
            (r.get("object") or "").strip().lower())


def _fact_key(f: dict) -> tuple:
    return ((f.get("subject") or "").strip().lower(),
            f.get("fact_type") or "",
            str(f.get("value") or "").strip().lower())


def _jaccard(a: set, b: set) -> float:
    return 1.0 if not a and not b else len(a & b) / max(1, len(a | b))


def cmd_compare(args: argparse.Namespace) -> None:
    da = json.loads(Path(args.a).read_text())
    db = json.loads(Path(args.b).read_text())
    ra = {r["chunk_id"]: r for r in da["results"]}
    rb = {r["chunk_id"]: r for r in db["results"]}
    if set(ra) != set(rb):
        sys.exit(f"FAIL: chunk_id sets differ (a-only={set(ra)-set(rb)}, "
                 f"b-only={set(rb)-set(ra)}) — dumps are not the same payload")

    ents_a, ents_b = set(), set()
    rels_a, rels_b = set(), set()
    facts_a, facts_b = set(), set()
    conf_deltas: list[float] = []
    facet_match = facet_total = 0
    per_chunk_diffs: list[str] = []

    for cid in sorted(ra):
        ea = {_ent_key(e): e for e in (ra[cid].get("entities") or [])}
        eb = {_ent_key(e): e for e in (rb[cid].get("entities") or [])}
        ents_a |= {(cid, *k) for k in ea}
        ents_b |= {(cid, *k) for k in eb}
        for k in set(ea) & set(eb):
            conf_deltas.append(abs(float(ea[k].get("confidence") or 0)
                                   - float(eb[k].get("confidence") or 0)))
            facet_total += 1
            if (ea[k].get("object_kind") or "") == (eb[k].get("object_kind") or ""):
                facet_match += 1
        only_a, only_b = set(ea) - set(eb), set(eb) - set(ea)
        if only_a or only_b:
            per_chunk_diffs.append(
                f"  {cid}: a-only={sorted(only_a)} b-only={sorted(only_b)}")
        rels_a |= {(cid, *_rel_key(r)) for r in (ra[cid].get("relations") or [])}
        rels_b |= {(cid, *_rel_key(r)) for r in (rb[cid].get("relations") or [])}
        facts_a |= {(cid, *_fact_key(f)) for f in (ra[cid].get("facts") or [])}
        facts_b |= {(cid, *_fact_key(f)) for f in (rb[cid].get("facts") or [])}

    ent_j = _jaccard(ents_a, ents_b)
    rel_j = _jaccard(rels_a, rels_b)
    fact_j = _jaccard(facts_a, facts_b)
    facet_agree = facet_match / facet_total if facet_total else 1.0
    max_conf = max(conf_deltas, default=0.0)

    print(f"lanes: a={da['lane'].get('backend')} ({da.get('ms_per_chunk')} ms/chunk)  "
          f"b={db['lane'].get('backend')} ({db.get('ms_per_chunk')} ms/chunk)")
    print(f"entities : jaccard {ent_j:.4f}  ({len(ents_a)} vs {len(ents_b)})")
    print(f"relations: jaccard {rel_j:.4f}  ({len(rels_a)} vs {len(rels_b)})")
    print(f"facts    : jaccard {fact_j:.4f}  ({len(facts_a)} vs {len(facts_b)})")
    print(f"facets   : agreement {facet_agree:.4f} on {facet_total} matched entities")
    print(f"confidence: max |delta| {max_conf:.5f} over {len(conf_deltas)} matched")
    if per_chunk_diffs:
        print("entity diffs by chunk:")
        print("\n".join(per_chunk_diffs))
    if max_conf > args.warn_conf_delta:
        print(f"WARN: confidence drift {max_conf:.5f} exceeds "
              f"{args.warn_conf_delta} — expected for quantized variants only")

    ok = (ent_j >= args.min_ent_jaccard
          and rel_j >= args.min_rel_jaccard
          and facet_agree >= args.min_facet_agree)
    print("RESULT:", "PASS" if ok else
          f"FAIL (gates: ent>={args.min_ent_jaccard} rel>={args.min_rel_jaccard} "
          f"facet>={args.min_facet_agree})")
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="run the pipeline in the env-selected lane")
    d.add_argument("--out", required=True)
    d.add_argument("--payload", default=None,
                   help="JSON list of chunk dicts with 'text' (default: embedded set)")
    d.set_defaults(fn=cmd_dump)
    c = sub.add_parser("compare", help="tolerance-diff two dumps")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--min-ent-jaccard", type=float, default=0.95)
    c.add_argument("--min-rel-jaccard", type=float, default=0.90)
    c.add_argument("--min-facet-agree", type=float, default=0.95)
    c.add_argument("--warn-conf-delta", type=float, default=0.02)
    c.set_defaults(fn=cmd_compare)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
