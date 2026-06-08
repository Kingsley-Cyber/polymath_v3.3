"""Phase 4 latency baseline — base model on 50 real chunks.

Measures:
  - cold first-call (model just loaded, no inference yet)
  - warm first-call (after a pre-warm dummy forward)
  - sustained chunks/sec on 50 chunks
  - per-230-chunk-file projection
"""

import json
import sys
import time
from pathlib import Path

import torch
from glirel_infer import GliRELClassifier


def main():
    chunks_path = sys.argv[1] if len(sys.argv) > 1 else "prompting_chunks.jsonl"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    chunks = [json.loads(l) for l in open(chunks_path)][:n]
    # Skip chunks with <2 entities (no pairs possible — would skew average)
    chunks = [c for c in chunks if len(c.get("entities") or []) >= 2]
    print(f"chunks loaded for benchmark: {len(chunks)} (after filtering single-entity chunks)")

    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"device: {dev}, torch: {torch.__version__}")

    t = time.time()
    clf = GliRELClassifier(ckpt="jackboyla/glirel-large-v0", threshold=0.5, device=dev)
    load_s = time.time() - t
    print(f"\nmodel load: {load_s:.1f}s")

    # Cold first-call: time the very first extract_chunk
    t = time.time()
    _ = clf.extract_chunk(chunks[0], max_related=3)
    cold_ms = (time.time() - t) * 1000
    print(f"COLD first-call latency: {cold_ms:.0f}ms  (chunk[0]: {len(chunks[0]['entities'])} ents)")

    # Pre-warm: a dummy forward (already happened above, but do one more on a tiny chunk)
    if len(chunks) > 1:
        _ = clf.extract_chunk(chunks[1], max_related=3)

    # Warm first-call: time a fresh chunk after pre-warm
    if len(chunks) > 2:
        t = time.time()
        _ = clf.extract_chunk(chunks[2], max_related=3)
        warm_first_ms = (time.time() - t) * 1000
        print(f"WARM first-call latency: {warm_first_ms:.0f}ms  (chunk[2]: {len(chunks[2]['entities'])} ents)")

    # Sustained: time 50 chunks (or however many are available)
    t = time.time()
    total_rels = 0
    for c in chunks:
        total_rels += len(clf.extract_chunk(c, max_related=3))
    sustained_s = time.time() - t

    cps = len(chunks) / sustained_s
    ms_per_chunk = sustained_s * 1000 / len(chunks)
    print(f"\nSUSTAINED over {len(chunks)} chunks:")
    print(f"  total wall: {sustained_s:.1f}s")
    print(f"  avg per chunk: {ms_per_chunk:.0f}ms")
    print(f"  throughput: {cps:.2f} chunks/sec on {dev}")
    print(f"  total relations: {total_rels}")
    print(f"\nPROJECTION:")
    for size in (100, 230, 500, 1000):
        proj = size / cps
        print(f"  {size:>4} chunks  -> {proj:.0f}s = {proj/60:.1f} min")


if __name__ == "__main__":
    main()
