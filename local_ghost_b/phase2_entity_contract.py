"""Phase 2: validate the GLiNER -> GLiREL entity contract on a real chunk.

Checks:
  1. surface_form locates VERBATIM in the chunk text via the regex tokenizer
     used in validate_glirel_env.py
  2. entity_type is one of the 15 Ghost B types (report exact mapping if not)
  3. predict_relations produces typed relations without errors
"""

import json
import re
import sys
import torch
from glirel import GLiREL

# 15 Ghost B entity types per the contract
GHOST_B_TYPES_15 = {
    "Person", "Organization", "Location", "Event", "Concept", "Method",
    "Product", "Software", "Document", "Standard", "Rule", "Law",
    "Artifact", "TimeReference", "other",
}

# Same 30 labels + same tokenizer as validate_glirel_env.py — the contract.
LABELS = [
    "part_of","member_of","located_in","works_for","created_by","owns",
    "affiliated_with","synonym_of","instance_of","example_of","uses","references",
    "implements","depends_on","produces","stores","detects","supports","defines",
    "represents","maps_to","preceded_by","causes","overlaps","during","derived_from",
    "contradicts","excepts","overrides","related_to",
]
TOK = re.compile(r"\w+(?:[-_]\w+)*|\S")


def locate(tokens_lower, name):
    w = [x.lower() for x in TOK.findall(name)]
    n = len(w)
    for i in range(len(tokens_lower) - n + 1):
        if tokens_lower[i:i + n] == w:
            return [i, i + n - 1]
    return None


def main():
    chunks_path = sys.argv[1] if len(sys.argv) > 1 else "vectordb_chunks.jsonl"
    chunk_idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    with open(chunks_path) as f:
        chunks = [json.loads(l) for l in f if l.strip()]
    chunk = chunks[chunk_idx]
    text = chunk["text"]
    entities = chunk.get("entities") or []

    print(f"=== chunk_id: {chunk['chunk_id']}")
    print(f"=== text len: {len(text)} chars, {len(entities)} entities")
    print()

    tokens = TOK.findall(text)
    tl = [x.lower() for x in tokens]
    print(f"=== regex-tokenized text: {len(tokens)} tokens (first 20: {tokens[:20]})")
    print()

    # Check 1: surface_form locate-rate
    located, missing = [], []
    for ent in entities:
        surf = ent.get("surface_form", "")
        verbatim_in_text = surf in text
        span = locate(tl, surf)
        if span is None:
            missing.append((surf, ent.get("entity_type"), verbatim_in_text))
        else:
            located.append((surf, ent.get("entity_type"), span))

    n_total = len(entities)
    n_located = len(located)
    pct = 100 * n_located / max(n_total, 1)
    print(f"=== CHECK 1: surface_form locate-rate")
    print(f"    located: {n_located}/{n_total}  ({pct:.0f}%)")
    if missing:
        print(f"    MISSING entities (these will be silently dropped by extract_chunk):")
        for surf, etype, in_text in missing:
            print(f"      \"{surf}\" type={etype}  in_text_substring={in_text}")
    print()

    # Check 2: entity_type in 15 Ghost B types
    print(f"=== CHECK 2: entity_type vs 15 Ghost B types")
    type_counts = {}
    for ent in entities:
        t = ent.get("entity_type") or "<none>"
        type_counts[t] = type_counts.get(t, 0) + 1
    off_vocab = {t: c for t, c in type_counts.items() if t not in GHOST_B_TYPES_15}
    print(f"    type distribution: {type_counts}")
    if off_vocab:
        print(f"    OFF-VOCAB TYPES (mapping needed): {off_vocab}")
        print(f"    Ghost B 15 vocabulary: {sorted(GHOST_B_TYPES_15)}")
    else:
        print(f"    all entity_types ∈ 15 Ghost B types ✓")
    print()

    # Check 3: predict_relations end-to-end
    if n_located < 2:
        print(f"=== CHECK 3: SKIPPED (fewer than 2 entities located, no pairs possible)")
        return
    print(f"=== CHECK 3: predict_relations on located entities")
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"    device: {dev}")
    m = GLiREL.from_pretrained("jackboyla/glirel-large-v0")
    m.to(dev)
    m.device = torch.device(dev)
    m.config.fixed_relation_types = True
    ner = [[sp[0], sp[1], etype] for surf, etype, sp in located]
    try:
        out = m.predict_relations(tokens, LABELS, ner=ner, threshold=0.5, top_k=1)
    except Exception as e:
        print(f"    ERROR: {type(e).__name__}: {e}")
        return
    typed = [r for r in out if r.get("label") and r["label"] != "no_relation"]
    print(f"    relations returned: {len(out)}  (typed: {len(typed)})")
    print(f"    sample (top 6 by score):")
    for r in sorted(out, key=lambda x: -x.get("score", 0))[:6]:
        print(f"      {r['head_text']} --{r['label']:>13s}--> {r['tail_text']}  cf={r['score']:.2f}")


if __name__ == "__main__":
    main()
