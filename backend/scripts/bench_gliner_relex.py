#!/usr/bin/env python3
"""Head-to-head: GLiNER-Relex vs the in-repo frame extractor.

SAME 30 chunks. SAME blind gold. SAME matcher function, copied verbatim from
backend/scripts/relation_stage_trace.py so the comparison cannot drift.

Scored two ways, because they answer different questions:
  PAIR-ONLY      did it find the (subject, object) link at all?
                 This is the number comparable to my generator's 0.229.
  PAIR+PREDICATE did it also name the relation the same way?
                 Strictly harder, and sensitive to vocabulary wording.
"""
from __future__ import annotations

import difflib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent

# --- matcher: byte-for-byte the same rule the repo instrument uses ----------
def make_matcher(strict: bool):
    def norm(s):
        return " ".join(str(s).lower().split()).strip(".,;:'\"()[]")

    def near(a, b):
        a, b = norm(a), norm(b)
        if not a or not b:
            return False
        if a == b or a in b or b in a:
            return True
        if strict:
            return False
        return difflib.SequenceMatcher(None, a, b).ratio() > 0.85
    return near


near = make_matcher(strict=False)
near_strict = make_matcher(strict=True)

# v2 entity vocabulary — the one that measured best in the A/B.
ENTITY_LABELS = ["person", "organization", "location", "product", "software",
                 "document", "method", "concept", "event", "standard", "artifact"]

# Union of the gold's predicates and ontology.yaml's, in the natural-language
# form GLiNER-Relex expects. Wording matters to a zero-shot model, so this is
# deliberately plain English rather than the snake_case internal names.
REL_LABEL_TO_PRED = {
    "affiliated with": "affiliated_with", "works for": "works_for",
    "created by": "created_by", "owns": "owns", "part of": "part_of",
    "located in": "located_in", "causes": "causes", "detects": "detects",
    "uses": "uses", "produces": "produces", "derived from": "derived_from",
    "is an instance of": "instance_of", "synonym of": "synonym_of",
    "includes": "includes", "supports": "supports", "implements": "implements",
    "has part": "has_part", "references": "references",
    "depends on": "depends_on", "member of": "member_of",
    "example of": "example_of", "evaluates": "evaluates",
    "deploys": "deploys", "creates": "creates", "trains": "trains",
    "runs on": "runs", "quantizes": "quantizes",
}
REL_LABELS = list(REL_LABEL_TO_PRED)


def main() -> int:
    ent_thr = float(sys.argv[1]) if len(sys.argv) > 1 else 0.35
    rel_thr = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5

    chunks = json.load(open(HERE / "recall_chunks30.json"))
    gold = json.load(open(HERE / "gold30.json"))["chunks"]
    assert len(chunks) == len(gold), "index join invalid"

    from gliner import GLiNER
    t0 = time.time()
    model = GLiNER.from_pretrained("knowledgator/gliner-relex-large-v1.0")
    model.eval()
    print(f"model loaded in {time.time()-t0:.1f}s", file=sys.stderr)

    tot = hit_pair = hit_full = hit_pair_strict = 0
    n_rel = n_ent = 0
    per_chunk = []
    t0 = time.time()

    for i, (c, g) in enumerate(zip(chunks, gold)):
        text = c["text"]
        try:
            ents, rels = model.inference(
                texts=[text], labels=ENTITY_LABELS, relations=REL_LABELS,
                threshold=ent_thr, relation_threshold=rel_thr,
                return_relations=True, flat_ner=False,
            )
        except Exception as e:  # noqa: BLE001
            print(f"ch{i+1:02d} inference failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            ents, rels = [[]], [[]]
        e0 = ents[0] if ents else []
        r0 = rels[0] if rels else []
        n_ent += len(e0)
        n_rel += len(r0)

        triples = []
        for r in r0:
            h = r.get("head"); t = r.get("tail")
            hs = h.get("text") if isinstance(h, dict) else str(h)
            ts = t.get("text") if isinstance(t, dict) else str(t)
            triples.append((hs, r.get("relation", ""), ts, r.get("score", 0.0)))

        found = []
        for gr in g["relations"]:
            tot += 1
            s, p, o = gr["s"], gr["p"], gr["o"]
            pair = full = pair_s = False
            for hs, rl, ts, _sc in triples:
                fwd = near(s, hs) and near(o, ts)
                rev = near(s, ts) and near(o, hs)
                if fwd or rev:
                    pair = True
                    if near_strict(s, hs) and near_strict(o, ts):
                        pair_s = True
                    elif near_strict(s, ts) and near_strict(o, hs):
                        pair_s = True
                    if REL_LABEL_TO_PRED.get(rl, rl) == p:
                        full = True
            hit_pair += pair; hit_full += full; hit_pair_strict += pair_s
            found.append((f"{s} -{p}-> {o}", pair, full))
        per_chunk.append((i + 1, len(e0), len(r0), triples[:6], found))

    el = time.time() - t0
    print("=" * 72)
    print(f"GLiNER-Relex  ent_thr={ent_thr}  rel_thr={rel_thr}")
    print("=" * 72)
    print(f"chunks={len(chunks)}  entities={n_ent} ({n_ent/len(chunks):.2f}/chunk)"
          f"  relations={n_rel} ({n_rel/len(chunks):.2f}/chunk)")
    print(f"wall={el:.1f}s  ({el/len(chunks):.2f}s/chunk, CPU/MPS host)")
    print()
    print(f"RECALL vs {tot} blind gold relations")
    print(f"  pair-only  (fuzzy) : {hit_pair}/{tot} = {hit_pair/tot:.3f}")
    print(f"  pair-only  (strict): {hit_pair_strict}/{tot} = {hit_pair_strict/tot:.3f}")
    print(f"  pair+predicate     : {hit_full}/{tot} = {hit_full/tot:.3f}")
    print()
    print("BASELINE, same gold, same matcher (docs/baselines/"
          "RELATION_RECALL_ATTRIBUTION_2026-07-31.md):")
    print("  frame generator ceiling : 0.229")
    print("  frame END-TO-END emitted: 0.000")
    print()
    print("SAMPLE OUTPUT")
    for n, ne, nr, tri, found in per_chunk[:8]:
        print(f"\nch{n:02d}  ents={ne} rels={nr}")
        for hs, rl, ts, sc in tri:
            print(f"     ({hs}) -{rl} {sc:.2f}-> ({ts})")
        for lbl, p, f in found:
            print(f"     GOLD {'HIT ' if p else 'MISS'}{' +pred' if f else ''}"
                  f"  {lbl}")
    json.dump({"ent_thr": ent_thr, "rel_thr": rel_thr, "total_gold": tot,
               "pair_fuzzy": hit_pair, "pair_strict": hit_pair_strict,
               "pair_and_predicate": hit_full, "entities": n_ent,
               "relations": n_rel, "wall_s": round(el, 1)},
              open(HERE / f"relex_result_{ent_thr}_{rel_thr}.json", "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
